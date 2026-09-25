# src/strategy_v2_h004.py
"""
STRATEGY_V2_HYPOTHESIS_004 — H003 + compuerta de régimen de mercado SPY (solo entrada, long-only).
Spec congelado: research/strategy_v2_hypothesis_004.md (commit 31526c27436e53780cae865dde991f2fd9df01a5).
SOLO investigación/backtest: sin contraparte en vivo; el motor compartido y H003 no cambian.

Reglas (spec §3–§14):
- H003 congelado genera su señal cruda EXACTAMENTE igual (consumo del setup por emisión, re-arm T−6 > S).
- Solo si la señal cruda es BUY se evalúa la compuerta en el instante de decisión D = inicio(T) + 5 min:
  R* = última cubeta SPY 15Min RTH de la MISMA sesión con fin <= D (nunca una cubeta parcial).
  R* ausente -> MARKET_REGIME_UNAVAILABLE (sin sustituir por una cubeta anterior).
  POSITIVE sii close_R > EMA50_R y EMA50_R − EMA50_{R−3} > 0 (ambas estrictas; R−3 = 3 velas EXISTENTES atrás).
  Otro caso -> MARKET_REGIME_BLOCKED.
- POSITIVE: la BUY sigue por el camino H003 sin cambios. BLOCKED/UNAVAILABLE: el motor no recibe BUY (no hay
  llamada al RiskManager). Las tres salidas consumen el setup H003 (la re-ejecución de H003 no ve la compuerta).
- Sin salida nueva: el régimen solo filtra entradas. SPY es solo contexto (nunca operado ni en P&L/D6).
- EMA50 de SPY: MISMA convención que H003 (`_own_window_ema`: ventana propia de 200 velas, expansiva al inicio).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .historical_audit import RTH_OPEN_MIN
from .strategy import StrategyResult
from .strategy_v2_h001 import _own_window_ema
from .strategy_v2_h003 import ConsolidationBreakoutH003

HYPOTHESIS_ID = "STRATEGY_V2_HYPOTHESIS_004"
FROZEN_SPEC_COMMIT = "31526c27436e53780cae865dde991f2fd9df01a5"
PARENT_ID = "STRATEGY_V2_HYPOTHESIS_003"
NY = "America/New_York"

# ---- constantes congeladas; NO optimizar
EMA_SPAN = 50
SLOPE_BARS = 3
BUCKET_MINUTES = 15
STOCK_BAR_MINUTES = 5
POSITIVE = "POSITIVE"
BLOCKED = "MARKET_REGIME_BLOCKED"
UNAVAILABLE = "MARKET_REGIME_UNAVAILABLE"


class AlignmentError(RuntimeError):
    """Violación de alineación temporal (cubeta SPY parcial/futura): falla la corrida."""


def regime_status(close_r: float, ema_r: float, ema_r3: float) -> str:
    """§9: ambas condiciones estrictas; cualquier valor no finito no puede ser POSITIVE."""
    vals = (close_r, ema_r, ema_r3)
    if not all(v is not None and math.isfinite(v) for v in vals):
        return BLOCKED
    return POSITIVE if (close_r > ema_r and (ema_r - ema_r3) > 0) else BLOCKED


def decision_time(signal_bar_start: pd.Timestamp) -> pd.Timestamp:
    """Vela 5Min que empieza en t se conoce (y se decide) en t + 5 min."""
    return pd.Timestamp(signal_bar_start) + pd.Timedelta(minutes=STOCK_BAR_MINUTES)


def expected_bucket(decision: pd.Timestamp) -> pd.Timestamp:
    """§10: inicio (UTC) de R* = última cubeta 15Min anclada a 09:30 de la misma sesión con fin <= D."""
    d = pd.Timestamp(decision).tz_convert(NY)
    minute = d.hour * 60 + d.minute + d.second / 60.0 + d.microsecond / 6e7
    k = math.floor((minute - RTH_OPEN_MIN) / BUCKET_MINUTES)       # cubetas completas desde 09:30
    if k < 1:
        raise AlignmentError(f"decisión {d.isoformat()} antes del fin de la primera cubeta SPY de la sesión")
    session_open = d.normalize() + pd.Timedelta(minutes=RTH_OPEN_MIN)
    start = session_open + pd.Timedelta(minutes=BUCKET_MINUTES * (k - 1))
    return start.tz_convert("UTC")


class SpyRegimeContext:
    """
    Serie SPY 15Min RTH (200 velas de soporte + split), índice UTC = inicio de cubeta. Solo lectura.
    `slope_gap` (opcional): bandera por vela (spy_context_data.slope_gap_flags), solo diagnóstico.
    """

    def __init__(self, bars15: pd.DataFrame, slope_gap: Optional[Sequence[int]] = None):
        if not bars15.index.is_monotonic_increasing or bars15.index.has_duplicates:
            raise ValueError("serie SPY 15Min no monótona o con duplicados")
        self.bars = bars15
        self.close = bars15["close"].to_numpy(float)
        self.ns = bars15.index.as_unit("ns").asi8
        self.pos = {int(v): k for k, v in enumerate(self.ns)}
        self.ema50 = _own_window_ema(self.close, list(range(len(self.close))), EMA_SPAN)   # helper de H003
        self.slope_gap = None if slope_gap is None else np.asarray(slope_gap)

    def regime_at(self, decision: pd.Timestamp) -> Dict[str, Any]:
        exp_start = expected_bucket(decision)
        exp_end = exp_start + pd.Timedelta(minutes=BUCKET_MINUTES)
        row: Dict[str, Any] = {"decision_time": pd.Timestamp(decision).tz_convert("UTC").isoformat(),
                               "expected_spy_bucket_start": exp_start.isoformat(),
                               "expected_spy_bucket_end": exp_end.isoformat(),
                               "spy_bar_start": None, "spy_bar_end": None, "spy_close": None, "spy_ema50": None,
                               "spy_ema50_r_minus_3": None, "spy_close_vs_ema50_pct": None,
                               "spy_ema50_slope3_pct": None, "slope_window_spans_missing_bucket": None}
        k = self.pos.get(int(exp_start.value))
        if k is None or k < SLOPE_BARS:
            row["status"] = UNAVAILABLE
            return row
        used_start = pd.Timestamp(self.ns[k], tz="UTC")
        used_end = used_start + pd.Timedelta(minutes=BUCKET_MINUTES)
        if used_end > pd.Timestamp(decision) or used_start != exp_start:
            raise AlignmentError(f"cubeta SPY {used_start} no completa/esperada en {decision}")
        c, e, e3 = float(self.close[k]), float(self.ema50[k]), float(self.ema50[k - SLOPE_BARS])
        row.update(spy_bar_start=used_start.isoformat(), spy_bar_end=used_end.isoformat(), spy_close=c,
                   spy_ema50=e, spy_ema50_r_minus_3=e3, spy_close_vs_ema50_pct=(c - e) / e * 100,
                   spy_ema50_slope3_pct=(e - e3) / e3 * 100,
                   slope_window_spans_missing_bucket=(None if self.slope_gap is None else bool(self.slope_gap[k] == 1)),
                   status=regime_status(c, e, e3))
        return row


class RegimeGatedH004:
    """Estrategia de investigación H004: H003 congelado + compuerta SPY (solo entrada). Sin salidas propias."""

    id = HYPOTHESIS_ID

    def __init__(self, context: SpyRegimeContext):
        self._h003 = ConsolidationBreakoutH003()
        self._context = context
        self._gate_enabled = True
        self.min_bars = self._h003.min_bars
        self.gate_log: List[Dict[str, Any]] = []
        self._calls = 0

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        call = self._calls                     # índice de llamada == índice en engine.evaluations (símbolo)
        self._calls += 1
        raw = self._h003.evaluate(df)          # señal cruda de H003 (consume su setup por emisión)
        if raw.signal != "BUY" or not self._gate_enabled:
            return raw
        start = df.index[-1]
        g = self._context.regime_at(decision_time(start))
        self.gate_log.append({"call_index": call, "bar_timestamp": start.isoformat(), **g})
        if g["status"] == POSITIVE:
            return raw
        return StrategyResult(None, g["status"], dict(raw.values, market_regime=g["status"]), warmup_ok=True)

    def signal(self, df: pd.DataFrame) -> Optional[str]:
        return self.evaluate(df).signal


def _parent_reproduction_strategy() -> RegimeGatedH004:
    """
    USO INTERNO (tests y chequeo previo a la corrida oficial): compuerta desactivada = H003 congelado puro.
    No hay flag de CLI ni opción pública; la corrida oficial siempre construye RegimeGatedH004(context).
    """
    s = RegimeGatedH004.__new__(RegimeGatedH004)
    s._h003 = ConsolidationBreakoutH003()
    s._context = None
    s._gate_enabled = False
    s.min_bars = s._h003.min_bars
    s.gate_log = []
    s._calls = 0
    return s
