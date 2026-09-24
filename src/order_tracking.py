# src/order_tracking.py
"""
Seguimiento de órdenes enviadas hasta que Alpaca confirma su estado final.

Regla contable: el P&L realizado sale SOLO de filled_qty / filled_avg_price
reportados por Alpaca. La respuesta del POST (casi siempre pending_new, con
filled_qty=0) no confirma nada; por eso cada orden queda registrada aquí y se
reconcilia en polls posteriores hasta llegar a un estado terminal.

Idempotencia: cada orden recuerda cuánto ya se contabilizó (accounted_qty y
accounted_notional). Un snapshot solo aporta la DIFERENCIA respecto de lo
contabilizado, así que ver el mismo fill dos veces no lo cuenta dos veces.

Módulo puro: no llama al broker ni conoce el position_book.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Estados de Alpaca tras los cuales la orden ya no puede recibir más fills.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})

# Propósito de las órdenes que abren posición; el resto reduce/cierra.
ENTRY_PURPOSE = "entry"


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_alpaca_ts(value: Any) -> Optional[datetime]:
    """Alpaca devuelve nanosegundos ("...39.724088489Z"); se recortan a microsegundos."""
    if not isinstance(value, str) or not value:
        return None
    s = value.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class FillDelta:
    """Lo que un snapshot aporta de NUEVO respecto de lo ya contabilizado."""
    status: Optional[str]
    changed: bool           # cambió el status o la cantidad llenada
    terminal: bool
    new_qty: float          # acciones llenadas desde el snapshot anterior
    new_notional: float     # importe de esas acciones (new_qty * su precio)
    fill_price: Optional[float]  # precio medio de SOLO las acciones nuevas


@dataclass
class TrackedOrder:
    order_id: str
    symbol: str
    side: str                    # "buy" | "sell"
    purpose: str                 # "entry" | "scale_out" | "stop_hit" | ...
    position_side: str           # "LONG" | "SHORT" (posición que abre/reduce)
    requested_qty: float
    submitted_at: Optional[str] = None
    bar_timestamp: Optional[str] = None
    cost_basis: Optional[float] = None  # precio de fill confirmado de la entrada (solo salidas)
    status: Optional[str] = None
    accounted_qty: float = 0.0
    accounted_notional: float = 0.0
    filled_avg_price: Optional[float] = None

    @property
    def is_entry(self) -> bool:
        return self.purpose == ENTRY_PURPOSE

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def apply(self, order: Dict[str, Any]) -> FillDelta:
        """
        Incorpora un snapshot de GET /v2/orders/{id}. filled_avg_price de
        Alpaca es la media acumulada de TODO lo llenado, así que el precio de
        las acciones nuevas es (notional_total - notional_ya_contado) / nuevas.
        """
        status = order.get("status") or self.status
        filled = _to_float(order.get("filled_qty"))
        avg = order.get("filled_avg_price")
        avg_f = _to_float(avg) if avg not in (None, "") else None

        new_qty = 0.0
        new_notional = 0.0
        fill_price = None
        # filled_qty nunca debería bajar; si lo hiciera, no se "des-contabiliza" nada.
        if filled > self.accounted_qty and avg_f is not None:
            new_qty = filled - self.accounted_qty
            new_notional = filled * avg_f - self.accounted_notional
            fill_price = new_notional / new_qty
            self.accounted_qty = filled
            self.accounted_notional = filled * avg_f
            self.filled_avg_price = avg_f

        changed = new_qty > 0 or status != self.status
        self.status = status
        return FillDelta(status=status, changed=changed, terminal=self.terminal,
                         new_qty=new_qty, new_notional=new_notional, fill_price=fill_price)

    def summary(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id, "symbol": self.symbol, "side": self.side, "purpose": self.purpose,
            "requested_qty": self.requested_qty, "status": self.status,
            "accounted_filled_qty": self.accounted_qty, "submitted_at": self.submitted_at,
        }


class OrderTracker:
    """Órdenes aún no terminales (o terminales todavía no aplicadas). Solo en memoria."""

    def __init__(self) -> None:
        self._orders: Dict[str, TrackedOrder] = {}

    def add(self, order: TrackedOrder) -> None:
        self._orders[order.order_id] = order

    def remove(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def get(self, order_id: str) -> Optional[TrackedOrder]:
        return self._orders.get(order_id)

    def open_orders(self, symbol: Optional[str] = None) -> List[TrackedOrder]:
        return [o for o in self._orders.values() if symbol is None or o.symbol == symbol]

    def has_open(self, symbol: str) -> bool:
        return any(o.symbol == symbol for o in self._orders.values())

    def __len__(self) -> int:
        return len(self._orders)
