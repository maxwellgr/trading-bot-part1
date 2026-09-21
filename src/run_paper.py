# src/run_paper.py
# (RiskManager Avanzado + --ignore-clock + Ensemble + Protecciones de ganancias)

import sys
import time
import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

from .logger import logger
from .broker_alpaca import BrokerAlpaca
from .data import bars_to_df
from .strategy import MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy

# === Risk Manager avanzado ===
from .risk_manager_avanzado import (
    RiskManager as AdvancedRiskManager,
    RiskConfig,
    Side,
    RiskDecision,
)

# === Ensemble de estrategias ===
from .ensemble import Ensemble, StrategyWrapper


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


def _open_position(
    broker: BrokerAlpaca,
    risk: AdvancedRiskManager,
    symbol: str,
    side: Side,
    price: float,
    bars_dict: Dict[str, list],
    label: str,
) -> Optional[dict]:
    """
    Evalúa una entrada con el RiskManager y, si se aprueba, coloca la orden
    de mercado y arma el registro de position_book. Centraliza lo que antes
    estaba duplicado (con pequeñas variaciones) en 4 lugares de este archivo.
    Devuelve el meta a guardar en position_book, o None si se rechazó.
    """
    decision: RiskDecision = risk.assess_entry(symbol, side, price, bars_dict)
    if not (decision.allow and decision.qty > 0):
        print(f"⛔ [{symbol}] {label} rechazado: {decision.reason}")
        return None

    broker.cancel_open_orders(symbol)
    market_side = "buy" if side == Side.LONG else "sell"
    order = broker.place_order_market(symbol, market_side, decision.qty)
    risk_ps = abs((decision.entry or price) - (decision.stop or price)) or (0.01 * price)
    meta = {
        "side": side, "qty": decision.qty, "entry": decision.entry or price,
        "stop": decision.stop, "take": decision.take_profit,
        "risk_ps": risk_ps, "be_done": False, "scaled": set(),
        "peak_px": decision.entry or price, "peak_pnl": 0.0,
    }
    print(f"✅ {label} [{symbol}] x{decision.qty} @ {decision.entry:.2f} | SL={decision.stop:.2f} TP={decision.take_profit:.2f} | id={order.get('id','sin_id')}")
    return meta


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
) -> None:
    # Verificamos si es operable
    if not broker.get_asset_tradable(symbol):
        msg = f"{symbol} no es 'tradable'. Omito este tick."
        logger.warning(msg)
        print(f"⚠️  {msg}")
        time.sleep(1)
        return

    print(f"⏳ Tick [{symbol}]: pidiendo barras…")
    bars = broker.get_bars(symbol, timeframe=timeframe, limit=lookback, start_iso=start_iso)
    df = bars_to_df(bars)
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

    # MAs opcionales para flags por estado
    ma_fast = ma_slow = None
    if args.strategy == "ma" or args.enter_when_above or args.exit_when_below or args.enter_short_when_below or args.exit_short_when_above:
        ma_fast = df["close"].rolling(args.fast).mean().iloc[-1]
        ma_slow = df["close"].rolling(args.slow).mean().iloc[-1]

    # Señal (ensemble o single)
    if ensemble is None:
        sig = strat.signal(df)
        print(f"🧭 [{symbol}] Señal: {sig or 'HOLD'}")
    else:
        sig, meta_sig = ensemble.decide(df, wrappers)  # type: ignore[arg-type]
        votes = meta_sig["votes"]; sc = meta_sig["score"]
        print(f"🧭 [{symbol}] Ensemble: {sig} | votes={votes} score={sc:.2f} | {meta_sig.get('reason','')}")

    print(f"📈 [{symbol}] Última {timeframe}: close={price:.2f}  (rows={len(df)})")
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

        # 4.2 Tomas parciales por niveles R (scale-out)
        for R_level, pct in scale_out_levels:
            key = f"R{R_level}"
            if R_now >= R_level and key not in meta.get("scaled", set()) and qty > 1:
                close_qty = max(1, int(qty * pct))
                if side == Side.LONG:
                    broker.place_order_market(symbol, "sell", close_qty)
                else:
                    broker.place_order_market(symbol, "buy", close_qty)
                meta.setdefault("scaled", set()).add(key)
                meta["qty"] = qty - close_qty
                print(f"✂️  [{symbol}] Scale-out {pct*100:.0f}% @ R={R_level:.1f} → qty={meta['qty']}")
                qty = meta["qty"]
                if qty <= 0:
                    break

        # 4.3 Límite de giveback por trade (cierre si devolvió mucho del pico)
        if args.max_giveback_pct > 0 and meta.get("peak_pnl", 0.0) > 0 and qty > 0:
            limit = meta["peak_pnl"] * (1.0 - args.max_giveback_pct)
            if open_pnl <= limit:
                if side == Side.LONG:
                    order = broker.place_order_market(symbol, "sell", qty)
                else:
                    order = broker.place_order_market(symbol, "buy", qty)
                pnl = open_pnl
                risk.record_close(symbol, side, qty, entry_px, meta.get("stop", 0.0), take, pnl)
                position_book.pop(symbol, None)
                print(f"🛡️  [{symbol}] Cierre por giveback (devuelto ≥ {args.max_giveback_pct:.0%}) | pnl={pnl:.2f} | id={order.get('id','sin_id')}")
                # objetivo diario
                session["pnl_today"] = session.get("pnl_today", 0.0) + pnl
                if args.daily_profit_halt > 0 and session["pnl_today"] >= args.daily_profit_halt:
                    session["halted"] = True
                    print(f"🧭 Objetivo diario alcanzado: +{session['pnl_today']:.2f}. Pausando nuevas entradas.")
                return

        # Chequear OCO (stop/take) o señal de salida explícita
        hit_stop = meta.get("stop") is not None and ((side == Side.LONG and price <= meta["stop"]) or (side == Side.SHORT and price >= meta["stop"]))
        hit_take = take is not None and ((side == Side.LONG and price >= take) or (side == Side.SHORT and price <= take))
        exit_signal = (sig == "SELL" and side == Side.LONG) or (sig == "BUY" and side == Side.SHORT) or (sig == "EXIT")

        if hit_stop or hit_take or exit_signal:
            close_qty = abs(pos_qty) if pos_qty != 0 else qty
            if close_qty <= 0:
                close_qty = meta.get("qty", 0)
            if side == Side.LONG:
                order = broker.place_order_market(symbol, "sell", close_qty)
            else:
                order = broker.place_order_market(symbol, "buy", close_qty)
            pnl = (price - entry_px) * close_qty if side == Side.LONG else (entry_px - price) * close_qty
            risk.record_close(symbol, side, close_qty, entry_px, meta.get("stop", 0.0), take, pnl)
            position_book.pop(symbol, None)
            print(f"✅ [{symbol}] Cierre -> qty={close_qty} pnl={pnl:.2f} | id={order.get('id','sin_id')}")
            # objetivo diario
            session["pnl_today"] = session.get("pnl_today", 0.0) + pnl
            if args.daily_profit_halt > 0 and session["pnl_today"] >= args.daily_profit_halt:
                session["halted"] = True
                print(f"🧭 Objetivo diario alcanzado: +{session['pnl_today']:.2f}. Pausando nuevas entradas.")
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
        bars_dict = {
            "close": df["close"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
        }
        meta = _open_position(broker, risk, symbol, Side.LONG, price, bars_dict, label="(state) BUY")
        if meta:
            position_book[symbol] = meta
        return

    if args.exit_when_below and pos_qty > 0 and ma_fast is not None and ma_slow is not None and ma_fast < ma_slow:
        qty = pos_qty
        broker.cancel_open_orders(symbol)
        order = broker.place_order_market(symbol, "sell", qty)
        meta = position_book.pop(symbol, {"side": Side.LONG, "qty": qty, "entry": price})
        pnl = (price - meta.get("entry", price)) * qty
        risk.record_close(symbol, Side.LONG, qty, meta.get("entry", price), meta.get("stop", 0.0), meta.get("take"), pnl)
        print(f"✅ (state) SELL [{symbol}] x{qty} -> id={order.get('id','sin_id')}")
        session["pnl_today"] = session.get("pnl_today", 0.0) + pnl
        if args.daily_profit_halt > 0 and session["pnl_today"] >= args.daily_profit_halt:
            session["halted"] = True
            print(f"🧭 Objetivo diario alcanzado: +{session['pnl_today']:.2f}. Pausando nuevas entradas.")
        return

    if args.allow_shorts and args.enter_short_when_below and pos_qty == 0 and ma_fast is not None and ma_slow is not None and ma_fast < ma_slow:
        if not broker.get_asset_shortable(symbol):
            print(f"🚫 [{symbol}] No shortable. Omito apertura de corto.")
        else:
            bars_dict = {
                "close": df["close"].tolist(),
                "high": df["high"].tolist(),
                "low": df["low"].tolist(),
                "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
            }
            meta = _open_position(broker, risk, symbol, Side.SHORT, price, bars_dict, label="(state) SHORT")
            if meta:
                position_book[symbol] = meta
        return

    if args.allow_shorts and args.exit_short_when_above and pos_qty < 0 and ma_fast is not None and ma_slow is not None and ma_fast > ma_slow:
        qty = abs(pos_qty)
        broker.cancel_open_orders(symbol)
        order = broker.place_order_market(symbol, "buy", qty)
        meta = position_book.pop(symbol, {"side": Side.SHORT, "qty": qty, "entry": price})
        pnl = (meta.get("entry", price) - price) * qty
        risk.record_close(symbol, Side.SHORT, qty, meta.get("entry", price), meta.get("stop", 0.0), meta.get("take"), pnl)
        print(f"✅ (state) COVER [{symbol}] x{qty} -> id={order.get('id','sin_id')}")
        session["pnl_today"] = session.get("pnl_today", 0.0) + pnl
        if args.daily_profit_halt > 0 and session["pnl_today"] >= args.daily_profit_halt:
            session["halted"] = True
            print(f"🧭 Objetivo diario alcanzado: +{session['pnl_today']:.2f}. Pausando nuevas entradas.")
        return

    # ---------- Ejecución por señal clásica (ensemble/single) usando RiskManager ----------
    if sig == "BUY":
        if pos_qty >= 0:
            if pos_qty > 0:
                msg = f"[{symbol}] Ya estás largo ({pos_qty})."
                logger.info(msg)
                print(f"ℹ️  {msg}")
            else:
                bars_dict = {
                    "close": df["close"].tolist(),
                    "high": df["high"].tolist(),
                    "low": df["low"].tolist(),
                    "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
                }
                meta = _open_position(broker, risk, symbol, Side.LONG, price, bars_dict, label="BUY")
                if meta:
                    position_book[symbol] = meta
        else:
            # BUY para cerrar short existente
            qty = abs(pos_qty)
            broker.cancel_open_orders(symbol)
            order = broker.place_order_market(symbol, "buy", qty)
            meta = position_book.pop(symbol, {"side": Side.SHORT, "qty": qty, "entry": price})
            pnl = (meta.get("entry", price) - price) * qty
            risk.record_close(symbol, Side.SHORT, qty, meta.get("entry", price), meta.get("stop", 0.0), meta.get("take"), pnl)
            print(f"✅ COVER [{symbol}] x{qty} -> id={order.get('id','sin_id')}")
            session["pnl_today"] = session.get("pnl_today", 0.0) + pnl
            if args.daily_profit_halt > 0 and session["pnl_today"] >= args.daily_profit_halt:
                session["halted"] = True
                print(f"🧭 Objetivo diario alcanzado: +{session['pnl_today']:.2f}. Pausando nuevas entradas.")

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
                    else:
                        bars_dict = {
                            "close": df["close"].tolist(),
                            "high": df["high"].tolist(),
                            "low": df["low"].tolist(),
                            "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
                        }
                        meta = _open_position(broker, risk, symbol, Side.SHORT, price, bars_dict, label="SHORT")
                        if meta:
                            position_book[symbol] = meta
                else:
                    msg = f"[{symbol}] Señal SELL pero shorts deshabilitados."
                    logger.info(msg)
                    print(f"ℹ️  {msg}")
        else:
            # SELL para cerrar largo existente
            qty = pos_qty
            broker.cancel_open_orders(symbol)
            order = broker.place_order_market(symbol, "sell", qty)
            meta = position_book.pop(symbol, {"side": Side.LONG, "qty": qty, "entry": price})
            pnl = (price - meta.get("entry", price)) * qty
            risk.record_close(symbol, Side.LONG, qty, meta.get("entry", price), meta.get("stop", 0.0), meta.get("take"), pnl)
            print(f"✅ SELL [{symbol}] x{qty} -> id={order.get('id','sin_id')}")
            session["pnl_today"] = session.get("pnl_today", 0.0) + pnl
            if args.daily_profit_halt > 0 and session["pnl_today"] >= args.daily_profit_halt:
                session["halted"] = True
                print(f"🧭 Objetivo diario alcanzado: +{session['pnl_today']:.2f}. Pausando nuevas entradas.")
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

    logger.info(
        "Loop multi-símbolo: %s, tf=%s, lookback=%s, strategy=%s, hours_back=%s, allow_shorts=%s, ignore_clock=%s, ensemble_mode=%s",
        symbols, args.timeframe, args.lookback, args.strategy, args.hours_back, args.allow_shorts, args.ignore_clock, args.ensemble_mode
    )
    print("🔁 Loop iniciado. CTRL+C para detener.")

    while True:
        try:
            if session.get("halted"):
                print("⏸️  Objetivo diario cumplido: pausa activa. Reanuda reiniciando o cambia --daily-profit-halt.")
                time.sleep(30)
                continue

            if not broker.get_clock_is_open() and not args.ignore_clock:
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


if __name__ == "__main__":
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
                   help="Pausa nuevas entradas al alcanzar este PnL realizado del día (USD).")

    args = p.parse_args()

    # Validación suave para 'ma'
    if args.strategy == "ma" and args.fast >= args.slow:
        print("❌ Para estrategia 'ma', fast debe ser menor que slow (ej. --fast 3 --slow 7).")
        sys.exit(2)

    main(args)
