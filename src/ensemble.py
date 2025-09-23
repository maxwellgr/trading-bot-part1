# src/ensemble.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

import math
import pandas as pd


@dataclass
class StrategyWrapper:
    name: str
    strategy: object   # debe tener .signal(df) -> "BUY"/"SELL"/"EXIT"/None
    weight: float = 1.0


class Ensemble:
    """
    Modos:
      - consensus: k-de-n; compra si >=k BUY y 0 SELL (y viceversa).
      - weighted:  score = sum(w BUY) - sum(w SELL). Entra si |score| >= min_score.
      - stacked:   'primary' debe dar BUY/SELL y >=(k-1) confirmadores alineados.
    Filtros de régimen:
      - trend filter: para largos exige close >= SMA(window), para cortos <=.
      - atr filter:   exige ATR(normalizado) >= umbral (actividad suficiente).
    """

    def __init__(
        self,
        mode: str = "consensus",
        k: int = 2,
        min_score: float = 1.0,
        primary: str = "ma",
        use_trend_filter: bool = False,
        trend_window: int = 200,
        use_atr_filter: bool = False,
        atr_window: int = 14,
        atr_threshold: float = 0.003,  # 0.3% del precio
    ):
        assert mode in {"consensus", "weighted", "stacked"}
        self.mode = mode
        self.k = max(1, int(k))
        self.min_score = float(min_score)
        self.primary = primary.lower() if primary else None
        self.use_trend_filter = use_trend_filter
        self.trend_window = trend_window
        self.use_atr_filter = use_atr_filter
        self.atr_window = atr_window
        self.atr_threshold = float(atr_threshold)

    # -------- utilidades técnicas --------
    @staticmethod
    def _sma(series: pd.Series, w: int) -> Optional[float]:
        if len(series) < w:
            return None
        return float(series.iloc[-w:].mean())

    @staticmethod
    def _atr(df: pd.DataFrame, w: int) -> Optional[float]:
        if not {"high", "low", "close"}.issubset(df.columns):
            return None
        if len(df) < w + 1:
            return None
        # True Range
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (df["high"] - df["low"]).abs(),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return float(tr.tail(w).mean())

    def _trend_gate(self, df: pd.DataFrame) -> Tuple[bool, bool, Optional[float]]:
        """Devuelve (allow_long, allow_short, sma_val) bajo filtro de tendencia."""
        if not self.use_trend_filter:
            return True, True, None
        sma = self._sma(df["close"], self.trend_window)
        if sma is None:
            return True, True, None
        last = float(df["close"].iloc[-1])
        allow_long = last >= sma
        allow_short = last <= sma
        return allow_long, allow_short, sma

    def _atr_gate(self, df: pd.DataFrame) -> Tuple[bool, Optional[float]]:
        """Devuelve (suficiente_volatilidad, atr_norm)."""
        if not self.use_atr_filter:
            return True, None
        atr = self._atr(df, self.atr_window)
        if atr is None:
            return True, None
        price = float(df["close"].iloc[-1])
        atr_norm = atr / price if price > 0 else 0.0
        return (atr_norm >= self.atr_threshold), atr_norm

    # -------- núcleo del ensemble --------
    def decide(self, df: pd.DataFrame, wrappers: List[StrategyWrapper]) -> Tuple[str, Dict]:
        """
        Ejecuta todas las estrategias y decide una señal final.
        Retorna: (signal, meta)  con meta["signals"], meta["votes"], filtros, etc.
        """
        raw_signals: Dict[str, Optional[str]] = {}
        buys, sells = 0, 0
        score = 0.0

        # Filtros de régimen
        allow_long, allow_short, sma_val = self._trend_gate(df)
        atr_ok, atr_norm = self._atr_gate(df)

        # Ejecutar estrategias
        for w in wrappers:
            sig = w.strategy.signal(df)
            sig = sig if sig in {"BUY", "SELL"} else None  # ignoramos HOLD/EXIT aquí
            # aplicar filtros de régimen como gate
            if sig == "BUY" and (not allow_long or not atr_ok):
                sig = None
            if sig == "SELL" and (not allow_short or not atr_ok):
                sig = None

            raw_signals[w.name] = sig
            if sig == "BUY":
                buys += 1
                score += float(w.weight)
            elif sig == "SELL":
                sells += 1
                score -= float(w.weight)

        # Decisión por modo
        final = "HOLD"
        reason = ""
        if self.mode == "consensus":
            if buys >= self.k and sells == 0:
                final, reason = "BUY", f"{buys}-de-{len(wrappers)} BUY (k={self.k})"
            elif sells >= self.k and buys == 0:
                final, reason = "SELL", f"{sells}-de-{len(wrappers)} SELL (k={self.k})"

        elif self.mode == "weighted":
            if score >= self.min_score and sells == 0:
                final, reason = "BUY", f"score={score:.2f} ≥ {self.min_score}"
            elif score <= -self.min_score and buys == 0:
                final, reason = "SELL", f"score={score:.2f} ≤ -{self.min_score}"

        elif self.mode == "stacked":
            # primaria + confirmaciones
            primary_sig = raw_signals.get(self.primary)
            if primary_sig in {"BUY", "SELL"}:
                agree = 0
                for name, sig in raw_signals.items():
                    if name == self.primary or sig is None:
                        continue
                    if sig == primary_sig:
                        agree += 1
                if agree >= max(0, self.k - 1):
                    final, reason = primary_sig, f"primary={self.primary} + {agree} confirm."
                else:
                    reason = f"primary={self.primary} sin suficientes confirmadores ({agree}/{self.k-1})"
            else:
                reason = f"primary={self.primary} sin señal"

        meta = {
            "signals": raw_signals,
            "votes": {"BUY": buys, "SELL": sells},
            "score": score,
            "reason": reason,
            "trend": {
                "enabled": self.use_trend_filter,
                "sma_window": self.trend_window,
                "sma_value": sma_val,
                "allow_long": allow_long,
                "allow_short": allow_short,
            },
            "volatility": {
                "enabled": self.use_atr_filter,
                "atr_window": self.atr_window,
                "atr_norm": atr_norm,
                "threshold": self.atr_threshold,
            },
        }
        return final, meta
