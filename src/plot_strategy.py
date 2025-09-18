# src/plot_strategy.py
import argparse
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt

from .broker_alpaca import BrokerAlpaca
from .data import bars_to_df
from .strategy import MACrossover


def compute_signals(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    """
    Añade columnas:
      - ma_fast, ma_slow: medias móviles
      - signal: 'BUY'/'SELL'/None en cada barra cuando hay cruce
    """
    out = df.copy()
    out["ma_fast"] = out["close"].rolling(fast).mean()
    out["ma_slow"] = out["close"].rolling(slow).mean()

    prev_cross = out["ma_fast"].shift(1) - out["ma_slow"].shift(1)
    now_cross = out["ma_fast"] - out["ma_slow"]

    buy_idx = (prev_cross <= 0) & (now_cross > 0)
    sell_idx = (prev_cross >= 0) & (now_cross < 0)

    out["signal"] = None
    out.loc[buy_idx, "signal"] = "BUY"
    out.loc[sell_idx, "signal"] = "SELL"
    return out


def plot_chart(df: pd.DataFrame, symbol: str, timeframe: str, fast: int, slow: int, outdir: Path) -> Path:
    """
    Grafica precio + MAs + marcas de BUY/SELL y guarda PNG.
    Nota: no se especifican colores (usa defaults de Matplotlib).
    """
    outdir.mkdir(parents=True, exist_ok=True)
    outfile = outdir / f"{symbol}_{timeframe}_fast{fast}_slow{slow}.png"

    fig = plt.figure()  # un solo plot (sin subplots)
    plt.plot(df.index, df["close"], label=f"{symbol} close")
    plt.plot(df.index, df["ma_fast"], label=f"MA({fast})")
    plt.plot(df.index, df["ma_slow"], label=f"MA({slow})")

    buys = df[df["signal"] == "BUY"]
    sells = df[df["signal"] == "SELL"]

    # Marcadores (sin especificar colores)
    if not buys.empty:
        plt.scatter(buys.index, buys["close"], marker="^", label="BUY")
    if not sells.empty:
        plt.scatter(sells.index, sells["close"], marker="v", label="SELL")

    plt.title(f"{symbol} {timeframe} — MA crossover {fast}/{slow}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(outfile, dpi=120)
    # plt.show()  # opcional: si quieres abrir la ventana
    return outfile


def main():
    parser = argparse.ArgumentParser(description="Graficar precio + MAs + señales BUY/SELL desde Alpaca")
    parser.add_argument("--symbol", type=str, default="AAPL")
    parser.add_argument("--timeframe", type=str, default="1Min")
    parser.add_argument("--lookback", type=int, default=300)
    parser.add_argument("--fast", type=int, default=10)
    parser.add_argument("--slow", type=int, default=30)
    args = parser.parse_args()

    if args.fast >= args.slow:
        raise ValueError("fast debe ser menor que slow (ej. fast=10, slow=30)")

    broker = BrokerAlpaca()
    bars = broker.get_bars(args.symbol, timeframe=args.timeframe, limit=args.lookback)
    df = bars_to_df(bars)
    df = compute_signals(df, args.fast, args.slow)

    outpath = plot_chart(df, args.symbol, args.timeframe, args.fast, args.slow, Path("data/plots"))
    print(f"✅ Gráfico generado: {outpath.resolve()}")


if __name__ == "__main__":
    main()
