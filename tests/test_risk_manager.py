"""
Pruebas para src/risk_manager_avanzado.py — el RiskManager que realmente
usa run_paper.py (a diferencia del extinto src/risk.py). Cubren sizing por
% de equity, rechazo por RR insuficiente, rechazo por liquidez, guardas de
máximo de posiciones, y la propiedad de "ratchet" del trailing stop
(nunca retrocede a favor del trade).
"""
import pytest

from src.risk_manager_avanzado import RiskManager, RiskConfig, Side, SimpleAdapter


def _liquid_bars(n=25, price=100.0, volume=50_000.0):
    """Barras con liquidez de sobra (price*volume >> min_liquidity_dollar por defecto)."""
    return {
        "close": [price] * n,
        "high": [price] * n,
        "low": [price] * n,
        "volume": [volume] * n,
    }


def _illiquid_bars(n=25, price=100.0, volume=1.0):
    return {
        "close": [price] * n,
        "high": [price] * n,
        "low": [price] * n,
        "volume": [volume] * n,
    }


def make_rm(**overrides):
    cfg_kwargs = dict(
        account_risk_pct=0.01,
        min_rr=2.0,
        fee_per_share=0.0,
        slippage_pct=0.0005,
        min_liquidity_dollar=1_000_000,
    )
    cfg_kwargs.update(overrides)
    cfg = RiskConfig(**cfg_kwargs)
    adapter = SimpleAdapter()
    adapter._equity = 10_000.0
    rm = RiskManager(cfg, adapter)
    return rm, adapter


def test_assess_entry_approves_and_sizes_by_percent_risk():
    rm, _ = make_rm()
    decision = rm.assess_entry(
        "AAPL", Side.LONG, price=100.0, bars=_liquid_bars(),
        custom_stop=95.0, custom_take_profit=112.0,
    )
    assert decision.allow, decision.reason
    # capital_risk = 10_000 * 1% = 100; riesgo efectivo por acción ~5.05 (stop=5 + slippage)
    # => qty ~= 100 / 5.05 ~= 19
    assert decision.qty == 19
    assert decision.stop == pytest.approx(95.0)
    assert decision.take_profit == pytest.approx(112.0)


def test_assess_entry_rejects_low_rr():
    rm, _ = make_rm(min_rr=2.0)
    # riesgo=5, beneficio=6 -> RR ~1.16, por debajo de min_rr=2.0
    decision = rm.assess_entry(
        "AAPL", Side.LONG, price=100.0, bars=_liquid_bars(),
        custom_stop=95.0, custom_take_profit=106.0,
    )
    assert not decision.allow
    assert "RR" in decision.reason


def test_assess_entry_rejects_low_liquidity():
    rm, _ = make_rm(min_liquidity_dollar=1_000_000)
    decision = rm.assess_entry(
        "AAPL", Side.LONG, price=100.0, bars=_illiquid_bars(),
        custom_stop=95.0, custom_take_profit=112.0,
    )
    assert not decision.allow
    assert "Liquidez" in decision.reason


def test_basic_guard_blocks_when_max_positions_reached():
    rm, adapter = make_rm(max_positions=2)
    adapter._positions = [
        {"symbol": "AAPL", "qty": 10, "avg_price": 100.0, "side": Side.LONG, "stop": 95.0},
        {"symbol": "MSFT", "qty": 5, "avg_price": 200.0, "side": Side.LONG, "stop": 190.0},
    ]
    decision = rm.assess_entry(
        "TSLA", Side.LONG, price=100.0, bars=_liquid_bars(),
        custom_stop=95.0, custom_take_profit=112.0,
    )
    assert not decision.allow
    assert "Max posiciones" in decision.reason


def test_trailing_stop_never_gives_back_gains_on_long():
    rm, _ = make_rm(trailing_atr_multiple=1.0, atr_window=5)
    bars = {
        "close": [100, 101, 102, 103, 104, 105],
        "high":  [101, 102, 103, 104, 105, 106],
        "low":   [99, 100, 101, 102, 103, 104],
        "volume": [10_000] * 6,
    }
    stop = 95.0
    # precio subiendo: el stop debe ser monótono no-decreciente (ratchet)
    for price in [100.0, 105.0, 110.0, 108.0, 112.0]:
        new_stop = rm.update_trailing_stop(Side.LONG, price, stop, bars)
        assert new_stop >= stop
        stop = new_stop


def test_trailing_stop_never_gives_back_gains_on_short():
    rm, _ = make_rm(trailing_atr_multiple=1.0, atr_window=5)
    bars = {
        "close": [105, 104, 103, 102, 101, 100],
        "high":  [106, 105, 104, 103, 102, 101],
        "low":   [104, 103, 102, 101, 100, 99],
        "volume": [10_000] * 6,
    }
    stop = 115.0
    for price in [105.0, 100.0, 95.0, 97.0, 90.0]:
        new_stop = rm.update_trailing_stop(Side.SHORT, price, stop, bars)
        assert new_stop <= stop
        stop = new_stop


def test_should_halt_trading_on_daily_loss_limit():
    rm, adapter = make_rm(daily_loss_limit_pct=0.03)
    rm.start_of_day()  # day_start_equity = 10_000
    adapter._equity = 9_600.0  # -4%, supera el límite de 3%
    halt, reason = rm.should_halt_trading()
    assert halt
    assert "diario" in reason.lower()


def test_should_halt_trading_on_consecutive_losses():
    rm, adapter = make_rm(max_consecutive_losses=2)
    rm.start_of_day()
    rm.record_close("AAPL", Side.LONG, 10, 100.0, 95.0, None, pnl=-50.0)
    rm.record_close("AAPL", Side.LONG, 10, 100.0, 95.0, None, pnl=-30.0)
    halt, reason = rm.should_halt_trading()
    assert halt
    assert "racha" in reason.lower()


def test_record_close_resets_streak_on_win():
    rm, adapter = make_rm(max_consecutive_losses=2)
    rm.start_of_day()
    rm.record_close("AAPL", Side.LONG, 10, 100.0, 95.0, None, pnl=-50.0)
    rm.record_close("AAPL", Side.LONG, 10, 100.0, 95.0, None, pnl=+50.0)
    assert rm.consecutive_losses == 0
