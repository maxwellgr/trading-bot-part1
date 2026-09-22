"""
Pruebas para src/structured_logger.py (Fase B — Structured Trading
Diagnostics / Event Logger). Puramente observacional: estas pruebas no
ejercitan ninguna decisión de trading, solo que lo que ya decidieron
strategy/ensemble/risk_manager_avanzado quede bien serializado en JSONL.
"""
import json

import pandas as pd
import pytest

from src.ensemble import Ensemble, StrategyWrapper
from src.risk_manager_avanzado import RiskConfig, RiskManager, Side, SimpleAdapter
from src.strategy import MACrossover, StrategyResult
from src.structured_logger import SessionLogger, classify_risk_reason


@pytest.fixture
def session_logger(tmp_path):
    sl = SessionLogger(session_id="session_test", directory=tmp_path)
    yield sl
    sl.close()


def _read_records(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------- serialización básica de cada tipo de evento ----------------

def test_session_start_includes_config_and_git_commit(session_logger):
    session_logger.session_start({"symbols": ["AAPL"], "strategy": "ma"})
    records = _read_records(session_logger.path)
    assert records[0]["event_type"] == "session_start"
    assert records[0]["config"] == {"symbols": ["AAPL"], "strategy": "ma"}
    assert "git_commit" in records[0]  # puede ser str o None, pero el campo existe


def test_strategy_evaluation_event_shape(session_logger):
    session_logger.strategy_evaluation(
        symbol="NVDA", timeframe="1Min", bar_timestamp="2026-09-22T15:54:00Z",
        bar_close=228.46, strategy="ma", signal="SELL", raw_signal="SELL",
        reason="Cruce bajista", warmup_ok=True, gated_by_regime=False,
        indicator_values={"ma_fast": 228.47, "ma_slow": 228.51},
    )
    rec = _read_records(session_logger.path)[0]
    assert rec["event_type"] == "strategy_evaluation"
    assert rec["symbol"] == "NVDA"
    assert rec["signal"] == "SELL"
    assert rec["indicator_values"]["ma_fast"] == 228.47
    assert rec["session_id"] == "session_test"
    assert isinstance(rec["observation_id"], int)


def test_ensemble_decision_event_shape(session_logger):
    session_logger.ensemble_decision(
        symbol="META", timeframe="1Min", bar_timestamp="2026-09-22T15:58:00Z",
        bar_close=749.33, ensemble_mode="weighted", signal="BUY",
        votes={"BUY": 1, "SELL": 0}, score=1.0, threshold=1.0,
        reason="score=1.00 >= 1.0", any_warmup_pending=False,
    )
    rec = _read_records(session_logger.path)[0]
    assert rec["event_type"] == "ensemble_decision"
    assert rec["votes"] == {"BUY": 1, "SELL": 0}
    assert rec["threshold"] == 1.0


def test_order_result_omits_unavailable_fields_as_null(session_logger):
    """No se inventa nada: si el broker no devuelve filled_at, queda None."""
    session_logger.order_result(symbol="INTC", order={"id": "abc123", "status": "accepted"})
    rec = _read_records(session_logger.path)[0]
    assert rec["order_id"] == "abc123"
    assert rec["status"] == "accepted"
    assert rec["filled_at"] is None
    assert rec["filled_avg_price"] is None


def test_order_result_handles_missing_order_entirely(session_logger):
    """Si el broker nunca respondió (excepción antes de tener order), None-safe."""
    session_logger.order_result(symbol="INTC", order=None)
    rec = _read_records(session_logger.path)[0]
    assert rec["order_id"] is None
    assert rec["status"] is None


def test_position_management_event_shape(session_logger):
    session_logger.position_management("HOOD", "trailing_stop_update", {
        "bar_timestamp": "2026-09-22T16:00:00Z", "previous_stop": 123.0, "new_stop": 124.0,
    })
    rec = _read_records(session_logger.path)[0]
    assert rec["event_type"] == "position_management"
    assert rec["action"] == "trailing_stop_update"
    assert rec["new_stop"] == 124.0


# ---------------- reason codes de riesgo (sin tocar risk_manager_avanzado.py) ----------------

@pytest.mark.parametrize("reason,expected_code", [
    ("Liquidez insuficiente ($150,779 < 200,000)", "LIQUIDITY_BELOW_MINIMUM"),
    ("RR 0.93 < min 1.3", "RR_BELOW_MINIMUM"),
    ("Max posiciones (4)", "MAX_POSITIONS_REACHED"),
    ("Racha negativa 3 >= 3", "MAX_CONSECUTIVE_LOSSES_HIT"),
    ("OK", "ACCEPTED"),
    ("algo que nunca vamos a reconocer", "UNCLASSIFIED"),
    ("", "UNCLASSIFIED"),
])
def test_classify_risk_reason_codes(reason, expected_code):
    assert classify_risk_reason(reason)["reason_code"] == expected_code


def test_classify_risk_reason_extracts_rr_values_without_inventing_them():
    out = classify_risk_reason("RR 0.67 < min 1.3")
    assert out["risk_reward"] == 0.67
    assert out["min_risk_reward"] == 1.3
    assert out["liquidity"] is None  # no inventa un campo que no aplica a este caso


def test_classify_risk_reason_extracts_liquidity_values():
    out = classify_risk_reason("Liquidez insuficiente ($150,779 < 200,000)")
    assert out["liquidity"] == 150779.0
    assert out["min_liquidity"] == 200000.0
    assert out["risk_reward"] is None


def test_risk_evaluation_reject_event_includes_reason_code(session_logger):
    session_logger.risk_evaluation(
        symbol="NVDA", side="LONG", signal="BUY", decision="REJECT",
        reason="RR 0.67 < min 1.3",
    )
    rec = _read_records(session_logger.path)[0]
    assert rec["decision"] == "REJECT"
    assert rec["reason_code"] == "RR_BELOW_MINIMUM"
    assert rec["risk_reward"] == 0.67
    assert rec["min_risk_reward"] == 1.3


# ---------------- UTF-8 / encoding ----------------

REPRESENTATIVE_TEXT = "🧭 📈 ⛔ · Señal · posición · Última"


def test_utf8_roundtrip_exact_match(session_logger):
    """El requisito explícito: escribir el texto de ejemplo, reabrir el
    archivo forzando UTF-8 y que la cadena vuelva exactamente igual."""
    session_logger.strategy_evaluation(
        symbol="AAPL", timeframe="1Min", bar_timestamp=None, bar_close=None,
        strategy="ma", signal=None, raw_signal=None,
        reason=REPRESENTATIVE_TEXT, warmup_ok=True,
    )
    session_logger.close()

    with open(session_logger.path, "r", encoding="utf-8") as fh:
        line = fh.readline()
    record = json.loads(line)
    assert record["reason"] == REPRESENTATIVE_TEXT


def test_jsonl_file_is_valid_utf8_bytes_not_escaped(session_logger):
    """ensure_ascii=False: el emoji/acento queda como bytes UTF-8 reales en
    el archivo, no como \\uXXXX — así se puede inspeccionar a simple vista."""
    session_logger.strategy_evaluation(
        symbol="AAPL", timeframe="1Min", bar_timestamp=None, bar_close=None,
        strategy="ma", signal=None, raw_signal=None,
        reason=REPRESENTATIVE_TEXT, warmup_ok=True,
    )
    session_logger.close()
    raw_bytes = session_logger.path.read_bytes()
    assert "🧭".encode("utf-8") in raw_bytes
    assert "Señal".encode("utf-8") in raw_bytes
    assert b"\\u00f1" not in raw_bytes  # no quedó escapado


# ---------------- distinguir polling repetido de barras únicas ----------------

def test_repeated_polls_of_same_bar_share_bar_timestamp_but_have_distinct_observation_ids(session_logger):
    """Si el bot re-evalúa la misma vela varias veces (poll más rápido que la
    formación de la vela), cada observación debe seguir siendo identificable
    individualmente (observation_id distinto) pero agrupable por la misma
    (symbol, timeframe, bar_timestamp) — sin alterar el comportamiento real
    de que se evalúe más de una vez."""
    same_bar_ts = "2026-09-22T15:54:00Z"
    for _ in range(3):
        session_logger.strategy_evaluation(
            symbol="INTC", timeframe="1Min", bar_timestamp=same_bar_ts, bar_close=122.30,
            strategy="ma", signal="BUY", raw_signal="BUY", reason="misma vela", warmup_ok=True,
        )
    records = _read_records(session_logger.path)
    assert len(records) == 3
    assert all(r["bar_timestamp"] == same_bar_ts for r in records)
    observation_ids = [r["observation_id"] for r in records]
    assert len(set(observation_ids)) == 3  # cada polling queda identificable


def test_missing_optional_fields_are_null_not_omitted(session_logger):
    session_logger.risk_evaluation(
        symbol="MU", side="LONG", signal="BUY", decision="ACCEPT", reason="OK",
    )
    rec = _read_records(session_logger.path)[0]
    for field in ("entry_price", "stop_price", "take_profit", "position_size", "atr"):
        assert field in rec
        assert rec[field] is None


# ---------------- enabling structured logging no cambia la señal/decisión ----------------

def test_strategy_evaluation_logging_does_not_change_strategy_output(session_logger):
    df = pd.DataFrame({"close": [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 103.5, 103.2]})
    strat = MACrossover(fast=2, slow=4)
    before = strat.evaluate(df)

    session_logger.strategy_evaluation(
        symbol="TEST", timeframe="1Min", bar_timestamp="t", bar_close=103.2,
        strategy="ma", signal=before.signal, raw_signal=before.signal,
        reason=before.reason, warmup_ok=before.warmup_ok, indicator_values=before.values,
    )

    after = strat.evaluate(df)
    assert after.signal == before.signal
    assert after.reason == before.reason
    assert after.values == before.values


# ---------------- camino completo mockeado: strategy -> ensemble -> risk reject -> JSONL ----------------

class _AlwaysBuyStrategy:
    def evaluate(self, df):
        return StrategyResult("BUY", "siempre BUY (mock)", {"mock": True}, warmup_ok=True)

    def signal(self, df):
        return self.evaluate(df).signal


def test_full_mocked_path_strategy_to_ensemble_to_risk_rejection_produces_jsonl_records(session_logger):
    df = pd.DataFrame({"close": [100.0] * 5})
    wrappers = [StrategyWrapper("ma", _AlwaysBuyStrategy(), 1.0)]
    ensemble = Ensemble(mode="weighted", min_score=1.0)
    sig, meta_sig = ensemble.decide(df, wrappers)
    assert sig == "BUY"

    for name, d in meta_sig["details"].items():
        session_logger.strategy_evaluation(
            symbol="XYZ", timeframe="1Min", bar_timestamp="t0", bar_close=100.0,
            strategy=name, signal=d["signal"], raw_signal=d["raw_signal"],
            reason=d["reason"], warmup_ok=d["warmup_ok"], gated_by_regime=d["gated_by_regime"],
            indicator_values=d["values"],
        )
    session_logger.ensemble_decision(
        symbol="XYZ", timeframe="1Min", bar_timestamp="t0", bar_close=100.0,
        ensemble_mode=ensemble.mode, signal=sig, votes=meta_sig["votes"], score=meta_sig["score"],
        threshold=ensemble.min_score, reason=meta_sig["reason"],
    )

    risk = RiskManager(RiskConfig(min_liquidity_dollar=1_000_000), SimpleAdapter())
    bars = {
        "close": [100.0] * 25, "high": [101.0] * 25, "low": [99.0] * 25,
        "volume": [1.0] * 25,  # volumen minúsculo -> liquidez insuficiente
    }
    decision = risk.assess_entry("XYZ", Side.LONG, 100.0, bars)
    assert decision.allow is False

    session_logger.risk_evaluation(
        symbol="XYZ", side="LONG", signal=sig,
        decision="ACCEPT" if decision.allow else "REJECT", reason=decision.reason,
    )

    records = _read_records(session_logger.path)
    event_types = [r["event_type"] for r in records]
    assert event_types == ["strategy_evaluation", "ensemble_decision", "risk_evaluation"]
    risk_rec = records[-1]
    assert risk_rec["decision"] == "REJECT"
    assert risk_rec["reason_code"] == "LIQUIDITY_BELOW_MINIMUM"
