from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple
import statistics
from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class RiskDecision:
    allow: bool
    qty: int = 0
    entry: Optional[float] = None
    stop: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeRecord:
    symbol: str
    side: Side
    qty: int
    entry: float
    stop: float
    take_profit: Optional[float]
    pnl: float
    closed_at: Optional[str] = None


@dataclass
class RiskConfig:
    # Asignación/Riesgo
    account_risk_pct: float = 0.01       # % de equity por trade (Fixed Fractional)
    max_portfolio_heat_pct: float = 0.2  # suma de riesgos abiertos / equity
    max_positions: int = 6               # posiciones simultáneas
    max_positions_per_symbol: int = 1
    max_leverage: float = 1.5            # equity * leverage >= gross exposure

    # Límites diarios/semanales
    daily_loss_limit_pct: float = 0.03   # detiene el día si equity cae X%
    max_consecutive_losses: int = 3      # corta si pierde N seguidas

    # Stops/Targets
    default_sl_pct: float = 0.02
    default_tp_pct: float = 0.04
    use_atr_based_stop: bool = True
    atr_multiple_sl: float = 1.5
    atr_multiple_tp: float = 3.0
    trailing_atr_multiple: Optional[float] = 2.0  # None para apagar trailing ATR

    # Validación de señal
    min_rr: float = 2.0                  # riesgo/beneficio mínimo
    min_liquidity_dollar: float = 1_000_000  # volumen $ promedio mínimo

    # Slippage y fees (para simulación y sizing más conservador)
    fee_per_share: float = 0.0
    slippage_pct: float = 0.0005         # 5 bps

    # Redondeos/mercado
    lot_size: int = 1                     # acciones; para cripto/futuros ajusta
    price_precision: int = 2

    # Control de exposición por símbolo/sector (básico)
    max_symbol_exposure_pct: float = 0.1  # exposición bruta por símbolo / equity

    # Ventanas
    atr_window: int = 14
    liq_window: int = 20


class RiskManager:
    """
    RiskManager avanzado, desacoplado del broker/estrategia, con:
    - Sizing por % de cuenta y stops ATR/porcentaje
    - Validación R:R mínimo
    - Límites de calor de portafolio, pérdidas diarias y racha negativa
    - Trailing stop basado en ATR
    - Límites de exposición, posiciones y apalancamiento
    - Considera fees/slippage al calcular R y tamaño

    Interfaz esperada del adaptador (inyéctalo en el constructor):
      adapter.get_equity() -> float
      adapter.get_open_positions() -> List[Dict]  # cada dict: {symbol, qty, avg_price, side, stop?}
      adapter.get_open_orders() -> List[Dict]     # opcional
      adapter.round_qty(qty: float, lot_size: int) -> int  # opcional

    Datos de mercado esperados en assess_entry():
      bars: Dict con claves "close", "high", "low", "volume" como listas (más reciente al final)
    """

    def __init__(self, config: RiskConfig, adapter):
        self.cfg = config
        self.adapter = adapter
        self.consecutive_losses = 0
        self.day_start_equity: Optional[float] = None
        self.trades: List[TradeRecord] = []

    # ---------- Utils ----------
    @staticmethod
    def _atr(highs: List[float], lows: List[float], closes: List[float], window: int) -> Optional[float]:
        if len(highs) < window + 1 or len(lows) < window + 1 or len(closes) < window + 1:
            return None
        trs = []
        for i in range(1, window + 1):
            h, l, pc = highs[-i], lows[-i], closes[-(i+1)]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return statistics.fmean(trs)

    @staticmethod
    def _sma(values: List[float], window: int) -> Optional[float]:
        if len(values) < window:
            return None
        return statistics.fmean(values[-window:])

    @staticmethod
    def _round_price(p: float, precision: int) -> float:
        return round(p, precision)

    def _round_qty(self, q: float) -> int:
        if hasattr(self.adapter, "round_qty"):
            return int(self.adapter.round_qty(q, self.cfg.lot_size))
        # Fallback simple por lotes
        return max(self.cfg.lot_size, int(q // self.cfg.lot_size * self.cfg.lot_size))

    # ---------- Estado diario ----------
    def start_of_day(self):
        self.day_start_equity = self.adapter.get_equity()
        self.consecutive_losses = 0

    def _daily_loss_limit_hit(self) -> bool:
        if self.day_start_equity is None:
            self.start_of_day()
        eq = self.adapter.get_equity()
        threshold = self.day_start_equity * (1 - self.cfg.daily_loss_limit_pct)
        return eq <= threshold

    def _portfolio_heat(self) -> float:
        equity = self.adapter.get_equity()
        if equity <= 0:
            return 1.0
        open_positions = self.adapter.get_open_positions() or []
        # Riesgo aproximado = qty * (entry - stop) (absoluto), si stop no existe, usa default %
        total_risk = 0.0
        for p in open_positions:
            entry = p.get("avg_price", 0.0)
            stop = p.get("stop", None)
            if not stop:
                stop = entry * (1 - self.cfg.default_sl_pct) if p.get("side") == Side.LONG else entry * (1 + self.cfg.default_sl_pct)
            risk_per_share = abs(entry - stop)
            total_risk += risk_per_share * abs(p.get("qty", 0))
        return total_risk / equity

    def _gross_exposure(self) -> float:
        open_positions = self.adapter.get_open_positions() or []
        exposure = 0.0
        for p in open_positions:
            exposure += abs(p.get("qty", 0) * p.get("avg_price", 0.0))
        return exposure

    def _symbol_exposure_pct(self, symbol: str) -> float:
        equity = self.adapter.get_equity()
        if equity <= 0:
            return 1.0
        open_positions = self.adapter.get_open_positions() or []
        exposure = 0.0
        for p in open_positions:
            if p.get("symbol") == symbol:
                exposure += abs(p.get("qty", 0) * p.get("avg_price", 0.0))
        return exposure / equity

    # ---------- Validaciones previas ----------
    def _basic_guards(self, symbol: str) -> Optional[str]:
        positions = self.adapter.get_open_positions() or []
        if len(positions) >= self.cfg.max_positions:
            return f"Max posiciones ({self.cfg.max_positions})"
        if sum(1 for p in positions if p.get("symbol") == symbol) >= self.cfg.max_positions_per_symbol:
            return f"Max por símbolo ({self.cfg.max_positions_per_symbol}) en {symbol}"
        if self._daily_loss_limit_hit():
            return "Límite de pérdida diaria alcanzado"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return f"Racha negativa {self.consecutive_losses} >= {self.cfg.max_consecutive_losses}"
        if self._portfolio_heat() >= self.cfg.max_portfolio_heat_pct:
            return "Calor de portafolio excedido"
        gross = self._gross_exposure()
        if gross >= self.adapter.get_equity() * self.cfg.max_leverage:
            return "Apalancamiento máximo excedido"
        if self._symbol_exposure_pct(symbol) >= self.cfg.max_symbol_exposure_pct:
            return f"Exposición por símbolo excedida en {symbol}"
        return None

    # ---------- Liquidez ----------
    def _estimate_liquidity_dollar(self, closes: List[float], volumes: List[float], window: int) -> Optional[float]:
        if len(closes) < window or len(volumes) < window:
            return None
        liq = [closes[-i] * volumes[-i] for i in range(1, window + 1)]
        return statistics.fmean(liq)

    # ---------- API principal ----------
    def assess_entry(
        self,
        symbol: str,
        side: Side,
        price: float,
        bars: Dict[str, List[float]],
        custom_stop: Optional[float] = None,
        custom_take_profit: Optional[float] = None,
    ) -> RiskDecision:
        """
        Decide si permitir una entrada y con qué tamaño/stop/tp.
        - price: precio de ejecución estimado (se ajusta por slippage)
        - bars: debe incluir listas para 'close','high','low','volume'
        """
        guard = self._basic_guards(symbol)
        if guard:
            return RiskDecision(False, reason=guard)

        closes = bars.get("close", [])
        highs = bars.get("high", [])
        lows = bars.get("low", [])
        vols = bars.get("volume", [])

        atr = self._atr(highs, lows, closes, self.cfg.atr_window) if self.cfg.use_atr_based_stop else None
        liq = self._estimate_liquidity_dollar(closes, vols, self.cfg.liq_window)
        if liq is not None and liq < self.cfg.min_liquidity_dollar:
            return RiskDecision(False, reason=f"Liquidez insuficiente (${liq:,.0f} < {self.cfg.min_liquidity_dollar:,.0f})")

        # Ajuste por slippage
        slip = price * self.cfg.slippage_pct
        est_entry = price + slip if side == Side.LONG else price - slip

        # Determinar stop/tp
        if custom_stop is not None:
            stop = custom_stop
        elif atr is not None:
            stop = est_entry - self.cfg.atr_multiple_sl * atr if side == Side.LONG else est_entry + self.cfg.atr_multiple_sl * atr
        else:
            stop = est_entry * (1 - self.cfg.default_sl_pct) if side == Side.LONG else est_entry * (1 + self.cfg.default_sl_pct)

        if custom_take_profit is not None:
            tp = custom_take_profit
        elif atr is not None:
            tp = est_entry + self.cfg.atr_multiple_tp * atr if side == Side.LONG else est_entry - self.cfg.atr_multiple_tp * atr
        else:
            tp = est_entry * (1 + self.cfg.default_tp_pct) if side == Side.LONG else est_entry * (1 - self.cfg.default_tp_pct)

        # Riesgo/beneficio esperado
        risk_per_share = abs(est_entry - stop)
        reward_per_share = abs(tp - est_entry)
        # considerar fees ida y vuelta
        roundtrip_fees = 2 * self.cfg.fee_per_share
        eff_reward = max(0.0, reward_per_share - roundtrip_fees - slip)
        eff_risk = max(1e-9, risk_per_share + roundtrip_fees + slip)
        rr = eff_reward / eff_risk
        if rr < self.cfg.min_rr:
            return RiskDecision(False, reason=f"RR {rr:.2f} < min {self.cfg.min_rr}")

        # Sizing por % de equity
        equity = self.adapter.get_equity()
        capital_risk = equity * self.cfg.account_risk_pct
        if eff_risk <= 0:
            return RiskDecision(False, reason="Riesgo por acción inválido")
        raw_qty = capital_risk / eff_risk
        qty = self._round_qty(raw_qty)

        if qty <= 0:
            return RiskDecision(False, reason="Qty calculada = 0")

        # Validar exposición y apalancamiento con la nueva posición
        new_exposure = self._gross_exposure() + qty * est_entry
        if new_exposure > equity * self.cfg.max_leverage:
            return RiskDecision(False, reason="Apalancamiento excedido con nueva posición")

        # Redondeos finales
        stop = self._round_price(stop, self.cfg.price_precision)
        tp = self._round_price(tp, self.cfg.price_precision)
        est_entry = self._round_price(est_entry, self.cfg.price_precision)

        meta = {
            "atr": atr,
            "liq": liq,
            "rr": rr,
            "capital_risk": capital_risk,
            "risk_per_share": risk_per_share,
        }
        return RiskDecision(True, qty=qty, entry=est_entry, stop=stop, take_profit=tp, reason="OK", meta=meta)

    # ---------- Gestión durante la posición ----------
    def update_trailing_stop(self, side: Side, current_price: float, stop: float, bars: Dict[str, List[float]]) -> float:
        if self.cfg.trailing_atr_multiple is None:
            return stop
        highs = bars.get("high", [])
        lows = bars.get("low", [])
        closes = bars.get("close", [])
        atr = self._atr(highs, lows, closes, self.cfg.atr_window)
        if atr is None:
            return stop
        if side == Side.LONG:
            new_stop = max(stop, current_price - self.cfg.trailing_atr_multiple * atr)
        else:
            new_stop = min(stop, current_price + self.cfg.trailing_atr_multiple * atr)
        return self._round_price(new_stop, self.cfg.price_precision)

    # ---------- Registro de resultados ----------
    def record_close(self, symbol: str, side: Side, qty: int, entry: float, stop: float, take_profit: Optional[float], pnl: float):
        self.trades.append(TradeRecord(symbol, side, qty, entry, stop, take_profit, pnl))
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # ---------- Helper de integración ----------
    def should_halt_trading(self) -> Tuple[bool, str]:
        if self._daily_loss_limit_hit():
            return True, "Límite diario alcanzado"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return True, "Racha negativa"
        if self._portfolio_heat() >= self.cfg.max_portfolio_heat_pct:
            return True, "Calor de portafolio"
        return False, "OK"


# ------------------ Ejemplo opcional de Adapter mínimo ------------------
class SimpleAdapter:
    def __init__(self):
        self._equity = 10_000.0
        self._positions: List[Dict[str, Any]] = []

    def get_equity(self) -> float:
        return self._equity

    def get_open_positions(self) -> List[Dict[str, Any]]:
        return self._positions

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return []

    def round_qty(self, qty: float, lot_size: int) -> int:
        return max(lot_size, int(qty // lot_size * lot_size))

