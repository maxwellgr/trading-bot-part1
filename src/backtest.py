# src/backtest.py
import argparse
import sys
from typing import List, Optional

import pandas as pd

# Windows: consola por defecto en cp1252, no UTF-8 (ver src/logger.py para más detalle).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .data import load_csv
from .strategy import MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy
from .metrics import (
    equity_to_returns,
    sharpe_ratio,
    sortino_ratio,
    calmar_ratio,
    max_drawdown,
    total_return,
    trade_stats,
)


class Backtester:
    """
    Backtester simple de una sola posición a la vez (sin apalancamiento,
    "todo dentro / todo fuera"), agnóstico a la estrategia: cualquier objeto
    con `.signal(df) -> "BUY"/"SELL"/None` sirve (MACrossover, RSIStrategy,
    MACDStrategy, BollingerStrategy...).

    Modela comisión fija por operación (`fee`) y slippage en puntos básicos
    (`slippage_bps`) sobre el precio de ejecución, y opcionalmente permite
    posiciones cortas (`allow_shorts`).

    Limitación conocida: NO usa el RiskManager avanzado (risk_manager_avanzado.py)
    — no valida RR mínimo, no dimensiona por % de riesgo ni aplica ATR
    stops/trailing. Sirve para validar la lógica de señales de una
    estrategia con costos realistas; el veredicto final sobre los
    parámetros de riesgo (--risk-per-trade, --atr-sl-mult, etc. en
    run_paper.py) sigue dependiendo de correr en paper trading.
    """

    def __init__(self, df: pd.DataFrame, cash: float = 10_000.0, fee: float = 0.0,
                 slippage_bps: float = 0.0, allow_shorts: bool = False):
        self.df = df.copy()
        self.cash = float(cash)
        self.fee = float(fee)
        self.slippage_bps = float(slippage_bps)
        self.allow_shorts = allow_shorts
        self.equity_curve: List[tuple] = []
        self.trades: List[float] = []  # PnL realizado (neto de fees/slippage) por trade cerrado

    def _exec_price(self, price: float, aggressor_side: str) -> float:
        """aggressor_side: 'buy' empeora el precio hacia arriba, 'sell' hacia abajo."""
        if self.slippage_bps <= 0:
            return price
        adj = price * (self.slippage_bps / 10_000.0)
        return price + adj if aggressor_side == "buy" else price - adj

    def run(self, strategy) -> pd.DataFrame:
        entry_side: Optional[str] = None  # "LONG" | "SHORT" | None
        entry_price = 0.0
        qty = 0

        for ts, row in self.df.iterrows():
            price = float(row["close"])
            sig = strategy.signal(self.df.loc[:ts])

            if sig == "BUY":
                if entry_side == "SHORT":
                    exec_price = self._exec_price(price, "buy")
                    pnl = (entry_price - exec_price) * qty - 2 * self.fee
                    self.cash += pnl
                    self.trades.append(pnl)
                    entry_side, qty = None, 0
                if entry_side is None:
                    q = int(self.cash // price)
                    if q > 0:
                        entry_price = self._exec_price(price, "buy")
                        self.cash -= self.fee
                        entry_side, qty = "LONG", q

            elif sig == "SELL":
                if entry_side == "LONG":
                    exec_price = self._exec_price(price, "sell")
                    pnl = (exec_price - entry_price) * qty - 2 * self.fee
                    self.cash += pnl
                    self.trades.append(pnl)
                    entry_side, qty = None, 0
                if entry_side is None and self.allow_shorts:
                    q = int(self.cash // price)
                    if q > 0:
                        entry_price = self._exec_price(price, "sell")
                        self.cash -= self.fee
                        entry_side, qty = "SHORT", q

            if entry_side == "LONG":
                open_pnl = (price - entry_price) * qty
            elif entry_side == "SHORT":
                open_pnl = (entry_price - price) * qty
            else:
                open_pnl = 0.0
            self.equity_curve.append((ts, self.cash + open_pnl))

        curve = pd.DataFrame(self.equity_curve, columns=["timestamp", "equity"]).set_index("timestamp")
        return curve


def build_strategy(args) -> object:
    st = args.strategy.lower()
    if st == "ma":
        return MACrossover(fast=args.fast, slow=args.slow)
    if st == "rsi":
        return RSIStrategy(period=args.rsi_period, buy_level=args.rsi_buy, sell_level=args.rsi_sell)
    if st == "macd":
        return MACDStrategy(fast=args.macd_fast, slow=args.macd_slow, signal=args.macd_signal)
    if st == "bbands":
        return BollingerStrategy(window=args.bb_window, k=args.bb_k)
    raise ValueError(f"Estrategia desconocida: {args.strategy}")


def _infer_steps_per_year(df: pd.DataFrame) -> int:
    """Heurística simple: si los timestamps están a ~1 día => 252; si son min => 252*390 (~98k).
    Puedes ajustar manualmente con --steps-per-year.
    """
    if len(df) < 3:
        return 252
    deltas = df.index.to_series().diff().dropna()
    median_sec = deltas.dt.total_seconds().median()
    if median_sec <= 120:  # ~minuteros
        return 252 * 390  # sesiones por año * minutos por sesión
    if median_sec <= 3600:  # ~horas
        return 252 * 6.5
    return 252  # diario


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtester con métricas — soporta las mismas estrategias que run_paper.py")
    parser.add_argument("--file", required=True, help="CSV: timestamp, open, high, low, close, volume")
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--fee", type=float, default=0.0, help="Comisión fija por lado de la operación (USD).")
    parser.add_argument("--slippage-bps", type=float, default=0.0, help="Slippage en puntos básicos sobre el precio de ejecución.")
    parser.add_argument("--allow-shorts", action="store_true", help="Permite abrir cortos ante señal SELL en flat.")
    parser.add_argument("--strategy", type=str, default="ma", choices=["ma", "rsi", "macd", "bbands"])
    # MA params
    parser.add_argument("--fast", type=int, default=10)
    parser.add_argument("--slow", type=int, default=30)
    # RSI params
    parser.add_argument("--rsi-period", type=int, default=14)
    parser.add_argument("--rsi-buy", type=float, default=30.0)
    parser.add_argument("--rsi-sell", type=float, default=70.0)
    # MACD params
    parser.add_argument("--macd-fast", type=int, default=12)
    parser.add_argument("--macd-slow", type=int, default=26)
    parser.add_argument("--macd-signal", type=int, default=9)
    # Bollinger params
    parser.add_argument("--bb-window", type=int, default=20)
    parser.add_argument("--bb-k", type=float, default=2.0)
    parser.add_argument("--steps-per-year", type=int, default=0, help="Override de anualización (0 = inferir)")
    args = parser.parse_args()

    if args.strategy == "ma" and args.fast >= args.slow:
        print("❌ Para estrategia 'ma', fast debe ser menor que slow (ej. --fast 10 --slow 30).")
        sys.exit(2)

    df = load_csv(args.file)
    bt = Backtester(df, cash=args.cash, fee=args.fee, slippage_bps=args.slippage_bps, allow_shorts=args.allow_shorts)
    curve = bt.run(build_strategy(args))
    rets = equity_to_returns(curve["equity"])
    spy = args.steps_per_year or _infer_steps_per_year(curve)

    tr = total_return(curve["equity"])
    sr = sharpe_ratio(rets, steps_per_year=spy, risk_free=0.0)
    sortino = sortino_ratio(rets, steps_per_year=spy, risk_free=0.0)
    calmar = calmar_ratio(curve["equity"], steps_per_year=spy)
    mdd = max_drawdown(curve["equity"])
    stats = trade_stats(bt.trades)

    print(f"Estrategia: {args.strategy}")
    print(f"Total return: {tr:.2%}")
    print(f"Sharpe ratio: {sr:.2f}  (steps_per_year={spy})")
    print(f"Sortino ratio: {sortino:.2f}")
    print(f"Calmar ratio: {calmar:.2f}")
    print(f"Max drawdown: {mdd:.2%}")
    print(f"Puntos en curva: {len(curve)}")
    print("--- Estadísticas por trade ---")
    print(f"N trades: {stats['n_trades']}")
    print(f"Win rate: {stats['win_rate']:.1%}")
    pf = stats["profit_factor"]
    print(f"Profit factor: {'inf' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"Expectancy/trade: ${stats['expectancy']:.2f}")
    print(f"Mejor / peor trade: ${stats['best_trade']:.2f} / ${stats['worst_trade']:.2f}")

    years = len(curve) / spy
    if years < 0.1 or stats["n_trades"] < 30:
        print(
            f"\n⚠️  Muestra corta (~{years:.3f} años, {stats['n_trades']} trades): las métricas "
            "anualizadas (Sharpe/Sortino/Calmar) y el win rate/profit factor pueden ser muy "
            "ruidosos o directamente engañosos. No tomes decisiones de riesgo sobre esto solo — "
            "usa un histórico más largo antes de fijar parámetros para paper/real."
        )
