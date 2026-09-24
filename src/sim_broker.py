# src/sim_broker.py
"""
Broker/portafolio SIMULADO para el backtester. No hace red y no conoce a
Alpaca: nunca puede enviar una orden real ni de paper.

Modelo de ejecución (v1, determinista):
- Solo órdenes de mercado, largo únicamente (los shorts están deshabilitados
  por defecto en producción; el motor rechaza configs con shorts).
- Una orden decidida con la vela N (a su cierre) queda PENDIENTE y se llena
  entera en la APERTURA de la siguiente vela de ese símbolo, más slippage:
      compra: open * (1 + bps/10_000)     venta: open * (1 - bps/10_000)
- Comisión fija opcional en USD por fill (0 por defecto, como Alpaca).
- Sin fills parciales ni aleatorios en v1.

P&L realizado por fill de venta = (precio_fill - costo_base) * qty - comisión.
La comisión de la compra se registra como P&L realizado negativo en su fill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SimOrder:
    order_id: str
    symbol: str
    side: str                 # "buy" | "sell"
    qty: int
    purpose: str              # "entry" | "scale_out" | "stop_hit" | "take_profit_hit" | "signal_exit" | "giveback_close"
    signal_bar_ts: str        # vela cuyo cierre originó la decisión
    decision_ts: str          # momento en que el bot en vivo la habría decidido (cierre de esa vela)


@dataclass
class SimFill:
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float              # con slippage
    reference_open: float     # open de la vela de ejecución (sin slippage)
    fill_ts: str              # timestamp (inicio) de la vela de ejecución
    purpose: str
    signal_bar_ts: str
    decision_ts: str
    commission: float
    realized_pnl: float       # 0/−comisión en compras; (px−base)*qty−comisión en ventas
    cost_basis: float         # costo base de la posición al momento del fill
    delay_seconds: float      # fill_ts − decision_ts


@dataclass
class SimPosition:
    qty: int = 0
    cost_basis: float = 0.0


class SimBroker:
    def __init__(self, initial_cash: float, slippage_bps: float = 0.0, commission: float = 0.0):
        if slippage_bps < 0 or commission < 0:
            raise ValueError("slippage_bps y commission deben ser >= 0")
        self.cash = float(initial_cash)
        self.slippage_bps = float(slippage_bps)
        self.commission = float(commission)
        self.positions: Dict[str, SimPosition] = {}
        self.pending: Dict[str, List[SimOrder]] = {}
        self.last_close: Dict[str, float] = {}
        self.orders: List[SimOrder] = []
        self.fills: List[SimFill] = []
        self._seq = 0

    # ---------------- órdenes ----------------
    def submit(self, symbol: str, side: str, qty: int, purpose: str, signal_bar_ts: str, decision_ts: str) -> SimOrder:
        if side not in ("buy", "sell") or int(qty) <= 0:
            raise ValueError(f"Orden inválida: {side} {qty}")
        self._seq += 1
        order = SimOrder(f"sim{self._seq}", symbol, side, int(qty), purpose, signal_bar_ts, decision_ts)
        self.pending.setdefault(symbol, []).append(order)
        self.orders.append(order)
        return order

    def has_pending(self, symbol: str) -> bool:
        return bool(self.pending.get(symbol))

    def pending_orders(self) -> List[SimOrder]:
        return [o for orders in self.pending.values() for o in orders]

    def fill_price(self, side: str, reference: float) -> float:
        adj = reference * self.slippage_bps / 10_000.0
        return reference + adj if side == "buy" else reference - adj

    def fill_pending(self, symbol: str, bar_ts: str, bar_open: float, delay_of) -> List[SimFill]:
        """Llena, en orden de envío, las órdenes pendientes del símbolo a la apertura de esta vela."""
        fills = []
        for order in self.pending.pop(symbol, []):
            fills.append(self._execute(order, bar_ts, bar_open, delay_of(order.decision_ts, bar_ts)))
        return fills

    def _execute(self, order: SimOrder, bar_ts: str, bar_open: float, delay_seconds: float) -> SimFill:
        px = self.fill_price(order.side, bar_open)
        pos = self.positions.setdefault(order.symbol, SimPosition())
        if order.side == "buy":
            new_qty = pos.qty + order.qty
            pos.cost_basis = (pos.cost_basis * pos.qty + px * order.qty) / new_qty
            pos.qty = new_qty
            self.cash -= px * order.qty + self.commission
            realized = -self.commission
        else:
            if order.qty > pos.qty:
                raise ValueError(f"{order.symbol}: venta de {order.qty} con posición de {pos.qty} (shorts no soportados en v1)")
            realized = (px - pos.cost_basis) * order.qty - self.commission
            pos.qty -= order.qty
            self.cash += px * order.qty - self.commission
        fill = SimFill(order.order_id, order.symbol, order.side, order.qty, px, bar_open, bar_ts, order.purpose,
                       order.signal_bar_ts, order.decision_ts, self.commission, realized, pos.cost_basis, delay_seconds)
        if pos.qty == 0:
            self.positions.pop(order.symbol, None)
        self.fills.append(fill)
        return fill

    # ---------------- valuación ----------------
    def mark(self, symbol: str, close: float) -> None:
        self.last_close[symbol] = float(close)

    def position_qty(self, symbol: str) -> int:
        p = self.positions.get(symbol)
        return p.qty if p else 0

    def cost_basis(self, symbol: str) -> Optional[float]:
        p = self.positions.get(symbol)
        return p.cost_basis if p else None

    def market_value(self) -> float:
        return sum(p.qty * self.last_close.get(s, p.cost_basis) for s, p in self.positions.items())

    def unrealized_pnl(self) -> float:
        return sum(p.qty * (self.last_close.get(s, p.cost_basis) - p.cost_basis) for s, p in self.positions.items())

    def equity(self) -> float:
        return self.cash + self.market_value()
