# src/h001_opportunity_autopsy.py
"""
H001 DEVELOPMENT OPPORTUNITY / SIGNAL-SELECTION AUTOPSY — diagnóstico de SOLO LECTURA.
H001 y H002 están cerradas (REJECTED_AT_DEVELOPMENT); nada aquí cambia reglas, riesgo ni vivo.

    python -m src.h001_opportunity_autopsy --data-dir data/historical \
        --output-dir data/research_v1/h001_opportunity_autopsy

Pasos
-----
1. Re-corre H001 SOLO en development con un observador (subclase de BacktestEngine que solo lee
   estado) y verifica que trades/equity/daily/summary son idénticos a la corrida congelada.
2. Ruta única por señal BUY: aceptada / rechazada por el RiskManager (motivo real) / bloqueada antes
   del riesgo (halt de ganancia diaria, racha de pérdidas, otro circuit breaker, símbolo ya abierto).
   Guarda orden de procesamiento en el timestamp y el estado del portafolio justo antes.
3. SOMBRA (contrafactual, descriptiva, nunca toca el portafolio real):
   - entrada sombra = apertura de la siguiente vela 5Min existente del símbolo + 5 bps;
   - R_ps reconstruido con la fórmula del RiskManager (entrada modelada = cierre·(1+slippage_pct),
     stop = entrada − atr_sl·ATR14, ambos redondeados a centavos) — puro, sin estado de portafolio;
   - excursiones por CIERRES a 3/6/12 velas y fin de sesión (sin highs/lows intrabarra).
   - trade sombra aislado: el MISMO motor de backtest, sin cambios, en un portafolio vacío de un solo
     símbolo con BUY forzado en la vela de señal y el equity que H001 tenía en esa decisión. Validado
     contra los trades aceptados reales antes de usarse.
4. Agregados por ruta, filtros de riesgo, halts, señales con el símbolo ya abierto, grupos de
   competencia en el mismo timestamp, sesgo de orden de procesamiento, ARREPENTIMIENTO EX-POST
   (usa información futura: NO es una regla), distribuciones de features por ruta, y el mapeo de los
   trades exclusivos de H002 a la ruta que tenían en H001.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import h001_autopsy as ap
from .backtest_engine import BacktestConfig, BacktestEngine, production_args
from .backtest_report import summarize, to_json, write_outputs
from .research_protocol import DEFAULT_PROTOCOL_PATH, load_protocol
from .risk_manager_avanzado import RiskManager
from .run_paper import build_risk_config
from .strategy import StrategyResult
from .strategy_v2_h001 import ENGINE_LOOKBACK, HygieneError

NY = "America/New_York"
H001_STORED = Path("data") / "research_v1" / "h001_development"
H002_STORED = Path("data") / "research_v1" / "h002_development"
HORIZONS = ((3, "15m"), (6, "30m"), (12, "60m"))
SHADOW_SLIPPAGE_BPS = 5.0
ISOLATED_PRE_BARS = 40
ISOLATED_POST_BARS = (80, 1200)          # intento corto; si queda abierto, uno largo
REGRET_WARNING = "THIS USES FUTURE INFORMATION AND CANNOT BE USED AS A LIVE RULE."
DISCLAIMER = ("Descriptive DEVELOPMENT-only diagnostics of CLOSED hypotheses. Shadow results are counterfactual, "
              "never portfolio trades; no ranking, filter or parameter is proposed; no H003.")
ROUTE_ORDER = ("accepted", "rejected:RR_BELOW_MINIMUM", "rejected:LIQUIDITY_BELOW_MINIMUM", "rejected:LEVERAGE_EXCEEDED",
               "rejected:MAX_POSITIONS_REACHED", "blocked:daily_profit_halt", "blocked:loss_streak_halt",
               "blocked:symbol_already_open")
LOSS_STREAK_REASON = "Racha negativa"


# ================================================================ higiene (reutiliza la guarda de development)
development_scope = ap.development_scope


# ================================================================ observador del ruteo real
class _RoutingObserver(ap._ObservingEngine):
    """Añade a cada señal: orden de procesamiento en su timestamp y estado del portafolio justo antes."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._ts = None
        self._rank = 0

    def _evaluate(self, sd, i):
        ts = sd.iso[i]
        if ts != self._ts:
            self._ts, self._rank = ts, 0
        state = {"open_positions_before": len(self.book), "gross_exposure_before": float(self.risk._gross_exposure()),
                 "equity_before": float(self.sim.equity()), "symbol_order": int(sd.order)}
        n = len(self.signal_stages)
        super()._evaluate(sd, i)
        if len(self.signal_stages) > n:
            self._rank += 1
            self.signal_stages[-1].update(state, buy_rank_in_timestamp=self._rank, decision_timestamp=sd.decision_iso[i])


def route_of(stage: str, reason: Optional[str]) -> str:
    if stage == "risk_accepted":
        return "accepted"
    if stage == "risk_rejected":
        return f"rejected:{reason}"
    if stage == "in_position":
        return "blocked:symbol_already_open"
    if stage == "daily_profit_halt":
        return "blocked:daily_profit_halt"
    if stage == "circuit_breaker":
        return "blocked:loss_streak_halt" if reason == LOSS_STREAK_REASON else f"blocked:circuit_breaker:{reason}"
    return f"unclassified:{stage}"


def rerun_observed(protocol: Dict[str, Any], split: Dict[str, Any], bars: Dict[str, pd.DataFrame]):
    ex = protocol["execution_defaults"]
    cfg = BacktestConfig(symbols=list(protocol["universe"]["symbols"]), timeframe="5Min", start=split["start"],
                         end=split["end"], initial_equity=float(ex["initial_equity"]),
                         slippage_bps=float(ex["slippage_bps"]), commission=float(ex["commission_per_fill"]),
                         record_evaluations=True, window_hours_limit=False)
    from .strategy_v2_h001 import TrendPullbackH001
    eng = _RoutingObserver(cfg, bars, production_args({"lookback": ENGINE_LOOKBACK}), TrendPullbackH001())
    return eng.run(), eng.signal_stages, eng


def verify_identical(result, out_dir: Path, stored: Path) -> Dict[str, Any]:
    write_outputs(result, summarize(result), out_dir)
    files = {f: (out_dir / f).read_bytes() == (stored / f).read_bytes()
             for f in ("trades.json", "trades.csv", "equity_curve.csv", "daily_results.csv")}
    files["summary.json"] = (json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
                             == json.loads((stored / "summary.json").read_text(encoding="utf-8")))
    return {"stored_run": str(stored), "files_identical": files, "identical": all(files.values())}


# ================================================================ sombra: R_ps y excursiones fijas
def reconstruct_risk_ps(price: float, h: np.ndarray, l: np.ndarray, c: np.ndarray, t: int, risk_cfg) -> Optional[float]:
    """R_ps exacto de H001 desde info conocida en la vela t (misma fórmula que assess_entry + motor; sin estado)."""
    atr = RiskManager._atr(h[:t + 1], l[:t + 1], c[:t + 1], risk_cfg.atr_window) if risk_cfg.use_atr_based_stop else None
    est = price + price * risk_cfg.slippage_pct
    stop = est - risk_cfg.atr_multiple_sl * atr if atr is not None else est * (1 - risk_cfg.default_sl_pct)
    entry = round(est, risk_cfg.price_precision)
    stop = round(stop, risk_cfg.price_precision)
    return abs(entry - stop) or (0.01 * price)


def shadow_fixed_horizon(ind: Dict[str, np.ndarray], t: int, risk_ps: Optional[float],
                         slippage_bps: float = SHADOW_SLIPPAGE_BPS) -> Dict[str, Any]:
    """Entrada sombra en la apertura de la siguiente vela (t+1) + slippage; excursiones por CIERRES."""
    o, c, d = ind["o"], ind["c"], ind["date"]
    n = len(c)
    out: Dict[str, Any] = {"shadow_valid": False, "shadow_entry_ts": None, "shadow_entry_price": None,
                           "shadow_risk_ps": risk_ps}
    if t + 1 >= n or not risk_ps:
        return out
    e = t + 1
    f = float(o[e]) * (1 + slippage_bps / 10_000)
    out.update(shadow_valid=True, shadow_entry_ts=ind["ts"][e], shadow_entry_price=f,
               shadow_entry_next_session=bool(d[e] != d[t]))

    def block(closes, name, crosses=None):
        x = (np.asarray(closes, float) - f) / risk_ps
        mfe, mae, last = float(x.max()), float(-x.min()), float(x[-1])
        out.update({f"mfe_r_{name}": mfe, f"mae_r_{name}": mae, f"close_r_{name}": last,
                    f"hit_p025_{name}": mfe >= 0.25, f"hit_p050_{name}": mfe >= 0.5, f"hit_p100_{name}": mfe >= 1.0,
                    f"hit_m050_{name}": mae >= 0.5, f"hit_m100_{name}": mae >= 1.0})
        if crosses is not None:
            out[f"crosses_session_{name}"] = crosses

    for bars, name in HORIZONS:
        if e + bars - 1 < n:
            block(c[e:e + bars], name, bool(d[e + bars - 1] != d[e]))
        else:
            out.update({f"{k}_{name}": None for k in ("mfe_r", "mae_r", "close_r", "hit_p025", "hit_p050", "hit_p100",
                                                      "hit_m050", "hit_m100", "crosses_session")})
    k = e
    while k + 1 < n and d[k + 1] == d[e]:
        k += 1
    block(c[e:k + 1], "eos")
    out["eos_bars"] = int(k - e + 1)
    return out


# ================================================================ sombra: trade aislado (motor real, sin cambios)
class _OneShot:
    """BUY forzado en una sola vela (aísla la gestión; la entrada real ya fue emitida por H001)."""
    min_bars = 1

    def __init__(self, when: pd.Timestamp):
        self.when = when

    def evaluate(self, df):
        return StrategyResult("BUY" if df.index[-1] == self.when else None, "shadow")


def isolated_shadow(job: Tuple[str, pd.DataFrame, int, float, str, str]) -> Dict[str, Any]:
    """Un trade sombra en un portafolio vacío de un solo símbolo. Nunca toca la corrida real."""
    sym, df, t, equity, dev_end, key = job
    signal_ts = df.index[t]
    start = str(signal_ts.tz_convert(NY).date())
    for post in ISOLATED_POST_BARS:
        lo, hi = max(0, t - ISOLATED_PRE_BARS), min(len(df), t + 1 + post)
        cfg = BacktestConfig(symbols=[sym], timeframe="5Min", start=start, end=dev_end, initial_equity=equity,
                             slippage_bps=SHADOW_SLIPPAGE_BPS, commission=0.0, window_hours_limit=False,
                             entry_diagnostics=False)
        r = BacktestEngine(cfg, {sym: df.iloc[lo:hi]}, production_args({"lookback": ENGINE_LOOKBACK}),
                           _OneShot(signal_ts)).run()
        if r.trades or not r.open_positions or hi == len(df):
            break
    ev = r.risk_evaluations[0] if r.risk_evaluations else None
    base = {"key": key, "shadow_decision": ev["decision"] if ev else None,
            "shadow_reject_reason": ev["reason_code"] if ev and ev["decision"] != "ACCEPT" else None}
    if r.trades:
        tr = r.trades[0]
        return dict(base, shadow_status="closed", shadow_exit_reason=tr["exit_reason"],
                    shadow_realized_r=tr["realized_r"], shadow_realized_pnl=tr["realized_pnl"],
                    shadow_entry_fill_ts=tr["entry_fill_timestamp"], shadow_entry_fill_price=tr["entry_fill_price"],
                    shadow_initial_qty=tr["initial_qty"], shadow_risk_ps_engine=tr["risk_per_share_modeled"],
                    shadow_mfe_r=tr["mfe_r"], shadow_mae_r=tr["mae_r"])
    if r.open_positions:
        return dict(base, shadow_status="unresolved_open_at_data_end")
    return dict(base, shadow_status="not_entered")


# ================================================================ agregados
def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _stat(s, fn):
    x = _num(s).dropna()
    return float(fn(x)) if len(x) else None


def _pct_true(s):
    x = s.dropna()
    return float(x.astype(bool).mean() * 100) if len(x) else None


def route_quality(df: pd.DataFrame, key: str = "route") -> List[Dict[str, Any]]:
    rows = []
    keys = [k for k in ROUTE_ORDER if k in set(df[key])] + sorted(set(df[key]) - set(ROUTE_ORDER))
    for k in keys:
        sub = df[df[key] == k]
        v = sub[sub["shadow_valid"] == True]  # noqa: E712
        row = {key: k, "signals": len(sub), "valid_shadow": len(v)}
        for _, name in HORIZONS + ((None, "eos"),):
            row[f"mean_mfe_r_{name}"] = _stat(v[f"mfe_r_{name}"], np.mean)
            row[f"median_mfe_r_{name}"] = _stat(v[f"mfe_r_{name}"], np.median)
        row.update({"mean_mae_r_60m": _stat(v["mae_r_60m"], np.mean), "median_mae_r_60m": _stat(v["mae_r_60m"], np.median),
                    "pct_hit_p050_60m": _pct_true(v["hit_p050_60m"]), "pct_hit_p100_60m": _pct_true(v["hit_p100_60m"]),
                    "pct_hit_m100_60m": _pct_true(v["hit_m100_60m"]),
                    "mean_close_r_eos": _stat(v["close_r_eos"], np.mean), "median_close_r_eos": _stat(v["close_r_eos"], np.median)})
        if "shadow_status" in sub:
            closed = sub[sub["shadow_status"] == "closed"]
            rr = _num(closed["shadow_realized_r"]).dropna()
            row.update({"isolated_attempted": int(sub["shadow_status"].notna().sum()),
                        "isolated_closed": len(closed),
                        "isolated_not_entered": int((sub["shadow_status"] == "not_entered").sum()),
                        "isolated_unresolved": int((sub["shadow_status"] == "unresolved_open_at_data_end").sum()),
                        "isolated_win_rate_pct": float((rr > 0).mean() * 100) if len(rr) else None,
                        "isolated_expectancy_r": float(rr.mean()) if len(rr) else None,
                        "isolated_pf_r": (float(rr[rr > 0].sum() / -rr[rr < 0].sum()) if (rr < 0).any() else None),
                        "isolated_exit_reasons": dict(closed["shadow_exit_reason"].value_counts().sort_index())
                        if len(closed) else {}})
        rows.append(row)
    return rows


def in_position_analysis(sig: pd.DataFrame, trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Señales con el símbolo ya abierto: posición existente, su R no realizado en la señal y su resultado final."""
    rows = []
    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    for _, s in sig[sig["route"] == "blocked:symbol_already_open"].iterrows():
        ts = pd.Timestamp(s["bar_timestamp"])
        host = next((t for t in by_sym.get(s["symbol"], []) if pd.Timestamp(t["entry_signal_timestamp"]) < ts
                     < pd.Timestamp(t["exit_fill_timestamp"])), None)
        if host is None:
            rows.append({"host_found": False})
            continue
        unreal = (s["signal_close"] - host["entry_fill_price"]) / host["risk_per_share_modeled"]
        rows.append({"host_found": True, "host_trade_id": host["trade_id"], "host_result": host["result"],
                     "host_realized_r": host["realized_r"], "host_exit_reason": host["exit_reason"],
                     "host_unrealized_r_at_signal": float(unreal)})
    df = pd.DataFrame(rows)
    if df.empty or not df["host_found"].any():
        return {"signals": len(rows), "host_found": 0}
    f = df[df["host_found"]]
    hosts = {t["trade_id"]: t for t in trades}
    n_host = f.groupby("host_trade_id").size()
    res = {"signals": len(rows), "host_found": int(len(f)),
           "signals_in_eventual_winners": int((f["host_result"] == "win").sum()),
           "signals_in_eventual_losers": int((f["host_result"] == "loss").sum()),
           "median_host_unrealized_r_at_signal": float(f["host_unrealized_r_at_signal"].median()),
           "pct_signals_host_above_entry": float((f["host_unrealized_r_at_signal"] > 0).mean() * 100),
           "median_host_final_r": float(f["host_realized_r"].median())}
    # tasa por vela mantenida (controla duración): señales / cierres mantenidos, ganadores vs perdedores
    for label in ("win", "loss"):
        ids = [tid for tid, t in hosts.items() if t["result"] == label]
        held = sum(hosts[i]["excursion_bars"] for i in ids)
        res[f"signals_per_held_bar_{label}"] = float(n_host.reindex(ids).fillna(0).sum() / held) if held else None
        res[f"share_of_{label}_trades_with_repeat_signal_pct"] = (float((n_host.reindex(ids).fillna(0) > 0).mean() * 100)
                                                                  if ids else None)
    return res


def competition(sig: pd.DataFrame) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    reached = sig[sig["route"].str.startswith(("accepted", "rejected:"))]
    groups, cands = [], []
    for ts, g in reached.groupby("bar_timestamp", sort=True):
        if len(g) < 2:
            continue
        g = g.sort_values("buy_rank_in_timestamp")
        acc = g[g["route"] == "accepted"]
        rej = g[g["route"] != "accepted"]
        cap = rej[rej["route"].isin(["rejected:LEVERAGE_EXCEEDED", "rejected:MAX_POSITIONS_REACHED"])]
        focus = len(acc) >= 1 and len(cap) >= 1
        first = g.iloc[0]
        row = {"bar_timestamp": ts, "candidates": len(g), "symbols": "|".join(g["symbol"]),
               "accepted": len(acc), "rejected": len(rej), "rejected_capacity": len(cap), "focus_group": focus,
               "open_positions_before_group": int(first["open_positions_before"]),
               "gross_exposure_before_group": float(first["gross_exposure_before"]),
               "equity_before_group": float(first["equity_before"]),
               "reject_reasons": "|".join(sorted(set(rej["route"].str.replace("rejected:", "", regex=False))))}
        if focus:
            a60 = _num(acc["mfe_r_60m"]).dropna()
            c60 = _num(cap["mfe_r_60m"]).dropna()
            if len(a60) and len(c60):
                row.update({"accepted_mean_mfe_r_60m": float(a60.mean()), "best_rejected_mfe_r_60m": float(c60.max()),
                            "ex_post_regret_mfe_r_60m": float(c60.max() - a60.mean()),
                            "ex_post_regret_warning": REGRET_WARNING})
            ar = _num(acc["actual_realized_r"]).dropna()
            cr = _num(cap.loc[cap["shadow_status"] == "closed", "shadow_realized_r"]).dropna() if "shadow_status" in cap else pd.Series(dtype=float)
            if len(ar) and len(cr):
                row.update({"accepted_mean_realized_r": float(ar.mean()), "best_rejected_shadow_r": float(cr.max()),
                            "ex_post_regret_realized_r": float(cr.max() - ar.mean())})
        groups.append(row)
        for _, cnd in g.iterrows():
            cands.append({"bar_timestamp": ts, "symbol": cnd["symbol"], "rank": int(cnd["buy_rank_in_timestamp"]),
                          "symbol_order": int(cnd["symbol_order"]), "route": cnd["route"], "focus_group": focus,
                          "mfe_r_60m": cnd.get("mfe_r_60m"), "hit_p100_60m": cnd.get("hit_p100_60m"),
                          "shadow_realized_r": cnd.get("shadow_realized_r"), "actual_realized_r": cnd.get("actual_realized_r")})
    gdf, cdf = pd.DataFrame(groups), pd.DataFrame(cands)
    summ: Dict[str, Any] = {"groups": len(gdf)}
    if len(gdf):
        summ.update({"candidate_count_distribution": {int(k): int(v) for k, v in gdf["candidates"].value_counts().sort_index().items()},
                     "focus_groups_accept_and_capacity_reject": int(gdf["focus_group"].sum())})
        f = cdf[cdf["focus_group"]]
        for lab, m in (("accepted", f["route"] == "accepted"),
                       ("capacity_rejected", f["route"].isin(["rejected:LEVERAGE_EXCEEDED", "rejected:MAX_POSITIONS_REACHED"]))):
            sub = f[m]
            summ[lab] = {"candidates": int(len(sub)), "median_mfe_r_60m": _stat(sub["mfe_r_60m"], np.median),
                         "pct_hit_p100_60m": _pct_true(sub["hit_p100_60m"]),
                         "mean_shadow_realized_r": _stat(sub["shadow_realized_r"], np.mean),
                         "mean_actual_realized_r": _stat(sub["actual_realized_r"], np.mean)}
        fg = gdf[gdf["focus_group"]]
        if "ex_post_regret_mfe_r_60m" in fg:
            rg = _num(fg["ex_post_regret_mfe_r_60m"]).dropna()
            summ["ex_post_selection_regret"] = {
                "WARNING": REGRET_WARNING, "groups": int(len(rg)),
                "median_regret_mfe_r_60m": float(rg.median()) if len(rg) else None,
                "mean_regret_mfe_r_60m": float(rg.mean()) if len(rg) else None,
                "pct_groups_best_rejected_beat_accepted_60m_mfe": float((rg > 0).mean() * 100) if len(rg) else None}
            if "ex_post_regret_realized_r" in fg:
                rr = _num(fg["ex_post_regret_realized_r"]).dropna()
                summ["ex_post_selection_regret"].update({
                    "groups_with_shadow_r": int(len(rr)), "median_regret_realized_r": float(rr.median()) if len(rr) else None,
                    "mean_regret_realized_r": float(rr.mean()) if len(rr) else None})
    return groups, cands, summ


def processing_order(sig: pd.DataFrame, cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    reached = sig[sig["route"].str.startswith(("accepted", "rejected:"))]
    by_sym = []
    for k, g in reached.groupby("symbol_order", sort=True):
        by_sym.append({"symbol_order": int(k), "symbol": g["symbol"].iloc[0], "candidates": len(g),
                       "accepted_pct": float((g["route"] == "accepted").mean() * 100),
                       "capacity_rejected_pct": float(g["route"].isin(["rejected:LEVERAGE_EXCEEDED", "rejected:MAX_POSITIONS_REACHED"]).mean() * 100),
                       "median_mfe_r_60m": _stat(g["mfe_r_60m"], np.median), "pct_hit_p100_60m": _pct_true(g["hit_p100_60m"])})
    cdf = pd.DataFrame(cands)
    by_rank = []
    if len(cdf):
        for k, g in cdf.groupby("rank", sort=True):
            by_rank.append({"rank_in_group": int(k), "candidates": len(g),
                            "accepted_pct": float((g["route"] == "accepted").mean() * 100),
                            "capacity_rejected_pct": float(g["route"].isin(["rejected:LEVERAGE_EXCEEDED", "rejected:MAX_POSITIONS_REACHED"]).mean() * 100),
                            "median_mfe_r_60m": _stat(g["mfe_r_60m"], np.median), "pct_hit_p100_60m": _pct_true(g["hit_p100_60m"])})
    return {"rule": "same-timestamp symbols are processed in --symbols (protocol universe) order", "by_symbol_order": by_sym,
            "by_rank_in_competition_group": by_rank}


FEATURES = ["atr_pct", "pullback_min_distance_ema20_atr", "trend_age_bars", "trigger_body_atr", "trigger_range_atr",
            "spread_atr", "decision_minute_et"]


def feature_routes(sig: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for r in [k for k in ROUTE_ORDER if k in set(sig["route"])]:
        g = sig[sig["route"] == r]
        row = {"route": r, "signals": len(g)}
        for f in FEATURES:
            row[f"median_{f}"] = _stat(g[f], np.median)
        for col in ("depth_group", "time_bucket", "symbol"):
            for k, v in g[col].astype(str).value_counts(normalize=True).sort_index().items():
                row[f"pct_{col}={k}"] = float(v * 100)
        rows.append(row)
    return rows


def h002_mapping(sig: pd.DataFrame, h001_trades: Sequence[Dict[str, Any]], h002_path: Path) -> Optional[Dict[str, Any]]:
    if not (h002_path / "trades.json").is_file():
        return None
    h2t = json.loads((h002_path / "trades.json").read_text(encoding="utf-8"))
    k1 = {(t["symbol"], t["entry_signal_timestamp"]) for t in h001_trades}
    routes = {(r["symbol"], r["bar_timestamp"]): r["route"] for _, r in sig[["symbol", "bar_timestamp", "route"]].iterrows()}
    rows: Dict[str, Dict[str, Any]] = {}
    for t in h2t:
        k = (t["symbol"], t["entry_signal_timestamp"])
        if k in k1:
            continue
        rt = routes.get(k, "not_an_h001_signal")
        g = rows.setdefault(rt, {"h001_route": rt, "h002_only_trades": 0, "h002_pnl": 0.0, "h002_r": 0.0, "wins": 0})
        g["h002_only_trades"] += 1
        g["h002_pnl"] += t["realized_pnl"]
        g["h002_r"] += t["realized_r"]
        g["wins"] += int(t["result"] == "win")
    out = sorted(rows.values(), key=lambda r: -r["h002_only_trades"])
    return {"source": str(h002_path), "h002_only_total": sum(r["h002_only_trades"] for r in out),
            "h002_only_pnl": sum(r["h002_pnl"] for r in out), "by_h001_route": out}


# ================================================================ orquestación
def build(protocol: Dict[str, Any], split: Dict[str, Any], data_dir: Path, out_dir: Path,
          stored: Path = H001_STORED, h002_stored: Path = H002_STORED, isolated: bool = True, workers: int = 0,
          verify: bool = True) -> Dict[str, Any]:
    _guard_out_dir(out_dir)
    symbols = list(protocol["universe"]["symbols"])
    loaded = ap.load_development_bars(data_dir, symbols, split)
    result, stages, eng = rerun_observed(protocol, split, loaded["bars"])
    ident = verify_identical(result, out_dir / "h001_rerun_check", stored) if verify else None
    if ident is not None and not ident["identical"]:
        raise RuntimeError(f"H001 re-run differs from stored run: {ident['files_identical']}")
    s_utc, e_utc = ap._start_utc(split["start"]), ap._end_utc(split["end"])
    ind = {s: ap.symbol_indicators(loaded["bars"][s]) for s in symbols}
    pos = {s: {ts: k for k, ts in enumerate(ind[s]["ts"])} for s in symbols}
    risk_cfg = build_risk_config(production_args())
    accepted_trades = {(t["symbol"], t["entry_signal_timestamp"]): t for t in result.trades}
    rows = []
    for st in stages:
        if not (s_utc <= pd.Timestamp(st["bar_timestamp"]) < e_utc):
            raise HygieneError(f"señal fuera de development: {st['bar_timestamp']}")
        s, t = st["symbol"], pos[st["symbol"]][st["bar_timestamp"]]
        f = ap.signal_features(ind[s], t)
        rps = reconstruct_risk_ps(float(ind[s]["c"][t]), ind[s]["h"], ind[s]["l"], ind[s]["c"], t, risk_cfg)
        row = {**st, "route": route_of(st["stage"], st["reason"]), "session_date_et": str(ind[s]["date"][t]),
               "signal_time_et": f"{int(ind[s]['minute'][t]) // 60:02d}:{int(ind[s]['minute'][t]) % 60:02d}",
               **f, "reconstructed_risk_ps": rps, **shadow_fixed_horizon(ind[s], t, rps)}
        tr = accepted_trades.get((s, st["bar_timestamp"]))
        row["actual_trade_id"] = tr["trade_id"] if tr else None
        row["actual_risk_ps"] = tr["risk_per_share_modeled"] if tr else None
        row["actual_realized_r"] = tr["realized_r"] if tr else None
        rows.append(row)
    sig = pd.DataFrame(rows)
    sig["key"] = sig["symbol"] + "|" + sig["bar_timestamp"]
    acc = sig[sig["route"] == "accepted"]
    rps_check = {"accepted": int(len(acc)),
                 "exact_matches": int((acc["reconstructed_risk_ps"] == acc["actual_risk_ps"]).sum()),
                 "max_abs_diff": float((acc["reconstructed_risk_ps"] - acc["actual_risk_ps"]).abs().max()) if len(acc) else None}
    iso_validation = None
    if isolated:
        jobs = []
        for r in sig.itertuples():
            if r.route == "blocked:symbol_already_open":
                continue
            t = pos[r.symbol][r.bar_timestamp]
            lo = max(0, t - ISOLATED_PRE_BARS)
            sl = loaded["bars"][r.symbol].iloc[lo: t + 1 + ISOLATED_POST_BARS[-1]]   # solo lo necesario por trabajo
            jobs.append((r.symbol, sl, t - lo, float(r.equity_before), split["end"], r.key))
        if workers and workers > 1:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                iso = list(pool.map(isolated_shadow, jobs, chunksize=64))
        else:
            iso = [isolated_shadow(j) for j in jobs]
        sig = sig.merge(pd.DataFrame(iso), on="key", how="left")
        iso_validation = validate_isolated(sig, result.trades)
    q = route_quality(sig)
    groups, cands, comp = competition(sig)
    summary = {
        "report": "h001_opportunity_autopsy", "evidence": "DEVELOPMENT EVIDENCE only", "disclaimer": DISCLAIMER,
        "dates": [split["start"], split["end"]], "h001_rerun_identical": ident,
        "shadow_conventions": {"entry": "next available 5Min bar open of the symbol + 5 bps", "r_denominator":
                               "reconstructed H001 risk_per_share (pure RiskManager formula)",
                               "horizons": "closes of bars E..E+h-1 (3/6/12 bars) and E..last bar of E's session; "
                                           "horizons count bars chronologically and may cross sessions (flagged)",
                               "mae_sign": "positive = adverse", "isolated_trade": "unchanged BacktestEngine, empty single-symbol "
                               "portfolio, one forced BUY at the signal bar, equity = H001 equity at that decision"},
        "risk_ps_reconstruction_check": rps_check,
        "isolated_shadow_validation": iso_validation,
        "funnel": {"generated": len(sig), "by_route": {k: int(v) for k, v in sig["route"].value_counts().items()}},
        "route_quality": q,
        "in_position": in_position_analysis(sig, result.trades),
        "competition": comp,
        "processing_order": processing_order(sig, cands),
        "feature_routes": feature_routes(sig),
        "h002_path_divergence": h002_mapping(sig, result.trades, h002_stored),
        "halt_confound_note": ("Halt-blocked signals occur only after prior portfolio outcomes (a $300 day or 3 straight "
                               "losses), so they are NOT independent samples of H001 opportunities."),
        "in_position_note": "symbol_already_open signals are overlapping/non-feasible under H001: fixed-horizon only, "
                            "excluded from any isolated shadow trade statistics.",
    }
    return {"summary": summary, "signals": sig, "groups": groups, "candidates": cands}


def validate_isolated(sig: pd.DataFrame, trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Los trades sombra aislados deben reproducir a los trades aceptados reales (entrada, motivo, R)."""
    acc = sig[sig["route"] == "accepted"]
    by = {(t["symbol"], t["entry_signal_timestamp"]): t for t in trades}
    n = ok_entry = ok_reason = ok_r = 0
    bad = []
    for r in acc.itertuples():
        t = by.get((r.symbol, r.bar_timestamp))
        if t is None:
            continue
        n += 1
        e = r.shadow_status == "closed" and r.shadow_entry_fill_ts == t["entry_fill_timestamp"] and \
            r.shadow_entry_fill_price == t["entry_fill_price"]
        rs = r.shadow_status == "closed" and r.shadow_exit_reason == t["exit_reason"]
        rr = r.shadow_status == "closed" and math.isclose(r.shadow_realized_r, t["realized_r"], rel_tol=0, abs_tol=1e-9)
        ok_entry += e
        ok_reason += rs
        ok_r += rr
        if not (e and rs and rr) and len(bad) < 20:
            bad.append({"key": r.key, "shadow": [r.shadow_status, getattr(r, "shadow_exit_reason", None),
                                                 getattr(r, "shadow_realized_r", None)],
                        "actual": [t["exit_reason"], t["realized_r"]]})
    validated = n > 0 and ok_entry == ok_reason == ok_r == n
    return {"accepted_trades_checked": n, "entry_timing_and_price_match": ok_entry, "exit_reason_match": ok_reason,
            "realized_r_match_1e-9": ok_r, "validated": validated, "examples_of_mismatch": bad,
            "use": "isolated shadow trades reported as secondary counterfactual" if validated
                   else "NOT validated: fixed-horizon excursions only"}


# ================================================================ salidas
def _csv(path: Path, rows: List[Dict[str, Any]]) -> Path:
    fields = list(dict.fromkeys(k for r in rows for k in r)) if rows else ["empty"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


SHADOW_COLS = (["key", "symbol", "bar_timestamp", "route", "shadow_valid", "shadow_entry_ts", "shadow_entry_price",
                "shadow_risk_ps", "shadow_entry_next_session", "eos_bars"]
               + [f"{m}_{h}" for h in ("15m", "30m", "60m", "eos") for m in
                  ("mfe_r", "mae_r", "close_r", "hit_p025", "hit_p050", "hit_p100", "hit_m050", "hit_m100")]
               + [f"crosses_session_{h}" for h in ("15m", "30m", "60m")])


def _guard_out_dir(out_dir: Path) -> None:
    for protected in (H001_STORED, H002_STORED):
        if Path(out_dir).resolve() == protected.resolve():
            raise HygieneError("no se sobrescriben resultados originales de H001/H002")


def write(res: Dict[str, Any], out_dir: Path) -> List[Path]:
    _guard_out_dir(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    s, sig = res["summary"], res["signals"]
    paths = []
    p = out_dir / "opportunity_summary.json"
    p.write_text(to_json(s), encoding="utf-8")
    paths.append(p)
    route_cols = ["key", "symbol", "bar_timestamp", "decision_timestamp", "session_date_et", "signal_time_et",
                  "buy_rank_in_timestamp", "symbol_order", "stage", "reason", "route", "open_positions_before",
                  "gross_exposure_before", "equity_before", "reconstructed_risk_ps", "actual_trade_id", "actual_risk_ps",
                  "actual_realized_r"]
    sig[route_cols].to_csv(out_dir / "signal_routes.csv", index=False)
    paths.append(out_dir / "signal_routes.csv")
    sig[[c for c in SHADOW_COLS if c in sig]].to_csv(out_dir / "shadow_fixed_horizon.csv", index=False)
    paths.append(out_dir / "shadow_fixed_horizon.csv")
    q = s["route_quality"]
    paths.append(_csv(out_dir / "route_quality.csv", q))
    paths.append(_csv(out_dir / "risk_reject_quality.csv",
                      [r for r in q if r["route"] == "accepted" or r["route"].startswith("rejected:")]))
    paths.append(_csv(out_dir / "halt_quality.csv", [r for r in q if r["route"] in
                                                     ("accepted", "blocked:daily_profit_halt", "blocked:loss_streak_halt")]))
    paths.append(_csv(out_dir / "competition_groups.csv", res["groups"]))
    paths.append(_csv(out_dir / "competition_candidates.csv", res["candidates"]))
    po = s["processing_order"]
    paths.append(_csv(out_dir / "processing_order.csv", [dict(r, table="by_symbol_order") for r in po["by_symbol_order"]]
                      + [dict(r, table="by_rank_in_group") for r in po["by_rank_in_competition_group"]]))
    paths.append(_csv(out_dir / "signal_feature_routes.csv", s["feature_routes"]))
    if "shadow_status" in sig:
        cols = ["key", "symbol", "bar_timestamp", "route", "shadow_status", "shadow_decision", "shadow_reject_reason",
                "shadow_exit_reason", "shadow_realized_r", "shadow_realized_pnl", "shadow_entry_fill_ts",
                "shadow_entry_fill_price", "shadow_initial_qty", "shadow_risk_ps_engine", "shadow_mfe_r", "shadow_mae_r",
                "actual_realized_r"]
        sig[[c for c in cols if c in sig]].to_csv(out_dir / "shadow_trades.csv", index=False)
        paths.append(out_dir / "shadow_trades.csv")
        paths.append(_csv(out_dir / "shadow_trade_quality.csv", [{k: v for k, v in r.items() if k.startswith(("route", "signals", "isolated"))} for r in q]))
    if s["h002_path_divergence"]:
        paths.append(_csv(out_dir / "h002_path_divergence.csv", s["h002_path_divergence"]["by_h001_route"]))
    return paths


def _f(x, fmt="{:+.3f}"):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def _p(x):
    return "-" if x is None else f"{x:.1f}%"


def format_report(s: Dict[str, Any]) -> str:
    L = ["H001 OPPORTUNITY / SIGNAL-SELECTION AUTOPSY (DEVELOPMENT 2024-01-02 → 2025-12-31; descriptive only)", "─" * 76]
    ident = s["h001_rerun_identical"]
    L.append(f"H001 re-run identical to stored: {ident['identical'] if ident else 'not checked'} | R_ps reconstruction: "
             f"{s['risk_ps_reconstruction_check']['exact_matches']}/{s['risk_ps_reconstruction_check']['accepted']} exact")
    iv = s["isolated_shadow_validation"]
    if iv:
        L.append(f"Isolated shadow validation: entry {iv['entry_timing_and_price_match']}/{iv['accepted_trades_checked']}, "
                 f"exit reason {iv['exit_reason_match']}, R {iv['realized_r_match_1e-9']} -> "
                 f"{'VALIDATED' if iv['validated'] else 'NOT VALIDATED'}")
    L += ["", "Signal funnel:"] + [f"  {k:<36}{v:>6}" for k, v in sorted(s["funnel"]["by_route"].items(), key=lambda kv: -kv[1])]
    L += ["", "Accepted vs blocked/rejected (shadow, closes; R = reconstructed H001 R):",
          f"  {'route':<34}{'n':>6}{'valid':>6}{'MFE15':>7}{'MFE30':>7}{'MFE60':>7}{'MAE60':>7}{'≥.5R':>7}{'≥1R':>7}"
          f"{'≤-1R':>7}{'EOS R':>7}{'iso n':>6}{'isoE[R]':>8}{'isoPF':>6}"]
    for r in s["route_quality"]:
        L.append(f"  {r['route']:<34}{r['signals']:>6}{r['valid_shadow']:>6}{_f(r['median_mfe_r_15m'], '{:+.2f}'):>7}"
                 f"{_f(r['median_mfe_r_30m'], '{:+.2f}'):>7}{_f(r['median_mfe_r_60m'], '{:+.2f}'):>7}"
                 f"{_f(r['median_mae_r_60m'], '{:+.2f}'):>7}{_p(r['pct_hit_p050_60m']):>7}{_p(r['pct_hit_p100_60m']):>7}"
                 f"{_p(r['pct_hit_m100_60m']):>7}{_f(r['median_close_r_eos'], '{:+.2f}'):>7}"
                 f"{r.get('isolated_closed', 0):>6}{_f(r.get('isolated_expectancy_r')):>8}{_f(r.get('isolated_pf_r'), '{:.2f}'):>6}")
    L.append("  (MFE/MAE medians; ≥.5R/≥1R/≤-1R = % of valid shadows within 60 min; iso = isolated shadow trades, "
             "counterfactual; symbol_already_open excluded from iso)")
    ip = s["in_position"]
    if ip.get("host_found"):
        L += ["", "Symbol-already-open signals (non-feasible under H001):",
              f"  {ip['signals']} signals; host trade later won {ip['signals_in_eventual_winners']} / lost "
              f"{ip['signals_in_eventual_losers']}; host above entry at signal {_p(ip['pct_signals_host_above_entry'])}; "
              f"median host unrealized {_f(ip['median_host_unrealized_r_at_signal'])}R",
              f"  repeat signals per held bar: winners {_f(ip['signals_per_held_bar_win'], '{:.3f}')} vs losers "
              f"{_f(ip['signals_per_held_bar_loss'], '{:.3f}')}"]
    c = s["competition"]
    L += ["", "Same-timestamp competition (≥2 signals reaching RiskManager):",
          f"  groups {c['groups']} | candidates per group {c.get('candidate_count_distribution')} | "
          f"groups with accept + capacity reject {c.get('focus_groups_accept_and_capacity_reject')}"]
    for lab in ("accepted", "capacity_rejected"):
        if lab in c:
            v = c[lab]
            L.append(f"  {lab:<18} n={v['candidates']:<5} median MFE60 {_f(v['median_mfe_r_60m'], '{:+.2f}')} "
                     f"≥1R {_p(v['pct_hit_p100_60m'])} | mean shadow R {_f(v['mean_shadow_realized_r'])} "
                     f"| mean actual R {_f(v['mean_actual_realized_r'])}")
    po = s["processing_order"]
    L += ["", f"Processing-order bias ({po['rule']}):"]
    for r in po["by_symbol_order"]:
        L.append(f"  order {r['symbol_order']} {r['symbol']:<5} n={r['candidates']:<5} accepted {_p(r['accepted_pct'])} "
                 f"capacity-rej {_p(r['capacity_rejected_pct'])} median MFE60 {_f(r['median_mfe_r_60m'], '{:+.2f}')} "
                 f"≥1R {_p(r['pct_hit_p100_60m'])}")
    for r in po["by_rank_in_competition_group"]:
        L.append(f"  rank {r['rank_in_group']} in group: n={r['candidates']:<5} accepted {_p(r['accepted_pct'])} "
                 f"median MFE60 {_f(r['median_mfe_r_60m'], '{:+.2f}')} ≥1R {_p(r['pct_hit_p100_60m'])}")
    reg = c.get("ex_post_selection_regret")
    L += ["", "Ex-post selection regret:"]
    if reg:
        L.append(f"  groups {reg['groups']} | median regret (best rejected MFE60 − accepted MFE60) "
                 f"{_f(reg['median_regret_mfe_r_60m'], '{:+.2f}')}R | best rejected beat accepted in "
                 f"{_p(reg['pct_groups_best_rejected_beat_accepted_60m_mfe'])} of groups"
                 + (f" | median shadow-R regret {_f(reg.get('median_regret_realized_r'), '{:+.2f}')}R" if reg.get("groups_with_shadow_r") else ""))
    L.append(f"  WARNING: {REGRET_WARNING}")
    h2 = s["h002_path_divergence"]
    if h2:
        L += ["", f"H002 path-divergence connection ({h2['h002_only_total']} H002-only trades, P&L ${h2['h002_only_pnl']:,.0f}):"]
        for r in h2["by_h001_route"]:
            L.append(f"  {r['h001_route']:<34}{r['h002_only_trades']:>5} trades  P&L ${r['h002_pnl']:>10,.0f}  R {r['h002_r']:+.1f}")
    L += ["", s["halt_confound_note"], s["disclaimer"]]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.h001_opportunity_autopsy",
                                description="Autopsia de oportunidades/selección de H001, SOLO development (sin bypass).")
    p.add_argument("--split", default="development", help="Solo 'development' es aceptado.")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, default=Path("data") / "research_v1" / "h001_opportunity_autopsy")
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args(argv)
    try:
        protocol = load_protocol(a.protocol)
        split = development_scope(protocol, a.split)
        res = build(protocol, split, a.data_dir, a.output_dir, workers=a.workers)
        paths = write(res, a.output_dir)
    except HygieneError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    print(format_report(res["summary"]))
    print("\nArchivos: " + ", ".join(str(x) for x in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
