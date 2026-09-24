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


def portfolio_main(argv: List[str]) -> int:
    """
    Backtest de PORTAFOLIO con la estrategia y el riesgo ACTUALES de run_paper
    (sin parámetros de estrategia a propósito: v1 mide, no optimiza).
    Ver src/backtest_engine.py para el modelo temporal y de fills.
    """
    from pathlib import Path

    from .backtest_engine import BacktestConfig, run_backtest
    from .backtest_report import format_report, summarize, to_json, write_outputs
    from .historical_data import HistoricalDataError, load_universe
    from .historical_download import date_range_utc

    p = argparse.ArgumentParser(
        prog="python -m src.backtest",
        description="Backtest de portafolio multi-símbolo con la config de producción de run_paper "
                    "(datos locales; nunca envía órdenes). Para el backtester simple de un CSV usa --file.")
    p.add_argument("--symbols", help="Lista separada por comas, en el orden del loop en vivo (desempate).")
    p.add_argument("--timeframe", default="1Min")
    p.add_argument("--start", help="Fecha NY inclusiva YYYY-MM-DD")
    p.add_argument("--end", help="Fecha NY inclusiva YYYY-MM-DD")
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--initial-equity", type=float, default=100_000.0)
    p.add_argument("--commission", type=float, default=0.0, help="USD fijos por fill (0 = como Alpaca).")
    p.add_argument("--slippage-bps", type=float, default=5.0,
                   help="Slippage sobre la apertura de la vela de ejecución (default 5 bps = el que asume el RiskManager).")
    p.add_argument("--output-dir", type=Path, help="Escribe trades.csv/json, daily_results.csv, equity_curve.csv, summary.json")
    p.add_argument("--json", action="store_true", help="Resumen en JSON por stdout")
    p.add_argument("--validate-session", type=Path, metavar="SESSION_JSONL",
                   help="Reproduce la ventana de una sesión de paper grabada y compara señales/riesgo/ciclo de vida.")
    a = p.parse_args(argv)

    try:
        if a.validate_session:
            from .backtest_validation import format_validation, validate_session
            v = validate_session(a.validate_session, a.data_dir, a.slippage_bps, a.commission)
            print(to_json(v) if a.json else format_validation(v))
            return 0
        if not (a.symbols and a.start and a.end):
            p.error("--symbols, --start y --end son obligatorios (o usa --validate-session / --file)")
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
        s_utc, e_utc = date_range_utc(a.start, a.end)
        # Velas previas al inicio solo como ventana de indicadores (en vivo: hasta 24 h hacia atrás).
        data = load_universe(a.data_dir, a.timeframe, symbols, s_utc - pd.Timedelta(days=4), e_utc)
    except HistoricalDataError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    cfg = BacktestConfig(symbols=symbols, timeframe=a.timeframe, start=a.start, end=a.end,
                         initial_equity=a.initial_equity, slippage_bps=a.slippage_bps, commission=a.commission)
    result = run_backtest(cfg, data.bars)
    result.warnings.extend(data.warnings)
    summary = summarize(result)
    print(to_json(summary) if a.json else format_report(summary))
    if a.output_dir:
        paths = write_outputs(result, summary, a.output_dir)
        if not a.json:
            print()
            print("Archivos: " + ", ".join(str(x) for x in paths))
    return 0


def legacy_main(argv: List[str]) -> None:
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
    args = parser.parse_args(argv)

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


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--file" in argv:
        legacy_main(argv)
        return 0
    return portfolio_main(argv)


if __name__ == "__main__":
    sys.exit(main())
