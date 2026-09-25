# src/random_control_0bps.py
"""
RANDOM CONTROL @ 0 BPS — diagnóstico confirmatorio y estrecho de Research Sanity Audit V1 (solo DEVELOPMENT).

    python -m src.random_control_0bps --output-dir data/research_v1/random_control_0bps

Pregunta: ¿las entradas aleatorias emparejadas también son positivas a 0 bps bajo la misma gestión aislada?
- Reutiliza EXACTAMENTE la metodología congelada del audit (research_sanity_audit.random_control: símbolo, mes,
  tramo de 30 min, quintil ATR% punto-en-tiempo, jerarquía L0–L4, exclusiones, 200 réplicas con las mismas semillas).
- Las señales crudas reales se leen de las salidas del audit (raw_signal_forward_*.csv); las estadísticas de
  horizonte fijo de cada réplica se recalculan y DEBEN coincidir con random_control_replicates.csv del audit
  (prueba de que las extracciones son idénticas). Si no coinciden: se detiene.
- Simulación aislada de un trade (mismo motor, misma gestión, portafolio vacío) a 0 bps de slippage de fills,
  para reales y aleatorias. El RiskManager sigue asumiendo sus 5 bps (igual que el escenario 0 bps del audit).
- Descriptivo; no define ninguna estrategia. Sin Validation ni Forward.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from . import research_sanity_audit as ra
from .backtest_engine import production_args
from .backtest_report import to_json
from .h001_autopsy import load_development_bars
from .historical_data import load_symbol_bars
from .research_protocol import DEFAULT_PROTOCOL_PATH, get_split, load_protocol
from .run_paper import build_risk_config
from .strategy_v2_h001 import resample_rth_5min

NY = "America/New_York"
BPS = 0.0
AUDIT_DIR = Path("data") / "research_v1" / "research_sanity_audit_v1"
METRICS = ("expectancy_r", "pf_r", "win_rate_pct", "immediate_failure_pct", "stop_hit_pct", "giveback_pct",
           "take_profit_pct", "scale_out_pct")
SIMILAR_BAND = (5.0, 95.0)      # "similar" = percentil empírico real dentro de [5, 95] (mismo criterio 95 del audit)


class ReplicateMismatch(RuntimeError):
    pass


def verify_replicates(rc_rep: pd.DataFrame, stored: pd.DataFrame, name: str) -> Dict[str, Any]:
    a = rc_rep.reset_index(drop=True)
    b = stored[stored["strategy"] == name].reset_index(drop=True)
    cols = [c for c in ra.STAT_KEYS] + ["n"]
    ok = len(a) == len(b) and all(
        ((a[c] - b[c]).abs().fillna(0) < 1e-12).all() for c in cols)
    if not ok:
        raise ReplicateMismatch(f"{name}: las 200 réplicas no coinciden con las del audit congelado")
    return {"replicates": int(len(a)), "identical_to_audit": True}


def summarize(name: str, real_stats: Dict[str, Any], per_rep: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for k in METRICS:
        vals = [p[k] for p in per_rep]
        pct = ra.percentile_of(real_stats[k], vals)
        out[k] = {"real": real_stats[k], "random_median": ra._med(vals), "random_q05": ra._q(vals, .05),
                  "random_q25": ra._q(vals, .25), "random_q75": ra._q(vals, .75), "random_q95": ra._q(vals, .95),
                  "real_empirical_percentile": pct,
                  "real_within_random_q05_q95": None if pct is None else SIMILAR_BAND[0] <= pct <= SIMILAR_BAND[1]}
    return out


def run(protocol: Dict[str, Any], data_dir: Path, audit_dir: Path, out_dir: Path, workers: int = 6,
        n_rep: int = ra.N_REPLICATES) -> Dict[str, Any]:
    ra._guard_out(out_dir)
    split = get_split(protocol, "development")
    ra.check_window("strategy", date.fromisoformat(split["start"]), date.fromisoformat(split["end"]))
    symbols = list(protocol["universe"]["symbols"])
    bars = load_development_bars(data_dir, symbols, split)["bars"]
    end_utc = (pd.Timestamp(split["end"]) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    full5 = {s: resample_rth_5min(load_symbol_bars(data_dir, "1Min", s, None, end_utc)) for s in symbols}
    ind = {s: ra.indicator_arrays(bars[s]) for s in symbols}
    risk_cfg = build_risk_config(production_args())
    eq = float(protocol["execution_defaults"]["initial_equity"])
    stored_reps = pd.read_csv(Path(audit_dir) / "random_control_replicates.csv")
    S: Dict[str, Any] = {"diagnostic": "RANDOM_CONTROL_0BPS", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "fill_slippage_bps": BPS, "risk_manager_assumed_slippage": "unchanged (5 bps)",
                         "equity_per_isolated_run": eq, "methodology": ra.PREDECLARED, "strategies": {}}
    rows = []
    for name in ra.STRATEGIES:
        fwd = pd.read_csv(Path(audit_dir) / f"raw_signal_forward_{name.lower()}.csv")
        rc = ra.random_control(name, fwd, full5, bars, ind, risk_cfg, n_rep)
        ver = verify_replicates(rc["replicates"], stored_reps, name) if n_rep == ra.N_REPLICATES else {"skipped": True}
        real = rc["real"]
        real_keys = [(s, int(t)) for s, t in zip(real["symbol"], real["t"])]
        keys = sorted(set(real_keys) | set(rc["picked"]))
        res = ra.run_isolated(keys, bars, eq, split["end"], workers, bps=BPS)
        real_stats = ra.isolated_stats([res[k] for k in real_keys])
        per_rep = [ra.isolated_stats([res[k] for k, c in picks.items() for _ in range(c)]) for picks in rc["picks_by_rep"]]
        summ = summarize(name, real_stats, per_rep)
        S["strategies"][name] = {"replicate_verification": ver, "fallback_usage": rc["fallback_usage"],
                                 "real_signals": int(len(real)), "unique_random_bars": rc["unique_random_bars"],
                                 "real_isolated_0bps": real_stats,
                                 "random_entered_closed_median": ra._med([p["entered_closed"] for p in per_rep]),
                                 "comparison": summ}
        for k, v in summ.items():
            rows.append({"strategy": name, "metric": k, **v})
        pd.DataFrame([{"strategy": name, "replicate": r, **p} for r, p in enumerate(per_rep)]).to_csv(
            Path(out_dir) / f"random_isolated_0bps_replicates_{name.lower()}.csv", index=False)
    pd.DataFrame(rows).to_csv(Path(out_dir) / "random_control_0bps_summary.csv", index=False)
    S["interpretation_rule"] = (f"'similar' = real expectancy_r empirical percentile within {SIMILAR_BAND}; "
                                "'materially exceeds' = percentile > 95 (the audit's predeclared 95th-percentile bar)")
    for name, v in S["strategies"].items():
        c = v["comparison"]["expectancy_r"]
        v["verdict"] = ("REAL_MATERIALLY_EXCEEDS_RANDOM" if (c["real_empirical_percentile"] or 0) > SIMILAR_BAND[1] else
                        "REAL_SIMILAR_TO_RANDOM" if c["real_within_random_q05_q95"] else "REAL_BELOW_RANDOM")
        v["random_median_positive_at_0bps"] = bool((c["random_median"] or 0) > 0)
    (Path(out_dir) / "random_control_0bps_summary.json").write_text(to_json(S), encoding="utf-8")
    return S


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.random_control_0bps", description="Random control @ 0 bps (DEVELOPMENT only).")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--data-dir", type=Path, default=ra.IEX_DIR)
    p.add_argument("--audit-dir", type=Path, default=AUDIT_DIR)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args(argv)
    try:
        a.output_dir.mkdir(parents=True, exist_ok=True)
        S = run(load_protocol(a.protocol), a.data_dir, a.audit_dir, a.output_dir, a.workers)
    except (ra.AuditHygieneError, ReplicateMismatch) as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps({n: {"verdict": v["verdict"], "expectancy_r": v["comparison"]["expectancy_r"]}
                      for n, v in S["strategies"].items()}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
