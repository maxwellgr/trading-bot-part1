"""Research Protocol V2: compuertas de admisión pre-portafolio (P1–P6), decisiones Q1–Q7, guardas. Sin datos de estrategia."""
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from src import preportfolio_screen as ps
from src import research_sanity_audit as ra

REPS = list(np.linspace(0, 0.5, 200))


def _p4_pass():
    return ps.gate_p4(0.10, 1.0, "mean_fwd_r", 1.0, REPS)          # A falso (10%) -> PASS


def _p5_na():
    return ps.gate_p5("iex", "iex", threshold_sensitive=False, secondary_feed_available=True)


def _pass_all(**over):
    g = {"P1": ps.gate_p1(500), "P2": ps.gate_p2("mean_fwd_r", 1.0, REPS),
         "P3": ps.gate_p3([{"name": "mfe", "real": 1.0, "replicates": [0.1, 0.2, 0.3], "higher_is_better": True},
                           {"name": "mae", "real": 0.1, "replicates": [0.4, 0.5, 0.6], "higher_is_better": False}]),
         "P4": _p4_pass(), "P5": ps.gate_p5("sip", "sip", True, True, jaccard=0.95), "P6": ps.gate_p6(True, True, True)}
    g.update(over)
    return g


# ---------------------------------------------------------------- protocolo
def test_v2_protocol_loads_records_decisions_and_v1_unchanged():
    p = ps.load_protocol_v2()
    assert p["protocol_version"] == "research_protocol_v2"
    assert p["pre_portfolio_gates"]["P1_sample_size"]["default_min_valid_forward_observations"] == 300
    assert p["random_control"]["default_replicates"] == 200
    assert p["pre_portfolio_gates"]["P2_primary_metric_vs_random"]["percentile_method"] == ps.PERCENTILE_METHOD
    assert set(p["review_decisions"]) >= {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"}
    assert p["execution_sensitivity_precheck"]["gating"] is False
    assert [s["name"] for s in p["splits"]] == ["warmup", "development", "validation", "known_diagnostic", "forward"]
    v1 = json.loads(Path("config/research_protocol_v1.json").read_text(encoding="utf-8"))
    assert v1["protocol_version"] == "research_protocol_v1" and "pre_portfolio_gates" not in v1
    with pytest.raises(ValueError):
        ps.load_protocol_v2(Path("config/research_protocol_v1.json"))


def test_statuses_listed():
    st = ps.load_protocol_v2()["statuses"]
    for s in ("REJECTED_AT_RAW_SIGNAL_SCREEN", "REJECTED_AT_ECONOMIC_SCALE", "REJECTED_AT_FEED_ROBUSTNESS",
              "REJECTED_PRE_PORTFOLIO", "PRE_PORTFOLIO_PASS", "DEVELOPMENT_PASS_VALIDATION_NOT_RUN", "FORWARD_TESTING"):
        assert s in st


# ---------------------------------------------------------------- aleatorio determinista y percentil
def test_matched_random_draws_are_deterministic():
    pools = [np.array([3, 5, 9]), np.array([1]), np.array([], dtype=np.int64)]
    for r in (0, 7, 199):
        np.testing.assert_array_equal(ps.draw_replicate(pools, r), ps.draw_replicate(pools, r))


def test_percentile_convention_and_ties_are_frozen():
    reps = list(range(100))
    assert ps.directional_percentile(95, reps, True) == pytest.approx(95.5)          # 95 below + 0.5 tie
    assert ps.directional_percentile(4, reps, False) == pytest.approx(100 - 4.5)
    assert ps.directional_percentile(5.0, [5.0] * 10) == 50.0                        # all ties -> 50
    assert ps.directional_percentile(1.0, [0.0, None, 2.0]) == 50.0                  # None replicates ignored
    assert ps.directional_percentile(None, reps) is None and ps.directional_percentile(1.0, []) is None
    assert ps.percentile_of is ra.percentile_of


def test_p2_threshold_and_unmeasured_fails():
    reps = list(range(100))
    assert ps.gate_p2("m", 93.9, reps)["outcome"] == "FAIL"                         # 94.0 < 95
    assert ps.gate_p2("m", 94.4, reps)["outcome"] == "PASS"                         # 95.0
    assert ps.gate_p2("m", 1, reps, higher_is_better=False)["outcome"] == "PASS"
    assert ps.gate_p2("m", None, reps)["outcome"] == "FAIL"


def test_p3_direction_aware_strictly_better():
    m = [{"name": "a", "real": 1.0, "replicates": [0, 1, 2], "higher_is_better": True},      # == mediana -> no
         {"name": "b", "real": 2.1, "replicates": [0, 1, 2], "higher_is_better": True},
         {"name": "c", "real": -1.0, "replicates": [0, 1, 2], "higher_is_better": False},     # menor es mejor
         {"name": "d", "real": 5.0, "replicates": [0, 1, 2], "higher_is_better": False}]      # mayor pero debía bajar
    g = ps.gate_p3(m)
    assert g["value"] == 2 and g["outcome"] == "PASS"
    assert [r["expected_direction"] for r in g["metrics"]] == [False, True, True, False]
    assert ps.gate_p3(m[:2])["outcome"] == "FAIL"


# ---------------------------------------------------------------- P1
def test_sample_size_counts_valid_observations():
    assert ps.gate_p1(300)["outcome"] == "PASS" and ps.gate_p1(299)["outcome"] == "FAIL"
    assert "valid raw-signal forward observations" in ps.gate_p1(300)["rule"]
    assert ps.gate_p1(150, preregistered_minimum=120, reason="structurally rare event")["outcome"] == "PASS"
    with pytest.raises(ValueError):
        ps.gate_p1(150, preregistered_minimum=120)
    assert ps.gate_p1(None)["outcome"] == "FAIL"


# ---------------------------------------------------------------- P4 (Q2)
def test_successful_signals_direction_normalized():
    rows = [{"side": "long", "return_0bps": 0.5, "favorable_move_pct": 0.8},
            {"side": "long", "return_0bps": -0.1, "favorable_move_pct": 0.9},   # no éxito
            {"side": "short", "return_0bps": -0.4, "favorable_move_pct": 0.6},  # short gana si el precio baja
            {"side": "short", "return_0bps": 0.3, "favorable_move_pct": 5.0},   # no éxito
            {"side": "long", "return_0bps": 0.0, "favorable_move_pct": 7.0}]    # 0 no es > 0
    assert ps.median_successful_favorable_move_pct(rows) == pytest.approx(0.7)
    assert ps.direction_normalized(0.2, "short") == -0.2
    with pytest.raises(ValueError):
        ps.direction_normalized(0.2, "flat")


def test_p4_fails_only_when_a_and_b():
    weak = list(np.linspace(0.9, 1.1, 200))                 # real 1.0 queda ~percentil 50 -> B verdadero
    assert ps.gate_p4(0.10, 0.30, "m", 1.0, weak)["outcome"] == "FAIL"          # 33% >= 25% y B
    assert ps.gate_p4(0.10, 0.30, "m", 1.0, REPS)["outcome"] == "PASS"          # A pero no B
    assert ps.gate_p4(0.10, 0.50, "m", 1.0, weak)["outcome"] == "PASS"          # 20% < 25%, B
    assert ps.gate_p4(0.10, 0.40, "m", 1.0, weak)["condition_a"] is True        # exactamente 25% -> A
    assert ps.gate_p4(0.10, None, "m", 1.0, weak)["outcome"] == "FAIL"
    assert ps.gate_p4(None, 1.0, "m", 1.0, REPS)["outcome"] == "FAIL"           # no medido
    assert ps.gate_p4(0.10, 0.30, "m", None, REPS)["condition_b"] is True       # real canónico no medido -> B


def test_p4_uses_same_p2_percentile_rule_and_direction():
    reps = list(range(100))
    g = ps.gate_p4(0.10, 0.20, "mae", 4, reps, higher_is_better=False)          # menor es mejor: orientado 95.5
    assert g["canonical_cost_primary_percentile"] == pytest.approx(95.5) and g["condition_b"] is False
    g2 = ps.gate_p4(0.10, 0.20, "exp_r", -0.01, [x / 100 for x in reps], primary_is_signed_return=True)
    assert g2["descriptive_primary_nonpositive_at_canonical_cost"] is True and g2["outcome"] == "FAIL"


# ---------------------------------------------------------------- P5 (Q1)
@pytest.mark.parametrize("j,band,outcome", [(0.95, "HIGH_STABILITY", "PASS"), (0.90, "HIGH_STABILITY", "PASS"),
                                            (0.85, "ACCEPTABLE_MODERATE", "PASS"), (0.80, "ACCEPTABLE_MODERATE", "PASS"),
                                            (0.77, "CAUTION", "PASS"), (0.75, "CAUTION", "PASS"),
                                            (0.69, "MATERIAL_FEED_SENSITIVITY", "FAIL")])
def test_feed_bands(j, band, outcome):
    g = ps.gate_p5("sip", "sip", True, True, jaccard=j)
    assert g["band"] == band and g["outcome"] == outcome


def test_p5_applicability_from_declarations():
    assert ps.p5_applicable("iex", "iex", False, True) is False
    assert ps.p5_applicable("iex", "iex", True, False) is False
    assert ps.p5_applicable("iex", "iex", True, True) is True        # sensible a umbrales + feed secundario
    assert ps.p5_applicable("sip", "iex", False, False) is True      # desajuste -> obligatorio
    assert _p5_na()["outcome"] == "NOT_APPLICABLE"
    assert ps.gate_p5("iex", "iex", True, True)["outcome"] == "FAIL"  # aplicable y no medido


def test_p5_feed_mismatch_requires_preregistered_justification():
    assert ps.gate_p5("sip", "iex", False, True, jaccard=0.95)["outcome"] == "FAIL"
    assert ps.gate_p5("sip", "iex", False, True, jaccard=0.95, mismatch_justification="x",
                      operationally_authoritative_feed="iex")["outcome"] == "PASS"
    assert ps.gate_p5("sip", "iex", False, True, jaccard=0.95, mismatch_justification="x",
                      operationally_authoritative_feed="other")["outcome"] == "FAIL"


def test_feed_exception_requires_all_three_preregistered_fields():
    full = {"why_sensitivity_expected": "x", "authoritative_feed": "sip", "why_operationally_meaningful": "y"}
    assert ps.gate_p5("sip", "sip", True, True, jaccard=0.6, preregistered_exception=full)["outcome"] == "PASS"
    part = {k: v for k, v in full.items() if k != "authoritative_feed"}
    assert ps.gate_p5("sip", "sip", True, True, jaccard=0.6, preregistered_exception=part)["outcome"] == "FAIL"


# ---------------------------------------------------------------- admisión (Q4)
def test_all_gates_pass_admits_with_p5_not_applicable():
    adm = ps.admission("HXXX", _pass_all(P5=_p5_na()))
    assert adm["status"] == "PRE_PORTFOLIO_PASS" and adm["full_portfolio_allowed"]
    assert adm["gate_outcomes"]["P5"] == "NOT_APPLICABLE" and adm["gate_outcomes"]["P1"] == "PASS"


@pytest.mark.parametrize("gate_name,bad,status", [
    ("P1", ps.gate_p1(10), "REJECTED_AT_RAW_SIGNAL_SCREEN"),
    ("P2", ps.gate_p2("m", 0.0, list(range(100))), "REJECTED_AT_RAW_SIGNAL_SCREEN"),
    ("P3", ps.gate_p3([]), "REJECTED_AT_RAW_SIGNAL_SCREEN"),
    ("P4", ps.gate_p4(0.1, 0.2, "m", 1.0, list(np.linspace(0.9, 1.1, 50))), "REJECTED_AT_ECONOMIC_SCALE"),
    ("P5", ps.gate_p5("sip", "sip", True, True, jaccard=0.5), "REJECTED_AT_FEED_ROBUSTNESS"),
    ("P6", ps.gate_p6(True, False, True), "REJECTED_PRE_PORTFOLIO")])
def test_single_failure_status(gate_name, bad, status):
    adm = ps.admission("HXXX", _pass_all(**{gate_name: bad}))
    assert adm["status"] == status and adm["pre_portfolio"] == "REJECTED_PRE_PORTFOLIO" and not adm["full_portfolio_allowed"]
    assert adm["gate_outcomes"][gate_name] == "FAIL"


def test_multi_stage_failures_and_same_stage_failures():
    adm = ps.admission("HXXX", _pass_all(P2=ps.gate_p2("m", 0, list(range(10))), P5=ps.gate_p5("sip", "sip", True, True, 0.4)))
    assert adm["status"] == "REJECTED_PRE_PORTFOLIO" and adm["failed_gates"] == ["P2", "P5"]
    same = ps.admission("HXXX", _pass_all(P1=ps.gate_p1(10), P2=ps.gate_p2("m", 0, list(range(10)))))
    assert same["status"] == "REJECTED_AT_RAW_SIGNAL_SCREEN" and same["failed_gates"] == ["P1", "P2"]
    with pytest.raises(ValueError):
        ps.admission("HXXX", {"P1": ps.gate_p1(500)})


def test_not_applicable_cannot_bypass_admission():
    forged = ps.gate("NOT_APPLICABLE", None, "skipped")
    adm = ps.admission("HXXX", _pass_all(P5=forged))                   # no derivado de declaraciones -> FAIL
    assert adm["failed_gates"] == ["P5"] and adm["gate_outcomes"]["P5"] == "FAIL"
    forged2 = dict(_p5_na(), threshold_sensitive=True, secondary_feed_available=True)   # declaraciones aplicables
    assert ps.admission("HXXX", _pass_all(P5=forged2))["failed_gates"] == ["P5"]
    adm3 = ps.admission("HXXX", _pass_all(P2=ps.gate("NOT_APPLICABLE", None, "x")))    # solo P5 puede ser N/A
    assert adm3["failed_gates"] == ["P2"]


def test_rejected_hypothesis_cannot_run_full_portfolio():
    calls = []
    runner = lambda *a, **k: calls.append((a, k)) or "ran"      # noqa: E731
    bad = ps.admission("HXXX", _pass_all(P1=ps.gate_p1(5)))
    with pytest.raises(ps.PortfolioAdmissionError):
        ps.run_full_development_if_admitted(bad, runner, 1, x=2)
    assert calls == []
    good = ps.admission("HXXX", _pass_all())
    assert ps.run_full_development_if_admitted(good, runner, 1, x=2) == "ran" and calls == [((1,), {"x": 2})]
    for tamper in (dict(good, failed_gates=["P3"]), dict(good, gate_outcomes=dict(good["gate_outcomes"], P3="FAIL")),
                   dict(good, pre_portfolio="REJECTED_PRE_PORTFOLIO")):
        with pytest.raises(ps.PortfolioAdmissionError):
            ps.require_admitted(tamper)


def test_deterministic_admission_output(tmp_path):
    a = ps.write_admission(ps.admission("HXXX", _pass_all()), tmp_path / "a.json")
    b = ps.write_admission(ps.admission("HXXX", _pass_all()), tmp_path / "b.json")
    assert hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest()


# ---------------------------------------------------------------- higiene
def test_no_validation_or_forward_access():
    ps.check_window("strategy", date(2024, 1, 2), date(2025, 12, 31))
    ps.check_window("feed", date(2026, 6, 1), date(2026, 9, 23))
    for kind, s, e in (("strategy", date(2026, 1, 2), date(2026, 5, 29)), ("feed", date(2026, 1, 2), date(2026, 2, 1)),
                       ("strategy", date(2026, 9, 24), date(2026, 10, 1)), ("feed", date(2026, 9, 20), date(2026, 9, 24))):
        with pytest.raises(ps.AuditHygieneError):
            ps.check_window(kind, s, e)
    assert ps.check_window is ra.check_window
