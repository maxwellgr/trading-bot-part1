# src/research_sanity_audit.py
"""
RESEARCH SANITY AUDIT V1 — ventaja de señal vs costos de ejecución vs control aleatorio emparejado vs filtrado
de riesgo vs feed de datos. SOLO DIAGNÓSTICO: no crea H005, no cambia estrategias, RiskManager, gestión,
ejecución ni comportamiento en vivo.

    python -m src.research_sanity_audit --output-dir data/research_v1/research_sanity_audit_v1

Higiene (sin bypass):
- Diagnósticos de estrategia/económicos H001/H003: SOLO DEVELOPMENT 2024-01-02 → 2025-12-31.
- Comparación de feeds IEX vs SIP: SOLO KNOWN/CONTAMINADO 2026-06-01 → 2026-09-23.
- Nunca Validation (2026-01-02 → 2026-05-29) ni Forward (2026-09-24 →).

Decisiones metodológicas fijadas ANTES de calcular resultados (ver PREDECLARED en el resumen):
- Reproducción exacta previa de H001/H003 DEVELOPMENT (byte a byte); si falla, nada es válido.
- Sensibilidad de ejecución: corridas de portafolio COMPLETAS a 0/2.5/5/7.5/10/15 bps (solo cambia el slippage de
  los fills simulados; el slippage que ASUME el RiskManager sigue en 5 bps, así R es comparable).
- Borde de señal cruda: metodología sombra validada (entrada = apertura de la vela siguiente; excursiones por
  CIERRES; R = reconstrucción sin estado de assess_entry); primaria a 0 bps, secundaria a 5 bps.
- Control aleatorio emparejado: mismo símbolo, mismo mes, mismo tramo RTH de 30 min (por hora de decisión),
  mismo quintil de ATR% (rango punto-en-tiempo contra las 1560 velas previas del símbolo); jerarquía de respaldo
  0–4; se excluyen TODAS las velas de señal cruda de la estrategia; 200 réplicas con semillas fijas, con reemplazo.
- Etapas de riesgo: 1 = señales crudas; 2 = pasan RR + liquidez sin estado; 3 = trades aceptados reales.
- Señales en KNOWN: calentamiento DENTRO de KNOWN (no se usan velas de soporte de Validation).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import historical_download as hd
from .backtest_engine import BacktestConfig, BacktestEngine, production_args
from .backtest_entry_quality import cles
from .backtest_report import _profit_factor, summarize, to_json
from .h001_autopsy import TIME_BUCKETS, load_development_bars, time_bucket
from .h001_opportunity_autopsy import SHADOW_SLIPPAGE_BPS, reconstruct_risk_ps, shadow_fixed_horizon
from .historical_audit import integrity, sha256_file
from .historical_data import load_symbol_bars, symbol_path, validate_bars
from .research_h004 import stateless_rr_liquidity
from .research_protocol import DEFAULT_PROTOCOL_PATH, get_split, load_protocol
from .run_paper import build_risk_config
from .shared_management_autopsy import verify_identical
from .spy_context_data import rth_mask
from .strategy import StrategyResult
from .strategy_v2_h001 import ENGINE_LOOKBACK, TrendPullbackH001, resample_rth_5min
from .strategy_v2_h003 import ConsolidationBreakoutH003

NY = "America/New_York"
DEV = (date(2024, 1, 2), date(2025, 12, 31))
VALIDATION = (date(2026, 1, 2), date(2026, 5, 29))
KNOWN = (date(2026, 6, 1), date(2026, 9, 23))
FORWARD_START = date(2026, 9, 24)
SYMBOLS = ("NVDA", "AMD", "PLTR", "HOOD", "MARA", "INTC", "MU", "META")
SCENARIOS = (0.0, 2.5, 5.0, 7.5, 10.0, 15.0)
CANONICAL_BPS = 5.0
N_REPLICATES = 200
BASE_SEED = 20260925                      # semilla de la réplica r = BASE_SEED + r
ATR_RANK_WINDOW = 1560                    # 20 sesiones completas de velas 5Min previas
HORIZON_BARS = {"15m": 3, "30m": 6, "60m": 12}
WEAK_MFE_R = 0.25
IMMEDIATE_FAIL_R = 0.25
STRATEGIES = {"H001": (TrendPullbackH001, Path("data") / "research_v1" / "h001_development"),
              "H003": (ConsolidationBreakoutH003, Path("data") / "research_v1" / "h003_development")}
PROTECTED = [Path("data") / "research_v1" / d for d in
             ("h001_development", "h002_development", "h003_development", "h004_development")]
SIP_DIR = Path("data") / "sip_known"
IEX_DIR = Path("data") / "historical"
# ---- criterios de etiquetas, fijados antes de ver resultados (descriptivos; no son reglas de H005)
MATERIAL_PERCENTILE = 95.0                # "materialmente superior al azar": percentil empírico >= 95
SEPARATION_MATERIAL = 0.06                # |CLES − 0.5| >= 0.06 en alguna variable en la Etapa 1
CONVERGENCE_FRACTION = 0.5                # y en la Etapa 3 esa separación cae a <= la mitad
FEED_STABLE_JACCARD = 0.90
FEED_SENSITIVE_JACCARD = 0.75

PREDECLARED = {
    "random_seeds": f"replicate r, symbol index j (symbols sorted): numpy default_rng(BASE_SEED + 1000*r + j), "
                    f"BASE_SEED={BASE_SEED}, r=0..{N_REPLICATES - 1}",
    "replacement": "one candidate drawn uniformly per matched real signal per replicate; with replacement "
                   "(a candidate can serve several real signals, and recur across replicates)",
    "atr_quintile": f"point-in-time rank of ATR14%/close against the previous {ATR_RANK_WINDOW} 5Min bars of the same "
                    "symbol (ties count half); quintile = min(4, floor(5*rank))",
    "time_bucket": "30-minute buckets anchored 09:30 on the DECISION time (bar start + 5 min)",
    "candidate_exclusion": "all raw BUY signal bars of the same strategy (and symbol) are excluded from its pools",
    "candidate_validity": "decision-eligible (decision in [09:30,16:00)), ATR14 available, 60m horizon inside loaded data",
    "fallback_levels": ["L0 symbol+month+bucket+quintile", "L1 adjacent bucket", "L2 adjacent quintile",
                        "L3 adjacent bucket+adjacent quintile", "L4 symbol+month+bucket any quintile", "unmatched"],
    "materially_superior": f"real median AND mean 60m return R both at empirical percentile >= {MATERIAL_PERCENTILE}",
    "risk_filter_compression": f"some feature |CLES-0.5| >= {SEPARATION_MATERIAL} at Stage 1 AND that feature's "
                               f"|CLES-0.5| at Stage 3 <= {CONVERGENCE_FRACTION} x its Stage 1 value",
    "feed_bands": {"stable": f">= {FEED_STABLE_JACCARD}", "moderate": f"{FEED_SENSITIVE_JACCARD}..<{FEED_STABLE_JACCARD}",
                   "sensitive": f"< {FEED_SENSITIVE_JACCARD}"},
    "known_period_signal_warmup": "raw signals on KNOWN use only KNOWN bars (warm-up inside KNOWN; no Validation support bars)",
    "cost_over_r_stage_metric": "2 x 5 bps x signal close / reconstructed initial risk per share (same formula every stage)",
}


class AuditHygieneError(RuntimeError):
    """Acceso a datos fuera de lo permitido (Validation/Forward o feed fuera de KNOWN). Sin bypass."""


class ReproductionError(RuntimeError):
    pass


# ================================================================ higiene
def _overlaps(a: Tuple[date, date], b: Tuple[date, date]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def check_window(kind: str, start: date, end: date) -> None:
    """kind='strategy' -> dentro de DEVELOPMENT; kind='feed' -> dentro de KNOWN. Nunca Validation/Forward."""
    if start > end:
        raise AuditHygieneError("rango vacío")
    if _overlaps((start, end), VALIDATION) or end >= FORWARD_START:
        raise AuditHygieneError(f"{start}..{end} toca Validation o Forward: prohibido")
    lo, hi = {"strategy": DEV, "feed": KNOWN}[kind]
    if start < lo or end > hi:
        raise AuditHygieneError(f"{kind}: {start}..{end} fuera de {lo}..{hi}")


def _guard_out(out_dir: Path) -> None:
    for p in PROTECTED:
        if Path(out_dir).resolve() == p.resolve() or p.resolve() in Path(out_dir).resolve().parents:
            raise AuditHygieneError(f"{out_dir} es una salida de investigación protegida")


# ================================================================ util
def _med(x) -> Optional[float]:
    s = pd.to_numeric(pd.Series(x), errors="coerce").dropna()
    return float(s.median()) if len(s) else None


def _mean(x) -> Optional[float]:
    s = pd.to_numeric(pd.Series(x), errors="coerce").dropna()
    return float(s.mean()) if len(s) else None


def _rate(x) -> Optional[float]:
    s = pd.Series(x).dropna()
    return float(s.astype(bool).mean() * 100) if len(s) else None


def _q(x, q) -> Optional[float]:
    s = pd.to_numeric(pd.Series(x), errors="coerce").dropna()
    return float(s.quantile(q)) if len(s) else None


def pf_r(rs: Sequence[float]) -> Optional[float]:
    pos = sum(r for r in rs if r > 0)
    neg = -sum(r for r in rs if r < 0)
    return pos / neg if neg > 0 else None


# ================================================================ B/C/D/E. reproducción y sensibilidad de ejecución
def _cfg(protocol: Dict[str, Any], split: Dict[str, Any], bps: float) -> BacktestConfig:
    ex = protocol["execution_defaults"]
    return BacktestConfig(symbols=list(protocol["universe"]["symbols"]), timeframe="5Min", start=split["start"],
                          end=split["end"], initial_equity=float(ex["initial_equity"]), slippage_bps=float(bps),
                          commission=float(ex["commission_per_fill"]), record_evaluations=True, window_hours_limit=False)


def _portfolio_job(job):
    name, bps, protocol, split, bars = job
    cls = STRATEGIES[name][0]
    res = BacktestEngine(_cfg(protocol, split, bps), bars, production_args({"lookback": ENGINE_LOOKBACK}), cls()).run()
    return name, bps, res


def run_portfolios(protocol, split, bars, names=("H001", "H003"), scenarios=SCENARIOS, workers: int = 6):
    """Corridas de portafolio COMPLETAS (nunca un ajuste aritmético sobre trades existentes)."""
    jobs = [(n, b, protocol, split, bars) for n in names for b in scenarios]
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            out = list(pool.map(_portfolio_job, jobs))
    else:
        out = [_portfolio_job(j) for j in jobs]
    return {(n, b): r for n, b, r in out}


def trade_cost_usd(t: Dict[str, Any], bps: float) -> float:
    """Costo de slippage modelado (entrada + todas las piernas de venta), en USD."""
    fill = t["entry_fill_price"]
    slip = sum((l["reference_open"] - l["price"]) * l["qty"] for l in t["legs"])
    return float(slip + (fill - fill / (1 + bps / 10_000)) * t["initial_qty"])


def scenario_metrics(result, bps: float) -> Dict[str, Any]:
    s = summarize(result)
    t, p = s["trades"], s["portfolio"]
    tr = result.trades
    n = len(tr)

    def cnt(reason):
        m = sum(x["exit_reason"] == reason for x in tr)
        return m, (m / n * 100 if n else None)
    st, gb, tp = cnt("stop_hit"), cnt("giveback_close"), cnt("take_profit_hit")
    so = sum((x.get("scale_outs") or 0) > 0 for x in tr)
    cost_r = [trade_cost_usd(x, bps) / (x["risk_per_share_modeled"] * x["initial_qty"]) for x in tr
              if x.get("risk_per_share_modeled") and x.get("initial_qty")]
    return {"slippage_bps": bps, "generated_signals": sum(v["BUY"] for v in result.counters["signals"].values()),
            "completed_trades": t["trades"], "wins": t["wins"], "losses": t["losses"], "win_rate_pct": t["win_rate"] * 100,
            "realized_pnl": p["realized_pnl_closed_trades"], "return_pct": p["return_pct"], "expectancy_usd": t["expectancy"],
            "expectancy_r": t["expectancy_r"], "profit_factor": t["profit_factor"], "total_r": t["total_r"],
            "max_drawdown_pct": p["max_drawdown_pct"], "max_loss_streak": t["max_consecutive_losses"],
            "stop_hit_count": st[0], "stop_hit_rate_pct": st[1], "giveback_count": gb[0], "giveback_rate_pct": gb[1],
            "take_profit_count": tp[0], "take_profit_rate_pct": tp[1], "scale_out_trades": so,
            "scale_out_trade_rate_pct": so / n * 100 if n else None, "median_roundtrip_cost_r": _med(cost_r)}


def path_divergence(canon, other) -> Dict[str, Any]:
    key = lambda t: (t["symbol"], t["entry_signal_timestamp"])   # noqa: E731
    a = {key(t): t for t in canon}
    b = {key(t): t for t in other}
    common = a.keys() & b.keys()

    def agg(keys, src):
        return {"count": len(keys), "pnl": float(sum(src[k]["realized_pnl"] for k in keys)),
                "total_r": float(sum(src[k]["realized_r"] or 0 for k in keys))}
    return {"common": agg(common, b), "common_in_canonical": agg(common, a),
            "scenario_only": agg(b.keys() - a.keys(), b), "canonical_5bps_only": agg(a.keys() - b.keys(), a)}


def break_even(rows: List[Dict[str, Any]], key: str, level: float) -> Dict[str, Any]:
    """Interpolación lineal DESCRIPTIVA entre escenarios congelados; no es un fill alcanzable."""
    pts = sorted((r["slippage_bps"], r[key]) for r in rows if r[key] is not None)
    if not pts:
        return {"bps": None, "note": "no data"}
    if pts[0][1] <= level:
        return {"bps": None, "note": f"{key} <= {level} already at {pts[0][0]} bps (never above break-even in 0–15 bps)"}
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if y0 > level >= y1:
            return {"bps": x0 + (y0 - level) * (x1 - x0) / (y0 - y1), "note": "linear interpolation between frozen scenarios"}
    return {"bps": None, "note": f"{key} stays > {level} through {pts[-1][0]} bps"}


# ================================================================ F. borde de señal cruda (sombra, solo lectura)
def indicator_arrays(df: pd.DataFrame) -> Dict[str, Any]:
    ny = df.index.tz_convert(NY)
    return {"o": df["open"].to_numpy(float), "h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
            "c": df["close"].to_numpy(float), "v": df["volume"].to_numpy(float), "date": np.asarray(ny.date),
            "ts": np.asarray([t.isoformat() for t in df.index]), "pos": {t.isoformat(): k for k, t in enumerate(df.index)},
            "dec_min": np.asarray((ny.hour * 60 + ny.minute + 5)), "month": np.asarray([f"{d.year}-{d.month:02d}" for d in ny])}


def forward_row(a: Dict[str, Any], t: int, risk_cfg) -> Dict[str, Any]:
    """Métricas de horizonte fijo por CIERRES a 0 bps (primaria) y 5 bps (secundaria). Solo velas <= t para R."""
    rps = reconstruct_risk_ps(float(a["c"][t]), a["h"], a["l"], a["c"], t, risk_cfg)
    s0 = shadow_fixed_horizon(a, t, rps, slippage_bps=0.0)
    s5 = shadow_fixed_horizon(a, t, rps, slippage_bps=CANONICAL_BPS)
    row = {"t": t, "risk_ps": rps, "risk_pct": rps / float(a["c"][t]) * 100, "valid": bool(s0["shadow_valid"]),
           "entry_next_session": s0.get("shadow_entry_next_session")}
    for h in ("15m", "30m", "60m", "eos"):
        row[f"ret_r_{h}"] = s0.get(f"close_r_{h}")
    for h in ("30m", "60m"):
        row[f"mfe_r_{h}"] = s0.get(f"mfe_r_{h}")
        row[f"mae_r_{h}"] = s0.get(f"mae_r_{h}")
        row[f"crosses_session_{h}"] = s0.get(f"crosses_session_{h}")
    for lab, col in (("p025", "hit_p025_60m"), ("p050", "hit_p050_60m"), ("p100", "hit_p100_60m"),
                     ("m050", "hit_m050_60m"), ("m100", "hit_m100_60m")):
        row[f"reached_{lab}_60m"] = s0.get(col)
    row["ret_r_60m_5bps"] = s5.get("close_r_60m")
    row["mfe_r_60m_5bps"] = s5.get("mfe_r_60m")
    row["mae_r_60m_5bps"] = s5.get("mae_r_60m")
    row["has_60m"] = row["mfe_r_60m"] is not None
    return {k: (v.item() if hasattr(v, "item") else v) for k, v in row.items()}


def raw_signals(result) -> List[Tuple[str, str]]:
    return [(e["symbol"], e["bar_timestamp"]) for e in result.evaluations if e["signal"] == "BUY"]


def raw_forward(signals, ind, risk_cfg) -> pd.DataFrame:
    rows = []
    for sym, ts in signals:
        a = ind[sym]
        t = a["pos"][ts]
        r = forward_row(a, t, risk_cfg)
        rows.append({"symbol": sym, "bar_timestamp": ts, "close": float(a["c"][t]), **r})
    return pd.DataFrame(rows)


def forward_summary(df: pd.DataFrame) -> Dict[str, Any]:
    v = df[df["valid"] & df["has_60m"]] if len(df) else df
    out = {"signals": int(len(df)), "valid_with_60m": int(len(v))}
    for h in ("15m", "30m", "60m", "eos"):
        out[f"median_ret_r_{h}"] = _med(v[f"ret_r_{h}"])
        out[f"mean_ret_r_{h}"] = _mean(v[f"ret_r_{h}"])
    for h in ("30m", "60m"):
        out[f"median_mfe_r_{h}"] = _med(v[f"mfe_r_{h}"])
        out[f"median_mae_r_{h}"] = _med(v[f"mae_r_{h}"])
    for lab in ("p025", "p050", "p100", "m050", "m100"):
        out[f"reached_{lab}_60m_pct"] = _rate(v[f"reached_{lab}_60m"])
    out["weak_forward_excursion_pct"] = _rate(pd.to_numeric(v["mfe_r_60m"]) < WEAK_MFE_R) if len(v) else None
    out["secondary_5bps"] = {"median_ret_r_60m": _med(v["ret_r_60m_5bps"]), "mean_ret_r_60m": _mean(v["ret_r_60m_5bps"]),
                             "median_mfe_r_60m": _med(v["mfe_r_60m_5bps"])}
    return out


# ================================================================ G–J. control aleatorio emparejado
def atr_pct_series(df: pd.DataFrame) -> np.ndarray:
    """ATR14 como RiskManager._atr (media de 14 TR con cierre previo) / close, en %. NaN si no hay 15 velas."""
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    atr = pd.Series(tr).rolling(14).mean().to_numpy().copy()
    atr[:14] = np.nan
    return atr / c * 100


def atr_quintile(x: np.ndarray, window: Optional[int] = None) -> np.ndarray:
    """Quintil punto-en-tiempo: rango de x[t] contra x[t-window..t-1] (sin futuro). -1 si no hay ventana completa."""
    window = window or ATR_RANK_WINDOW
    n = len(x)
    q = np.full(n, -1, dtype=np.int64)
    for s in range(window, n, 4096):
        e = min(n, s + 4096)
        W = np.lib.stride_tricks.sliding_window_view(x[s - window:e - 1], window)   # fila k: x[s+k-window .. s+k-1]
        cur = x[s:e][:, None]
        valid = ~np.isnan(W).any(axis=1) & ~np.isnan(cur[:, 0])
        rank = ((W < cur).sum(axis=1) + 0.5 * (W == cur).sum(axis=1)) / window
        qq = np.minimum(4, np.floor(rank * 5)).astype(np.int64)
        q[s:e] = np.where(valid, qq, -1)
    return q


def candidate_table(full5: pd.DataFrame, dev_bars: pd.DataFrame, excluded: set, risk_cfg) -> pd.DataFrame:
    """Velas candidatas (solo DEVELOPMENT, elegibles para decisión) con sus rasgos de emparejamiento."""
    atrp = atr_pct_series(full5)
    q = pd.Series(atr_quintile(atrp), index=full5.index)
    idx = dev_bars.index
    ny = idx.tz_convert(NY)
    dec = ny + pd.Timedelta(minutes=5)
    dec_min = np.asarray(dec.hour * 60 + dec.minute)
    dates = np.asarray(ny.date)
    in_dev = np.array([DEV[0] <= d <= DEV[1] for d in dates])
    ok = in_dev & (dec_min >= 570) & (dec_min < 960) & (np.asarray(ny.weekday) < 5)
    n = len(idx)
    t = np.arange(n)
    ok &= (t >= 15) & (t + 12 < n)
    iso = np.asarray([x.isoformat() for x in idx])
    ok &= ~np.isin(iso, list(excluded))
    return pd.DataFrame({"t": t[ok], "bar_timestamp": iso[ok], "month": [f"{d.year}-{d.month:02d}" for d in dates[ok]],
                         "bucket": (dec_min[ok] - 570) // 30, "quintile": q.reindex(idx).to_numpy()[ok]})


def signal_features(full5: pd.DataFrame, dev_bars: pd.DataFrame, ts: Sequence[str]) -> pd.DataFrame:
    atrp = atr_pct_series(full5)
    q = pd.Series(atr_quintile(atrp), index=full5.index)
    a = pd.Series(atrp, index=full5.index)
    idx = pd.DatetimeIndex([pd.Timestamp(x) for x in ts])
    ny = idx.tz_convert(NY)
    dec = ny + pd.Timedelta(minutes=5)
    return pd.DataFrame({"bar_timestamp": list(ts), "month": [f"{d.year}-{d.month:02d}" for d in ny],
                         "bucket": np.asarray((dec.hour * 60 + dec.minute - 570) // 30),
                         "quintile": q.reindex(idx).to_numpy(), "atr_pct": a.reindex(idx).to_numpy()})


def build_pools(sig: pd.DataFrame, cand: pd.DataFrame) -> Tuple[List[np.ndarray], List[int]]:
    """Jerarquía de respaldo fija (L0..L4); devuelve la lista de posiciones t candidatas y el nivel por señal."""
    groups = {k: g["t"].to_numpy() for k, g in cand.groupby(["month", "bucket", "quintile"])}
    by_mb = {k: g["t"].to_numpy() for k, g in cand.groupby(["month", "bucket"])}

    def get(m, b, q):
        return groups.get((m, b, q), np.empty(0, dtype=np.int64))
    pools, levels = [], []
    for m, b, q in zip(sig["month"], sig["bucket"], sig["quintile"]):
        b, q = int(b), int(q)
        tries = [
            [get(m, b, q)] if q >= 0 else [],
            [get(m, b - 1, q), get(m, b + 1, q)] if q >= 0 else [],
            [get(m, b, q - 1), get(m, b, q + 1)] if q >= 0 else [],
            [get(m, b + db, q + dq) for db in (-1, 1) for dq in (-1, 1)] if q >= 0 else [],
            [by_mb.get((m, b), np.empty(0, dtype=np.int64))],
        ]
        chosen, lvl = np.empty(0, dtype=np.int64), -1
        for k, parts in enumerate(tries):
            arr = np.unique(np.concatenate(parts)) if parts else np.empty(0, dtype=np.int64)
            if len(arr):
                chosen, lvl = arr, k
                break
        pools.append(chosen)
        levels.append(lvl)
    return pools, levels


def draw_replicate(pools: Sequence[np.ndarray], r: int) -> np.ndarray:
    """Réplica r: una candidata uniforme por señal emparejada (-1 si no hay pool). Reproducible con BASE_SEED + r."""
    rng = np.random.default_rng(BASE_SEED + r)
    u = rng.random(len(pools))
    out = np.full(len(pools), -1, dtype=np.int64)
    for i, p in enumerate(pools):
        if len(p):
            out[i] = p[min(len(p) - 1, int(u[i] * len(p)))]
    return out


STAT_KEYS = ("median_ret_r_30m", "median_ret_r_60m", "mean_ret_r_60m", "median_mfe_r_60m", "median_mae_r_60m",
             "reached_p050_60m_pct", "reached_p100_60m_pct", "reached_m100_60m_pct", "weak_forward_excursion_pct")


def sample_stats(df: pd.DataFrame) -> Dict[str, Optional[float]]:
    v = df[df["valid"] & df["has_60m"]]
    return {"n": int(len(v)), "median_ret_r_30m": _med(v["ret_r_30m"]), "median_ret_r_60m": _med(v["ret_r_60m"]),
            "mean_ret_r_60m": _mean(v["ret_r_60m"]), "median_mfe_r_60m": _med(v["mfe_r_60m"]),
            "median_mae_r_60m": _med(v["mae_r_60m"]), "reached_p050_60m_pct": _rate(v["reached_p050_60m"]),
            "reached_p100_60m_pct": _rate(v["reached_p100_60m"]), "reached_m100_60m_pct": _rate(v["reached_m100_60m"]),
            "weak_forward_excursion_pct": _rate(pd.to_numeric(v["mfe_r_60m"]) < WEAK_MFE_R) if len(v) else None}


def percentile_of(real: float, rand: Sequence[float]) -> Optional[float]:
    x = np.asarray([r for r in rand if r is not None], float)
    if real is None or not len(x):
        return None
    return float(((x < real).sum() + 0.5 * (x == real).sum()) / len(x) * 100)


def random_control(name: str, sig_df: pd.DataFrame, full5: Dict[str, pd.DataFrame], dev_bars: Dict[str, pd.DataFrame],
                   ind: Dict[str, Any], risk_cfg, n_rep: int = N_REPLICATES) -> Dict[str, Any]:
    """Controles emparejados por símbolo; métricas 0 bps idénticas a las señales reales (misma función)."""
    real = sig_df[sig_df["valid"] & sig_df["has_60m"]].copy()
    picks_by_rep = [dict() for _ in range(n_rep)]
    level_counts: Dict[int, int] = {}
    cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
    order: List[Tuple[str, int]] = []
    per_sym_pools = {}
    for sym, g in real.groupby("symbol", sort=True):
        excluded = set(sig_df.loc[sig_df["symbol"] == sym, "bar_timestamp"])
        cand = candidate_table(full5[sym], dev_bars[sym], excluded, risk_cfg)
        feats = signal_features(full5[sym], dev_bars[sym], list(g["bar_timestamp"]))
        pools, levels = build_pools(feats, cand)
        per_sym_pools[sym] = (list(g.index), pools)
        for lv in levels:
            level_counts[lv] = level_counts.get(lv, 0) + 1
        real.loc[g.index, "atr_quintile"] = feats["quintile"].to_numpy()
        real.loc[g.index, "match_level"] = levels
    reps = []
    for r in range(n_rep):
        rows = []
        for sym in sorted(per_sym_pools):
            _, pools = per_sym_pools[sym]
            for t in draw_replicate(pools, r * 1000 + sorted(per_sym_pools).index(sym)):
                if t < 0:
                    continue
                key = (sym, int(t))
                if key not in cache:
                    cache[key] = forward_row(ind[sym], int(t), risk_cfg)
                    order.append(key)
                rows.append(cache[key])
                picks_by_rep[r][key] = picks_by_rep[r].get(key, 0) + 1
        st = sample_stats(pd.DataFrame(rows))
        reps.append({"strategy": name, "replicate": r, "seed_base": BASE_SEED, **st})
    real_stats = sample_stats(real)
    rep_df = pd.DataFrame(reps)
    summary = []
    for k in STAT_KEYS:
        vals = rep_df[k].tolist()
        summary.append({"strategy": name, "metric": k, "real": real_stats[k], "random_median": _med(vals),
                        "random_q05": _q(vals, .05), "random_q25": _q(vals, .25), "random_q75": _q(vals, .75),
                        "random_q95": _q(vals, .95), "real_empirical_percentile": percentile_of(real_stats[k], vals)})
    lv_names = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4", -1: "unmatched"}
    return {"summary": pd.DataFrame(summary), "replicates": rep_df, "real_stats": real_stats,
            "fallback_usage": {lv_names[k]: v for k, v in sorted(level_counts.items())},
            "matched_real_signals": int(sum(v for k, v in level_counts.items() if k >= 0)),
            "unique_random_bars": len(cache), "picked": order, "picks_by_rep": picks_by_rep, "real": real,
            "seed_note": "per replicate r and symbol index j (sorted symbols): default_rng(BASE_SEED + r*1000 + j)"}


# ================================================================ K. control aislado (motor real, trade único)
class _OneShot:
    min_bars = 1

    def __init__(self, when):
        self.when = when

    def evaluate(self, df):
        return StrategyResult("BUY" if df.index[-1] == self.when else None, "shadow")


def _isolated_one(sym: str, df: pd.DataFrame, t: int, equity: float, end: str, bps: float = SHADOW_SLIPPAGE_BPS):
    """Igual que h001_opportunity_autopsy.isolated_shadow (validado 1679/1679), devolviendo también salida/escalas."""
    signal_ts = df.index[t]
    start = str(signal_ts.tz_convert(NY).date())
    for post in (80, 1200):
        lo, hi = max(0, t - 40), min(len(df), t + 1 + post)
        cfg = BacktestConfig(symbols=[sym], timeframe="5Min", start=start, end=end, initial_equity=equity,
                             slippage_bps=bps, commission=0.0, window_hours_limit=False, entry_diagnostics=False)
        r = BacktestEngine(cfg, {sym: df.iloc[lo:hi]}, production_args({"lookback": ENGINE_LOOKBACK}), _OneShot(signal_ts)).run()
        if r.trades or not r.open_positions or hi == len(df):
            break
    if r.trades:
        tr = r.trades[0]
        return {"status": "closed", "exit_reason": tr["exit_reason"], "realized_r": tr["realized_r"],
                "realized_pnl": tr["realized_pnl"], "mfe_r": tr["mfe_r"], "scale_outs": tr.get("scale_outs") or 0}
    return {"status": "open_at_end" if r.open_positions else "not_entered"}


def _isolated_chunk(job):
    sym, df, ts, equity, end, bps = job
    return [(sym, int(t), _isolated_one(sym, df, int(t), equity, end, bps)) for t in ts]


def run_isolated(keys: Sequence[Tuple[str, int]], dev_bars, equity: float, end: str, workers: int = 6,
                 bps: float = SHADOW_SLIPPAGE_BPS):
    by = {}
    for s, t in keys:
        by.setdefault(s, []).append(t)
    jobs = []
    for s, ts in sorted(by.items()):
        ts = sorted(set(ts))
        for k in range(0, len(ts), 2000):
            jobs.append((s, dev_bars[s], ts[k:k + 2000], equity, end, bps))
    out = {}
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for chunk in pool.map(_isolated_chunk, jobs):
                out.update({(s, t): r for s, t, r in chunk})
    else:
        for j in jobs:
            out.update({(s, t): r for s, t, r in _isolated_chunk(j)})
    return out


def isolated_stats(rs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [r for r in rs if r["status"] == "closed"]
    n = len(closed)
    R = [r["realized_r"] for r in closed]

    def rate(f):
        return sum(1 for r in closed if f(r)) / n * 100 if n else None
    return {"attempts": len(rs), "entered_closed": n, "not_entered": sum(r["status"] == "not_entered" for r in rs),
            "open_at_end": sum(r["status"] == "open_at_end" for r in rs),
            "expectancy_r": statistics.fmean(R) if R else None, "pf_r": pf_r(R),
            "win_rate_pct": rate(lambda r: r["realized_pnl"] > 0),
            "immediate_failure_pct": rate(lambda r: r["mfe_r"] < IMMEDIATE_FAIL_R and r["realized_r"] < 0),
            "stop_hit_pct": rate(lambda r: r["exit_reason"] == "stop_hit"),
            "giveback_pct": rate(lambda r: r["exit_reason"] == "giveback_close"),
            "take_profit_pct": rate(lambda r: r["exit_reason"] == "take_profit_hit"),
            "scale_out_pct": rate(lambda r: r["scale_outs"] > 0)}


# ================================================================ L–N. etapas de riesgo
def stage_table(sig: pd.DataFrame, accepted: set, ind, risk_cfg) -> pd.DataFrame:
    rows = []
    for _, r in sig.iterrows():
        a = ind[r["symbol"]]
        t = int(r["t"])
        st = stateless_rr_liquidity(risk_cfg, a["h"], a["l"], a["c"], a["v"], t)
        d = pd.Timestamp(r["bar_timestamp"]).tz_convert(NY) + pd.Timedelta(minutes=5)
        rows.append({"stage2_stateless_pass": st["stateless_eligible"], "stateless_rr_ok": st["stateless_rr_ok"],
                     "stateless_liquidity_ok": st["stateless_liquidity_ok"],
                     "stage3_accepted": (r["symbol"], r["bar_timestamp"]) in accepted,
                     "time_bucket": time_bucket(d.hour * 60 + d.minute) or "other",
                     "cost_r": 2 * 0.0005 * r["close"] / r["risk_ps"]})
    return pd.concat([sig.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def stage_summary(df: pd.DataFrame) -> Dict[str, Any]:
    v = df[df["valid"] & df["has_60m"]]
    n = len(df)
    return {"count": n, "valid_with_60m": int(len(v)),
            "symbol_pct": {k: round(c / n * 100, 2) for k, c in df["symbol"].value_counts().sort_index().items()} if n else {},
            "time_of_day_pct": {k: round(c / n * 100, 2) for k, c in df["time_bucket"].value_counts().sort_index().items()} if n else {},
            "median_atr_pct": _med(df["atr_pct"]), "median_initial_stop_pct": _med(df["risk_pct"]),
            "median_cost_r": _med(df["cost_r"]), "median_mfe_r_60m": _med(v["mfe_r_60m"]),
            "median_mae_r_60m": _med(v["mae_r_60m"]), "median_ret_r_60m": _med(v["ret_r_60m"]),
            "reached_p050_60m_pct": _rate(v["reached_p050_60m"]), "reached_p100_60m_pct": _rate(v["reached_p100_60m"]),
            "reached_m100_60m_pct": _rate(v["reached_m100_60m"]),
            "weak_forward_excursion_pct": _rate(pd.to_numeric(v["mfe_r_60m"]) < WEAK_MFE_R) if len(v) else None}


STAGES = {"stage1_raw": lambda d: d, "stage2_stateless": lambda d: d[d["stage2_stateless_pass"]],
          "stage3_accepted": lambda d: d[d["stage3_accepted"]]}
COMPARE_FEATURES = ("atr_pct", "risk_pct", "mfe_r_60m", "mae_r_60m", "ret_r_60m", "cost_r")


def stage_comparison(d1: pd.DataFrame, d3: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for st, f in STAGES.items():
        a, b = f(d1), f(d3)
        for feat in COMPARE_FEATURES:
            va = a[a["valid"] & a["has_60m"]][feat] if feat in ("mfe_r_60m", "mae_r_60m", "ret_r_60m") else a[feat]
            vb = b[b["valid"] & b["has_60m"]][feat] if feat in ("mfe_r_60m", "mae_r_60m", "ret_r_60m") else b[feat]
            c = cles(va, vb)
            rows.append({"stage": st, "feature": feat, "n_h001": int(len(va)), "n_h003": int(len(vb)),
                         "median_h001": _med(va), "median_h003": _med(vb),
                         "median_diff_h003_minus_h001": (None if _med(va) is None or _med(vb) is None else _med(vb) - _med(va)),
                         "cles_h003_gt_h001": c, "separation_abs_cles_minus_half": None if c is None else abs(c - 0.5)})
    return pd.DataFrame(rows)


def compression_verdict(cmp: pd.DataFrame) -> Dict[str, Any]:
    s1 = cmp[cmp["stage"] == "stage1_raw"].set_index("feature")["separation_abs_cles_minus_half"]
    s2 = cmp[cmp["stage"] == "stage2_stateless"].set_index("feature")["separation_abs_cles_minus_half"]
    s3 = cmp[cmp["stage"] == "stage3_accepted"].set_index("feature")["separation_abs_cles_minus_half"]
    feats = [f for f in COMPARE_FEATURES if s1.get(f) is not None and s1[f] >= SEPARATION_MATERIAL
             and s3.get(f) is not None and s3[f] <= CONVERGENCE_FRACTION * s1[f]]
    return {"max_separation_stage1": float(s1.max()), "max_separation_stage2": float(s2.max()),
            "max_separation_stage3": float(s3.max()), "mean_separation_stage1": float(s1.mean()),
            "mean_separation_stage2": float(s2.mean()), "mean_separation_stage3": float(s3.mean()),
            "features_materially_separated_stage1": [f for f in COMPARE_FEATURES if s1.get(f, 0) >= SEPARATION_MATERIAL],
            "features_converging": feats, "risk_filter_compression": bool(feats)}


def filter_value(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for lab, sub in (("stateless_rejected", df[~df["stage2_stateless_pass"]]), ("stateless_passed", df[df["stage2_stateless_pass"]]),
                     ("rr_rejected", df[~df["stateless_rr_ok"]]), ("liquidity_rejected", df[~df["stateless_liquidity_ok"]])):
        rows.append({"group": lab, **{k: v for k, v in stage_summary(sub).items() if not isinstance(v, dict)}})
    rej, pas = df[~df["stage2_stateless_pass"]], df[df["stage2_stateless_pass"]]
    rv, pv = rej[rej["valid"] & rej["has_60m"]], pas[pas["valid"] & pas["has_60m"]]
    rows.append({"group": "cles_passed_gt_rejected", **{f"cles_{f}": cles(rv[f] if f in ("mfe_r_60m", "mae_r_60m", "ret_r_60m") else rej[f],
                                                                        pv[f] if f in ("mfe_r_60m", "mae_r_60m", "ret_r_60m") else pas[f])
                                                        for f in COMPARE_FEATURES}})
    return pd.DataFrame(rows)


# ================================================================ O–T. feeds (solo KNOWN)
def fetch_feed_bars(symbol: str, feed: str, start: date, end: date, get, data_url: str, headers: Dict[str, str],
                    sleep=time.sleep) -> List[dict]:
    """GET /v2/stocks/{sym}/bars 1Min adjustment=raw con feed explícito. Solo KNOWN (guardia sin bypass)."""
    check_window("feed", start, end)
    s_utc, e_utc = hd.date_range_utc(start.isoformat(), end.isoformat())
    e_utc = e_utc - pd.Timedelta(seconds=1)
    params = {"timeframe": "1Min", "start": s_utc.isoformat().replace("+00:00", "Z"),
              "end": e_utc.isoformat().replace("+00:00", "Z"), "limit": 10000, "feed": feed, "adjustment": "raw", "sort": "asc"}
    out: List[dict] = []
    while True:
        r = hd._get_with_retry(get, f"{data_url}/stocks/{symbol}/bars", headers, params, sleep)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            return out
        params = dict(params, page_token=token)


def download_sip(sip_dir: Path, get=None, data_url=None, headers=None, symbols=SYMBOLS) -> Dict[str, Any]:
    """SIP 1Min raw, solo KNOWN, caché separada. Si la cuenta no lo permite -> UNAVAILABLE (sin cambiar nada)."""
    if Path(sip_dir).resolve() == IEX_DIR.resolve():
        raise AuditHygieneError("la caché SIP no puede ser la caché IEX")
    if get is None:
        import requests
        from .config import settings
        get, data_url = requests.get, settings.alpaca_data_url
        headers = {"APCA-API-KEY-ID": settings.alpaca_api_key or "", "APCA-API-SECRET-KEY": settings.alpaca_api_secret or ""}
    man: Dict[str, Any] = {"endpoint": f"GET {data_url}/stocks/{{symbol}}/bars", "feed": "sip", "adjustment": "raw",
                           "timeframe": "1Min", "requested_range_ny": [KNOWN[0].isoformat(), KNOWN[1].isoformat()],
                           "files": {}, "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for sym in symbols:
        path = symbol_path(sip_dir, "1Min", sym)
        if not path.exists():
            try:
                bars = fetch_feed_bars(sym, "sip", KNOWN[0], KNOWN[1], get, data_url, headers)
            except Exception as e:    # permiso/suscripción: no se compra, cambia ni evade nada
                status = getattr(getattr(e, "response", None), "status_code", None)
                man["status"] = "UNAVAILABLE"
                man["reason"] = f"{sym}: {type(e).__name__} status={status}: {str(e)[:200]}"
                return man
            df = hd.bars_to_frame(sym, bars)
            if df.empty:
                man["status"] = "UNAVAILABLE"
                man["reason"] = f"{sym}: SIP returned no bars"
                return man
            ny = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").dt.tz_convert(NY).dt.date
            if ny.min() < KNOWN[0] or ny.max() > KNOWN[1]:
                raise AuditHygieneError(f"{sym}: respuesta SIP fuera de KNOWN")
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False)
        raw = pd.read_csv(path, dtype={"timestamp": str, "symbol": str})
        ts = pd.to_datetime(raw["timestamp"], utc=True, format="ISO8601")
        integ = integrity(raw)
        man["files"][sym] = {"path": str(path).replace("\\", "/"), "rows": int(len(raw)), "sha256": sha256_file(path),
                             "first_timestamp": ts.min().isoformat(), "last_timestamp": ts.max().isoformat(), **integ,
                             "integrity_pass": bool(integ["duplicate_timestamps"] == 0 and integ["non_monotonic_timestamps"] == 0
                                                    and integ["impossible_ohlc_bars"] == 0 and integ["non_positive_prices"] == 0
                                                    and integ["negative_volume"] == 0 and integ["null_or_nan_total"] == 0)}
    man["status"] = "AVAILABLE"
    man["all_integrity_pass"] = all(v["integrity_pass"] for v in man["files"].values())
    return man


def load_known(data_dir: Path, sym: str) -> pd.DataFrame:
    check_window("feed", *KNOWN)
    s = pd.Timestamp(KNOWN[0]).tz_localize(NY).tz_convert("UTC")
    e = (pd.Timestamp(KNOWN[1]) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    df = load_symbol_bars(data_dir, "1Min", sym, s, e)
    d = df.index.tz_convert(NY).date
    if len(df) and (min(d) < KNOWN[0] or max(d) > KNOWN[1]):
        raise AuditHygieneError("datos fuera de KNOWN")
    return df


def _bps(a: np.ndarray, b: np.ndarray, ref: np.ndarray) -> np.ndarray:
    return np.abs(a - b) / ref * 10_000


def compare_frames(iex: pd.DataFrame, sip: pd.DataFrame, rth_only: bool) -> Dict[str, Any]:
    if rth_only:
        iex, sip = iex[rth_mask(iex.index)], sip[rth_mask(sip.index)]
    common = iex.index.intersection(sip.index)
    a, b = iex.loc[common], sip.loc[common]
    out = {"iex_count": int(len(iex)), "sip_count": int(len(sip)), "overlap": int(len(common)),
           "overlap_pct_of_iex": len(common) / len(iex) * 100 if len(iex) else None,
           "overlap_pct_of_sip": len(common) / len(sip) * 100 if len(sip) else None,
           "iex_only": int(len(iex.index.difference(sip.index))), "sip_only": int(len(sip.index.difference(iex.index)))}
    for k in ("open", "high", "low", "close"):
        d = _bps(a[k].to_numpy(float), b[k].to_numpy(float), a[k].to_numpy(float))
        out[f"abs_{k}_diff_bps_median"] = float(np.median(d)) if len(d) else None
        out[f"abs_{k}_diff_bps_p95"] = float(np.quantile(d, .95)) if len(d) else None
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = b["volume"].to_numpy(float) / a["volume"].to_numpy(float)
    ratio = ratio[np.isfinite(ratio)]
    out["sip_iex_volume_ratio_median"] = float(np.median(ratio)) if len(ratio) else None
    out["sip_iex_volume_ratio_p95"] = float(np.quantile(ratio, .95)) if len(ratio) else None
    return out


def generate_raw_signals(df5: pd.DataFrame, strategy_cls, start: date, end: date, lookback: int = ENGINE_LOOKBACK) -> List[str]:
    """Réplica de la decisión del motor sin portafolio: decisión en [09:30,16:00) dentro de [start,end], ventana tail."""
    strat = strategy_cls()
    idx = df5.index
    dec = idx + pd.Timedelta(minutes=5)
    dny = dec.tz_convert(NY)
    sec = dny.hour * 3600 + dny.minute * 60 + dny.second
    ok = (np.asarray(dny.weekday) < 5) & (np.asarray(sec) >= 34200) & (np.asarray(sec) < 57600)
    dd = np.asarray(dny.date)
    ok &= np.array([start <= d <= end for d in dd])
    need = int(strat.min_bars)
    out = []
    for i in np.flatnonzero(ok):
        lo = max(0, i + 1 - lookback)
        if i + 1 - lo < need:
            continue
        if strat.evaluate(df5.iloc[lo:i + 1]).signal == "BUY":
            out.append(idx[i].isoformat())
    return out


def _signal_job(job):
    name, sym, df5, start, end = job
    return name, sym, generate_raw_signals(df5, STRATEGIES[name][0], start, end)


def run_signal_jobs(jobs, workers: int = 6):
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_signal_job, jobs))
    return [_signal_job(j) for j in jobs]


def signal_overlap(iex: Dict[str, List[str]], sip: Dict[str, List[str]]) -> Dict[str, Any]:
    A = {(s, t) for s, ts in iex.items() for t in ts}
    B = {(s, t) for s, ts in sip.items() for t in ts}
    inter, union = A & B, A | B

    def near(x, other):
        s, t = x
        tt = pd.Timestamp(t)
        return any((s, (tt + pd.Timedelta(minutes=5 * k)).isoformat()) in other for k in (-1, 1))
    io, so = A - B, B - A
    j = len(inter) / len(union) if union else None
    band = (None if j is None else "DATA_FEED_STABLE" if j >= FEED_STABLE_JACCARD else
            "MODERATE_FEED_SENSITIVITY" if j >= FEED_SENSITIVE_JACCARD else "DATA_FEED_SENSITIVE")
    return {"iex_signals": len(A), "sip_signals": len(B), "exact_matches": len(inter), "iex_only": len(io),
            "sip_only": len(so), "jaccard": j, "band": band,
            "iex_only_with_sip_signal_within_1_bar": sum(near(x, B) for x in io),
            "sip_only_with_iex_signal_within_1_bar": sum(near(x, A) for x in so)}


# ================================================================ U. costo vs escala del movimiento
def cost_move_scale(trades, fwd: pd.DataFrame, bps: float = CANONICAL_BPS) -> Dict[str, Any]:
    rp = [t["risk_per_share_modeled"] / t["modeled_entry"] * 100 for t in trades]
    cost_pct = [trade_cost_usd(t, bps) / (t["entry_fill_price"] * t["initial_qty"]) * 100 for t in trades]
    cost_r = [trade_cost_usd(t, bps) / (t["risk_per_share_modeled"] * t["initial_qty"]) for t in trades]
    v = fwd[fwd["valid"] & fwd["has_60m"]]
    fill0 = v["close"]      # aproximación de escala: R en % del cierre de la señal
    gross = [(t["realized_pnl"] + trade_cost_usd(t, bps)) / (t["entry_fill_price"] * t["initial_qty"]) * 100 for t in trades]
    win = [t for t, g in zip(trades, gross) if t["realized_pnl"] > 0]
    lose = [t for t, g in zip(trades, gross) if t["realized_pnl"] <= 0]
    return {"median_initial_r_pct": _med(rp), "median_roundtrip_cost_pct": _med(cost_pct), "median_cost_over_r": _med(cost_r),
            "raw_signals_median_mfe_30m_pct": _med(v["mfe_r_30m"] * v["risk_ps"] / fill0 * 100),
            "raw_signals_median_mfe_60m_pct": _med(v["mfe_r_60m"] * v["risk_ps"] / fill0 * 100),
            "winners_median_mfe_pct": _med([t["mfe_pct"] for t in win]),
            "winners_median_gross_realized_move_pct": _med([g for t, g in zip(trades, gross) if t["realized_pnl"] > 0]),
            "losers_median_mae_pct": _med([t["mae_pct"] for t in lose]),
            "losers_median_gross_realized_move_pct": _med([g for t, g in zip(trades, gross) if t["realized_pnl"] <= 0])}


# ================================================================ orquestación
def build(protocol: Dict[str, Any], data_dir: Path, out_dir: Path, stored: Dict[str, Path], sip_dir: Path = SIP_DIR,
          workers: int = 6, n_rep: int = N_REPLICATES, feed: bool = True, scenarios=SCENARIOS,
          sip_fetch: Optional[Dict[str, Any]] = None, known_iex_dir: Optional[Path] = None) -> Dict[str, Any]:
    _guard_out(out_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    split = get_split(protocol, "development")
    check_window("strategy", date.fromisoformat(split["start"]), date.fromisoformat(split["end"]))
    symbols = list(protocol["universe"]["symbols"])
    loaded = load_development_bars(data_dir, symbols, split)
    bars = loaded["bars"]
    end_utc = (pd.Timestamp(split["end"]) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    full5 = {s: resample_rth_5min(load_symbol_bars(data_dir, "1Min", s, None, end_utc)) for s in symbols}
    risk_cfg = build_risk_config(production_args())
    S: Dict[str, Any] = {"audit": "RESEARCH_SANITY_AUDIT_V1", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "predeclared": PREDECLARED, "strategy_scope": [split["start"], split["end"]],
                         "feed_scope": [KNOWN[0].isoformat(), KNOWN[1].isoformat()]}

    # ---- B/C: corridas completas (la canónica 5 bps sirve de reproducción exacta)
    runs = run_portfolios(protocol, split, bars, tuple(stored), scenarios, workers)
    S["reproduction"] = {}
    for n in stored:
        rep = verify_identical(runs[(n, CANONICAL_BPS)], out_dir / f"{n.lower()}_rerun_check", stored[n])
        S["reproduction"][n] = rep
        if not rep["identical"]:
            (out_dir / "research_sanity_summary.json").write_text(to_json(S), encoding="utf-8")
            raise ReproductionError(f"{n} no se reproduce: {rep['files_identical']}")
    S["execution"] = {}
    div_rows = []
    for n in stored:
        rows = [scenario_metrics(runs[(n, b)], b) for b in scenarios]
        pd.DataFrame(rows).to_csv(out_dir / f"execution_sensitivity_{n.lower()}.csv", index=False)
        canon = runs[(n, CANONICAL_BPS)].trades
        for b in scenarios:
            if b != CANONICAL_BPS:
                d = path_divergence(canon, runs[(n, b)].trades)
                div_rows.append({"strategy": n, "slippage_bps": b, **{f"{g}_{k}": v for g, x in d.items() for k, v in x.items()}})
        zero = next(r for r in rows if r["slippage_bps"] == 0.0) if 0.0 in scenarios else None
        can = next(r for r in rows if r["slippage_bps"] == CANONICAL_BPS)
        S["execution"][n] = {"scenarios": rows, "positive_at_0bps": None if zero is None else bool(zero["expectancy_r"] > 0),
                             "break_even_expectancy_r": break_even(rows, "expectancy_r", 0.0),
                             "break_even_profit_factor": break_even(rows, "profit_factor", 1.0),
                             "canonical_expectancy_r_over_0bps": (None if zero is None or not zero["expectancy_r"] else
                                                                  can["expectancy_r"] / zero["expectancy_r"])}
    pd.DataFrame(div_rows).to_csv(out_dir / "execution_path_divergence.csv", index=False)
    S["execution_path_divergence"] = div_rows

    # ---- F: borde crudo
    ind = {s: indicator_arrays(bars[s]) for s in symbols}
    fwd, stages, rc, iso_real = {}, {}, {}, {}
    for n in stored:
        sig = raw_signals(runs[(n, CANONICAL_BPS)])
        df = raw_forward(sig, ind, risk_cfg)
        feats = pd.concat([signal_features(full5[s], bars[s], list(g["bar_timestamp"])).assign(_i=g.index)
                           for s, g in df.groupby("symbol")]).set_index("_i").sort_index()
        df["atr_pct"] = feats["atr_pct"].to_numpy()
        fwd[n] = df
        df.to_csv(out_dir / f"raw_signal_forward_{n.lower()}.csv", index=False)
    S["raw_signal_forward"] = {n: forward_summary(fwd[n]) for n in stored}

    # ---- G–J: aleatorio emparejado
    rep_frames, S["random_control"] = [], {}
    for n in stored:
        rc[n] = random_control(n, fwd[n], full5, bars, ind, risk_cfg, n_rep)
        rc[n]["summary"].to_csv(out_dir / f"random_control_summary_{n.lower()}.csv", index=False)
        rep_frames.append(rc[n]["replicates"])
        S["random_control"][n] = {"real_stats": rc[n]["real_stats"], "fallback_usage": rc[n]["fallback_usage"],
                                  "matched_real_signals": rc[n]["matched_real_signals"],
                                  "unique_random_bars": rc[n]["unique_random_bars"], "seed_note": rc[n]["seed_note"],
                                  "summary": rc[n]["summary"].to_dict(orient="records")}
    pd.concat(rep_frames).to_csv(out_dir / "random_control_replicates.csv", index=False)

    # ---- K: aislado (motor real, un trade, 5 bps, equity inicial fija)
    eq = float(protocol["execution_defaults"]["initial_equity"])
    S["isolated_control"] = {"equity_per_trade_run": eq, "execution_bps": SHADOW_SLIPPAGE_BPS,
                             "method": "same engine/management in an empty single-symbol portfolio per entry "
                                       "(h001_opportunity_autopsy isolated shadow), RiskManager assess_entry applies"}
    for n in stored:
        real = rc[n]["real"]
        real_keys = [(s, int(t)) for s, t in zip(real["symbol"], real["t"])]
        keys = set(real_keys) | set(rc[n]["picked"])
        res = run_isolated(sorted(keys), bars, eq, split["end"], workers)
        rs_real = isolated_stats([res[k] for k in real_keys])
        per_rep = []
        for r, picks in enumerate(rc[n]["picks_by_rep"]):
            lst = [res[k] for k, c in picks.items() for _ in range(c)]
            per_rep.append(isolated_stats(lst))
        keysx = ("expectancy_r", "pf_r", "win_rate_pct", "immediate_failure_pct", "stop_hit_pct", "giveback_pct",
                 "take_profit_pct", "scale_out_pct")
        S["isolated_control"][n] = {"real": rs_real, "random": {
            k: {"median": _med([p[k] for p in per_rep]), "q05": _q([p[k] for p in per_rep], .05),
                "q95": _q([p[k] for p in per_rep], .95),
                "real_empirical_percentile": percentile_of(rs_real[k], [p[k] for p in per_rep])} for k in keysx},
            "random_entered_closed_median": _med([p["entered_closed"] for p in per_rep])}

    # ---- L–N: etapas de riesgo
    for n in stored:
        acc = {(t["symbol"], t["entry_signal_timestamp"]) for t in runs[(n, CANONICAL_BPS)].trades}
        stages[n] = stage_table(fwd[n], acc, ind, risk_cfg)
        rows = [{"stage": st, **{k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in stage_summary(f(stages[n])).items()}}
                for st, f in STAGES.items()]
        pd.DataFrame(rows).to_csv(out_dir / f"risk_stage_{n.lower()}.csv", index=False)
        actual = {(r["symbol"], r["bar_timestamp"]): r["reason_code"] if r["decision"] == "REJECT" else "ACCEPT"
                  for r in runs[(n, CANONICAL_BPS)].risk_evaluations}
        chk = agree = 0
        for _, r in stages[n].iterrows():
            code = actual.get((r["symbol"], r["bar_timestamp"]))
            if code in ("ACCEPT", "RR_BELOW_MINIMUM", "LIQUIDITY_BELOW_MINIMUM", "LEVERAGE_EXCEEDED"):
                chk += 1
                exp = {"RR_BELOW_MINIMUM": r["stateless_liquidity_ok"] and not r["stateless_rr_ok"],
                       "LIQUIDITY_BELOW_MINIMUM": not r["stateless_liquidity_ok"]}.get(code, r["stage2_stateless_pass"])
                agree += bool(exp)
        fv = filter_value(stages[n])
        fv.to_csv(out_dir / f"filter_value_{n.lower()}.csv", index=False)
        S.setdefault("risk_stages", {})[n] = {"stages": {st: stage_summary(f(stages[n])) for st, f in STAGES.items()},
                                              "stateless_vs_actual_risk_manager": {"checked": chk, "agree": agree}}
        S.setdefault("filter_value", {})[n] = fv.to_dict(orient="records")
    if "H001" in stages and "H003" in stages:
        cmp = stage_comparison(stages["H001"], stages["H003"])
        cmp.to_csv(out_dir / "risk_stage_comparison.csv", index=False)
        S["risk_stage_comparison"] = {"rows": cmp.to_dict(orient="records"), "verdict": compression_verdict(cmp)}

    # ---- U
    cms = []
    for n in stored:
        c = cost_move_scale(runs[(n, CANONICAL_BPS)].trades, fwd[n])
        ex = S["execution"][n]
        c["canonical_5bps_expectancy_r"] = next(r["expectancy_r"] for r in ex["scenarios"] if r["slippage_bps"] == CANONICAL_BPS)
        c["zero_bps_expectancy_r"] = next((r["expectancy_r"] for r in ex["scenarios"] if r["slippage_bps"] == 0.0), None)
        cms.append({"strategy": n, **c})
    pd.DataFrame(cms).to_csv(out_dir / "cost_move_scale.csv", index=False)
    S["cost_move_scale"] = cms

    # ---- validación del generador de señales crudas (sin portafolio) contra el motor, en DEVELOPMENT
    gjobs = [(n, s, bars[s], date.fromisoformat(split["start"]), date.fromisoformat(split["end"])) for n in stored for s in symbols]
    gres = run_signal_jobs(gjobs, workers)
    gen = {n: sorted((s, t) for (nn, s, lst) in gres if nn == n for t in lst) for n in stored}
    S["raw_signal_generator_validation"] = {n: {"generator": len(gen[n]), "engine": len(raw_signals(runs[(n, CANONICAL_BPS)])),
                                                "identical": gen[n] == sorted(raw_signals(runs[(n, CANONICAL_BPS)]))}
                                            for n in stored}

    # ---- O–T: feeds (solo KNOWN)
    S["feed"] = feed_section(out_dir, sip_dir, workers, known_iex_dir or data_dir, sip_fetch) if feed else {"status": "SKIPPED"}

    S["labels"] = decide_labels(S)
    (out_dir / "research_sanity_summary.json").write_text(to_json(S), encoding="utf-8")
    return S


def feed_section(out_dir: Path, sip_dir: Path, workers: int, iex_dir: Path, sip_fetch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    man = download_sip(sip_dir, **(sip_fetch or {}))
    (out_dir / "sip_data_manifest.json").write_text(to_json(man), encoding="utf-8")
    if man.get("status") != "AVAILABLE":
        return {"status": "UNAVAILABLE", "manifest": man}
    rows1, rows5, iex5, sip5 = [], [], {}, {}
    for s in man["files"]:
        a = load_known(iex_dir, s)
        b = validate_bars(pd.read_csv(symbol_path(sip_dir, "1Min", s), dtype={"timestamp": str, "symbol": str}), s, "sip")
        rows1.append({"symbol": s, **compare_frames(a, b, rth_only=True)})
        iex5[s], sip5[s] = resample_rth_5min(a), resample_rth_5min(b)
        rows5.append({"symbol": s, **compare_frames(iex5[s], sip5[s], rth_only=False)})
    pd.DataFrame(rows1).to_csv(out_dir / "iex_sip_1min_comparison.csv", index=False)
    pd.DataFrame(rows5).to_csv(out_dir / "iex_sip_5min_comparison.csv", index=False)
    jobs = [(n, s, df5, KNOWN[0], KNOWN[1]) for n in STRATEGIES for feedname, src in (("iex", iex5), ("sip", sip5))
            for s, df5 in sorted(src.items())]
    tags = [(n, feedname, s) for n in STRATEGIES for feedname, src in (("iex", iex5), ("sip", sip5)) for s in sorted(src)]
    res = run_signal_jobs(jobs, workers)
    sigs: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    for (n, f, s), (_, _, lst) in zip(tags, res):
        sigs.setdefault((n, f), {})[s] = lst
    ov = []
    for n in STRATEGIES:
        o = signal_overlap(sigs[(n, "iex")], sigs[(n, "sip")])
        ov.append({"strategy": n, **o})
    pd.DataFrame(ov).to_csv(out_dir / "iex_sip_signal_overlap.csv", index=False)
    return {"status": "AVAILABLE", "manifest": man, "one_min": rows1, "five_min": rows5, "signal_overlap": ov,
            "note": "raw signals only (no portfolio, no P&L); warm-up inside KNOWN"}


def decide_labels(S: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for n in S.get("execution", {}):
        rc = {r["metric"]: r for r in S["random_control"][n]["summary"]}
        sup = all((rc[m]["real_empirical_percentile"] or 0) >= MATERIAL_PERCENTILE for m in ("median_ret_r_60m", "mean_ret_r_60m"))
        pos0 = S["execution"][n]["positive_at_0bps"]
        can = next(r["expectancy_r"] for r in S["execution"][n]["scenarios"] if r["slippage_bps"] == CANONICAL_BPS)
        labels = []
        if not sup and pos0 is False:
            labels.append("SIGNAL_EDGE_ABSENT")
        if (sup or pos0) and can < 0:
            labels.append("SIGNAL_EDGE_TOO_SMALL_FOR_COST")
        out[n] = {"real_materially_superior_to_random": sup, "positive_at_0bps": pos0, "canonical_expectancy_r": can,
                  "labels": labels}
    comp = (S.get("risk_stage_comparison") or {}).get("verdict", {}).get("risk_filter_compression")
    common = ["RISK_FILTER_COMPRESSION"] if comp else []
    feed = S.get("feed", {})
    if feed.get("status") == "AVAILABLE":
        for o in feed["signal_overlap"]:
            if o["band"] == "DATA_FEED_SENSITIVE":
                out[o["strategy"]]["labels"].append("DATA_FEED_SENSITIVE")
            elif o["band"] == "DATA_FEED_STABLE":
                out[o["strategy"]]["labels"].append("DATA_FEED_STABLE")
            out[o["strategy"]]["feed_jaccard"] = o["jaccard"]
            out[o["strategy"]]["feed_band"] = o["band"]
    for n in out:
        if not out[n]["labels"] and not common:
            out[n]["labels"].append("UNRESOLVED")
    return {"per_strategy": out, "cross_strategy": common or ["no RISK_FILTER_COMPRESSION by the predeclared criterion"],
            "note": "research-diagnostic labels only; not H005 rules"}


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.research_sanity_audit", description="Research Sanity Audit V1 (diagnostic only).")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--data-dir", type=Path, default=IEX_DIR)
    p.add_argument("--sip-dir", type=Path, default=SIP_DIR)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args(argv)
    try:
        S = build(load_protocol(a.protocol), a.data_dir, a.output_dir, {n: v[1] for n, v in STRATEGIES.items()},
                  a.sip_dir, a.workers)
    except (AuditHygieneError, ReproductionError) as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(S["labels"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
