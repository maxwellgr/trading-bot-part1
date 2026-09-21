# src/broker_alpaca.py
import requests
from typing import Dict, Any, List
from .config import settings
from .broker_base import BrokerBase


def _headers() -> Dict[str, str]:
    return {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }


class BrokerAlpaca(BrokerBase):
    def __init__(self):
        self.base = settings.alpaca_base_url
        self.data_base = settings.alpaca_data_url

    def get_account(self) -> Dict[str, Any]:
        """Devuelve info de la cuenta (Paper)."""
        r = requests.get(f"{self.base}/v2/account", headers=_headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 120, start_iso: str | None = None):
        params = {"timeframe": timeframe, "limit": limit, "feed": "iex"}  # feed gratuito
        if start_iso:
            params["start"] = start_iso  # ISO8601, ej: 2025-09-21T13:00:00Z
        r = requests.get(
            f"{self.data_base}/stocks/{symbol}/bars",
            headers=_headers(),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("bars", [])
    def get_clock_is_open(self) -> bool:
        """Devuelve True si el mercado está abierto (según Alpaca)."""
        r = requests.get(f"{self.base}/v2/clock", headers=_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        return bool(data.get("is_open", False))

    def get_asset_tradable(self, symbol: str) -> bool:
        """Comprueba si el símbolo es 'tradable' en Alpaca."""
        r = requests.get(f"{self.base}/v2/assets/{symbol}", headers=_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        return bool(data.get("tradable", False))
   
    def get_asset_shortable(self, symbol: str) -> bool:
        """Devuelve True si el símbolo se puede shortear en Alpaca."""
        r = requests.get(f"{self.base}/v2/assets/{symbol}", headers=_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        # Algunos planes exigen 'easy_to_borrow' además de 'shortable'
        return bool(data.get("shortable", False))


    def get_position_qty(self, symbol: str) -> int:
        """Devuelve la cantidad actual (entera) en la posición del símbolo; 0 si no hay."""
        r = requests.get(f"{self.base}/v2/positions/{symbol}", headers=_headers(), timeout=15)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        data = r.json()
        # qty viene como string; convertir a int redondeando hacia abajo
        try:
            return int(float(data.get("qty", "0")))
        except Exception:
            return 0

    def cancel_open_orders(self, symbol: str) -> None:
        """Cancela órdenes abiertas del símbolo (por higiene antes de mandar otra)."""
        r = requests.get(f"{self.base}/v2/orders", headers=_headers(), params={"status": "open", "symbols": symbol}, timeout=15)
        r.raise_for_status()
        for o in r.json():
            oid = o.get("id")
            if oid:
                requests.delete(f"{self.base}/v2/orders/{oid}", headers=_headers(), timeout=15)

    def place_order_market(self, symbol: str, side: str, qty: int, tif: str = "day") -> dict:
        """Envía una orden a mercado simple."""
        payload = {
            "symbol": symbol,
            "side": side,               # "buy" | "sell"
            "type": "market",
            "time_in_force": tif,       # "day" o "gtc"
            "qty": str(qty),
        }
        r = requests.post(f"{self.base}/v2/orders", headers=_headers(), json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    # NOTA: se eliminó place_order_bracket() — nunca se usaba (el bot gestiona
    # stop/trailing/take-profit por software vía risk_manager_avanzado.py y
    # el polling en run_paper.py, porque el trailing ATR es dinámico y no se
    # puede expresar como una orden bracket estática). La versión anterior
    # además estaba rota: la API de brackets de Alpaca espera limit_price /
    # stop_price absolutos, no offsets porcentuales como se pasaban aquí.

    # ---------- Implementación de BrokerBase ----------
    def get_positions(self) -> List[Dict[str, Any]]:
        """Lista de posiciones abiertas tal cual las reporta Alpaca."""
        r = requests.get(f"{self.base}/v2/positions", headers=_headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def place_order(self, symbol: str, side: str, qty: int, order_type: str = "market") -> Dict[str, Any]:
        """Implementación genérica de BrokerBase; delega en place_order_market (única soportada hoy)."""
        if order_type != "market":
            raise NotImplementedError(f"order_type={order_type!r} no soportado; usa place_order_market().")
        return self.place_order_market(symbol, side, qty)

