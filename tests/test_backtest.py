"""Pruebas para src/backtest.py: el Backtester debe correr end-to-end sobre
las 4 estrategias (antes de nuestro fix en strategy.py, RSI/MACD/Bollinger
reventaban en la primera barra), contabilizar equity de forma consistente,
y soportar cortos cuando se habilitan."""
import numpy as np
import pandas as pd
import pytest

from src.backtest import Backtester
from src.strategy import MACrossover, RSIStrategy, MACDStrategy, BollingerStrategy


def _trend_reversal_df(n_flat=20, n_up=60, n_down=60) -> pd.DataFrame:
    """Ver docstring equivalente en tests/test_strategy.py: el tramo plano
    inicial es necesario para que el cruce de medias sea observable."""
    flat = np.full(n_flat, 100.0)
    up = np.linspace(100, 160, n_up)
    down = np.linspace(160, 90, n_down)
    close = np.concatenate([flat, up, down])
    idx = pd.date_range("2024-01-01", periods=len(close), freq="1min", tz="UTC")
    return pd.DataFrame({"close": close}, index=idx)


@pytest.mark.parametrize("strategy_factory", [
    lambda: MACrossover(fast=3, slow=10),
    lambda: RSIStrategy(period=5),
    lambda: MACDStrategy(fast=5, slow=13, signal=4),
    lambda: BollingerStrategy(window=10, k=2.0),
])
def test_backtester_runs_all_strategies_without_crashing(strategy_factory):
    df = _trend_reversal_df()
    bt = Backtester(df, cash=10_000.0)
    curve = bt.run(strategy_factory())
    assert len(curve) == len(df)
    assert not curve["equity"].isna().any()


def test_equity_curve_matches_cash_when_flat_at_end():
    df = _trend_reversal_df()
    bt = Backtester(df, cash=10_000.0, fee=0.0)
    curve = bt.run(MACrossover(fast=3, slow=10))
    if bt.trades:
        # Si terminó flat (sin posición abierta), la última equity == cash.
        # Detectamos "flat al final" comparando con el cash acumulado.
        assert curve["equity"].iloc[-1] == pytest.approx(bt.cash, rel=1e-6)


def test_fees_reduce_final_equity_vs_no_fees():
    df = _trend_reversal_df()
    bt_no_fee = Backtester(df, cash=10_000.0, fee=0.0)
    bt_no_fee.run(MACrossover(fast=3, slow=10))

    bt_fee = Backtester(df, cash=10_000.0, fee=5.0)
    bt_fee.run(MACrossover(fast=3, slow=10))

    if bt_no_fee.trades and bt_fee.trades:
        assert sum(bt_fee.trades) < sum(bt_no_fee.trades)


def test_shorts_disabled_by_default_ignores_sell_when_flat():
    df = pd.DataFrame({"close": [100.0, 90.0, 80.0]})

    class AlwaysSell:
        def signal(self, df):
            return "SELL"

    bt = Backtester(df, cash=10_000.0, allow_shorts=False)
    bt.run(AlwaysSell())
    assert bt.trades == []  # nunca abrió nada porque no hay long que cerrar y shorts están off


def test_shorts_enabled_can_open_and_profit_on_downtrend():
    class SellOnce:
        """SELL en la primera barra (abre corto), BUY en la última (lo cubre)."""
        def __init__(self, n):
            self.n = n
            self.calls = 0

        def signal(self, df):
            self.calls += 1
            if self.calls == 2:  # segunda barra evaluada -> abre short
                return "SELL"
            if self.calls == self.n:  # última barra -> cubre
                return "BUY"
            return None

    df = pd.DataFrame({"close": [100.0, 100.0, 90.0, 80.0, 70.0]})
    strat = SellOnce(n=len(df))
    bt = Backtester(df, cash=10_000.0, allow_shorts=True)
    bt.run(strat)
    assert len(bt.trades) == 1
    assert bt.trades[0] > 0  # corto en tendencia bajista -> ganancia
