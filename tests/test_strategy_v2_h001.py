"""
STRATEGY_V2_HYPOTHESIS_001: resampleo 5Min RTH, cada regla del §5 con valores
a mano (incluidas igualdades de borde), re-arm/reset de setup determinista,
equivalencia con una implementación de referencia secuencial, sin look-ahead,
aislamiento del soporte de calentamiento, D6, guardas de higiene y que el
motor/benchmark MA no cambien. Sin red.
"""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca
from src import strategy_v2_h001 as h
from src.backtest_engine import BacktestConfig, production_args, run_backtest
from src.research_h001 import SupportError, build_report, prepare_bars, run_split
from src.research_protocol import validate_protocol, load_protocol
from src.risk_manager_avanzado import RiskManager
from src.strategy import MACrossover

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


def _utc(day, hhmm):
    return pd.Timestamp(f"{day} {hhmm}", tz=NY).tz_convert("UTC")


# ================================================================ §3 resampleo
def _one_min(rows):
    idx = pd.DatetimeIndex([_utc(d, t) for d, t, *_ in rows], name="timestamp")
    return pd.DataFrame({"open": [r[2] for r in rows], "high": [r[3] for r in rows], "low": [r[4] for r in rows],
                         "close": [r[5] for r in rows], "volume": [r[6] for r in rows]}, index=idx)


def test_resample_aggregates_rth_buckets_aligned_to_0930():
    rows = [("2024-03-04", "09:29", 50, 99, 1, 60, 1000),                       # pre-market: fuera
            ("2024-03-04", "09:30", 10, 11, 9.5, 10.5, 100), ("2024-03-04", "09:31", 10.5, 12, 10, 11, 200),
            ("2024-03-04", "09:34", 11, 11.5, 8, 9, 300),
            ("2024-03-04", "09:35", 9, 9.2, 8.8, 9.1, 50),                      # siguiente cubeta
            ("2024-03-04", "15:59", 20, 21, 19, 20.5, 10), ("2024-03-04", "16:00", 30, 31, 29, 30, 10)]  # 16:00 fuera
    b = h.resample_rth_5min(_one_min(rows))
    assert list(b.index) == [_utc("2024-03-04", "09:30"), _utc("2024-03-04", "09:35"), _utc("2024-03-04", "15:55")]
    first = b.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume, first.n_minutes) == (10, 12, 8, 9, 600, 3)
    assert b.iloc[1].n_minutes == 1 and b.iloc[2].close == 20.5


def test_resample_empty_bucket_has_no_bar_and_early_close_is_cut():
    rows = [("2024-07-03", "09:30", 1, 1, 1, 1, 1), ("2024-07-03", "09:47", 2, 2, 2, 2, 1),   # 09:35-09:44 vacío
            ("2024-07-03", "12:59", 3, 3, 3, 3, 1), ("2024-07-03", "13:00", 4, 4, 4, 4, 1)]   # cierre anticipado
    b = h.resample_rth_5min(_one_min(rows))
    assert [t.tz_convert(NY).strftime("%H:%M") for t in b.index] == ["09:30", "09:45", "12:55"]


def test_premarket_and_afterhours_bars_have_no_effect():
    rth = [("2024-03-04", f"{9 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}", 10 + m, 11 + m, 9 + m, 10.5 + m, 100)
           for m in range(60)]
    ext = [("2024-03-04", "08:00", 500, 600, 400, 550, 9999), ("2024-03-04", "17:00", 1, 1, 1, 1, 1)]
    a = h.resample_rth_5min(_one_min(rth))
    b = h.resample_rth_5min(_one_min(sorted(rth + ext, key=lambda r: r[1])))
    pd.testing.assert_frame_equal(a, b)


# ================================================================ §5 reglas con valores a mano
def _base():
    """Una sesión de 10 velas (s0=0); BUY válida en j=7. EMA50 plana=100, EMA20 sube, ATR=1."""
    n = 10
    e20 = np.array([101 + 0.1 * i for i in range(n)])
    e50 = np.full(n, 100.0)
    atr = np.ones(n)
    o = np.full(n, 102.0)
    hi = np.full(n, 102.0)
    lo = e20 + 2.0                    # lejos de EMA20
    c = np.full(n, 101.5)
    lo[4] = e20[4]                    # toque dentro de P(7) = {2..6}
    o[7], c[7], hi[6] = 101.0, 103.0, 102.0
    return dict(o=o, h=hi, l=lo, c=c, e20=e20, e50=e50, atr=atr)


def _cond(a, j=7, s0=0):
    return h.conditions_at(j, s0, a["o"], a["h"], a["l"], a["c"], a["e20"], a["e50"], a["atr"])


def _replay(a, t=None, s0=0, eligible=None):
    n = len(a["c"])
    el = np.ones(n, bool) if eligible is None else eligible
    return h.replay_session(s0, n - 1 if t is None else t, a["o"], a["h"], a["l"], a["c"], a["e20"], a["e50"], a["atr"], el)


def test_base_case_emits_buy_and_all_conditions_true():
    a = _base()
    assert all(_cond(a).values()) and 7 in _replay(a, t=7)


@pytest.mark.parametrize("mutate,key", [
    (lambda a: a["e20"].__setitem__(7, 99.0), "ema20_gt_ema50"),
    (lambda a: a["e20"].__setitem__(7, 100.0), "ema20_gt_ema50"),                         # igualdad: no
    (lambda a: a["e20"].__setitem__(4, a["e20"][7]), "ema20_slope_pos"),                  # pendiente 0: no
    (lambda a: a["e50"].__setitem__(4, 100.01), "ema50_slope_nonneg"),
    (lambda a: a["c"].__setitem__(7, 100.0), "close_gt_ema50"),
    (lambda a: a["l"].__setitem__(4, a["e20"][4] + 0.5001), "pullback_touch"),
    (lambda a: a["c"].__setitem__(3, 99.99), "pullback_hold"),
    (lambda a: a["h"].__setitem__(6, 103.0), "trigger_close_gt_prev_high"),               # igualdad: no
    (lambda a: a["e20"].__setitem__(7, 103.0), "trigger_close_gt_ema20"),
    (lambda a: a["o"].__setitem__(7, 103.0), "trigger_close_gt_open"),                    # igualdad: no
])
def test_each_rule_blocks_the_signal(mutate, key):
    a = _base()
    mutate(a)
    cond = _cond(a)
    assert cond[key] is False or cond[key] == False  # noqa: E712
    assert 7 not in _replay(a, t=7)


def test_boundary_equalities_that_are_allowed():
    a = _base()
    a["l"][4] = a["e20"][4] + 0.5 * a["atr"][4]          # toque exactamente a 0.50 ATR
    a["c"][3] = a["e50"][3]                              # cierre == EMA50 en el pullback (>=)
    a["e50"][4] = a["e50"][7]                            # pendiente EMA50 == 0 (>=)
    assert all(_cond(a).values()) and 7 in _replay(a, t=7)


def test_touch_only_counts_inside_the_five_bar_window_and_uses_own_bar_values():
    a = _base()
    a["l"][4] = a["e20"][4] + 2.0
    a["l"][1] = a["e20"][1]                              # fuera de P(7) = {2..6}
    assert not _cond(a)["pullback_touch"]
    b = _base()
    b["l"][4] = b["e20"][4] + 0.9
    b["atr"][4] = 2.0                                    # su propio ATR: 0.9 <= 1.0
    b["atr"][7] = 0.1                                    # el ATR de T no importa
    assert _cond(b)["pullback_touch"]


def test_sixth_session_bar_rule():
    a = _base()
    assert _cond(a, j=4) == {"session_position": False}
    assert _cond(a, j=7, s0=3) == {"session_position": False}   # T es la 5a vela de la sesión
    assert _cond(a, j=7, s0=2)["session_position"] is True       # 6a vela


def _two_triggers():
    """Sesión de 12 velas: BUY en 7; la vela 8 repite el gatillo con el mismo pullback."""
    a = _base()
    n = 12
    for k in a:
        a[k] = np.concatenate([a[k], np.repeat(a[k][-1:], n - len(a[k]))])
    a["e20"] = np.array([101 + 0.1 * i for i in range(n)])
    a["l"] = a["e20"] + 2.0
    a["l"][4] = a["e20"][4]
    a["h"][7], a["o"][8], a["c"][8] = 103.0, 103.1, 104.0          # 8: close > high[7], > open
    a["h"][8], a["o"][9], a["c"][9] = 104.0, 104.1, 105.0          # 9: igual
    return a


def test_setup_is_consumed_and_rearms_only_after_a_new_touch():
    a = _two_triggers()
    assert _replay(a) == [7]                               # 8 y 9 reutilizarían el pullback de la vela 4
    b = _two_triggers()
    b["l"][8] = b["e20"][8]                                # nuevo toque DESPUÉS de la señal en 7
    assert _replay(b) == [7, 9]                            # 9 re-arma (toque en 8 ∈ P(9), 8 > 7)
    c = _two_triggers()
    c["l"][7] = c["e20"][7]                                # toque en la misma vela de la señal: índice == S, no > S
    assert _replay(c) == [7]


def test_consumption_is_by_emission_independent_of_execution():
    # la replay no recibe estado de portafolio: dos corridas idénticas emiten lo mismo
    a = _two_triggers()
    assert _replay(a) == _replay(_two_triggers()) == [7]


def test_session_reset_state_does_not_carry():
    a = _two_triggers()
    # s0=2: la vela 7 es la 6a de la sesión -> BUY en 7; 8 y 9 reutilizan el mismo pullback -> suprimidas
    assert _replay(a, s0=2) == [7]
    # s0=3: 7 es solo la 5a vela (no puede señalar) -> la primera señal de ESA sesión es 8; 9 queda consumida
    assert _replay(a, s0=3) == [8]


def test_ineligible_bars_never_signal_nor_consume():
    a = _two_triggers()
    el = np.ones(12, bool)
    el[7] = False                                         # 7 aún en calentamiento
    assert _replay(a, eligible=el) == [8]                 # 8 usa el pullback que 7 no consumió


# ================================================================ referencia secuencial + sin look-ahead
def synthetic_5min(seed, sessions, start="2024-03-04", drift=0.03, vol=0.12):
    rng = np.random.default_rng(seed)
    days = [d for d in pd.bdate_range(start, periods=sessions)]
    idx, px, rows = [], 100.0, []
    for d in days:
        for k in range(78):
            ts = _utc(d.date(), "09:30") + pd.Timedelta(minutes=5 * k)
            o = px + rng.normal(0, vol / 3)
            c = o + drift + rng.normal(0, vol)
            rows.append((o, max(o, c) + abs(rng.normal(0, vol / 2)), min(o, c) - abs(rng.normal(0, vol / 2)), c,
                         1000.0))
            idx.append(ts)
            px = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex(idx, name="timestamp"))
    return df


def reference_signals(df):
    """Implementación independiente y secuencial (con estado) de §4-§5, sobre la historia completa."""
    o, hi, lo, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    e20, e50, atr = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for i in range(n):
        w = pd.Series(c[max(0, i - 199): i + 1])
        e20[i] = w.ewm(span=20, adjust=False).mean().iloc[-1]
        e50[i] = w.ewm(span=50, adjust=False).mean().iloc[-1]
        v = RiskManager._atr(hi[: i + 1], lo[: i + 1], c[: i + 1], 14)
        atr[i] = np.nan if v is None else v
    days = df.index.tz_convert(NY).date
    out, last, cur_day, pos = [], None, None, 0
    stats = {"rearm_suppressed": 0}
    for j in range(n):
        if days[j] != cur_day:
            cur_day, last, pos = days[j], None, 0
        pos += 1
        if j + 1 < 150 or pos < 6:
            continue
        P = range(j - 5, j)
        ok = (e20[j] > e50[j] and e20[j] - e20[j - 3] > 0 and e50[j] - e50[j - 3] >= 0 and c[j] > e50[j]
              and any(lo[i] <= e20[i] + 0.5 * atr[i] for i in P) and all(c[i] >= e50[i] for i in P)
              and c[j] > hi[j - 1] and c[j] > e20[j] and c[j] > o[j])
        if not ok:
            continue
        if last is not None and not any(i > last and lo[i] <= e20[i] + 0.5 * atr[i] for i in P):
            stats["rearm_suppressed"] += 1
            continue
        out.append(j)
        last = j
    return out, stats


def strategy_signals(df, strat=None):
    strat = strat or h.TrendPullbackH001()
    return [j for j in range(len(df))
            if strat.evaluate(df.iloc[max(0, j + 1 - h.ENGINE_LOOKBACK): j + 1]).signal == "BUY"]


@pytest.fixture(scope="module")
def synth():
    df = synthetic_5min(7, 8)
    return df, reference_signals(df)


def test_strategy_matches_independent_sequential_reference(synth):
    df, (ref, stats) = synth
    got = strategy_signals(df)
    assert got == ref
    assert len(ref) >= 5 and stats["rearm_suppressed"] >= 1   # el caso de re-arm está ejercitado


def test_signals_are_invariant_to_future_bars(synth):
    df, (ref, _) = synth
    cut = ref[len(ref) // 2]
    fut = df.copy()
    fut.iloc[cut + 1:, :4] = fut.iloc[cut + 1:, :4] * 0.5 + 7.0
    assert [j for j in strategy_signals(fut) if j <= cut] == [j for j in ref if j <= cut]


def test_signals_do_not_depend_on_window_start(synth):
    df, (ref, _) = synth
    strat = h.TrendPullbackH001()
    for j in ref:
        longer = strat.evaluate(df.iloc[max(0, j - 400): j + 1]).signal
        assert longer == "BUY"


def test_evaluation_is_deterministic(synth):
    df, (ref, _) = synth
    assert strategy_signals(df) == strategy_signals(df)


def test_indicator_helpers_match_pandas_and_risk_manager(synth):
    df, _ = synth
    c = df["close"].to_numpy(float)
    idx = [150, 199, 300, 450]
    got = h._own_window_ema(c, idx, 50)
    for k, i in enumerate(idx):
        exp = pd.Series(c[max(0, i - 199): i + 1]).ewm(span=50, adjust=False).mean().iloc[-1]
        assert got[k] == exp
    hi, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    assert h._atr_at(hi, lo, c, 300) == RiskManager._atr(hi[:301], lo[:301], c[:301], 14)


def test_warmup_below_150_bars_never_signals(synth):
    df, _ = synth
    strat = h.TrendPullbackH001()
    assert all(strat.evaluate(df.iloc[: j + 1]).signal is None for j in range(149))
    assert strat.evaluate(df.iloc[:100]).warmup_ok is False


# ================================================================ motor / soporte / higiene
def _mini_protocol(start, end):
    p = load_protocol()
    p["universe"]["symbols"] = ["AAA", "BBB"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": start, "end": end},
                   {"name": "validation", "role": "validation", "start": "2024-04-01", "end": "2024-04-30"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    return validate_protocol(p)


def _write_1min(root, sym, df5, seed):
    """1Min sintéticas coherentes con cada vela 5Min (5 minutos, OHLC contenidos)."""
    rng = np.random.default_rng(seed)
    rows = []
    for ts, r in df5.iterrows():
        path = np.linspace(r.open, r.close, 5) + rng.normal(0, 0.01, 5)
        path[0], path[-1] = r.open, r.close
        for k in range(5):
            o = path[k - 1] if k else r.open
            cc = path[k]
            rows.append({"timestamp": (ts + pd.Timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": o,
                         "high": max(o, cc) + 0.01, "low": min(o, cc) - 0.01, "close": cc, "volume": 5000.0,
                         "symbol": sym})
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(d / f"{sym}.csv", index=False)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    root = tmp_path_factory.mktemp("h001")
    for k, sym in enumerate(["AAA", "BBB"]):
        _write_1min(root, sym, synthetic_5min(20 + k, 8), 30 + k)   # 2024-03-04 .. 2024-03-13
    return root


ENTRY = {"id": h.HYPOTHESIS_ID, "status": "IMPLEMENTED", "validation_viewed_at": None}


def test_support_bars_are_exactly_200_and_nothing_happens_before_start(mini):
    proto = _mini_protocol("2024-03-11", "2024-03-13")
    run = run_split(proto, "development", mini, ENTRY)
    s_utc = _utc("2024-03-11", "00:00")
    assert {s: v["support_bars"] for s, v in run["support"].items()} == {"AAA": 200, "BBB": 200}
    r = run["result"]
    assert min(pd.Timestamp(e["bar_timestamp"]) for e in r.evaluations) >= s_utc
    assert all(pd.Timestamp(ts) >= s_utc for ts, _ in r.equity_curve)
    assert all(pd.Timestamp(f["fill_ts"]) >= s_utc for f in r.fills)
    assert all(pd.Timestamp(t["entry_signal_timestamp"]) >= s_utc for t in r.trades)
    for e in r.evaluations:
        if e["signal"] == "BUY":  # 6a vela de la sesión como mínimo: vela de señal >= 09:55
            assert pd.Timestamp(e["bar_timestamp"]).tz_convert(NY).strftime("%H:%M") >= "09:55"


def test_support_bars_affect_indicators_only(mini, tmp_path):
    proto = _mini_protocol("2024-03-11", "2024-03-13")
    base = run_split(proto, "development", mini, ENTRY)
    # perturbar SOLO el soporte (antes del inicio) de AAA
    import shutil
    shutil.copytree(mini, tmp_path / "d")
    path = tmp_path / "d" / "1Min" / "AAA.csv"
    raw = pd.read_csv(path)
    early = pd.to_datetime(raw["timestamp"], utc=True) < _utc("2024-03-11", "00:00")
    raw.loc[early, ["open", "high", "low", "close"]] *= 1.3
    raw.to_csv(path, index=False)
    alt = run_split(proto, "development", tmp_path / "d", ENTRY)
    s_utc = _utc("2024-03-11", "00:00")
    for r in (base["result"], alt["result"]):
        assert all(pd.Timestamp(e["bar_timestamp"]) >= s_utc for e in r.evaluations)
    # mismo número de decisiones, pero los indicadores (y por lo tanto posiblemente señales) pueden diferir
    assert len(base["result"].evaluations) == len(alt["result"].evaluations)
    # el soporte sí entra en los indicadores de las primeras velas del split (EMA50 de la vela 205)
    alt5 = prepare_bars(tmp_path / "d", ["AAA"], "2024-03-11", "2024-03-13")["bars"]["AAA"]
    base5 = prepare_bars(mini, ["AAA"], "2024-03-11", "2024-03-13")["bars"]["AAA"]
    assert (alt5.iloc[200:] == base5.iloc[200:]).all().all()          # velas del split idénticas
    e_alt = h._own_window_ema(alt5["close"].to_numpy(float), [205], 50)[0]
    e_base = h._own_window_ema(base5["close"].to_numpy(float), [205], 50)[0]
    assert e_alt != e_base


def test_insufficient_support_fails_loudly(mini):
    proto = _mini_protocol("2024-03-06", "2024-03-08")   # solo 2 sesiones (156 velas) antes
    with pytest.raises(SupportError, match="se requieren 200"):
        run_split(proto, "development", mini, ENTRY)


def test_engine_look_ahead_future_bars_do_not_change_earlier_decisions(mini, tmp_path):
    proto = _mini_protocol("2024-03-11", "2024-03-13")
    base = run_split(proto, "development", mini, ENTRY)["result"]
    import shutil
    shutil.copytree(mini, tmp_path / "d")
    cut = _utc("2024-03-12", "12:00")
    for sym in ("AAA", "BBB"):
        path = tmp_path / "d" / "1Min" / f"{sym}.csv"
        raw = pd.read_csv(path)
        late = pd.to_datetime(raw["timestamp"], utc=True) >= cut
        raw.loc[late, ["open", "high", "low", "close"]] = raw.loc[late, ["open", "high", "low", "close"]] * 0.7 + 3
        raw.to_csv(path, index=False)
    alt = run_split(proto, "development", tmp_path / "d", ENTRY)["result"]
    pre = lambda r: [e for e in r.evaluations if pd.Timestamp(e["bar_timestamp"]) + pd.Timedelta(minutes=5) <= cut]  # noqa: E731
    assert pre(base) == pre(alt) and len(pre(base)) > 0


def test_report_gates_and_outputs_are_deterministic(mini, tmp_path):
    proto = _mini_protocol("2024-03-11", "2024-03-13")
    reps = []
    for name in ("a", "b"):
        run = run_split(proto, "development", mini, ENTRY)
        reps.append(build_report(run, proto, tmp_path / name))
    assert reps[0] == reps[1]
    for f in ("trades.csv", "equity_curve.csv", "daily_results.csv", "h001_report.json"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    g = reps[0]["gates"]
    assert set(g["gates"]) == {"D1_expectancy_r", "D2_profit_factor", "D3_completed_trades", "D4_max_drawdown_pct",
                               "D5_total_r", "D6_symbol_concentration"}
    assert g["progression_to_validation"] in ("PASS", "FAIL")
    assert reps[0]["exit_reasons"].get("signal_exit", 0) == 0     # sin señal de salida (Q1)


# ================================================================ D6
def _tr(sym, pnl):
    return {"symbol": sym, "realized_pnl": pnl}


def test_d6_normal_case():
    d = h.d6_concentration([_tr("A", 40), _tr("B", 60), _tr("C", -500), _tr("A", 20)], ["A", "B", "C", "D"])
    assert d["per_symbol_pnl"] == {"A": 60.0, "B": 60.0, "C": -500.0, "D": 0.0}
    assert d["positive_pool"] == 120.0 and d["max_share"] == 0.5 and d["passed"] is True


def test_d6_exactly_half_passes_and_above_fails():
    assert h.d6_concentration([_tr("A", 50), _tr("B", 50)], ["A", "B"])["passed"] is True
    d = h.d6_concentration([_tr("A", 50.0001), _tr("B", 50)], ["A", "B"])
    assert d["passed"] is False and d["max_share_symbol"] == "A"


def test_d6_single_positive_symbol_fails():
    d = h.d6_concentration([_tr("A", 10), _tr("B", -100)], ["A", "B"])
    assert d["max_share"] == 1.0 and d["passed"] is False


def test_d6_empty_pool_is_undefined_and_fails():
    for trades in ([], [_tr("A", -1), _tr("B", 0.0)]):
        d = h.d6_concentration(trades, ["A", "B"])
        assert d["passed"] is False and d["shares"] is None and d["note"] == "undefined: empty positive pool"


def test_development_gates_individual_rules():
    summ = {"trades": {"expectancy_r": 0.05, "profit_factor": 1.10, "trades": 150, "total_r": 7.5},
            "portfolio": {"max_drawdown_pct": -25.0}}
    g = h.development_gates(summ, [_tr("A", 10), _tr("B", 10)], ["A", "B"])
    assert g["progression_to_validation"] == "PASS" and all(v["passed"] for v in g["gates"].values())
    for key, patch in (("D1_expectancy_r", {"expectancy_r": 0.0}), ("D2_profit_factor", {"profit_factor": 1.0999}),
                       ("D3_completed_trades", {"trades": 149}), ("D5_total_r", {"total_r": 0.0})):
        s2 = {"trades": dict(summ["trades"], **patch), "portfolio": summ["portfolio"]}
        g2 = h.development_gates(s2, [_tr("A", 10), _tr("B", 10)], ["A", "B"])
        assert g2["gates"][key]["passed"] is False and g2["progression_to_validation"] == "FAIL"
    s3 = {"trades": summ["trades"], "portfolio": {"max_drawdown_pct": -25.01}}
    assert h.development_gates(s3, [_tr("A", 10), _tr("B", 10)], ["A", "B"])["gates"]["D4_max_drawdown_pct"]["passed"] is False
    assert h.development_gates(dict(summ, trades=dict(summ["trades"], profit_factor=None, expectancy_r=None, total_r=None)),
                               [], ["A"])["progression_to_validation"] == "FAIL"


# ================================================================ higiene
def test_hygiene_guard():
    p = load_protocol()
    split = lambda n: next(s for s in p["splits"] if s["name"] == n)  # noqa: E731
    h.check_split_allowed({"id": "X", "status": "IMPLEMENTED"}, split("development"))
    for st in ("REJECTED_AT_DEVELOPMENT", "VALIDATION_VIEWED"):
        with pytest.raises(h.HygieneError):
            h.check_split_allowed({"id": "X", "status": st}, split("development"))
    with pytest.raises(h.HygieneError, match="FROZEN"):
        h.check_split_allowed({"id": "X", "status": "IMPLEMENTED"}, split("validation"), True)
    with pytest.raises(h.HygieneError, match="compuerta"):
        h.check_split_allowed({"id": "X", "status": "FROZEN"}, split("validation"), False)
    with pytest.raises(h.HygieneError, match="una sola vez"):
        h.check_split_allowed({"id": "X", "status": "FROZEN", "validation_viewed_at": "t"}, split("validation"), True)
    h.check_split_allowed({"id": "X", "status": "FROZEN", "validation_viewed_at": None}, split("validation"), True)
    for n in ("known_diagnostic", "forward", "warmup"):
        with pytest.raises(h.HygieneError, match="no está permitido"):
            h.check_split_allowed({"id": "X", "status": "FROZEN"}, split(n), True)


def test_cli_refuses_validation_for_current_registry(capsys):
    from src.research_h001 import main
    assert main(["--split", "validation", "--output-dir", "unused_never_written"]) == 2
    assert "FROZEN" in capsys.readouterr().err
    assert main(["--split", "known_diagnostic", "--output-dir", "unused_never_written"]) == 2


# ================================================================ motor / benchmark intactos
def _ma_bars(seed):
    rng = np.random.default_rng(seed)
    t0 = _utc("2026-06-01", "09:30")
    out = {}
    for k in range(2):
        c = list(100 + np.cumsum(rng.normal(0, 0.25, 390)))
        o = [c[0]] + c[:-1]
        idx = pd.DatetimeIndex([t0 + pd.Timedelta(minutes=i) for i in range(390)])
        out[f"S{k}"] = pd.DataFrame({"open": o, "high": [max(a, b) + .3 for a, b in zip(o, c)],
                                     "low": [min(a, b) - .3 for a, b in zip(o, c)], "close": c,
                                     "volume": 10_000.0}, index=idx)
    return out


def test_engine_defaults_and_injected_ma_are_identical():
    bars = _ma_bars(4)
    cfg = BacktestConfig(symbols=["S0", "S1"])
    a = run_backtest(cfg, bars)
    ma = MACrossover(3, 7)
    ma.min_bars = 7          # una estrategia inyectada declara su warm-up (MA de producción: max(fast, slow))
    b = run_backtest(cfg, bars, production_args(), ma)
    assert a.trades and a.trades == b.trades and a.equity_curve == b.equity_curve and a.counters == b.counters
    assert BacktestConfig(symbols=["S0"]).window_hours_limit is True


def test_live_code_not_affected():
    # la estrategia H001 no está registrada en producción
    import inspect
    from src import run_paper
    assert "TrendPullbackH001" not in inspect.getsource(run_paper)
    assert production_args().strategy == "ma" and production_args().lookback == 120 and production_args().hours_back == 24
