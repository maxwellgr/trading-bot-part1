# src/execution_sensitivity.py
"""
Sensibilidad a costos de ejecución (v1): corre el MISMO backtest de portafolio
variando SOLO el slippage de los fills simulados. No optimiza ni elige un
"mejor" slippage; mide cuánto depende el resultado de ese supuesto.

Qué cambia y qué no
-------------------
- Cambia: BacktestConfig.slippage_bps -> precio de cada fill del SimBroker
  (compra: open·(1+bps), venta: open·(1−bps), siempre en la apertura siguiente).
- NO cambia: datos, símbolos, fechas, estrategia, RiskManager ni sus
  parámetros. En particular el slippage que ASUME el RiskManager (slippage_pct
  de build_risk_config, 5 bps en producción) sigue igual: entrada modelada,
  stop, sizing y el denominador de R son los de producción en todos los
  escenarios, así que R es comparable entre escenarios.
- Dependencia de camino: con otro slippage cambian P&L y equity, y con ellos
  el tamaño de posiciones posteriores, rachas, halts diarios y qué entradas
  existen después. Por eso NO se exige igual número de trades entre escenarios;
  se documenta la divergencia contra el escenario de referencia (5 bps).
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .backtest_engine import BacktestConfig, BacktestResult, run_backtest
from .backtest_report import _profit_factor, summarize, to_json, write_outputs

DEFAULT_BPS = (0.0, 2.5, 5.0, 7.5, 10.0, 15.0)
REFERENCE_BPS = 5.0
EXIT_REASONS = ("signal_exit", "giveback_close", "stop_hit", "take_profit_hit")
LOSS_STREAK_KEY = "Racha negativa"   # motivo de should_halt_trading() (texto de producción)
ATR_FEATURE = "atr_pct"              # del contexto de entrada (backtest_entry_quality)
BASELINE_FILES = ("trades.csv", "daily_results.csv", "equity_curve.csv", "entry_quality.csv")
DEFAULT_BASELINE_DIR = Path("data") / "backtests" / "baseline_v1_entry_quality"
DISCLAIMER = ("Descriptive sensitivity of this historical sample only. No scenario is 'best'; "
              "no slippage level is recommended; nothing here is an optimized threshold.")


# ================================================================ escenarios
def parse_bps(text: str) -> List[float]:
    vals = sorted({float(x) for x in str(text).split(",") if x.strip()})
    if not vals:
        raise ValueError("--slippage-bps vacío")
    if any(v < 0 or math.isnan(v) for v in vals):
        raise ValueError("--slippage-bps debe ser >= 0")
    return vals


def scenario_dirname(bps: float) -> str:
    return f"slippage_{bps:g}bps".replace(".", "_")


def _run_one(job: Tuple[float, Dict[str, Any], Dict[str, pd.DataFrame]]) -> BacktestResult:
    bps, cfg_kwargs, bars = job
    return run_backtest(BacktestConfig(slippage_bps=bps, **cfg_kwargs), bars)


def run_scenarios(bars: Dict[str, pd.DataFrame], bps_list: Sequence[float], jobs: int = 1,
                  **cfg_kwargs) -> List[Tuple[float, BacktestResult]]:
    """Un backtest completo por nivel de slippage; resultado en orden ascendente de bps (determinista)."""
    if "slippage_bps" in cfg_kwargs:
        raise ValueError("slippage_bps lo fija cada escenario")
    ordered = sorted(set(float(b) for b in bps_list))
    work = [(b, cfg_kwargs, bars) for b in ordered]
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(work))) as pool:
            results = list(pool.map(_run_one, work))  # map conserva el orden de entrada
    else:
        results = [_run_one(w) for w in work]
    return list(zip(ordered, results))


# ================================================================ métricas
def _finite(x: Any) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _sub(a: Any, b: Any) -> Optional[float]:
    a, b = _finite(a), _finite(b)
    return None if a is None or b is None else a - b


def _fills_slippage(result: BacktestResult) -> float:
    return sum(abs(f["price"] - f["reference_open"]) * f["qty"] for f in result.fills)


def scenario_metrics(bps: float, result: BacktestResult, summary: Dict[str, Any]) -> Dict[str, Any]:
    p, t, e = summary["portfolio"], summary["trades"], summary["execution"]
    cb = e["circuit_breaker_blocked_entries"]
    ex = e["completed_trade_exit_reasons"]
    row = {
        "slippage_bps": bps,
        "starting_equity": p["initial_equity"], "ending_equity": p["ending_equity"], "return_pct": p["return_pct"],
        "realized_pnl": p["realized_pnl_closed_trades"], "unrealized_pnl": p["unrealized_pnl_open_positions"],
        "trades": t["trades"], "wins": t["wins"], "losses": t["losses"], "breakevens": t["breakevens"],
        "win_rate_pct": t["win_rate"] * 100, "avg_win": t["avg_win"], "avg_loss": t["avg_loss"],
        "expectancy": t["expectancy"], "expectancy_r": t["expectancy_r"], "profit_factor": t["profit_factor"],
        "total_r": t["total_r"], "max_drawdown_pct": p["max_drawdown_pct"],
        "max_consecutive_losses": t["max_consecutive_losses"],
        "largest_win": t["largest_win"], "largest_loss": t["largest_loss"],
        "risk_accepts": e["risk_accept"], "risk_rejects": e["risk_reject"],
        "entries_blocked_by_loss_streak": cb.get(LOSS_STREAK_KEY, 0),
        "entries_blocked_by_other_circuit_breakers": sum(v for k, v in cb.items() if k != LOSS_STREAK_KEY),
        "entries_blocked_by_daily_profit_halt": e["daily_profit_halt_blocked_entries"],
        "daily_profit_halt_days": e["daily_profit_halt_days"], "loss_streak_halts": e["loss_streak_halts"],
    }
    for reason in EXIT_REASONS:
        row[f"exit_{reason}"] = ex.get(reason, 0)
    row["scale_outs"] = e["scale_outs"]
    row["slippage_paid"] = _fills_slippage(result)
    return row


MARGINAL = (("realized_pnl", "delta_pnl"), ("return_pct", "delta_return_pct"), ("expectancy", "delta_expectancy"),
            ("expectancy_r", "delta_expectancy_r"), ("profit_factor", "delta_profit_factor"),
            ("max_drawdown_pct", "delta_max_drawdown_pct"), ("total_r", "delta_total_r"), ("trades", "delta_trades"))


def marginal_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a, b in zip(rows, rows[1:]):
        d = {"from_bps": a["slippage_bps"], "to_bps": b["slippage_bps"]}
        for key, name in MARGINAL:
            d[name] = _sub(b[key], a[key])
        step = b["slippage_bps"] - a["slippage_bps"]
        d["pnl_change_per_bps"] = d["delta_pnl"] / step if d["delta_pnl"] is not None and step else None
        out.append(d)
    return out


def sensitivity_estimate(rows: Sequence[Dict[str, Any]], marg: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    xs = [r["slippage_bps"] for r in rows]
    ys = [r["realized_pnl"] for r in rows]
    slope = float(np.polyfit(xs, ys, 1)[0]) if len(rows) >= 2 else None
    per = [m["pnl_change_per_bps"] for m in marg if m["pnl_change_per_bps"] is not None]
    return {
        "note": "Empirical sensitivity of THIS sample (P&L change per +1 bps). Path-dependent; not a universal constant.",
        "least_squares_pnl_per_bps": slope,
        "per_transition_pnl_per_bps": {f"{m['from_bps']:g}->{m['to_bps']:g}": m["pnl_change_per_bps"] for m in marg},
        "min_transition_pnl_per_bps": min(per) if per else None,
        "max_transition_pnl_per_bps": max(per) if per else None,
    }


def break_even(rows: Sequence[Dict[str, Any]], key: str, target: float) -> Dict[str, Any]:
    """Cruces de `target` solo DENTRO del rango probado (interpolación lineal entre escenarios vecinos)."""
    pts = [(r["slippage_bps"], _finite(r[key])) for r in rows]
    pts = [(x, y) for x, y in pts if y is not None]
    crossings = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if y0 == target:
            crossings.append({"bps": x0, "method": "observed"})
        elif (y0 - target) * (y1 - target) < 0:
            crossings.append({"bps": x0 + (target - y0) * (x1 - x0) / (y1 - y0), "between": [x0, x1],
                              "method": "approximate descriptive linear interpolation (not an optimized threshold)"})
    if pts and pts[-1][1] == target:
        crossings.append({"bps": pts[-1][0], "method": "observed"})
    if crossings:
        status = "crossing observed within tested range"
    elif pts and all(y > target for _, y in pts):
        status = f"above {target:g} in every tested scenario; no break-even observed within tested range"
    else:
        status = "no break-even slippage observed within tested range"
    return {"metric": key, "target": target, "tested_range_bps": [pts[0][0], pts[-1][0]] if pts else None,
            "status": status, "crossings": crossings}


def zero_cost(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    z = next((r for r in rows if r["slippage_bps"] == 0), None)
    if z is None:
        return {"available": False, "note": "0 bps not among the tested scenarios"}

    def sign(v):
        v = _finite(v)
        return None if v is None else ("positive" if v > 0 else "negative" if v < 0 else "zero")
    pf = z["profit_factor"]
    return {"available": True, "realized_pnl": z["realized_pnl"], "total_pnl_sign": sign(z["realized_pnl"]),
            "expectancy": z["expectancy"], "expectancy_sign": sign(z["expectancy"]),
            "expectancy_r": z["expectancy_r"], "expectancy_r_sign": sign(z["expectancy_r"]),
            "profit_factor": pf,
            "profit_factor_vs_1": None if pf is None else ("above" if pf > 1 else "below" if pf < 1 else "equal"),
            "total_r": z["total_r"], "total_r_sign": sign(z["total_r"]),
            "scope": "This historical sample only (2026 period/symbols tested); not a general statement."}


# ================================================================ desgloses
def _trade_slippage(trade: Dict[str, Any], entry_fills: Dict[Tuple[str, str], Dict[str, Any]]) -> Optional[float]:
    """Slippage pagado por el trade en USD: entrada (px − open)·qty + salidas (open − px)·qty."""
    f = entry_fills.get((trade["symbol"], trade["entry_fill_timestamp"]))
    if f is None:
        return None
    cost = (f["price"] - f["reference_open"]) * f["qty"]
    cost += sum((leg["reference_open"] - leg["price"]) * leg["qty"] for leg in trade["legs"])
    return cost


def enrich_trades(result: BacktestResult) -> List[Dict[str, Any]]:
    entry_fills = {(f["symbol"], f["fill_ts"]): f for f in result.fills if f["side"] == "buy"}
    out = []
    for t in result.trades:
        cost = _trade_slippage(t, entry_fills)
        risk = (t.get("risk_per_share_modeled") or 0) * (t.get("initial_qty") or 0)
        cost_r = cost / risk if cost is not None and risk > 0 else None
        ctx = t.get("entry_context") or {}
        out.append({"symbol": t["symbol"], "entry_signal_timestamp": t["entry_signal_timestamp"],
                    "exit_reason": t["exit_reason"], "realized_pnl": t["realized_pnl"], "realized_r": t.get("realized_r"),
                    "result": t["result"], "slippage_cost": cost, "slippage_cost_r": cost_r,
                    "gross_r": (t["realized_r"] + cost_r) if cost_r is not None and t.get("realized_r") is not None else None,
                    ATR_FEATURE: ctx.get(ATR_FEATURE)})
    return out


def _group_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Mismas definiciones que backtest_report.trade_metrics/per_symbol (PF solo con >= 2 trades)."""
    n = len(trades)
    pnls = [t["realized_pnl"] for t in trades]
    rs = [t["realized_r"] for t in trades if t.get("realized_r") is not None]
    cr = [t["slippage_cost_r"] for t in trades if t.get("slippage_cost_r") is not None]
    return {"trades": n, "pnl": sum(pnls),
            "win_rate_pct": sum(1 for t in trades if t["result"] == "win") / n * 100 if n else None,
            "expectancy": statistics.fmean(pnls) if n else None,
            "profit_factor": _profit_factor(pnls) if n >= 2 else None,
            "avg_r": statistics.fmean(rs) if rs else None, "median_r": statistics.median(rs) if rs else None,
            "avg_slippage_cost_r": statistics.fmean(cr) if cr else None,
            "slippage_paid": sum(t["slippage_cost"] or 0.0 for t in trades)}


def _degradation(rows: List[Dict[str, Any]], key_field: str, pairs: Sequence[Tuple[float, float]]) -> List[Dict[str, Any]]:
    by = {(r[key_field], r["slippage_bps"]): r for r in rows}
    keys = list(dict.fromkeys(r[key_field] for r in rows))
    out = []
    for k in keys:
        d = {key_field: k}
        for lo, hi in pairs:
            a, b = by.get((k, lo)), by.get((k, hi))
            d[f"pnl_change_{lo:g}_to_{hi:g}_bps"] = _sub(b["pnl"], a["pnl"]) if a and b else None
        out.append(d)
    return out


def per_symbol_rows(scen: Sequence[Tuple[float, List[Dict[str, Any]]]], symbols: Sequence[str]) -> List[Dict[str, Any]]:
    rows = []
    for bps, trades in scen:
        for sym in symbols:
            rows.append({"slippage_bps": bps, "symbol": sym,
                         **_group_metrics([t for t in trades if t["symbol"] == sym])})
    return rows


def atr_edges(trades: List[Dict[str, Any]], q: int = 5) -> Optional[List[float]]:
    vals = [t[ATR_FEATURE] for t in trades if t.get(ATR_FEATURE) is not None]
    if len(vals) < q:
        return None
    return [float(x) for x in np.quantile(vals, [i / q for i in range(1, q)])]


def atr_label(value: Optional[float], edges: Optional[List[float]]) -> str:
    if value is None or edges is None:
        return "missing"
    return f"Q{int(np.searchsorted(edges, value, side='left')) + 1}"  # intervalos (a, b], como qcut


def per_atr_rows(scen: Sequence[Tuple[float, List[Dict[str, Any]]]], edges: Optional[List[float]]) -> List[Dict[str, Any]]:
    rows = []
    for bps, trades in scen:
        labels = [atr_label(t.get(ATR_FEATURE), edges) for t in trades]
        for lab in sorted(set(labels), key=lambda s: (s == "missing", s)):
            rows.append({"slippage_bps": bps, "atr_quintile": lab,
                         **_group_metrics([t for t, l in zip(trades, labels) if l == lab])})
    return rows


def per_exit_rows(scen: Sequence[Tuple[float, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    rows = []
    for bps, trades in scen:
        present = {t["exit_reason"] for t in trades}
        for reason in list(EXIT_REASONS) + sorted(present - set(EXIT_REASONS)):
            rows.append({"slippage_bps": bps, "exit_reason": reason,
                         **_group_metrics([t for t in trades if t["exit_reason"] == reason])})
    return rows


def path_dependence(scen: Sequence[Tuple[float, List[Dict[str, Any]]]], ref_bps: float) -> Dict[str, Any]:
    ref = dict(scen).get(ref_bps)
    if ref is None:
        return {"available": False}
    ref_keys = {(t["symbol"], t["entry_signal_timestamp"]) for t in ref}
    out = {}
    for bps, trades in scen:
        keys = {(t["symbol"], t["entry_signal_timestamp"]) for t in trades}
        diff = sorted((keys ^ ref_keys), key=lambda k: k[1])
        out[f"{bps:g}"] = {"trades": len(trades), "common_with_reference": len(keys & ref_keys),
                           "only_in_scenario": len(keys - ref_keys), "only_in_reference": len(ref_keys - keys),
                           "first_divergent_entry": ({"symbol": diff[0][0], "entry_signal_timestamp": diff[0][1]}
                                                     if diff else None)}
    return {"available": True, "reference_bps": ref_bps, "match_key": "(symbol, entry_signal_timestamp)",
            "by_scenario": out}


def decomposition(scen: Sequence[Tuple[float, List[Dict[str, Any]]]], low: float = 0.0,
                  high: float = REFERENCE_BPS) -> Dict[str, Any]:
    """P&L(high) − P&L(low) = Δ en trades comunes − P&L de trades que desaparecen + P&L de trades nuevos."""
    d = dict(scen)
    if low not in d or high not in d:
        return {"available": False}
    lo = {(t["symbol"], t["entry_signal_timestamp"]): t for t in d[low]}
    hi = {(t["symbol"], t["entry_signal_timestamp"]): t for t in d[high]}
    common = lo.keys() & hi.keys()
    pnl_lo, pnl_hi = sum(t["realized_pnl"] for t in lo.values()), sum(t["realized_pnl"] for t in hi.values())
    common_delta = sum(hi[k]["realized_pnl"] - lo[k]["realized_pnl"] for k in common)
    dropped = sum(lo[k]["realized_pnl"] for k in lo.keys() - hi.keys())
    added = sum(hi[k]["realized_pnl"] for k in hi.keys() - lo.keys())
    return {
        "available": True, "low_bps": low, "high_bps": high,
        f"gross_result_at_{low:g}_bps": pnl_lo, f"result_at_{high:g}_bps": pnl_hi,
        "difference_attributable_to_execution_cost_assumption": pnl_hi - pnl_lo,
        "components": {
            "common_trades": len(common), "common_trades_pnl_change": common_delta,
            f"trades_only_at_{low:g}_bps": len(lo.keys() - hi.keys()), "their_pnl_removed": -dropped,
            f"trades_only_at_{high:g}_bps": len(hi.keys() - lo.keys()), "their_pnl_added": added,
        },
        f"slippage_paid_on_all_trades_at_{high:g}_bps": sum(t["slippage_cost"] or 0 for t in hi.values()),
        f"slippage_paid_on_common_trades_at_{high:g}_bps": sum(hi[k]["slippage_cost"] or 0 for k in common),
        "note": ("Path-dependent: slippage changes realized P&L and equity, hence later position sizes, loss "
                 "streaks, daily halts and which entries exist. The difference is NOT simply shares x bps; "
                 "common + removed + added components sum exactly to the total difference."),
    }


# ================================================================ baseline
def baseline_match(scenario_dir: Path, baseline_dir: Optional[Path]) -> Dict[str, Any]:
    if baseline_dir is None or not baseline_dir.is_dir():
        return {"checked": False, "baseline_dir": str(baseline_dir) if baseline_dir else None,
                "note": "baseline directory not found; match not checked"}
    files = {}
    for name in BASELINE_FILES:
        a, b = scenario_dir / name, baseline_dir / name
        files[name] = "missing_in_baseline" if not b.exists() else ("identical" if a.read_bytes() == b.read_bytes()
                                                                     else "DIFFERENT")
    for name in ("summary.json", "trades.json"):
        a, b = scenario_dir / name, baseline_dir / name
        if not b.exists():
            files[name] = "missing_in_baseline"
            continue
        ja, jb = json.loads(a.read_text(encoding="utf-8")), json.loads(b.read_text(encoding="utf-8"))
        files[name] = "identical" if ja == jb else "DIFFERENT"
    checked = {k: v for k, v in files.items() if v != "missing_in_baseline"}
    return {"checked": True, "baseline_dir": str(baseline_dir), "files": files,
            "exact_match": bool(checked) and all(v == "identical" for v in checked.values())}


# ================================================================ estudio completo
def build_study(scenarios: Sequence[Tuple[float, BacktestResult]], symbols: Sequence[str],
                reference_bps: float = REFERENCE_BPS) -> Dict[str, Any]:
    summaries = [(b, summarize(r)) for b, r in scenarios]
    rows = [scenario_metrics(b, r, s) for (b, r), (_, s) in zip(scenarios, summaries)]
    marg = marginal_rows(rows)
    enriched = [(b, enrich_trades(r)) for b, r in scenarios]
    ref_trades = dict(enriched).get(reference_bps) or enriched[0][1]
    edges = atr_edges(ref_trades)
    bps = [b for b, _ in scenarios]
    pairs = [(lo, hi) for lo, hi in ((0.0, 5.0), (5.0, 10.0)) if lo in bps and hi in bps]
    sym_rows = per_symbol_rows(enriched, symbols)
    atr_rows = per_atr_rows(enriched, edges)
    exit_rows = per_exit_rows(enriched)
    first = bps[0]
    return {
        "disclaimer": DISCLAIMER,
        "scenarios_bps": bps,
        "reference_bps": reference_bps,
        "held_constant": ["symbols", "dates", "data", "strategy", "risk parameters (incl. RiskManager slippage_pct "
                          "used for modeled entry/stop/sizing/R)", "symbol tie ordering", "next-bar-open fill timing",
                          "commission"],
        "config": scenarios[0][1].config | {"slippage_bps": "varied"},
        "scenarios": rows,
        "marginal": marg,
        "sensitivity": sensitivity_estimate(rows, marg),
        "zero_cost": zero_cost(rows),
        "break_even": {k: break_even(rows, k, tgt) for k, tgt in (("realized_pnl", 0.0), ("expectancy", 0.0),
                                                                   ("expectancy_r", 0.0), ("total_r", 0.0),
                                                                   ("profit_factor", 1.0))},
        "per_symbol": sym_rows,
        "per_symbol_degradation": _degradation(sym_rows, "symbol", pairs),
        "atr_quintile_edges_atr_pct": edges,
        "atr_quintile_note": (f"Quintile edges of entry-context {ATR_FEATURE} fixed from the {reference_bps:g} bps "
                              "scenario and applied to every scenario (trade sets differ between scenarios)."),
        "per_atr_quintile": atr_rows,
        "per_atr_quintile_degradation": _degradation(atr_rows, "atr_quintile", pairs),
        "per_exit_reason": exit_rows,
        "per_exit_reason_degradation": _degradation(exit_rows, "exit_reason",
                                                    [(first, b) for b in bps[1:]] + pairs),
        "path_dependence": path_dependence(enriched, reference_bps),
        "decomposition": decomposition(enriched),
        "warnings": sorted({w for _, r in scenarios for w in r.warnings}),
    }


# ================================================================ salidas
SCENARIO_COLUMNS = ["slippage_bps", "starting_equity", "ending_equity", "return_pct", "realized_pnl", "unrealized_pnl",
                    "trades", "wins", "losses", "breakevens", "win_rate_pct", "avg_win", "avg_loss", "expectancy",
                    "expectancy_r", "profit_factor", "total_r", "max_drawdown_pct", "max_consecutive_losses",
                    "largest_win", "largest_loss", "risk_accepts", "risk_rejects", "entries_blocked_by_loss_streak",
                    "entries_blocked_by_other_circuit_breakers", "entries_blocked_by_daily_profit_halt",
                    "daily_profit_halt_days", "loss_streak_halts"] + [f"exit_{r}" for r in EXIT_REASONS] + \
                   ["scale_outs", "slippage_paid"]
GROUP_COLUMNS = ["trades", "pnl", "win_rate_pct", "expectancy", "profit_factor", "avg_r", "median_r",
                 "avg_slippage_cost_r", "slippage_paid"]


def _write_csv(path: Path, fields: List[str], rows: Iterable[Dict[str, Any]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r.get(k) for k in fields} for r in rows)
    return path


def write_study(study: Dict[str, Any], out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "execution_sensitivity_summary.json"
    p.write_text(to_json(study), encoding="utf-8")
    return [p,
            _write_csv(out_dir / "execution_sensitivity.csv", SCENARIO_COLUMNS, study["scenarios"]),
            _write_csv(out_dir / "execution_sensitivity_marginal.csv",
                       ["from_bps", "to_bps"] + [n for _, n in MARGINAL] + ["pnl_change_per_bps"], study["marginal"]),
            _write_csv(out_dir / "execution_sensitivity_by_symbol.csv", ["slippage_bps", "symbol"] + GROUP_COLUMNS,
                       study["per_symbol"]),
            _write_csv(out_dir / "execution_sensitivity_by_atr_quintile.csv",
                       ["slippage_bps", "atr_quintile"] + GROUP_COLUMNS, study["per_atr_quintile"]),
            _write_csv(out_dir / "execution_sensitivity_by_exit_reason.csv",
                       ["slippage_bps", "exit_reason"] + GROUP_COLUMNS, study["per_exit_reason"])]


# ================================================================ consola
def _m(v: Any) -> str:
    v = _finite(v)
    return "-" if v is None else f"{v:,.0f}"


def _n(v: Any, fmt: str = "{:.2f}") -> str:
    if v is not None and not math.isfinite(float(v)):
        return "inf"
    return "-" if v is None else fmt.format(v)


def format_study(s: Dict[str, Any]) -> str:
    line = "─" * 72
    L = ["EXECUTION SENSITIVITY (only fill slippage varies; commission $0; next-bar-open fills)", line,
         f"{'bps':>5}{'trades':>8}{'W/L':>9}{'P&L':>11}{'return':>9}{'exp $':>9}{'exp R':>8}{'PF':>6}{'tot R':>8}"
         f"{'max DD':>9}{'slip paid':>11}"]
    for r in s["scenarios"]:
        L.append(f"{r['slippage_bps']:>5g}{r['trades']:>8}{str(r['wins']) + '/' + str(r['losses']):>9}"
                 f"{_m(r['realized_pnl']):>11}{r['return_pct']:>8.2f}%{_n(r['expectancy']):>9}"
                 f"{_n(r['expectancy_r'], '{:+.3f}'):>8}{_n(r['profit_factor']):>6}{_n(r['total_r'], '{:+.1f}'):>8}"
                 f"{r['max_drawdown_pct']:>8.2f}%{_m(r['slippage_paid']):>11}")
    L += ["", f"{'bps':>5}{'accept':>8}{'reject':>8}{'LS blk':>8}{'DPH blk':>8}{'DPH d':>6}{'LS h':>6}"
              f"{'giveback':>9}{'signal':>8}{'stop':>6}{'TP':>5}{'scale':>7}"]
    for r in s["scenarios"]:
        L.append(f"{r['slippage_bps']:>5g}{r['risk_accepts']:>8}{r['risk_rejects']:>8}"
                 f"{r['entries_blocked_by_loss_streak']:>8}{r['entries_blocked_by_daily_profit_halt']:>8}"
                 f"{r['daily_profit_halt_days']:>6}{r['loss_streak_halts']:>6}{r['exit_giveback_close']:>9}"
                 f"{r['exit_signal_exit']:>8}{r['exit_stop_hit']:>6}{r['exit_take_profit_hit']:>5}{r['scale_outs']:>7}")
    L.append("(LS blk = entries blocked by loss streak, DPH = daily profit halt blocked entries / days, LS h = loss-streak halts)")
    L += ["", f"{'Δ':<11}{'ΔP&L':>10}{'Δret':>8}{'Δexp$':>8}{'ΔexpR':>8}{'ΔPF':>7}{'ΔmaxDD':>8}{'Δtrades':>8}{'$/bps':>9}"]
    for m in s["marginal"]:
        L.append(f"{m['from_bps']:g}→{m['to_bps']:g}".ljust(11)
                 + f"{_m(m['delta_pnl']):>10}{_n(m['delta_return_pct'], '{:+.2f}'):>8}{_n(m['delta_expectancy'], '{:+.2f}'):>8}"
                   f"{_n(m['delta_expectancy_r'], '{:+.3f}'):>8}{_n(m['delta_profit_factor'], '{:+.3f}'):>7}"
                   f"{_n(m['delta_max_drawdown_pct'], '{:+.2f}'):>8}{m['delta_trades']:>+8.0f}{_m(m['pnl_change_per_bps']):>9}")
    se = s["sensitivity"]
    L.append(f"Least-squares P&L change per +1 bps: {_m(se['least_squares_pnl_per_bps'])} "
             f"(transitions {_m(se['min_transition_pnl_per_bps'])} .. {_m(se['max_transition_pnl_per_bps'])}) — "
             "empirical, this sample only")

    z = s["zero_cost"]
    L += ["", "ZERO-COST RESULT (0 bps, this sample only)"]
    if z["available"]:
        L += [f"  Total P&L: {_m(z['realized_pnl'])} ({z['total_pnl_sign']})",
              f"  Expectancy: {_n(z['expectancy'])} $ ({z['expectancy_sign']}) / {_n(z['expectancy_r'], '{:+.3f}')} R",
              f"  Profit factor: {_n(z['profit_factor'])} ({z['profit_factor_vs_1']} 1.0)",
              f"  Total R: {_n(z['total_r'], '{:+.2f}')} ({z['total_r_sign']})"]
    else:
        L.append(f"  {z['note']}")
    L += ["", "BREAK-EVEN EXECUTION COST"]
    for k, be in s["break_even"].items():
        cr = "; ".join(f"≈{c['bps']:.2f} bps ({c['method'].split(' (')[0]})" for c in be["crossings"])
        L.append(f"  {k:<14}{cr or be['status']}")

    bm = s.get("baseline_match") or {}
    L += ["", f"{s['reference_bps']:g} BPS BASELINE MATCH"]
    if bm.get("checked"):
        L.append(f"  vs {bm['baseline_dir']}: {'EXACT MATCH' if bm['exact_match'] else 'MISMATCH'} — "
                 + ", ".join(f"{k} {v}" for k, v in bm["files"].items()))
    else:
        L.append(f"  {bm.get('note', 'not checked')}")

    dec = s["decomposition"]
    if dec.get("available"):
        c, lo_b, hi_b = dec["components"], dec["low_bps"], dec["high_bps"]
        L += ["", f"RAW RESULT VS EXECUTION COST ({lo_b:g} → {hi_b:g} bps)",
              f"  Result at {lo_b:g} bps: {_m(dec['gross_result_at_%g_bps' % lo_b])} | at {hi_b:g} bps: "
              f"{_m(dec['result_at_%g_bps' % hi_b])} | difference "
              f"{_m(dec['difference_attributable_to_execution_cost_assumption'])}",
              f"  = common trades ({c['common_trades']}) {_m(c['common_trades_pnl_change'])} "
              f"+ removed trades {_m(c['their_pnl_removed'])} + added trades {_m(c['their_pnl_added'])}",
              f"  Slippage actually paid at {hi_b:g} bps: "
              f"{_m(dec['slippage_paid_on_all_trades_at_%g_bps' % hi_b])} (path-dependent; not shares×bps)"]
    pd_ = s["path_dependence"]
    if pd_.get("available"):
        L.append("  Trades shared with the reference run: " + ", ".join(
            f"{k} bps {v['common_with_reference']}/{v['trades']}" for k, v in pd_["by_scenario"].items()))

    bps = s["scenarios_bps"]
    L += ["", "ATR% QUINTILE SENSITIVITY (edges fixed from reference run; P&L | avg R | avg slip cost R)"]
    L.append(f"{'quintile':<10}" + "".join(f"{b:>22g}" for b in bps))
    by = {(r["atr_quintile"], r["slippage_bps"]): r for r in s["per_atr_quintile"]}
    for q in dict.fromkeys(r["atr_quintile"] for r in s["per_atr_quintile"]):
        cells = []
        for b in bps:
            r = by.get((q, b))
            cells.append("-" if r is None else f"{_m(r['pnl'])}|{_n(r['avg_r'], '{:+.2f}')}|{_n(r['avg_slippage_cost_r'])}")
        L.append(f"{q:<10}" + "".join(f"{c:>22}" for c in cells))

    L += ["", "PER-SYMBOL SENSITIVITY (P&L by scenario)"]
    L.append(f"{'symbol':<8}" + "".join(f"{b:>10g}" for b in bps)
             + "".join(f"{'Δ%g→%g' % (lo, hi):>10}" for lo, hi in ((0, 5), (5, 10))))
    sy = {(r["symbol"], r["slippage_bps"]): r for r in s["per_symbol"]}
    deg = {d["symbol"]: d for d in s["per_symbol_degradation"]}
    for sym in dict.fromkeys(r["symbol"] for r in s["per_symbol"]):
        L.append(f"{sym:<8}" + "".join(f"{_m(sy[(sym, b)]['pnl']):>10}" for b in bps)
                 + "".join(f"{_m(deg[sym].get('pnl_change_%g_to_%g_bps' % (lo, hi))):>10}" for lo, hi in ((0, 5), (5, 10))))

    L += ["", "EXIT-REASON SENSITIVITY (P&L | avg R)"]
    L.append(f"{'exit':<17}" + "".join(f"{b:>17g}" for b in bps))
    ex = {(r["exit_reason"], r["slippage_bps"]): r for r in s["per_exit_reason"]}
    for reason in dict.fromkeys(r["exit_reason"] for r in s["per_exit_reason"]):
        L.append(f"{reason:<17}" + "".join(
            f"{_m(ex[(reason, b)]['pnl']) + '|' + _n(ex[(reason, b)]['avg_r'], '{:+.2f}'):>17}" for b in bps))
    L += ["", s["disclaimer"]]
    return "\n".join(L)


# ================================================================ CLI
def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    from .historical_data import HistoricalDataError, load_universe
    from .historical_download import date_range_utc

    p = argparse.ArgumentParser(prog="python -m src.execution_sensitivity",
                                description="Sensibilidad del backtest de portafolio al slippage de ejecución "
                                            "(datos locales; nunca envía órdenes; no optimiza).")
    p.add_argument("--symbols", required=True)
    p.add_argument("--timeframe", default="1Min")
    p.add_argument("--start", required=True, help="Fecha NY inclusiva YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Fecha NY inclusiva YYYY-MM-DD")
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--slippage-bps", default=",".join(f"{b:g}" for b in DEFAULT_BPS),
                   help="Lista separada por comas (default 0,2.5,5,7.5,10,15)")
    p.add_argument("--initial-equity", type=float, default=100_000.0)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR,
                   help=f"Corrida de referencia a {REFERENCE_BPS:g} bps para verificar igualdad exacta")
    p.add_argument("--jobs", type=int, default=0, help="Procesos en paralelo (0 = uno por escenario)")
    a = p.parse_args(argv)

    try:
        bps = parse_bps(a.slippage_bps)
    except ValueError as e:
        p.error(str(e))
    out = a.output_dir.resolve()
    if a.baseline_dir is not None and a.baseline_dir.resolve() == out:
        p.error("--output-dir no puede ser el directorio de baseline")
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    try:
        s_utc, e_utc = date_range_utc(a.start, a.end)
        # igual que python -m src.backtest: velas previas solo como ventana de indicadores
        data = load_universe(a.data_dir, a.timeframe, symbols, s_utc - pd.Timedelta(days=4), e_utc)
    except HistoricalDataError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    jobs = a.jobs if a.jobs > 0 else len(bps)
    scenarios = run_scenarios(data.bars, bps, jobs=jobs, symbols=symbols, timeframe=a.timeframe, start=a.start,
                              end=a.end, initial_equity=a.initial_equity, commission=0.0)
    for _, r in scenarios:
        r.warnings.extend(data.warnings)
    for b, r in scenarios:
        write_outputs(r, summarize(r), a.output_dir / "scenarios" / scenario_dirname(b))
    study = build_study(scenarios, symbols)
    study["baseline_match"] = (baseline_match(a.output_dir / "scenarios" / scenario_dirname(REFERENCE_BPS), a.baseline_dir)
                               if REFERENCE_BPS in bps else {"checked": False,
                                                             "note": f"{REFERENCE_BPS:g} bps not among scenarios"})
    paths = write_study(study, a.output_dir)
    print(format_study(study))
    print()
    print("Archivos: " + ", ".join(str(x) for x in paths) + f" + scenarios/ ({len(bps)} full backtest outputs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
