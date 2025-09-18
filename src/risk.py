# src/risk.py
from .config import settings
from datetime import datetime, timezone

class RiskManager:
    def __init__(self, equity: float):
        self.equity = float(equity)
        self.daily_max_loss_pct = float(settings.daily_max_loss_pct)
        self.position_risk_pct = float(settings.position_risk_pct)
        self.daily_pnl = 0.0
        self._day = self._today_utc()

    def _today_utc(self):
        return datetime.now(timezone.utc).date()

    def reset_if_new_day(self):
        today = self._today_utc()
        if today != self._day:
            self._day = today
            self.daily_pnl = 0.0

    def size(self, price: float) -> int:
        """Tamaño de posición por orden usando % de equity."""
        if price <= 0:
            return 0
        risk_capital = self.equity * (self.position_risk_pct / 100.0)
        qty = int(risk_capital // price)
        return max(1, qty)

    def can_trade(self) -> bool:
        limit = -self.equity * (self.daily_max_loss_pct / 100.0)
        return self.daily_pnl > limit

    def update_pnl(self, realized_pnl: float):
        self.daily_pnl += float(realized_pnl)
