# src/strategy.py
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pandas as pd


@dataclass
class StrategyResult:
    """
    Resultado estructurado de evaluar una estrategia en un tick — el "por qué"
    detrás de BUY/SELL/HOLD, no solo el veredicto final.

    - signal: "BUY" / "SELL" / None — idéntico a lo que devuelve signal().
    - reason: explicación en texto de por qué se llegó a esa señal.
    - values: valores crudos de los indicadores usados en la decisión (ej.
      {"rsi_prev": 28.4, "rsi_curr": 31.2}), para loguear/graficar después.
    - warmup_ok: False cuando el HOLD es por falta de datos (pocas velas o
      indicadores todavía en NaN); True cuando el HOLD es porque la condición
      de entrada/salida simplemente no se dio con datos suficientes. Esta es
      la distinción explícita que se pidió: "no hubo señal" vs "no había
      datos para evaluar si había señal".
    """
    signal: Optional[str]
    reason: str
    values: Dict[str, Any] = field(default_factory=dict)
    warmup_ok: bool = True


class MACrossover:
    def __init__(self, fast: int = 10, slow: int = 30):
        assert fast < slow, "fast debe ser < slow"
        self.fast = fast
        self.slow = slow

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        if "close" not in df.columns:
            raise ValueError("El DataFrame debe contener columna 'close'")
        prices = df["close"]
        needed = self.slow + 2
        if len(prices) < needed:
            return StrategyResult(None, f"Warm-up insuficiente ({len(prices)}/{needed} velas)", warmup_ok=False)

        ma_fast = prices.rolling(self.fast).mean()
        ma_slow = prices.rolling(self.slow).mean()
        prev_cross = ma_fast.iloc[-2] - ma_slow.iloc[-2]
        now_cross = ma_fast.iloc[-1] - ma_slow.iloc[-1]

        if pd.isna(prev_cross) or pd.isna(now_cross):
            # Con largo suficiente esto normalmente no pasa; puede pasar si
            # hay huecos/NaN en el precio de entrada (datos sucios).
            return StrategyResult(None, "Medias aún no definidas (datos con huecos/NaN)", warmup_ok=False)

        values = {
            "ma_fast": float(ma_fast.iloc[-1]),
            "ma_slow": float(ma_slow.iloc[-1]),
            "prev_diff": float(prev_cross),
            "now_diff": float(now_cross),
        }
        if prev_cross <= 0 and now_cross > 0:
            return StrategyResult(
                "BUY", f"Cruce alcista MA{self.fast}/MA{self.slow}: diff pasó de {prev_cross:.4f} a {now_cross:.4f}", values
            )
        if prev_cross >= 0 and now_cross < 0:
            return StrategyResult(
                "SELL", f"Cruce bajista MA{self.fast}/MA{self.slow}: diff pasó de {prev_cross:.4f} a {now_cross:.4f}", values
            )
        return StrategyResult(None, f"Sin cruce (MA{self.fast}-MA{self.slow}={now_cross:.4f})", values)

    def signal(self, df: pd.DataFrame) -> Optional[str]:
        """Compatibilidad: idéntico a evaluate(df).signal (ver evaluate())."""
        return self.evaluate(df).signal


class RSIStrategy:
    def __init__(self, period: int = 14, buy_level: float = 30.0, sell_level: float = 70.0):
        self.period = period
        self.buy_level = buy_level
        self.sell_level = sell_level

    def rsi(self, s: pd.Series) -> pd.Series:
        delta = s.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/self.period, min_periods=self.period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/self.period, min_periods=self.period, adjust=False).mean()
        rs = avg_gain / (avg_loss.replace(0, 1e-12))
        return 100 - (100 / (1 + rs))

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        if "close" not in df.columns:
            raise ValueError("El DataFrame debe contener columna 'close'")
        needed = self.period + 2
        if len(df) < needed:
            return StrategyResult(None, f"Warm-up insuficiente ({len(df)}/{needed} velas)", warmup_ok=False)

        rsi = self.rsi(df["close"])
        r0, r1 = rsi.iloc[-2], rsi.iloc[-1]
        if pd.isna(r0) or pd.isna(r1):
            return StrategyResult(None, "RSI aún no definido (NaN, falta warm-up de la EMA interna)", warmup_ok=False)

        values = {"rsi_prev": float(r0), "rsi_curr": float(r1), "buy_level": self.buy_level, "sell_level": self.sell_level}
        # BUY cuando cruza hacia arriba nivel de sobreventa
        if r0 <= self.buy_level and r1 > self.buy_level:
            return StrategyResult("BUY", f"RSI cruzó hacia arriba el nivel de sobreventa ({self.buy_level}): {r0:.1f} -> {r1:.1f}", values)
        # SELL cuando cruza hacia abajo nivel de sobrecompra
        if r0 >= self.sell_level and r1 < self.sell_level:
            return StrategyResult("SELL", f"RSI cruzó hacia abajo el nivel de sobrecompra ({self.sell_level}): {r0:.1f} -> {r1:.1f}", values)
        return StrategyResult(None, f"RSI={r1:.1f}, sin cruce de niveles ({self.buy_level}/{self.sell_level})", values)

    def signal(self, df: pd.DataFrame) -> str | None:
        """Compatibilidad: idéntico a evaluate(df).signal (ver evaluate())."""
        return self.evaluate(df).signal


class MACDStrategy:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_p = signal

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        if "close" not in df.columns:
            raise ValueError("El DataFrame debe contener columna 'close'")
        needed = self.slow + 2
        if len(df) < needed:
            return StrategyResult(None, f"Warm-up insuficiente ({len(df)}/{needed} velas)", warmup_ok=False)

        close = df["close"]
        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig = macd.ewm(span=self.signal_p, adjust=False).mean()
        hist_prev, hist_curr = (macd - sig).iloc[-2], (macd - sig).iloc[-1]
        if pd.isna(hist_prev) or pd.isna(hist_curr):
            return StrategyResult(None, "Histograma MACD aún no definido (NaN)", warmup_ok=False)

        values = {
            "macd": float(macd.iloc[-1]),
            "signal_line": float(sig.iloc[-1]),
            "hist_prev": float(hist_prev),
            "hist_curr": float(hist_curr),
        }
        if hist_prev <= 0 and hist_curr > 0:
            return StrategyResult("BUY", f"Histograma MACD cruzó a positivo: {hist_prev:.4f} -> {hist_curr:.4f}", values)
        if hist_prev >= 0 and hist_curr < 0:
            return StrategyResult("SELL", f"Histograma MACD cruzó a negativo: {hist_prev:.4f} -> {hist_curr:.4f}", values)
        return StrategyResult(None, f"Sin cruce (histograma={hist_curr:.4f})", values)

    def signal(self, df: pd.DataFrame) -> str | None:
        """Compatibilidad: idéntico a evaluate(df).signal (ver evaluate())."""
        return self.evaluate(df).signal


class BollingerStrategy:
    def __init__(self, window: int = 20, k: float = 2.0):
        self.window = window
        self.k = k

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        if "close" not in df.columns:
            raise ValueError("El DataFrame debe contener columna 'close'")
        needed = self.window + 2
        if len(df) < needed:
            return StrategyResult(None, f"Warm-up insuficiente ({len(df)}/{needed} velas)", warmup_ok=False)

        close = df["close"]
        ma = close.rolling(self.window).mean()
        std = close.rolling(self.window).std()
        upper = ma + self.k * std
        lower = ma - self.k * std
        c0, c1 = close.iloc[-2], close.iloc[-1]
        u0, u1 = upper.iloc[-2], upper.iloc[-1]
        l0, l1 = lower.iloc[-2], lower.iloc[-1]
        if pd.isna(u0) or pd.isna(u1) or pd.isna(l0) or pd.isna(l1):
            return StrategyResult(None, "Bandas aún no definidas (NaN)", warmup_ok=False)

        values = {"close": float(c1), "ma": float(ma.iloc[-1]), "upper": float(u1), "lower": float(l1)}
        # Reversión a la media: si sale de banda inferior → BUY; si sale de superior → SELL
        if c0 <= l0 and c1 > l1:
            return StrategyResult("BUY", f"Cierre reingresó desde la banda inferior: {c0:.2f} -> {c1:.2f} (banda={l1:.2f})", values)
        if c0 >= u0 and c1 < u1:
            return StrategyResult("SELL", f"Cierre reingresó desde la banda superior: {c0:.2f} -> {c1:.2f} (banda={u1:.2f})", values)
        return StrategyResult(None, f"close={c1:.2f} dentro de bandas [{l1:.2f}, {u1:.2f}]", values)

    def signal(self, df: pd.DataFrame) -> str | None:
        """Compatibilidad: idéntico a evaluate(df).signal (ver evaluate())."""
        return self.evaluate(df).signal
