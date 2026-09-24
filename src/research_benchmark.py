# src/research_benchmark.py
"""
Benchmark de MA_BASELINE_V1 (estrategia de producción SIN cambios) sobre los
splits del protocolo de investigación, cada uno por separado. Caracteriza; no
optimiza, no combina períodos en un puntaje y no elige "mejor" nada.

    python -m src.research_benchmark --protocol config/research_protocol_v1.json \
        --data-dir data/historical --output-dir data/research_v1 \
        --periods development,validation,known_diagnostic \
        --reuse known_diagnostic=data/backtests/baseline_v1_entry_quality

- Cada split se corre como un backtest independiente (fechas NY inclusivas del
  protocolo; velas de 4 días previos solo como ventana de indicadores, igual que
  python -m src.backtest) con los supuestos de ejecución del protocolo.
- --reuse NAME=DIR reutiliza una corrida ya validada SOLO si su config coincide
  exactamente (símbolos, fechas, timeframe, slippage, comisión, equity y los
  parámetros de estrategia/riesgo de producción); si no, se rechaza.
- Solo splits cuyo rol permite "benchmark" (development, validation,
  contaminated). Cada resultado lleva su etiqueta de evidencia; nunca se suman.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .research_protocol import DEFAULT_PROTOCOL_PATH, evidence_label, load_protocol, require_use

STRATEGY_ID = "MA_BASELINE_V1"
BENCHMARK_PARAMS = {"strategy": "ma", "fast": 3, "slow": 7}
DISCLAIMER = ("Benchmark characterization only. Periods are separate evidence classes and are never combined; "
              "nothing here is a recommendation, a selection or an optimized result.")
CONFIG_KEYS = ("symbols", "timeframe", "start", "end", "initial_equity", "slippage_bps", "commission")


# ================================================================ corrida
def _backtest_config(protocol: Dict[str, Any], split: Dict[str, Any]) -> Dict[str, Any]:
    ex = protocol["execution_defaults"]
    return {"symbols": list(protocol["universe"]["symbols"]), "timeframe": protocol["universe"]["timeframe"],
            "start": split["start"], "end": split["end"], "initial_equity": float(ex["initial_equity"]),
            "slippage_bps": float(ex["slippage_bps"]), "commission": float(ex["commission_per_fill"])}


def _run_split(job: Tuple[Dict[str, Any], str, str]) -> str:
    """Worker: carga datos, corre el backtest de producción y escribe las salidas estándar."""
    cfg, data_dir, out_dir = job
    from .backtest_engine import BacktestConfig, run_backtest
    from .backtest_report import summarize, write_outputs
    from .historical_data import load_universe
    from .historical_download import date_range_utc

    s_utc, e_utc = date_range_utc(cfg["start"], cfg["end"])
    data = load_universe(Path(data_dir), cfg["timeframe"], cfg["symbols"], s_utc - pd.Timedelta(days=4), e_utc)
    result = run_backtest(BacktestConfig(**cfg), data.bars)
    result.warnings.extend(data.warnings)
    write_outputs(result, summarize(result), Path(out_dir))
    return out_dir


def check_reusable(run_dir: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """La corrida en run_dir es reutilizable solo si coincide exactamente con cfg y con producción."""
    from .backtest_engine import production_args
    s = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    mism = [k for k in CONFIG_KEYS if s["config"].get(k) != cfg[k]]
    prod = vars(production_args())
    mism += [f"strategy.{k}" for k, v in s["strategy"].items() if prod.get(k) != v]
    if mism:
        raise ValueError(f"{run_dir}: no reutilizable, difiere en {mism}")
    return s


# ================================================================ agregación
def _pf(pnls: Sequence[float]) -> Optional[float]:
    from .backtest_report import _profit_factor
    return _profit_factor(pnls)


def period_row(name: str, split: Dict[str, Any], summary: Dict[str, Any], source: str) -> Dict[str, Any]:
    t, p = summary["trades"], summary["portfolio"]
    return {"period": name, "role": split["role"], "evidence": evidence_label(split),
            "start": split["start"], "end": split["end"], "source": source,
            "trades": t["trades"], "wins": t["wins"], "losses": t["losses"], "win_rate_pct": t["win_rate"] * 100,
            "realized_pnl": p["realized_pnl_closed_trades"], "ending_equity": p["ending_equity"],
            "return_pct": p["return_pct"], "expectancy": t["expectancy"], "expectancy_r": t["expectancy_r"],
            "profit_factor": t["profit_factor"], "total_r": t["total_r"], "max_drawdown_pct": p["max_drawdown_pct"],
            "max_consecutive_losses": t["max_consecutive_losses"],
            "open_positions_at_end": p["open_positions_at_end"], "warnings": summary.get("warnings", [])}


def symbol_rows(name: str, split: Dict[str, Any], trades: List[Dict[str, Any]],
                symbols: Sequence[str]) -> List[Dict[str, Any]]:
    rows = []
    for sym in symbols:
        tr = [t for t in trades if t["symbol"] == sym]
        rs = [t["realized_r"] for t in tr if t.get("realized_r") is not None]
        pnls = [t["realized_pnl"] for t in tr]
        rows.append({"period": name, "evidence": evidence_label(split), "symbol": sym, "trades": len(tr),
                     "realized_pnl": sum(pnls), "expectancy_r": sum(rs) / len(rs) if rs else None,
                     "profit_factor": _pf(pnls) if len(tr) >= 2 else None,
                     "win_rate_pct": sum(1 for t in tr if t["result"] == "win") / len(tr) * 100 if tr else None})
    return rows


def aggregate(protocol: Dict[str, Any], runs: Dict[str, Tuple[Path, str]]) -> Dict[str, Any]:
    """runs: nombre -> (dir con summary.json/trades.json, fuente). Orden = orden del protocolo (determinista)."""
    symbols = protocol["universe"]["symbols"]
    periods, by_symbol = [], []
    for split in protocol["splits"]:
        name = split["name"]
        if name not in runs:
            continue
        run_dir, source = runs[name]
        summary = json.loads((Path(run_dir) / "summary.json").read_text(encoding="utf-8"))
        trades = json.loads((Path(run_dir) / "trades.json").read_text(encoding="utf-8"))
        periods.append(period_row(name, split, summary, source))
        by_symbol += symbol_rows(name, split, trades, symbols)
    return {"periods": periods, "by_symbol": by_symbol}


def _git_state() -> Dict[str, Any]:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        return {"commit": head, "working_tree_dirty": bool(dirty.strip()),
                "dirty_paths": [l[3:] for l in dirty.splitlines()]}
    except Exception as e:  # sin git: se registra, no se inventa
        return {"commit": None, "error": str(e)}


def build_report(protocol: Dict[str, Any], agg: Dict[str, Any], generated_at: str) -> Dict[str, Any]:
    views = [{"split": r["period"], "strategy_id": STRATEGY_ID, "viewed_at": generated_at,
              "note": "MA_BASELINE_V1 is the frozen production strategy; viewing validation does not "
                      "contaminate it, but any NEW version must be frozen before its own validation look."}
             for r in agg["periods"] if r["role"] == "validation"]
    return {"report": "research_benchmark_v1", "generated_at": generated_at, "disclaimer": DISCLAIMER,
            "protocol_version": protocol["protocol_version"], "strategy_id": STRATEGY_ID,
            "strategy_parameters": BENCHMARK_PARAMS, "execution": protocol["execution_defaults"],
            "code": _git_state(), "validation_views": views, **agg}


# ================================================================ salidas
PERIOD_COLUMNS = ["period", "role", "evidence", "start", "end", "source", "trades", "wins", "losses", "win_rate_pct",
                  "realized_pnl", "ending_equity", "return_pct", "expectancy", "expectancy_r", "profit_factor",
                  "total_r", "max_drawdown_pct", "max_consecutive_losses", "open_positions_at_end"]
SYMBOL_COLUMNS = ["period", "evidence", "symbol", "trades", "realized_pnl", "expectancy_r", "profit_factor",
                  "win_rate_pct"]


def write_report(report: Dict[str, Any], out_dir: Path, text: str) -> List[Path]:
    from .backtest_report import to_json
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    p = out_dir / "benchmark_periods.json"
    p.write_text(to_json(report), encoding="utf-8")
    written.append(p)
    for name, cols, rows in (("benchmark_periods.csv", PERIOD_COLUMNS, report["periods"]),
                             ("benchmark_by_symbol.csv", SYMBOL_COLUMNS, report["by_symbol"])):
        p = out_dir / name
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows({k: r.get(k) for k in cols} for r in rows)
        written.append(p)
    p = out_dir / "research_protocol_report.txt"
    p.write_text(text + "\n", encoding="utf-8")
    written.append(p)
    return written


def _n(v: Any, fmt: str = "{:.2f}") -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and v == float("inf"):
        return "inf"
    return fmt.format(v)


def format_report(report: Dict[str, Any]) -> str:
    L = [f"RESEARCH BENCHMARK — {report['strategy_id']} (MA {report['strategy_parameters']['fast']}/"
         f"{report['strategy_parameters']['slow']}, unchanged production strategy)", "─" * 72,
         f"Execution: next-bar open, {report['execution']['slippage_bps']:g} bps, "
         f"${report['execution']['commission_per_fill']:g}/fill | protocol {report['protocol_version']} | "
         f"code {str(report['code'].get('commit'))[:10]}{' (dirty tree)' if report['code'].get('working_tree_dirty') else ''}"]
    for r in report["periods"]:
        L += ["", f"{r['evidence']} — {r['period']} {r['start']} → {r['end']} ({r['source']})",
              f"  trades {r['trades']} (W {r['wins']} / L {r['losses']}) | win {r['win_rate_pct']:.1f}% | "
              f"P&L ${r['realized_pnl']:,.2f} | return {r['return_pct']:.2f}%",
              f"  expectancy ${_n(r['expectancy'])} / {_n(r['expectancy_r'], '{:+.3f}')} R | PF {_n(r['profit_factor'])} | "
              f"total R {_n(r['total_r'], '{:+.1f}')} | max DD {r['max_drawdown_pct']:.2f}% | "
              f"max loss streak {r['max_consecutive_losses']}"]
        for w in r["warnings"]:
            L.append(f"  WARNING: {w}")
        L.append(f"  {'symbol':<7}{'trades':>7}{'P&L':>12}{'exp R':>8}{'PF':>7}")
        for s in (x for x in report["by_symbol"] if x["period"] == r["period"]):
            L.append(f"  {s['symbol']:<7}{s['trades']:>7}{s['realized_pnl']:>12,.0f}{_n(s['expectancy_r'], '{:+.3f}'):>8}"
                     f"{_n(s['profit_factor']):>7}")
    L += ["", "FORWARD EVIDENCE — none yet (chronological paper/live observations from the forward start).",
          "", report["disclaimer"]]
    return "\n".join(L)


# ================================================================ CLI
def parse_reuse(items: Sequence[str]) -> Dict[str, Path]:
    out = {}
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ValueError(f"--reuse espera NOMBRE=DIR, recibido {item!r}")
        out[name.strip()] = Path(path.strip())
    return out


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.research_benchmark",
                                description="Benchmark de MA_BASELINE_V1 por split del protocolo (sin optimizar).")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--output-dir", type=Path, default=Path("data") / "research_v1")
    p.add_argument("--periods", default="development,validation,known_diagnostic")
    p.add_argument("--reuse", action="append", default=[], metavar="NAME=DIR",
                   help="Reutiliza una corrida validada idéntica (p. ej. known_diagnostic=data/backtests/baseline_v1_entry_quality)")
    p.add_argument("--jobs", type=int, default=0, help="Procesos en paralelo (0 = uno por período a correr)")
    a = p.parse_args(argv)

    protocol = load_protocol(a.protocol)
    names = [n.strip() for n in a.periods.split(",") if n.strip()]
    try:
        splits = {n: require_use(protocol, n, "benchmark") for n in names}
        reuse = parse_reuse(a.reuse)
        runs: Dict[str, Tuple[Path, str]] = {}
        to_run = []
        for n in names:
            cfg = _backtest_config(protocol, splits[n])
            if n in reuse:
                check_reusable(reuse[n], cfg)
                runs[n] = (reuse[n], f"reused {reuse[n].as_posix()}")
            else:
                out = a.output_dir / "periods" / n
                to_run.append((cfg, str(a.data_dir), str(out)))
                runs[n] = (out, "run")
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    jobs = a.jobs if a.jobs > 0 else max(1, len(to_run))
    if jobs > 1 and len(to_run) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(to_run))) as pool:
            list(pool.map(_run_split, to_run))
    else:
        for job in to_run:
            _run_split(job)
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = build_report(protocol, aggregate(protocol, runs), generated)
    text = format_report(report)
    paths = write_report(report, a.output_dir, text)
    print(text)
    print("\nArchivos: " + ", ".join(str(x) for x in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
