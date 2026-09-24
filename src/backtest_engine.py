# src/backtest_engine.py
"""
Motor de backtest de PORTAFOLIO (v1): mide la estrategia ACTUAL tal como la
ejecuta run_paper.py, sobre barras históricas locales, sin red y sin broker real.

Modelo temporal (sin look-ahead)
--------------------------------
Cada vela se identifica por su timestamp de INICIO (convención de Alpaca):
la vela N de 1Min con ts=13:35 cubre [13:35, 13:36) y solo se conoce
completa a las 13:36 ("decision_ts" = ts + timeframe).

1. Al llegar al instante T se procesan, en este orden, las velas con ts=T
   de todos los símbolos:
     a) FILLS: órdenes pendientes del símbolo se llenan a la APERTURA de esta
        vela (open ± slippage). Una orden decidida con la vela N nunca se
        llena con la vela N: como mínimo con la N+1 del mismo símbolo.
     b) MARCA: el cierre de la vela actualiza la valuación del portafolio.
     c) DECISIÓN (solo si el mercado regular está abierto en decision_ts,
        como el reloj de Alpaca en vivo): estrategia, gestión y riesgo con la
        ventana de velas conocidas hasta N (inclusive), nunca N+1.
2. Empate en el mismo T: los símbolos se procesan en el orden de --symbols,
   el mismo orden del loop en vivo. Así una entrada aceptada de un símbolo
   consume apalancamiento/calor antes de evaluar el siguiente.
3. Stops, take-profit, break-even, trailing, scale-out y giveback se evalúan
   con el CIERRE de la vela (como el bot en vivo, que solo ve cierres) y se
   ejecutan en la apertura siguiente. No se usa high/low para inventar fills
   intrabarra que el bot en vivo no habría podido observar.

Qué es producción y qué se simula
---------------------------------
Reutilizado sin cambios: MACrossover (build_strategy), RiskManager
(assess_entry, update_trailing_stop, should_halt_trading, record_close),
la vista de posiciones de AlpacaRiskAdapter, parse_scale_out, los defaults
de build_arg_parser y build_risk_config de run_paper.
Simulado aquí: la secuencia de gestión de trade_one_symbol (línea a línea,
ver _manage), el broker/portafolio (sim_broker.py), el reloj de mercado y
el reinicio diario (una sesión del bot por día de mercado).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .analyze_session import timeframe_to_seconds
from .risk_manager_avanzado import RiskDecision, RiskManager, Side
from .run_paper import AlpacaRiskAdapter, build_arg_parser, build_risk_config, build_strategy, parse_scale_out
from .sim_broker import SimBroker, SimFill
from .structured_logger import classify_risk_reason

NY = "America/New_York"
RTH_OPEN_SEC = 9 * 3600 + 30 * 60
RTH_CLOSE_SEC = 16 * 3600
DELAYED_FILL_BARS = 5
BREAKEVEN_EPS = 0.005  # |P&L| menor a medio centavo = breakeven

# Flags de producción que v1 no simula (con los defaults actuales están apagados).
_UNSUPPORTED = {
    "ensemble_mode": lambda v: v != "off",
    "allow_shorts": bool,
    "enter_when_above": bool,
    "exit_when_below": bool,
    "enter_short_when_below": bool,
    "exit_short_when_above": bool,
}


def production_args(overrides: Optional[Dict[str, Any]] = None) -> argparse.Namespace:
    """Defaults EXACTOS de run_paper (sin flags), opcionalmente con la config de una sesión grabada."""
    args = build_arg_parser().parse_args([])
    for k, v in (overrides or {}).items():
        if hasattr(args, k):
            setattr(args, k, v)
    bad = [k for k, is_bad in _UNSUPPORTED.items() if is_bad(getattr(args, k))]
    if bad:
        raise ValueError(f"Backtester v1 no simula estas opciones activas: {bad}")
    return args


@dataclass
class BacktestConfig:
    symbols: List[str]
    timeframe: str = "1Min"
    start: Optional[str] = None           # fecha NY inclusiva
    end: Optional[str] = None             # fecha NY inclusiva
    initial_equity: float = 100_000.0
    slippage_bps: float = 5.0
    commission: float = 0.0
    decision_start_utc: Optional[pd.Timestamp] = None  # ventana opcional (validación de sesiones)
    decision_end_utc: Optional[pd.Timestamp] = None
    record_evaluations: bool = False


class SimRiskAdapter(AlpacaRiskAdapter):
    """Misma vista de posiciones que en vivo (position_book local); equity del portafolio simulado."""

    def __init__(self, sim: SimBroker, position_book: Dict[str, dict]):
        super().__init__(broker=None, position_book=position_book)
        self.sim = sim

    def get_equity(self) -> float:
        return self.sim.equity()


@dataclass
class _Sym:
    name: str
    order: int
    df: pd.DataFrame
    ts_ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    iso: List[str]
    decision_iso: List[str]
    decision_date: List[date]
    decision_ok: np.ndarray
    bar_in_rth: np.ndarray
    window_lo: np.ndarray


@dataclass
class BacktestResult:
    config: Dict[str, Any]
    strategy_config: Dict[str, Any]
    initial_equity: float
    final_equity: float
    trades: List[Dict[str, Any]]
    open_positions: List[Dict[str, Any]]
    unfilled_orders: List[Dict[str, Any]]
    fills: List[Dict[str, Any]]
    equity_curve: List[tuple]              # (as_of_iso, equity)
    risk_evaluations: List[Dict[str, Any]]
    counters: Dict[str, Any]
    daily_profit_halt_dates: List[str]
    loss_streak_halt_dates: List[str]
    evaluations: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _in_rth(idx: pd.DatetimeIndex) -> np.ndarray:
    ny = idx.tz_convert(NY)
    sec = ny.hour * 3600 + ny.minute * 60 + ny.second
    return np.asarray((ny.weekday < 5) & (sec >= RTH_OPEN_SEC) & (sec < RTH_CLOSE_SEC), dtype=bool)


class BacktestEngine:
    def __init__(self, config: BacktestConfig, bars: Dict[str, pd.DataFrame], args: Optional[argparse.Namespace] = None):
        self.cfg = config
        self.args = args if args is not None else production_args()
        if self.args.timeframe != config.timeframe:
            self.args.timeframe = config.timeframe
        tf = timeframe_to_seconds(config.timeframe)
        if not tf:
            raise ValueError(f"Timeframe no soportado: {config.timeframe}")
        self.tf_seconds = tf
        self.strategy = build_strategy(self.args)
        self.min_needed = self._min_needed()
        self.scale_out_levels = parse_scale_out(self.args.scale_out)

        self.sim = SimBroker(config.initial_equity, config.slippage_bps, config.commission)
        self.book: Dict[str, dict] = {}
        self.risk_cfg = build_risk_config(self.args)
        self.risk = RiskManager(self.risk_cfg, SimRiskAdapter(self.sim, self.book))
        self.session: Dict[str, Any] = {"pnl_today": 0.0, "halted": False}
        self.current_day: Optional[date] = None

        self.syms = [self._prepare(s, k, bars[s]) for k, s in enumerate(config.symbols) if s in bars]
        self.open_trades: Dict[str, Dict[str, Any]] = {}
        self.trades: List[Dict[str, Any]] = []
        self.risk_evaluations: List[Dict[str, Any]] = []
        self.evaluations: List[Dict[str, Any]] = []
        self.equity_curve: List[tuple] = []
        self.halt_dates: List[str] = []
        self.loss_streak_dates: List[str] = []
        self._trade_seq = 0
        self.counters: Dict[str, Any] = {
            "decision_bars": 0, "warmup_skips": 0,
            "signals": {s: {"BUY": 0, "SELL": 0} for s in config.symbols},
            "risk": {"ACCEPT": 0, "REJECT": 0}, "rejects_by_reason": {},
            "circuit_breaker_blocked_entries": {}, "daily_profit_halt_blocked_entries": 0,
            "sell_signals_flat_shorts_disabled": 0, "duplicate_signals_prevented": 0,
            "trailing_stop_updates": 0, "break_evens": 0, "scale_outs": 0,
            "exit_orders": {}, "delayed_fills": 0,
        }

    # ---------------- preparación ----------------
    def _min_needed(self) -> int:
        a = self.args  # mismo warm-up mínimo que trade_one_symbol
        return {"ma": max(a.fast, a.slow), "rsi": a.rsi_period + 1,
                "macd": max(a.macd_slow, a.macd_signal) + 1, "bbands": a.bb_window + 1}.get(a.strategy, 0)

    def _prepare(self, name: str, order: int, df: pd.DataFrame) -> _Sym:
        idx = df.index
        tf = pd.Timedelta(seconds=self.tf_seconds)
        decision_idx = idx + tf
        ts_ns = idx.as_unit("ns").asi8 if hasattr(idx, "as_unit") else idx.asi8
        dec_dates = list(decision_idx.tz_convert(NY).date)
        ok = _in_rth(decision_idx)
        if self.cfg.start:
            ok &= np.array([d >= date.fromisoformat(self.cfg.start) for d in dec_dates], dtype=bool)
        if self.cfg.end:
            ok &= np.array([d <= date.fromisoformat(self.cfg.end) for d in dec_dates], dtype=bool)
        if self.cfg.decision_start_utc is not None:
            ok &= np.asarray(decision_idx >= self.cfg.decision_start_utc)
        if self.cfg.decision_end_utc is not None:
            ok &= np.asarray(decision_idx <= self.cfg.decision_end_utc)
        # Ventana en vivo: velas con t >= ahora - hours_back (luego tail(lookback)).
        back_ns = int(self.args.hours_back) * 3600 * 10**9
        window_lo = np.searchsorted(ts_ns, ts_ns + self.tf_seconds * 10**9 - back_ns, side="left")
        return _Sym(
            name=name, order=order, df=df, ts_ns=ts_ns,
            open=df["open"].to_numpy(float), high=df["high"].to_numpy(float), low=df["low"].to_numpy(float),
            close=df["close"].to_numpy(float), volume=df["volume"].to_numpy(float),
            iso=[t.isoformat() for t in idx], decision_iso=[t.isoformat() for t in decision_idx],
            decision_date=dec_dates, decision_ok=ok, bar_in_rth=_in_rth(idx), window_lo=window_lo,
        )

    # ---------------- loop principal ----------------
    def run(self) -> BacktestResult:
        if not self.syms:
            raise ValueError("Ningún símbolo tiene velas en el rango solicitado.")
        all_ts = np.concatenate([s.ts_ns for s in self.syms])
        all_k = np.concatenate([np.full(len(s.ts_ns), j) for j, s in enumerate(self.syms)])
        all_i = np.concatenate([np.arange(len(s.ts_ns)) for s in self.syms])
        order = np.lexsort((all_k, all_ts))  # por timestamp y, en empate, orden de --symbols
        all_ts, all_k, all_i = all_ts[order], all_k[order], all_i[order]
        bounds = np.flatnonzero(np.diff(all_ts)) + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [len(all_ts)]])
        start_date = date.fromisoformat(self.cfg.start) if self.cfg.start else None
        end_date = date.fromisoformat(self.cfg.end) if self.cfg.end else None

        for a, b in zip(starts, ends):
            group = [(self.syms[int(all_k[j])], int(all_i[j])) for j in range(a, b)]
            for sd, i in group:                       # a) fills a la apertura
                if self.sim.has_pending(sd.name):
                    for fill in self.sim.fill_pending(sd.name, sd.iso[i], sd.open[i], self._delay):
                        self._on_fill(fill, sd, i)
            for sd, i in group:                       # b) marca al cierre
                self.sim.mark(sd.name, sd.close[i])
            for sd, i in group:                       # c) decisiones
                if sd.decision_ok[i]:
                    self._roll_day(sd.decision_date[i])
                    self._evaluate(sd, i)
            sd0, i0 = group[0]
            d0 = sd0.decision_date[i0]
            if (start_date is None or d0 >= start_date) and (end_date is None or d0 <= end_date):
                self.equity_curve.append((sd0.decision_iso[i0], self.sim.equity()))
        return self._result()

    def _delay(self, decision_iso: str, fill_iso: str) -> float:
        return (pd.Timestamp(fill_iso) - pd.Timestamp(decision_iso)).total_seconds()

    def _roll_day(self, day: date) -> None:
        """Nuevo día de mercado = nueva sesión del bot (en vivo se reinicia a diario):
        start_of_day() del RiskManager y P&L/objetivo diario a cero."""
        if day == self.current_day:
            return
        self.current_day = day
        self.risk.start_of_day()
        self.session = {"pnl_today": 0.0, "halted": False}

    # ---------------- evaluación de una vela (espejo de trade_one_symbol) ----------------
    def _evaluate(self, sd: _Sym, i: int) -> None:
        a = self.args
        lo = max(int(sd.window_lo[i]), i + 1 - a.lookback)
        if i + 1 - lo < self.min_needed:
            self.counters["warmup_skips"] += 1
            return  # en vivo: return antes de gestionar
        self.counters["decision_bars"] += 1
        result = self.strategy.evaluate(sd.df.iloc[lo:i + 1])
        sig = result.signal
        price = float(sd.close[i])
        if self.cfg.record_evaluations:
            self.evaluations.append({"symbol": sd.name, "bar_timestamp": sd.iso[i], "bar_close": price,
                                     "signal": sig, "warmup_ok": result.warmup_ok})
        if sig in ("BUY", "SELL"):
            self.counters["signals"][sd.name][sig] += 1

        def bars_dict() -> Dict[str, list]:
            return {"close": sd.close[lo:i + 1].tolist(), "high": sd.high[lo:i + 1].tolist(),
                    "low": sd.low[lo:i + 1].tolist(), "volume": sd.volume[lo:i + 1].tolist()}

        if sd.name in self.book:
            # Con posición, trade_one_symbol solo puede gestionarla/cerrarla:
            # BUY con posición = "ya estás largo"; SELL = salida (dentro de _manage).
            self._manage(sd, i, sig, price, bars_dict())
            return

        halt, why = self.risk.should_halt_trading()
        if halt:
            if sig == "BUY":
                cb = self.counters["circuit_breaker_blocked_entries"]
                cb[why] = cb.get(why, 0) + 1
            return
        if sig == "SELL":
            self.counters["sell_signals_flat_shorts_disabled"] += 1
            return
        if sig != "BUY":
            return
        if self.session.get("halted"):
            self.counters["daily_profit_halt_blocked_entries"] += 1
            return
        self._enter(sd, i, price, bars_dict())

    def _enter(self, sd: _Sym, i: int, price: float, bd: Dict[str, list]) -> None:
        decision: RiskDecision = self.risk.assess_entry(sd.name, Side.LONG, price, bd)
        accepted = bool(decision.allow and decision.qty > 0)
        code = classify_risk_reason(decision.reason)["reason_code"]
        self.counters["risk"]["ACCEPT" if accepted else "REJECT"] += 1
        if not accepted:
            rj = self.counters["rejects_by_reason"]
            rj[code] = rj.get(code, 0) + 1
        self.risk_evaluations.append({
            "symbol": sd.name, "bar_timestamp": sd.iso[i], "decision": "ACCEPT" if accepted else "REJECT",
            "reason": decision.reason, "reason_code": code, "entry_price": decision.entry,
            "stop_price": decision.stop, "take_profit": decision.take_profit,
            "position_size": decision.qty if accepted else None, "equity": self.sim.equity(),
        })
        if not accepted:
            return
        entry = decision.entry or price
        risk_ps = abs(entry - (decision.stop or price)) or (0.01 * price)
        self.book[sd.name] = {  # mismo meta que _open_position en vivo
            "side": Side.LONG, "qty": decision.qty, "entry": entry, "stop": decision.stop,
            "take": decision.take_profit, "risk_ps": risk_ps, "be_done": False, "scaled": set(),
            "peak_px": entry, "peak_pnl": 0.0, "cost_basis": None, "entry_filled_qty": 0, "realized_pnl": 0.0,
        }
        self._trade_seq += 1
        self.open_trades[sd.name] = {
            "trade_id": self._trade_seq, "symbol": sd.name, "side": "LONG",
            "entry_signal_timestamp": sd.iso[i], "entry_decision_timestamp": sd.decision_iso[i],
            "modeled_entry": entry, "initial_stop": decision.stop, "initial_take": decision.take_profit,
            "risk_per_share_modeled": risk_ps, "requested_qty": decision.qty,
            "entry_fill_timestamp": None, "entry_fill_price": None, "initial_qty": 0, "max_qty": 0,
            "entry_commission": 0.0, "legs": [],
        }
        self.sim.submit(sd.name, "buy", decision.qty, "entry", sd.iso[i], sd.decision_iso[i])

    def _manage(self, sd: _Sym, i: int, sig: Optional[str], price: float, bd: Dict[str, list]) -> None:
        """Espejo de la gestión de posición abierta de trade_one_symbol (largos)."""
        a = self.args
        meta = self.book[sd.name]
        stop, take = meta.get("stop"), meta.get("take")
        entry_px = meta.get("entry", price)
        qty = meta.get("qty", 0)

        new_stop = self.risk.update_trailing_stop(Side.LONG, price, stop or price, bd)
        if stop is None or new_stop > stop:
            meta["stop"] = new_stop
            self.counters["trailing_stop_updates"] += 1

        risk_ps = meta.get("risk_ps", max(0.01, 0.01 * price))
        r_now = (price - entry_px) / risk_ps if risk_ps > 0 else 0.0
        meta["peak_px"] = max(meta.get("peak_px", entry_px), price)
        open_pnl = (price - entry_px) * qty
        peak_pnl = (meta["peak_px"] - entry_px) * qty
        meta["peak_pnl"] = max(meta.get("peak_pnl", 0.0), peak_pnl)

        if (not meta.get("be_done")) and r_now >= a.be_at_r:
            meta["stop"] = entry_px
            meta["be_done"] = True
            self.counters["break_evens"] += 1

        for r_level, pct in self.scale_out_levels:
            key = f"R{r_level}"
            if r_now >= r_level and key not in meta.get("scaled", set()) and qty > 1:
                close_qty = max(1, int(qty * pct))
                meta.setdefault("scaled", set()).add(key)
                self.counters["scale_outs"] += 1
                self._submit_exit(sd, i, close_qty, "scale_out")
                return  # en vivo: orden en curso -> nada más para el símbolo hasta confirmarla

        if a.max_giveback_pct > 0 and meta.get("peak_pnl", 0.0) > 0 and qty > 0:
            limit = meta["peak_pnl"] * (1.0 - a.max_giveback_pct)
            if open_pnl <= limit:
                self._submit_exit(sd, i, qty, "giveback_close")
                return

        hit_stop = meta.get("stop") is not None and price <= meta["stop"]
        hit_take = take is not None and price >= take
        exit_signal = sig in ("SELL", "EXIT")
        if hit_stop or hit_take or exit_signal:
            kind = "stop_hit" if hit_stop else ("take_profit_hit" if hit_take else "signal_exit")
            self._submit_exit(sd, i, qty, kind)

    def _submit_exit(self, sd: _Sym, i: int, qty: int, purpose: str) -> None:
        ex = self.counters["exit_orders"]
        ex[purpose] = ex.get(purpose, 0) + 1
        self.sim.submit(sd.name, "sell", qty, purpose, sd.iso[i], sd.decision_iso[i])

    # ---------------- fills y contabilidad (solo con fills simulados) ----------------
    def _on_fill(self, fill: SimFill, sd: _Sym, i: int) -> None:
        if fill.delay_seconds > DELAYED_FILL_BARS * self.tf_seconds or not sd.bar_in_rth[i]:
            self.counters["delayed_fills"] += 1
        meta = self.book.get(fill.symbol)
        trade = self.open_trades.get(fill.symbol)
        if fill.side == "buy":
            meta["cost_basis"] = self.sim.cost_basis(fill.symbol)
            meta["entry_filled_qty"] = fill.qty
            trade.update(entry_fill_timestamp=fill.fill_ts, entry_fill_price=fill.price, initial_qty=fill.qty,
                         max_qty=fill.qty, entry_commission=fill.commission,
                         entry_fill_delay_seconds=fill.delay_seconds)
            if fill.realized_pnl:
                self._book_realized(fill.realized_pnl)
            return

        meta["qty"] = meta.get("qty", 0) - fill.qty
        meta["realized_pnl"] = meta.get("realized_pnl", 0.0) + fill.realized_pnl
        trade["legs"].append({"purpose": fill.purpose, "signal_bar_timestamp": fill.signal_bar_ts,
                              "decision_timestamp": fill.decision_ts, "fill_timestamp": fill.fill_ts,
                              "qty": fill.qty, "price": fill.price, "reference_open": fill.reference_open,
                              "realized_pnl": fill.realized_pnl, "commission": fill.commission,
                              "fill_delay_seconds": fill.delay_seconds})
        self._book_realized(fill.realized_pnl)
        if meta["qty"] > 0:
            return
        pnl_total = meta["realized_pnl"] - trade["entry_commission"]
        self.risk.record_close(fill.symbol, Side.LONG, meta["entry_filled_qty"], meta["cost_basis"],
                               meta.get("stop", 0.0), meta.get("take"), pnl_total)
        if self.risk.consecutive_losses == self.risk_cfg.max_consecutive_losses:
            self.loss_streak_dates.append(str(self.current_day))
        self.book.pop(fill.symbol, None)
        self.trades.append(self._finalize_trade(self.open_trades.pop(fill.symbol), pnl_total))

    def _book_realized(self, pnl: float) -> None:
        s = self.session
        s["pnl_today"] = s.get("pnl_today", 0.0) + pnl
        halt = self.args.daily_profit_halt
        if halt > 0 and not s.get("halted") and s["pnl_today"] >= halt:
            s["halted"] = True
            self.halt_dates.append(str(self.current_day))

    def _finalize_trade(self, t: Dict[str, Any], pnl_total: float) -> Dict[str, Any]:
        legs = t["legs"]
        exit_qty = sum(l["qty"] for l in legs)
        t["exit_fill_timestamp"] = legs[-1]["fill_timestamp"]
        t["exit_fill_price"] = sum(l["price"] * l["qty"] for l in legs) / exit_qty
        t["exit_reason"] = legs[-1]["purpose"]
        t["scale_outs"] = sum(1 for l in legs if l["purpose"] == "scale_out")
        t["commission"] = t["entry_commission"] + sum(l["commission"] for l in legs)
        t["realized_pnl"] = pnl_total
        initial_risk = t["risk_per_share_modeled"] * t["initial_qty"]
        t["realized_r"] = pnl_total / initial_risk if initial_risk > 0 else None
        t["holding_seconds"] = (pd.Timestamp(t["exit_fill_timestamp"]) - pd.Timestamp(t["entry_fill_timestamp"])).total_seconds()
        t["result"] = "win" if pnl_total > BREAKEVEN_EPS else ("loss" if pnl_total < -BREAKEVEN_EPS else "breakeven")
        return t

    # ---------------- resultado ----------------
    def _result(self) -> BacktestResult:
        open_positions = []
        for sym, meta in self.book.items():
            qty = self.sim.position_qty(sym)
            if qty:
                basis = self.sim.cost_basis(sym)
                last = self.sim.last_close.get(sym, basis)
                open_positions.append({"symbol": sym, "qty": qty, "cost_basis": basis, "last_close": last,
                                       "unrealized_pnl": (last - basis) * qty,
                                       "realized_pnl_so_far": meta.get("realized_pnl", 0.0),
                                       "trade": self.open_trades.get(sym)})
        unfilled = [{"symbol": o.symbol, "side": o.side, "qty": o.qty, "purpose": o.purpose,
                     "signal_bar_timestamp": o.signal_bar_ts, "decision_timestamp": o.decision_ts}
                    for o in self.sim.pending_orders()]
        fills = [f.__dict__.copy() for f in self.sim.fills]
        return BacktestResult(
            config={"symbols": self.cfg.symbols, "timeframe": self.cfg.timeframe, "start": self.cfg.start,
                    "end": self.cfg.end, "initial_equity": self.cfg.initial_equity,
                    "slippage_bps": self.cfg.slippage_bps, "commission": self.cfg.commission},
            strategy_config={k: v for k, v in vars(self.args).items()},
            initial_equity=self.cfg.initial_equity, final_equity=self.sim.equity(), trades=self.trades,
            open_positions=open_positions, unfilled_orders=unfilled, fills=fills,
            equity_curve=self.equity_curve, risk_evaluations=self.risk_evaluations, counters=self.counters,
            daily_profit_halt_dates=self.halt_dates, loss_streak_halt_dates=self.loss_streak_dates,
            evaluations=self.evaluations,
        )


def run_backtest(config: BacktestConfig, bars: Dict[str, pd.DataFrame],
                 args: Optional[argparse.Namespace] = None) -> BacktestResult:
    return BacktestEngine(config, bars, args).run()
