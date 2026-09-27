# src/h005_preportfolio.py
"""
STRATEGY_V2_HYPOTHESIS_005 — 30Min relative-strength trend continuation: implementación de investigación
PRE-PORTAFOLIO (Research Protocol V2). Solo investigación; nunca ejecución en vivo.

Spec congelado: research/strategy_v2_hypothesis_005.md (commit 29e564810d3c7b82fa467ccc9dfaa362d071b9ad).
Protocolo:      Research Protocol V2 (commit 565f7aa4b7e72a55780ecfe59ada805f166e4290).

Contenido (todo función pura sobre marcos en memoria; la carga de datos reales está acotada y guardada):
- velas 30Min RTH ancladas a 09:30 (resampler RTH congelado, 30 min);
- señal A∧B∧C por símbolo/sesión (primera calificante evaluable; las siguientes = calificantes suprimidas);
- observación de 60 min (E1 = cubeta que empieza en D, E2 = D+30), R = 2·ATR14(T), fwdR_60 con costo en fills;
- poblaciones emitted / valid / matched / unmatched;
- control aleatorio emparejado (mes, slot de decisión, quintil ATR% de 260 valores válidos previos; L0–L4;
  200 réplicas; semilla 20260925 + 1000·r + j);
- métricas P2/P3, P4-A/B, precheck de costos, mecánica P5 agnóstica de feed, compuertas vía preportfolio_screen.
No calcula nada sobre datos reales al importarse.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

from . import preportfolio_screen as ps
from . import research_sanity_audit as ra
from .historical_audit import EARLY_CLOSE_MIN, EARLY_CLOSES, RTH_CLOSE_MIN, RTH_OPEN_MIN, sha256_file
from .spy_context_data import resample_rth

HYPOTHESIS_ID = "STRATEGY_V2_HYPOTHESIS_005"
FROZEN_SPEC_COMMIT = "29e564810d3c7b82fa467ccc9dfaa362d071b9ad"
PROTOCOL_V2_COMMIT = "565f7aa4b7e72a55780ecfe59ada805f166e4290"
NY = "America/New_York"

# ---- constantes congeladas (spec §23); NO optimizar
TRADABLE = ("NVDA", "AMD", "PLTR", "HOOD", "MARA", "INTC", "MU", "META")
BAR_MINUTES = 30
EARLIEST_DECISION_MIN = 11 * 60                  # 11:00 ET
LATEST_DECISION_BEFORE_CLOSE_MIN = 60            # scheduled close − 60 min
ATR_WINDOW = 14
R_MULTIPLE = 2.0
ATR_RANK_WINDOW = 260
PRIMARY_HORIZON_MIN = 60
CANONICAL_BPS = 5.0
PRECHECK_BPS = (0.0, 2.5, 5.0, 7.5, 10.0)
N_REPLICATES = 200
BASE_SEED = 20260925
WEAK_MFE_R = 0.25
FRICTION_PCT = 2 * CANONICAL_BPS / 100.0         # 0.10 % ida y vuelta
DEV = (date(2024, 1, 2), date(2025, 12, 31))
HISTORY_START = date(2023, 12, 1)
KNOWN = (date(2026, 6, 1), date(2026, 9, 23))
FEEDS = {"PRIMARY_RESEARCH_FEED": "iex", "INTENDED_LIVE_SIGNAL_FEED": "iex", "SECONDARY_ROBUSTNESS_FEED": "sip"}

assert ra.BASE_SEED == BASE_SEED, "la semilla del auditor congelado debe coincidir con BASE_SEED de H005"


class H005Error(RuntimeError):
    pass


# ================================================================ calendario / barras
def scheduled_close_min(d: date) -> int:
    """Cierre PROGRAMADO (lista congelada EARLY_CLOSES), nunca inferido de los datos."""
    return EARLY_CLOSE_MIN if d in EARLY_CLOSES else RTH_CLOSE_MIN


def resample_30min(df1: pd.DataFrame) -> pd.DataFrame:
    """Resampler RTH congelado a 30 min, anclado a 09:30 (cubeta existe con >= 1 vela 1Min; nada fabricado)."""
    return resample_rth(df1, BAR_MINUTES)


def _bar_frame(b30: pd.DataFrame) -> pd.DataFrame:
    """Vista NY: fecha de sesión, minuto de inicio, minuto de decisión D, ns del inicio."""
    ny = b30.index.tz_convert(NY)
    start_min = np.asarray(ny.hour * 60 + ny.minute)
    return pd.DataFrame({"ts_ns": b30.index.as_unit("ns").asi8, "session": np.asarray(ny.date), "start_min": start_min,
                         "decision_min": start_min + BAR_MINUTES, "open": b30["open"].to_numpy(float),
                         "high": b30["high"].to_numpy(float), "low": b30["low"].to_numpy(float),
                         "close": b30["close"].to_numpy(float)})


def decision_in_window(session: date, decision_min: int) -> bool:
    return EARLIEST_DECISION_MIN <= decision_min <= scheduled_close_min(session) - LATEST_DECISION_BEFORE_CLOSE_MIN


# ================================================================ señal (spec §5–§7)
def signal_table(stocks: Dict[str, pd.DataFrame], spy: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """
    Una fila por barra EVALUABLE (símbolo, T) en sesiones [start, end], con A, B, C, qualifies, emitted, suppressed.
    Solo usa, para T, cubetas de la misma sesión con inicio <= inicio(T) (sin mirar después de D).
    """
    sp = _bar_frame(spy)
    spy_close = dict(zip(sp["ts_ns"], sp["close"]))
    spy_open0 = {s: o for s, m, o in zip(sp["session"], sp["start_min"], sp["open"]) if m == RTH_OPEN_MIN}
    rows: List[Dict[str, Any]] = []
    for sym in sorted(stocks):
        bf = _bar_frame(stocks[sym])
        bf["t"] = np.arange(len(bf))
        for sess, g in bf.groupby("session", sort=True):
            if not (start <= sess <= end):
                continue
            first = g[g["start_min"] == RTH_OPEN_MIN]
            if first.empty or sess not in spy_open0:          # sin ancla alternativa
                continue
            o_x, o_spy = float(first["open"].iloc[0]), float(spy_open0[sess])
            prior_max = -math.inf
            emitted = False
            for _, r in g.iterrows():                          # g está ordenado por tiempo
                c_x = float(r["close"])
                if decision_in_window(sess, int(r["decision_min"])) and int(r["ts_ns"]) in spy_close:
                    c_spy = float(spy_close[int(r["ts_ns"])])
                    a = c_x > o_x
                    b = (c_x / o_x - 1.0) - (c_spy / o_spy - 1.0) > 0
                    c = c_x > prior_max
                    q = bool(a and b and c)
                    rows.append({"symbol": sym, "bar_timestamp": pd.Timestamp(int(r["ts_ns"]), tz="UTC").isoformat(),
                                 "t": int(r["t"]), "session": sess, "decision_min": int(r["decision_min"]),
                                 "A": bool(a), "B": bool(b), "C": bool(c), "qualifies": q,
                                 "emitted": bool(q and not emitted), "suppressed": bool(q and emitted)})
                    emitted = emitted or q
                prior_max = max(prior_max, c_x)                # C compara con TODAS las cubetas previas (incl. < 11:00)
    cols = ["symbol", "bar_timestamp", "t", "session", "decision_min", "A", "B", "C", "qualifies", "emitted", "suppressed"]
    return pd.DataFrame(rows, columns=cols)


# ================================================================ R y ATR% (spec §9, §15)
def atr14(b30: pd.DataFrame) -> np.ndarray:
    """ATR14(T) = media de TR_{T−13..T}; TR usa el cierre previo de la serie; NaN si faltan barras T−14..T."""
    h, l, c = (b30[k].to_numpy(float) for k in ("high", "low", "close"))
    n = len(c)
    out = np.full(n, np.nan)
    if n < ATR_WINDOW + 1:
        return out
    pc = c[:-1]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])   # tr[k] corresponde a la barra k+1
    win = np.lib.stride_tricks.sliding_window_view(tr, ATR_WINDOW)                     # win[k] = tr de barras k+1..k+14
    out[ATR_WINDOW:] = win.mean(axis=1)
    return out


def r_per_share(b30: pd.DataFrame) -> np.ndarray:
    r = R_MULTIPLE * atr14(b30)
    r[~np.isfinite(r) | (r <= 0)] = np.nan
    return r


def atr_quintiles(b30: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """ATR% y quintil punto-en-tiempo: rango contra los 260 valores ATR% VÁLIDOS inmediatamente previos; −1 si < 260."""
    atr = atr14(b30)
    c = b30["close"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = atr / c * 100.0
    valid = np.isfinite(pct) & (atr > 0)
    q = np.full(len(pct), -1, dtype=np.int64)
    idx = np.flatnonzero(valid)
    vals = pct[idx]
    if len(vals) > ATR_RANK_WINDOW:
        prev = np.lib.stride_tricks.sliding_window_view(vals[:-1], ATR_RANK_WINDOW)      # prev[k] = vals[k..k+259]
        cur = vals[ATR_RANK_WINDOW:][:, None]
        rank = ((prev < cur).sum(axis=1) + 0.5 * (prev == cur).sum(axis=1)) / ATR_RANK_WINDOW
        q[idx[ATR_RANK_WINDOW:]] = np.minimum(4, np.floor(rank * 5)).astype(np.int64)
    pct[~valid] = np.nan
    return pct, q


# ================================================================ observación (spec §8)
def observation(b30: pd.DataFrame, t: int, R: np.ndarray, pos: Dict[int, int]) -> Dict[str, Any]:
    """E1 = cubeta que empieza exactamente en D; E2 = D + 30 min. Sin sustitución. Inválida si falta E1/E2 o R."""
    ts = b30.index.as_unit("ns").asi8
    d_ns = int(ts[t]) + BAR_MINUTES * 60 * 10**9
    e1 = pos.get(d_ns)
    e2 = pos.get(d_ns + BAR_MINUTES * 60 * 10**9)
    r = R[t]
    reason = None if e1 is not None else "missing_E1"
    if reason is None and e2 is None:
        reason = "missing_E2"
    if reason is None and not (np.isfinite(r) and r > 0):
        reason = "invalid_R"
    if reason:
        return {"valid": False, "invalid_reason": reason}
    o1 = float(b30["open"].iat[e1])
    c1, c2 = float(b30["close"].iat[e1]), float(b30["close"].iat[e2])
    x1, x2 = (c1 - o1) / r, (c2 - o1) / r
    return {"valid": True, "invalid_reason": None, "R": float(r), "open_E1": o1, "close_E1": c1, "close_E2": c2,
            "mfe_60": max(x1, x2), "mae_60": -min(x1, x2)}


def fwd_r(obs: Dict[str, Any], bps: float) -> float:
    """fwdR_60 = (close(E2)(1−c) − open(E1)(1+c)) / R. Salida sintética de horizonte fijo (no un fill ejecutable)."""
    c = bps / 10_000.0
    return (obs["close_E2"] * (1 - c) - obs["open_E1"] * (1 + c)) / obs["R"]


def favorable_move_pct(obs: Dict[str, Any]) -> float:
    return obs["mfe_60"] * obs["R"] / obs["open_E1"] * 100.0


# ================================================================ estadísticas
def sample_stats(obs: Sequence[Dict[str, Any]], bps: float) -> Dict[str, Optional[float]]:
    """P2 (mediana fwdR) y P3 (media fwdR, mediana MFE, mediana MAE) sobre una muestra de observaciones válidas."""
    if not obs:
        return {"n": 0, "median_fwdR_60": None, "mean_fwdR_60": None, "median_mfe_60": None, "median_mae_60": None}
    f = [fwd_r(o, bps) for o in obs]
    return {"n": len(obs), "median_fwdR_60": statistics.median(f), "mean_fwdR_60": statistics.fmean(f),
            "median_mfe_60": statistics.median(o["mfe_60"] for o in obs),
            "median_mae_60": statistics.median(o["mae_60"] for o in obs)}


def cost_row(obs: Sequence[Dict[str, Any]], bps: float) -> Dict[str, Any]:
    f = [fwd_r(o, bps) for o in obs]
    pos, neg = sum(x for x in f if x > 0), -sum(x for x in f if x < 0)
    return {"slippage_bps": bps, "n": len(f), "median_fwdR_60": statistics.median(f) if f else None,
            "mean_fwdR_60": statistics.fmean(f) if f else None, "pf": pos / neg if neg > 0 else None,
            "win_rate_pct": sum(x > 0 for x in f) / len(f) * 100 if f else None}


def cost_precheck(valid_obs: Sequence[Dict[str, Any]], costs: Sequence[float] = PRECHECK_BPS) -> Dict[str, Any]:
    """No-gating: todas las observaciones válidas; R sin cambios; sin stop/trailing/escalas/giveback/portafolio."""
    rows = [cost_row(valid_obs, b) for b in costs]
    return {"rows": rows, "break_even_mean": ra.break_even(rows, "mean_fwdR_60", 0.0),
            "break_even_median": ra.break_even(rows, "median_fwdR_60", 0.0),
            "note": "descriptive; synthetic fixed-horizon exit; not achievable fills; non-gating"}


# ================================================================ poblaciones y control aleatorio
@dataclass
class Universe:
    """Barras 30Min por símbolo (historia desde HISTORY_START, acotada al fin del periodo) + SPY del MISMO feed."""
    stocks: Dict[str, pd.DataFrame]
    spy: pd.DataFrame
    start: date
    end: date
    feed: str = "iex"
    derived: Dict[str, Any] = field(default_factory=dict)

    def prepare(self) -> "Universe":
        for s, b in self.stocks.items():
            R = r_per_share(b)
            pct, q = atr_quintiles(b)
            pos = {int(v): k for k, v in enumerate(b.index.as_unit("ns").asi8)}
            ny = b.index.tz_convert(NY)
            self.derived[s] = {"R": R, "atr_pct": pct, "quintile": q, "pos": pos,
                               "month": np.asarray([f"{d.year}-{d.month:02d}" for d in ny])}
        return self


def build_populations(u: Universe) -> Dict[str, Any]:
    """emitted / valid / candidates (evaluables, R válido, E1, E2, no emitidas; suprimidas incluidas)."""
    table = signal_table(u.stocks, u.spy, u.start, u.end)
    real, cand = [], []
    for _, r in table.iterrows():
        dv = u.derived[r["symbol"]]
        ob = observation(u.stocks[r["symbol"]], int(r["t"]), dv["R"], dv["pos"])
        rec = {"symbol": r["symbol"], "bar_timestamp": r["bar_timestamp"], "t": int(r["t"]), "decision_min": int(r["decision_min"]),
               "slot": (int(r["decision_min"]) - EARLIEST_DECISION_MIN) // BAR_MINUTES,
               "month": dv["month"][int(r["t"])], "quintile": int(dv["quintile"][int(r["t"])]),
               "atr_pct": float(dv["atr_pct"][int(r["t"])]) if np.isfinite(dv["atr_pct"][int(r["t"])]) else None,
               "suppressed": bool(r["suppressed"]), **ob}
        if r["emitted"]:
            real.append(rec)
        elif ob["valid"]:
            cand.append(rec)
    return {"table": table, "emitted": real, "valid": [o for o in real if o["valid"]], "candidates": cand}


def match(pop: Dict[str, Any]) -> Dict[str, Any]:
    """Pools L0–L4 (build_pools congelado, slot entero = (D − 11:00)/30) por símbolo; matched vs unmatched."""
    valid, cand = pop["valid"], pop["candidates"]
    syms = sorted({o["symbol"] for o in valid})                  # j = índice entre símbolos con >= 1 obs válida
    cand_by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for c in cand:
        cand_by_sym.setdefault(c["symbol"], []).append(c)
    matched, unmatched, pools_by_sym, levels = [], [], {}, {}
    for s in syms:
        real = [o for o in valid if o["symbol"] == s]
        cs = cand_by_sym.get(s, [])
        cdf = pd.DataFrame({"t": np.arange(len(cs), dtype=np.int64), "month": [c["month"] for c in cs],
                            "bucket": [c["slot"] for c in cs], "quintile": [c["quintile"] for c in cs]})
        sdf = pd.DataFrame({"month": [o["month"] for o in real], "bucket": [o["slot"] for o in real],
                            "quintile": [o["quintile"] for o in real]})
        pools, lv = ra.build_pools(sdf, cdf) if len(real) else ([], [])
        mp = []
        for o, p, l in zip(real, pools, lv):
            o = dict(o, match_level=l)
            levels[l] = levels.get(l, 0) + 1
            if l >= 0:
                matched.append(o)
                mp.append(p)
            else:
                unmatched.append(o)
        pools_by_sym[s] = {"pools": mp, "candidates": cs, "matched": [o for o in matched if o["symbol"] == s]}
    names = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4", -1: "unmatched"}
    return {"symbols": syms, "matched": matched, "unmatched": unmatched, "by_symbol": pools_by_sym,
            "fallback_usage": {names[k]: v for k, v in sorted(levels.items())}}


def replicates(m: Dict[str, Any], n_rep: int = N_REPLICATES) -> List[List[Dict[str, Any]]]:
    """Réplica r: exactamente una extracción (con reemplazo) por observación de matched_real_population."""
    out = []
    for r in range(n_rep):
        draws: List[Dict[str, Any]] = []
        for j, s in enumerate(m["symbols"]):
            e = m["by_symbol"][s]
            if not e["pools"]:
                continue
            picks = ra.draw_replicate(e["pools"], r * 1000 + j)     # rng = default_rng(20260925 + 1000·r + j)
            draws.extend(e["candidates"][int(k)] for k in picks)
        out.append(draws)
    return out


# ================================================================ compuertas
def random_comparison(matched: Sequence[Dict[str, Any]], reps: Sequence[Sequence[Dict[str, Any]]], bps: float) -> Dict[str, Any]:
    real = sample_stats(matched, bps)
    rep = [sample_stats(d, bps) for d in reps]
    return {"real": real, "replicates": rep}


def gates(pop: Dict[str, Any], m: Dict[str, Any], reps: Sequence[Sequence[Dict[str, Any]]],
          jaccard: Optional[float], integrity: Optional[bool], no_lookahead: Optional[bool],
          reproduction: Optional[bool]) -> Dict[str, Any]:
    """P1–P6 congeladas (spec §11–§18) con preportfolio_screen; P2/P3/P4-B solo sobre matched_real_population."""
    c0 = random_comparison(m["matched"], reps, 0.0)
    c5 = random_comparison(m["matched"], reps, CANONICAL_BPS)

    def col(c, k):
        return [x[k] for x in c["replicates"]]
    g = {"P1": ps.gate_p1(len(pop["valid"])),
         "P2": ps.gate_p2("median_fwdR_60_0bps", c0["real"]["median_fwdR_60"], col(c0, "median_fwdR_60"), True),
         "P3": ps.gate_p3([
             {"name": "mean_fwdR_60", "real": c0["real"]["mean_fwdR_60"], "replicates": col(c0, "mean_fwdR_60"), "higher_is_better": True},
             {"name": "median_mfe_60", "real": c0["real"]["median_mfe_60"], "replicates": col(c0, "median_mfe_60"), "higher_is_better": True},
             {"name": "median_mae_60", "real": c0["real"]["median_mae_60"], "replicates": col(c0, "median_mae_60"), "higher_is_better": False}]),
         "P4": ps.gate_p4(FRICTION_PCT, p4a_median_favorable_move(pop["valid"]), "median_fwdR_60",
                          c5["real"]["median_fwdR_60"], col(c5, "median_fwdR_60"), higher_is_better=True,
                          primary_is_signed_return=True),
         "P5": ps.gate_p5(FEEDS["PRIMARY_RESEARCH_FEED"], FEEDS["INTENDED_LIVE_SIGNAL_FEED"], threshold_sensitive=True,
                          secondary_feed_available=True, jaccard=jaccard),
         "P6": ps.gate_p6(integrity, no_lookahead, reproduction)}
    return {"gates": g, "admission": ps.admission(HYPOTHESIS_ID, g), "comparison_0bps": c0, "comparison_5bps": c5}


def p4a_median_favorable_move(valid: Sequence[Dict[str, Any]]) -> Optional[float]:
    """P4-A: mediana de MFE_60·R/open(E1)·100 entre observaciones VÁLIDAS exitosas (fwdR_60 a 0 bps > 0)."""
    rows = [{"side": "long", "return_0bps": fwd_r(o, 0.0), "favorable_move_pct": favorable_move_pct(o)} for o in valid]
    return ps.median_successful_favorable_move_pct(rows)


# ================================================================ transparencia (spec §19)
def transparency(pop: Dict[str, Any], m: Dict[str, Any], reps: Sequence[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    emitted, valid = pop["emitted"], pop["valid"]
    un = m["unmatched"]
    cand = pop["candidates"]
    sup = [c for c in cand if c["suppressed"]]
    sup_draws = sum(1 for d in reps for c in d if c["suppressed"])
    inv: Dict[str, int] = {}
    for o in emitted:
        if not o["valid"]:
            inv[o["invalid_reason"]] = inv.get(o["invalid_reason"], 0) + 1

    def dist(xs, key):
        out: Dict[str, int] = {}
        for x in xs:
            k = str(x[key])
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))
    q_neg = sum(o["quintile"] == -1 for o in valid)
    atr = [o["atr_pct"] for o in valid if o["atr_pct"] is not None]
    return {"emitted_signal_count": len(emitted), "valid_real_count": len(valid), "matched_real_count": len(m["matched"]),
            "unmatched_real_count": len(un), "matched_fraction": len(m["matched"]) / len(valid) if valid else None,
            "unmatched_by_symbol": dist(un, "symbol"), "unmatched_by_month": dist(un, "month"),
            "unmatched_by_decision_time": dist(un, "decision_min"), "fallback_usage": m["fallback_usage"],
            "quintile_minus1_count": q_neg, "quintile_minus1_fraction": q_neg / len(valid) if valid else None,
            "suppressed_qualifier_count": len(sup), "suppressed_qualifier_pool_share": len(sup) / len(cand) if cand else None,
            "suppressed_qualifier_draw_count": sup_draws, "invalid_observation_counts": inv,
            "symbol_distribution": dist(valid, "symbol"), "decision_time_distribution": dist(valid, "decision_min"),
            "atr_pct_distribution": ({"min": min(atr), "q25": float(np.quantile(atr, .25)), "median": statistics.median(atr),
                                      "q75": float(np.quantile(atr, .75)), "max": max(atr)} if atr else None)}


def descriptive(valid: Sequence[Dict[str, Any]], matched: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """No-gating: métricas completas del conjunto válido y del emparejado, 0 y 5 bps, alcances y excursión débil."""
    def reach(obs):
        n = len(obs)
        if not n:
            return {}
        return {"reach_plus_0_5R_pct": sum(o["mfe_60"] >= 0.5 for o in obs) / n * 100,
                "reach_plus_1R_pct": sum(o["mfe_60"] >= 1.0 for o in obs) / n * 100,
                "reach_minus_1R_pct": sum(o["mae_60"] >= 1.0 for o in obs) / n * 100,
                "weak_forward_excursion_pct": sum(o["mfe_60"] < WEAK_MFE_R for o in obs) / n * 100}
    return {"full_valid": {"0bps": sample_stats(valid, 0.0), "5bps": sample_stats(valid, CANONICAL_BPS), **reach(valid)},
            "matched": {"0bps": sample_stats(matched, 0.0), "5bps": sample_stats(matched, CANONICAL_BPS), **reach(matched)}}


def evaluate(u: Universe, n_rep: int = N_REPLICATES, jaccard: Optional[float] = None, integrity: Optional[bool] = None,
             no_lookahead: Optional[bool] = None, reproduction: Optional[bool] = None) -> Dict[str, Any]:
    """Tubería completa pre-portafolio sobre un Universe en memoria (datos sintéticos en tests)."""
    u.prepare()
    pop = build_populations(u)
    m = match(pop)
    reps = replicates(m, n_rep)
    gt = gates(pop, m, reps, jaccard, integrity, no_lookahead, reproduction)
    return {"hypothesis_id": HYPOTHESIS_ID, "frozen_spec_commit": FROZEN_SPEC_COMMIT, "populations": pop, "matching": m,
            "replicates": reps, "gates": gt, "transparency": transparency(pop, m, reps),
            "cost_precheck": cost_precheck(pop["valid"]), "descriptive": descriptive(pop["valid"], m["matched"])}


# ================================================================ P5 (mecánica agnóstica de feed)
@dataclass
class FeedBundle:
    """Acciones y SPY de UN SOLO feed. Nunca se mezclan feeds."""
    feed: str
    stocks: Dict[str, pd.DataFrame]
    spy: pd.DataFrame

    def validate(self) -> None:
        for name, df in list(self.stocks.items()) + [("SPY", self.spy)]:
            tag = df.attrs.get("feed")
            if tag is not None and tag != self.feed:
                raise H005Error(f"{name}: feed '{tag}' mezclado en el paquete '{self.feed}'")


def emitted_set(b: FeedBundle, start: date, end: date) -> Set[Tuple[str, str]]:
    b.validate()
    t = signal_table(b.stocks, b.spy, start, end)
    return {(s, ts) for s, ts in zip(t.loc[t["emitted"], "symbol"], t.loc[t["emitted"], "bar_timestamp"])}


def feed_overlap(a: Set[Tuple[str, str]], b: Set[Tuple[str, str]]) -> Dict[str, Any]:
    inter, union = a & b, a | b

    def near(x, other):
        s, ts = x
        t0 = pd.Timestamp(ts)
        return any((s, (t0 + pd.Timedelta(minutes=k * BAR_MINUTES)).isoformat()) in other for k in (-1, 1))
    ao, bo = a - b, b - a
    j = len(inter) / len(union) if union else None
    return {"a_signals": len(a), "b_signals": len(b), "exact_matches": len(inter), "a_only": sorted(ao), "b_only": sorted(bo),
            "a_only_count": len(ao), "b_only_count": len(bo), "jaccard": j, "band": ps.feed_band(j),
            "a_only_with_b_within_30min": sum(near(x, b) for x in ao), "b_only_with_a_within_30min": sum(near(x, a) for x in bo)}


def missing_bucket_sessions(b: FeedBundle, start: date, end: date) -> Dict[str, Any]:
    """Sesiones sin cubeta 09:30 de SPY (bloquea todo) o de cada acción (bloquea esa acción)."""
    def first_missing(df):
        f = _bar_frame(df)
        have = set(f.loc[f["start_min"] == RTH_OPEN_MIN, "session"])
        sessions = {s for s in f["session"] if start <= s <= end}
        return sorted(s.isoformat() for s in sessions - have)
    return {"spy_first_bucket_missing": first_missing(b.spy),
            "stock_first_bucket_missing": {s: first_missing(df) for s, df in sorted(b.stocks.items())}}


def p5_compare(iex: FeedBundle, sip: FeedBundle, start: date = KNOWN[0], end: date = KNOWN[1]) -> Dict[str, Any]:
    """IEX(acciones+SPY) vs SIP(acciones+SPY), cada uno independiente. Solo KNOWN; sin resultados ni P&L."""
    ra.check_window("feed", start, end)
    if iex.feed == sip.feed:
        raise H005Error("P5 requiere dos feeds distintos")
    ov = feed_overlap(emitted_set(iex, start, end), emitted_set(sip, start, end))
    ohlc = {s: ra.compare_frames(iex.stocks[s], sip.stocks[s], rth_only=False)
            for s in sorted(set(iex.stocks) & set(sip.stocks))}
    ohlc["SPY"] = ra.compare_frames(iex.spy, sip.spy, rth_only=False)
    return {"feeds": [iex.feed, sip.feed], "overlap": ov, "common_bucket_ohlc": ohlc,
            "missing_bucket_sessions": {iex.feed: missing_bucket_sessions(iex, start, end),
                                        sip.feed: missing_bucket_sessions(sip, start, end)}}


# ================================================================ carga de datos reales (NO ejecutada en este paso)
def load_development_universe(stock_dir: Path, spy_dir: Path, spy_manifest: Path, stock_manifest: Path) -> Universe:
    """IEX 1Min acotado a 2025-12-31 → 30Min. Verifica checksums de manifiestos. Solo DEVELOPMENT (sin bypass)."""
    from .historical_data import load_symbol_bars, symbol_path, validate_bars
    ra.check_window("strategy", DEV[0], DEV[1])
    end_utc = (pd.Timestamp(DEV[1]) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    start_utc = pd.Timestamp(HISTORY_START).tz_localize(NY).tz_convert("UTC")
    man = json.loads(Path(stock_manifest).read_text(encoding="utf-8"))
    by = {f["symbol"]: f["sha256"] for f in man["files"]}
    stocks = {}
    for s in TRADABLE:
        if sha256_file(symbol_path(stock_dir, "1Min", s)) != by[s]:
            raise H005Error(f"{s}: checksum distinto del manifiesto histórico")
        stocks[s] = resample_30min(load_symbol_bars(stock_dir, "1Min", s, start_utc, end_utc))
    sm = json.loads(Path(spy_manifest).read_text(encoding="utf-8"))
    p = symbol_path(spy_dir, "1Min", "SPY")
    if sm.get("h004_data_readiness") != "PASS" or sha256_file(p) != sm["raw_file"]["sha256"]:
        raise H005Error("SPY de desarrollo no coincide con el manifiesto de contexto auditado")
    spy1 = validate_bars(pd.read_csv(p, dtype={"timestamp": str, "symbol": str}), "SPY", str(p))
    spy1 = spy1[(spy1.index >= start_utc) & (spy1.index < end_utc)]
    return Universe(stocks=stocks, spy=resample_30min(spy1), start=DEV[0], end=DEV[1], feed="iex")
