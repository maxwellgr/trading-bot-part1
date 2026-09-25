# src/research_h003.py
"""
Corrida de investigación de STRATEGY_V2_HYPOTHESIS_003 (solo backtest, sin red).

    python -m src.research_h003 --split development --output-dir data/research_v1/h003_development

- Guardas (§11/§16 del spec): development permitido; validation solo con H003 FROZEN, compuerta D
  aprobada y nunca vista; contaminado/forward siempre rechazados. Sin flag de bypass.
- Datos: 1Min cargado SOLO hasta el fin del split -> 5Min RTH -> exactamente 200 velas de soporte + split.
- Motor de portafolio sin cambios con la estrategia inyectada; config idéntica a H001
  (production_args + lookback 278, sin límite de 24 h, 5 bps, $0).
- Reporte (§14/§15): embudo, métricas, salidas, símbolos + D6, estructura de señales, diagnósticos de
  ventana no contigua (nunca filtro), señales NVDA de la semana del split, re-arms y comparación con
  MA_BASELINE_V1 / H001 / H002 (contexto) desde resultados guardados de DEVELOPMENT.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .backtest_engine import BacktestConfig, BacktestEngine, production_args
from .backtest_report import _profit_factor, summarize, to_json, write_outputs
from .h001_autopsy import TIME_BUCKETS, load_development_bars, time_bucket
from .research_benchmark import _git_state
from .research_protocol import DEFAULT_PROTOCOL_PATH, get_split, load_protocol
from .strategy_v2_h001 import ENGINE_LOOKBACK, HygieneError, check_split_allowed, development_gates
from .strategy_v2_h003 import FROZEN_SPEC_COMMIT, HYPOTHESIS_ID, ConsolidationBreakoutH003, signal_structure

NY = "America/New_York"
DEFAULT_REGISTRY = Path("research") / "strategy_registry_v1.json"
STORED = {"MA_BASELINE_V1": Path("data") / "research_v1" / "benchmark_periods.json",
          "STRATEGY_V2_HYPOTHESIS_001": Path("data") / "research_v1" / "h001_development",
          "STRATEGY_V2_HYPOTHESIS_002": Path("data") / "research_v1" / "h002_development"}
NVDA_SPLIT_WEEK = ("2024-06-10", "2024-06-14")


def registry_entry(path: Path = DEFAULT_REGISTRY) -> Dict[str, Any]:
    reg = json.loads(Path(path).read_text(encoding="utf-8"))
    return next(e for e in reg["entries"] if e["id"] == HYPOTHESIS_ID)


def run_split(protocol: Dict[str, Any], split_name: str, data_dir: Path, entry: Dict[str, Any],
              dev_gate_passed: Optional[bool] = None) -> Dict[str, Any]:
    split = get_split(protocol, split_name)
    check_split_allowed(entry, split, dev_gate_passed)
    symbols = list(protocol["universe"]["symbols"])
    loaded = load_development_bars(data_dir, symbols, split)      # acotado al fin del split; soporte == 200
    ex = protocol["execution_defaults"]
    cfg = BacktestConfig(symbols=symbols, timeframe="5Min", start=split["start"], end=split["end"],
                         initial_equity=float(ex["initial_equity"]), slippage_bps=float(ex["slippage_bps"]),
                         commission=float(ex["commission_per_fill"]), record_evaluations=True,
                         window_hours_limit=False)
    result = BacktestEngine(cfg, loaded["bars"], production_args({"lookback": ENGINE_LOOKBACK}),
                            ConsolidationBreakoutH003()).run()
    s_utc = pd.Timestamp(split["start"]).tz_localize(NY).tz_convert("UTC")
    early = [e["bar_timestamp"] for e in result.evaluations if pd.Timestamp(e["bar_timestamp"]) < s_utc]
    early += [ts for ts, _ in result.equity_curve if pd.Timestamp(ts) < s_utc]
    if early:
        raise HygieneError(f"actividad antes del inicio del split: {early[:3]}")
    return {"split": split, "result": result, "bars": loaded["bars"], "support": loaded["info"]}


def _med(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def signal_report(result, bars: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    arr = {s: {"o": df["open"].to_numpy(float), "h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
               "c": df["close"].to_numpy(float), "ts": df.index.as_unit("ns").asi8,
               "pos": {t.isoformat(): k for k, t in enumerate(df.index)}} for s, df in bars.items()}
    sigs = []
    for e in result.evaluations:
        if e["signal"] != "BUY":
            continue
        a = arr[e["symbol"]]
        t = a["pos"][e["bar_timestamp"]]
        st = signal_structure(a["o"], a["h"], a["l"], a["c"], a["ts"], t)
        dec = pd.Timestamp(e["bar_timestamp"]).tz_convert(NY) + pd.Timedelta(minutes=5)
        sigs.append({"symbol": e["symbol"], "bar_timestamp": e["bar_timestamp"], "session": str(dec.date()),
                     "decision_minute_et": dec.hour * 60 + dec.minute, **st})
    traded = {(t["symbol"], t["entry_signal_timestamp"]) for t in result.trades}
    tr_sigs = [s for s in sigs if (s["symbol"], s["bar_timestamp"]) in traded]
    per_session: Dict[tuple, int] = {}
    rearm = 0
    for s in sigs:
        k = (s["symbol"], s["session"])
        per_session[k] = per_session.get(k, 0) + 1
        rearm += per_session[k] > 1
    tb: Dict[str, int] = {name: 0 for name, _, _ in TIME_BUCKETS}
    for s in sigs:
        b = time_bucket(s["decision_minute_et"])
        tb[b if b else "other"] = tb.get(b if b else "other", 0) + 1
    lo, hi = NVDA_SPLIT_WEEK

    def med(rows, key):
        return _med([r[key] for r in rows])
    return {
        "generated_signals": len(sigs),
        "all_signals": {k: med(sigs, k) for k in ("consolidation_range_atr", "breakout_range_atr", "breakout_distance_atr")},
        "accepted_trades": {k: med(tr_sigs, k) for k in ("consolidation_range_atr", "breakout_range_atr", "breakout_distance_atr")},
        "signals_with_noncontiguous_consolidation_window": sum(s["noncontiguous_window"] for s in sigs),
        "trades_with_noncontiguous_consolidation_window": sum(s["noncontiguous_window"] for s in tr_sigs),
        "time_of_day_signal_counts": tb,
        "nvda_split_week_signals": [s["bar_timestamp"] for s in sigs if s["symbol"] == "NVDA" and lo <= s["session"] <= hi],
        "same_session_rearm_signals": rearm,
        "_rows": sigs,
    }


def exit_report(trades) -> Dict[str, Any]:
    ex: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        g = ex.setdefault(t["exit_reason"], {"count": 0, "pnl": 0.0})
        g["count"] += 1
        g["pnl"] += t["realized_pnl"]
    legs = [l for t in trades for l in t["legs"] if l["purpose"] == "scale_out"]
    return {"final_exit_reason": dict(sorted(ex.items())),
            "scale_out_legs": {"count": len(legs), "realized_pnl": float(sum(l["realized_pnl"] for l in legs))}}


def symbol_report(trades, symbols) -> List[Dict[str, Any]]:
    rows = []
    for s in symbols:
        tr = [t for t in trades if t["symbol"] == s]
        rs = [t["realized_r"] for t in tr if t.get("realized_r") is not None]
        pnls = [t["realized_pnl"] for t in tr]
        rows.append({"symbol": s, "trades": len(tr), "pnl": float(sum(pnls)),
                     "expectancy_r": statistics.fmean(rs) if rs else None,
                     "profit_factor": _profit_factor(pnls) if len(tr) >= 2 else None})
    return rows


def benchmark_comparison(summary: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("trades", "win_rate_pct", "expectancy_r", "profit_factor", "total_r", "max_drawdown_pct", "max_consecutive_losses")

    def from_summary(s):
        t, p = s["trades"], s["portfolio"]
        return {"trades": t["trades"], "win_rate_pct": t["win_rate"] * 100, "expectancy_r": t["expectancy_r"],
                "profit_factor": t["profit_factor"], "total_r": t["total_r"], "max_drawdown_pct": p["max_drawdown_pct"],
                "max_consecutive_losses": t["max_consecutive_losses"]}
    out = {HYPOTHESIS_ID: from_summary(summary)}
    ma = STORED["MA_BASELINE_V1"]
    if ma.is_file():
        row = next(p for p in json.loads(ma.read_text(encoding="utf-8"))["periods"] if p["period"] == "development")
        out["MA_BASELINE_V1"] = {k: row[k] for k in keys}
    for hid in ("STRATEGY_V2_HYPOTHESIS_001", "STRATEGY_V2_HYPOTHESIS_002"):
        f = STORED[hid] / "summary.json"
        if f.is_file():
            out[hid + (" (context only)" if hid.endswith("002") else "")] = from_summary(json.loads(f.read_text(encoding="utf-8")))
    return {"evidence": "DEVELOPMENT only; descriptive; no ranking score", "metrics": out}


def build_report(run: Dict[str, Any], protocol: Dict[str, Any], out_dir: Path, compare: bool = True) -> Dict[str, Any]:
    result, split = run["result"], run["split"]
    summary = summarize(result)
    symbols = list(protocol["universe"]["symbols"])
    write_outputs(result, summary, out_dir)
    sr = signal_report(result, run["bars"])
    pd.DataFrame(sr.pop("_rows")).to_csv(out_dir / "h003_signals.csv", index=False)
    c = result.counters
    n_buy = sr["generated_signals"]
    cb = sum(c["circuit_breaker_blocked_entries"].values())
    dph = c["daily_profit_halt_blocked_entries"]
    t, p = summary["trades"], summary["portfolio"]
    report = {
        "hypothesis_id": HYPOTHESIS_ID, "frozen_spec_commit": FROZEN_SPEC_COMMIT, "split": split["name"],
        "role": split["role"], "dates": [split["start"], split["end"]],
        "evidence": ("DEVELOPMENT: hypothesis-development evidence only (close-location boundary already viewed in "
                     "H001 development); VALIDATION is the first untouched test"),
        "protocol_version": protocol["protocol_version"], "code": _git_state(), "execution": protocol["execution_defaults"],
        "support_bars": run["support"],
        "funnel": {"generated_buy_signals": n_buy, "risk_accepts": c["risk"]["ACCEPT"], "risk_rejects": c["risk"]["REJECT"],
                   "rejects_by_reason": dict(sorted(c["rejects_by_reason"].items())),
                   "pre_risk_blocks": {"circuit_breaker": dict(c["circuit_breaker_blocked_entries"]),
                                       "daily_profit_halt": dph,
                                       "symbol_already_open": n_buy - c["risk"]["ACCEPT"] - c["risk"]["REJECT"] - cb - dph}},
        "headline": {"completed_trades": t["trades"], "wins": t["wins"], "losses": t["losses"],
                     "win_rate_pct": t["win_rate"] * 100, "realized_pnl": p["realized_pnl_closed_trades"],
                     "return_pct": p["return_pct"], "expectancy": t["expectancy"], "expectancy_r": t["expectancy_r"],
                     "profit_factor": t["profit_factor"], "total_r": t["total_r"], "max_drawdown_pct": p["max_drawdown_pct"],
                     "max_consecutive_losses": t["max_consecutive_losses"], "open_positions_at_end": p["open_positions_at_end"],
                     "unfilled_orders_at_end": p["unfilled_orders_at_end"]},
        "exits": exit_report(result.trades),
        "by_symbol": symbol_report(result.trades, symbols),
        "signal_structure": sr,
        "gates": development_gates(summary, result.trades, symbols) if split["role"] == "development" else None,
    }
    if compare and split["role"] == "development":
        report["comparison_development"] = benchmark_comparison(summary)
    (out_dir / "h003_report.json").write_text(to_json(report), encoding="utf-8")
    return report


def _n(x, fmt="{:+.4f}"):
    return "-" if x is None else fmt.format(x)


def format_report(r: Dict[str, Any]) -> str:
    h, f, s = r["headline"], r["funnel"], r["signal_structure"]
    L = [f"{r['hypothesis_id']} — {r['split'].upper()} {r['dates'][0]} → {r['dates'][1]} "
         f"(frozen spec {r['frozen_spec_commit'][:7]}; 5Min RTH; next-bar open + 5 bps; $0)", "─" * 76, r["evidence"],
         f"Signals {f['generated_buy_signals']} | risk accepts {f['risk_accepts']} / rejects {f['risk_rejects']} "
         f"{f['rejects_by_reason']} | pre-risk blocks {f['pre_risk_blocks']}",
         f"Completed trades {h['completed_trades']} (W {h['wins']} / L {h['losses']}) | win {h['win_rate_pct']:.1f}% | "
         f"P&L ${h['realized_pnl']:,.2f} | return {h['return_pct']:.2f}%",
         f"Expectancy ${_n(h['expectancy'], '{:.2f}')} / {_n(h['expectancy_r'])}R | PF {_n(h['profit_factor'], '{:.4f}')} | "
         f"total R {_n(h['total_r'], '{:+.2f}')} | max DD {h['max_drawdown_pct']:.2f}% | max loss streak "
         f"{h['max_consecutive_losses']} | open at end {h['open_positions_at_end']}",
         f"Exits {r['exits']}",
         f"Signal structure (median, all / trades): range/ATR {_n(s['all_signals']['consolidation_range_atr'], '{:.3f}')} / "
         f"{_n(s['accepted_trades']['consolidation_range_atr'], '{:.3f}')} | breakout-bar range/ATR "
         f"{_n(s['all_signals']['breakout_range_atr'], '{:.3f}')} / {_n(s['accepted_trades']['breakout_range_atr'], '{:.3f}')} | "
         f"breakout distance/ATR {_n(s['all_signals']['breakout_distance_atr'], '{:.3f}')} / "
         f"{_n(s['accepted_trades']['breakout_distance_atr'], '{:.3f}')}",
         f"Time of day {s['time_of_day_signal_counts']} | non-contiguous windows: signals "
         f"{s['signals_with_noncontiguous_consolidation_window']}, trades {s['trades_with_noncontiguous_consolidation_window']} "
         f"| same-session re-arms {s['same_session_rearm_signals']} | NVDA split-week signals {len(s['nvda_split_week_signals'])}",
         "Support bars: " + ", ".join("%s=%d" % (k, v["support_bars"]) for k, v in r["support_bars"].items())]
    L.append(f"  {'symbol':<7}{'trades':>7}{'P&L':>12}{'exp R':>9}{'PF':>7}")
    for row in r["by_symbol"]:
        L.append(f"  {row['symbol']:<7}{row['trades']:>7}{row['pnl']:>12,.0f}{_n(row['expectancy_r'], '{:+.3f}'):>9}"
                 f"{_n(row['profit_factor'], '{:.2f}'):>7}")
    if r.get("comparison_development"):
        L.append("\nDEVELOPMENT comparison (descriptive):")
        for k, m in r["comparison_development"]["metrics"].items():
            L.append(f"  {k:<42} trades {m['trades']:>5} win {m['win_rate_pct']:.1f}% expR {_n(m['expectancy_r'])} "
                     f"PF {_n(m['profit_factor'], '{:.3f}')} totR {_n(m['total_r'], '{:+.1f}')} DD {m['max_drawdown_pct']:.1f}% "
                     f"streak {m['max_consecutive_losses']}")
    if r.get("gates"):
        L.append("")
        for k, g in r["gates"]["gates"].items():
            L.append(f"  {k:<26} value={g['value']}  rule {g['rule']}  -> {'PASS' if g['passed'] else 'FAIL'}")
        L.append(f"\nProgression to validation: {r['gates']['progression_to_validation']}")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.research_h003", description=f"Corrida de investigación de {HYPOTHESIS_ID}.")
    p.add_argument("--split", required=True)
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args(argv)
    protocol = load_protocol(a.protocol)
    try:
        run = run_split(protocol, a.split, a.data_dir, registry_entry(a.registry))
    except HygieneError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    print(format_report(build_report(run, protocol, a.output_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
