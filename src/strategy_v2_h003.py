# src/strategy_v2_h003.py
"""
STRATEGY_V2_HYPOTHESIS_003 — 5Min RTH trend-filtered consolidation breakout (long-only).
Spec congelado: research/strategy_v2_hypothesis_003.md (commit 6eb6078719d12e50542d7284d6e96f598c115f71).
SOLO investigación/backtest: sin contraparte en vivo; el motor compartido no cambia.

Reglas (§5-§7 del spec), evaluadas al CIERRE de la vela 5Min T (conocida en start(T)+5min):
A. close_T > EMA50_T  y  EMA50_T − EMA50_{T−3} > 0
B. C(T) = las 6 velas EXISTENTES T−6..T−1, todas en la sesión de T (T >= 7ª vela existente)
C. rango de C(T) / ATR14_{T−1} <= 2.00  (ATR cero / no finito / no disponible -> inválido, sin fallback)
D. close_T > max(high de C(T))  (estricto)
E. close_T > open_T
F. (close_T − low_T)/(high_T − low_T) >= 0.75; high_T == low_T -> falla
G. re-arm: tras una BUY en S (misma sesión), la próxima exige T−6 > S. Consumo por EMISIÓN.
   Estado derivado re-jugando la sesión desde su primera vela; se reinicia cada sesión.
Sin señal de salida. Indicadores con la semántica de H001: EMA50 de cada vela sobre SU ventana de 200
velas; ATR14 = RiskManager._atr sobre velas <= i; mínimo 150 velas de historia.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .strategy import StrategyResult
from .strategy_v2_h001 import (
    ENGINE_LOOKBACK, MAX_SESSION_BARS, WARMUP_BARS, _atr_at, _own_window_ema, session_ids,
)

HYPOTHESIS_ID = "STRATEGY_V2_HYPOTHESIS_003"
FROZEN_SPEC_COMMIT = "6eb6078719d12e50542d7284d6e96f598c115f71"
NY = "America/New_York"

# ---- constantes congeladas (§5); NO optimizar
EMA_SPAN = 50
SLOPE_BARS = 3
CONSOLIDATION_BARS = 6
MAX_RANGE_ATR = 2.00
MIN_CLOSE_LOCATION = 0.75
NONCONTIGUOUS_SPAN_MIN = 25          # start(T−1) − start(T−6) > 25 min -> ventana no contigua (solo diagnóstico)


def _finite_pos(x: Optional[float]) -> bool:
    return x is not None and math.isfinite(x) and x > 0


def bar_conditions(j: int, s0: int, o, h, l, c, e50, atr) -> Dict[str, bool]:
    """Condiciones A–F en la vela j (s0 = primera vela de la sesión de j). Arrays indexados igual."""
    if j - s0 < CONSOLIDATION_BARS:
        return {"same_session_window": False}
    w = range(j - CONSOLIDATION_BARS, j)
    a_prev = atr[j - 1]
    atr_ok = _finite_pos(a_prev)
    hi = max(h[i] for i in w)
    lo = min(l[i] for i in w)
    rng_t = h[j] - l[j]
    cond = {
        "same_session_window": True,
        "close_gt_ema50": c[j] > e50[j],
        "ema50_slope_pos": (e50[j] - e50[j - SLOPE_BARS]) > 0,
        "atr_prev_valid": atr_ok,
        "consolidation_valid": atr_ok and (hi - lo) / a_prev <= MAX_RANGE_ATR,
        "breakout_close_gt_high": c[j] > hi,
        "bullish_bar": c[j] > o[j],
        "close_location_ok": rng_t > 0 and (c[j] - l[j]) / rng_t >= MIN_CLOSE_LOCATION,
    }
    return {k: bool(v) for k, v in cond.items()}


def replay_session(s0: int, t: int, o, h, l, c, e50, atr, eligible) -> List[int]:
    """§7: re-juega [s0, t] y devuelve las velas donde se EMITE BUY (consumo por emisión; re-arm T−6 > S)."""
    emitted: List[int] = []
    last: Optional[int] = None
    for j in range(s0, t + 1):
        if not eligible[j]:
            continue
        if not all(bar_conditions(j, s0, o, h, l, c, e50, atr).values()):
            continue
        if last is not None and not (j - CONSOLIDATION_BARS > last):
            continue
        emitted.append(j)
        last = j
    return emitted


class ConsolidationBreakoutH003:
    """Estrategia de investigación H003. evaluate(df): velas 5Min RTH completas; la última es T."""

    id = HYPOTHESIS_ID
    min_bars = WARMUP_BARS

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        n = len(df)
        if n < WARMUP_BARS:
            return StrategyResult(None, f"Warm-up insuficiente ({n}/{WARMUP_BARS} velas)", warmup_ok=False)
        t = n - 1
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        off = max(0, t - MAX_SESSION_BARS)
        sess = session_ids(df.index[off:])
        s0 = t
        while s0 - 1 >= off and sess[s0 - 1 - off] == sess[t - off]:
            s0 -= 1
        if t - s0 < CONSOLIDATION_BARS:
            return StrategyResult(None, "Ventana de consolidación incompleta en la sesión")
        # prefiltro barato con las mismas reglas D–F en T (si fallan, no hay BUY en T)
        rng_t = h[t] - l[t]
        if not (c[t] > o[t] and rng_t > 0 and (c[t] - l[t]) / rng_t >= MIN_CLOSE_LOCATION
                and c[t] > max(h[t - CONSOLIDATION_BARS:t])):
            return StrategyResult(None, "Sin ruptura válida")
        idx = list(range(s0, t + 1))
        e50 = np.full(n, np.nan)
        atr = np.full(n, np.nan)
        e50[idx] = _own_window_ema(c, idx, EMA_SPAN)
        for i in idx:
            atr[i] = _atr_at(h, l, c, i)
        eligible = np.zeros(n, dtype=bool)
        eligible[idx] = [i + 1 >= WARMUP_BARS for i in idx]
        emitted = replay_session(s0, t, o, h, l, c, e50, atr, eligible)
        values = {"ema50": float(e50[t]), "atr_prev": float(atr[t - 1]), "session_bar": int(t - s0 + 1),
                  "session_signals_before": [int(j - s0 + 1) for j in emitted if j < t]}
        if emitted and emitted[-1] == t:
            return StrategyResult("BUY", "H003: tendencia + consolidación + ruptura", values)
        return StrategyResult(None, "H003: sin señal", values)

    def signal(self, df: pd.DataFrame) -> Optional[str]:
        return self.evaluate(df).signal


# ================================================================ estructura de la señal (solo reporte)
def signal_structure(o, h, l, c, ts_ns: np.ndarray, t: int) -> Dict[str, Any]:
    """Métricas de reporte en la vela de señal t (solo velas <= t). Normalizadas por ATR_{T−1} (Q5)."""
    w = range(t - CONSOLIDATION_BARS, t)
    hi, lo = max(h[i] for i in w), min(l[i] for i in w)
    a = _atr_at(h, l, c, t - 1)
    span_min = (int(ts_ns[t - 1]) - int(ts_ns[t - CONSOLIDATION_BARS])) / 60e9
    return {"consolidation_range_atr": (hi - lo) / a, "breakout_range_atr": (h[t] - l[t]) / a,
            "breakout_distance_atr": (c[t] - hi) / a, "consolidation_high": float(hi), "atr_prev": float(a),
            "window_span_minutes": float(span_min), "noncontiguous_window": bool(span_min > NONCONTIGUOUS_SPAN_MIN)}
