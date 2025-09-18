# src/backtest.py
import argparse
import pandas as pd
from .data import load_csv
from .strategy import MACrossover
from .metrics import equity_to_returns, sharpe_ratio, max_drawdown, total_return

class Backtester:
    def __init__(self, df: pd.DataFrame, cash: float = 10_000.0, fee: float = 0.0):
        self.df = df.copy()
        self.cash = float(cash)
        self.shares = 0
        self.fee = float(fee)
        self.equity_curve = []

    def run(self, strategy: MACrossover):
        for ts, row in self.df.iterrows():
            price = float(row["close"])
            sig = strategy.signal(self.df.loc[:ts])
            # Ejecuta operaciones simples long-only
            if sig == "BUY" and self.shares == 0:
                qty = int(self.cash // price)
                if qty > 0:
                    self.cash -= qty * price + self.fee
                    self.shares += qty
            elif sig == "SELL" and self.shares > 0:
                self.cash += self.shares * price - self.fee
                self.shares = 0
            equity = self.cash + self.shares * price
            self.equity_curve.append((ts, equity))
        curve = pd.DataFrame(self.equity_curve, columns=["timestamp", "equity"]).set_index("timestamp")
        return curve

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
    parser = argparse.ArgumentParser(description="Backtester MACrossover con métricas")
    parser.add_argument("--file", required=True, help="CSV: timestamp, open, high, low, close, volume")
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--fee", type=float, default=0.0)
    parser.add_argument("--fast", type=int, default=10)
    parser.add_argument("--slow", type=int, default=30)
    parser.add_argument("--steps-per-year", type=int, default=0, help="Override de anualización (0 = inferir)")
    args = parser.parse_args()

    df = load_csv(args.file)
    bt = Backtester(df, cash=args.cash, fee=args.fee)
    curve = bt.run(MACrossover(fast=args.fast, slow=args.slow))
    rets = equity_to_returns(curve["equity"])
    spy = args.steps_per_year or _infer_steps_per_year(curve)

    tr = total_return(curve["equity"])
    sr = sharpe_ratio(rets, steps_per_year=spy, risk_free=0.0)
    mdd = max_drawdown(curve["equity"])

    print(f"Total return: {tr:.2%}")
    print(f"Sharpe ratio: {sr:.2f}  (steps_per_year={spy})")
    print(f"Max drawdown: {mdd:.2%}")
    print(f"Puntos en curva: {len(curve)}")

