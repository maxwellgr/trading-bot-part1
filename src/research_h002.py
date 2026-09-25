# src/research_h002.py
"""
Corrida de investigación de STRATEGY_V2_HYPOTHESIS_002 (solo backtest, sin red).

    python -m src.research_h002 --split development --output-dir data/research_v1/h002_development
    python -m src.research_h002 --split development --ftp-off-check --output-dir data/research_v1/h002_ftp_off_check

- Guardas (§7/§11 del spec): development permitido; validation solo con H002 FROZEN, compuerta D
  aprobada y nunca vista; contaminado/forward siempre rechazados. Sin flag de bypass.
- Datos: 1Min cargado SOLO hasta el fin del split -> 5Min RTH -> 200 velas de soporte + split (igual a H001).
- Config idéntica a H001 (production_args + lookback 278, sin límite de 24 h, 5 bps, $0) y la MISMA
  estrategia TrendPullbackH001; la única diferencia es FTPEngine(ftp_enabled=True).
- --ftp-off-check: corre FTPEngine(ftp_enabled=False) y compara trades.json byte a byte con la corrida
  H001 guardada (prueba de que H001 no cambia con FTP apagado).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .backtest_engine import BacktestConfig, production_args
from .backtest_report import summarize, to_json, write_outputs
from .h001_autopsy import load_development_bars
from .research_benchmark import _git_state
from .research_protocol import DEFAULT_PROTOCOL_PATH, get_split, load_protocol
from .strategy_v2_h001 import ENGINE_LOOKBACK, TIMEFRAME, HygieneError, TrendPullbackH001, check_split_allowed, development_gates
from .strategy_v2_h002 import (
    FROZEN_SPEC_COMMIT, HYPOTHESIS_ID, FTPEngine, compare_with_h001, ftp_summary, overnight_ftp_metrics,
)

DEFAULT_REGISTRY = Path("research") / "strategy_registry_v1.json"
H001_STORED = Path("data") / "research_v1" / "h001_development"


def registry_entry(path: Path = DEFAULT_REGISTRY) -> Dict[str, Any]:
    reg = json.loads(Path(path).read_text(encoding="utf-8"))
    return next(e for e in reg["entries"] if e["id"] == HYPOTHESIS_ID)


def run_split(protocol: Dict[str, Any], split_name: str, data_dir: Path, entry: Dict[str, Any],
              ftp_enabled: bool = True, dev_gate_passed: Optional[bool] = None) -> Dict[str, Any]:
    split = get_split(protocol, split_name)
    check_split_allowed(entry, split, dev_gate_passed)
    symbols = list(protocol["universe"]["symbols"])
    loaded = load_development_bars(data_dir, symbols, split)   # carga acotada al fin del split
    ex = protocol["execution_defaults"]
    cfg = BacktestConfig(symbols=symbols, timeframe=TIMEFRAME, start=split["start"], end=split["end"],
                         initial_equity=float(ex["initial_equity"]), slippage_bps=float(ex["slippage_bps"]),
                         commission=float(ex["commission_per_fill"]), record_evaluations=True,
                         window_hours_limit=False)
    eng = FTPEngine(cfg, loaded["bars"], production_args({"lookback": ENGINE_LOOKBACK}), TrendPullbackH001(),
                    ftp_enabled=ftp_enabled)
    result = eng.run()
    return {"split": split, "result": result, "events": eng.ftp_events, "support": loaded["info"]}


def build_report(run: Dict[str, Any], protocol: Dict[str, Any], out_dir: Path,
                 h001_dir: Optional[Path] = H001_STORED) -> Dict[str, Any]:
    result, split = run["result"], run["split"]
    summary = summarize(result)
    symbols = list(protocol["universe"]["symbols"])
    write_outputs(result, summary, out_dir)
    events = run["events"]
    (out_dir / "h002_ftp_events.json").write_text(to_json(events), encoding="utf-8")
    report = {
        "hypothesis_id": HYPOTHESIS_ID, "frozen_spec_commit": FROZEN_SPEC_COMMIT, "split": split["name"],
        "role": split["role"], "dates": [split["start"], split["end"]],
        "evidence": ("DEVELOPMENT: screening/resubstitution check only (FTP constants derived on this split); "
                     "VALIDATION is the first untouched test"),
        "protocol_version": protocol["protocol_version"], "code": _git_state(),
        "execution": protocol["execution_defaults"], "support_bars": run["support"],
        "headline": {k: summary["trades"][k] for k in ("trades", "wins", "losses", "win_rate", "expectancy",
                                                      "expectancy_r", "profit_factor", "total_r", "max_consecutive_losses")}
                    | {k: summary["portfolio"][k] for k in ("realized_pnl_closed_trades", "return_pct", "max_drawdown_pct",
                                                            "ending_equity", "open_positions_at_end", "unfilled_orders_at_end")},
        "exit_reasons": summary["execution"]["completed_trade_exit_reasons"],
        "ftp": ftp_summary(result.trades, events),
        "overnight_ftp": overnight_ftp_metrics(result.trades, events),
        "gates": development_gates(summary, result.trades, symbols) if split["role"] == "development" else None,
    }
    if h001_dir is not None and (h001_dir / "trades.json").is_file() and split["role"] == "development":
        h1t = json.loads((h001_dir / "trades.json").read_text(encoding="utf-8"))
        h1s = json.loads((h001_dir / "summary.json").read_text(encoding="utf-8"))
        report["comparison_vs_h001"] = compare_with_h001(h1t, h1s, json.loads(to_json(result.trades)), summary)
    (out_dir / "h002_report.json").write_text(to_json(report), encoding="utf-8")
    return report


def ftp_off_check(run: Dict[str, Any], out_dir: Path, h001_dir: Path = H001_STORED) -> Dict[str, Any]:
    result = run["result"]
    summary = summarize(result)
    write_outputs(result, summary, out_dir)
    same = {}
    for f in ("trades.json", "trades.csv", "equity_curve.csv", "daily_results.csv"):
        same[f] = (out_dir / f).read_bytes() == (h001_dir / f).read_bytes()
    same["summary.json"] = (json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
                            == json.loads((h001_dir / "summary.json").read_text(encoding="utf-8")))
    rep = {"check": "FTPEngine(ftp_enabled=False) vs stored H001 development", "h001_dir": str(h001_dir),
           "files_identical": same, "identical": all(same.values()), "ftp_events": len(run["events"])}
    (out_dir / "ftp_off_check.json").write_text(to_json(rep), encoding="utf-8")
    return rep


def _n(x, fmt="{:+.4f}"):
    return "-" if x is None else fmt.format(x)


def format_report(r: Dict[str, Any]) -> str:
    h, f, o = r["headline"], r["ftp"], r["overnight_ftp"]
    L = [f"{r['hypothesis_id']} — {r['split'].upper()} {r['dates'][0]} → {r['dates'][1]} "
         f"(frozen spec {r['frozen_spec_commit'][:7]}; 5Min RTH; next-bar open + 5 bps)", "─" * 72,
         r["evidence"],
         f"Trades {h['trades']} (W {h['wins']} / L {h['losses']}) | win {h['win_rate'] * 100:.1f}% | "
         f"P&L ${h['realized_pnl_closed_trades']:,.2f} | return {h['return_pct']:.2f}%",
         f"Expectancy {_n(h['expectancy_r'])}R | PF {_n(h['profit_factor'], '{:.4f}')} | total R {_n(h['total_r'], '{:+.2f}')} | "
         f"max DD {h['max_drawdown_pct']:.2f}% | max loss streak {h['max_consecutive_losses']} | open at end "
         f"{h['open_positions_at_end']} | unfilled at end {h['unfilled_orders_at_end']}",
         f"Exits: {r['exit_reasons']}",
         f"FTP: checkpoints {f['checkpoints_evaluated']} | condition met {f['condition_met']} | triggered {f['triggered']} "
         f"{f['triggered_by_path']} | blocked {f['blocked']} | FTP-exit trades {f['ftp_exit_trades']} "
         f"P&L ${f['ftp_trades_realized_pnl']:,.2f} | gap trades >0.75R {f['gap_trades_fill_gt_0_75r_above_modeled']}",
         f"Overnight FTP: fills {o['overnight_ftp_fills']} | P&L ${o['overnight_ftp_realized_pnl']:,.2f} | "
         f"losses {o['overnight_ftp_losses']} | wins {o['overnight_ftp_wins']} | BE {o['overnight_ftp_breakevens']} | "
         f"by path {o['by_checkpoint_path']}"]
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
    p = argparse.ArgumentParser(prog="python -m src.research_h002", description=f"Corrida de investigación de {HYPOTHESIS_ID}.")
    p.add_argument("--split", required=True)
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--ftp-off-check", action="store_true",
                   help="Corre con FTP apagado y compara con la corrida H001 guardada (solo development).")
    a = p.parse_args(argv)
    protocol = load_protocol(a.protocol)
    try:
        entry = registry_entry(a.registry)
        if a.ftp_off_check and get_split(protocol, a.split)["role"] != "development":
            raise HygieneError("--ftp-off-check solo sobre development")
        run = run_split(protocol, a.split, a.data_dir, entry, ftp_enabled=not a.ftp_off_check)
    except HygieneError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    if a.ftp_off_check:
        rep = ftp_off_check(run, a.output_dir)
        print(f"FTP-off vs stored H001 development identical: {rep['identical']} {rep['files_identical']}")
        return 0 if rep["identical"] else 1
    rep = build_report(run, protocol, a.output_dir)
    print(format_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
