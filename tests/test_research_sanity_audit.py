"""
RESEARCH SANITY AUDIT V1: reproducción exacta, sensibilidad de ejecución por corridas completas, divergencia de
camino, punto de equilibrio descriptivo, borde crudo a horizonte fijo, control aleatorio emparejado (símbolo/mes/
tramo/quintil, respaldo, semillas), control aislado, etapas de riesgo, feeds IEX vs SIP (solo KNOWN) e higiene.
Sin red; datos sintéticos.
"""
import copy
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

from src import broker_alpaca
from src import research_sanity_audit as ra
from src.backtest_report import to_json
from src.research_protocol import load_protocol, validate_protocol
from src.run_paper import build_risk_config
from src.backtest_engine import production_args

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)
        monkeypatch.setattr(requests, fn, boom)


# ================================================================ datos sintéticos
def _synth(seed, sessions, start="2024-03-04", drift=0.06):
    rng = np.random.default_rng(seed)
    rows, idx, px = [], [], 100.0
    for d in pd.bdate_range(start, periods=sessions):
        for k in range(78):
            o = px + rng.normal(0, 0.1)
            c = o + drift + rng.normal(0, 0.35)
            rows.append((o, max(o, c) + abs(rng.normal(0, .15)), min(o, c) - abs(rng.normal(0, .15)), c, 20_000.0))
            idx.append(pd.Timestamp(f"{d.date()} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k))
            px = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))


def _rows_1min(sym, df5, seed, jitter=0.0):
    rng = np.random.default_rng(seed)
    rows = []
    for ts, r in df5.iterrows():
        path = np.linspace(r.open, r.close, 5) + rng.normal(0, 0.01, 5)
        path[0], path[-1] = r.open, r.close
        for k in range(5):
            o = (path[k - 1] if k else r.open) * (1 + jitter)
            c = path[k] * (1 + jitter)
            rows.append({"timestamp": (ts + pd.Timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": o,
                         "high": max(o, c) + 0.01, "low": min(o, c) - 0.01, "close": c, "volume": 20_000.0, "symbol": sym})
    return rows


def _write_1min(root, sym, df5, seed, jitter=0.0):
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_rows_1min(sym, df5, seed, jitter)).to_csv(d / f"{sym}.csv", index=False)


SYMS = ["AAA", "BBB", "CCC"]


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    from src import research_h001 as r1, research_h003 as r3
    root = tmp_path_factory.mktemp("rsa")
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
    # feed KNOWN sintético: IEX y "SIP" (SIP = IEX con ligero desplazamiento de precio y un minuto extra)
    known = {}
    for k, sym in enumerate(SYMS):
        df5 = _synth(300 + k, 6, start="2026-06-01")
        _write_1min(root / "iex_known", sym, df5, 400 + k)
        known[sym] = _rows_1min(sym, df5, 400 + k, jitter=0.0002)
    return root, p, known


class FakeResp:
    def __init__(self, bars, status=200):
        self.status_code, self._bars = status, bars

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} subscription does not permit")
            err.response = self
            raise err

    def json(self):
        return {"bars": self._bars, "next_page_token": None}


def fake_sip_get(known, calls):
    def get(url, headers=None, params=None, timeout=None):
        calls.append((url, dict(params)))
        sym = url.rstrip("/").split("/")[-2]
        rows = known.get(sym, [])
        return FakeResp([{"t": r["timestamp"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
                          "v": r["volume"] * 3} for r in rows])
    return get


@pytest.fixture(scope="module")
def built(mini, tmp_path_factory, monkeypatch_module):
    root, p, known = mini
    monkeypatch_module.setattr(ra, "ATR_RANK_WINDOW", 120)
    out = tmp_path_factory.mktemp("out")
    calls = []
    S = ra.build(p, root / "hist", out, {"H001": root / "h001", "H003": root / "h003"}, sip_dir=root / "sip",
                 workers=1, n_rep=200, sip_fetch={"get": fake_sip_get(known, calls), "data_url": "https://x/v2",
                                                  "headers": {}, "symbols": SYMS},
                 known_iex_dir=root / "iex_known")
    return S, out, calls


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


# ================================================================ reproducción
def test_exact_reproduction_h001_and_h003(built):
    S, _, _ = built
    for n in ("H001", "H003"):
        rep = S["reproduction"][n]
        assert rep["identical"]
        assert set(rep["files_identical"]) == {"trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv", "summary.json"}


def test_reproduction_failure_stops(mini, tmp_path):
    root, p, _ = mini
    bad = tmp_path / "bad"
    bad.mkdir()
    for f in ("trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv", "summary.json"):
        (bad / f).write_bytes((root / "h001" / f).read_bytes())
    (bad / "equity_curve.csv").write_text("x", encoding="utf-8")
    with pytest.raises(ra.ReproductionError):
        ra.build(p, root / "hist", tmp_path / "o", {"H001": bad}, workers=1, n_rep=2, feed=False, scenarios=(5.0,))


# ================================================================ ejecución
def test_all_scenarios_run_and_canonical_equals_stored(built, mini):
    S, out, _ = built
    root = mini[0]
    for n in ("H001", "H003"):
        rows = S["execution"][n]["scenarios"]
        assert [r["slippage_bps"] for r in rows] == [0.0, 2.5, 5.0, 7.5, 10.0, 15.0]
        stored = json.loads((root / n.lower() / "summary.json").read_text(encoding="utf-8"))
        can = next(r for r in rows if r["slippage_bps"] == 5.0)
        assert can["completed_trades"] == stored["trades"]["trades"] and can["expectancy_r"] == stored["trades"]["expectancy_r"]
        assert next(r for r in rows if r["slippage_bps"] == 0.0)["median_roundtrip_cost_r"] == 0.0
        assert (out / f"execution_sensitivity_{n.lower()}.csv").is_file()


def test_sensitivity_is_a_full_rerun_not_arithmetic(mini):
    root, p, _ = mini
    from src.h001_autopsy import load_development_bars
    from src.research_protocol import get_split
    sp = get_split(p, "development")
    bars = load_development_bars(root / "hist", SYMS, sp)["bars"]
    runs = ra.run_portfolios(p, sp, bars, ("H003",), (0.0, 15.0), workers=1)
    for b in (0.0, 15.0):
        for t in runs[("H003", b)].trades:
            leg0 = t["entry_fill_price"]
            o = bars[t["symbol"]].loc[pd.Timestamp(t["entry_fill_timestamp"]), "open"]
            assert leg0 == pytest.approx(o * (1 + b / 10_000), rel=1e-12)   # el simulador llenó a ese slippage


def test_path_divergence_accounting():
    def t(s, ts, pnl):
        return {"symbol": s, "entry_signal_timestamp": ts, "realized_pnl": pnl, "realized_r": pnl / 10}
    canon = [t("A", "1", 10), t("A", "2", -5), t("B", "3", 1)]
    other = [t("A", "1", 12), t("B", "4", 3)]
    d = ra.path_divergence(canon, other)
    assert d["common"]["count"] == 1 and d["scenario_only"]["count"] == 1 and d["canonical_5bps_only"]["count"] == 2
    assert d["common"]["pnl"] + d["scenario_only"]["pnl"] == 15 and d["canonical_5bps_only"]["pnl"] == -4


def test_break_even_interpolation_descriptive():
    rows = [{"slippage_bps": b, "expectancy_r": e} for b, e in ((0, 0.04), (2.5, 0.02), (5, -0.01), (7.5, -0.03))]
    be = ra.break_even(rows, "expectancy_r", 0.0)
    assert be["bps"] == pytest.approx(2.5 + 0.02 / 0.03 * 2.5)
    neg = [{"slippage_bps": b, "expectancy_r": e} for b, e in ((0, -0.01), (5, -0.05))]
    assert ra.break_even(neg, "expectancy_r", 0.0)["bps"] is None


# ================================================================ señales crudas
def _arrays():
    df = _synth(9, 3)
    return df, ra.indicator_arrays(df)


def test_forward_row_next_bar_entry_zero_bps_and_horizons():
    df, a = _arrays()
    cfg = build_risk_config(production_args())
    t = 100
    r = ra.forward_row(a, t, cfg)
    fill = df["open"].iloc[t + 1]                                  # 0 bps: apertura siguiente sin costo
    closes = df["close"].iloc[t + 1:t + 13].to_numpy()
    assert r["ret_r_60m"] == pytest.approx((closes[-1] - fill) / r["risk_ps"])
    assert r["mfe_r_60m"] == pytest.approx((closes.max() - fill) / r["risk_ps"])
    assert r["mae_r_60m"] == pytest.approx(-(closes.min() - fill) / r["risk_ps"])
    assert r["ret_r_30m"] == pytest.approx((closes[5] - fill) / r["risk_ps"])
    assert r["ret_r_60m_5bps"] == pytest.approx((closes[-1] - fill * 1.0005) / r["risk_ps"])
    assert r["reached_p050_60m"] == (r["mfe_r_60m"] >= 0.5) and r["reached_m100_60m"] == (r["mae_r_60m"] >= 1.0)


def test_no_lookahead_in_risk_unit_and_missing_future_is_deterministic():
    df, a = _arrays()
    cfg = build_risk_config(production_args())
    t = 120
    r1 = ra.forward_row(a, t, cfg)
    df2 = df.copy()
    df2.iloc[t + 1:, :4] *= 2.0
    r2 = ra.forward_row(ra.indicator_arrays(df2), t, cfg)
    assert r1["risk_ps"] == r2["risk_ps"] and r1["risk_pct"] == r2["risk_pct"]
    last = ra.forward_row(a, len(df) - 5, cfg)
    assert last["has_60m"] is False and last["mfe_r_60m"] is None
    assert ra.forward_row(a, len(df) - 1, cfg)["valid"] is False


# ================================================================ aleatorio emparejado
def test_atr_quintile_point_in_time():
    x = np.random.default_rng(1).random(400)
    q = ra.atr_quintile(x, window=100)
    y = x.copy()
    y[300:] = 99.0
    q2 = ra.atr_quintile(y, window=100)
    np.testing.assert_array_equal(q[:300], q2[:300])
    assert (q[:100] == -1).all() and set(q[100:]) <= {0, 1, 2, 3, 4}


def _cand(rows):
    return pd.DataFrame(rows, columns=["t", "month", "bucket", "quintile"])


def test_pools_match_month_bucket_quintile_and_fallback_hierarchy():
    cand = _cand([(1, "2024-03", 2, 3), (2, "2024-03", 2, 3), (3, "2024-03", 1, 3), (4, "2024-03", 2, 2),
                  (5, "2024-03", 3, 4), (6, "2024-04", 2, 3), (7, "2024-03", 5, 0)])
    sig = pd.DataFrame({"month": ["2024-03", "2024-03", "2024-03", "2024-03", "2024-03", "2024-05"],
                        "bucket": [2, 0, 4, 4, 5, 2], "quintile": [3, 3, 3, 0, 4, 3]})
    pools, lv = ra.build_pools(sig, cand)
    assert list(pools[0]) == [1, 2] and lv[0] == 0            # L0 exacto; t=6 (otro mes) nunca entra
    assert list(pools[1]) == [3] and lv[1] == 1               # L1 tramo adyacente
    assert list(pools[2]) == [5] and lv[2] == 3               # L3 tramo y quintil adyacentes (3±1 -> 4, tramo 3)
    assert list(pools[3]) == [7] and lv[3] == 1               # tramo 4 q0 -> L1 (tramo 5, q0)
    assert list(pools[4]) == [7] and lv[4] == 4               # tramo 5 q4 -> solo L4 (cualquier quintil)
    assert lv[5] == -1 and len(pools[5]) == 0                 # otro mes sin candidatas: sin emparejar


def test_fallback_level_2_and_4():
    cand = _cand([(1, "2024-03", 2, 0), (2, "2024-03", 2, 4)])
    sig = pd.DataFrame({"month": ["2024-03", "2024-03"], "bucket": [2, 2], "quintile": [1, 2]})
    pools, lv = ra.build_pools(sig, cand)
    assert lv == [2, 4] and list(pools[0]) == [1] and list(pools[1]) == [1, 2]


def test_candidates_same_symbol_month_exclude_real_and_need_history():
    full = _synth(3, 12)
    dev = full.iloc[200:]
    excl = {dev.index[50].isoformat()}
    cand = ra.candidate_table(full, dev, excl, None)
    assert dev.index[50].isoformat() not in set(cand["bar_timestamp"])
    assert (cand["t"] >= 15).all() and (cand["t"] + 12 < len(dev)).all()
    dec = pd.DatetimeIndex(cand["bar_timestamp"]).tz_convert(NY) + pd.Timedelta(minutes=5)
    assert ((dec.hour * 60 + dec.minute) < 960).all()          # nunca la vela 15:55 (decisión a las 16:00)


def test_draw_replicate_deterministic_and_seeded():
    pools = [np.array([1, 2, 3]), np.array([], dtype=np.int64), np.array([9])]
    a, b = ra.draw_replicate(pools, 7), ra.draw_replicate(pools, 7)
    np.testing.assert_array_equal(a, b)
    assert a[1] == -1 and a[2] == 9 and a[0] in (1, 2, 3)
    assert any((ra.draw_replicate(pools, r)[0] != a[0]) for r in range(20))


def test_random_control_200_replicates_reproducible_and_readonly(built):
    S, out, _ = built
    reps = pd.read_csv(out / "random_control_replicates.csv")
    for n in ("H001", "H003"):
        assert (reps["strategy"] == n).sum() == 200
    summ = pd.read_csv(out / "random_control_summary_h003.csv")
    assert set(summ["metric"]) == set(ra.STAT_KEYS)
    assert "fallback_usage" in S["random_control"]["H003"]


def test_random_control_rerun_identical_and_does_not_mutate_inputs(mini, monkeypatch):
    root, p, _ = mini
    monkeypatch.setattr(ra, "ATR_RANK_WINDOW", 120)
    from src.h001_autopsy import load_development_bars
    from src.research_protocol import get_split
    from src.strategy_v2_h001 import resample_rth_5min
    from src.historical_data import load_symbol_bars
    sp = get_split(p, "development")
    bars = load_development_bars(root / "hist", SYMS, sp)["bars"]
    full5 = {s: resample_rth_5min(load_symbol_bars(root / "hist", "1Min", s)) for s in SYMS}
    ind = {s: ra.indicator_arrays(bars[s]) for s in SYMS}
    cfg = build_risk_config(production_args())
    sig = pd.read_csv(root / "h003" / "h003_signals.csv")
    fwd = ra.raw_forward(list(zip(sig["symbol"], sig["bar_timestamp"])), ind, cfg)
    snap = fwd.copy(deep=True), {s: b.copy() for s, b in bars.items()}
    r1 = ra.random_control("H003", fwd, full5, bars, ind, cfg, n_rep=5)
    r2 = ra.random_control("H003", fwd, full5, bars, ind, cfg, n_rep=5)
    pd.testing.assert_frame_equal(r1["replicates"], r2["replicates"])
    pd.testing.assert_frame_equal(fwd, snap[0])
    for s in SYMS:
        pd.testing.assert_frame_equal(bars[s], snap[1][s])


def test_percentile_of():
    assert ra.percentile_of(5, [1, 2, 3, 4, 5, 6, 7, 8]) == pytest.approx((4 + 0.5) / 8 * 100)
    assert ra.percentile_of(None, [1]) is None


def test_isolated_control_reported_or_skipped(built):
    S, _, _ = built
    iso = S["isolated_control"]["H003"]
    assert {"real", "random"} <= set(iso)
    assert iso["real"]["attempts"] > 0 and "expectancy_r" in iso["random"]


# ================================================================ etapas de riesgo
def test_risk_stages_classification(built, mini):
    S, out, _ = built
    for n in ("H001", "H003"):
        st = S["risk_stages"][n]["stages"]
        assert st["stage1_raw"]["count"] >= st["stage2_stateless"]["count"]
        assert st["stage1_raw"]["count"] >= st["stage3_accepted"]["count"]
        chk = S["risk_stages"][n]["stateless_vs_actual_risk_manager"]
        assert chk["agree"] == chk["checked"]
    root = mini[0]
    trades = json.loads((root / "h003" / "trades.json").read_text(encoding="utf-8"))
    assert S["risk_stages"]["H003"]["stages"]["stage3_accepted"]["count"] == len(trades)
    assert (out / "risk_stage_comparison.csv").is_file()


def test_stage2_has_no_portfolio_state():
    df, a = _arrays()
    cfg = build_risk_config(production_args())
    sig = pd.DataFrame([{"symbol": "X", "bar_timestamp": df.index[t].isoformat(), **ra.forward_row(a, t, cfg),
                         "close": float(df["close"].iloc[t]), "atr_pct": 1.0} for t in (60, 90, 120)])
    s_empty = ra.stage_table(sig, set(), {"X": a}, cfg)
    s_all = ra.stage_table(sig, {("X", x) for x in sig["bar_timestamp"]}, {"X": a}, cfg)
    assert list(s_empty["stage2_stateless_pass"]) == list(s_all["stage2_stateless_pass"])
    assert not s_empty["stage3_accepted"].any() and s_all["stage3_accepted"].all()


def test_compression_verdict_rules():
    def rows(stage, seps):
        return [{"stage": stage, "feature": f, "separation_abs_cles_minus_half": s} for f, s in zip(ra.COMPARE_FEATURES, seps)]
    cmp = pd.DataFrame(rows("stage1_raw", [0.10, 0.01, 0, 0, 0, 0]) + rows("stage2_stateless", [0.05] + [0] * 5)
                       + rows("stage3_accepted", [0.04] + [0] * 5))
    assert ra.compression_verdict(cmp)["risk_filter_compression"] is True
    cmp2 = pd.DataFrame(rows("stage1_raw", [0.03] * 6) + rows("stage2_stateless", [0.03] * 6) + rows("stage3_accepted", [0.01] * 6))
    assert ra.compression_verdict(cmp2)["risk_filter_compression"] is False


# ================================================================ feeds
def test_feed_window_guards():
    ra.check_window("feed", date(2026, 6, 1), date(2026, 9, 23))
    ra.check_window("strategy", date(2024, 1, 2), date(2025, 12, 31))
    for k, s, e in (("feed", date(2026, 5, 29), date(2026, 6, 5)), ("feed", date(2026, 9, 1), date(2026, 9, 24)),
                    ("feed", date(2026, 1, 2), date(2026, 5, 29)), ("strategy", date(2025, 12, 1), date(2026, 1, 2)),
                    ("strategy", date(2026, 6, 1), date(2026, 9, 23)), ("feed", date(2024, 1, 2), date(2024, 2, 1))):
        with pytest.raises(ra.AuditHygieneError):
            ra.check_window(k, s, e)


def test_fetch_refuses_validation_and_forward():
    with pytest.raises(ra.AuditHygieneError):
        ra.fetch_feed_bars("NVDA", "sip", date(2026, 1, 2), date(2026, 5, 29), None, "u", {})
    with pytest.raises(ra.AuditHygieneError):
        ra.fetch_feed_bars("NVDA", "sip", date(2026, 9, 1), date(2026, 9, 30), None, "u", {})


def test_sip_separate_cache_known_only_and_iex_unchanged(built, mini):
    S, out, calls = built
    root = mini[0]
    assert S["feed"]["status"] == "AVAILABLE"
    for url, params in calls:
        assert params["feed"] == "sip" and params["adjustment"] == "raw"
        assert pd.Timestamp(params["start"]).tz_convert(NY).date() == date(2026, 6, 1)
        assert pd.Timestamp(params["end"]).tz_convert(NY).date() == date(2026, 9, 23)
    assert all((root / "sip" / "1Min" / f"{s}.csv").is_file() for s in SYMS)
    man = json.loads((out / "sip_data_manifest.json").read_text(encoding="utf-8"))
    assert man["feed"] == "sip" and man["all_integrity_pass"] and set(man["files"]) == set(SYMS)
    with pytest.raises(ra.AuditHygieneError):
        ra.download_sip(ra.IEX_DIR, get=lambda *a, **k: None, data_url="u", headers={})


def test_iex_files_unchanged_by_feed_section(mini, tmp_path):
    root, _, known = mini
    before = {s: hashlib.sha256((root / "iex_known" / "1Min" / f"{s}.csv").read_bytes()).hexdigest() for s in SYMS}
    ra.feed_section(tmp_path, tmp_path / "sip2", 1, root / "iex_known",
                    {"get": fake_sip_get(known, []), "data_url": "u", "headers": {}, "symbols": SYMS})
    after = {s: hashlib.sha256((root / "iex_known" / "1Min" / f"{s}.csv").read_bytes()).hexdigest() for s in SYMS}
    assert before == after


def test_sip_unavailable_is_reported_not_bypassed(tmp_path):
    def get(url, headers=None, params=None, timeout=None):
        return FakeResp([], status=403)
    man = ra.download_sip(tmp_path / "sip", get=get, data_url="u", headers={}, symbols=["NVDA"])
    assert man["status"] == "UNAVAILABLE" and "403" in man["reason"]
    assert not (tmp_path / "sip" / "1Min" / "NVDA.csv").exists()


def test_one_minute_and_five_minute_comparison():
    idx = pd.date_range(pd.Timestamp("2026-06-01 09:28", tz=NY), periods=12, freq="1min").tz_convert("UTC")
    base = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0}, index=idx)
    iex = base.drop(idx[5])
    sip = base.copy()
    sip.loc[:, "close"] = 100.1
    sip.loc[:, "volume"] = 30.0
    r = ra.compare_frames(iex, sip, rth_only=True)
    assert r["iex_count"] == 9 and r["sip_count"] == 10 and r["overlap"] == 9 and r["sip_only"] == 1 and r["iex_only"] == 0
    assert r["abs_close_diff_bps_median"] == pytest.approx(10.0) and r["abs_open_diff_bps_median"] == 0.0
    assert r["sip_iex_volume_ratio_median"] == 3.0
    from src.strategy_v2_h001 import resample_rth_5min
    r5 = ra.compare_frames(resample_rth_5min(iex), resample_rth_5min(sip), rth_only=False)
    assert r5["overlap"] == 2 and r5["abs_close_diff_bps_median"] == pytest.approx(10.0)


def test_signal_jaccard_bands_and_near_matching():
    iex = {"A": ["2026-06-02T15:00:00+00:00", "2026-06-02T16:00:00+00:00"], "B": ["2026-06-03T15:00:00+00:00"]}
    sip = {"A": ["2026-06-02T15:00:00+00:00", "2026-06-02T16:05:00+00:00"], "B": []}
    o = ra.signal_overlap(iex, sip)
    assert o["exact_matches"] == 1 and o["iex_only"] == 2 and o["sip_only"] == 1
    assert o["jaccard"] == pytest.approx(1 / 4) and o["band"] == "DATA_FEED_SENSITIVE"
    assert o["iex_only_with_sip_signal_within_1_bar"] == 1 and o["sip_only_with_iex_signal_within_1_bar"] == 1
    same = ra.signal_overlap(iex, iex)
    assert same["jaccard"] == 1.0 and same["band"] == "DATA_FEED_STABLE"


def test_raw_signal_generator_matches_engine(built):
    S, _, _ = built
    for n in ("H001", "H003"):
        assert S["raw_signal_generator_validation"][n]["identical"]


def test_known_feed_signals_reported(built):
    S, out, _ = built
    ov = pd.read_csv(out / "iex_sip_signal_overlap.csv")
    assert set(ov["strategy"]) == {"H001", "H003"} and {"jaccard", "band"} <= set(ov.columns)


# ================================================================ higiene, salidas y determinismo
def test_protected_outputs_refused(mini):
    _, p, _ = mini
    with pytest.raises(ra.AuditHygieneError):
        ra.build(p, Path("x"), Path("data") / "research_v1" / "h003_development", {}, feed=False)


def test_outputs_and_labels(built):
    S, out, _ = built
    for f in ("execution_sensitivity_h001.csv", "execution_sensitivity_h003.csv", "execution_path_divergence.csv",
              "raw_signal_forward_h001.csv", "raw_signal_forward_h003.csv", "random_control_summary_h001.csv",
              "random_control_summary_h003.csv", "random_control_replicates.csv", "risk_stage_h001.csv", "risk_stage_h003.csv",
              "risk_stage_comparison.csv", "filter_value_h001.csv", "filter_value_h003.csv", "cost_move_scale.csv",
              "iex_sip_1min_comparison.csv", "iex_sip_5min_comparison.csv", "iex_sip_signal_overlap.csv",
              "sip_data_manifest.json", "research_sanity_summary.json"):
        assert (out / f).is_file(), f
    allowed = {"SIGNAL_EDGE_ABSENT", "SIGNAL_EDGE_TOO_SMALL_FOR_COST", "RISK_FILTER_COMPRESSION", "DATA_FEED_SENSITIVE",
               "DATA_FEED_STABLE", "UNRESOLVED"}
    for n, v in S["labels"]["per_strategy"].items():
        assert set(v["labels"]) <= allowed


def test_deterministic_outputs(built, mini, tmp_path, monkeypatch):
    S, out, _ = built
    root, p, known = mini
    monkeypatch.setattr(ra, "ATR_RANK_WINDOW", 120)
    out2 = tmp_path / "again"
    ra.build(p, root / "hist", out2, {"H001": root / "h001", "H003": root / "h003"}, sip_dir=root / "sip", workers=1,
             n_rep=200, sip_fetch={"get": fake_sip_get(known, []), "data_url": "https://x/v2", "headers": {}, "symbols": SYMS},
             known_iex_dir=root / "iex_known")
    for f in ("execution_sensitivity_h003.csv", "raw_signal_forward_h001.csv", "random_control_replicates.csv",
              "risk_stage_comparison.csv", "iex_sip_signal_overlap.csv", "cost_move_scale.csv"):
        assert (out / f).read_bytes() == (out2 / f).read_bytes(), f
