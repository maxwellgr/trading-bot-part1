# src/execution_guards.py
"""
Guardas de seguridad en la frontera de ejecución (datos -> riesgo/órdenes).

No tocan la estrategia ni el ensemble: la señal se calcula y se registra
igual que siempre. Estas guardas solo deciden si una señal ACCIONABLE
(BUY/SELL) puede continuar hacia RiskManager / broker:

- BarFreshnessGuard: detecta cuando la última vela de un símbolo deja de
  avanzar (datos "congelados"). Una señal basada en datos obsoletos no
  llega a una orden nueva.
- ActionableSignalDeduper: una misma (símbolo, bar_timestamp, lado) solo
  puede pasar UNA vez por riesgo/ejecución, aunque el polling vea la misma
  vela varias veces.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from .logger import logger


_TF_RE = re.compile(r"^\s*(\d+)\s*(Min|T|Hour|H|Day|D|Week|W|Month|M)\s*$", re.IGNORECASE)
_TF_UNIT_SECONDS = {
    "min": 60, "t": 60,
    "hour": 3600, "h": 3600,
    "day": 86400, "d": 86400,
    "week": 7 * 86400, "w": 7 * 86400,
    "month": 30 * 86400, "m": 30 * 86400,
}


def timeframe_to_seconds(timeframe: str) -> Optional[int]:
    """'1Min' -> 60, '15Min' -> 900, '1Hour' -> 3600, '1Day' -> 86400. None si no se reconoce."""
    m = _TF_RE.match(timeframe or "")
    if not m:
        return None
    return int(m.group(1)) * _TF_UNIT_SECONDS[m.group(2).lower()]


# Umbral de obsolescencia: STALE_AFTER_BARS velas del timeframe, con un piso
# de MIN_STALE_SECONDS. Para 1Min = 300s (5 min) medido desde la APERTURA de
# la vela. Justificación:
#  - Normal: la vela T cierra en T+60s y se publica segundos después; con
#    poll de 15s y ~8 símbolos secuenciales, verla hasta ~T+120s es normal.
#  - El feed IEX es disperso: en nombres menos líquidos pueden faltar 1–3
#    velas de 1Min sin que nada esté roto.
#  - 5 min tolera ambos casos sin avisar cada 15s, pero bloquea operar con un
#    precio de hace 5+ min (en el incidente eran 60+ min).
STALE_AFTER_BARS = 5
MIN_STALE_SECONDS = 300
# Mientras un símbolo siga obsoleto, se re-avisa como mucho cada N segundos.
STALE_REMINDER_SECONDS = 300


@dataclass
class FreshnessStatus:
    symbol: str
    bar_timestamp: str
    is_stale: bool
    age_seconds: float
    threshold_seconds: Optional[float]
    unchanged_seconds: float   # tiempo de reloj desde que esta vela se vio por primera vez
    event: Optional[str]       # "stale" | "still_stale" | "recovered" | None (nada que avisar)


class BarFreshnessGuard:
    """
    Registra, por símbolo, la última bar_timestamp vista y cuándo se vio por
    primera vez. Una vela es obsoleta si, con el mercado abierto, su edad
    (ahora - bar_timestamp) supera el umbral del timeframe.

    Con el mercado cerrado (--ignore-clock) los datos son históricos por
    definición: la guarda no clasifica nada como obsoleto ni avisa, para no
    cambiar el comportamiento de ese modo.
    """

    def __init__(
        self,
        timeframe: str,
        stale_after_bars: int = STALE_AFTER_BARS,
        min_stale_seconds: float = MIN_STALE_SECONDS,
        reminder_seconds: float = STALE_REMINDER_SECONDS,
    ):
        tf = timeframe_to_seconds(timeframe)
        self.threshold_seconds: Optional[float] = (
            max(stale_after_bars * tf, min_stale_seconds) if tf else None
        )
        if self.threshold_seconds is None:
            logger.warning(f"Timeframe '{timeframe}' no reconocido: detección de datos obsoletos desactivada.")
        self.reminder_seconds = reminder_seconds
        # symbol -> (bar_ts, first_seen_at)
        self._latest: Dict[str, Tuple[datetime, datetime]] = {}
        # symbol -> última vez que se avisó (None si no está obsoleto)
        self._stale_since_warn: Dict[str, Optional[datetime]] = {}

    def observe(self, symbol: str, bar_ts: datetime, now: datetime, market_open: bool = True) -> FreshnessStatus:
        prev = self._latest.get(symbol)
        if prev is None or bar_ts > prev[0]:
            self._latest[symbol] = (bar_ts, now)
            first_seen = now
        else:
            first_seen = prev[1]

        age = (now - bar_ts).total_seconds()
        unchanged = (now - first_seen).total_seconds()
        is_stale = bool(market_open and self.threshold_seconds is not None and age > self.threshold_seconds)

        event: Optional[str] = None
        last_warn = self._stale_since_warn.get(symbol)
        if is_stale:
            if last_warn is None:
                event = "stale"
                self._stale_since_warn[symbol] = now
            elif (now - last_warn).total_seconds() >= self.reminder_seconds:
                event = "still_stale"
                self._stale_since_warn[symbol] = now
        elif last_warn is not None:
            event = "recovered"
            self._stale_since_warn[symbol] = None

        return FreshnessStatus(
            symbol=symbol, bar_timestamp=bar_ts.isoformat(), is_stale=is_stale,
            age_seconds=age, threshold_seconds=self.threshold_seconds,
            unchanged_seconds=unchanged, event=event,
        )


class ActionableSignalDeduper:
    """
    Idempotencia de intentos de ejecución: la clave (símbolo, bar_timestamp,
    lado) se consume la PRIMERA vez que una señal accionable intenta pasar a
    riesgo/orden, sin importar el resultado (ACCEPT, REJECT o excepción) —
    reintentar tras un error de red podría duplicar una orden que sí llegó.

    Una vela nueva produce una clave nueva, así que vuelve a ser elegible.
    Memoria acotada: se recuerdan las últimas `max_keys` claves.
    """

    def __init__(self, max_keys: int = 5000):
        self.max_keys = max_keys
        self._seen: "OrderedDict[Tuple[str, str, str], None]" = OrderedDict()

    @staticmethod
    def key(symbol: str, bar_timestamp: Optional[str], side: str) -> Tuple[str, str, str]:
        return (symbol.upper(), str(bar_timestamp), side.upper())

    def try_acquire(self, symbol: str, bar_timestamp: Optional[str], side: str) -> bool:
        """True si es el primer intento para esta clave (y la marca); False si es duplicado."""
        k = self.key(symbol, bar_timestamp, side)
        if k in self._seen:
            return False
        self._seen[k] = None
        while len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return True


class ExecutionGuards:
    """Agrupa ambas guardas; run_paper.main() crea una instancia por sesión."""

    def __init__(self, timeframe: str):
        self.freshness = BarFreshnessGuard(timeframe)
        self.dedup = ActionableSignalDeduper()
