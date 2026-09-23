"""
Pruebas del resumen de fin de sesión y del cierre ordenado (observability
only): contadores derivados de los eventos JSONL escritos, session_end único
y ruta de Ctrl+C en run_paper.main(). Sin red.
"""
import json

import pytest

from src import run_paper, structured_logger
from src.structured_logger import SessionLogger


@pytest.fixture
def slog(tmp_path):
    sl = SessionLogger(session_id="summary_test", directory=tmp_path)
    yield sl
    sl.close()


def _records(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _strategy(sl, symbol, bar_ts, strategy="ma", signal=None):
    sl.strategy_evaluation(symbol=symbol, timeframe="1Min", bar_timestamp=bar_ts, bar_close=1.0,
                           strategy=strategy, signal=signal, raw_signal=signal, reason="r", warmup_ok=True)


def _ensemble(sl, symbol, bar_ts, signal):
    sl.ensemble_decision(symbol=symbol, timeframe="1Min", bar_timestamp=bar_ts, bar_close=1.0,
                         ensemble_mode="weighted", signal=signal, votes={}, score=0.0, threshold=1.0, reason="r")


def _full_poll(sl, symbol, bar_ts, signal):
    """Un poll en modo ensemble: 4 strategy_evaluation + 1 ensemble_decision para la MISMA vela."""
    for name in ("ma", "macd", "rsi", "bbands"):
        _strategy(sl, symbol, bar_ts, strategy=name)
    _ensemble(sl, symbol, bar_ts, signal)


B1, B2, B3 = "2026-09-23T15:29:00+00:00", "2026-09-23T15:30:00+00:00", "2026-09-23T15:31:00+00:00"


# ---------------- velas únicas ----------------
def test_unique_bar_counting_per_symbol_with_first_and_last(slog):
    slog.session_start({"symbols_parsed": ["NVDA", "AMD", "MU"]})
    for ts in (B2, B1, B3):  # fuera de orden a propósito
        _full_poll(slog, "NVDA", ts, None)
    _full_poll(slog, "AMD", B1, None)

    s = slog.session_end("test")
    assert s["symbols"] == ["NVDA", "AMD", "MU"]
    assert s["per_symbol"]["NVDA"] == {"first_bar_timestamp": B1, "last_bar_timestamp": B3, "unique_bar_count": 3}
    assert s["per_symbol"]["AMD"] == {"first_bar_timestamp": B1, "last_bar_timestamp": B1, "unique_bar_count": 1}
    # configurado pero sin datos: se reporta vacío, no se inventa nada
    assert s["per_symbol"]["MU"] == {"first_bar_timestamp": None, "last_bar_timestamp": None, "unique_bar_count": 0}
    assert s["unique_bar_count_total"] == 4


def test_repeated_polls_and_multiple_strategies_do_not_inflate_bar_count(slog):
    for _ in range(4):  # 4 polls de 15s sobre la misma vela, 4 estrategias + ensemble cada uno
        _full_poll(slog, "MARA", B1, "BUY")
    s = slog.session_end("test")
    assert s["per_symbol"]["MARA"]["unique_bar_count"] == 1
    assert s["event_counts"]["strategy_evaluation"] == 16
    # las evaluaciones sí se cuentan por poll, pero por vela única es 1
    assert s["ensemble"]["per_evaluation"]["BUY"] == 4
    assert s["ensemble"]["per_unique_bar"]["BUY"] == 1


def test_single_strategy_mode_counts_bars_without_ensemble(slog):
    _strategy(slog, "NVDA", B1, signal="BUY")
    _strategy(slog, "NVDA", B1, signal="BUY")
    s = slog.session_end("test")
    assert s["per_symbol"]["NVDA"]["unique_bar_count"] == 1
    assert s["ensemble"]["evaluations"] == 0
    assert s["ensemble"]["per_evaluation"] == {"BUY": 0, "SELL": 0, "HOLD": 0}


# ---------------- ensemble ----------------
def test_ensemble_buy_sell_hold_counts(slog):
    _ensemble(slog, "NVDA", B1, "BUY")
    _ensemble(slog, "NVDA", B1, "BUY")
    _ensemble(slog, "NVDA", B2, "SELL")
    _ensemble(slog, "AMD", B1, None)
    _ensemble(slog, "AMD", B2, "HOLD")
    s = slog.session_end("test")
    assert s["ensemble"]["evaluations"] == 5
    assert s["ensemble"]["per_evaluation"] == {"BUY": 2, "SELL": 1, "HOLD": 2}
    assert s["ensemble"]["per_unique_bar"] == {"BUY": 1, "SELL": 1, "HOLD": 2}


# ---------------- guardas / freshness ----------------
def test_execution_guard_counts_by_type(slog):
    for _ in range(3):
        slog.execution_guard(symbol="MARA", bar_timestamp=B1, side="BUY", action="signal_entry", guard="DUPLICATE_SIGNAL")
    slog.execution_guard(symbol="NVDA", bar_timestamp=B1, side="BUY", action="signal_entry", guard="STALE_DATA")
    slog.data_freshness(symbol="NVDA", timeframe="1Min", bar_timestamp=B1, status="stale",
                        age_seconds=400.0, threshold_seconds=300.0, unchanged_seconds=100.0)
    s = slog.session_end("test")
    assert s["execution_guards"] == {"DUPLICATE_SIGNAL": 3, "STALE_DATA": 1}
    assert s["data_freshness"] == {"stale": 1}


# ---------------- riesgo ----------------
def test_risk_rejections_grouped_by_reason_code(slog):
    for _ in range(3):
        slog.risk_evaluation(symbol="MARA", side="LONG", signal="BUY", decision="REJECT",
                             reason="Liquidez insuficiente ($150,000 < $200,000)", bar_timestamp=B1)
    slog.risk_evaluation(symbol="NVDA", side="LONG", signal="BUY", decision="REJECT", reason="RR 1.10 < min 1.30")
    slog.risk_evaluation(symbol="AMD", side="LONG", signal="BUY", decision="REJECT", reason="algo nuevo")
    slog.risk_evaluation(symbol="META", side="LONG", signal="BUY", decision="ACCEPT", reason="OK")
    s = slog.session_end("test")
    assert s["risk"] == {
        "total": 6, "accept": 1, "reject": 5,
        "rejections_by_reason_code": {"LIQUIDITY_BELOW_MINIMUM": 3, "RR_BELOW_MINIMUM": 1, "UNCLASSIFIED": 1},
    }


# ---------------- órdenes ----------------
def test_order_counts_and_results_by_status(slog):
    slog.order_submission(symbol="META", side="buy", requested_qty=1)
    slog.order_submission(symbol="NVDA", side="buy", requested_qty=1)
    slog.order_result(symbol="META", order={"id": "1", "status": "accepted"})
    slog.order_result(symbol="NVDA", order={"id": "2", "status": "accepted"})
    slog.order_result(symbol="AMD", order=None)
    s = slog.session_end("test")
    assert s["orders"] == {"submissions": 2, "results_by_status": {"accepted": 2, "status_unavailable": 1}}


def test_empty_session_reports_zeros_not_fabricated_values(slog):
    s = slog.session_end("test")
    assert s["unique_bar_count_total"] == 0
    assert s["risk"]["total"] == 0 and s["risk"]["rejections_by_reason_code"] == {}
    assert s["orders"] == {"submissions": 0, "results_by_status": {}}
    assert s["execution_guards"] == {}


# ---------------- session_end único y compatible ----------------
def test_exactly_one_session_end_event(slog):
    first = slog.session_end("manual_stop")
    second = slog.session_end("manual_stop")
    third = slog.session_end("other")
    ends = [r for r in _records(slog.path) if r["event_type"] == "session_end"]
    assert first is not None and second is None and third is None
    assert len(ends) == 1
    assert slog.ended


def test_session_end_is_backward_compatible_and_complete(slog):
    slog.session_start({"symbols_parsed": ["NVDA"]})
    slog.session_end("manual_stop", extra={"note": "x"})
    rec = _records(slog.path)[-1]
    assert rec["event_type"] == "session_end"
    assert rec["reason"] == "manual_stop" and rec["note"] == "x"  # campos anteriores intactos
    s = rec["summary"]
    for key in ("session_id", "started_at", "ended_at", "runtime_seconds", "symbols", "per_symbol",
                "ensemble", "execution_guards", "risk", "orders"):
        assert key in s, key
    assert s["session_id"] == rec["session_id"] == "summary_test"
    assert s["runtime_seconds"] >= 0


def test_summary_failure_never_breaks_event_logging(slog, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug en el resumen")
    monkeypatch.setattr(slog.summary, "observe", boom)
    _ensemble(slog, "NVDA", B1, "BUY")
    assert _records(slog.path)[-1]["event_type"] == "ensemble_decision"


# ---------------- run_paper.main(): cierre ordenado ----------------
class FakeBroker:
    def __init__(self, clock_error=False):
        self.clock_error = clock_error

    def get_clock_is_open(self):
        if self.clock_error and getattr(self, "_started", False):
            raise RuntimeError("clock caído")
        self._started = True
        return True

    def get_account(self):
        return {"equity": "10000", "last_equity": "10000"}

    def get_position_qty(self, symbol):
        return 0


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(structured_logger, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(run_paper, "STATE_PATH", tmp_path / "state.json")

    def fake_tick(*, symbol, session_logger, **kw):
        _full_poll(session_logger, symbol, B1, "HOLD")

    monkeypatch.setattr(run_paper, "trade_one_symbol", fake_tick)
    args = run_paper.build_arg_parser().parse_args(
        ["--symbols", "NVDA,AMD", "--ensemble-mode", "weighted", "--poll-seconds", "0"])

    def session_files():
        return sorted(sessions.glob("*.jsonl"))
    return args, session_files


def _sleep_raising(exc_factory, after=0):
    calls = {"n": 0}

    def _sleep(seconds):
        calls["n"] += 1
        if calls["n"] > after:
            raise exc_factory()
    return _sleep


def test_keyboard_interrupt_writes_one_session_end_and_prints_summary(main_env, monkeypatch, capsys):
    args, session_files = main_env
    monkeypatch.setattr(run_paper, "BrokerAlpaca", lambda: FakeBroker())
    # 3 ciclos completos; Ctrl+C llega en el sleep del 3er poll
    monkeypatch.setattr(run_paper.time, "sleep", _sleep_raising(KeyboardInterrupt, after=2))

    run_paper.main(args)  # no debe propagar KeyboardInterrupt

    recs = _records(session_files()[0])
    ends = [r for r in recs if r["event_type"] == "session_end"]
    assert len(ends) == 1 and recs[-1]["event_type"] == "session_end"
    assert ends[0]["reason"] == "manual_stop"
    s = ends[0]["summary"]
    assert s["symbols"] == ["NVDA", "AMD"]
    assert s["per_symbol"]["NVDA"]["unique_bar_count"] == 1  # 3 polls x 5 eventos, una sola vela
    assert s["ensemble"]["per_evaluation"]["HOLD"] == 6

    out = capsys.readouterr().out
    assert "Bot detenido manualmente" in out
    assert "Resumen de sesión" in out
    assert out.count("Resumen de sesión") == 1


def test_keyboard_interrupt_during_error_backoff_is_graceful(main_env, monkeypatch):
    """Antes: Ctrl+C durante el sleep(10) tras un error escapaba de main() sin session_end."""
    args, session_files = main_env
    monkeypatch.setattr(run_paper, "BrokerAlpaca", lambda: FakeBroker(clock_error=True))
    monkeypatch.setattr(run_paper.time, "sleep", _sleep_raising(KeyboardInterrupt))

    run_paper.main(args)

    ends = [r for r in _records(session_files()[0]) if r["event_type"] == "session_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "manual_stop"


def test_unexpected_base_exception_is_not_swallowed_but_session_is_closed(main_env, monkeypatch):
    class Boom(BaseException):
        pass

    args, session_files = main_env
    monkeypatch.setattr(run_paper, "BrokerAlpaca", lambda: FakeBroker())
    monkeypatch.setattr(run_paper.time, "sleep", _sleep_raising(Boom))

    with pytest.raises(Boom):
        run_paper.main(args)

    ends = [r for r in _records(session_files()[0]) if r["event_type"] == "session_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "exception:Boom"
