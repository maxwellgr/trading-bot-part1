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
