# src/research_h001.py
"""
Corrida de investigación de STRATEGY_V2_HYPOTHESIS_001 (solo backtest, sin red).

    python -m src.research_h001 --split development \
        --data-dir data/historical --output-dir data/research_v1/h001_development

- Guardas de higiene (§9 del spec): development permitido; validation solo con
  H001 FROZEN, compuerta D aprobada y nunca vista; contaminado/forward nunca.
- Datos: caché 1Min auditada -> 5Min RTH (§3). Soporte: exactamente las 200
  velas 5Min previas a la primera sesión del split, por símbolo (Q3); si faltan,
  falla. El soporte solo alimenta indicadores: el motor no decide antes del inicio.
- Riesgo/gestión: production_args() sin cambios salvo `lookback` (ventana del
  indicador, §4) y timeframe 5Min; sin límite de 24 h (window_hours_limit=False).
- Ejecución: apertura siguiente + 5 bps, comisión 0 (protocolo).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .backtest_engine import BacktestConfig, BacktestResult, production_args, run_backtest
from .backtest_report import summarize, to_json, write_outputs
from .historical_data import load_symbol_bars
from .research_benchmark import _git_state
from .research_protocol import DEFAULT_PROTOCOL_PATH, get_split, load_protocol
from .strategy_v2_h001 import (
    ENGINE_LOOKBACK, HYPOTHESIS_ID, SUPPORT_BARS, TIMEFRAME, HygieneError, TrendPullbackH001, check_split_allowed,
    development_gates, resample_rth_5min, split_bars,
)

DEFAULT_REGISTRY = Path("research") / "strategy_registry_v1.json"
NVDA_SPLIT_WINDOW = ("2024-06-10", "2024-06-14")


class SupportError(RuntimeError):
    pass


def registry_entry(path: Path = DEFAULT_REGISTRY) -> Dict[str, Any]:
    reg = json.loads(Path(path).read_text(encoding="utf-8"))
    return next(e for e in reg["entries"] if e["id"] == HYPOTHESIS_ID)


def prepare_bars(data_dir: Path, symbols: List[str], start: str, end: str) -> Dict[str, Any]:
    bars, info = {}, {}
    for sym in symbols:
        b5 = resample_rth_5min(load_symbol_bars(data_dir, "1Min", sym))
        part = split_bars(b5, start, end)
        if part["support_bars"] < SUPPORT_BARS:
            raise SupportError(f"{sym}: solo {part['support_bars']} velas de soporte antes de {start} "
                               f"(se requieren {SUPPORT_BARS}); no se usa ningún fallback")
        inside = part["bars"].iloc[part["support_bars"]:]
        bars[sym] = part["bars"][["open", "high", "low", "close", "volume"]]
        info[sym] = {k: part[k] for k in ("support_bars", "support_first", "support_last", "split_bars")}
        info[sym]["split_bars_with_missing_minutes"] = int((inside["n_minutes"] < 5).sum())
    return {"bars": bars, "info": info}


def run_split(protocol: Dict[str, Any], split_name: str, data_dir: Path,
              entry: Dict[str, Any], dev_gate_passed: Optional[bool] = None) -> Dict[str, Any]:
    split = get_split(protocol, split_name)
    check_split_allowed(entry, split, dev_gate_passed)
    symbols = list(protocol["universe"]["symbols"])
    ex = protocol["execution_defaults"]
    prep = prepare_bars(data_dir, symbols, split["start"], split["end"])
    cfg = BacktestConfig(symbols=symbols, timeframe=TIMEFRAME, start=split["start"], end=split["end"],
                         initial_equity=float(ex["initial_equity"]), slippage_bps=float(ex["slippage_bps"]),
                         commission=float(ex["commission_per_fill"]), record_evaluations=True,
                         window_hours_limit=False)
    args = production_args({"lookback": ENGINE_LOOKBACK})
    result = run_backtest(cfg, prep["bars"], args, TrendPullbackH001())
    _assert_nothing_before_start(result, split["start"])
    return {"split": split, "result": result, "support": prep["info"]}


def _assert_nothing_before_start(result: BacktestResult, start: str) -> None:
    """El soporte solo alimenta indicadores: ninguna decisión, orden, fill ni punto de equity antes del inicio."""
    s_utc = pd.Timestamp(start).tz_localize("America/New_York").tz_convert("UTC")
    checks = ([e["bar_timestamp"] for e in result.evaluations] + [f["fill_ts"] for f in result.fills]
              + [ts for ts, _ in result.equity_curve] + [e["bar_timestamp"] for e in result.risk_evaluations])
    early = [x for x in checks if pd.Timestamp(x) < s_utc]
    if early:
        raise HygieneError(f"actividad antes del inicio del split: {early[:3]}")


def build_report(run: Dict[str, Any], protocol: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    result, split = run["result"], run["split"]
    summary = summarize(result)
    symbols = list(protocol["universe"]["symbols"])
    write_outputs(result, summary, out_dir)
    signals = [e for e in result.evaluations if e["signal"] == "BUY"]
    lo, hi = NVDA_SPLIT_WINDOW
    nvda = [{"bar_timestamp": e["bar_timestamp"], "close": e["bar_close"]} for e in signals
            if e["symbol"] == "NVDA" and lo <= str(pd.Timestamp(e["bar_timestamp"]).tz_convert("America/New_York").date()) <= hi]
    report = {
        "hypothesis_id": HYPOTHESIS_ID, "split": split["name"], "role": split["role"],
        "dates": [split["start"], split["end"]], "protocol_version": protocol["protocol_version"],
        "spec": "research/strategy_v2_hypothesis_001.md", "code": _git_state(),
        "execution": protocol["execution_defaults"], "timeframe": TIMEFRAME, "engine_lookback": ENGINE_LOOKBACK,
        "support_bars": run["support"],
        "signals_emitted": {s: result.counters["signals"][s]["BUY"] for s in symbols},
        "entries_accepted": result.counters["risk"]["ACCEPT"], "entries_rejected": result.counters["risk"]["REJECT"],
        "nvda_signals_2024_06_10_to_14": nvda,
        "headline": {k: summary["trades"][k] for k in ("trades", "wins", "losses", "win_rate", "expectancy",
                                                      "expectancy_r", "profit_factor", "total_r",
                                                      "max_consecutive_losses")}
                    | {k: summary["portfolio"][k] for k in ("realized_pnl_closed_trades", "return_pct",
                                                            "max_drawdown_pct", "ending_equity",
                                                            "open_positions_at_end")},
        "exit_reasons": summary["execution"]["completed_trade_exit_reasons"],
        "gates": development_gates(summary, result.trades, symbols) if split["role"] == "development" else None,
    }
    (out_dir / "h001_report.json").write_text(to_json(report), encoding="utf-8")
    return report


def format_report(r: Dict[str, Any]) -> str:
    h = r["headline"]
    L = [f"{r['hypothesis_id']} — {r['split'].upper()} {r['dates'][0]} → {r['dates'][1]} "
         f"(5Min RTH, next-bar open + {r['execution']['slippage_bps']:g} bps, $0 commission)", "─" * 72,
         f"Trades {h['trades']} (W {h['wins']} / L {h['losses']}) | win {h['win_rate'] * 100:.1f}% | "
         f"P&L ${h['realized_pnl_closed_trades']:,.2f} | return {h['return_pct']:.2f}%",
         f"Expectancy ${h['expectancy']:.2f} / {h['expectancy_r'] if h['expectancy_r'] is None else round(h['expectancy_r'], 4)} R | "
         f"PF {h['profit_factor']} | total R {h['total_r']} | max DD {h['max_drawdown_pct']:.2f}% | "
         f"max loss streak {h['max_consecutive_losses']} | open at end {h['open_positions_at_end']}",
         f"Signals emitted {sum(r['signals_emitted'].values())} | entries accepted {r['entries_accepted']} / "
         f"rejected {r['entries_rejected']} | exits {r['exit_reasons']}",
         "Support bars per symbol: " + ", ".join("%s=%d" % (s, v["support_bars"]) for s, v in r["support_bars"].items()),
         f"NVDA signals 2024-06-10..14: {r['nvda_signals_2024_06_10_to_14'] or 'none'}"]
    if r["gates"]:
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
    p = argparse.ArgumentParser(prog="python -m src.research_h001", description=f"Corrida de investigación de {HYPOTHESIS_ID}.")
    p.add_argument("--split", required=True)
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args(argv)
    protocol = load_protocol(a.protocol)
    try:
        entry = registry_entry(a.registry)
        run = run_split(protocol, a.split, a.data_dir, entry)
    except (HygieneError, SupportError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    report = build_report(run, protocol, a.output_dir)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
