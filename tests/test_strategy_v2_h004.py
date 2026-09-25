"""
STRATEGY_V2_HYPOTHESIS_004: compuerta SPY sobre H003 congelado. Alineación 15Min (10:05/10:15, sin cubetas
parciales, sin sustitución), régimen estricto, enrutamiento (BLOCKED/UNAVAILABLE nunca llegan al RiskManager),
consumo del setup, sin salida SPY, SPY nunca operable ni en D6, auditoría temporal, diagnóstico de pendiente,
sesión truncada, reproducción de H003, descomposición A/B/B′/C, sombra de solo lectura, determinismo e higiene.
Sin red; datos sintéticos.
"""
import copy
import inspect
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca
from src import research_h004 as rh
from src import spy_context_data as scd
from src import strategy_v2_h004 as h4
from src.research_protocol import load_protocol, validate_protocol
from src.strategy_v2_h001 import HygieneError, _own_window_ema
from src.strategy_v2_h003 import ConsolidationBreakoutH003

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


def et(s):
    return pd.Timestamp(s, tz=NY)


# ================================================================ datos sintéticos
def spy_1min(days, seed=7, skip=None, trunc=None, amp=2.0):
    """SPY 1Min RTH sintético con tendencia oscilante (ambos regímenes aparecen)."""
    rng = np.random.default_rng(seed)
    rows, px, k = [], 400.0, 0
    for d in days:
        end = trunc.get(d, "16:00") if trunc else "16:00"
        for t in pd.date_range(et(f"{d} 09:30"), et(f"{d} {end}"), freq="1min", inclusive="left"):
            k += 1
            if skip and t.strftime("%Y-%m-%d %H:%M") in skip:
                continue
            drift = amp * np.sin(k / 700.0) * 0.01
            o = px
            c = o + drift + rng.normal(0, 0.05)
            rows.append((t.tz_convert("UTC"), o, max(o, c) + 0.02, min(o, c) - 0.02, c, 1000.0))
            px = c
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"]).set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index, name="timestamp")
    return df


def bars15_for(days, **kw):
    return scd.resample_rth(spy_1min(days, **kw))


def bdays(a, b):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(a, b)]


SPLIT = {"name": "development", "role": "development", "start": "2024-03-11", "end": "2024-03-19"}
SPY_DAYS = bdays("2024-02-20", "2024-03-19")


def ctx_for(days=SPY_DAYS, split=SPLIT, **kw):
    b = bars15_for(days, **kw)
    return rh.build_context(b, split, [date.fromisoformat(d) for d in days])


# ================================================================ 1. alineación
@pytest.mark.parametrize("dec,exp", [("2024-03-12 10:05", "09:45"), ("2024-03-12 10:10", "09:45"),
                                     ("2024-03-12 10:15", "10:00"), ("2024-03-12 10:29", "10:00"),
                                     ("2024-03-12 15:55", "15:30"), ("2024-07-03 13:00", "12:45"),
                                     ("2024-11-29 13:00", "12:45")])
def test_expected_bucket_boundaries(dec, exp):
    s = h4.expected_bucket(et(dec))
    assert s.tz_convert(NY).strftime("%H:%M") == exp
    assert s + pd.Timedelta(minutes=15) <= et(dec)          # nunca una cubeta parcial


def test_decision_before_first_complete_bucket_is_an_alignment_error():
    with pytest.raises(h4.AlignmentError):
        h4.expected_bucket(et("2024-03-12 09:40"))


def test_10_05_uses_0945_bucket_and_10_15_uses_1000_bucket():
    c = ctx_for()["context"]
    r1 = c.regime_at(et("2024-03-12 10:05").tz_convert("UTC"))
    assert pd.Timestamp(r1["spy_bar_start"]).tz_convert(NY).strftime("%H:%M") == "09:45"
    assert pd.Timestamp(r1["spy_bar_end"]) <= et("2024-03-12 10:05")
    r2 = c.regime_at(et("2024-03-12 10:15").tz_convert("UTC"))
    assert pd.Timestamp(r2["spy_bar_start"]).tz_convert(NY).strftime("%H:%M") == "10:00"


def test_no_partial_or_future_spy_bar_influences_the_regime():
    base = spy_1min(SPY_DAYS)
    D = et("2024-03-14 11:05")
    mod = base.copy()
    later = mod.index >= D.tz_convert("UTC") - pd.Timedelta(minutes=5)     # 11:00–11:05 (parcial) y futuro
    mod.loc[later, ["open", "high", "low", "close"]] *= 1.5
    days = [date.fromisoformat(d) for d in SPY_DAYS]
    a = rh.build_context(scd.resample_rth(base), SPLIT, days)["context"].regime_at(D.tz_convert("UTC"))
    b = rh.build_context(scd.resample_rth(mod), SPLIT, days)["context"].regime_at(D.tz_convert("UTC"))
    assert a == b


def test_missing_expected_bucket_is_unavailable_without_fallback():
    skip = {f"2024-03-13 10:{m:02d}" for m in range(0, 15)}
    c = ctx_for(skip=skip)["context"]
    r = c.regime_at(et("2024-03-13 10:20").tz_convert("UTC"))
    assert r["status"] == h4.UNAVAILABLE and r["spy_bar_start"] is None     # la cubeta 09:45 existe pero no se usa
    assert c.regime_at(et("2024-03-13 10:05").tz_convert("UTC"))["status"] != h4.UNAVAILABLE


# ================================================================ 2. régimen
def test_regime_strict_boundaries():
    assert h4.regime_status(101.0, 100.0, 99.0) == h4.POSITIVE
    assert h4.regime_status(100.0, 100.0, 99.0) == h4.BLOCKED          # close == EMA50
    assert h4.regime_status(101.0, 100.0, 100.0) == h4.BLOCKED         # pendiente == 0
    assert h4.regime_status(99.0, 100.0, 99.0) == h4.BLOCKED
    assert h4.regime_status(101.0, 100.0, 100.5) == h4.BLOCKED
    assert h4.regime_status(float("nan"), 100.0, 99.0) == h4.BLOCKED


def test_spy_ema50_is_h003_own_window_helper_with_split_boundary_truncation():
    info = ctx_for()
    c = info["context"]
    assert info["support_bars"] == 200
    expect = _own_window_ema(c.close, list(range(len(c.close))), 50)
    np.testing.assert_array_equal(c.ema50, expect)
    # vela de soporte temprana: ventana expansiva (solo velas cargadas), igual que las acciones
    assert c.ema50[5] == pd.Series(c.close[:6]).ewm(span=50, adjust=False).mean().iloc[-1]


def test_slope_uses_three_existing_bars_and_regime_values():
    c = ctx_for()["context"]
    D = et("2024-03-14 12:05").tz_convert("UTC")
    r = c.regime_at(D)
    k = c.pos[int(pd.Timestamp(r["spy_bar_start"]).value)]
    assert r["spy_ema50_r_minus_3"] == c.ema50[k - 3] and r["spy_close"] == c.close[k]
    assert r["status"] == h4.regime_status(c.close[k], c.ema50[k], c.ema50[k - 3])


def test_support_fewer_than_200_fails():
    with pytest.raises(rh.ContextError):
        ctx_for(days=bdays("2024-03-05", "2024-03-19"))


def test_truncated_session_behavior_and_slope_gap_flags():
    trunc = {"2024-03-13": "10:22"}
    info = ctx_for(trunc=trunc)
    c = info["context"]
    U = lambda s: c.regime_at(et(s).tz_convert("UTC"))           # noqa: E731
    assert U("2024-03-13 10:25")["status"] != h4.UNAVAILABLE        # 10:00–10:15 existe
    assert U("2024-03-13 10:35")["status"] != h4.UNAVAILABLE        # 10:15–10:30 parcial pero existe (>= 1 vela)
    assert U("2024-03-13 10:50")["status"] == h4.UNAVAILABLE        # 10:30–10:45 ausente
    nxt = [U(f"2024-03-14 {t}") for t in ("10:05", "10:20", "10:35")]
    assert [r["slope_window_spans_missing_bucket"] for r in nxt] == [True, True, False]
    assert info["bucket_level_slope_gap_windows"] == 3


# ================================================================ 3. envoltorio
def test_wrapper_passthrough_for_non_buy_and_never_sells():
    c = ctx_for()["context"]
    w = h4.RegimeGatedH004(c)
    df = pd.DataFrame({"open": [1.0] * 10, "high": [1.0] * 10, "low": [1.0] * 10, "close": [1.0] * 10, "volume": [1.0] * 10},
                      index=pd.date_range("2024-03-12 14:30", periods=10, freq="5min", tz="UTC"))
    r = w.evaluate(df)
    assert r.signal is None and w.gate_log == []
    assert '"SELL"' not in inspect.getsource(h4.RegimeGatedH004)


def test_no_public_gate_bypass():
    sig = inspect.signature(h4.RegimeGatedH004.__init__)
    assert list(sig.parameters) == ["self", "context"]
    src = inspect.getsource(rh.main).lower()
    assert "_parent_reproduction_strategy" not in src and "gate" not in src and "bypass" not in src
    assert "disable" not in src and "context_override" not in src
    assert inspect.getsource(rh.run_split).count("RegimeGatedH004(ctx[\"context\"])") == 1


# ================================================================ extremo a extremo (mini)
def _synth(seed, sessions, start="2024-03-04"):
    rng = np.random.default_rng(seed)
    rows, idx, px = [], [], 100.0
    for d in pd.bdate_range(start, periods=sessions):
        for k in range(78):
            o = px + rng.normal(0, 0.1)
            c = o + 0.06 + rng.normal(0, 0.35)
            rows.append((o, max(o, c) + abs(rng.normal(0, .15)), min(o, c) - abs(rng.normal(0, .15)), c, 20_000.0))
            idx.append(pd.Timestamp(f"{d.date()} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k))
            px = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))


def _write_1min(root, sym, df5, seed):
    rng = np.random.default_rng(seed)
    rows = []
    for ts, r in df5.iterrows():
        path = np.linspace(r.open, r.close, 5) + rng.normal(0, 0.01, 5)
        path[0], path[-1] = r.open, r.close
        for k in range(5):
            o = path[k - 1] if k else r.open
            rows.append({"timestamp": (ts + pd.Timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": o,
                         "high": max(o, path[k]) + 0.01, "low": min(o, path[k]) - 0.01, "close": path[k],
                         "volume": 20_000.0, "symbol": sym})
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(d / f"{sym}.csv", index=False)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    from src import research_h003 as r3
    root = tmp_path_factory.mktemp("h4")
    for k, sym in enumerate(["AAA", "BBB", "CCC"]):
        _write_1min(root / "hist", sym, _synth(120 + k, 12), 130 + k)
    p = load_protocol()
    p["universe"]["symbols"] = ["AAA", "BBB", "CCC"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   dict(SPLIT),
                   {"name": "validation", "role": "validation", "start": "2024-03-20", "end": "2024-03-21"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    p = validate_protocol(p)
    r3.build_report(r3.run_split(p, "development", root / "hist", {"id": "X", "status": "IMPLEMENTED"}), p,
                    root / "h003", compare=False)
    return root, p


ENTRY = {"id": h4.HYPOTHESIS_ID, "status": "SPECIFIED_NOT_IMPLEMENTED"}


def _run(mini, tmp, ctx=None):
    root, p = mini
    ctx = ctx or ctx_for()
    return rh.run_split(p, "development", root / "hist", ENTRY, None, None, root / "h003", tmp / "repro",
                        context_override=ctx)


class ForcedContext:
    """Contexto real (alineación real) con el estado forzado por una función del instante de decisión."""

    def __init__(self, real, fn):
        self.real, self.fn = real, fn

    def regime_at(self, d):
        r = dict(self.real.regime_at(d))
        r["status"] = self.fn(d)
        return r


def _forced(fn):
    info = ctx_for()
    return dict(info, context=ForcedContext(info["context"], fn))


def _h003_raw(mini):
    root, _ = mini
    sig = pd.read_csv(root / "h003" / "h003_signals.csv")
    return sorted(zip(sig["symbol"], sig["bar_timestamp"]))


@pytest.fixture(scope="module")
def real_run(mini, tmp_path_factory):
    return _run(mini, tmp_path_factory.mktemp("rr"))


def test_h003_reproduction_is_byte_identical(real_run):
    rep = real_run["reproduction"]
    assert rep["identical"] and set(rep["files_identical"]) == {"trades.csv", "trades.json", "daily_results.csv",
                                                                "equity_curve.csv", "summary.json"}


def test_failed_reproduction_aborts_h004(mini, tmp_path):
    root, p = mini
    bad = tmp_path / "bad_h003"
    bad.mkdir()
    for f in ("trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv", "summary.json"):
        (bad / f).write_bytes((root / "h003" / f).read_bytes())
    (bad / "trades.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(rh.ReproductionError):
        rh.run_split(p, "development", root / "hist", ENTRY, None, None, bad, tmp_path / "r", context_override=ctx_for())


def test_raw_h003_signals_identical_and_funnel_identity(real_run, mini):
    rows = real_run["gate"]
    assert sorted((r["symbol"], r["bar_timestamp"]) for r in rows) == _h003_raw(mini)
    f = rh.funnel(rows, real_run["result"])
    assert f["identity_raw_eq_blocked_plus_unavailable_plus_passed"] and f["engine_buy_signals_equal_passed"]
    assert f["identity_passed_eq_downstream"]
    assert f["market_regime_blocked"] > 0 and f["market_regime_passed"] > 0   # el sintético produce ambos


@pytest.mark.parametrize("status", [h4.BLOCKED, h4.UNAVAILABLE])
def test_blocked_and_unavailable_never_reach_risk_manager_and_consume_setup(mini, tmp_path, status):
    run = _run(mini, tmp_path, _forced(lambda d: status))
    res = run["result"]
    assert res.risk_evaluations == [] and res.trades == []
    assert sum(v["BUY"] for v in res.counters["signals"].values()) == 0
    # consumo: la secuencia cruda es la de H003 (ninguna re-emisión del mismo setup tras el bloqueo)
    assert sorted((r["symbol"], r["bar_timestamp"]) for r in run["gate"]) == _h003_raw(mini)
    assert all(r["status"] == status for r in run["gate"])


def test_positive_regime_reaches_normal_path_exactly_like_h003(mini, tmp_path):
    root, _ = mini
    run = _run(mini, tmp_path, _forced(lambda d: h4.POSITIVE))
    stored = json.loads((root / "h003" / "trades.json").read_text(encoding="utf-8"))
    from src.backtest_report import to_json
    assert json.loads(to_json(run["result"].trades)) == stored            # gestión idéntica; sin salida SPY
    assert {r["reason_code"] for r in run["result"].risk_evaluations} and len(run["result"].risk_evaluations) > 0


def test_mixed_regime_setup_consumption_and_rearm_unchanged(mini, tmp_path):
    flip = lambda d: h4.POSITIVE if (pd.Timestamp(d).minute // 5) % 2 else h4.BLOCKED   # noqa: E731
    run = _run(mini, tmp_path, _forced(flip))
    assert sorted((r["symbol"], r["bar_timestamp"]) for r in run["gate"]) == _h003_raw(mini)
    passed = {(r["symbol"], r["bar_timestamp"]) for r in run["gate"] if r["status"] == h4.POSITIVE}
    reached = {(r["symbol"], r["bar_timestamp"]) for r in run["result"].risk_evaluations}
    assert reached <= passed
    assert all((t["symbol"], t["entry_signal_timestamp"]) in passed for t in run["result"].trades)


def test_no_spy_exit_regime_flip_after_entry_changes_nothing(mini, tmp_path):
    """Una vez dentro, el régimen no cierra nada: salidas idénticas a H003 para los trades comunes."""
    root, _ = mini
    stored = json.loads((root / "h003" / "trades.json").read_text(encoding="utf-8"))
    first = min(stored, key=lambda t: (t["entry_signal_timestamp"], t["symbol"]))
    dec = pd.Timestamp(first["entry_signal_timestamp"]) + pd.Timedelta(minutes=5)
    # POSITIVE solo en la decisión del primer trade; BLOCKED en todo lo demás (incluida toda la vida del trade)
    run = _run(mini, tmp_path, _forced(lambda d: h4.POSITIVE if pd.Timestamp(d) == dec else h4.BLOCKED))
    got = [t for t in run["result"].trades if t["symbol"] == first["symbol"]
           and t["entry_signal_timestamp"] == first["entry_signal_timestamp"]]
    assert len(got) == 1
    from src.backtest_report import to_json
    assert json.loads(to_json(got[0])) == first                      # misma salida, mismas piernas: sin salida SPY


def test_spy_never_tradable_and_excluded_from_d6(real_run, mini, tmp_path):
    _, p = mini
    res = real_run["result"]
    assert "SPY" not in res.counters["signals"] and all(t["symbol"] != "SPY" for t in res.trades)
    rep = rh.build_report(real_run, p, tmp_path / "out", mini[0] / "h003")
    d6 = rep["gates"]["gates"]["D6_symbol_concentration"]["detail"]
    assert "SPY" not in json.dumps(d6)
    assert "SPY" not in {row["symbol"] for row in rep["by_symbol"]}


def test_time_alignment_audit_and_injected_violation(real_run):
    assert real_run["alignment"] == {"rows": len(real_run["gate"]), "violations": 0, "pass": True}
    rows = copy.deepcopy(real_run["gate"][:3])
    r = next(x for x in rows if x["spy_bar_start"])
    r["spy_bar_end"] = (pd.Timestamp(r["decision_time"]) + pd.Timedelta(minutes=10)).isoformat()   # futura/parcial
    with pytest.raises(h4.AlignmentError):
        rh.alignment_audit(rows)
    rows = copy.deepcopy(real_run["gate"][:3])
    rows[0]["expected_spy_bucket_end"] = (pd.Timestamp(rows[0]["decision_time"]) + pd.Timedelta(minutes=1)).isoformat()
    with pytest.raises(h4.AlignmentError):
        rh.alignment_audit(rows)


def test_report_outputs_shadow_readonly_and_determinism(real_run, mini, tmp_path):
    _, p = mini
    before = copy.deepcopy(real_run["result"].trades)
    rep = rh.build_report(real_run, p, tmp_path / "a", mini[0] / "h003")
    assert real_run["result"].trades == before                       # la sombra no muta el portafolio real
    for f in ("summary.json", "trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv", "signal_funnel.csv",
              "signal_funnel.json", "market_regime_audit.csv", "path_decomposition.csv", "blocked_signal_shadow.csv",
              "regime_diagnostics.json", "h004_report.json"):
        assert (tmp_path / "a" / f).is_file(), f
    rh.build_report(real_run, p, tmp_path / "b", mini[0] / "h003")
    for f in ("trades.csv", "market_regime_audit.csv", "path_decomposition.csv", "blocked_signal_shadow.csv",
              "regime_diagnostics.json", "signal_funnel.csv"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f
    sh = rep["blocked_signal_shadow"]
    assert sh["stateless_check_vs_actual_risk_manager"]["agree"] == sh["stateless_check_vs_actual_risk_manager"]["checked"] > 0
    g = sh["groups"]["MARKET_REGIME_BLOCKED"]
    assert set(g) == {"A_all_valid", "B_stateless_rr_liquidity_eligible"}
    assert "weak_forward_excursion_pct" in g["A_all_valid"]
    pdc = rep["path_decomposition"]
    assert pdc["identity_A_B_Bprime_eq_h003_trades"] and pdc["bridge"]["reconciles"]
    assert rep["comparison_h003"]["raw_signals_identical_count"] is True


def test_rerun_is_deterministic(mini, tmp_path, real_run):
    again = _run(mini, tmp_path)
    assert again["gate"] == real_run["gate"]
    from src.backtest_report import to_json
    assert to_json(again["result"].trades) == to_json(real_run["result"].trades)


# ================================================================ descomposición A/B/B′/C (construida)
def test_path_decomposition_partition_and_bridge():
    def t(sym, ts, pnl, r):
        return {"symbol": sym, "entry_signal_timestamp": ts, "realized_pnl": pnl, "realized_r": r}
    h3 = [t("A", "1", 10, 1), t("A", "2", -5, -0.5), t("B", "3", 7, 0.7), t("B", "4", -2, -0.2)]
    h4t = [t("A", "1", 12, 1), t("C", "9", 3, 0.3)]
    rows = [{"symbol": "A", "bar_timestamp": "1", "status": h4.POSITIVE}, {"symbol": "A", "bar_timestamp": "2", "status": h4.BLOCKED},
            {"symbol": "B", "bar_timestamp": "3", "status": h4.UNAVAILABLE}, {"symbol": "B", "bar_timestamp": "4", "status": h4.POSITIVE},
            {"symbol": "C", "bar_timestamp": "9", "status": h4.POSITIVE}]
    s = rh.path_decomposition(h3, h4t, rows)["summary"]
    assert (s["A_in_h003"]["count"], s["B"]["count"], s["B_prime"]["count"], s["C"]["count"]) == (1, 2, 1, 1)
    assert s["B"]["pnl"] == 2 and s["B_prime"]["pnl"] == -2 and s["C"]["pnl"] == 3
    assert s["identity_A_B_Bprime_eq_h003_trades"] and s["identity_A_C_eq_h004_trades"] and s["bridge"]["reconciles"]
    with pytest.raises(h4.AlignmentError):
        rh.path_decomposition(h3, h4t, rows[:1])


def test_immediate_failure_definition():
    tr = [{"mfe_r": 0.1, "realized_r": -1.0, "realized_pnl": -10}, {"mfe_r": 0.25, "realized_r": -0.2, "realized_pnl": -2},
          {"mfe_r": 0.1, "realized_r": 0.0, "realized_pnl": 0}, {"mfe_r": 0.5, "realized_r": 0.3, "realized_pnl": 3}]
    r = rh.immediate_failure(tr)
    assert r["immediate_failures"] == 1 and r["rate_pct"] == 25.0


# ================================================================ higiene y contexto auditado
@pytest.mark.parametrize("split", ["validation", "known_diagnostic", "forward"])
def test_non_development_splits_refused(mini, tmp_path, split):
    root, p = mini
    with pytest.raises(HygieneError):
        rh.run_split(p, split, root / "hist", ENTRY, None, None, root / "h003", tmp_path / "r", context_override=ctx_for())


def test_validation_refused_even_when_frozen_without_dev_pass(mini, tmp_path):
    root, p = mini
    with pytest.raises(HygieneError):
        rh.run_split(p, "validation", root / "hist", dict(ENTRY, status="FROZEN"), None, None, root / "h003",
                     tmp_path / "r", context_override=ctx_for())


def test_cli_refuses_validation_known_forward_and_protected_outputs(tmp_path):
    for s in ("validation", "known_diagnostic", "forward"):
        assert rh.main(["--split", s, "--output-dir", str(tmp_path / "o")]) == 2
    assert rh.main(["--split", "development", "--output-dir", str(Path("data") / "research_v1" / "h003_development")]) == 2
    assert not (tmp_path / "o").exists()


def test_registry_points_to_frozen_spec():
    e = rh.registry_entry()
    assert e["spec_frozen"]["frozen_spec_commit"] == h4.FROZEN_SPEC_COMMIT


def _manifest_env(tmp_path, readiness="PASS"):
    spy_dir = tmp_path / "spy"
    df = spy_1min(SPY_DAYS)
    out = df.reset_index()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["symbol"] = "SPY"
    (spy_dir / "1Min").mkdir(parents=True)
    out.to_csv(spy_dir / "1Min" / "SPY.csv", index=False)
    from src.historical_audit import sha256_file
    man = {"h004_data_readiness": readiness, "frozen_spec_commit": h4.FROZEN_SPEC_COMMIT,
           "source": {"feed": "iex", "adjustment": "raw"}, "raw_file": {"sha256": sha256_file(spy_dir / "1Min" / "SPY.csv")},
           "required_sessions": {"required_session_dates": [d for d in SPY_DAYS if d >= SPLIT["start"]],
                                 "required_session_dates_sha256": "x"},
           "diagnostics_only": {"spy_slope_windows_spanning_missing_bucket": 0}}
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(man), encoding="utf-8")
    return spy_dir, mp


def test_load_spy_context_verifies_manifest_checksum_and_range(tmp_path):
    spy_dir, mp = _manifest_env(tmp_path)
    stock = {"AAA": pd.DataFrame(index=pd.DatetimeIndex([et("2024-03-08 15:55").tz_convert("UTC")]))}
    info = rh.load_spy_context(spy_dir, mp, SPLIT, stock)
    assert info["support_bars"] == 200 and info["manifest"]["h004_data_readiness"] == "PASS"
    (spy_dir / "1Min" / "SPY.csv").write_bytes((spy_dir / "1Min" / "SPY.csv").read_bytes() + b"\n")
    with pytest.raises(rh.ContextError, match="checksum"):
        rh.load_spy_context(spy_dir, mp, SPLIT, stock)
    with pytest.raises(rh.ContextError):
        rh.load_spy_context(spy_dir, mp, {"name": "validation", "start": "2026-01-02", "end": "2026-05-29"}, stock)


def test_load_spy_context_requires_readiness_pass(tmp_path):
    spy_dir, mp = _manifest_env(tmp_path, readiness="HARD_FAIL")
    with pytest.raises(rh.ContextError):
        rh.load_spy_context(spy_dir, mp, SPLIT, {})


def test_h003_is_unchanged():
    from src import strategy_v2_h003 as h3
    assert h3.FROZEN_SPEC_COMMIT == "6eb6078719d12e50542d7284d6e96f598c115f71"
    assert isinstance(h4.RegimeGatedH004(ctx_for()["context"])._h003, ConsolidationBreakoutH003)


def test_exit_rates_use_engine_exit_reason_codes():
    tr = [{"exit_reason": "giveback_close"}, {"exit_reason": "giveback_close"}, {"exit_reason": "stop_hit"},
          {"exit_reason": "take_profit_hit"}]
    r = rh.exit_rates(tr)
    assert r["giveback_close"]["count"] == 2 and r["stop_hit"]["count"] == 1 and r["take_profit_hit"]["count"] == 1
    assert sum(v["count"] for v in r.values()) == len(tr)
