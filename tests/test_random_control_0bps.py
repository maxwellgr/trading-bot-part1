"""RANDOM CONTROL @ 0 BPS: reutiliza exactamente el control emparejado del audit y corre la gestión aislada a 0 bps."""
import json

import numpy as np
import pandas as pd
import pytest
import requests

from src import broker_alpaca
from src import random_control_0bps as rc0
from src import research_sanity_audit as ra
from tests.test_research_sanity_audit import SYMS, _synth, _write_1min, fake_sip_get  # noqa: F401

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)
        monkeypatch.setattr(requests, fn, boom)


def _accepted_bar(df):
    for t in range(60, len(df) - 20):
        r = ra._isolated_one("X", df, t, 100_000.0, "2024-03-25", 0.0)
        if r["status"] == "closed":
            return t
    raise AssertionError("ninguna entrada aceptada en el sintético")


def test_isolated_zero_bps_fills_at_open_and_five_bps_adds_slippage(monkeypatch):
    df = _synth(5, 10)
    t = _accepted_bar(df)
    from src.backtest_engine import BacktestEngine
    seen = {}
    orig = BacktestEngine.run

    def spy(self):
        res = orig(self)
        seen["trades"] = res.trades
        return res
    monkeypatch.setattr(BacktestEngine, "run", spy)
    for bps in (0.0, 5.0):
        ra._isolated_one("X", df, t, 100_000.0, "2024-03-25", bps)
        tr = seen["trades"][0]
        o = df.loc[pd.Timestamp(tr["entry_fill_timestamp"]), "open"]
        assert tr["entry_fill_price"] == pytest.approx(o * (1 + bps / 10_000), rel=1e-12)


def test_run_isolated_passes_bps_through():
    df = _synth(5, 10)
    t = _accepted_bar(df)
    for bps in (0.0, 5.0):
        got = ra.run_isolated([("X", t)], {"X": df}, 100_000.0, "2024-03-25", workers=1, bps=bps)[("X", t)]
        assert got == ra._isolated_one("X", df, t, 100_000.0, "2024-03-25", bps)
    assert ra.run_isolated([("X", t)], {"X": df}, 100_000.0, "2024-03-25", workers=1)[("X", t)] == \
        ra._isolated_one("X", df, t, 100_000.0, "2024-03-25", 5.0)                  # default del audit intacto


def test_verify_replicates_requires_identity():
    cols = list(ra.STAT_KEYS) + ["n"]
    a = pd.DataFrame([{c: float(i) for c in cols} for i in range(3)])
    stored = a.assign(strategy="H001")
    assert rc0.verify_replicates(a, stored, "H001")["identical_to_audit"]
    b = a.copy()
    b.loc[1, "median_ret_r_60m"] += 1e-6
    with pytest.raises(rc0.ReplicateMismatch):
        rc0.verify_replicates(b, stored, "H001")


def test_summarize_percentiles_and_band():
    per_rep = [{k: float(i) for k in rc0.METRICS} for i in range(100)]
    s = rc0.summarize("H", {k: 50.0 for k in rc0.METRICS}, per_rep)
    assert s["expectancy_r"]["real_empirical_percentile"] == pytest.approx(50.5)
    assert s["expectancy_r"]["real_within_random_q05_q95"] is True
    s2 = rc0.summarize("H", {k: 1000.0 for k in rc0.METRICS}, per_rep)
    assert s2["expectancy_r"]["real_empirical_percentile"] == 100.0 and not s2["expectancy_r"]["real_within_random_q05_q95"]


def test_protected_output_and_development_only(tmp_path):
    from src.research_protocol import load_protocol
    with pytest.raises(ra.AuditHygieneError):
        rc0.run(load_protocol(), tmp_path, tmp_path, ra.PROTECTED[2])
    with pytest.raises(ra.AuditHygieneError):
        ra.check_window("strategy", ra.VALIDATION[0], ra.VALIDATION[1])


def test_end_to_end_reuses_frozen_replicates(tmp_path_factory, monkeypatch):
    from src import research_h001 as r1, research_h003 as r3
    from src.research_protocol import load_protocol, validate_protocol
    root = tmp_path_factory.mktemp("rc0")
    for k, sym in enumerate(SYMS):
        _write_1min(root / "hist", sym, _synth(120 + k, 16), 130 + k)
    p = load_protocol()
    p["universe"]["symbols"] = SYMS
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": "2024-03-11", "end": "2024-03-25"},
                   {"name": "validation", "role": "validation", "start": "2026-01-02", "end": "2026-05-29"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2026-06-01", "end": "2026-09-23"},
                   {"name": "forward", "role": "forward", "start": "2026-09-24", "end": None}]
    p = validate_protocol(p)
    e = {"id": "X", "status": "IMPLEMENTED"}
    r1.build_report(r1.run_split(p, "development", root / "hist", e), p, root / "h001")
    r3.build_report(r3.run_split(p, "development", root / "hist", e), p, root / "h003", compare=False)
    monkeypatch.setattr(ra, "ATR_RANK_WINDOW", 120)
    audit = root / "audit"
    ra.build(p, root / "hist", audit, {"H001": root / "h001", "H003": root / "h003"}, workers=1, feed=False)
    out = root / "rc0"
    out.mkdir()
    S = rc0.run(p, root / "hist", audit, out, workers=1)
    for n in ("H001", "H003"):
        v = S["strategies"][n]
        assert v["replicate_verification"] == {"replicates": 200, "identical_to_audit": True}
        assert v["verdict"] in ("REAL_MATERIALLY_EXCEEDS_RANDOM", "REAL_SIMILAR_TO_RANDOM", "REAL_BELOW_RANDOM")
        assert set(v["comparison"]) == set(rc0.METRICS)
        assert len(pd.read_csv(out / f"random_isolated_0bps_replicates_{n.lower()}.csv")) == 200
    assert json.loads((out / "random_control_0bps_summary.json").read_text(encoding="utf-8"))["fill_slippage_bps"] == 0.0
