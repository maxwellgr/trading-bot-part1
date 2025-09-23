# src/run_paper.py
# (RiskManager Avanzado + --ignore-clock + Ensemble + Protecciones de ganancias)

import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
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

    # Circuit breakers (pérdida diaria / racha / calor de portafolio)
    halt, why = risk.should_halt_trading()
    if halt:
        logger.warning(f"[{symbol}] Trading pausado: {why}")
        print(f"🚨 [{symbol}] Trading pausado: {why}")
        time.sleep(1)
        return

    # Estado de posición local
    pos_qty = broker.get_position_qty(symbol)  # positivo=long, negativo=short, 0=flat
    has_pos = symbol in position_book

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

    # ---------- Flags por estado (MA) ----------
    if args.enter_when_above and pos_qty == 0 and ma_fast is not None and ma_slow is not None and ma_fast > ma_slow:
        side = Side.LONG
        bars_dict = {
            "close": df["close"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
        }
        decision: RiskDecision = risk.assess_entry(symbol, side, price, bars_dict)
        if decision.allow and decision.qty > 0:
            broker.cancel_open_orders(symbol)
            order = broker.place_order_market(symbol, "buy", decision.qty)
            risk_ps = abs((decision.entry or price) - (decision.stop or price)) or (0.01 * price)
            position_book[symbol] = {
                "side": side, "qty": decision.qty, "entry": decision.entry or price,
                "stop": decision.stop, "take": decision.take_profit,
                "risk_ps": risk_ps, "be_done": False, "scaled": set(),
                "peak_px": decision.entry or price, "peak_pnl": 0.0
            }
            print(f"✅ (state) BUY [{symbol}] x{decision.qty} @ {decision.entry:.2f} | SL={decision.stop:.2f} TP={decision.take_profit:.2f} | id={order.get('id','sin_id')}")
        else:
            print(f"⛔ [{symbol}] (state) BUY rechazado: {decision.reason}")
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
            side = Side.SHORT
            bars_dict = {
                "close": df["close"].tolist(),
                "high": df["high"].tolist(),
                "low": df["low"].tolist(),
                "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
            }
            decision: RiskDecision = risk.assess_entry(symbol, side, price, bars_dict)
            if decision.allow and decision.qty > 0:
                broker.cancel_open_orders(symbol)
                order = broker.place_order_market(symbol, "sell", decision.qty)
                risk_ps = abs((decision.entry or price) - (decision.stop or price)) or (0.01 * price)
                position_book[symbol] = {
                    "side": side, "qty": decision.qty, "entry": decision.entry or price,
                    "stop": decision.stop, "take": decision.take_profit,
                    "risk_ps": risk_ps, "be_done": False, "scaled": set(),
                    "peak_px": decision.entry or price, "peak_pnl": 0.0
                }
                print(f"✅ (state) SHORT [{symbol}] x{decision.qty} @ {decision.entry:.2f} | SL={decision.stop:.2f} TP={decision.take_profit:.2f} | id={order.get('id','sin_id')}")
            else:
                print(f"⛔ [{symbol}] (state) SHORT rechazado: {decision.reason}")
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
                side = Side.LONG
                bars_dict = {
                    "close": df["close"].tolist(),
                    "high": df["high"].tolist(),
                    "low": df["low"].tolist(),
                    "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
                }
                decision: RiskDecision = risk.assess_entry(symbol, side, price, bars_dict)
                if decision.allow and decision.qty > 0:
                    broker.cancel_open_orders(symbol)
                    order = broker.place_order_market(symbol, "buy", decision.qty)
                    risk_ps = abs((decision.entry or price) - (decision.stop or price)) or (0.01 * price)
                    position_book[symbol] = {
                        "side": side, "qty": decision.qty, "entry": decision.entry or price,
                        "stop": decision.stop, "take": decision.take_profit,
                        "risk_ps": risk_ps, "be_done": False, "scaled": set(),
                        "peak_px": decision.entry or price, "peak_pnl": 0.0
                    }
                    print(f"✅ BUY [{symbol}] x{decision.qty} @ {decision.entry:.2f} | SL={decision.stop:.2f} TP={decision.take_profit:.2f} | id={order.get('id','sin_id')}")
                else:
                    print(f"⛔ [{symbol}] BUY rechazado: {decision.reason}")
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
                        side = Side.SHORT
                        bars_dict = {
                            "close": df["close"].tolist(),
                            "high": df["high"].tolist(),
                            "low": df["low"].tolist(),
                            "volume": df["volume"].tolist() if "volume" in df.columns else [1_000_000] * len(df),
                        }
                        decision: RiskDecision = risk.assess_entry(symbol, side, price, bars_dict)
                        if decision.allow and decision.qty > 0:
                            broker.cancel_open_orders(symbol)
                            order = broker.place_order_market(symbol, "sell", decision.qty)
                            risk_ps = abs((decision.entry or price) - (decision.stop or price)) or (0.01 * price)
                            position_book[symbol] = {
                                "side": side, "qty": decision.qty, "entry": decision.entry or price,
                                "stop": decision.stop, "take": decision.take_profit,
                                "risk_ps": risk_ps, "be_done": False, "scaled": set(),
                                "peak_px": decision.entry or price, "peak_pnl": 0.0
                            }
                            print(f"✅ SHORT [{symbol}] x{decision.qty} @ {decision.entry:.2f} | SL={decision.stop:.2f} TP={decision.take_profit:.2f} | id={order.get('id','sin_id')}")
                        else:
                            print(f"⛔ [{symbol}] SHORT rechazado: {decision.reason}")
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

    # Libro local de posiciones con meta (entry/stop/tp) para OCO y trailing
    position_book: Dict[str, dict] = {}

    # Config de riesgo avanzada (ajústala a tu gusto)
    cfg = RiskConfig(
        account_risk_pct=0.005,     # 0.5% por trade (más conservador)
        max_positions=4,
        max_positions_per_symbol=1,
        min_rr=1.3,                 # más permisivo en rango; súbelo a 2.0 para tendencia
        use_atr_based_stop=True,
        atr_window=14,
        atr_multiple_sl=2.0,        # stop más ancho reduce tamaño y apalancamiento
        atr_multiple_tp=3.0,        # TP proporcional (RR ~1.5–2)
        trailing_atr_multiple=1.5,
        price_precision=2,
        slippage_pct=0.0005,
        min_liquidity_dollar=200_000,
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
    # Filtros de régimen
    p.add_argument("--regime-trend-filter", action="store_true", help="Activa filtro de tendencia (SMA)")
    p.add_argument("--regime-trend-window", type=int, default=200, help="Ventana SMA para filtro de tendencia")
    p.add_argument("--regime-atr-filter", action="store_true", help="Activa filtro de volatilidad (ATR/Precio)")
    p.add_argument("--regime-atr-window", type=int, default=14, help="Ventana ATR")
    p.add_argument("--regime-atr-threshold", type=float, default=0.003, help="Umbral ATR/Precio (ej. 0.003 ≈ 0.3%%)")
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
