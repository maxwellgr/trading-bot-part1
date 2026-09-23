"""
Pruebas de las guardas de ejecución (src/execution_guards.py) y de su
integración en run_paper.trade_one_symbol:
- datos obsoletos -> una señal BUY/SELL no llega a riesgo ni a orden;
- misma (símbolo, vela, lado) -> riesgo/orden como mucho una vez;
- bar_timestamp presente en risk_evaluation / order_submission / order_result.

Sin red: broker, riesgo y estrategia falsos.
"""
import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest

from src import run_paper
from src.execution_guards import (
    ActionableSignalDeduper,
    BarFreshnessGuard,
    ExecutionGuards,
    timeframe_to_seconds,
)
from src.risk_manager_avanzado import RiskDecision
from src.strategy import StrategyResult
from src.structured_logger import SessionLogger


T0 = datetime(2026, 9, 23, 15, 29, tzinfo=timezone.utc)


# ---------------- unidades: timeframe ----------------
@pytest.mark.parametrize("tf,secs", [("1Min", 60), ("5Min", 300), ("15T", 900), ("1Hour", 3600), ("1Day", 86400)])
def test_timeframe_to_seconds(tf, secs):
    assert timeframe_to_seconds(tf) == secs


def test_timeframe_unknown_disables_staleness():
    g = BarFreshnessGuard("weird")
    st = g.observe("NVDA", T0, T0 + timedelta(hours=5))
    assert g.threshold_seconds is None
    assert not st.is_stale and st.event is None


# ---------------- unidades: frescura ----------------
def test_1min_threshold_is_five_minutes():
    assert BarFreshnessGuard("1Min").threshold_seconds == 300


def test_repeated_polls_of_current_candle_are_not_stale_and_do_not_warn():
    g = BarFreshnessGuard("1Min")
    for s in range(60, 181, 15):  # misma vela vista entre T+60s y T+180s cada 15s
        st = g.observe("NVDA", T0, T0 + timedelta(seconds=s))
        assert not st.is_stale
        assert st.event is None


def test_frozen_bar_becomes_stale_warns_once_then_throttled_then_recovers():
    g = BarFreshnessGuard("1Min")
    now = T0 + timedelta(seconds=90)
    assert g.observe("NVDA", T0, now).event is None

    now = T0 + timedelta(seconds=301)
    st = g.observe("NVDA", T0, now)
    assert st.is_stale and st.event == "stale"
    assert st.unchanged_seconds == pytest.approx(211)

    # polls cada 15s durante < 5 min: sigue obsoleto pero NO re-avisa
    for _ in range(19):
        now += timedelta(seconds=15)
        st = g.observe("NVDA", T0, now)
        assert st.is_stale and st.event is None

    now += timedelta(seconds=15)  # 300s desde el primer aviso
    st = g.observe("NVDA", T0, now)
    assert st.is_stale and st.event == "still_stale"

    # llega una vela nueva y reciente
    new_bar = now - timedelta(seconds=70)
    st = g.observe("NVDA", new_bar, now)
    assert not st.is_stale and st.event == "recovered"
    assert g.observe("NVDA", new_bar, now + timedelta(seconds=15)).event is None


def test_staleness_is_per_symbol():
    g = BarFreshnessGuard("1Min")
    now = T0 + timedelta(hours=1)
    assert g.observe("NVDA", T0, now).is_stale
    assert not g.observe("AMD", now - timedelta(seconds=70), now).is_stale


def test_closed_market_never_classified_stale():
    g = BarFreshnessGuard("1Min")
    st = g.observe("NVDA", T0, T0 + timedelta(hours=10), market_open=False)
    assert not st.is_stale and st.event is None


# ---------------- unidades: idempotencia ----------------
def test_dedup_same_symbol_bar_side_only_once():
    d = ActionableSignalDeduper()
    assert d.try_acquire("MARA", "2026-09-23T14:55:00+00:00", "BUY")
    assert not d.try_acquire("MARA", "2026-09-23T14:55:00+00:00", "BUY")
    assert not d.try_acquire("mara", "2026-09-23T14:55:00+00:00", "buy")


def test_dedup_new_bar_or_other_side_or_symbol_is_eligible():
    d = ActionableSignalDeduper()
    assert d.try_acquire("MARA", "2026-09-23T14:55:00+00:00", "BUY")
    assert d.try_acquire("MARA", "2026-09-23T14:56:00+00:00", "BUY")
    assert d.try_acquire("MARA", "2026-09-23T14:57:00+00:00", "SELL")
    assert d.try_acquire("MARA", "2026-09-23T14:55:00+00:00", "SELL")
    assert d.try_acquire("AMD", "2026-09-23T14:55:00+00:00", "BUY")


def test_dedup_memory_is_bounded():
    d = ActionableSignalDeduper(max_keys=3)
    for i in range(10):
        d.try_acquire("X", f"t{i}", "BUY")
    assert len(d._seen) == 3


# ---------------- integración: trade_one_symbol ----------------
class FakeBroker:
    """Posición siempre 0 (simula orden aún no reflejada en el broker): es
    justo el escenario donde, sin idempotencia, un segundo poll de la misma
    vela volvería a mandar la orden."""

    def __init__(self, bars):
        self.bars = bars
        self.orders = []

    def get_asset_tradable(self, symbol):
        return True

    def get_bars(self, symbol, timeframe="1Min", limit=120, start_iso=None):
        return self.bars[-limit:]

    def get_position_qty(self, symbol):
        return 0

    def cancel_open_orders(self, symbol):
        pass

    def place_order_market(self, symbol, side, qty, tif="day"):
        self.orders.append((symbol, side, qty))
        return {"id": f"ord{len(self.orders)}", "side": side, "status": "accepted"}


class FakeRisk:
    def __init__(self, allow=True):
        self.allow = allow
        self.calls = 0

    def assess_entry(self, symbol, side, price, bars_dict):
        self.calls += 1
        if not self.allow:
            return RiskDecision(allow=False, reason="Liquidez insuficiente ($100 < $200,000)")
        return RiskDecision(allow=True, qty=1, entry=price, stop=price - 1, take_profit=price + 2, reason="OK")

    def should_halt_trading(self):
        return False, ""

    def update_trailing_stop(self, side, price, stop, bars_dict):
        return stop


class FakeStrategy:
    def __init__(self, signal):
        self.signal = signal

    def evaluate(self, df):
        return StrategyResult(signal=self.signal, reason="test")


def _args(**kw):
    base = dict(strategy="ma", fast=3, slow=7, explain=False, debug_ma=False, allow_shorts=False,
                enter_when_above=False, exit_when_below=False, enter_short_when_below=False,
                exit_short_when_above=False, be_at_r=1.0, max_giveback_pct=0.5, daily_profit_halt=300.0)
    base.update(kw)
    return Namespace(**base)


def _bars(last: datetime, n: int = 30):
    first = last - timedelta(minutes=n - 1)
    return [{"t": (first + timedelta(minutes=i)).isoformat(), "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.0 + i * 0.01, "v": 1000}
            for i in range(n)]


def _recent_bar_ts() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)


@pytest.fixture
def slog(tmp_path):
    sl = SessionLogger(session_id="guards_test", directory=tmp_path)
    yield sl
    sl.close()


def _events(sl):
    with open(sl.path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _tick(broker, risk, strat, guards, slog, market_open=True, position_book=None):
    run_paper.trade_one_symbol(
        broker=broker, risk=risk, strat=strat, symbol="MARA", timeframe="1Min", lookback=120,
        start_iso="x", args=_args(), position_book=position_book if position_book is not None else {},
        ensemble=None, wrappers=None, scale_out_levels=[], session={"pnl_today": 0.0, "halted": False},
        session_logger=slog, guards=guards, market_open=market_open,
    )


def test_same_bar_buy_reaches_risk_and_order_only_once(slog):
    last = _recent_bar_ts()
    broker, risk, guards = FakeBroker(_bars(last)), FakeRisk(allow=True), ExecutionGuards("1Min")
    book = {}
    for _ in range(4):  # 4 polls de 15s sobre la misma vela
        _tick(broker, risk, FakeStrategy("BUY"), guards, slog, position_book=book)
    assert risk.calls == 1
    assert broker.orders == [("MARA", "buy", 1)]

    ev = _events(slog)
    # las observaciones de estrategia se siguen registrando en cada poll
    assert sum(e["event_type"] == "strategy_evaluation" for e in ev) == 4
    dup = [e for e in ev if e["event_type"] == "execution_guard"]
    assert len(dup) == 3 and all(e["guard"] == "DUPLICATE_SIGNAL" for e in dup)


def test_rejected_signal_is_not_reevaluated_on_same_bar_but_new_bar_is(slog):
    last = _recent_bar_ts()
    broker, risk, guards = FakeBroker(_bars(last - timedelta(minutes=1))), FakeRisk(allow=False), ExecutionGuards("1Min")
    for _ in range(3):
        _tick(broker, risk, FakeStrategy("BUY"), guards, slog)
    assert risk.calls == 1

    broker.bars = _bars(last)  # vela nueva
    _tick(broker, risk, FakeStrategy("BUY"), guards, slog)
    assert risk.calls == 2
    assert broker.orders == []


def test_stale_data_blocks_actionable_signal_but_keeps_observations(slog):
    stale_last = _recent_bar_ts() - timedelta(hours=1)
    broker, risk, guards = FakeBroker(_bars(stale_last)), FakeRisk(allow=True), ExecutionGuards("1Min")
    for _ in range(3):
        _tick(broker, risk, FakeStrategy("BUY"), guards, slog)
    assert risk.calls == 0
    assert broker.orders == []

    ev = _events(slog)
    assert sum(e["event_type"] == "strategy_evaluation" for e in ev) == 3
    fresh = [e for e in ev if e["event_type"] == "data_freshness"]
    assert [e["status"] for e in fresh] == ["stale"]  # un solo aviso, no uno por poll
    blocked = [e for e in ev if e["event_type"] == "execution_guard"]
    assert len(blocked) == 3 and all(e["guard"] == "STALE_DATA" for e in blocked)


def test_stale_hold_is_logged_without_guard_events(slog):
    stale_last = _recent_bar_ts() - timedelta(hours=1)
    broker, risk, guards = FakeBroker(_bars(stale_last)), FakeRisk(), ExecutionGuards("1Min")
    _tick(broker, risk, FakeStrategy(None), guards, slog)
    ev = _events(slog)
    assert any(e["event_type"] == "strategy_evaluation" and e["signal"] is None for e in ev)
    assert not any(e["event_type"] == "execution_guard" for e in ev)


def test_closed_market_ignore_clock_keeps_previous_behavior(slog):
    old_last = _recent_bar_ts() - timedelta(hours=10)
    broker, risk, guards = FakeBroker(_bars(old_last)), FakeRisk(allow=True), ExecutionGuards("1Min")
    _tick(broker, risk, FakeStrategy("BUY"), guards, slog, market_open=False)
    assert risk.calls == 1


def test_without_guards_behavior_is_unchanged(slog):
    last = _recent_bar_ts()
    broker, risk = FakeBroker(_bars(last)), FakeRisk(allow=False)
    for _ in range(2):
        _tick(broker, risk, FakeStrategy("BUY"), None, slog)
    assert risk.calls == 2


def test_bar_timestamp_on_risk_and_order_events(slog):
    last = _recent_bar_ts()
    broker, risk, guards = FakeBroker(_bars(last)), FakeRisk(allow=True), ExecutionGuards("1Min")
    _tick(broker, risk, FakeStrategy("BUY"), guards, slog)
    ev = _events(slog)
    expected = next(e["bar_timestamp"] for e in ev if e["event_type"] == "strategy_evaluation")
    assert expected == last.isoformat()
    for etype in ("risk_evaluation", "order_submission", "order_result"):
        matching = [e for e in ev if e["event_type"] == etype]
        assert matching, etype
        assert all(e["bar_timestamp"] == expected for e in matching), etype


def test_dataframe_is_trimmed_to_lookback_newest(slog):
    """Aunque el broker devolviera de más, trade_one_symbol evalúa las `lookback` más recientes."""
    last = _recent_bar_ts()

    class Oversupply(FakeBroker):
        def get_bars(self, symbol, timeframe="1Min", limit=120, start_iso=None):
            return self.bars  # ignora limit

    seen = {}

    class Spy(FakeStrategy):
        def evaluate(self, df):
            seen["len"], seen["last"] = len(df), df.index[-1]
            return super().evaluate(df)

    broker = Oversupply(_bars(last, n=200))
    run_paper.trade_one_symbol(
        broker=broker, risk=FakeRisk(), strat=Spy(None), symbol="MARA", timeframe="1Min", lookback=120,
        start_iso="x", args=_args(), position_book={}, ensemble=None, wrappers=None, scale_out_levels=[],
        session={"pnl_today": 0.0, "halted": False}, session_logger=slog, guards=ExecutionGuards("1Min"),
    )
    assert seen["len"] == 120
    assert seen["last"] == last
