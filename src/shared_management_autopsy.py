# src/shared_management_autopsy.py
"""
SHARED TRADE-MANAGEMENT AUTOPSY — H001 vs H003, DEVELOPMENT ONLY (diagnóstico de solo lectura).
Ambas hipótesis están cerradas (REJECTED_AT_DEVELOPMENT). Nada aquí cambia reglas, riesgo, gestión ni vivo.

    python -m src.shared_management_autopsy --data-dir data/historical \
        --output-dir data/research_v1/shared_management_autopsy

1. Re-corre H001 y H003 SOLO en development con _ExitObserver (subclase de BacktestEngine que solo
   registra, al enviarse cada orden de salida, el stop/take vigentes y el cierre de la vela de
   detección) y prueba que trades.csv/json, daily_results.csv, equity_curve.csv y summary.json son
   idénticos a las corridas guardadas.
2. Métricas por trade con definiciones idénticas para ambas (excursiones por CIERRES 5Min).
3. Anatomía de stops, latencia detección→fill, sobrepaso de −1R, trayectoria post-stop (USA
   INFORMACIÓN FUTURA: nunca una regla), giveback, take-profit/scale-out, ciclo de vida, geometría de
   riesgo, descomposición de P&L, firma común, diferencias por familia, filtro RR, hora/overnight,
   símbolos y correlaciones.

Convención de signos
--------------------
detection_r = (cierre de la vela de detección − F) / R_ps ; fill_r = (precio del fill de salida − F) / R_ps
latency_cost_r = fill_r − detection_r   (NEGATIVO = el fill fue PEOR que el cierre de detección: costo adverso)
detection_gap_r = (cierre de detección − stop vigente) / R_ps  (<= 0 en stops: el cierre ya perforó el stop)
F = entry_fill_price real; R_ps = risk_per_share_modeled (denominador de realized_r).
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
from .backtest_engine import BREAKEVEN_EPS, BacktestConfig, BacktestEngine, production_args
from .backtest_report import _profit_factor, summarize, to_json, write_outputs
from .research_protocol import DEFAULT_PROTOCOL_PATH, load_protocol
from .risk_manager_avanzado import RiskManager
from .strategy_v2_h001 import ENGINE_LOOKBACK, HygieneError, TrendPullbackH001
from .strategy_v2_h003 import ConsolidationBreakoutH003

NY = "America/New_York"
STRATEGIES = {"H001": (TrendPullbackH001, Path("data") / "research_v1" / "h001_development"),
              "H003": (ConsolidationBreakoutH003, Path("data") / "research_v1" / "h003_development")}
REACH = (("reached_0_25r", 0.25), ("reached_0_50r", 0.5), ("reached_1_00r", 1.0), ("reached_1_50r", 1.5),
         ("reached_2_00r", 2.0))
POST_STOP_BARS = (1, 2, 3, 6, 12)
FUTURE_WARNING = "THIS USES FUTURE INFORMATION. IT MUST NOT BE USED AS A LIVE EXIT RULE."
DISCLAIMER = ("Descriptive DEVELOPMENT-only diagnostics of CLOSED hypotheses H001 and H003. No stop, exit, risk or "
              "parameter change is proposed; correlation is not causation; no H004.")
EXIT_REASONS = ("giveback_close", "stop_hit", "take_profit_hit")
development_scope = ap.development_scope


# ================================================================ observador de salidas (solo lectura)
class _ExitObserver(BacktestEngine):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.exit_submits: List[Dict[str, Any]] = []

    def _submit_exit(self, sd, i, qty, purpose):
        meta = self.book.get(sd.name, {})
        trade = self.open_trades.get(sd.name, {})
        self.exit_submits.append({"trade_id": trade.get("trade_id"), "symbol": sd.name, "purpose": purpose,
                                  "detection_bar": sd.iso[i], "detection_decision_ts": sd.decision_iso[i],
                                  "detection_close": float(sd.close[i]), "qty": int(qty),
                                  "stop_at_detection": meta.get("stop"), "take_at_detection": meta.get("take"),
                                  "modeled_entry": meta.get("entry"), "be_done": bool(meta.get("be_done"))})
        super()._submit_exit(sd, i, qty, purpose)


def _run_one(job):
    name, cfg_kwargs, bars = job
    strat_cls = STRATEGIES[name][0]
    eng = _ExitObserver(BacktestConfig(**cfg_kwargs), bars, production_args({"lookback": ENGINE_LOOKBACK}), strat_cls())
    return name, eng.run(), eng.exit_submits


def rerun(protocol, split, bars, names=("H001", "H003"), workers: int = 2):
    ex = protocol["execution_defaults"]
    cfg = dict(symbols=list(protocol["universe"]["symbols"]), timeframe="5Min", start=split["start"], end=split["end"],
               initial_equity=float(ex["initial_equity"]), slippage_bps=float(ex["slippage_bps"]),
               commission=float(ex["commission_per_fill"]), record_evaluations=True, window_hours_limit=False)
    jobs = [(n, cfg, bars) for n in names]
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            out = list(pool.map(_run_one, jobs))
    else:
        out = [_run_one(j) for j in jobs]
    return {n: {"result": r, "exit_submits": s} for n, r, s in out}


def verify_identical(result, out_dir: Path, stored: Path) -> Dict[str, Any]:
    write_outputs(result, summarize(result), out_dir)
    files = {f: (out_dir / f).read_bytes() == (stored / f).read_bytes()
             for f in ("trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv")}
    files["summary.json"] = (json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
                             == json.loads((stored / "summary.json").read_text(encoding="utf-8")))
    return {"stored_run": str(stored), "files_identical": files, "identical": all(files.values())}


# ================================================================ métricas por trade
def _minutes(a: str, b: str) -> float:
    return (pd.Timestamp(b) - pd.Timestamp(a)).total_seconds() / 60.0


def held_path(ind, pos, t: Dict[str, Any]) -> Tuple[int, int]:
    """Índices [e, x) de las velas con acciones al cierre: desde la vela del fill de entrada hasta la previa al fill final."""
    return pos[t["entry_fill_timestamp"]], pos[t["exit_fill_timestamp"]]


def first_reach_minutes(ind, e: int, x: int, fill: float, rps: float, level: float, entry_ts: str) -> Optional[float]:
    for k in range(e, x):
        if (ind["c"][k] - fill) / rps >= level:
            return _minutes(entry_ts, str(ind["ts"][k])) + 5.0
    return None


def trade_rows(name: str, trades: Sequence[Dict[str, Any]], submits: Sequence[Dict[str, Any]],
               ind: Dict[str, Dict[str, np.ndarray]]) -> List[Dict[str, Any]]:
    pos = {s: {ts: k for k, ts in enumerate(v["ts"])} for s, v in ind.items()}
    sub = {(s["trade_id"], s["purpose"], s["detection_bar"]): s for s in submits}
    rows = []
    for t in trades:
        s, fill, rps = t["symbol"], float(t["entry_fill_price"]), float(t["risk_per_share_modeled"])
        d = ind[s]
        e, x = held_path(d, pos[s], t)
        closes = d["c"][e:x]
        xr = (closes - fill) / rps if len(closes) else np.array([np.nan])
        k_sig = pos[s][t["entry_signal_timestamp"]]
        atr = RiskManager._atr(d["h"][:k_sig + 1], d["l"][:k_sig + 1], d["c"][:k_sig + 1], 14)
        sig_close = float(d["c"][k_sig])
        last = t["legs"][-1]
        so = [l for l in t["legs"] if l["purpose"] == "scale_out"]
        slip = sum((l["reference_open"] - l["price"]) * l["qty"] for l in t["legs"])
        slip += (fill - fill / 1.0005) * t["initial_qty"]
        det = sub.get((t["trade_id"], last["purpose"], last["signal_bar_timestamp"]))
        slip_px = sig_close * 0.0005
        row = {
            "strategy": name, "trade_id": t["trade_id"], "symbol": s, "entry_signal_timestamp": t["entry_signal_timestamp"],
            "entry_fill_timestamp": t["entry_fill_timestamp"], "exit_fill_timestamp": t["exit_fill_timestamp"],
            "final_exit_reason": t["exit_reason"], "result": t["result"], "realized_pnl": t["realized_pnl"],
            "realized_r": t["realized_r"], "entry_fill_price": fill, "modeled_entry": t["modeled_entry"],
            "exit_fill_price": t["exit_fill_price"], "initial_risk_per_share": rps, "initial_stop_price": t["initial_stop"],
            "initial_take_profit_price": t["initial_take"], "initial_qty": t["initial_qty"],
            "holding_minutes": t["holding_seconds"] / 60.0, "bars_held": int(x - e),
            "mfe_r": t["mfe_r"], "mae_r": t["mae_r"], "mfe_timestamp": t["mfe_timestamp"], "mae_timestamp": t["mae_timestamp"],
            "minutes_to_mfe": t["minutes_to_mfe"], "minutes_to_mae": t["minutes_to_mae"],
            "mfe_r_recomputed": float(np.nanmax(xr)), "mae_r_recomputed": float(-np.nanmin(xr)),
            "minutes_to_0_25r": first_reach_minutes(d, e, x, fill, rps, 0.25, t["entry_fill_timestamp"]),
            "minutes_to_1_00r": first_reach_minutes(d, e, x, fill, rps, 1.0, t["entry_fill_timestamp"]),
            "scale_out_legs": len(so), "scale_out_pnl": float(sum(l["realized_pnl"] for l in so)),
            "overnight": pd.Timestamp(t["entry_fill_timestamp"]).tz_convert(NY).date() != pd.Timestamp(t["exit_fill_timestamp"]).tz_convert(NY).date(),
            "entry_decision_minute_et": int(pd.Timestamp(t["entry_decision_timestamp"]).tz_convert(NY).hour * 60
                                            + pd.Timestamp(t["entry_decision_timestamp"]).tz_convert(NY).minute),
            # geometría
            "atr_at_signal": atr, "atr_pct": atr / sig_close * 100, "entry_gap_r": (fill - t["modeled_entry"]) / rps,
            "stop_distance_usd": rps, "stop_distance_from_fill_usd": fill - t["initial_stop"],
            "stop_distance_pct": rps / t["modeled_entry"] * 100, "stop_distance_atr": rps / atr,
            "take_distance_atr": (t["initial_take"] - t["modeled_entry"]) / atr,
            "initial_stop_r_from_fill": (t["initial_stop"] - fill) / rps,
            "approx_initial_rr": ((t["initial_take"] - t["modeled_entry"]) - slip_px) / (rps + slip_px),
            "slippage_cost_usd": slip, "cost_r": slip / (rps * t["initial_qty"]),
        }
        for nm, lvl in REACH:
            row[nm] = bool(t["mfe_r"] >= lvl)
        # detección vs fill del tramo final
        row.update({"final_leg_purpose": last["purpose"], "detection_bar": last["signal_bar_timestamp"],
                    "detection_decision_ts": last["decision_timestamp"], "final_fill_ts": last["fill_timestamp"],
                    "next_bar_open": last["reference_open"], "final_exit_fill": last["price"], "final_leg_qty": last["qty"]})
        if det is not None:
            dc = det["detection_close"]
            row.update({"detection_bar_close": dc, "stop_price_at_detection": det["stop_at_detection"],
                        "be_done_at_detection": det["be_done"], "detection_r": (dc - fill) / rps,
                        "fill_r": (last["price"] - fill) / rps,
                        "latency_cost_r": (last["price"] - dc) / rps,
                        "latency_cost_usd": (last["price"] - dc) * last["qty"],
                        "detection_gap_r": (dc - det["stop_at_detection"]) / rps if det["stop_at_detection"] is not None else None})
        rows.append(row)
    return rows


def lifecycle(mfe: float, r: float) -> str:
    if mfe >= 2.0:
        return "E_reached_2r"
    if mfe >= 1.0:
        return "D_reached_1r"
    if mfe >= 0.5:
        return "C_moderate_progress"
    if mfe >= 0.25:
        return "B_weak_progress"
    return "A_immediate_failure" if r < 0 else "F_no_progress_nonnegative"


def post_stop_path(ind, pos, row) -> Dict[str, Any]:
    """Cierres DESPUÉS del fill de salida (vela del fill = +1). R respecto de la entrada y R originales. FUTURO."""
    d = ind[row["symbol"]]
    x = pos[row["symbol"]][row["exit_fill_timestamp"]]
    fill, rps, exitp = row["entry_fill_price"], row["initial_risk_per_share"], row["final_exit_fill"]
    out = {"warning": FUTURE_WARNING}
    for k in POST_STOP_BARS:
        if x + k - 1 < len(d["c"]):
            cl = d["c"][x:x + k]
            r = (cl - fill) / rps
            out.update({f"close_r_{k}": float(r[-1]), f"best_r_{k}": float(r.max()), f"worst_r_{k}": float(r.min()),
                        f"recovered_above_entry_{k}": bool((cl > fill).any()), f"hit_p050_{k}": bool((r >= 0.5).any()),
                        f"hit_p100_{k}": bool((r >= 1.0).any()), f"below_exit_{k}": bool((cl < exitp).any()),
                        f"crosses_session_{k}": bool(d["date"][x + k - 1] != d["date"][x])})
        else:
            out.update({f"{m}_{k}": None for m in ("close_r", "best_r", "worst_r", "recovered_above_entry", "hit_p050",
                                                   "hit_p100", "below_exit", "crosses_session")})
    return out


# ================================================================ agregados
def _s(x):
    x = pd.to_numeric(pd.Series(x), errors="coerce").dropna()
    return x


def _q(x, fn):
    x = _s(x)
    return float(fn(x)) if len(x) else None


def _pct(mask):
    m = pd.Series(mask).dropna()
    return float(m.astype(bool).mean() * 100) if len(m) else None


def overshoot_buckets(r: pd.Series) -> Dict[str, Any]:
    r = _s(r)
    n = len(r)
    p = lambda m: float(m.sum() / n * 100) if n else None  # noqa: E731
    return {"n": n, "pct_better_than_-1.0": p(r > -1.0), "pct_-1.0_to_-1.25": p((r <= -1.0) & (r > -1.25)),
            "pct_-1.25_to_-1.5": p((r <= -1.25) & (r > -1.5)), "pct_worse_than_-1.5": p(r <= -1.5),
            "pct_worse_than_-2.0": p(r <= -2.0), "median_r": _q(r, np.median), "mean_r": _q(r, np.mean),
            "largest_loss_r": _q(r, np.min)}


def loss_split(df: pd.DataFrame) -> Dict[str, Any]:
    """P&L perdido hasta −1R vs sobrepaso más allá de −1R (1R$ = R_ps × qty inicial)."""
    los = df[df["realized_pnl"] < 0]
    one_r = los["initial_risk_per_share"] * los["initial_qty"]
    within = -np.minimum(-los["realized_pnl"], one_r)
    beyond = los["realized_pnl"] - within
    return {"losing_trades": int(len(los)), "gross_loss": float(los["realized_pnl"].sum()),
            "loss_up_to_minus_1r": float(within.sum()), "overshoot_beyond_minus_1r": float(beyond.sum())}


def stop_anatomy(df: pd.DataFrame) -> Dict[str, Any]:
    st = df[df["final_exit_reason"] == "stop_hit"]
    lat = _s(st["latency_cost_r"])
    return {
        "stop_trades": int(len(st)), "pct_never_0_25r": _pct(st["mfe_r"] < 0.25), "pct_never_0_50r": _pct(st["mfe_r"] < 0.5),
        "pct_reached_1r_before_stop": _pct(st["mfe_r"] >= 1.0), "median_mfe_r": _q(st["mfe_r"], np.median),
        "median_mae_r": _q(st["mae_r"], np.median), "median_bars_held": _q(st["bars_held"], np.median),
        "median_minutes_held": _q(st["holding_minutes"], np.median), "median_realized_r": _q(st["realized_r"], np.median),
        "pct_break_even_moved_before_stop": _pct(st["be_done_at_detection"]),
        "latency": {"sign_convention": "latency_cost_r = fill_r − detection_r; negative = adverse",
                    "mean_r": _q(lat, np.mean), "median_r": _q(lat, np.median), "q25_r": _q(lat, lambda s: s.quantile(.25)),
                    "q75_r": _q(lat, lambda s: s.quantile(.75)), "worst_r": _q(lat, np.min),
                    "total_usd": float(_s(st["latency_cost_usd"]).sum()),
                    "median_detection_gap_r_close_vs_stop": _q(st["detection_gap_r"], np.median),
                    "median_detection_r": _q(st["detection_r"], np.median), "median_fill_r": _q(st["fill_r"], np.median)},
        "overshoot": overshoot_buckets(st["realized_r"]), "loss_split_stop_trades": loss_split(st),
        "loss_split_all_losers": loss_split(df),
    }


def post_stop_summary(ps: pd.DataFrame) -> Dict[str, Any]:
    out = {"WARNING": FUTURE_WARNING, "stop_trades": int(len(ps))}
    for k, lab in ((3, "15m"), (6, "30m"), (12, "60m")):
        out[lab] = {"n": int(ps[f"close_r_{k}"].notna().sum()), "pct_recovered_above_entry": _pct(ps[f"recovered_above_entry_{k}"]),
                    "pct_hit_p050": _pct(ps[f"hit_p050_{k}"]), "pct_hit_p100": _pct(ps[f"hit_p100_{k}"]),
                    "pct_below_actual_exit": _pct(ps[f"below_exit_{k}"]), "median_close_r": _q(ps[f"close_r_{k}"], np.median),
                    "median_best_r": _q(ps[f"best_r_{k}"], np.median), "median_worst_r": _q(ps[f"worst_r_{k}"], np.median)}
    for k in (1, 2):
        out[f"{k}bar"] = {"median_close_r": _q(ps[f"close_r_{k}"], np.median), "pct_below_actual_exit": _pct(ps[f"below_exit_{k}"])}
    return out


def giveback_autopsy(df: pd.DataFrame) -> Dict[str, Any]:
    g = df[df["final_exit_reason"] == "giveback_close"]
    pos = g[g["mfe_r"] > 0]
    bands = []
    for lab, m in (("A_loss", g["realized_r"] < 0), ("B_0_to_0.25", (g["realized_r"] >= 0) & (g["realized_r"] < 0.25)),
                   ("C_0.25_to_0.5", (g["realized_r"] >= 0.25) & (g["realized_r"] < 0.5)),
                   ("D_0.5_to_1", (g["realized_r"] >= 0.5) & (g["realized_r"] < 1.0)), ("E_ge_1", g["realized_r"] >= 1.0)):
        sub = g[m]
        bands.append({"band": lab, "count": int(len(sub)), "pnl": float(sub["realized_pnl"].sum()),
                      "avg_realized_r": _q(sub["realized_r"], np.mean), "avg_mfe_r": _q(sub["mfe_r"], np.mean)})
    return {"trades": int(len(g)), "win_rate_pct": _pct(g["result"] == "win"), "pnl": float(g["realized_pnl"].sum()),
            "avg_realized_r": _q(g["realized_r"], np.mean), "median_realized_r": _q(g["realized_r"], np.median),
            "avg_mfe_r": _q(g["mfe_r"], np.mean), "median_mfe_r": _q(g["mfe_r"], np.median),
            "avg_mae_r": _q(g["mae_r"], np.mean), "median_mae_r": _q(g["mae_r"], np.median),
            "aggregate_capture_realized_over_mfe": float(pos["realized_r"].sum() / pos["mfe_r"].sum()) if pos["mfe_r"].sum() > 0 else None,
            "mean_mfe_left_on_table_r": _q(g["mfe_r"] - g["realized_r"], np.mean),
            **{f"pct_{nm}": _pct(g[nm]) for nm, _ in REACH if nm in ("reached_0_25r", "reached_0_50r", "reached_1_00r", "reached_2_00r")},
            "bands": bands}


def tp_scaleout(df: pd.DataFrame) -> Dict[str, Any]:
    tp = df[df["final_exit_reason"] == "take_profit_hit"]
    so = df[df["scale_out_legs"] > 0]
    return {"take_profit": {"count": int(len(tp)), "pnl": float(tp["realized_pnl"].sum()),
                            "avg_realized_r": _q(tp["realized_r"], np.mean), "avg_mfe_r": _q(tp["mfe_r"], np.mean),
                            "avg_mae_r": _q(tp["mae_r"], np.mean), "median_minutes_to_1r": _q(tp["minutes_to_1_00r"], np.median),
                            "median_minutes_to_take_profit": _q(tp["holding_minutes"], np.median)},
            "scale_out": {"trades_with_scale_out": int(len(so)), "partial_exit_legs": int(so["scale_out_legs"].sum()),
                          "partial_pnl": float(so["scale_out_pnl"].sum()), "final_trade_pnl": float(so["realized_pnl"].sum()),
                          "avg_final_realized_r": _q(so["realized_r"], np.mean), "avg_mfe_r": _q(so["mfe_r"], np.mean),
                          "pct_final_giveback": _pct(so["final_exit_reason"] == "giveback_close"),
                          "pct_final_take_profit": _pct(so["final_exit_reason"] == "take_profit_hit"),
                          "pct_final_stop": _pct(so["final_exit_reason"] == "stop_hit")}}


def group_table(df: pd.DataFrame, key: str, extra: bool = False) -> List[Dict[str, Any]]:
    rows = []
    for k in sorted(df[key].astype(str).unique()):
        sub = df[df[key].astype(str) == k]
        row = {key: k, "trades": int(len(sub)), "win_rate_pct": _pct(sub["result"] == "win"),
               "pnl": float(sub["realized_pnl"].sum()), "avg_realized_r": _q(sub["realized_r"], np.mean),
               "median_realized_r": _q(sub["realized_r"], np.median),
               "profit_factor": _profit_factor(sub["realized_pnl"].tolist()) if len(sub) >= 2 else None,
               "median_holding_minutes": _q(sub["holding_minutes"], np.median),
               "exit_mix": {r: int((sub["final_exit_reason"] == r).sum()) for r in EXIT_REASONS}}
        if extra:
            st = sub[sub["final_exit_reason"] == "stop_hit"]
            row.update({"stop_hit_pct": _pct(sub["final_exit_reason"] == "stop_hit"), "stop_avg_r": _q(st["realized_r"], np.mean),
                        "stop_median_latency_r": _q(st["latency_cost_r"], np.median), "avg_mfe_r": _q(sub["mfe_r"], np.mean),
                        "avg_mae_r": _q(sub["mae_r"], np.mean),
                        **{f"{r}_pnl": float(sub.loc[sub["final_exit_reason"] == r, "realized_pnl"].sum()) for r in EXIT_REASONS}})
        rows.append(row)
    return rows


def geometry(df: pd.DataFrame) -> Dict[str, Any]:
    cols = ("stop_distance_usd", "stop_distance_pct", "stop_distance_atr", "take_distance_atr", "atr_pct", "cost_r",
            "entry_gap_r", "initial_stop_r_from_fill", "approx_initial_rr")
    return {c: {"q10": _q(df[c], lambda s: s.quantile(.1)), "median": _q(df[c], np.median), "q90": _q(df[c], lambda s: s.quantile(.9)),
                "mean": _q(df[c], np.mean)} for c in cols}


def decomposition(df: pd.DataFrame) -> Dict[str, Any]:
    pnl = df["realized_pnl"]
    st = df[df["final_exit_reason"] == "stop_hit"]
    return {"total_pnl": float(pnl.sum()),
            **{f"{r}_trades_pnl": float(df.loc[df["final_exit_reason"] == r, "realized_pnl"].sum()) for r in EXIT_REASONS},
            "scale_out_partial_pnl_included_above": float(df["scale_out_pnl"].sum()),
            "stop_latency_usd_included_above": float(_s(st["latency_cost_usd"]).sum()),
            "stop_overshoot_beyond_minus_1r_usd_included_above": loss_split(st)["overshoot_beyond_minus_1r"],
            "gross_win": float(pnl[pnl > 0].sum()), "gross_loss": float(pnl[pnl < 0].sum()),
            "profit_factor": _profit_factor(pnl.tolist())}


def signature(df: pd.DataFrame, summary: Dict[str, Any]) -> Dict[str, Any]:
    st = df[df["final_exit_reason"] == "stop_hit"]
    gb = df[df["final_exit_reason"] == "giveback_close"]
    gpos = gb[gb["mfe_r"] > 0]
    t = summary["trades"]
    return {"trades": t["trades"], "win_rate_pct": t["win_rate"] * 100, "expectancy_r": t["expectancy_r"],
            "profit_factor": t["profit_factor"], "median_mfe_r": _q(df["mfe_r"], np.median), "median_mae_r": _q(df["mae_r"], np.median),
            "pct_reach_0_25r": _pct(df["reached_0_25r"]), "pct_reach_0_5r": _pct(df["reached_0_50r"]),
            "pct_reach_1r": _pct(df["reached_1_00r"]),
            "pct_immediate_failures": _pct(df["lifecycle"] == "A_immediate_failure"),
            "stop_hit_pct": _pct(df["final_exit_reason"] == "stop_hit"), "stop_avg_r": _q(st["realized_r"], np.mean),
            "stop_median_mfe_r": _q(st["mfe_r"], np.median), "stop_median_latency_cost_r": _q(st["latency_cost_r"], np.median),
            "giveback_pct": _pct(df["final_exit_reason"] == "giveback_close"), "giveback_avg_r": _q(gb["realized_r"], np.mean),
            "giveback_mfe_capture": float(gpos["realized_r"].sum() / gpos["mfe_r"].sum()) if gpos["mfe_r"].sum() > 0 else None,
            "take_profit_pct": _pct(df["final_exit_reason"] == "take_profit_hit"), "scale_out_pct": _pct(df["scale_out_legs"] > 0),
            "median_holding_minutes": _q(df["holding_minutes"], np.median), "median_cost_r": _q(df["cost_r"], np.median)}


def family_differences(a: pd.DataFrame, b: pd.DataFrame) -> Dict[str, Any]:
    from .backtest_entry_quality import cles
    out = {}
    for col in ("mfe_r", "mae_r", "realized_r", "holding_minutes", "minutes_to_0_25r", "atr_pct", "cost_r", "bars_held"):
        out[col] = {"H001": {"q25": _q(a[col], lambda s: s.quantile(.25)), "median": _q(a[col], np.median),
                             "q75": _q(a[col], lambda s: s.quantile(.75))},
                    "H003": {"q25": _q(b[col], lambda s: s.quantile(.25)), "median": _q(b[col], np.median),
                             "q75": _q(b[col], lambda s: s.quantile(.75))},
                    "cles_h003_gt_h001": cles(a[col], b[col])}
    out["pct_reached_0_25r_before_exit_with_time"] = {"H001": _pct(a["minutes_to_0_25r"].notna()),
                                                      "H003": _pct(b["minutes_to_0_25r"].notna())}
    return out


def correlations(df: pd.DataFrame) -> List[Dict[str, Any]]:
    cols = ["atr_pct", "stop_distance_pct", "cost_r", "mfe_r", "mae_r", "realized_r", "holding_minutes"]
    rows = []
    for i, x in enumerate(cols):
        for y in cols[i + 1:]:
            xs, ys = _s(df[x]), _s(df[y])
            ok = xs.index.intersection(ys.index)
            xv, yv = df.loc[ok, x].astype(float), df.loc[ok, y].astype(float)
            rows.append({"x": x, "y": y, "n": int(len(ok)), "pearson": float(xv.corr(yv)) if len(ok) > 2 else None,
                         "spearman": float(xv.rank().corr(yv.rank())) if len(ok) > 2 else None})
    return rows


# ================================================================ orquestación
def _guard(out_dir: Path) -> None:
    for _, stored in STRATEGIES.values():
        if Path(out_dir).resolve() == stored.resolve():
            raise HygieneError("no se sobrescriben resultados guardados de H001/H003")


def build(protocol, split, data_dir: Path, out_dir: Path, stored: Optional[Dict[str, Path]] = None,
          workers: int = 2) -> Dict[str, Any]:
    _guard(out_dir)
    stored = stored or {n: p for n, (_, p) in STRATEGIES.items()}
    symbols = list(protocol["universe"]["symbols"])
    loaded = ap.load_development_bars(data_dir, symbols, split)
    runs = rerun(protocol, split, loaded["bars"], tuple(stored), workers)
    ident = {n: verify_identical(runs[n]["result"], out_dir / f"{n.lower()}_rerun_check", stored[n]) for n in stored}
    bad = {n: v for n, v in ident.items() if not v["identical"]}
    if bad:
        raise RuntimeError(f"re-run differs from stored development result: {bad}")
    ind = {s: ap.symbol_indicators(loaded["bars"][s]) for s in symbols}
    pos = {s: {ts: k for k, ts in enumerate(v["ts"])} for s, v in ind.items()}
    frames, post, summaries = {}, {}, {}
    for n in stored:
        res = runs[n]["result"]
        rows = trade_rows(n, res.trades, runs[n]["exit_submits"], ind)
        df = pd.DataFrame(rows)
        df["lifecycle"] = [lifecycle(m, r) for m, r in zip(df["mfe_r"], df["realized_r"])]
        frames[n] = df
        st = df[df["final_exit_reason"] == "stop_hit"]
        post[n] = pd.DataFrame([{"strategy": n, "trade_id": r["trade_id"], "symbol": r["symbol"], **post_stop_path(ind, pos, r)}
                                for _, r in st.iterrows()])
        summaries[n] = summarize(res)
    time_col = lambda df: df["entry_decision_minute_et"].map(ap.time_bucket)  # noqa: E731
    report = {"report": "shared_management_autopsy", "evidence": "DEVELOPMENT EVIDENCE only", "disclaimer": DISCLAIMER,
              "dates": [split["start"], split["end"]], "reproduction": ident,
              "conventions": {"excursions": "5Min CLOSES while shares held (fill bar through bar before final exit fill)",
                              "R": "risk_per_share_modeled", "latency": "latency_cost_r = fill_r − detection_r (negative = adverse)",
                              "post_stop": FUTURE_WARNING},
              "strategies": {}}
    for n, df in frames.items():
        df["time_bucket"] = time_col(df)
        st = df[df["final_exit_reason"] == "stop_hit"]
        ov = df[df["overnight"]]
        rr_rej = summaries[n]["execution"]["rejects_by_reason"].get("RR_BELOW_MINIMUM", 0)
        report["strategies"][n] = {
            "excursion_recompute_check": {"max_abs_diff_mfe": float((df["mfe_r"] - df["mfe_r_recomputed"]).abs().max()),
                                          "max_abs_diff_mae": float((df["mae_r"] - df["mae_r_recomputed"]).abs().max())},
            "signature": signature(df, summaries[n]), "stop_anatomy": stop_anatomy(df),
            "post_stop": post_stop_summary(post[n]) if len(post[n]) else None,
            "giveback": giveback_autopsy(df), "take_profit_scale_out": tp_scaleout(df),
            "lifecycle": group_table(df, "lifecycle"), "geometry": geometry(df), "decomposition": decomposition(df),
            "rr_filter": {"rr_rejects": int(rr_rej), "accepted_atr_pct": geometry(df)["atr_pct"],
                          "accepted_approx_rr": geometry(df)["approx_initial_rr"], "cost_r": geometry(df)["cost_r"]},
            "time_buckets": group_table(df, "time_bucket", extra=True),
            "overnight": {"trades": int(len(ov)), "pnl": float(ov["realized_pnl"].sum()),
                          "expectancy_r": _q(ov["realized_r"], np.mean),
                          "exit_mix": {r: int((ov["final_exit_reason"] == r).sum()) for r in EXIT_REASONS}},
            "by_symbol": group_table(df, "symbol", extra=True),
            "correlations": correlations(df)}
    report["entry_family_differences"] = family_differences(frames["H001"], frames["H003"]) if {"H001", "H003"} <= set(frames) else None
    return {"summary": report, "trades": frames, "post_stop": post}


# ================================================================ salidas
def _csv(path: Path, rows: List[Dict[str, Any]]) -> Path:
    fields = list(dict.fromkeys(k for r in rows for k in r)) if rows else ["empty"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def write(res: Dict[str, Any], out_dir: Path) -> List[Path]:
    _guard(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    s = res["summary"]
    paths = []
    p = out_dir / "shared_management_summary.json"
    p.write_text(to_json(s), encoding="utf-8")
    paths.append(p)
    allt = pd.concat(res["trades"].values(), ignore_index=True)
    lc = ["strategy", "trade_id", "symbol", "final_exit_reason", "result", "realized_pnl", "realized_r", "mfe_r", "mae_r",
          "lifecycle", "holding_minutes", "bars_held", "overnight", "time_bucket", "scale_out_legs", "scale_out_pnl"]
    allt[lc].to_csv(out_dir / "trade_lifecycle.csv", index=False)
    paths.append(out_dir / "trade_lifecycle.csv")
    stops = allt[allt["final_exit_reason"] == "stop_hit"]
    sc = ["strategy", "trade_id", "symbol", "entry_fill_price", "modeled_entry", "initial_stop_price", "initial_risk_per_share",
          "bars_held", "holding_minutes", "realized_r", "mfe_r", "mae_r", "minutes_to_mfe", "minutes_to_mae",
          "reached_0_25r", "reached_0_50r", "reached_1_00r", "be_done_at_detection"]
    stops[sc].to_csv(out_dir / "stop_autopsy.csv", index=False)
    paths.append(out_dir / "stop_autopsy.csv")
    lcols = ["strategy", "trade_id", "symbol", "stop_price_at_detection", "detection_bar_close", "detection_bar",
             "detection_decision_ts", "next_bar_open", "final_exit_fill", "final_fill_ts", "detection_r", "fill_r",
             "latency_cost_r", "latency_cost_usd", "detection_gap_r", "realized_r"]
    stops[lcols].to_csv(out_dir / "stop_latency.csv", index=False)
    paths.append(out_dir / "stop_latency.csv")
    pd.concat(res["post_stop"].values(), ignore_index=True).to_csv(out_dir / "post_stop_paths.csv", index=False)
    paths.append(out_dir / "post_stop_paths.csv")
    gb_rows, tp_rows, geo_rows, sym_rows, tb_rows, corr_rows, sig_rows = [], [], [], [], [], [], []
    for n, v in s["strategies"].items():
        gb_rows += [dict(b, strategy=n) for b in v["giveback"]["bands"]]
        gb_rows.append(dict({k: x for k, x in v["giveback"].items() if k != "bands"}, strategy=n, band="ALL"))
        tp_rows += [dict(v["take_profit_scale_out"]["take_profit"], strategy=n, kind="take_profit"),
                    dict(v["take_profit_scale_out"]["scale_out"], strategy=n, kind="scale_out")]
        geo_rows += [dict(q, strategy=n, metric=m) for m, q in v["geometry"].items()]
        sym_rows += [dict(r, strategy=n) for r in v["by_symbol"]]
        tb_rows += [dict(r, strategy=n) for r in v["time_buckets"]]
        corr_rows += [dict(r, strategy=n) for r in v["correlations"]]
    names = list(s["strategies"])
    for m in s["strategies"][names[0]]["signature"]:
        sig_rows.append({"metric": m, **{n: s["strategies"][n]["signature"][m] for n in names}})
    for name, rows in (("giveback_autopsy", gb_rows), ("takeprofit_scaleout", tp_rows), ("risk_geometry", geo_rows),
                       ("by_symbol", sym_rows), ("by_time_bucket", tb_rows), ("correlations", corr_rows),
                       ("h001_vs_h003", sig_rows)):
        paths.append(_csv(out_dir / f"{name}.csv", rows))
    return paths


def _f(x, fmt="{:+.3f}"):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def _p(x):
    return "-" if x is None else f"{x:.1f}%"


def format_report(s: Dict[str, Any]) -> str:
    S = s["strategies"]
    names = list(S)
    L = ["SHARED TRADE-MANAGEMENT AUTOPSY", "H001 vs H003 — DEVELOPMENT ONLY (2024-01-02 → 2025-12-31)", "─" * 76,
         "Reproduction: " + ", ".join(f"{n} identical={s['reproduction'][n]['identical']}" for n in names)]
    L += ["", "Core comparison / common management signature:", f"  {'metric':<30}" + "".join(f"{n:>12}" for n in names)]
    for m in S[names[0]]["signature"]:
        vals = [S[n]["signature"][m] for n in names]
        L.append(f"  {m:<30}" + "".join(f"{('-' if v is None else (f'{v:.3f}' if isinstance(v, float) else v)):>12}" for v in vals))
    L += ["", "Stop anatomy:"]
    for n in names:
        a = S[n]["stop_anatomy"]
        L.append(f"  {n}: n={a['stop_trades']} never +0.25R {_p(a['pct_never_0_25r'])} never +0.5R {_p(a['pct_never_0_50r'])} "
                 f"reached +1R {_p(a['pct_reached_1r_before_stop'])} | median MFE {_f(a['median_mfe_r'])} MAE {_f(a['median_mae_r'])} "
                 f"bars {_f(a['median_bars_held'], '{:.0f}')} ({_f(a['median_minutes_held'], '{:.0f}')} min) realized "
                 f"{_f(a['median_realized_r'])} | BE moved {_p(a['pct_break_even_moved_before_stop'])}")
    L += ["", "Detection → fill latency (latency_cost_r = fill_r − detection_r; negative = adverse):"]
    for n in names:
        l = S[n]["stop_anatomy"]["latency"]
        L.append(f"  {n}: mean {_f(l['mean_r'])} median {_f(l['median_r'])} Q25 {_f(l['q25_r'])} Q75 {_f(l['q75_r'])} "
                 f"worst {_f(l['worst_r'])} | total ${l['total_usd']:,.0f} | median close-vs-stop at detection "
                 f"{_f(l['median_detection_gap_r_close_vs_stop'])}R | median detection {_f(l['median_detection_r'])} → fill {_f(l['median_fill_r'])}")
    L += ["", "Stop overshoot (final stop realized R):"]
    for n in names:
        o, ls = S[n]["stop_anatomy"]["overshoot"], S[n]["stop_anatomy"]["loss_split_stop_trades"]
        L.append(f"  {n}: >-1R {_p(o['pct_better_than_-1.0'])} | -1..-1.25 {_p(o['pct_-1.0_to_-1.25'])} | -1.25..-1.5 "
                 f"{_p(o['pct_-1.25_to_-1.5'])} | <-1.5 {_p(o['pct_worse_than_-1.5'])} | <-2 {_p(o['pct_worse_than_-2.0'])} | "
                 f"median {_f(o['median_r'])} mean {_f(o['mean_r'])} worst {_f(o['largest_loss_r'])} | loss up to -1R "
                 f"${ls['loss_up_to_minus_1r']:,.0f}, beyond ${ls['overshoot_beyond_minus_1r']:,.0f}")
    L += ["", f"Post-stop behavior — WARNING: {FUTURE_WARNING}"]
    for n in names:
        p = S[n]["post_stop"]
        if p:
            L.append(f"  {n}: " + " | ".join(f"{lab}: above entry {_p(p[lab]['pct_recovered_above_entry'])}, +0.5R "
                                             f"{_p(p[lab]['pct_hit_p050'])}, +1R {_p(p[lab]['pct_hit_p100'])}, below exit "
                                             f"{_p(p[lab]['pct_below_actual_exit'])}" for lab in ("15m", "30m", "60m")))
    L += ["", "Giveback:"]
    for n in names:
        g = S[n]["giveback"]
        L.append(f"  {n}: n={g['trades']} win {_p(g['win_rate_pct'])} P&L ${g['pnl']:,.0f} avgR {_f(g['avg_realized_r'])} "
                 f"MFE avg {_f(g['avg_mfe_r'])} capture {_f(g['aggregate_capture_realized_over_mfe'], '{:.2f}')} | "
                 + ", ".join(f"{b['band']} {b['count']} ${b['pnl']:,.0f}" for b in g["bands"]))
    L += ["", "Take profit / scale-out:"]
    for n in names:
        t, so = S[n]["take_profit_scale_out"]["take_profit"], S[n]["take_profit_scale_out"]["scale_out"]
        L.append(f"  {n}: TP n={t['count']} ${t['pnl']:,.0f} avgR {_f(t['avg_realized_r'])} min→1R {_f(t['median_minutes_to_1r'], '{:.0f}')} "
                 f"min→TP {_f(t['median_minutes_to_take_profit'], '{:.0f}')} | scale-out trades {so['trades_with_scale_out']} legs "
                 f"{so['partial_exit_legs']} partial ${so['partial_pnl']:,.0f} final ${so['final_trade_pnl']:,.0f} → giveback "
                 f"{_p(so['pct_final_giveback'])} TP {_p(so['pct_final_take_profit'])} stop {_p(so['pct_final_stop'])}")
    L += ["", "Risk geometry (median [q10, q90]):"]
    for n in names:
        g = S[n]["geometry"]
        L.append(f"  {n}: stop/ATR {_f(g['stop_distance_atr']['median'], '{:.2f}')} TP/ATR {_f(g['take_distance_atr']['median'], '{:.2f}')} "
                 f"stop% {_f(g['stop_distance_pct']['median'], '{:.2f}')} ATR% {_f(g['atr_pct']['median'], '{:.3f}')} "
                 f"[{_f(g['atr_pct']['q10'], '{:.3f}')}, {_f(g['atr_pct']['q90'], '{:.3f}')}] cost R {_f(g['cost_r']['median'], '{:.3f}')} "
                 f"entry gap R {_f(g['entry_gap_r']['median'])} approx RR {_f(g['approx_initial_rr']['median'], '{:.2f}')} "
                 f"| RR rejects {S[n]['rr_filter']['rr_rejects']}")
    L += ["", "Payoff decomposition:"]
    for n in names:
        d = S[n]["decomposition"]
        L.append(f"  {n}: total ${d['total_pnl']:,.0f} = giveback ${d['giveback_close_trades_pnl']:,.0f} + stop "
                 f"${d['stop_hit_trades_pnl']:,.0f} + TP ${d['take_profit_hit_trades_pnl']:,.0f} (scale-out partials "
                 f"${d['scale_out_partial_pnl_included_above']:,.0f}; stop latency ${d['stop_latency_usd_included_above']:,.0f}; "
                 f"stop overshoot beyond -1R ${d['stop_overshoot_beyond_minus_1r_usd_included_above']:,.0f}) | gross win "
                 f"${d['gross_win']:,.0f} loss ${d['gross_loss']:,.0f} PF {_f(d['profit_factor'], '{:.3f}')}")
    L += ["", "Lifecycle buckets (trades / P&L / avg R):"]
    for n in names:
        L.append(f"  {n}: " + " | ".join(f"{r['lifecycle']} {r['trades']} ${r['pnl']:,.0f} {_f(r['avg_realized_r'], '{:+.2f}')}"
                                         for r in S[n]["lifecycle"]))
    fd = s.get("entry_family_differences")
    if fd:
        L += ["", "Entry-family differences (median H001 vs H003; CLES = P(H003 > H001)):"]
        for c in ("mfe_r", "mae_r", "realized_r", "holding_minutes", "minutes_to_0_25r", "atr_pct", "cost_r", "bars_held"):
            v = fd[c]
            L.append(f"  {c:<20} {_f(v['H001']['median'], '{:.3f}'):>9} vs {_f(v['H003']['median'], '{:.3f}'):>9}  CLES "
                     f"{_f(v['cles_h003_gt_h001'], '{:.2f}')}")
    L += ["", "Time of day / overnight:"]
    for n in names:
        o = S[n]["overnight"]
        L.append(f"  {n}: " + " | ".join(f"{r['time_bucket']} n={r['trades']} {_f(r['avg_realized_r'], '{:+.3f}')}R stop "
                                         f"{_p(r['stop_hit_pct'])}" for r in S[n]["time_buckets"])
                 + f" || overnight n={o['trades']} ${o['pnl']:,.0f} {_f(o['expectancy_r'], '{:+.3f}')}R {o['exit_mix']}")
    L += ["", s["disclaimer"]]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.shared_management_autopsy",
                                description="Autopsia de gestión compartida H001 vs H003, SOLO development (sin bypass).")
    p.add_argument("--split", default="development", help="Solo 'development' es aceptado.")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, default=Path("data") / "research_v1" / "shared_management_autopsy")
    a = p.parse_args(argv)
    try:
        protocol = load_protocol(a.protocol)
        split = development_scope(protocol, a.split)
        res = build(protocol, split, a.data_dir, a.output_dir)
        paths = write(res, a.output_dir)
    except HygieneError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    print(format_report(res["summary"]))
    print("\nArchivos: " + ", ".join(str(x) for x in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
