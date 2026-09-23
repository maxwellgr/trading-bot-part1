"""
Pruebas del analizador offline de sesiones (src/analyze_session.py).
Las sesiones se generan con el SessionLogger real (mismo esquema que produce
run_paper) o, cuando hace falta controlar los timestamps de log, con
registros crudos equivalentes. Sin red.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src import analyze_session as az
from src import execution_guards
from src.structured_logger import SessionLogger

B = [f"2026-09-23T15:{m:02d}:00+00:00" for m in range(20, 40)]  # 15:20 .. 15:39
CONFIG = {"symbols_parsed": ["NVDA", "MARA"], "timeframe": "1Min", "ensemble_mode": "weighted", "ignore_clock": False}


@pytest.fixture
def slog(tmp_path):
    sl = SessionLogger(session_id="az_test", directory=tmp_path)
    yield sl
    sl.close()


def _poll(sl, symbol, bar_ts, signal=None):
    for name in ("ma", "macd", "rsi", "bbands"):
        sl.strategy_evaluation(symbol=symbol, timeframe="1Min", bar_timestamp=bar_ts, bar_close=1.0, strategy=name,
                               signal=None, raw_signal=None, reason="r", warmup_ok=True)
    sl.ensemble_decision(symbol=symbol, timeframe="1Min", bar_timestamp=bar_ts, bar_close=1.0, ensemble_mode="weighted",
                         signal=signal, votes={}, score=0.0, threshold=1.0, reason="r")


def _risk(sl, symbol, bar_ts, decision="REJECT", reason="RR 1.10 < min 1.30", signal="BUY"):
    sl.risk_evaluation(symbol=symbol, side="LONG", signal=signal, decision=decision, reason=reason, bar_timestamp=bar_ts)


def _analyze(sl):
    sl._fh.flush()
    return az.analyze_file(sl.path)


def _write_raw(path, records, t0=datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)):
    """Registros con el mismo esquema del SessionLogger pero timestamps de log controlados."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i, (offset_s, etype, fields) in enumerate(records, 1):
            rec = {"observation_id": i, "timestamp": (t0 + timedelta(seconds=offset_s)).isoformat(timespec="milliseconds"),
                   "session_id": "raw", "event_type": etype}
            rec.update(fields)
            fh.write(json.dumps(rec) + "\n")
    return path


# ---------------- sesión normal completa ----------------
def _recent_bars(n):
    """n velas de 1Min que terminan hace 1 minuto: frescas respecto del reloj con que SessionLogger estampa."""
    last = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
    return [(last - timedelta(minutes=n - 1 - i)).isoformat() for i in range(n)]


def test_normal_completed_session(slog):
    B = _recent_bars(6)  # la ejecución de una sesión sana ocurre sobre velas frescas
    slog.session_start(CONFIG)
    for ts in B[:5]:
        _poll(slog, "NVDA", ts, "HOLD")
        _poll(slog, "MARA", ts, "HOLD")
    _poll(slog, "NVDA", B[5], "BUY")
    _risk(slog, "NVDA", B[5], decision="ACCEPT", reason="OK")
    slog.order_submission(symbol="NVDA", side="buy", requested_qty=1, bar_timestamp=B[5])
    slog.order_result(symbol="NVDA", order={"id": "o1", "status": "accepted", "side": "buy"}, bar_timestamp=B[5])
    slog.session_end("manual_stop")

    a = _analyze(slog)
    m = a["metadata"]
    assert m["session_id"] == "az_test" and m["has_session_end"] and m["end_reason"] == "manual_stop"
    assert m["symbols"] == ["NVDA", "MARA"] and m["timeframe"] == "1Min"
    assert m["total_records"] == 1 + 11 * 5 + 3 + 1  # start + 11 polls x 5 eventos + risk/sub/result + end
    assert a["market_data"]["per_symbol"]["NVDA"]["unique_bar_count"] == 6
    assert a["execution_integrity"]["status"] == "OK"
    assert a["orders"]["submissions"] == 1 and a["orders"]["results_by_status"] == {"accepted": 1}
    assert a["orders"]["per_symbol"] == {"NVDA": {"submissions": 1, "results": 1}}
    assert a["summary_reconciliation"]["status"] == "MATCH"
    assert a["warnings"] == []
    assert "Sesión az_test" in az.format_report(a)


# ---------------- datos de mercado ----------------
def test_repeated_polls_of_one_bar_are_not_an_error(slog):
    slog.session_start(CONFIG)
    for _ in range(4):
        _poll(slog, "NVDA", B[0], "HOLD")
    d = _analyze(slog)["market_data"]["per_symbol"]["NVDA"]
    assert d["unique_bar_count"] == 1
    assert d["observations"] == 4 and d["repeated_observations"] == 3
    assert d["timestamp_regressions"] == 0
    assert _analyze(slog)["execution_integrity"]["status"] == "OK"


def test_timestamp_progression(slog):
    slog.session_start(CONFIG)
    for ts in B[:10]:
        _poll(slog, "NVDA", ts)
        _poll(slog, "NVDA", ts)  # polling normal: dos veces cada vela
    d = _analyze(slog)["market_data"]["per_symbol"]["NVDA"]
    assert d["first_bar_timestamp"] == B[0] and d["last_bar_timestamp"] == B[9]
    assert d["unique_bar_count"] == 10 and d["repeated_observations"] == 10
    assert d["timestamp_regressions"] == 0 and d["missing_bars"] == 0


def test_timestamp_regression_detected(slog):
    slog.session_start(CONFIG)
    for ts in (B[0], B[1], B[2], B[1], B[3]):  # vuelve a 15:21 tras ver 15:22
        _poll(slog, "NVDA", ts)
    a = _analyze(slog)
    d = a["market_data"]["per_symbol"]["NVDA"]
    assert d["timestamp_regressions"] == 1
    assert d["regression_examples"][0]["previous_bar"] == B[2] and d["regression_examples"][0]["bar"] == B[1]
    issues = {i["check"]: i for i in a["execution_integrity"]["issues"]}
    assert issues["bar_timestamp_regression"]["per_symbol"] == {"NVDA": 1}


def test_gap_detection_uses_timeframe(slog):
    slog.session_start(CONFIG)
    for ts in (B[0], B[1], B[5], B[6], B[9]):  # faltan 15:22-15:24 (3) y 15:27-15:28 (2)
        _poll(slog, "NVDA", ts)
    d = _analyze(slog)["market_data"]["per_symbol"]["NVDA"]
    assert d["missing_bars"] == 5 and d["gap_count"] == 2
    assert d["largest_gaps"][0] == {"after": B[1], "before": B[5], "missing_bars": 3}


def test_gap_detection_5min_timeframe(tmp_path):
    cfg = dict(CONFIG, timeframe="5Min")
    p = _write_raw(tmp_path / "s.jsonl", [(0, "session_start", {"config": cfg})] + [
        (i, "ensemble_decision", {"symbol": "NVDA", "bar_timestamp": ts, "signal": None})
        for i, ts in enumerate(["2026-09-23T15:00:00+00:00", "2026-09-23T15:05:00+00:00", "2026-09-23T15:20:00+00:00"], 1)])
    d = az.analyze_file(p)["market_data"]["per_symbol"]["NVDA"]
    assert d["missing_bars"] == 2  # faltan 15:10 y 15:15


def test_max_bar_age_and_frozen_data(tmp_path):
    # vela 15:29 re-evaluada durante una hora (el incidente original)
    recs = [(0, "session_start", {"config": CONFIG})]
    recs += [(29 * 60 + 70 + k * 15 * 60, "ensemble_decision",
              {"symbol": "NVDA", "bar_timestamp": "2026-09-23T15:29:00+00:00", "signal": "SELL"}) for k in range(5)]
    a = az.analyze_file(_write_raw(tmp_path / "s.jsonl", recs))
    d = a["market_data"]["per_symbol"]["NVDA"]
    assert d["max_bar_age_seconds"] == 70 + 60 * 60
    assert d["observations_older_than_threshold"] == 4
    assert d["unique_bar_count"] == 1


# ---------------- señales ----------------
def test_signal_counts_separate_evaluations_from_unique_bars(slog):
    slog.session_start(CONFIG)
    for _ in range(3):
        _poll(slog, "NVDA", B[0], "BUY")
    _poll(slog, "NVDA", B[1], "SELL")
    _poll(slog, "NVDA", B[2], None)
    s = _analyze(slog)["signals"]["per_symbol"]["NVDA"]
    assert s["evaluations"] == {"BUY": 3, "SELL": 1, "HOLD": 1}
    assert s["unique_bars"] == {"BUY": 1, "SELL": 1, "HOLD": 1}


# ---------------- integridad de ejecución ----------------
def test_duplicate_signal_reaching_risk_twice_is_flagged(slog):
    slog.session_start(CONFIG)
    _poll(slog, "MARA", B[0], "BUY")
    _risk(slog, "MARA", B[0], reason="Liquidez insuficiente ($1 < $2)")
    _poll(slog, "MARA", B[0], "BUY")
    _risk(slog, "MARA", B[0], reason="Liquidez insuficiente ($1 < $2)")
    _poll(slog, "MARA", B[1], "BUY")
    _risk(slog, "MARA", B[1])  # vela nueva: legítimo
    integ = _analyze(slog)["execution_integrity"]
    assert integ["status"] == "ISSUES"
    dup = next(i for i in integ["issues"] if i["check"] == "duplicate_signal_reached_risk")
    assert dup["count"] == 1
    assert dup["examples"][0]["symbol"] == "MARA" and dup["examples"][0]["bar_timestamp"] == B[0]
    assert dup["examples"][0]["risk_evaluations"] == 2


def test_risk_without_bar_timestamp_is_flagged_but_duplicates_not_inferred(slog):
    slog.session_start(CONFIG)
    for _ in range(2):
        slog.risk_evaluation(symbol="MARA", side="LONG", signal="BUY", decision="REJECT", reason="x")  # esquema antiguo
    integ = _analyze(slog)["execution_integrity"]
    checks = [i["check"] for i in integ["issues"]]
    assert "risk_evaluation_missing_bar_timestamp" in checks
    assert "duplicate_signal_reached_risk" not in checks
    assert integ["not_verifiable"][0]["check"] == "duplicate_signal_reached_risk"


def test_orders_missing_context_flagged(slog):
    slog.session_start(CONFIG)
    slog.order_submission(symbol="NVDA", side="buy", requested_qty=1)  # sin bar_timestamp
    slog.order_result(symbol="NVDA", order={"status": "accepted"}, bar_timestamp=B[0])  # sin order_id
    issues = {i["check"]: i for i in _analyze(slog)["execution_integrity"]["issues"]}
    assert issues["order_submission_missing_context"]["missing_fields"] == {"bar_timestamp": 1}
    assert issues["order_result_missing_context"]["missing_fields"] == {"order_id": 1}


def test_stale_execution_attempt_flagged_only_with_sufficient_evidence(tmp_path):
    def build(ignore_clock):
        cfg = dict(CONFIG, ignore_clock=ignore_clock)
        return [(0, "session_start", {"config": cfg}),
                # risk_evaluation 20 min después de la vela 15:00 (umbral 5 min)
                (20 * 60, "risk_evaluation", {"symbol": "NVDA", "bar_timestamp": "2026-09-23T15:00:00+00:00",
                                              "side": "LONG", "signal": "BUY", "decision": "REJECT", "reason_code": "X"}),
                # risk_evaluation fresca: 90 s
                (21 * 60 + 90, "risk_evaluation", {"symbol": "NVDA", "bar_timestamp": "2026-09-23T15:21:00+00:00",
                                                   "side": "LONG", "signal": "BUY", "decision": "REJECT", "reason_code": "X"})]

    integ = az.analyze_file(_write_raw(tmp_path / "a.jsonl", build(False)))["execution_integrity"]
    stale = next(i for i in integ["issues"] if i["check"] == "stale_data_execution_attempt")
    assert stale["count"] == 1 and stale["examples"][0]["age_seconds"] == 1200

    integ = az.analyze_file(_write_raw(tmp_path / "b.jsonl", build(True)))["execution_integrity"]
    assert not any(i["check"] == "stale_data_execution_attempt" for i in integ["issues"])
    assert any(n["check"] == "stale_data_execution_attempt" for n in integ["not_verifiable"])


# ---------------- riesgo / órdenes ----------------
def test_risk_grouping_by_reason_and_symbol(slog):
    slog.session_start(CONFIG)
    for i in range(3):
        _risk(slog, "MARA", B[i], reason="Liquidez insuficiente ($150,000 < $200,000)")
    _risk(slog, "NVDA", B[0], reason="RR 1.10 < min 1.30")
    _risk(slog, "NVDA", B[1], decision="ACCEPT", reason="OK")
    r = _analyze(slog)["risk"]
    assert (r["total"], r["accept"], r["reject"]) == (5, 1, 4)
    assert r["rejections_by_reason_code"] == {"LIQUIDITY_BELOW_MINIMUM": 3, "RR_BELOW_MINIMUM": 1}
    assert r["per_symbol"] == {"MARA": {"total": 3, "accept": 0, "reject": 3}, "NVDA": {"total": 2, "accept": 1, "reject": 1}}


def test_no_order_session_is_not_an_error(slog):
    slog.session_start(CONFIG)
    _poll(slog, "NVDA", B[0], "HOLD")
    slog.session_end("manual_stop")
    a = _analyze(slog)
    assert a["orders"]["submissions"] == 0 and a["orders"]["note"]
    assert a["execution_integrity"]["status"] == "OK"
    assert "Sin órdenes" in az.format_report(a)


# ---------------- reconciliación ----------------
def _rich_session(sl):
    sl.session_start(CONFIG)
    for ts in B[:3]:
        _poll(sl, "NVDA", ts, "BUY")
        _poll(sl, "NVDA", ts, "BUY")
    _risk(sl, "NVDA", B[0], reason="Liquidez insuficiente ($1 < $2)")
    sl.execution_guard(symbol="NVDA", bar_timestamp=B[0], side="BUY", action="signal_entry", guard="DUPLICATE_SIGNAL")
    sl.data_freshness(symbol="MARA", timeframe="1Min", bar_timestamp=B[0], status="stale",
                      age_seconds=400.0, threshold_seconds=300.0, unchanged_seconds=100.0)
    sl.order_submission(symbol="NVDA", side="buy", requested_qty=1, bar_timestamp=B[1])
    sl.order_result(symbol="NVDA", order=None, bar_timestamp=B[1])
    sl.session_end("manual_stop")


def test_summary_match(slog):
    _rich_session(slog)
    rec = _analyze(slog)["summary_reconciliation"]
    assert rec["status"] == "MATCH", rec["mismatches"]
    assert rec["compared_fields"] > 20
    assert set(rec["not_compared"]) == {"started_at", "ended_at", "runtime_seconds"}


def test_summary_mismatch_identifies_fields(slog):
    _rich_session(slog)
    slog.close()
    lines = slog.path.read_text(encoding="utf-8").splitlines()
    end = json.loads(lines[-1])
    end["summary"]["risk"]["reject"] = 99
    end["summary"]["per_symbol"]["NVDA"]["unique_bar_count"] = 7
    lines[-1] = json.dumps(end)
    slog.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rec = az.analyze_file(slog.path)["summary_reconciliation"]
    assert rec["status"] == "MISMATCH"
    fields = {m["field"]: m for m in rec["mismatches"]}
    assert set(fields) == {"risk.reject", "per_symbol.NVDA.unique_bar_count"}
    assert fields["risk.reject"]["session_end_summary"] == 99 and fields["risk.reject"]["reconstructed"] == 1


# ---------------- sesiones parciales ----------------
def test_missing_session_end(slog):
    slog.session_start(CONFIG)
    _poll(slog, "NVDA", B[0], "HOLD")
    a = _analyze(slog)
    assert a["metadata"]["has_session_end"] is False
    assert a["metadata"]["runtime_basis"].startswith("last_event")
    assert a["metadata"]["runtime_seconds"] is not None
    assert a["summary_reconciliation"]["status"] == "UNAVAILABLE"
    assert a["market_data"]["per_symbol"]["NVDA"]["unique_bar_count"] == 1


def test_truncated_final_line_is_skipped_with_warning(slog):
    slog.session_start(CONFIG)
    for ts in B[:3]:
        _poll(slog, "NVDA", ts, "HOLD")
    slog.close()
    with open(slog.path, "a", encoding="utf-8") as fh:
        fh.write('{"observation_id": 17, "timestamp": "2026-09-23T15:2')  # proceso muerto a mitad de línea

    a = az.analyze_file(slog.path)
    assert a["metadata"]["total_records"] == 16  # nada válido se descartó
    assert a["metadata"]["corrupt_lines"] == 0
    assert any("incompleta" in w for w in a["warnings"])
    assert a["market_data"]["per_symbol"]["NVDA"]["unique_bar_count"] == 3
    assert a["execution_integrity"]["status"] == "OK"


def test_corrupt_middle_line_is_reported_not_silently_dropped(slog):
    slog.session_start(CONFIG)
    _poll(slog, "NVDA", B[0])
    slog.close()
    lines = slog.path.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "{garbage")
    slog.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    a = az.analyze_file(slog.path)
    assert a["metadata"]["total_records"] == 6 and a["metadata"]["corrupt_lines"] == 1
    assert any(i["check"] == "corrupt_jsonl_lines" for i in a["execution_integrity"]["issues"])


# ---------------- CLI y coherencia con la guarda en vivo ----------------
def test_cli_json_and_text(slog, capsys):
    _rich_session(slog)
    slog.close()
    assert az.main([str(slog.path), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["summary_reconciliation"]["status"] == "MATCH"
    assert az.main([str(slog.path)]) == 0
    assert "Reconciliación" in capsys.readouterr().out


def test_cli_missing_file(tmp_path, capsys):
    assert az.main([str(tmp_path / "nope.jsonl")]) == 2


@pytest.mark.parametrize("tf", ["1Min", "5Min", "15Min", "1Hour", "1Day", "weird"])
def test_local_threshold_matches_live_guard(tf):
    assert az.timeframe_to_seconds(tf) == execution_guards.timeframe_to_seconds(tf)
    assert az.stale_threshold_seconds(tf) == execution_guards.BarFreshnessGuard(tf).threshold_seconds
