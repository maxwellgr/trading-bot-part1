# src/broker_alpaca.py
import requests
from typing import Dict, Any, List
from .config import settings


def _headers() -> Dict[str, str]:
    return {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }


class BrokerAlpaca:
    def __init__(self):
        self.base = settings.alpaca_base_url
        self.data_base = settings.alpaca_data_url

    def get_account(self) -> Dict[str, Any]:
        """Devuelve info de la cuenta (Paper)."""
        r = requests.get(f"{self.base}/v2/account", headers=_headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def get_bars(
        self,
        symbol: str,
        timeframe: str = "1Min",
        limit: int = 120,
    ) -> List[Dict[str, Any]]:
        """Obtiene barras históricas de Alpaca Data API (Paper)."""
        if not self.data_base:
            raise ValueError(
                "ALPACA_DATA_URL no configurada en .env para obtener barras."
            )

        params = {"timeframe": timeframe, "limit": limit, "feed": "iex"}  # feed gratuito
        r = requests.get(
            f"{self.data_base}/stocks/{symbol}/bars",
            headers=_headers(),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("bars", [])
