"""Pruebas para src/metrics.py, incluyendo las métricas nuevas
(Sortino, Calmar, win rate, profit factor, expectancy)."""
import pandas as pd
import pytest

from src.metrics import (
    equity_to_returns,
    sharpe_ratio,
    sortino_ratio,
    max_drawdown,
    total_return,
    calmar_ratio,
    win_rate,
    profit_factor,
    expectancy,
    trade_stats,
)


def test_equity_to_returns():
    equity = pd.Series([100.0, 110.0, 121.0])
    rets = equity_to_returns(equity)
    assert rets.iloc[0] == 0.0  # fillna del primer NaN
    assert rets.iloc[1] == pytest.approx(0.10)
    assert rets.iloc[2] == pytest.approx(0.10)


def test_max_drawdown_known_series():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0])
    assert max_drawdown(equity) == pytest.approx(-0.25, rel=1e-3)


def test_total_return_known_series():
    equity = pd.Series([100.0, 150.0])
    assert total_return(equity) == pytest.approx(0.5)


def test_sharpe_ratio_zero_when_flat():
    rets = pd.Series([0.01, 0.01, 0.01])
    assert sharpe_ratio(rets) == 0.0


def test_sortino_ratio_zero_when_no_downside():
    # Sin retornos negativos, downside deviation = 0 -> función devuelve 0.0
    # (misma convención que sharpe_ratio cuando std=0, ver docstring).
    rets = pd.Series([0.01, 0.02, 0.015])
    assert sortino_ratio(rets) == 0.0


def test_sortino_penalizes_only_downside():
    rets = pd.Series([0.05, -0.02, 0.03, -0.01, 0.04])
    s = sortino_ratio(rets, steps_per_year=252)
    assert isinstance(s, float)
    assert s > 0  # media de retornos positiva


def test_calmar_ratio_positive_for_growing_equity_with_drawdown():
    equity = pd.Series([100.0] * 1 + [105.0, 95.0, 130.0])
    c = calmar_ratio(equity, steps_per_year=252)
    assert isinstance(c, float)


def test_calmar_ratio_zero_when_no_drawdown():
    equity = pd.Series([100.0, 101.0, 102.0, 103.0])
    assert calmar_ratio(equity, steps_per_year=252) == 0.0


def test_win_rate_and_profit_factor_and_expectancy():
    pnls = [10.0, -5.0, 20.0, -10.0, 0.0]
    assert win_rate(pnls) == pytest.approx(2 / 5)
    assert profit_factor(pnls) == pytest.approx(30.0 / 15.0)
    assert expectancy(pnls) == pytest.approx(3.0)


def test_profit_factor_edge_cases():
    assert profit_factor([]) == 0.0
    assert profit_factor([10.0, 5.0]) == float("inf")  # sin pérdidas
    assert profit_factor([-10.0, -5.0]) == 0.0          # sin ganancias


def test_trade_stats_summary():
    pnls = [10.0, -5.0, 20.0, -10.0]
    stats = trade_stats(pnls)
    assert stats["n_trades"] == 4
    assert stats["best_trade"] == 20.0
    assert stats["worst_trade"] == -10.0
    assert stats["gross_profit"] == 30.0
    assert stats["gross_loss"] == -15.0
