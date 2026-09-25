"""
STRATEGY_V2_HYPOTHESIS_003 (consolidation breakout). Pruebas del plan §16 del spec congelado
(commit 6eb6078): datos/soporte, cada regla A–F con bordes exactos, ventana de 6 velas existente
de la misma sesión, primera señal (vela de 10:00 decidida a las 10:05), consumo por emisión y re-arm
(T−6 > S), reset de sesión, sin señal de salida, fill en la apertura siguiente, sin look-ahead,
equivalencia con una referencia secuencial, determinismo, diagnósticos de ventana no contigua,
higiene y motor/H001/MA intactos. Sin red.
"""
import inspect
import json
import math

import numpy as np
import pandas as pd
import pytest

from src import backtest_engine, broker_alpaca
from src import strategy_v2_h003 as h3
from src.backtest_engine import BacktestConfig, BacktestEngine, production_args
from src.research_protocol import load_protocol, validate_protocol
from src.risk_manager_avanzado import RiskManager
from src.strategy_v2_h001 import ENGINE_LOOKBACK, HygieneError, resample_rth_5min

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


# ================================================================ reglas con valores a mano
def _base():
    """Sesión de 12 velas (s0=0); BUY válida en j=8: ventana 2..7 con rango 1.0 y ATR 1.0."""
    n = 12
    o = np.full(n, 100.5)
    h = np.full(n, 101.0)
    l = np.full(n, 100.0)
    c = np.full(n, 100.5)
    e50 = np.array([99.0 + 0.01 * i for i in range(n)])
    atr = np.ones(n)
    o[8], h[8], l[8], c[8] = 100.5, 101.6, 100.4, 101.5          # ubicación de cierre 0.9167
    return dict(o=o, h=h, l=l, c=c, e50=e50, atr=atr)


def _cond(a, j=8, s0=0):
    return h3.bar_conditions(j, s0, a["o"], a["h"], a["l"], a["c"], a["e50"], a["atr"])


def test_base_case_all_conditions_true():
    assert all(_cond(_base()).values())


@pytest.mark.parametrize("mutate,key", [
    (lambda a: a["e50"].__setitem__(8, 101.5), "close_gt_ema50"),              # close == EMA50 -> falla
    (lambda a: a["e50"].__setitem__(8, a["e50"][5]), "ema50_slope_pos"),       # pendiente 0 -> falla
    (lambda a: a["atr"].__setitem__(7, 0.49), "consolidation_valid"),          # 1.0/0.49 > 2
    (lambda a: a["c"].__setitem__(8, 101.0), "breakout_close_gt_high"),        # igualdad -> falla
    (lambda a: a["o"].__setitem__(8, 101.5), "bullish_bar"),                   # close == open -> falla
    (lambda a: a["o"].__setitem__(8, 101.55), "bullish_bar"),                  # bajista
])
def test_each_rule_blocks(mutate, key):
    a = _base()
    mutate(a)
    assert _cond(a)[key] is False


def test_range_exactly_two_atr_passes_and_uses_atr_t_minus_1():
    a = _base()
    a["atr"][7] = 0.5                      # rango 1.0 / 0.5 == 2.0 exacto
    a["atr"][8] = 1e-9                     # ATR_T no importa
    assert all(_cond(a).values())
    a["atr"][7] = 0.1
    assert _cond(a)["consolidation_valid"] is False


@pytest.mark.parametrize("bad", [0.0, float("nan"), float("inf"), -1.0])
def test_invalid_atr_prev_gives_no_signal_and_no_fallback(bad):
    a = _base()
    a["atr"][7] = bad
    cond = _cond(a)
    assert cond["atr_prev_valid"] is False and cond["consolidation_valid"] is False


def test_close_location_exact_boundary_and_zero_range():
    a = _base()
    a["l"][8], a["h"][8], a["c"][8], a["o"][8] = 100.0, 102.0, 101.5, 100.5     # (1.5)/2 == 0.75 exacto
    assert all(_cond(a).values())
    a["c"][8] = 101.48                                                           # 0.74
    assert _cond(a)["close_location_ok"] is False
    b = _base()
    b["h"][8] = b["l"][8] = b["c"][8] = 101.5                                    # rango 0
    b["o"][8] = 101.0
    assert _cond(b)["close_location_ok"] is False


def test_window_is_exactly_t6_to_t1():
    a = _base()
    a["h"][1] = 200.0                      # T−7: fuera de la ventana
    a["l"][1] = 1.0
    assert all(_cond(a).values())
    b = _base()
    b["h"][2] = 101.5                      # T−6: dentro -> close == high -> sin ruptura
    assert _cond(b)["breakout_close_gt_high"] is False


def test_same_session_window_seventh_bar_rule():
    a = _base()
    assert _cond(a, j=8, s0=2)["same_session_window"] is True        # T es la 7ª vela
    assert _cond(a, j=8, s0=3) == {"same_session_window": False}     # T es la 6ª vela


# ================================================================ consumo / re-arm (portafolio-independiente)
def _replay_with(monkeypatch, true_at, n=30, s0=0, eligible=None):
    monkeypatch.setattr(h3, "bar_conditions", lambda j, *a, **k: {"x": j in true_at})
    z = np.zeros(n)
    el = np.ones(n, bool) if eligible is None else eligible
    return h3.replay_session(s0, n - 1, z, z, z, z, z, z, el)


def test_rearm_requires_fresh_six_bars_after_signal(monkeypatch):
    assert _replay_with(monkeypatch, {8, 9, 13, 14}) == [8]          # 14 = 8+6 -> todavía no
    assert _replay_with(monkeypatch, {8, 14, 15}) == [8, 15]         # 15 = 8+7 -> ventana 9..14 toda después de S
    assert _replay_with(monkeypatch, {8, 15, 21, 22}) == [8, 15, 22]


def test_setup_consumed_by_emission_even_if_not_executable(monkeypatch):
    # la replay no recibe estado de portafolio: una BUY rechazada/bloqueada consume igual
    el = np.ones(30, bool)
    el[8] = False                                                    # 8 no elegible (calentamiento): no consume
    assert _replay_with(monkeypatch, {8, 10}, eligible=el) == [10]
    assert _replay_with(monkeypatch, {8, 10}) == [8]


def test_session_reset(monkeypatch):
    assert _replay_with(monkeypatch, {8, 10}, s0=9) == [10]          # sesión arranca en 9: la BUY de 8 no existe


# ================================================================ datos sintéticos (5Min RTH)
def _synth(seed, sessions, start="2024-03-04", drift=0.05, vol=0.3):
    rng = np.random.default_rng(seed)
    rows, idx, px = [], [], 100.0
    for d in pd.bdate_range(start, periods=sessions):
        for k in range(78):
            o = px + rng.normal(0, 0.05)
            c = o + drift + rng.normal(0, vol)
            rows.append((o, max(o, c) + abs(rng.normal(0, .12)), min(o, c) - abs(rng.normal(0, .12)), c, 20_000.0))
            idx.append(pd.Timestamp(f"{d.date()} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k))
            px = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))


def reference_signals(df):
    """Implementación independiente, secuencial y con estado de §4–§7 sobre toda la historia."""
    o, hi, lo, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    e50 = np.array([pd.Series(c[max(0, i - 199): i + 1]).ewm(span=50, adjust=False).mean().iloc[-1] for i in range(n)])
    atr = np.array([np.nan if (v := RiskManager._atr(hi[:i + 1], lo[:i + 1], c[:i + 1], 14)) is None else v for i in range(n)])
    days = df.index.tz_convert(NY).date
    out, stats, last, cur, pos = [], {"rearm_blocked": 0}, None, None, 0
    for j in range(n):
        if days[j] != cur:
            cur, last, pos = days[j], None, 0
        pos += 1
        if j + 1 < 150 or pos < 7:
            continue
        w = range(j - 6, j)
        a = atr[j - 1]
        if not (math.isfinite(a) and a > 0):
            continue
        hh, ll = max(hi[i] for i in w), min(lo[i] for i in w)
        rng = hi[j] - lo[j]
        ok = (c[j] > e50[j] and e50[j] - e50[j - 3] > 0 and (hh - ll) / a <= 2.0 and c[j] > hh and c[j] > o[j]
              and rng > 0 and (c[j] - lo[j]) / rng >= 0.75)
        if not ok:
            continue
        if last is not None and not (j - 6 > last):
            stats["rearm_blocked"] += 1
            continue
        out.append(j)
        last = j
    return out, stats


def strategy_signals(df):
    s = h3.ConsolidationBreakoutH003()
    return [j for j in range(len(df)) if s.evaluate(df.iloc[max(0, j + 1 - ENGINE_LOOKBACK): j + 1]).signal == "BUY"]


@pytest.fixture(scope="module")
def synth():
    df = _synth(11, 8)
    return df, reference_signals(df)


def test_strategy_matches_independent_reference(synth):
    df, (ref, stats) = synth
    assert strategy_signals(df) == ref
    assert len(ref) >= 5 and stats["rearm_blocked"] >= 1


def test_no_look_ahead_and_window_start_independence(synth):
    df, (ref, _) = synth
    cut = ref[len(ref) // 2]
    fut = df.copy()
    fut.iloc[cut + 1:, :4] = fut.iloc[cut + 1:, :4] * 0.6 + 9.0
    assert [j for j in strategy_signals(fut) if j <= cut] == [j for j in ref if j <= cut]
    s = h3.ConsolidationBreakoutH003()
    assert all(s.evaluate(df.iloc[max(0, j - 400): j + 1]).signal == "BUY" for j in ref)


def test_never_emits_sell_and_is_deterministic(synth):
    df, (ref, _) = synth
    s = h3.ConsolidationBreakoutH003()
    sigs = {s.evaluate(df.iloc[max(0, j + 1 - ENGINE_LOOKBACK): j + 1]).signal for j in range(len(df))}
    assert sigs <= {None, "BUY"}
    assert strategy_signals(df) == strategy_signals(df)


def test_min_history_guard():
    df = _synth(3, 3)
    s = h3.ConsolidationBreakoutH003()
    assert all(s.evaluate(df.iloc[: j + 1]).signal is None for j in range(149))
    assert s.evaluate(df.iloc[:100]).warmup_ok is False


# ================================================================ motor: temporización, fill, portafolio
def _timing_frame(breakout_at=6, day="2024-03-06"):
    """2 sesiones subiendo (historia) + hoy: 6 velas planas y ruptura en la vela `breakout_at` (6 = 10:00)."""
    rows, idx = [], []
    px = 100.0
    for d in ("2024-03-04", "2024-03-05"):
        for k in range(78):
            o, c = px, px + 0.05
            rows.append((o, c + 0.4, o - 0.4, c))                   # ATR% ~0.8%: por encima del piso implícito de RR
            idx.append(pd.Timestamp(f"{d} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k))
            px = c
    base = px
    for k in range(20):
        ts = pd.Timestamp(f"{day} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k)
        if k == breakout_at:
            rows.append((base, base + 1.05, base - 0.05, base + 1.0))      # cierre > máximo de la ventana, ubicación 0.95
        elif k > breakout_at:
            rows.append((base + 1.0, base + 1.2, base + 0.8, base + 1.0))
        else:
            rows.append((base, base + 0.4, base - 0.4, base + 0.02))      # consolidación: rango ~0.8 <= 2·ATR
        idx.append(ts)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx))
    df["volume"] = 50_000.0
    return df


def _engine(df, vol=None, **cfg):
    d = df.copy()
    if vol is not None:
        d["volume"] = vol
    c = BacktestConfig(symbols=["AAA"], timeframe="5Min", window_hours_limit=False, record_evaluations=True, **cfg)
    return BacktestEngine(c, {"AAA": d}, production_args({"lookback": ENGINE_LOOKBACK}), h3.ConsolidationBreakoutH003()).run()


def test_earliest_signal_is_the_1000_bar_decided_at_1005_and_fills_at_1005_open():
    df = _timing_frame(6)
    r = _engine(df)
    buys = [e for e in r.evaluations if e["signal"] == "BUY"]
    t1000 = pd.Timestamp("2024-03-06 10:00", tz=NY).tz_convert("UTC")
    assert [e["bar_timestamp"] for e in buys] == [t1000.isoformat()]
    [t] = r.trades or [r.open_positions[0]["trade"]]
    assert t["entry_decision_timestamp"] == (t1000 + pd.Timedelta(minutes=5)).isoformat()      # decisión 10:05
    assert t["entry_fill_timestamp"] == (t1000 + pd.Timedelta(minutes=5)).isoformat()          # apertura 10:05
    assert t["entry_fill_price"] == pytest.approx(df.loc[t1000 + pd.Timedelta(minutes=5), "open"] * 1.0005)


def test_sixth_bar_breakout_cannot_signal():
    r = _engine(_timing_frame(5))                                   # ruptura en la vela 09:55
    assert not any(e["signal"] == "BUY" and pd.Timestamp(e["bar_timestamp"]).tz_convert(NY).strftime("%H:%M") < "10:00"
                   for e in r.evaluations)


def test_signals_independent_of_portfolio_state_rejected_buy_still_consumes(synth):
    df, _ = synth
    normal = _engine(df.assign(volume=20_000.0))
    starved = _engine(df.assign(volume=1.0))                         # todo rechazado por liquidez
    sig = lambda r: [(e["bar_timestamp"], e["signal"]) for e in r.evaluations]  # noqa: E731
    assert sig(normal) == sig(starved) and starved.counters["risk"]["ACCEPT"] == 0
    assert starved.counters["rejects_by_reason"].get("LIQUIDITY_BELOW_MINIMUM", 0) > 0
    assert all(t["exit_reason"] != "signal_exit" for t in normal.trades)


def test_no_same_setup_reentry_after_quick_close(synth):
    df, (ref, _) = synth
    r = _engine(df.assign(volume=20_000.0))
    buys = [e["bar_timestamp"] for e in r.evaluations if e["signal"] == "BUY"]
    pos = {ts.isoformat(): k for k, ts in enumerate(df.index)}
    days = df.index.tz_convert(NY).date
    for a, b in zip(buys, buys[1:]):
        ia, ib = pos[a], pos[b]
        if days[ia] == days[ib]:
            assert ib - 6 > ia                                        # re-arm siempre con ventana fresca


def test_missing_bar_uses_six_existing_bars_and_is_flagged_noncontiguous():
    df = _timing_frame(7)
    gap = pd.Timestamp("2024-03-06 09:40", tz=NY).tz_convert("UTC")
    df = df.drop(gap)                                                 # falta un cubo IEX dentro de la ventana
    r = _engine(df)
    buys = [e["bar_timestamp"] for e in r.evaluations if e["signal"] == "BUY"]
    t = pd.Timestamp("2024-03-06 10:05", tz=NY).tz_convert("UTC")
    assert buys == [t.isoformat()]                                    # 7ª vela EXISTENTE de la sesión
    k = list(df.index).index(t)
    st = h3.signal_structure(df["open"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float),
                             df["close"].to_numpy(float), df.index.as_unit("ns").asi8, k)
    assert st["noncontiguous_window"] is True and st["window_span_minutes"] == 30.0
    k2 = list(_timing_frame(6).index).index(pd.Timestamp("2024-03-06 10:00", tz=NY).tz_convert("UTC"))
    f6 = _timing_frame(6)
    st2 = h3.signal_structure(f6["open"].to_numpy(float), f6["high"].to_numpy(float), f6["low"].to_numpy(float),
                              f6["close"].to_numpy(float), f6.index.as_unit("ns").asi8, k2)
    assert st2["noncontiguous_window"] is False and st2["window_span_minutes"] == 25.0


def test_signal_structure_values_normalized_by_atr_prev():
    df = _timing_frame(6)
    k = list(df.index).index(pd.Timestamp("2024-03-06 10:00", tz=NY).tz_convert("UTC"))
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    st = h3.signal_structure(o, h, l, c, df.index.as_unit("ns").asi8, k)
    a = RiskManager._atr(h[:k], l[:k], c[:k], 14)
    hi, lo = h[k - 6:k].max(), l[k - 6:k].min()
    assert st["atr_prev"] == a and st["consolidation_range_atr"] == pytest.approx((hi - lo) / a)
    assert st["breakout_range_atr"] == pytest.approx((h[k] - l[k]) / a)
    assert st["breakout_distance_atr"] == pytest.approx((c[k] - hi) / a)


# ================================================================ runner / soporte / higiene
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


def _mini_protocol(start="2024-03-11", end="2024-03-13"):
    p = load_protocol()
    p["universe"]["symbols"] = ["AAA", "BBB"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": start, "end": end},
                   {"name": "validation", "role": "validation", "start": "2024-03-14", "end": "2024-03-15"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    return validate_protocol(p)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    root = tmp_path_factory.mktemp("h003")
    for k, sym in enumerate(["AAA", "BBB"]):
        _write_1min(root, sym, _synth(80 + k, 10), 90 + k)
    return root


ENTRY = {"id": h3.HYPOTHESIS_ID, "status": "IMPLEMENTED", "validation_viewed_at": None}


def test_runner_uses_h001_resampler_exactly_200_support_bars_and_isolation(mini):
    from src import research_h003 as rh
    from src.historical_data import load_symbol_bars
    run = rh.run_split(_mini_protocol(), "development", mini, ENTRY)
    assert {s: v["support_bars"] for s, v in run["support"].items()} == {"AAA": 200, "BBB": 200}
    full5 = resample_rth_5min(load_symbol_bars(mini, "1Min", "AAA"))
    pd.testing.assert_frame_equal(run["bars"]["AAA"], full5.loc[run["bars"]["AAA"].index, ["open", "high", "low", "close", "volume"]])
    s_utc = pd.Timestamp("2024-03-11", tz=NY).tz_convert("UTC")
    r = run["result"]
    assert all(pd.Timestamp(e["bar_timestamp"]) >= s_utc for e in r.evaluations)
    assert all(pd.Timestamp(ts) >= s_utc for ts, _ in r.equity_curve)
    assert all(pd.Timestamp(f["fill_ts"]) >= s_utc for f in r.fills)


def test_insufficient_support_fails_loudly(mini):
    from src import research_h003 as rh
    with pytest.raises(HygieneError, match="soporte insuficiente"):
        rh.run_split(_mini_protocol("2024-03-06", "2024-03-08"), "development", mini, ENTRY)


def test_report_fields_gates_and_determinism(mini, tmp_path):
    from src import research_h003 as rh
    reps = [rh.build_report(rh.run_split(_mini_protocol(), "development", mini, ENTRY), _mini_protocol(), tmp_path / n,
                            compare=False) for n in ("a", "b")]
    assert reps[0] == reps[1]
    for f in ("trades.json", "h003_report.json", "h003_signals.csv"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    r = reps[0]
    ss = r["signal_structure"]
    for k in ("signals_with_noncontiguous_consolidation_window", "trades_with_noncontiguous_consolidation_window",
              "time_of_day_signal_counts", "nvda_split_week_signals", "same_session_rearm_signals"):
        assert k in ss
    f = r["funnel"]
    assert f["generated_buy_signals"] == ss["generated_signals"]
    assert f["risk_accepts"] + f["risk_rejects"] + sum(f["pre_risk_blocks"]["circuit_breaker"].values()) + \
        f["pre_risk_blocks"]["daily_profit_halt"] + f["pre_risk_blocks"]["symbol_already_open"] == f["generated_buy_signals"]
    assert set(r["gates"]["gates"]) == {"D1_expectancy_r", "D2_profit_factor", "D3_completed_trades",
                                        "D4_max_drawdown_pct", "D5_total_r", "D6_symbol_concentration"}
    assert "hypothesis-development evidence" in r["evidence"] and "signal_exit" not in r["exits"]["final_exit_reason"]


def test_hygiene_development_only(tmp_path):
    from src import research_h003 as rh
    from src.strategy_v2_h001 import check_split_allowed
    p = load_protocol()
    split = lambda n: next(s for s in p["splits"] if s["name"] == n)  # noqa: E731
    check_split_allowed(ENTRY, split("development"))
    with pytest.raises(HygieneError, match="FROZEN"):
        check_split_allowed(ENTRY, split("validation"), True)
    for n in ("known_diagnostic", "forward"):
        with pytest.raises(HygieneError):
            check_split_allowed(dict(ENTRY, status="FROZEN"), split(n), True)
    for n in ("validation", "known_diagnostic", "forward"):
        assert rh.main(["--split", n, "--output-dir", str(tmp_path / "o")]) == 2
    assert not (tmp_path / "o").exists()


def test_shared_engine_live_code_and_benchmarks_untouched():
    assert "ConsolidationBreakout" not in inspect.getsource(backtest_engine)
    from src import run_paper, strategy_v2_h001
    assert "ConsolidationBreakout" not in inspect.getsource(run_paper)
    assert strategy_v2_h001.EMA_FAST == 20 and strategy_v2_h001.EMA_SLOW == 50 and strategy_v2_h001.TOUCH_ATR_MULT == 0.5
    assert production_args().strategy == "ma" and production_args().fast == 3 and production_args().slow == 7
