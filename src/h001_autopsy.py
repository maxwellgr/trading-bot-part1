# src/h001_autopsy.py
"""
H001 DEVELOPMENT AUTOPSY — diagnóstico de SOLO LECTURA de STRATEGY_V2_HYPOTHESIS_001
sobre el split DEVELOPMENT (2024-01-02 → 2025-12-31). H001 está cerrada
(REJECTED_AT_DEVELOPMENT): nada aquí cambia reglas, constantes, riesgo ni vivo.

    python -m src.h001_autopsy --data-dir data/historical --output-dir data/research_v1/h001_autopsy

Qué hace
--------
1. Re-corre H001 SOLO en development, con la MISMA config que src/research_h001
   (datos cargados como máximo hasta el fin de development), usando un
   _ObservingEngine: subclase de BacktestEngine que solo OBSERVA qué rama toma
   cada BUY (en posición / circuit breaker / halt de ganancia diaria / riesgo).
   Verifica que trades.json del re-run es idéntico byte a byte al guardado en
   data/research_v1/h001_development (prueba de que observar no cambió nada).
2. Calcula features de cada señal con velas <= vela de señal (sin look-ahead):
   profundidad/estructura del pullback, madurez de tendencia, gatillo, volatilidad,
   hora. Las métricas de ejecución (fill, gap) son POST-decisión y se etiquetan así.
3. Agrega: embudo, MFE/MAE de perdedores, motivos de salida, grupos de pullback,
   edad de tendencia, gatillo, quintiles ATR%, hora, símbolo, separaciones (CLES),
   4 cross-tabs, sesgo señal vs trade aceptado, y H001 vs MA_BASELINE_V1 (development).

Umbrales descriptivos FIJADOS ANTES de ver resultados (no se ajustan):
- Profundidad (distancia mínima low-EMA20 en ATR): penetrated <= 0; close (0, 0.25]; loose (0.25, 0.50].
- Dirección del pullback (retorno neto open(T-5) -> close(T-1) en ATR): down <= -0.25; up >= +0.25; si no, sideways.
- Pendiente EMA50 "flat": |slope3/3| / ATR < 0.02 (convención existente de backtest_entry_quality).
- Edad de tendencia (velas continuas con EMA20 > EMA50): 0-5, 6-12, 13-24, 25-48, 49+.
- Ubicación del cierre del gatillo: top_quarter >= 0.75; upper_half [0.5, 0.75); below_mid < 0.5.
- Hora (decisión ET): 10:00-10:30, 10:30-11:00, 11:00-12:00, 12:00-14:00, 14:00-16:00.
- "Falla inmediata": MFE < +0.25R (convención de backtest_excursion).
Cuantiles (ATR% quintiles, terciles de fuerza del gatillo) se calculan sobre trades completados.
max_pullback_10bar_high_atr usa las 10 velas previas a T y puede cruzar a la sesión anterior (el resto de
features del pullback usan solo P = T-5..T-1, que por §5.1 está en la misma sesión).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .backtest_engine import BacktestConfig, BacktestEngine, production_args
from .backtest_entry_quality import cles, numeric_stats
from .backtest_report import _profit_factor, to_json
from .historical_data import load_symbol_bars
from .research_protocol import DEFAULT_PROTOCOL_PATH, ProtocolError, get_split, load_protocol
from .strategy_v2_h001 import (
    ENGINE_LOOKBACK, HYPOTHESIS_ID, SUPPORT_BARS, TIMEFRAME, TOUCH_ATR_MULT, HygieneError, TrendPullbackH001,
    _atr_at, _own_window_ema, conditions_at, resample_rth_5min, split_bars,
)

NY = "America/New_York"
DEPTH_GROUPS = ("penetrated", "close", "loose")
TREND_AGE_BUCKETS = (("0-5", 0, 5), ("6-12", 6, 12), ("13-24", 13, 24), ("25-48", 25, 48), ("49+", 49, 10**9))
TIME_BUCKETS = (("10:00-10:30", 600, 630), ("10:30-11:00", 630, 660), ("11:00-12:00", 660, 720),
                ("12:00-14:00", 720, 840), ("14:00-16:00", 840, 960))
DIRECTION_ATR = 0.25
FLAT_SLOPE_ATR_PER_BAR = 0.02
IMMEDIATE_FAIL_R = 0.25
REACH = (("reached_0_25r", 0.25), ("reached_0_50r", 0.5), ("reached_1_00r", 1.0), ("reached_1_50r", 1.5),
         ("reached_2_00r", 2.0))
EXIT_REASONS = ("giveback_close", "stop_hit", "take_profit_hit")
STORED_RUN = Path("data") / "research_v1" / "h001_development"
MA_BENCHMARK = Path("data") / "research_v1" / "benchmark_periods.json"
DISCLAIMER = ("Descriptive DEVELOPMENT-only diagnostics of a CLOSED hypothesis. No threshold, filter or parameter "
              "is recommended; correlation is not causation; nothing here was evaluated on validation/known/forward.")


# ================================================================ higiene
def development_scope(protocol: Dict[str, Any], split_name: str = "development") -> Dict[str, Any]:
    """Solo el split DEVELOPMENT del protocolo. Cualquier otro (o un rol distinto) se rechaza, sin bypass."""
    try:
        split = get_split(protocol, split_name)
    except ProtocolError as e:
        raise HygieneError(str(e)) from e
    if split["role"] != "development" or split_name != "development":
        raise HygieneError(f"la autopsia de {HYPOTHESIS_ID} solo acepta DEVELOPMENT; "
                           f"'{split_name}' (rol {split['role']}) está prohibido")
    return split


def _end_utc(day: str) -> pd.Timestamp:
    return (pd.Timestamp(day) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")


def _start_utc(day: str) -> pd.Timestamp:
    return pd.Timestamp(day).tz_localize(NY).tz_convert("UTC")


# ================================================================ re-run observado
class _ObservingEngine(BacktestEngine):
    """Observa (no modifica) la rama que toma cada BUY en _evaluate, por deltas de contadores."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.signal_stages: List[Dict[str, Any]] = []

    def _evaluate(self, sd, i):
        n_eval, n_risk = len(self.evaluations), len(self.risk_evaluations)
        in_pos = sd.name in self.book
        cb_before = dict(self.counters["circuit_breaker_blocked_entries"])
        dph_before = self.counters["daily_profit_halt_blocked_entries"]
        super()._evaluate(sd, i)
        if len(self.evaluations) == n_eval or self.evaluations[-1]["signal"] != "BUY":
            return
        cb_after = self.counters["circuit_breaker_blocked_entries"]
        changed = [k for k in cb_after if cb_after[k] != cb_before.get(k, 0)]
        if in_pos:
            stage, reason = "in_position", None
        elif changed:
            stage, reason = "circuit_breaker", changed[0]
        elif self.counters["daily_profit_halt_blocked_entries"] != dph_before:
            stage, reason = "daily_profit_halt", None
        elif len(self.risk_evaluations) > n_risk:
            ev = self.risk_evaluations[-1]
            stage, reason = ("risk_accepted", None) if ev["decision"] == "ACCEPT" else ("risk_rejected", ev["reason_code"])
        else:
            stage, reason = "unclassified", None
        self.signal_stages.append({"symbol": sd.name, "bar_timestamp": sd.iso[i], "stage": stage, "reason": reason})


def load_development_bars(data_dir: Path, symbols: Sequence[str], split: Dict[str, Any]) -> Dict[str, Any]:
    """1Min cargado SOLO hasta el fin de development -> 5Min RTH -> soporte (200) + split, igual que research_h001."""
    end = _end_utc(split["end"])
    bars, full5, info = {}, {}, {}
    for sym in symbols:
        b1 = load_symbol_bars(data_dir, "1Min", sym, None, end)
        b5 = resample_rth_5min(b1)
        part = split_bars(b5, split["start"], split["end"])
        if part["support_bars"] < SUPPORT_BARS:
            raise HygieneError(f"{sym}: soporte insuficiente ({part['support_bars']})")
        if len(part["bars"]) and part["bars"].index[-1] >= end:
            raise HygieneError(f"{sym}: velas posteriores a development")
        bars[sym] = part["bars"][["open", "high", "low", "close", "volume"]]
        full5[sym] = part["bars"]
        info[sym] = {"support_bars": part["support_bars"], "split_bars": part["split_bars"]}
    return {"bars": bars, "info": info}


def rerun_development(protocol: Dict[str, Any], split: Dict[str, Any], bars: Dict[str, pd.DataFrame]):
    ex = protocol["execution_defaults"]
    symbols = list(protocol["universe"]["symbols"])
    cfg = BacktestConfig(symbols=symbols, timeframe=TIMEFRAME, start=split["start"], end=split["end"],
                         initial_equity=float(ex["initial_equity"]), slippage_bps=float(ex["slippage_bps"]),
                         commission=float(ex["commission_per_fill"]), record_evaluations=True,
                         window_hours_limit=False)
    eng = _ObservingEngine(cfg, bars, production_args({"lookback": ENGINE_LOOKBACK}), TrendPullbackH001())
    return eng.run(), eng.signal_stages


# ================================================================ indicadores por vela (idénticos a la estrategia)
def symbol_indicators(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    o, h, l, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    idx = list(range(len(df)))
    e20 = _own_window_ema(c, idx, 20)
    e50 = _own_window_ema(c, idx, 50)
    atr = np.array([_atr_at(h, l, c, i) for i in idx])
    ny = df.index.tz_convert(NY)
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "e20": e20, "e50": e50, "atr": atr,
            "date": np.asarray(ny.date), "minute": np.asarray(ny.hour * 60 + ny.minute),
            "ts": np.asarray([t.isoformat() for t in df.index])}


def _run_length(mask: np.ndarray, t: int) -> Tuple[int, bool]:
    """Velas consecutivas terminando en t con mask True; (largo, truncado_por_inicio_de_historia)."""
    n = 0
    while t - n >= 0 and mask[t - n]:
        n += 1
    return n, t - n < 0


def _depth_group(d: float) -> str:
    if d <= 0:
        return "penetrated"
    if d <= 0.25:
        return "close"
    if d <= 0.50:
        return "loose"
    return "beyond_0_50"   # no debería ocurrir: la regla de toque exige <= 0.50 en alguna vela


def _trend_age_bucket(n: int) -> str:
    return next(name for name, lo, hi in TREND_AGE_BUCKETS if lo <= n <= hi)


def time_bucket(decision_minute: int) -> Optional[str]:
    return next((name for name, lo, hi in TIME_BUCKETS if lo <= decision_minute < hi), None)


def _loc_bucket(loc: Optional[float]) -> Optional[str]:
    if loc is None:
        return None
    return "top_quarter" if loc >= 0.75 else ("upper_half" if loc >= 0.5 else "below_mid")


def _div(a: float, b: float) -> Optional[float]:
    return None if b is None or b == 0 or not math.isfinite(b) else a / b


def signal_features(ind: Dict[str, np.ndarray], t: int) -> Dict[str, Any]:
    """Features en la vela de señal t usando SOLO velas <= t."""
    o, h, l, c, v, e20, e50, atr = (ind[k] for k in ("o", "h", "l", "c", "v", "e20", "e50", "atr"))
    a = atr[t]
    P = list(range(t - 5, t))
    dist = [(l[i] - e20[i]) / atr[i] for i in P]
    dmin = min(dist)
    net = c[t - 1] - o[t - 5]
    run_hi5 = [max(h[t - 5:i + 1]) - l[i] for i in P]
    W10 = list(range(max(0, t - 10), t))
    run_hi10 = [max(h[W10[0]:i + 1]) - l[i] for i in W10]
    down = 0
    k = t - 1
    while k - 1 >= 0 and c[k] < c[k - 1] and k >= t - 5:
        down += 1
        k -= 1
    age, age_trunc = _run_length(e20 > e50, t)
    above50, above_trunc = _run_length(c > e50, t)
    rng = h[t] - l[t]
    loc = float((c[t] - l[t]) / rng) if rng > 0 else None
    slope50 = (e50[t] - e50[t - 3]) / a
    minute = int(ind["minute"][t]) + 5
    return {
        # pullback: profundidad
        "pullback_min_distance_ema20_atr": dmin,
        "pullback_touched_or_crossed_ema20": bool(dmin <= 0),
        "pullback_crossed_below_ema20": bool(dmin < 0),
        "depth_group": _depth_group(dmin),
        "deepest_low_vs_ema50_atr": min((l[i] - e50[i]) / atr[i] for i in P),
        # pullback: estructura
        "n_down_closes": int(sum(1 for i in P if c[i] < c[i - 1])),
        "n_red": int(sum(1 for i in P if c[i] < o[i])),
        "n_green": int(sum(1 for i in P if c[i] > o[i])),
        "consecutive_down_before_trigger": down,
        "max_pullback_5bar_high_atr": max(run_hi5) / a,
        "max_pullback_10bar_high_atr": max(run_hi10) / a,
        "pullback_range_atr": (max(h[i] for i in P) - min(l[i] for i in P)) / a,
        "pullback_net_return_pct": (c[t - 1] / o[t - 5] - 1) * 100,
        "pullback_net_return_atr": net / a,
        "any_pullback_close_below_ema20": bool(any(c[i] < e20[i] for i in P)),
        "all_pullback_closes_above_ema20": bool(all(c[i] > e20[i] for i in P)),
        "pullback_direction": "down" if net / a <= -DIRECTION_ATR else ("up" if net / a >= DIRECTION_ATR else "sideways"),
        # tendencia
        "trend_age_bars": int(age), "trend_age_truncated": bool(age_trunc), "trend_age_bucket": _trend_age_bucket(age),
        "bars_since_bullish_crossover": None if age_trunc else int(age - 1),
        "bars_close_above_ema50": int(above50), "close_above_ema50_truncated": bool(above_trunc),
        "spread_atr": (e20[t] - e50[t]) / a,
        "ema20_slope_atr": (e20[t] - e20[t - 3]) / a,
        "ema50_slope_atr": slope50,
        "ema50_slope_direction": "flat" if abs(slope50 / 3) < FLAT_SLOPE_ATR_PER_BAR else "rising",
        "close_above_ema20_atr": (c[t] - e20[t]) / a,
        "close_above_ema50_atr": (c[t] - e50[t]) / a,
        # gatillo
        "trigger_body_atr": abs(c[t] - o[t]) / a,
        "trigger_range_atr": rng / a,
        "trigger_close_location": loc,
        "trigger_location_bucket": _loc_bucket(loc),
        "trigger_in_upper_25": bool(loc is not None and loc >= 0.75),
        "trigger_in_upper_50": bool(loc is not None and loc >= 0.5),
        "trigger_below_mid": bool(loc is not None and loc < 0.5),
        "breakout_distance_atr": (c[t] - h[t - 1]) / a,
        "trigger_return_pct": (c[t] / c[t - 1] - 1) * 100,
        "volume_ratio_5": _div(v[t], float(np.mean(v[t - 5:t]))),
        "volume_ratio_20": _div(v[t], float(np.mean(v[max(0, t - 20):t]))) if t >= 1 else None,
        # volatilidad / hora
        "atr": a, "atr_pct": a / c[t] * 100,
        "decision_minute_et": minute, "time_bucket": time_bucket(minute),
        "signal_close": c[t], "ema20": e20[t], "ema50": e50[t],
    }


# ================================================================ trades
def trade_execution_metrics(trade: Dict[str, Any], feat: Dict[str, Any],
                            entry_fill: Dict[str, Any]) -> Dict[str, Any]:
    """Métricas POST-decisión (fill real): persecución, gap y costo de slippage en R."""
    a = feat["atr"]
    fill = float(trade["entry_fill_price"])
    risk = float(trade["risk_per_share_modeled"]) * float(trade["initial_qty"])
    slip = (entry_fill["price"] - entry_fill["reference_open"]) * entry_fill["qty"]
    slip += sum((leg["reference_open"] - leg["price"]) * leg["qty"] for leg in trade["legs"])
    return {"entry_fill_distance_atr": (fill - feat["signal_close"]) / a,
            "next_bar_gap_atr": (entry_fill["reference_open"] - feat["signal_close"]) / a,
            "entry_above_ema20_atr": (fill - feat["ema20"]) / a,
            "entry_above_ema50_atr": (fill - feat["ema50"]) / a,
            "slippage_cost_usd": slip, "slippage_cost_r": slip / risk if risk > 0 else None}


def trade_row(trade: Dict[str, Any], feat: Dict[str, Any], execm: Dict[str, Any]) -> Dict[str, Any]:
    mfe = trade.get("mfe_r")
    row = {"trade_id": trade["trade_id"], "symbol": trade["symbol"], "entry_signal_timestamp": trade["entry_signal_timestamp"],
           "entry_fill_timestamp": trade["entry_fill_timestamp"], "exit_fill_timestamp": trade["exit_fill_timestamp"],
           "exit_reason": trade["exit_reason"], "result": trade["result"], "realized_pnl": trade["realized_pnl"],
           "realized_r": trade["realized_r"], "mfe_r": mfe, "mae_r": trade.get("mae_r"),
           "mfe_timestamp": trade.get("mfe_timestamp"), "mae_timestamp": trade.get("mae_timestamp"),
           "minutes_to_mfe": trade.get("minutes_to_mfe"), "minutes_to_mae": trade.get("minutes_to_mae"),
           "mae_before_mfe": trade.get("mae_before_mfe"), "exit_efficiency": trade.get("exit_efficiency"),
           "mfe_left_on_table_r": trade.get("mfe_left_on_table_r"), "scale_outs": trade.get("scale_outs"),
           "excursion_bars": trade.get("excursion_bars")}
    for name, lvl in REACH:
        row[name] = None if mfe is None else bool(mfe >= lvl)
    row.update(feat)
    row.update(execm)
    return row


# ================================================================ agregados
def outcome_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    n = len(df)
    pnl = df["realized_pnl"].astype(float).tolist() if n else []
    rr = pd.to_numeric(df["realized_r"], errors="coerce").dropna() if n else pd.Series(dtype=float)
    mfe = pd.to_numeric(df["mfe_r"], errors="coerce").dropna() if n else pd.Series(dtype=float)
    mae = pd.to_numeric(df["mae_r"], errors="coerce").dropna() if n else pd.Series(dtype=float)

    def pct(col):
        return float(df[col].astype(bool).mean() * 100) if n else None

    def f(x):
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    return {"trades": n, "win_rate_pct": float((df["result"] == "win").mean() * 100) if n else None,
            "pnl": float(sum(pnl)), "avg_pnl": f(statistics.fmean(pnl)) if pnl else None,
            "expectancy_r": f(rr.mean()) if len(rr) else None, "median_r": f(rr.median()) if len(rr) else None,
            "profit_factor": _profit_factor(pnl) if n >= 2 else None, "total_r": f(rr.sum()) if len(rr) else None,
            "avg_mfe_r": f(mfe.mean()) if len(mfe) else None, "median_mfe_r": f(mfe.median()) if len(mfe) else None,
            "avg_mae_r": f(mae.mean()) if len(mae) else None, "median_mae_r": f(mae.median()) if len(mae) else None,
            "pct_reached_0_5r": pct("reached_0_50r"), "pct_reached_1r": pct("reached_1_00r")}


def group_table(df: pd.DataFrame, key: str, order: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    keys = df[key].map(lambda x: "missing" if x is None or (isinstance(x, float) and math.isnan(x)) else str(x))
    vals = list(order or []) + sorted(set(keys) - set(order or []))
    return [{key: v, **outcome_metrics(df[keys == v])} for v in vals if (keys == v).any() or v in (order or [])]


def quantile_labels(s: pd.Series, q: int, prefix: str) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    out = pd.Series("missing", index=s.index, dtype=object)
    ok = x.notna()
    if ok.any():
        out[ok] = [f"{prefix}{int(k) + 1}" for k in pd.qcut(x[ok], q, labels=False, duplicates="drop")]
    return out


def loss_behavior(tr: pd.DataFrame) -> Dict[str, Any]:
    los = tr[(tr["result"] == "loss") & tr["mfe_r"].notna()]
    n = len(los)

    def p(mask):
        return float(mask.sum() / n * 100) if n else None
    return {
        "losing_trades": n,
        "q1_pct_never_reached_0_25r": p(los["mfe_r"] < 0.25),
        "q2_pct_never_reached_0_50r": p(los["mfe_r"] < 0.5),
        "q3_pct_reached_1r_then_negative": p(los["mfe_r"] >= 1.0),
        "q4_median_mfe_r": float(los["mfe_r"].median()) if n else None,
        "q5_median_mae_r": float(los["mae_r"].median()) if n else None,
        "q6_median_minutes_to_mae": float(los["minutes_to_mae"].median()) if n else None,
        "q6_p25_p75_minutes_to_mae": [float(los["minutes_to_mae"].quantile(.25)), float(los["minutes_to_mae"].quantile(.75))] if n else None,
        "q7_pct_immediate_failure_mfe_lt_0_25r": p(los["mfe_r"] < IMMEDIATE_FAIL_R),
        "q7_pct_mae_before_mfe": p(los["mae_before_mfe"] == True),  # noqa: E712
        "q7_pct_moved_favorably_ge_0_25r_first": p(los["mfe_r"] >= IMMEDIATE_FAIL_R),
    }


def exit_reason_autopsy(tr: pd.DataFrame) -> Dict[str, Any]:
    rows = []
    for r in EXIT_REASONS:
        sub = tr[tr["exit_reason"] == r]
        m = outcome_metrics(sub)
        eff = pd.to_numeric(sub["exit_efficiency"], errors="coerce").dropna()
        left = pd.to_numeric(sub["mfe_left_on_table_r"], errors="coerce").dropna()
        rows.append({"exit_reason": r, **m, "avg_realized_r": m["expectancy_r"], "median_realized_r": m["median_r"],
                     "median_exit_efficiency": float(eff.median()) if len(eff) else None,
                     "avg_mfe_left_on_table_r": float(left.mean()) if len(left) else None,
                     "median_mfe_left_on_table_r": float(left.median()) if len(left) else None})
    gross_profit = float(tr.loc[tr["realized_pnl"] > 0, "realized_pnl"].sum())
    gb = tr[tr["exit_reason"] == "giveback_close"]
    st = tr[tr["exit_reason"] == "stop_hit"]
    tp = tr[tr["exit_reason"] == "take_profit_hit"]
    gb_tiny = gb[gb["mfe_r"] < 0.25]
    gb_big = gb[gb["mfe_r"] >= 1.0]
    return {
        "table": rows,
        "A_giveback_aggregate_pnl": float(gb["realized_pnl"].sum()),
        "B_giveback": {
            "trades": len(gb), "pct_mfe_below_0_25r": float((gb["mfe_r"] < 0.25).mean() * 100) if len(gb) else None,
            "pnl_mfe_below_0_25r": float(gb_tiny["realized_pnl"].sum()), "n_mfe_below_0_25r": len(gb_tiny),
            "pnl_mfe_ge_1r": float(gb_big["realized_pnl"].sum()), "n_mfe_ge_1r": len(gb_big),
            "median_mfe_r": float(gb["mfe_r"].median()) if len(gb) else None,
            "median_realized_r": float(gb["realized_r"].median()) if len(gb) else None,
            "median_mfe_left_on_table_r": float(gb["mfe_left_on_table_r"].median()) if len(gb) else None,
            "win_rate_pct": float((gb["result"] == "win").mean() * 100) if len(gb) else None},
        "C_stop_hit": {"trades": len(st),
                       "pct_immediate_failure_mfe_lt_0_25r": float((st["mfe_r"] < 0.25).mean() * 100) if len(st) else None,
                       "median_minutes_to_mae": float(st["minutes_to_mae"].median()) if len(st) else None,
                       "median_mfe_r": float(st["mfe_r"].median()) if len(st) else None},
        "D_take_profit": {"trades": len(tp), "pnl": float(tp["realized_pnl"].sum()),
                          "share_of_gross_profit_pct": float(tp.loc[tp["realized_pnl"] > 0, "realized_pnl"].sum()
                                                             / gross_profit * 100) if gross_profit > 0 else None,
                          "gross_profit_all_trades": gross_profit},
    }


NUMERIC = ["pullback_min_distance_ema20_atr", "deepest_low_vs_ema50_atr", "n_down_closes", "n_red", "n_green",
           "consecutive_down_before_trigger", "max_pullback_5bar_high_atr", "max_pullback_10bar_high_atr",
           "pullback_range_atr", "pullback_net_return_atr", "trend_age_bars", "bars_close_above_ema50", "spread_atr",
           "ema20_slope_atr", "ema50_slope_atr", "close_above_ema20_atr", "close_above_ema50_atr", "trigger_body_atr",
           "trigger_range_atr", "trigger_close_location", "breakout_distance_atr", "trigger_return_pct",
           "volume_ratio_5", "volume_ratio_20", "atr_pct", "decision_minute_et"]
EXECUTION_NUMERIC = ["entry_fill_distance_atr", "next_bar_gap_atr", "entry_above_ema20_atr", "entry_above_ema50_atr",
                     "slippage_cost_r"]
CATEGORICAL = ["depth_group", "pullback_direction", "any_pullback_close_below_ema20", "trend_age_bucket",
               "ema50_slope_direction", "trigger_location_bucket", "time_bucket", "symbol"]


def compare(tr: pd.DataFrame, a_mask: pd.Series, b_mask: pd.Series, a_name: str, b_name: str,
            include_exec: bool = True) -> Dict[str, Any]:
    a, b = tr[a_mask], tr[b_mask]
    num = {}
    for f in NUMERIC + (EXECUTION_NUMERIC if include_exec else []):
        sa, sb = numeric_stats(a[f]), numeric_stats(b[f])
        num[f] = {a_name: sa, b_name: sb, "cles_b_gt_a": cles(a[f], b[f]),
                  "post_decision": f in EXECUTION_NUMERIC}
    cat = {}
    for f in CATEGORICAL:
        vals = sorted({str(x) for x in pd.concat([a[f], b[f]])})
        cat[f] = {v: {f"count_{a_name}": int((a[f].astype(str) == v).sum()),
                      f"pct_{a_name}": float((a[f].astype(str) == v).mean() * 100) if len(a) else None,
                      f"count_{b_name}": int((b[f].astype(str) == v).sum()),
                      f"pct_{b_name}": float((b[f].astype(str) == v).mean() * 100) if len(b) else None} for v in vals}
    return {"groups": [a_name, b_name], f"n_{a_name}": len(a), f"n_{b_name}": len(b), "numeric": num, "categorical": cat}


def separations(tr: pd.DataFrame) -> Dict[str, Any]:
    comps = {
        "A_losers_vs_winners": compare(tr, tr["result"] == "loss", tr["result"] == "win", "losers", "winners"),
        "B_never_0_5r_vs_reached_0_5r": compare(tr, tr["reached_0_50r"] == False, tr["reached_0_50r"] == True,  # noqa: E712
                                                "never_0_5r", "reached_0_5r"),
        "C_never_1r_vs_reached_1r": compare(tr, tr["reached_1_00r"] == False, tr["reached_1_00r"] == True,  # noqa: E712
                                            "never_1r", "reached_1r"),
        "D_stop_hit_vs_giveback": compare(tr, tr["exit_reason"] == "stop_hit", tr["exit_reason"] == "giveback_close",
                                          "stop_hit", "giveback_close"),
        "E_loose_vs_penetrated": compare(tr, tr["depth_group"] == "loose", tr["depth_group"] == "penetrated",
                                         "loose", "penetrated"),
    }
    top = {}
    for name, c in comps.items():
        items = [(f, v["cles_b_gt_a"]) for f, v in c["numeric"].items() if v["cles_b_gt_a"] is not None]
        items.sort(key=lambda x: (-abs(x[1] - 0.5), x[0]))
        top[name] = [{"feature": f, "cles_b_gt_a": v, "post_decision": f in EXECUTION_NUMERIC} for f, v in items[:6]]
    return {"comparisons": comps, "largest_abs_cles_minus_half_inspection_only": top}


CROSSTABS = (("depth_group", "trend_age_bucket"), ("depth_group", "ema50_slope_direction"),
             ("depth_group", "atr_pct_quintile"), ("trigger_strength", "depth_group"))


def crosstabs(tr: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for rf, cf in CROSSTABS:
        for rv in sorted(tr[rf].astype(str).unique()):
            for cv in sorted(tr[cf].astype(str).unique()):
                sub = tr[(tr[rf].astype(str) == rv) & (tr[cf].astype(str) == cv)]
                if len(sub):
                    m = outcome_metrics(sub)
                    rows.append({"crosstab": f"{rf} x {cf}", "row_value": rv, "col_value": cv,
                                 **{k: m[k] for k in ("trades", "win_rate_pct", "expectancy_r", "total_r", "pnl",
                                                      "pct_reached_1r")}})
    return rows


FUNNEL_STAGES = ("risk_accepted", "risk_rejected", "in_position", "circuit_breaker", "daily_profit_halt", "unclassified")


def funnel(signals: pd.DataFrame, n_completed: int) -> Dict[str, Any]:
    counts = signals["stage"].value_counts().to_dict()
    reached = int(counts.get("risk_accepted", 0) + counts.get("risk_rejected", 0))
    rej = signals[signals["stage"] == "risk_rejected"]["reason"].value_counts().to_dict()
    cb = signals[signals["stage"] == "circuit_breaker"]["reason"].value_counts().to_dict()
    return {"generated": len(signals), "reached_risk": reached, "accepted": int(counts.get("risk_accepted", 0)),
            "completed": n_completed, "not_reaching_risk": {k: int(counts.get(k, 0)) for k in
                                                            ("in_position", "circuit_breaker", "daily_profit_halt",
                                                             "unclassified")},
            "rejected_by_reason": {k: int(v) for k, v in rej.items()},
            "circuit_breaker_by_reason": {k: int(v) for k, v in cb.items()}}


def population_bias(signals: pd.DataFrame) -> Dict[str, Any]:
    pops = {"generated": signals, "reached_risk": signals[signals["stage"].isin(["risk_accepted", "risk_rejected"])],
            "accepted": signals[signals["stage"] == "risk_accepted"],
            "rejected": signals[signals["stage"] == "risk_rejected"]}
    out = {}
    for name, df in pops.items():
        def share(col):
            return {str(k): float(v * 100) for k, v in df[col].astype(str).value_counts(normalize=True).sort_index().items()}
        out[name] = {"n": len(df), "median_pullback_min_distance_ema20_atr": _med(df, "pullback_min_distance_ema20_atr"),
                     "median_atr_pct": _med(df, "atr_pct"), "p10_atr_pct": _q(df, "atr_pct", .10),
                     "median_trend_age_bars": _med(df, "trend_age_bars"),
                     "median_trigger_body_atr": _med(df, "trigger_body_atr"),
                     "median_breakout_distance_atr": _med(df, "breakout_distance_atr"),
                     "depth_group_pct": share("depth_group"), "trend_age_bucket_pct": share("trend_age_bucket"),
                     "symbol_pct": share("symbol"), "time_bucket_pct": share("time_bucket")}
    rr = signals[(signals["stage"] == "risk_rejected") & (signals["reason"] == "RR_BELOW_MINIMUM")]
    acc = pops["accepted"]
    out["rr_rejects_atr_pct"] = {"n": len(rr), "median": _med(rr, "atr_pct"), "max": _q(rr, "atr_pct", 1.0),
                                 "accepted_min_atr_pct": _q(acc, "atr_pct", 0.0)}
    return out


def _med(df, col):
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    return float(s.median()) if len(s) else None


def _q(df, col, q):
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    return float(s.quantile(q)) if len(s) else None


def symbol_table(tr: pd.DataFrame, symbols: Sequence[str]) -> List[Dict[str, Any]]:
    rows = []
    for s in symbols:
        sub = tr[tr["symbol"] == s]
        m = outcome_metrics(sub)
        rows.append({"symbol": s, **m, **{f"{r}_pnl": float(sub.loc[sub["exit_reason"] == r, "realized_pnl"].sum())
                                          for r in EXIT_REASONS},
                     **{f"{r}_count": int((sub["exit_reason"] == r).sum()) for r in EXIT_REASONS},
                     "median_atr_pct": _med(sub, "atr_pct")})
    return rows


def ma_comparison(h001_summary: Dict[str, Any], bench_path: Path) -> Dict[str, Any]:
    b = json.loads(bench_path.read_text(encoding="utf-8"))
    ma = next(p for p in b["periods"] if p["period"] == "development")
    t, p = h001_summary["trades"], h001_summary["portfolio"]
    h = {"trades": t["trades"], "win_rate_pct": t["win_rate"] * 100, "expectancy_r": t["expectancy_r"],
         "profit_factor": t["profit_factor"], "total_r": t["total_r"], "max_drawdown_pct": p["max_drawdown_pct"],
         "max_consecutive_losses": t["max_consecutive_losses"], "realized_pnl": p["realized_pnl_closed_trades"]}
    m = {k: ma[k] for k in ("trades", "win_rate_pct", "expectancy_r", "profit_factor", "total_r", "max_drawdown_pct",
                            "max_consecutive_losses", "realized_pnl")}
    return {"source": str(bench_path), "evidence": "DEVELOPMENT EVIDENCE only", "MA_BASELINE_V1": m, HYPOTHESIS_ID: h,
            "delta_h001_minus_ma": {k: (h[k] - m[k]) if h[k] is not None and m[k] is not None else None for k in m}}


# ================================================================ orquestación
def build_autopsy(protocol: Dict[str, Any], split: Dict[str, Any], data_dir: Path,
                  stored_run: Path = STORED_RUN, bench_path: Path = MA_BENCHMARK,
                  verify_stored: bool = True) -> Dict[str, Any]:
    from .backtest_report import summarize
    symbols = list(protocol["universe"]["symbols"])
    loaded = load_development_bars(data_dir, symbols, split)
    result, stages = rerun_development(protocol, split, loaded["bars"])
    s_utc, e_utc = _start_utc(split["start"]), _end_utc(split["end"])
    for st in stages:
        if not (s_utc <= pd.Timestamp(st["bar_timestamp"]) < e_utc):
            raise HygieneError(f"señal fuera de development: {st['bar_timestamp']}")
    reproduction = None
    if verify_stored:
        stored = (stored_run / "trades.json").read_text(encoding="utf-8")
        reproduction = {"stored_run": str(stored_run), "trades_json_identical": to_json(result.trades) == stored}
        if not reproduction["trades_json_identical"]:
            raise RuntimeError("el re-run no reproduce los trades guardados de H001 development")
    # features por señal (solo velas <= señal)
    ind = {s: symbol_indicators(loaded["bars"][s]) for s in symbols}
    pos = {s: {ts: k for k, ts in enumerate(ind[s]["ts"])} for s in symbols}
    if not stages:
        raise RuntimeError("development sin señales BUY: nada que diagnosticar")
    sig_rows = []
    for st in stages:
        s = st["symbol"]
        t = pos[s][st["bar_timestamp"]]
        f = signal_features(ind[s], t)
        sig_rows.append({**st, **f})
    signals = pd.DataFrame(sig_rows)
    # consistencia: las features re-calculadas satisfacen las reglas de H001 en cada señal emitida
    rule_violations = 0
    for st in stages:
        s, t = st["symbol"], pos[st["symbol"]][st["bar_timestamp"]]
        d = ind[s]["date"]
        s0 = t
        while s0 - 1 >= 0 and d[s0 - 1] == d[t]:
            s0 -= 1
        cond = conditions_at(t, s0, *(ind[s][k] for k in ("o", "h", "l", "c", "e20", "e50", "atr")))
        rule_violations += 0 if all(cond.values()) else 1
    # trades completados + features de su señal + ejecución
    entry_fills = {(f["symbol"], f["fill_ts"]): f for f in result.fills if f["side"] == "buy"}
    feat_by_key = {(r["symbol"], r["bar_timestamp"]): r for r in sig_rows}
    tr_rows = []
    for t in result.trades:
        f = feat_by_key[(t["symbol"], t["entry_signal_timestamp"])]
        feat = {k: v for k, v in f.items() if k not in ("symbol", "bar_timestamp", "stage", "reason")}
        tr_rows.append(trade_row(t, feat, trade_execution_metrics(t, feat, entry_fills[(t["symbol"], t["entry_fill_timestamp"])])))
    if tr_rows:
        tr = pd.DataFrame(tr_rows)
    else:  # sin trades completados: tablas vacías pero con columnas (no se inventan filas)
        feat_cols = [c for c in signals.columns if c not in ("symbol", "bar_timestamp", "stage", "reason")]
        base = list(trade_row({"trade_id": 0, "symbol": "", "entry_signal_timestamp": "", "entry_fill_timestamp": "",
                               "exit_fill_timestamp": "", "exit_reason": "", "result": "", "realized_pnl": 0.0,
                               "realized_r": 0.0}, {}, {}).keys())
        tr = pd.DataFrame(columns=list(dict.fromkeys(base + feat_cols + EXECUTION_NUMERIC + ["slippage_cost_usd"])))
    tr["atr_pct_quintile"] = quantile_labels(tr["atr_pct"], 5, "Q")
    tr["trigger_strength"] = quantile_labels(tr["trigger_body_atr"], 3, "T")
    summary = summarize(result)
    atr_rows = []
    for q in sorted(tr["atr_pct_quintile"].unique()):
        sub = tr[tr["atr_pct_quintile"] == q]
        atr_rows.append({"atr_pct_quintile": q, "atr_pct_min": float(sub["atr_pct"].min()),
                         "atr_pct_max": float(sub["atr_pct"].max()), **outcome_metrics(sub),
                         "avg_slippage_cost_r": float(sub["slippage_cost_r"].mean()),
                         "median_slippage_cost_r": float(sub["slippage_cost_r"].median())})
    trig_rows = group_table(tr, "trigger_location_bucket", ["top_quarter", "upper_half", "below_mid"])
    trig_strength = []
    for q in sorted(tr["trigger_strength"].unique()):
        sub = tr[tr["trigger_strength"] == q]
        trig_strength.append({"trigger_strength": q, "body_atr_min": float(sub["trigger_body_atr"].min()),
                              "body_atr_max": float(sub["trigger_body_atr"].max()), **outcome_metrics(sub)})
    chase = {g: {f: _med(tr[mask], f) for f in EXECUTION_NUMERIC + ["close_above_ema20_atr"]}
             for g, mask in (("losers", tr["result"] == "loss"), ("winners", tr["result"] == "win"),
                             ("never_0_5r", tr["reached_0_50r"] == False),  # noqa: E712
                             ("reached_1r", tr["reached_1_00r"] == True))}  # noqa: E712
    out = {
        "hypothesis_id": HYPOTHESIS_ID, "split": split["name"], "role": split["role"],
        "dates": [split["start"], split["end"]], "evidence": "DEVELOPMENT EVIDENCE only", "disclaimer": DISCLAIMER,
        "data_access": {"bars_loaded_until_utc": e_utc.isoformat(), "support_and_split_bars": loaded["info"],
                        "reproduction_of_stored_run": reproduction},
        "prefixed_thresholds": {"depth_groups": "penetrated <=0 | close (0,0.25] | loose (0.25,0.50]",
                                "pullback_direction_atr": DIRECTION_ATR, "ema50_flat_per_bar_atr": FLAT_SLOPE_ATR_PER_BAR,
                                "trend_age_buckets": [b[0] for b in TREND_AGE_BUCKETS],
                                "time_buckets": [b[0] for b in TIME_BUCKETS], "immediate_failure_mfe_r": IMMEDIATE_FAIL_R},
        "consistency": {"signals_violating_h001_rules_on_recomputed_indicators": rule_violations,
                        "depth_groups_beyond_0_50": int((signals["depth_group"] == "beyond_0_50").sum())},
        "funnel": funnel(signals, len(tr)),
        "loss_behavior": loss_behavior(tr),
        "exit_reasons": exit_reason_autopsy(tr),
        "pullback_depth": group_table(tr, "depth_group", list(DEPTH_GROUPS)),
        "pullback_direction": group_table(tr, "pullback_direction", ["down", "sideways", "up"]),
        "pullback_close_below_ema20": group_table(tr, "any_pullback_close_below_ema20", ["True", "False"]),
        "trend_age": group_table(tr, "trend_age_bucket", [b[0] for b in TREND_AGE_BUCKETS]),
        "trigger_location": trig_rows, "trigger_strength": trig_strength,
        "entry_chase_medians_post_decision": chase,
        "atr_quintiles": atr_rows,
        "time_of_day": group_table(tr, "time_bucket", [b[0] for b in TIME_BUCKETS]),
        "by_symbol": symbol_table(tr, symbols),
        "separations": separations(tr),
        "crosstabs": crosstabs(tr),
        "population_bias": population_bias(signals),
        "h001_vs_ma_baseline_development": ma_comparison(summary, bench_path) if bench_path.is_file() else None,
    }
    return {"summary": out, "trades": tr, "signals": signals}


# ================================================================ salidas
def _csv(path: Path, rows: List[Dict[str, Any]]) -> Path:
    fields = list(dict.fromkeys(k for r in rows for k in r)) if rows else ["empty"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def write_autopsy(res: Dict[str, Any], out_dir: Path) -> List[Path]:
    if out_dir.resolve() == STORED_RUN.resolve():
        raise HygieneError("no se sobrescriben los resultados originales de H001")
    out_dir.mkdir(parents=True, exist_ok=True)
    s = res["summary"]
    paths = []
    p = out_dir / "h001_autopsy_summary.json"
    p.write_text(to_json(s), encoding="utf-8")
    paths.append(p)
    res["trades"].to_csv(out_dir / "h001_autopsy_trades.csv", index=False)
    paths.append(out_dir / "h001_autopsy_trades.csv")
    res["signals"].to_csv(out_dir / "h001_autopsy_signals.csv", index=False)
    paths.append(out_dir / "h001_autopsy_signals.csv")
    for name, rows in (("pullback_depth", s["pullback_depth"]), ("exit_reason", s["exit_reasons"]["table"]),
                       ("trend_age", s["trend_age"]), ("atr_quintile", s["atr_quintiles"]),
                       ("time_bucket", s["time_of_day"]), ("by_symbol", s["by_symbol"]), ("crosstabs", s["crosstabs"])):
        paths.append(_csv(out_dir / f"h001_autopsy_{name}.csv", rows))
    f = s["funnel"]
    fun = [{"stage": "generated", "count": f["generated"]}, {"stage": "reached_risk", "count": f["reached_risk"]},
           {"stage": "accepted", "count": f["accepted"]}, {"stage": "completed", "count": f["completed"]}]
    fun += [{"stage": f"not_reaching_risk:{k}", "count": v} for k, v in f["not_reaching_risk"].items()]
    fun += [{"stage": f"rejected:{k}", "count": v} for k, v in f["rejected_by_reason"].items()]
    paths.append(_csv(out_dir / "h001_autopsy_signal_funnel.csv", fun))
    tr = res["trades"]
    corr = []
    for feat in NUMERIC + EXECUTION_NUMERIC:
        x = pd.to_numeric(tr[feat], errors="coerce")
        row = {"feature": feat, "post_decision": feat in EXECUTION_NUMERIC}
        for tgt in ("realized_r", "mfe_r", "mae_r"):
            y = pd.to_numeric(tr[tgt], errors="coerce")
            ok = x.notna() & y.notna()
            good = ok.sum() >= 10 and x[ok].nunique() > 1 and y[ok].nunique() > 1
            row[f"pearson_{tgt}"] = float(x[ok].corr(y[ok])) if good else None
            row[f"spearman_{tgt}"] = float(x[ok].rank().corr(y[ok].rank())) if good else None
        corr.append(row)
    paths.append(_csv(out_dir / "h001_autopsy_correlations.csv", corr))
    return paths


def _f(x, fmt="{:+.3f}"):
    return "-" if x is None else fmt.format(x)


def _p(x):
    return "-" if x is None else f"{x:.1f}%"


def format_autopsy(s: Dict[str, Any]) -> str:
    lb, ex, fu = s["loss_behavior"], s["exit_reasons"], s["funnel"]
    L = ["H001 DEVELOPMENT AUTOPSY (2024-01-02 → 2025-12-31; DEVELOPMENT EVIDENCE only)", "─" * 72,
         f"Re-run reproduces stored H001 development trades exactly: "
         f"{s['data_access']['reproduction_of_stored_run']['trades_json_identical'] if s['data_access']['reproduction_of_stored_run'] else 'not checked'}"
         f" | rule-consistency violations: {s['consistency']['signals_violating_h001_rules_on_recomputed_indicators']}",
         "", "Loss behavior (completed losing trades, 5Min closes):",
         f"  losers {lb['losing_trades']} | never +0.25R {_p(lb['q1_pct_never_reached_0_25r'])} | never +0.5R "
         f"{_p(lb['q2_pct_never_reached_0_50r'])} | reached +1R then negative {_p(lb['q3_pct_reached_1r_then_negative'])}",
         f"  median MFE {_f(lb['q4_median_mfe_r'])}R | median MAE {_f(lb['q5_median_mae_r'])}R | median minutes to MAE "
         f"{_f(lb['q6_median_minutes_to_mae'], '{:.0f}')} | MAE before MFE {_p(lb['q7_pct_mae_before_mfe'])}",
         "", "Exit reasons:",
         f"  {'reason':<16}{'n':>6}{'win%':>7}{'P&L':>12}{'avgR':>8}{'medR':>8}{'MFE med':>9}{'MAE med':>9}{'≥0.5R':>7}{'≥1R':>7}{'left med':>9}"]
    for r in ex["table"]:
        L.append(f"  {r['exit_reason']:<16}{r['trades']:>6}{_p(r['win_rate_pct']):>7}{r['pnl']:>12,.0f}"
                 f"{_f(r['avg_realized_r'], '{:+.2f}'):>8}{_f(r['median_realized_r'], '{:+.2f}'):>8}"
                 f"{_f(r['median_mfe_r'], '{:+.2f}'):>9}{_f(r['median_mae_r'], '{:+.2f}'):>9}"
                 f"{_p(r['pct_reached_0_5r']):>7}{_p(r['pct_reached_1r']):>7}{_f(r['median_mfe_left_on_table_r'], '{:+.2f}'):>9}")
    b = ex["B_giveback"]
    L += [f"  giveback aggregate P&L ${ex['A_giveback_aggregate_pnl']:,.0f}; {b['n_mfe_below_0_25r']} giveback trades with MFE<+0.25R "
          f"P&L ${b['pnl_mfe_below_0_25r']:,.0f}; {b['n_mfe_ge_1r']} with MFE≥+1R P&L ${b['pnl_mfe_ge_1r']:,.0f}",
          f"  stop_hit immediate failures (MFE<+0.25R) {_p(ex['C_stop_hit']['pct_immediate_failure_mfe_lt_0_25r'])}; "
          f"take-profit trades = {_p(ex['D_take_profit']['share_of_gross_profit_pct'])} of gross profit"]

    def table(title, rows, key):
        out = ["", title, f"  {'group':<14}{'n':>6}{'win%':>7}{'expR':>8}{'PF':>6}{'totR':>8}{'MFE avg':>9}{'MAE avg':>9}{'≥0.5R':>7}{'≥1R':>7}{'P&L':>11}"]
        for r in rows:
            out.append(f"  {str(r[key]):<14}{r['trades']:>6}{_p(r['win_rate_pct']):>7}{_f(r['expectancy_r']):>8}"
                       f"{_f(r['profit_factor'], '{:.2f}'):>6}{_f(r['total_r'], '{:+.1f}'):>8}{_f(r['avg_mfe_r'], '{:+.2f}'):>9}"
                       f"{_f(r['avg_mae_r'], '{:+.2f}'):>9}{_p(r['pct_reached_0_5r']):>7}{_p(r['pct_reached_1r']):>7}{r['pnl']:>11,.0f}")
        return out
    L += table("Pullback depth (closest low vs EMA20, ATR):", s["pullback_depth"], "depth_group")
    L += table("Trend maturity (bars EMA20 > EMA50):", s["trend_age"], "trend_age_bucket")
    L += table("Trigger quality (close location):", s["trigger_location"], "trigger_location_bucket")
    L += table("Trigger strength (body/ATR terciles):", s["trigger_strength"], "trigger_strength")
    L += table("Volatility (ATR% quintiles):", s["atr_quintiles"], "atr_pct_quintile")
    L.append("  avg modeled slippage cost R by quintile: "
             + ", ".join(f"{r['atr_pct_quintile']} {r['avg_slippage_cost_r']:.3f}" for r in s["atr_quintiles"]))
    L += table("Time of day (decision ET):", s["time_of_day"], "time_bucket")
    L += table("Symbols:", s["by_symbol"], "symbol")
    pb = s["population_bias"]
    L += ["", "Signal funnel:",
          f"  generated {fu['generated']} → reached risk {fu['reached_risk']} → accepted {fu['accepted']} → completed {fu['completed']}",
          f"  not reaching risk: {fu['not_reaching_risk']}", f"  rejected by reason: {fu['rejected_by_reason']}",
          f"  median ATR%: generated {_f(pb['generated']['median_atr_pct'], '{:.3f}')} | accepted "
          f"{_f(pb['accepted']['median_atr_pct'], '{:.3f}')} | rejected {_f(pb['rejected']['median_atr_pct'], '{:.3f}')}; "
          f"RR-rejected max ATR% {_f(pb['rr_rejects_atr_pct']['max'], '{:.3f}')} vs accepted min "
          f"{_f(pb['rr_rejects_atr_pct']['accepted_min_atr_pct'], '{:.3f}')}"]
    mc = s["h001_vs_ma_baseline_development"]
    if mc:
        L += ["", "H001 vs MA_BASELINE_V1 (DEVELOPMENT):"]
        for k in ("trades", "win_rate_pct", "expectancy_r", "profit_factor", "total_r", "max_drawdown_pct",
                  "max_consecutive_losses", "realized_pnl"):
            L.append(f"  {k:<24}{mc['MA_BASELINE_V1'][k]:>14.3f}{mc[HYPOTHESIS_ID][k]:>14.3f}")
    L += ["", "Largest |CLES−0.5| per comparison (inspection only; * = post-decision execution metric):"]
    for name, items in s["separations"]["largest_abs_cles_minus_half_inspection_only"].items():
        L.append(f"  {name}: " + ", ".join(f"{i['feature']}{'*' if i['post_decision'] else ''} {i['cles_b_gt_a']:.2f}"
                                          for i in items[:4]))
    L += ["", s["disclaimer"]]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.h001_autopsy",
                                description=f"Autopsia de {HYPOTHESIS_ID} SOLO sobre DEVELOPMENT (no hay bypass).")
    p.add_argument("--split", default="development", help="Solo 'development' es aceptado.")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, default=Path("data") / "research_v1" / "h001_autopsy")
    a = p.parse_args(argv)
    try:
        protocol = load_protocol(a.protocol)
        split = development_scope(protocol, a.split)
        res = build_autopsy(protocol, split, a.data_dir)
        paths = write_autopsy(res, a.output_dir)
    except HygieneError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    print(format_autopsy(res["summary"]))
    print("\nArchivos: " + ", ".join(str(x) for x in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
