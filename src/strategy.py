# src/strategy.py
import pandas as pd
from typing import Optional

class MACrossover:
    def __init__(self, fast: int = 10, slow: int = 30):
        assert fast < slow, "fast debe ser < slow"
        self.fast = fast
        self.slow = slow

    def signal(self, df: pd.DataFrame) -> Optional[str]:
        if "close" not in df.columns:
            raise ValueError("El DataFrame debe contener columna 'close'")
        prices = df["close"]
        if len(prices) < self.slow + 2:
            return None
        ma_fast = prices.rolling(self.fast).mean()
        ma_slow = prices.rolling(self.slow).mean()
        prev_cross = ma_fast.iloc[-2] - ma_slow.iloc[-2]
        now_cross  = ma_fast.iloc[-1] - ma_slow.iloc[-1]
        if pd.notna(prev_cross) and pd.notna(now_cross):
            if prev_cross <= 0 and now_cross > 0:
                return "BUY"
            if prev_cross >= 0 and now_cross < 0:
                return "SELL"
        return None
# En src/strategy.py (añadir debajo de MACrossover)
import pandas as pd

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

    def signal(self, df: pd.DataFrame) -> str | None:
        rsi = self.rsi(df["close"])
        r0, r1 = rsi.iloc[-2], rsi.iloc[-1]
        # BUY cuando cruza hacia arriba nivel de sobreventa
        if r0 <= self.buy_level and r1 > self.buy_level:
            return "BUY"
        # SELL cuando cruza hacia abajo nivel de sobrecompra
        if r0 >= self.sell_level and r1 < self.sell_level:
            return "SELL"
        return None


class MACDStrategy:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_p = signal

    def signal(self, df: pd.DataFrame) -> str | None:
        close = df["close"]
        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig = macd.ewm(span=self.signal_p, adjust=False).mean()
        hist_prev, hist_curr = (macd - sig).iloc[-2], (macd - sig).iloc[-1]
        if hist_prev <= 0 and hist_curr > 0:
            return "BUY"
        if hist_prev >= 0 and hist_curr < 0:
            return "SELL"
        return None


class BollingerStrategy:
    def __init__(self, window: int = 20, k: float = 2.0):
        self.window = window
        self.k = k

    def signal(self, df: pd.DataFrame) -> str | None:
        close = df["close"]
        ma = close.rolling(self.window).mean()
        std = close.rolling(self.window).std()
        upper = ma + self.k * std
        lower = ma - self.k * std
        c0, c1 = close.iloc[-2], close.iloc[-1]
        u0, u1 = upper.iloc[-2], upper.iloc[-1]
        l0, l1 = lower.iloc[-2], lower.iloc[-1]
        # Reversión a la media: si sale de banda inferior → BUY; si sale de superior → SELL
        if c0 <= l0 and c1 > l1:
            return "BUY"
        if c0 >= u0 and c1 < u1:
            return "SELL"
        return None
