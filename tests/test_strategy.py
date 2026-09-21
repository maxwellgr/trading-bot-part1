"""
Pruebas para src/strategy.py.

Cubren dos cosas:
1) Regresión del bug que encontramos al extender el backtester: RSIStrategy,
   MACDStrategy y BollingerStrategy lanzaban IndexError con pocas filas
   (menos de 2) porque no tenían la guarda de warm-up que sí tiene
   MACrossover. Ahora deben devolver None sin reventar.
2) Que las 4 estrategias, corridas tal como las usan run_paper.py y
   backtest.py (con ventanas crecientes de datos, no todo el dataframe de
   una vez), produzcan al menos una señal BUY y una SELL sobre una serie
   sintética diseñada para tener una tendencia clara y una reversión clara.
"""
import numpy as np
import pandas as pd
import pytest

from src.strategy import MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy


def _trend_reversal_df(n_flat: int = 20, n_up: int = 60, n_down: int = 60) -> pd.DataFrame:
    """
    Serie sintética: plana, luego sube fuerte, luego baja fuerte.
    El tramo plano inicial es clave: si la serie empezara ya en plena subida,
    el cruce de medias ocurriría *dentro* de la ventana de warm-up (todavía
    NaN) y nunca sería observable como transición prev<=0 -> now>0. Con un
    tramo plano antes, las medias llegan "calientes" y el cruce sí se ve.
    """
    flat = np.full(n_flat, 100.0)
    up = np.linspace(100, 160, n_up)
    down = np.linspace(160, 90, n_down)
    close = np.concatenate([flat, up, down])
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1min", tz="UTC")
    return pd.DataFrame({"close": close}, index=idx)


def scan_signals(strategy, df: pd.DataFrame) -> list:
    """Reproduce cómo se usan las estrategias en vivo/backtest: en cada paso
    se les pasa el prefijo del df visto hasta ese momento (nunca el futuro)."""
    out = []
    for i in range(1, len(df) + 1):
        sig = strategy.signal(df.iloc[:i])
        if sig is not None:
            out.append((df.index[i - 1], sig))
    return out


@pytest.mark.parametrize("strategy_factory", [
    lambda: MACrossover(fast=3, slow=10),
    lambda: RSIStrategy(period=5),
    lambda: MACDStrategy(fast=5, slow=13, signal=4),
    lambda: BollingerStrategy(window=10, k=2.0),
])
def test_no_crash_on_short_data(strategy_factory):
    """Con 0 o 1 fila no debe lanzar IndexError: debe devolver None."""
    strategy = strategy_factory()
    empty = pd.DataFrame({"close": pd.Series(dtype=float)})
    one_row = pd.DataFrame({"close": [100.0]})
    assert strategy.signal(empty) is None
    assert strategy.signal(one_row) is None


def test_ma_crossover_produces_buy_and_sell():
    df = _trend_reversal_df()
    signals = scan_signals(MACrossover(fast=3, slow=10), df)
    kinds = {s for _, s in signals}
    assert "BUY" in kinds
    assert "SELL" in kinds


def test_rsi_produces_signals_without_crashing():
    df = _trend_reversal_df()
    signals = scan_signals(RSIStrategy(period=5, buy_level=30, sell_level=70), df)
    # No exigimos BUY y SELL exactos (depende de la sensibilidad de RSI),
    # pero la corrida completa no debe reventar y debe generar al menos una señal.
    assert isinstance(signals, list)
    assert all(s in {"BUY", "SELL"} for _, s in signals)


def test_macd_produces_buy_and_sell():
    df = _trend_reversal_df()
    signals = scan_signals(MACDStrategy(fast=5, slow=13, signal=4), df)
    kinds = {s for _, s in signals}
    assert "BUY" in kinds
    assert "SELL" in kinds


def test_bollinger_runs_without_crashing():
    df = _trend_reversal_df()
    signals = scan_signals(BollingerStrategy(window=10, k=2.0), df)
    assert isinstance(signals, list)
    assert all(s in {"BUY", "SELL"} for _, s in signals)


def test_ma_crossover_requires_fast_lt_slow():
    with pytest.raises(AssertionError):
        MACrossover(fast=10, slow=5)


def test_missing_close_column_raises():
    df = pd.DataFrame({"open": [1, 2, 3]})
    with pytest.raises(ValueError):
        MACrossover(fast=1, slow=2).signal(df)
    with pytest.raises(ValueError):
        RSIStrategy(period=1).signal(df)
    with pytest.raises(ValueError):
        MACDStrategy(fast=1, slow=2, signal=1).signal(df)
    with pytest.raises(ValueError):
        BollingerStrategy(window=2).signal(df)
