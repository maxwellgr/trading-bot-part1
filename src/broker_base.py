# src/broker_base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, List

class BrokerBase(ABC):
    @abstractmethod
    def get_account(self) -> Dict[str, Any]:
        """Devuelve info de la cuenta (cash, equity, etc.)."""
        ...

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """Lista de posiciones abiertas."""
        ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 120) -> List[Dict[str, Any]]:
        """Barras OHLCV para un símbolo."""
        ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int, order_type: str = "market") -> Dict[str, Any]:
        """Envía una orden y devuelve el objeto de orden."""
        ...
