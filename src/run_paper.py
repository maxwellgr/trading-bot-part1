# src/run_paper.py
# (RiskManager Avanzado + --ignore-clock + Ensemble + Protecciones de ganancias)

import sys
import time
import json
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

from .logger import logger
from .broker_alpaca import BrokerAlpaca
from .data import bars_to_df
from .strategy import MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy, StrategyResult

# === Risk Manager avanzado ===
from .risk_manager_avanzado import (
    RiskManager as AdvancedRiskManager,
    RiskConfig,
    Side,
    RiskDecision,
)

# === Ensemble de estrategias ===
from .ensemble import Ensemble, StrategyWrapper

# === Diagnóstico estructurado (Fase B — observability only) ===
from .structured_logger import SessionLogger
from .session_summary import format_summary

# === Guardas de ejecución (datos obsoletos + idempotencia de señales) ===
from .execution_guards import ExecutionGuards, FreshnessStatus

# === Ciclo de vida de órdenes (P&L solo con fills confirmados por Alpaca) ===
from .order_tracking import ENTRY_PURPOSE, OrderTracker, TrackedOrder, parse_alpaca_ts

# Tras enviar una orden se consulta su estado durante como mucho este tiempo
# (sin bloquear el loop); si sigue abierta, se reconcilia en ticks posteriores.
FILL_REFRESH_SECONDS = 1.0
FILL_REFRESH_INTERVAL = 0.25


# ---------------- Utilidades ----------------
def parse_symbols(single: str, plural: str) -> List[str]:
    if plural:
        syms = [s.strip().upper() for s in plural.split(",") if s.strip()]
        return [s for s in syms if s]
    return [single.strip().upper()] if single else []


def iso_utc_hours_back(hours: int) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(hours=hours))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def build_strategy(args) -> object:
    st = args.strategy.lower()
    if st == "ma":
        return MACrossover(fast=args.fast, slow=args.slow)
    if st == "rsi":
        return RSIStrategy(period=args.rsi_period, buy_level=args.rsi_buy, sell_level=args.rsi_sell)
    if st == "macd":
        return MACDStrategy(fast=args.macd_fast, slow=args.macd_slow, signal=args.macd_signal)
    if st == "bbands":
        return BollingerStrategy(window=args.bb_window, k=args.bb_k)
    raise ValueError(f"Estrategia desconocida: {args.strategy}")


def parse_weights(s: str) -> Dict[str, float]:
    out = {"ma": 1.0, "macd": 1.0, "rsi": 0.5, "bbands": 0.5}
    if not s:
        return out
    for part in s.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        try:
            out[k] = float(v)
        except ValueError:
            pass
    return out


def parse_scale_out(s: str) -> List[Tuple[float, float]]:
    """
    Formato: "1.0:0.5,2.0:0.5" -> [(1.0, 0.5), (2.0, 0.5)]
    R:porcentaje (0<pct<=1), ordena por R ascendente.
    """
    levels: List[Tuple[float, float]] = []
    if not s:
        return levels
    for part in s.split(","):
        if ":" not in part:
            continue
        r, pct = part.split(":", 1)
        try:
            R = float(r.strip())
            p = float(pct.strip())
            if R > 0 and 0 < p <= 1:
                levels.append((R, p))
        except Exception:
            pass
    levels.sort(key=lambda x: x[0])
    return levels


def _format_values(values: Dict[str, Any]) -> str:
    parts = []
    for k, v in values.items():
        parts.append(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}")
    return " ".join(parts)


def _print_strategy_detail(name: str, result: StrategyResult) -> None:
    """Solo se imprime con --explain. No afecta ninguna decisión de trading,
    únicamente muestra por qué la estrategia dio lo que dio."""
    tag = "OK" if result.warmup_ok else "WARM-UP"
    vals = _format_values(result.values)
    print(f"    · {name:<8} {(result.signal or 'HOLD'):<5} [{tag}] {result.reason}" + (f" | {vals}" if vals else ""))


def _print_ensemble_detail(meta_sig: Dict[str, Any]) -> None:
    """Solo se imprime con --explain. Desglosa el voto de cada estrategia
    del ensemble: señal, si fue vetada por el filtro de régimen, warm-up, y
    los valores del indicador que la sustentan."""
    for name, d in meta_sig["details"].items():
        tag = "OK" if d["warmup_ok"] else "WARM-UP"
        gate = " [bloqueado por filtro de régimen]" if d["gated_by_regime"] else ""
        vals = _format_values(d["values"])
        print(f"    · {name.upper():<8} {(d['signal'] or 'HOLD'):<5} [{tag}]{gate} {d['reason']}" + (f" | {vals}" if vals else ""))


# ---------------- Persistencia del estado de posiciones ----------------
# Sin esto, si el proceso se reinicia con una posición abierta en el broker,
# el bot "olvida" su stop/take/trailing y la deja corriendo sin protección
# hasta que aparezca una señal nueva. Guardamos el position_book en disco
# después de cada tick y lo reconciliamos contra el broker al arrancar.
STATE_PATH = Path("data") / "state.json"


def save_position_book(position_book: Dict[str, dict]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        serializable: Dict[str, dict] = {}
        for sym, meta in position_book.items():
            m = dict(meta)
            side = m.get("side")
            m["side"] = side.value if isinstance(side, Side) else str(side)
            m["scaled"] = sorted(m.get("scaled", set()))
            serializable[sym] = m
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:
        logger.warning(f"No se pudo guardar el estado de posiciones en {STATE_PATH}: {e}")


def load_position_book() -> Dict[str, dict]:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"No se pudo leer el estado persistido ({STATE_PATH}): {e}")
        return {}
    book: Dict[str, dict] = {}
    for sym, meta in raw.items():
        m = dict(meta)
        m["side"] = Side.LONG if m.get("side") == "LONG" else Side.SHORT
        m["scaled"] = set(m.get("scaled", []))
        book[sym] = m
    return book


def reconcile_positions(broker: BrokerAlpaca, position_book: Dict[str, dict], symbols: List[str]) -> None:
    """
    Concilia el position_book persistido/local contra la verdad del broker.
    - Si el broker no tiene posición pero el libro local sí -> se descarta
      (se cerró externamente o durante el tiempo que el bot estuvo caído).
    - Si el broker tiene una posición que el libro local desconoce -> se
      reconstruye con un stop conservador en vez de dejarla sin protección.
    - Si las cantidades no coinciden -> se ajusta al valor real del broker.
    """
    all_syms = set(symbols) | set(position_book.keys())
    for sym in all_syms:
        try:
            broker_qty = broker.get_position_qty(sym)
        except Exception as e:
            logger.warning(f"[{sym}] No se pudo verificar la posición real en el broker: {e}")
            continue

        local = position_book.get(sym)

        if broker_qty == 0 and local is not None:
            logger.warning(f"[{sym}] Libro local tenía una posición pero el broker reporta 0. Se descarta el registro local.")
            position_book.pop(sym, None)
            continue

        if broker_qty != 0 and local is None:
            side = Side.LONG if broker_qty > 0 else Side.SHORT
            qty = abs(broker_qty)
            entry_guess = None
            try:
                bars = broker.get_bars(sym, timeframe="1Min", limit=5)
                if bars:
                    entry_guess = float(bars[-1].get("c"))
            except Exception:
                entry_guess = None
            if entry_guess is None:
                try:
                    acct = broker.get_account()
                    entry_guess = float(acct.get("last_equity", 0.0)) or 1.0
                except Exception:
                    entry_guess = 1.0
            conservative_sl_pct = 0.02
            stop_guess = entry_guess * (1 - conservative_sl_pct) if side == Side.LONG else entry_guess * (1 + conservative_sl_pct)
            position_book[sym] = {
                "side": side, "qty": qty, "entry": entry_guess,
                "stop": stop_guess, "take": None,
                "risk_ps": max(0.01, abs(entry_guess - stop_guess)),
                "be_done": False, "scaled": set(),
                "peak_px": entry_guess, "peak_pnl": 0.0,
            }
            logger.warning(
                f"[{sym}] Posición huérfana detectada en el broker (qty={broker_qty}) sin registro local. "
                f"Reconstruida con stop conservador ({conservative_sl_pct:.0%}) @ {stop_guess:.2f}."
            )
            continue

        if broker_qty != 0 and local is not None:
            local_qty = local.get("qty", 0)
            if abs(broker_qty) != local_qty:
                logger.warning(f"[{sym}] Discrepancia de cantidad: broker={broker_qty} vs local={local_qty}. Ajustando al valor del broker.")
                local["qty"] = abs(broker_qty)

    save_position_book(position_book)


@dataclass
class OrderContext:
    """Todo lo que necesita el ciclo de vida de una orden para aplicar sus fills."""
    broker: Any
    orders: OrderTracker
    position_book: Dict[str, dict]
    risk: Any
    session: Dict[str, Any]
    daily_profit_halt: float
    session_logger: Optional[SessionLogger] = None


def _qty(x: float):
    """Cantidades de Alpaca llegan como float/str; enteras se guardan como int."""
    x = float(x)
    return int(x) if x.is_integer() else x


def _book_realized_pnl(ctx: OrderContext, pnl: float) -> bool:
    """
    ÚNICO punto que suma P&L realizado del día. Solo se llama con P&L de
    fills confirmados. Devuelve True si este fill activó el objetivo diario.
    """
    s = ctx.session
    s["pnl_today"] = s.get("pnl_today", 0.0) + pnl
    if ctx.daily_profit_halt > 0 and not s.get("halted") and s["pnl_today"] >= ctx.daily_profit_halt:
        s["halted"] = True
        msg = (f"Objetivo diario alcanzado con P&L confirmado: +{s['pnl_today']:.2f}. "
               f"Nuevas entradas bloqueadas; las posiciones abiertas se siguen gestionando.")
        logger.info(msg)
        print(f"🧭 {msg}")
        return True
    return False


def _place_order(
    ctx: OrderContext,
    symbol: str,
    side: str,
    qty: int,
    purpose: str,
    position_side: Side,
    bar_timestamp: Optional[str],
    cancel_first: bool = False,
) -> Tuple[dict, Optional[TrackedOrder]]:
    """
    Envía una orden de mercado y la registra para reconciliación. NO aplica
    fills ni toca el position_book: el acuse del POST (pending_new) no
    confirma ejecución. Todas las rutas de orden pasan por aquí, así que
    todas escriben order_submission + order_result.
    """
    if cancel_first:
        ctx.broker.cancel_open_orders(symbol)
    meta = ctx.position_book.get(symbol) or {}
    cost_basis = None if purpose == ENTRY_PURPOSE else meta.get("cost_basis")
    if ctx.session_logger is not None:
        ctx.session_logger.order_submission(symbol=symbol, side=side, requested_qty=qty,
                                            bar_timestamp=bar_timestamp, purpose=purpose)
    order = ctx.broker.place_order_market(symbol, side, qty) or {}
    if ctx.session_logger is not None:
        ctx.session_logger.order_result(symbol=symbol, order=order, bar_timestamp=bar_timestamp,
                                        purpose=purpose, requested_qty=qty)
    oid = order.get("id")
    if not oid:
        logger.warning(f"[{symbol}] Orden {purpose} sin id en la respuesta del broker: no se puede reconciliar "
                       f"y no se contabilizará P&L por ella.")
        return order, None
    tracked = TrackedOrder(
        order_id=str(oid), symbol=symbol, side=side, purpose=purpose, position_side=position_side.value,
        requested_qty=qty, submitted_at=order.get("submitted_at"), bar_timestamp=bar_timestamp,
        cost_basis=cost_basis, status=order.get("status"),
    )
    ctx.orders.add(tracked)
    return order, tracked


def _apply_order_snapshot(ctx: OrderContext, tracked: TrackedOrder, snapshot: Dict[str, Any]) -> None:
    """
    ÚNICO punto que aplica el estado real de una orden (GET /v2/orders/{id}):
      - entrada: fija el costo base con filled_avg_price confirmado;
      - salida / scale-out: P&L = (precio de las acciones NUEVAS - costo base) * nuevas,
        y baja meta["qty"] solo por lo realmente llenado;
      - estado terminal: deja de seguir la orden y cierra la posición solo si
        la cantidad confirmada llegó a 0 (un remanente sigue gestionado).
    Solo cuenta lo llenado desde el snapshot anterior, así que reconciliar
    el mismo estado varias veces no duplica nada.
    """
    delta = tracked.apply(snapshot)
    if not delta.changed:
        return
    sym = tracked.symbol
    meta = ctx.position_book.get(sym)
    realized: Optional[float] = None
    halt_triggered = False
    note: Optional[str] = None

    if delta.new_qty > 0:
        if tracked.is_entry:
            if meta is not None:
                meta["cost_basis"] = tracked.filled_avg_price
                meta["entry_filled_qty"] = _qty(tracked.accounted_qty)
        else:
            if tracked.cost_basis is not None:
                sign = 1.0 if tracked.position_side == Side.LONG.value else -1.0
                realized = sign * (delta.new_notional - tracked.cost_basis * delta.new_qty)
                halt_triggered = _book_realized_pnl(ctx, realized)
                if meta is not None:
                    meta["realized_pnl"] = meta.get("realized_pnl", 0.0) + realized
            else:
                note = "cost_basis_unavailable"
                logger.warning(f"[{sym}] Fill confirmado de {delta.new_qty:g} ({tracked.purpose}) sin precio de entrada "
                               f"confirmado: P&L NO contabilizado (no se estima).")
            if meta is not None:
                meta["qty"] = max(0, _qty(float(meta.get("qty", 0)) - delta.new_qty))

    latency = None
    filled_at = snapshot.get("filled_at")
    t_sub, t_fill = parse_alpaca_ts(tracked.submitted_at or snapshot.get("submitted_at")), parse_alpaca_ts(filled_at)
    if t_sub and t_fill:
        latency = round((t_fill - t_sub).total_seconds(), 3)

    if ctx.session_logger is not None:
        ctx.session_logger.order_update(
            symbol=sym, order_id=tracked.order_id, purpose=tracked.purpose, side=tracked.side,
            requested_qty=tracked.requested_qty, status=delta.status, filled_qty=tracked.accounted_qty,
            newly_filled_qty=delta.new_qty, filled_avg_price=tracked.filled_avg_price, fill_price=delta.fill_price,
            filled_at=filled_at, submitted_at=tracked.submitted_at, latency_seconds=latency, terminal=delta.terminal,
            realized_pnl=realized, cost_basis=(tracked.filled_avg_price if tracked.is_entry else tracked.cost_basis),
            pnl_today=ctx.session.get("pnl_today"), halt_triggered=halt_triggered,
            bar_timestamp=tracked.bar_timestamp, note=note,
        )
    if delta.new_qty > 0:
        pnl_txt = f" | P&L confirmado {realized:+.2f}" if realized is not None else ""
        print(f"🧾 [{sym}] Fill {tracked.purpose}: {delta.new_qty:g} @ {delta.fill_price:.4f} ({delta.status}){pnl_txt}")

    if delta.terminal:
        ctx.orders.remove(tracked.order_id)
        _finalize_order(ctx, tracked, meta)


def _finalize_order(ctx: OrderContext, tracked: TrackedOrder, meta: Optional[dict]) -> None:
    sym = tracked.symbol
    if meta is None:
        return
    if tracked.is_entry:
        filled = _qty(tracked.accounted_qty)
        if filled <= 0:
            ctx.position_book.pop(sym, None)
            msg = f"[{sym}] Entrada terminada en '{tracked.status}' sin fills: no hay posición."
            logger.warning(msg)
            print(f"⚠️  {msg}")
            if ctx.session_logger is not None:
                ctx.session_logger.position_management(sym, "entry_unfilled", {
                    "bar_timestamp": tracked.bar_timestamp, "order_id": tracked.order_id, "status": tracked.status,
                })
        elif filled != meta.get("qty"):
            logger.warning(f"[{sym}] Entrada llenada parcialmente ({filled}/{tracked.requested_qty}); se gestiona {filled}.")
            meta["qty"] = filled
        return

    if meta.get("qty", 0) <= 0:
        # Posición completamente cerrada según fills confirmados: se registra el trade completo.
        pnl_total = meta.get("realized_pnl") if meta.get("cost_basis") is not None else None
        if pnl_total is not None:
            ctx.risk.record_close(sym, meta["side"], meta.get("entry_filled_qty") or _qty(tracked.accounted_qty),
                                  meta["cost_basis"], meta.get("stop", 0.0), meta.get("take"), pnl_total)
        ctx.position_book.pop(sym, None)
        pnl_txt = f"{pnl_total:+.2f}" if pnl_total is not None else "no disponible (sin costo base confirmado)"
        print(f"✅ [{sym}] Posición cerrada ({tracked.purpose}) | P&L realizado confirmado del trade: {pnl_txt}")
        if ctx.session_logger is not None:
            ctx.session_logger.position_management(sym, "position_closed", {
                "bar_timestamp": tracked.bar_timestamp, "purpose": tracked.purpose,
                "realized_pnl": pnl_total, "order_id": tracked.order_id,
            })
    elif tracked.accounted_qty < tracked.requested_qty:
        msg = (f"[{sym}] Orden {tracked.purpose} terminó en '{tracked.status}' con {tracked.accounted_qty:g}/"
               f"{tracked.requested_qty} llenadas; el remanente ({meta['qty']}) sigue gestionado.")
        logger.warning(msg)
        print(f"⚠️  {msg}")


def reconcile_order(ctx: OrderContext, tracked: TrackedOrder) -> bool:
    """Consulta el estado real de una orden y lo aplica. False si no se pudo consultar."""
    try:
        snapshot = ctx.broker.get_order(tracked.order_id)
    except Exception as e:
        logger.warning(f"[{tracked.symbol}] No se pudo consultar la orden {tracked.order_id}: {e}")
        return False
    _apply_order_snapshot(ctx, tracked, snapshot or {})
    return True


def reconcile_pending_orders(ctx: OrderContext, symbol: Optional[str] = None) -> None:
    for tracked in ctx.orders.open_orders(symbol):
        reconcile_order(ctx, tracked)


def _refresh_order(ctx: OrderContext, tracked: Optional[TrackedOrder]) -> None:
    """Consulta breve justo tras el envío (≤ FILL_REFRESH_SECONDS). Si la orden
    sigue abierta, queda registrada y se reconcilia en los ticks siguientes."""
    if tracked is None:
        return
    deadline = time.monotonic() + FILL_REFRESH_SECONDS
    while ctx.orders.get(tracked.order_id) is not None:
        if not reconcile_order(ctx, tracked):
            return
        if ctx.orders.get(tracked.order_id) is None or time.monotonic() >= deadline:
            return
        time.sleep(FILL_REFRESH_INTERVAL)


def _submit_and_refresh(ctx: OrderContext, symbol: str, side: str, qty: int, purpose: str,
                        position_side: Side, bar_timestamp: Optional[str], cancel_first: bool = False) -> dict:
    order, tracked = _place_order(ctx, symbol, side, qty, purpose, position_side, bar_timestamp, cancel_first)
    _refresh_order(ctx, tracked)
    return order


def _open_position(
    ctx: OrderContext,
    symbol: str,
    side: Side,
    price: float,
    bars_dict: Dict[str, list],
    label: str,
    signal: Optional[str] = None,
    bar_timestamp: Optional[str] = None,
) -> Optional[dict]:
    """
    Evalúa una entrada con el RiskManager y, si se aprueba, coloca la orden
    de mercado y arma el registro de position_book. Centraliza lo que antes
    estaba duplicado (con pequeñas variaciones) en 4 lugares de este archivo.
    Devuelve el meta guardado en position_book, o None si se rechazó/bloqueó.

    Con el objetivo diario alcanzado (session["halted"]) no se abre nada:
    es la única puerta de las 4 rutas de entrada.

    session_logger es puramente observacional: registra la misma decisión
    que ya se imprime por consola, no participa en ella.
    """
    risk, session_logger = ctx.risk, ctx.session_logger
    if ctx.session.get("halted"):
        msg = (f"[{symbol}] {label} bloqueado: objetivo diario alcanzado (P&L confirmado "
               f"{ctx.session.get('pnl_today', 0.0):+.2f}). Solo se gestionan posiciones abiertas.")
        logger.info(msg)
        print(f"🧭 {msg}")
        if session_logger is not None:
            session_logger.execution_guard(symbol=symbol, bar_timestamp=bar_timestamp, side=signal or side.value,
                                           action="entry", guard="DAILY_PROFIT_HALT",
                                           detail=f"pnl_today={ctx.session.get('pnl_today', 0.0):.2f}")
        return None

    decision: RiskDecision = risk.assess_entry(symbol, side, price, bars_dict)

    if session_logger is not None:
        session_logger.risk_evaluation(
            symbol=symbol,
            side=side.value,
            signal=signal,
            decision="ACCEPT" if (decision.allow and decision.qty > 0) else "REJECT",
            reason=decision.reason,
            entry_price=decision.entry,
            stop_price=decision.stop,
            take_profit=decision.take_profit,
            position_size=decision.qty if decision.allow else None,
            atr=decision.meta.get("atr") if decision.meta else None,
            extra={"risk_reward_meta": decision.meta.get("rr") if decision.meta else None},
            bar_timestamp=bar_timestamp,
        )

    if not (decision.allow and decision.qty > 0):
        print(f"⛔ [{symbol}] {label} rechazado: {decision.reason}")
        return None

    market_side = "buy" if side == Side.LONG else "sell"
    order, tracked = _place_order(ctx, symbol, market_side, decision.qty, ENTRY_PURPOSE, side,
                                  bar_timestamp, cancel_first=True)
    risk_ps = abs((decision.entry or price) - (decision.stop or price)) or (0.01 * price)
    # "entry" es el precio MODELADO (vela + slippage) y sigue alimentando R,
    # break-even y stops como antes. El P&L realizado usa "cost_basis", que
    # solo se fija con el filled_avg_price confirmado por Alpaca.
    meta = {
        "side": side, "qty": decision.qty, "entry": decision.entry or price,
        "stop": decision.stop, "take": decision.take_profit,
        "risk_ps": risk_ps, "be_done": False, "scaled": set(),
        "peak_px": decision.entry or price, "peak_pnl": 0.0,
        "cost_basis": None, "entry_filled_qty": 0, "realized_pnl": 0.0,
    }
    ctx.position_book[symbol] = meta
    print(f"✅ {label} [{symbol}] x{decision.qty} orden enviada (ref. modelada {decision.entry:.2f}) | "
          f"SL={decision.stop:.2f} TP={decision.take_profit:.2f} | id={order.get('id','sin_id')}")
    _refresh_order(ctx, tracked)
    return ctx.position_book.get(symbol)


def _execution_gate(
    guards: Optional[ExecutionGuards],
    freshness: Optional[FreshnessStatus],
    symbol: str,
    bar_ts: Optional[str],
    side: str,
    action: str,
    session_logger: Optional[SessionLogger] = None,
) -> bool:
    """
    Única puerta entre una señal ACCIONABLE (BUY/SELL, por señal o por flag
    de estado) y RiskManager/broker. No cambia la señal: solo decide si este
    intento concreto puede continuar.
      1) Datos obsoletos -> no se abre/cierra nada por señal.
      2) Misma (símbolo, vela, lado) ya procesada -> no se repite.
    Devuelve True si el intento puede seguir. Sin guards (tests/uso
    antiguo) siempre True.
    """
    if guards is None:
        return True
    if freshness is not None and freshness.is_stale:
        msg = (f"[{symbol}] {action} {side} bloqueado: datos obsoletos "
               f"(vela {bar_ts}, edad {freshness.age_seconds:.0f}s > {freshness.threshold_seconds:.0f}s).")
        logger.info(msg)
        print(f"🧊 {msg}")
        if session_logger is not None:
            session_logger.execution_guard(symbol=symbol, bar_timestamp=bar_ts, side=side, action=action,
                                           guard="STALE_DATA",
                                           detail=f"age={freshness.age_seconds:.0f}s threshold={freshness.threshold_seconds:.0f}s")
        return False
    if not guards.dedup.try_acquire(symbol, bar_ts, side):
        msg = f"[{symbol}] {action} {side} ya procesado para la vela {bar_ts}; se omite (idempotencia)."
        logger.info(msg)
        print(f"🔁 {msg}")
        if session_logger is not None:
            session_logger.execution_guard(symbol=symbol, bar_timestamp=bar_ts, side=side, action=action,
                                           guard="DUPLICATE_SIGNAL")
        return False
    return True


# ---------------- Adapter para el RiskManager ----------------
class AlpacaRiskAdapter:
    """
    Envuelve BrokerAlpaca para exponer la API mínima que exige RiskManager:
      - get_equity()
      - get_open_positions()  -> usamos un position_book local para tener stop/tp
      - get_open_orders()     -> no usado aquí
      - round_qty(qty, lot_size)
    """
    def __init__(self, broker: BrokerAlpaca, position_book: Dict[str, dict]):
        self.broker = broker
        self.position_book = position_book

    def get_equity(self) -> float:
        try:
            acct = self.broker.get_account()
            return float(acct.get("equity", 0.0))
        except Exception:
            return 0.0

    def get_open_positions(self):
        out = []
        for sym, p in self.position_book.items():
            out.append({
                "symbol": sym,
                "qty": p.get("qty", 0) if p.get("side") == Side.LONG else -p.get("qty", 0),
                "avg_price": p.get("entry", 0.0),
                "side": p.get("side"),
                "stop": p.get("stop"),
            })
        return out

    def get_open_orders(self):
        return []

    def round_qty(self, qty: float, lot_size: int) -> int:
        return max(lot_size, int(qty // lot_size * lot_size))


# ---------------- Lógica principal de trading ----------------
def trade_one_symbol(
    broker: BrokerAlpaca,
    risk: AdvancedRiskManager,
    strat: object,
    symbol: str,
    timeframe: str,
    lookback: int,
    start_iso: str,
    args,
    position_book: Dict[str, dict],
    ensemble: Optional[Ensemble],
    wrappers: Optional[List[StrategyWrapper]],
    scale_out_levels: List[Tuple[float, float]],
    session: Dict[str, Any],
    session_logger: Optional[SessionLogger] = None,
    guards: Optional[ExecutionGuards] = None,
    market_open: bool = True,
    orders: Optional[OrderTracker] = None,
) -> None:
    ctx = OrderContext(
        broker=broker, orders=orders if orders is not None else OrderTracker(),
        position_book=position_book, risk=risk, session=session,
        daily_profit_halt=float(getattr(args, "daily_profit_halt", 0.0) or 0.0),
        session_logger=session_logger,
    )
    # Fills pendientes de este símbolo primero: el P&L y las cantidades que
    # se usen abajo salen de lo que Alpaca confirmó, no de lo que se envió.
    if ctx.orders.has_open(symbol):
        reconcile_pending_orders(ctx, symbol)

    # Verificamos si es operable
    if not broker.get_asset_tradable(symbol):
        msg = f"{symbol} no es 'tradable'. Omito este tick."
        logger.warning(msg)
        print(f"⚠️  {msg}")
        time.sleep(1)
        return

    print(f"⏳ Tick [{symbol}]: pidiendo barras…")
    bars = broker.get_bars(symbol, timeframe=timeframe, limit=lookback, start_iso=start_iso)
    # get_bars ya devuelve las `lookback` más recientes en orden cronológico;
    # tail() lo garantiza también aquí (nunca las más antiguas).
    df = bars_to_df(bars).tail(lookback)
    if df.empty:
        logger.warning(f"[{symbol}] Sin barras.")
        print(f"⚠️  [{symbol}] Sin barras.")
        time.sleep(1)
        return

    # Warm-up mínimo según estrategia base
    min_needed = 0
    if args.strategy == "ma":
        min_needed = max(args.fast, args.slow)
    elif args.strategy == "rsi":
        min_needed = args.rsi_period + 1
    elif args.strategy == "macd":
        min_needed = max(args.macd_slow, args.macd_signal) + 1
    elif args.strategy == "bbands":
        min_needed = args.bb_window + 1

    if len(df) < min_needed:
        msg = f"[{symbol}] Warm-up {len(df)}/{min_needed} velas."
        logger.info(msg)
        print(f"⏳ {msg}")
        time.sleep(1)
        return

    last = df.iloc[-1]
    price = float(last["close"])
    bar_ts = df.index[-1].isoformat() if len(df.index) else None

    # MAs opcionales para flags por estado
    ma_fast = ma_slow = None
    if args.strategy == "ma" or args.enter_when_above or args.exit_when_below or args.enter_short_when_below or args.exit_short_when_above:
        ma_fast = df["close"].rolling(args.fast).mean().iloc[-1]
        ma_slow = df["close"].rolling(args.slow).mean().iloc[-1]

    # Señal (ensemble o single)
    if ensemble is None:
        result = strat.evaluate(df)  # evaluate()/signal() dan la MISMA señal (ver strategy.py); evaluate() solo suma diagnóstico.
        sig = result.signal
        warmup_note = "" if result.warmup_ok else " ⏳(warm-up)"
        print(f"🧭 [{symbol}] Señal: {sig or 'HOLD'}{warmup_note}")
        if args.explain:
            _print_strategy_detail(args.strategy.upper(), result)
        if session_logger is not None:
            session_logger.strategy_evaluation(
                symbol=symbol, timeframe=timeframe, bar_timestamp=bar_ts, bar_close=price,
                strategy=args.strategy, signal=result.signal, raw_signal=result.signal,
                reason=result.reason, warmup_ok=result.warmup_ok, gated_by_regime=False,
                indicator_values=result.values,
            )
    else:
        sig, meta_sig = ensemble.decide(df, wrappers)  # type: ignore[arg-type]
        votes = meta_sig["votes"]; sc = meta_sig["score"]
        warmup_note = ""
        if meta_sig.get("any_warmup_pending"):
            pending = [n.upper() for n, d in meta_sig["details"].items() if not d["warmup_ok"]]
            warmup_note = f" | ⏳ en warm-up: {','.join(pending)}"
        print(f"🧭 [{symbol}] Ensemble: {sig} | votes={votes} score={sc:.2f} | {meta_sig.get('reason','')}{warmup_note}")
        if args.explain:
            _print_ensemble_detail(meta_sig)
        if session_logger is not None:
            for name, d in meta_sig["details"].items():
                session_logger.strategy_evaluation(
                    symbol=symbol, timeframe=timeframe, bar_timestamp=bar_ts, bar_close=price,
                    strategy=name, signal=d["signal"], raw_signal=d["raw_signal"],
                    reason=d["reason"], warmup_ok=d["warmup_ok"], gated_by_regime=d["gated_by_regime"],
                    indicator_values=d["values"],
                )
            session_logger.ensemble_decision(
                symbol=symbol, timeframe=timeframe, bar_timestamp=bar_ts, bar_close=price,
                ensemble_mode=ensemble.mode, signal=sig, votes=votes, score=sc,
                threshold=ensemble.min_score, reason=meta_sig.get("reason", ""),
                any_warmup_pending=meta_sig.get("any_warmup_pending", False),
            )

    print(f"📈 [{symbol}] Última {timeframe}: close={price:.2f}  (rows={len(df)})")

    # ---------- Frescura de datos (capa de seguridad, no cambia la señal) ----------
    freshness: Optional[FreshnessStatus] = None
    if guards is not None:
        freshness = guards.freshness.observe(symbol, df.index[-1].to_pydatetime(), datetime.now(timezone.utc), market_open)
        if freshness.event in ("stale", "still_stale"):
            msg = (f"[{symbol}] Datos {timeframe} OBSOLETOS: última vela {bar_ts} con edad {freshness.age_seconds:.0f}s "
                   f"(umbral {freshness.threshold_seconds:.0f}s; sin avanzar hace {freshness.unchanged_seconds:.0f}s). "
                   f"Señales BUY/SELL bloqueadas hasta que llegue una vela nueva.")
            logger.warning(msg)
            print(f"⚠️  {msg}")
        elif freshness.event == "recovered":
            logger.info(f"[{symbol}] Datos {timeframe} vuelven a estar al día (vela {bar_ts}).")
        if freshness.event is not None and session_logger is not None:
            session_logger.data_freshness(
                symbol=symbol, timeframe=timeframe, bar_timestamp=bar_ts, status=freshness.event,
                age_seconds=freshness.age_seconds, threshold_seconds=freshness.threshold_seconds,
                unchanged_seconds=freshness.unchanged_seconds,
            )

    def gate(order_side: str, action: str) -> bool:
        return _execution_gate(guards, freshness, symbol, bar_ts, order_side, action, session_logger)
    if args.debug_ma and ma_fast is not None and ma_slow is not None:
        print(f"🧮 [{symbol}] MA_fast({args.fast})={ma_fast:.4f} | MA_slow({args.slow})={ma_slow:.4f}")

    # NOTA: los circuit breakers (should_halt_trading) se evalúan MÁS ABAJO,
    # justo antes de abrir posiciones nuevas — nunca aquí arriba. Si se
    # evaluaran en este punto, un `return` temprano dejaría posiciones YA
    # ABIERTAS sin trailing stop, sin break-even y sin chequeo de stop/take
    # exactamente cuando el sistema decidió que el riesgo es alto. Un
    # circuit breaker debe bloquear ENTRADAS nuevas, no la gestión de lo
    # que ya está expuesto.

    # Estado de posición local
    pos_qty = broker.get_position_qty(symbol)  # positivo=long, negativo=short, 0=flat
    has_pos = symbol in position_book

    # Orden de este símbolo aún sin estado final: la cantidad del broker está
    # cambiando y el P&L no está confirmado. No se ajusta drift, no se gestiona
    # ni se envía otra orden (evita vender dos veces lo mismo) hasta que
    # Alpaca confirme; se reconcilia en el próximo tick.
    if ctx.orders.has_open(symbol):
        open_ids = ", ".join(f"{o.purpose}:{o.status}" for o in ctx.orders.open_orders(symbol))
        msg = f"[{symbol}] Orden en curso sin confirmar ({open_ids}); se espera a Alpaca antes de gestionar u operar."
        logger.info(msg)
        print(f"⏳ {msg}")
        return

    # Guarda contra drift: si el libro local cree tener posición pero el
    # broker ya no la tiene (cerrada externamente, o por cualquier motivo
    # fuera del bot), no sigas gestionando un fantasma — descarta el
    # registro para no tomar decisiones de riesgo sobre datos falsos.
    if has_pos and pos_qty == 0:
        logger.warning(f"[{symbol}] position_book decía posición abierta pero el broker reporta 0. Descartando registro local.")
        position_book.pop(symbol, None)
        has_pos = False
    elif has_pos:
        expected_qty = position_book[symbol].get("qty", 0)
        if abs(pos_qty) != expected_qty:
            logger.warning(f"[{symbol}] Drift de cantidad detectado (broker={pos_qty}, local={expected_qty}). Ajustando al valor del broker.")
            position_book[symbol]["qty"] = abs(pos_qty)

    # ---------- Gestión de posiciones abiertas: trailing + protecciones ----------
    if has_pos:
        meta = position_book[symbol]
        side: Side = meta["side"]
        stop: Optional[float] = meta.get("stop")
        take: Optional[float] = meta.get("take")
        entry_px: float = meta.get("entry", price)
        qty: int = meta.get("qty", abs(pos_qty) if pos_qty != 0 else 0) or meta.get("qty", 0)

        # Trailing ATR (según RM)
        bars_dict = {
            "close": df["close"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
        }
        new_stop = risk.update_trailing_stop(side, price, stop or price, bars_dict)
        if stop is None or (side == Side.LONG and new_stop > stop) or (side == Side.SHORT and new_stop < stop):
            meta["stop"] = new_stop
            print(f"🔧 [{symbol}] Trailing stop -> {new_stop:.2f}")
            if session_logger is not None:
                session_logger.position_management(symbol, "trailing_stop_update", {
                    "bar_timestamp": bar_ts, "previous_stop": stop, "new_stop": new_stop, "price": price,
                })

        # ---------- Protección de ganancias ----------
        risk_ps = meta.get("risk_ps", max(0.01, 0.01 * price))  # riesgo por acción
        # R actual
        if side == Side.LONG:
            R_now = (price - entry_px) / risk_ps if risk_ps > 0 else 0.0
        else:
            R_now = (entry_px - price) / risk_ps if risk_ps > 0 else 0.0

        # High-watermark y PnL abierto/pico
        if side == Side.LONG:
            meta["peak_px"] = max(meta.get("peak_px", entry_px), price)
            open_pnl = (price - entry_px) * qty
            peak_pnl = (meta["peak_px"] - entry_px) * qty
        else:
            meta["peak_px"] = min(meta.get("peak_px", entry_px), price)
            open_pnl = (entry_px - price) * qty
            peak_pnl = (entry_px - meta["peak_px"]) * qty

        meta["peak_pnl"] = max(meta.get("peak_pnl", 0.0), peak_pnl)

        # 4.1 Break-even al alcanzar R objetivo
        if (not meta.get("be_done")) and (R_now >= args.be_at_r):
            meta["stop"] = entry_px
            meta["be_done"] = True
            print(f"🏁 [{symbol}] Break-even activado @ {entry_px:.2f} (R={R_now:.2f})")
            if session_logger is not None:
                session_logger.position_management(symbol, "break_even", {
                    "bar_timestamp": bar_ts, "entry_price": entry_px, "r_multiple": R_now,
                })

        # 4.2 Tomas parciales por niveles R (scale-out)
        for R_level, pct in scale_out_levels:
            key = f"R{R_level}"
            if R_now >= R_level and key not in meta.get("scaled", set()) and qty > 1:
                close_qty = max(1, int(qty * pct))
                meta.setdefault("scaled", set()).add(key)
                print(f"✂️  [{symbol}] Scale-out {pct*100:.0f}% @ R={R_level:.1f}: orden de {close_qty} "
                      f"(qty se ajusta con el fill confirmado)")
                if session_logger is not None:
                    session_logger.position_management(symbol, "scale_out", {
                        "bar_timestamp": bar_ts, "r_level": R_level, "pct": pct,
                        "requested_qty": close_qty, "qty_before": qty,
                    })
                _submit_and_refresh(ctx, symbol, "sell" if side == Side.LONG else "buy", close_qty,
                                    "scale_out", side, bar_ts)
                # Sin confirmación todavía -> nada más para este símbolo en este tick.
                if ctx.orders.has_open(symbol) or symbol not in position_book:
                    return
                qty = meta["qty"]
                if qty <= 0:
                    break

        # 4.3 Límite de giveback por trade (cierre si devolvió mucho del pico)
        if args.max_giveback_pct > 0 and meta.get("peak_pnl", 0.0) > 0 and qty > 0:
            limit = meta["peak_pnl"] * (1.0 - args.max_giveback_pct)
            if open_pnl <= limit:
                print(f"🛡️  [{symbol}] Cierre por giveback (devuelto ≥ {args.max_giveback_pct:.0%}): orden de {qty} "
                      f"(P&L se contabiliza con el fill confirmado)")
                if session_logger is not None:
                    session_logger.position_management(symbol, "giveback_close", {
                        "bar_timestamp": bar_ts, "peak_pnl": meta.get("peak_pnl", 0.0),
                        "max_giveback_pct": args.max_giveback_pct, "qty": qty,
                    })
                _submit_and_refresh(ctx, symbol, "sell" if side == Side.LONG else "buy", qty,
                                    "giveback_close", side, bar_ts)
                return

        # Chequear OCO (stop/take) o señal de salida explícita
        hit_stop = meta.get("stop") is not None and ((side == Side.LONG and price <= meta["stop"]) or (side == Side.SHORT and price >= meta["stop"]))
        hit_take = take is not None and ((side == Side.LONG and price >= take) or (side == Side.SHORT and price <= take))
        exit_signal = (sig == "SELL" and side == Side.LONG) or (sig == "BUY" and side == Side.SHORT) or (sig == "EXIT")
        # Stop/take son protección y NO pasan por la guarda; solo la salida
        # disparada por señal se bloquea con datos obsoletos o si ya se intentó.
        # Si se bloquea, no hay nada más que hacer en este tick (las rutas de
        # abajo usarían la misma clave y serían bloqueadas igual).
        if exit_signal and not (hit_stop or hit_take) and not gate("SELL" if side == Side.LONG else "BUY", "signal_exit"):
            return

        if hit_stop or hit_take or exit_signal:
            close_qty = abs(pos_qty) if pos_qty != 0 else qty
            if close_qty <= 0:
                close_qty = meta.get("qty", 0)
            exit_kind = "stop_hit" if hit_stop else ("take_profit_hit" if hit_take else "signal_exit")
            print(f"📤 [{symbol}] Cierre ({exit_kind}): orden de {close_qty} (P&L se contabiliza con el fill confirmado)")
            if session_logger is not None:
                session_logger.position_management(symbol, "exit", {
                    "bar_timestamp": bar_ts, "exit_kind": exit_kind, "qty": close_qty,
                })
            _submit_and_refresh(ctx, symbol, "sell" if side == Side.LONG else "buy", close_qty,
                                exit_kind, side, bar_ts)
            return

    # ---------- Circuit breakers: bloquean SOLO la apertura de posiciones nuevas ----------
    # Se evalúan aquí (después de gestionar/cerrar lo que ya estaba abierto arriba)
    # para que una pérdida diaria, racha negativa o calor de portafolio excesivo
    # detenga nuevas entradas sin dejar huérfanas las posiciones existentes.
    halt, why = risk.should_halt_trading()
    if halt:
        logger.warning(f"[{symbol}] Nuevas entradas pausadas: {why}")
        print(f"🚨 [{symbol}] Nuevas entradas pausadas: {why}")
        time.sleep(1)
        return

    # ---------- Flags por estado (MA) ----------
    if args.enter_when_above and pos_qty == 0 and ma_fast is not None and ma_slow is not None and ma_fast > ma_slow:
        if gate("BUY", "state_entry"):
            bars_dict = {
                "close": df["close"].tolist(),
                "high": df["high"].tolist(),
                "low": df["low"].tolist(),
                "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
            }
            _open_position(ctx, symbol, Side.LONG, price, bars_dict, label="(state) BUY",
                           signal="BUY", bar_timestamp=bar_ts)
        return

    if args.exit_when_below and pos_qty > 0 and ma_fast is not None and ma_slow is not None and ma_fast < ma_slow:
        if gate("SELL", "state_exit"):
            qty = pos_qty
            order = _submit_and_refresh(ctx, symbol, "sell", qty, "state_exit", Side.LONG, bar_ts, cancel_first=True)
            print(f"📤 (state) SELL [{symbol}] x{qty} -> orden enviada id={order.get('id','sin_id')}")
        return

    if args.allow_shorts and args.enter_short_when_below and pos_qty == 0 and ma_fast is not None and ma_slow is not None and ma_fast < ma_slow:
        if not broker.get_asset_shortable(symbol):
            print(f"🚫 [{symbol}] No shortable. Omito apertura de corto.")
        elif gate("SELL", "state_short_entry"):
            bars_dict = {
                "close": df["close"].tolist(),
                "high": df["high"].tolist(),
                "low": df["low"].tolist(),
                "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
            }
            _open_position(ctx, symbol, Side.SHORT, price, bars_dict, label="(state) SHORT",
                           signal="SELL", bar_timestamp=bar_ts)
        return

    if args.allow_shorts and args.exit_short_when_above and pos_qty < 0 and ma_fast is not None and ma_slow is not None and ma_fast > ma_slow:
        if gate("BUY", "state_cover"):
            qty = abs(pos_qty)
            order = _submit_and_refresh(ctx, symbol, "buy", qty, "state_cover", Side.SHORT, bar_ts, cancel_first=True)
            print(f"📤 (state) COVER [{symbol}] x{qty} -> orden enviada id={order.get('id','sin_id')}")
        return

    # ---------- Ejecución por señal clásica (ensemble/single) usando RiskManager ----------
    if sig == "BUY":
        if pos_qty >= 0:
            if pos_qty > 0:
                msg = f"[{symbol}] Ya estás largo ({pos_qty})."
                logger.info(msg)
                print(f"ℹ️  {msg}")
            elif gate("BUY", "signal_entry"):
                bars_dict = {
                    "close": df["close"].tolist(),
                    "high": df["high"].tolist(),
                    "low": df["low"].tolist(),
                    "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
                }
                _open_position(ctx, symbol, Side.LONG, price, bars_dict, label="BUY",
                               signal="BUY", bar_timestamp=bar_ts)
        elif gate("BUY", "signal_cover"):
            # BUY para cerrar short existente
            qty = abs(pos_qty)
            order = _submit_and_refresh(ctx, symbol, "buy", qty, "signal_cover", Side.SHORT, bar_ts, cancel_first=True)
            print(f"📤 COVER [{symbol}] x{qty} -> orden enviada id={order.get('id','sin_id')}")

    elif sig == "SELL":
        if pos_qty <= 0:
            if pos_qty < 0:
                msg = f"[{symbol}] Ya estás en short ({pos_qty}). No incremento."
                logger.info(msg)
                print(f"ℹ️  {msg}")
            else:
                if args.allow_shorts:
                    if not broker.get_asset_shortable(symbol):
                        print(f"🚫 [{symbol}] No shortable. Ignoro apertura de corto.")
                    elif gate("SELL", "signal_short_entry"):
                        bars_dict = {
                            "close": df["close"].tolist(),
                            "high": df["high"].tolist(),
                            "low": df["low"].tolist(),
                            "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
                        }
                        _open_position(ctx, symbol, Side.SHORT, price, bars_dict, label="SHORT",
                                       signal="SELL", bar_timestamp=bar_ts)
                else:
                    msg = f"[{symbol}] Señal SELL pero shorts deshabilitados."
                    logger.info(msg)
                    print(f"ℹ️  {msg}")
        elif gate("SELL", "signal_exit"):
            # SELL para cerrar largo existente
            qty = pos_qty
            order = _submit_and_refresh(ctx, symbol, "sell", qty, "signal_exit", Side.LONG, bar_ts, cancel_first=True)
            print(f"📤 SELL [{symbol}] x{qty} -> orden enviada id={order.get('id','sin_id')}")
    else:
        msg = f"[{symbol}] Sin señal."
        logger.info(msg)
        print(msg)


# ---------------- Main ----------------
def main(args: argparse.Namespace) -> None:
    symbols = parse_symbols(args.symbol, args.symbols)
    if not symbols:
        print("❌ Debes indicar --symbol TICKER o --symbols A,B,C")
        sys.exit(2)

    print(f"▶️ Iniciando bot: symbols={symbols}, tf={args.timeframe}, lookback={args.lookback}, strategy={args.strategy}")

    if args.dry_run:
        logger.info("Dry-run activo. Saliendo sin conectar a broker.")
        print("Dry run OK ✅")
        return

    try:
        broker = BrokerAlpaca()
    except Exception as e:
        logger.error(f"No se pudo inicializar BrokerAlpaca: {e}")
        print(f"Error inicializando broker: {e}")
        sys.exit(1)

    is_open = broker.get_clock_is_open()
    if not is_open:
        if args.ignore_clock:
            print("⚠️  Mercado cerrado según Alpaca, pero --ignore-clock activo; usando histórico.")
        else:
            print("⏸️  Mercado cerrado según Alpaca. El bot seguirá revisando cada 60s.")
    else:
        print("✅ Mercado abierto.")

    acct = broker.get_account()
    equity = float(acct.get("equity", 10_000))

    # Libro local de posiciones con meta (entry/stop/tp) para OCO y trailing.
    # Se carga desde disco (data/state.json) y se concilia contra el broker
    # para no perder la protección de posiciones abiertas si el proceso
    # se reinició (ver save_position_book / reconcile_positions).
    position_book: Dict[str, dict] = load_position_book()
    reconcile_positions(broker, position_book, symbols)

    # Config de riesgo avanzada (todos los parámetros son ajustables por CLI,
    # ver --help; los valores por defecto reproducen los que antes estaban
    # hardcodeados aquí).
    cfg = RiskConfig(
        account_risk_pct=args.risk_per_trade,
        max_positions=args.max_positions,
        max_positions_per_symbol=1,
        max_portfolio_heat_pct=args.max_portfolio_heat,
        max_leverage=args.max_leverage,
        daily_loss_limit_pct=args.daily_loss_limit_pct,
        max_consecutive_losses=args.max_consecutive_losses,
        min_rr=args.min_rr,                 # más permisivo en rango; súbelo a 2.0 para tendencia
        use_atr_based_stop=True,
        atr_window=14,
        atr_multiple_sl=args.atr_sl_mult,   # stop más ancho reduce tamaño y apalancamiento
        atr_multiple_tp=args.atr_tp_mult,   # TP proporcional (RR ~1.5–2)
        trailing_atr_multiple=args.trailing_atr_mult,
        price_precision=2,
        slippage_pct=0.0005,
        min_liquidity_dollar=args.min_liquidity,
        max_symbol_exposure_pct=args.max_symbol_exposure,
    )

    risk = AdvancedRiskManager(cfg, AlpacaRiskAdapter(broker, position_book))
    risk.start_of_day()

    # Estrategia base (compatibilidad con CLI)
    strat = build_strategy(args)

    # Ensemble (si está activo)
    wrappers: Optional[List[StrategyWrapper]] = None
    ensemble: Optional[Ensemble] = None
    if args.ensemble_mode != "off":
        w = parse_weights(args.ensemble_weights)
        strat_ma = MACrossover(fast=args.fast, slow=args.slow)
        strat_macd = MACDStrategy(fast=args.macd_fast, slow=args.macd_slow, signal=args.macd_signal)
        strat_rsi = RSIStrategy(period=args.rsi_period, buy_level=args.rsi_buy, sell_level=args.rsi_sell)
        strat_bb = BollingerStrategy(window=args.bb_window, k=args.bb_k)

        wrappers = [
            StrategyWrapper("ma", strat_ma, w.get("ma", 1.0)),
            StrategyWrapper("macd", strat_macd, w.get("macd", 1.0)),
            StrategyWrapper("rsi", strat_rsi, w.get("rsi", 0.5)),
            StrategyWrapper("bbands", strat_bb, w.get("bbands", 0.5)),
        ]

        ensemble = Ensemble(
            mode=args.ensemble_mode,
            k=args.ensemble_k,
            min_score=args.ensemble_min_score,
            primary="ma",
            use_trend_filter=args.regime_trend_filter,
            trend_window=args.regime_trend_window,
            use_atr_filter=args.regime_atr_filter,
            atr_window=args.regime_atr_window,
            atr_threshold=args.regime_atr_threshold,
            require_no_opposition=args.ensemble_require_no_opposition,
        )

    # Protección de ganancias: parseo de scale-out y sesión
    scale_out_levels = parse_scale_out(args.scale_out)
    session: Dict[str, Any] = {"pnl_today": 0.0, "halted": False}

    # Diagnóstico estructurado (Fase B): un archivo JSONL por corrida, con la
    # config completa al inicio (vars(args) capta cualquier flag existente o
    # futuro sin tener que listarlos a mano y arriesgarse a que se desactualice).
    session_logger = SessionLogger()
    session_config = dict(vars(args))
    session_config["symbols_parsed"] = symbols
    session_config["starting_equity"] = equity
    session_logger.session_start(session_config)
    print(f"🗒️  Diagnóstico estructurado: {session_logger.path}")

    # Guardas de ejecución: frescura de datos + idempotencia por
    # (símbolo, vela, lado). Viven toda la sesión (estado en memoria).
    guards = ExecutionGuards(args.timeframe)

    # Órdenes enviadas aún sin estado final en Alpaca (solo en memoria; ver
    # _finish_session para qué pasa si el proceso se detiene con alguna abierta).
    orders = OrderTracker()

    logger.info(
        "Loop multi-símbolo: %s, tf=%s, lookback=%s, strategy=%s, hours_back=%s, allow_shorts=%s, ignore_clock=%s, ensemble_mode=%s",
        symbols, args.timeframe, args.lookback, args.strategy, args.hours_back, args.allow_shorts, args.ignore_clock, args.ensemble_mode
    )
    print("🔁 Loop iniciado. CTRL+C para detener.")

    # Cierre ordenado: pase lo que pase, UN session_end con el resumen.
    # Ctrl+C (incluso durante el back-off de 10s tras un error) = parada
    # manual; cualquier otra excepción se registra como motivo y se relanza.
    end_reason = "loop_exit"
    try:
        _run_loop(args, symbols, broker, risk, strat, position_book, ensemble, wrappers,
                  scale_out_levels, session, session_logger, guards, orders)
        end_reason = "manual_stop"
    except KeyboardInterrupt:
        logger.info("Bot detenido manualmente.")
        print("🛑 Bot detenido manualmente.")
        end_reason = "manual_stop"
    except BaseException as e:
        end_reason = f"exception:{type(e).__name__}"
        raise
    finally:
        _finish_session(session_logger, end_reason, orders)


def _finish_session(session_logger: SessionLogger, reason: str, orders: Optional[OrderTracker] = None) -> None:
    """
    Escribe el único session_end (idempotente), imprime el resumen y cierra el JSONL.

    Órdenes aún abiertas al cerrar: NO se inventa ningún fill ni P&L. Se listan
    en session_end.unresolved_orders para revisarlas en Alpaca. El seguimiento
    no se persiste: tras reiniciar, reconcile_positions() ajusta cantidades a
    la verdad del broker, pero el P&L de esos fills no entra en pnl_today.
    """
    extra = None
    if orders is not None and len(orders):
        pending = [o.summary() for o in orders.open_orders()]
        extra = {"unresolved_orders": pending}
        msg = (f"{len(pending)} orden(es) sin estado final al cerrar: no se contabiliza P&L por ellas. "
               f"Revísalas en Alpaca: " + ", ".join(f"{o['symbol']} {o['purpose']} {o['order_id']}" for o in pending))
        logger.warning(msg)
        print(f"⚠️  {msg}")
    try:
        summary = session_logger.session_end(reason, extra=extra)
        if summary is not None:
            print(format_summary(summary, reason))
    except Exception as e:
        logger.warning(f"No se pudo escribir el resumen de sesión: {e}")
    finally:
        session_logger.close()


def _run_loop(args, symbols, broker, risk, strat, position_book, ensemble, wrappers,
              scale_out_levels, session, session_logger, guards, orders: Optional[OrderTracker] = None) -> None:
    """
    Loop principal. Retorna tras Ctrl+C.

    El objetivo diario (session["halted"]) NO detiene el loop: solo bloquea
    entradas nuevas (en _open_position). Stops, take-profit, trailing,
    break-even, scale-outs, salidas y la reconciliación de órdenes siguen.
    """
    orders = orders if orders is not None else OrderTracker()
    ctx = OrderContext(broker=broker, orders=orders, position_book=position_book, risk=risk, session=session,
                       daily_profit_halt=float(args.daily_profit_halt or 0.0), session_logger=session_logger)
    while True:
        try:
            # Fills diferidos: se reconcilian en cada pasada, también con el mercado cerrado.
            if len(orders):
                reconcile_pending_orders(ctx)
                save_position_book(position_book)

            if session.get("halted"):
                print(f"⏸️  Objetivo diario cumplido (P&L confirmado +{session.get('pnl_today', 0.0):.2f}): "
                      f"sin entradas nuevas; se siguen gestionando posiciones y órdenes abiertas.")

            market_open = broker.get_clock_is_open()
            if not market_open and not args.ignore_clock:
                msg = "Mercado cerrado. Reintentando en 60s."
                logger.info(msg)
                print(f"⏸️  {msg}")
                time.sleep(60)
                continue

            start_iso = iso_utc_hours_back(args.hours_back)

            for sym in symbols:
                try:
                    trade_one_symbol(
                        broker=broker,
                        risk=risk,
                        strat=strat,
                        symbol=sym,
                        timeframe=args.timeframe,
                        lookback=args.lookback,
                        start_iso=start_iso,
                        args=args,
                        position_book=position_book,
                        ensemble=ensemble,
                        wrappers=wrappers,
                        scale_out_levels=scale_out_levels,
                        session=session,
                        session_logger=session_logger,
                        guards=guards,
                        market_open=market_open,
                        orders=orders,
                    )
                except Exception as e_sym:
                    logger.exception(f"Error procesando [{sym}]: {e_sym}")
                    print(f"❌ Error en símbolo [{sym}]: {e_sym}")
                finally:
                    # Persistimos el estado tras cada símbolo: si el proceso
                    # muere entre ticks, el próximo arranque reconcilia desde
                    # el último estado conocido en vez de partir en blanco.
                    save_position_book(position_book)

            time.sleep(args.poll_seconds)

        except KeyboardInterrupt:
            logger.info("Bot detenido manualmente.")
            print("🛑 Bot detenido manualmente.")
            break
        except Exception as e:
            logger.exception(f"Error en loop principal: {e}")
            print(f"❌ Error en loop: {e}")
            time.sleep(10)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Paper-trading multi-símbolo (Alpaca) con estrategias, ensemble, control de riesgo avanzado y protecciones de ganancias")
    # símbolos
    p.add_argument("--symbol", type=str, default="AAPL")
    p.add_argument("--symbols", type=str, default="")
    # datos
    p.add_argument("--timeframe", type=str, default="1Min")
    p.add_argument("--lookback", type=int, default=120)
    p.add_argument("--hours-back", type=int, default=24)
    p.add_argument("--poll-seconds", type=int, default=10)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--ignore-clock", action="store_true", help="No pausar aunque el mercado esté cerrado (usa histórico)")
    # estrategia base (compatibilidad)
    p.add_argument("--strategy", type=str, default="ma", choices=["ma", "rsi", "macd", "bbands"])
    # MA params (usados por 'ma' y para flags por estado)
    p.add_argument("--fast", type=int, default=3, help="MA rápida")
    p.add_argument("--slow", type=int, default=7, help="MA lenta")
    p.add_argument("--debug-ma", action="store_true")
    p.add_argument("--explain", action="store_true",
                   help="Imprime, en cada tick, por qué cada estrategia (o el ensemble) dio BUY/SELL/HOLD: "
                        "razón, valores de indicadores, y si el motivo fue warm-up insuficiente. "
                        "Solo diagnóstico — no cambia ninguna decisión de trading.")
    # RSI params
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--rsi-buy", type=float, default=30.0)
    p.add_argument("--rsi-sell", type=float, default=70.0)
    # MACD params
    p.add_argument("--macd-fast", type=int, default=12)
    p.add_argument("--macd-slow", type=int, default=26)
    p.add_argument("--macd-signal", type=int, default=9)
    # Bollinger params
    p.add_argument("--bb-window", type=int, default=20)
    p.add_argument("--bb-k", type=float, default=2.0)
    # flags largos por estado
    p.add_argument("--enter-when-above", action="store_true", help="Entrar largo si MA_fast > MA_slow")
    p.add_argument("--exit-when-below", action="store_true", help="Salir de largo si MA_fast < MA_slow")
    # shorts
    p.add_argument("--allow-shorts", action="store_true", help="Permite abrir cortos si el símbolo es shortable")
    p.add_argument("--enter-short-when-below", action="store_true", help="Entrar en corto si MA_fast < MA_slow")
    p.add_argument("--exit-short-when-above", action="store_true", help="Cubrir corto si MA_fast > MA_slow")
    # === Ensemble CLI ===
    p.add_argument("--ensemble-mode", type=str, default="off",
                   choices=["off", "consensus", "weighted", "stacked"],
                   help="Modo de combinación de estrategias")
    p.add_argument("--ensemble-k", type=int, default=2, help="k para consensus/stacked")
    p.add_argument("--ensemble-weights", type=str, default="ma=1,macd=1,rsi=0.5,bbands=0.5",
                   help="Pesos para modo weighted (ej: ma=1,macd=1,rsi=0.5,bbands=0.5)")
    p.add_argument("--ensemble-min-score", type=float, default=1.0,
                   help="Umbral de score para modo weighted")
    p.add_argument("--ensemble-require-no-opposition", action="store_true",
                   help="En modo weighted, además del umbral de score exige 0 votos en contra "
                        "(comportamiento estricto anterior; por defecto el score manda, como indica su nombre).")
    # Filtros de régimen
    p.add_argument("--regime-trend-filter", action="store_true", help="Activa filtro de tendencia (SMA)")
    p.add_argument("--regime-trend-window", type=int, default=200, help="Ventana SMA para filtro de tendencia")
    p.add_argument("--regime-atr-filter", action="store_true", help="Activa filtro de volatilidad (ATR/Precio)")
    p.add_argument("--regime-atr-window", type=int, default=14, help="Ventana ATR")
    p.add_argument("--regime-atr-threshold", type=float, default=0.003, help="Umbral ATR/Precio (ej. 0.003 ≈ 0.3%%)")
    # === RiskManager avanzado (antes hardcodeado en el código; ahora ajustable) ===
    p.add_argument("--risk-per-trade", type=float, default=0.005,
                   help="Fracción de equity arriesgada por trade (Fixed Fractional). Default 0.005 = 0.5%%.")
    p.add_argument("--max-positions", type=int, default=4, help="Máximo de posiciones simultáneas.")
    p.add_argument("--min-rr", type=float, default=1.3,
                   help="Riesgo/beneficio mínimo para aceptar una entrada. Súbelo (ej. 2.0) para mercados en tendencia.")
    p.add_argument("--atr-sl-mult", type=float, default=2.0, help="Múltiplo de ATR para el stop-loss inicial.")
    p.add_argument("--atr-tp-mult", type=float, default=3.0, help="Múltiplo de ATR para el take-profit inicial.")
    p.add_argument("--trailing-atr-mult", type=float, default=1.5, help="Múltiplo de ATR para el trailing stop.")
    p.add_argument("--min-liquidity", type=float, default=200_000.0,
                   help="Volumen $ promedio mínimo (ventana liq_window) para aceptar una entrada.")
    p.add_argument("--max-portfolio-heat", type=float, default=0.2,
                   help="Suma de riesgos abiertos / equity antes de bloquear nuevas entradas.")
    p.add_argument("--max-leverage", type=float, default=1.5, help="Exposición bruta máxima como múltiplo de equity.")
    p.add_argument("--max-symbol-exposure", type=float, default=0.1,
                   help="Exposición bruta máxima por símbolo / equity.")
    p.add_argument("--daily-loss-limit-pct", type=float, default=0.03,
                   help="Detiene NUEVAS entradas si equity cae esta fracción respecto al inicio del día.")
    p.add_argument("--max-consecutive-losses", type=int, default=3,
                   help="Detiene NUEVAS entradas tras N pérdidas seguidas.")
    # === Protecciones de ganancias ===
    p.add_argument("--be-at-r", type=float, default=1.0,
                   help="Mueve el stop a break-even al alcanzar este múltiplo R.")
    p.add_argument("--scale-out", type=str, default="1.0:0.5,2.0:0.5",
                   help="Tomas parciales como R:porcentaje, ej: 1.0:0.5,2.0:0.5")
    p.add_argument("--max-giveback-pct", type=float, default=0.5,
                   help="Cierra si devuelve más de esta fracción (0–1) del PnL pico por trade.")
    p.add_argument("--daily-profit-halt", type=float, default=300.0,
                   help="Pausa nuevas entradas al alcanzar este PnL realizado del día (USD), calculado solo "
                        "con fills confirmados por Alpaca. Las posiciones abiertas se siguen gestionando.")
    return p


if __name__ == "__main__":
    args = build_arg_parser().parse_args()

    # Validación suave para 'ma'
    if args.strategy == "ma" and args.fast >= args.slow:
        print("❌ Para estrategia 'ma', fast debe ser menor que slow (ej. --fast 3 --slow 7).")
        sys.exit(2)

    main(args)
