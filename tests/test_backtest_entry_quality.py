"""
Diagnóstico de calidad de entrada: cada feature con valores a mano, sin
look-ahead (las velas futuras no cambian el contexto de entrada pero sí el
resultado), unión con MFE/MAE, buckets, crosstabs, correlaciones, salidas
deterministas e invariancia del backtest (con/sin diagnóstico). Sin red.
"""
import json

import numpy as np
import pandas as pd
import pytest

from src import backtest_engine, broker_alpaca
from src.backtest_engine import BacktestConfig, production_args, run_backtest
from src.backtest_entry_quality import (
    ROW_COLUMNS, bucket_tables, cles, correlations, crosstabs, entry_features, entry_quality_rows,
    entry_quality_summary, format_entry_quality, session_bucket,
)
from src.backtest_report import format_report, summarize, to_json, write_outputs
from src.strategy import MACrossover

NY = "America/New_York"
T0 = pd.Timestamp("2026-06-01 09:30", tz=NY).tz_convert("UTC")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


def make_bars(closes, start=T0, spread=0.3, vol=10_000):
    opens = [closes[0]] + list(closes[:-1])
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=i) for i in range(len(closes))])
    return pd.DataFrame({"open": opens, "high": [max(o, c) + spread for o, c in zip(opens, closes)],
                         "low": [min(o, c) - spread for o, c in zip(opens, closes)], "close": list(closes),
                         "volume": float(vol)}, index=idx)


def decline(p, n, step=0.02):
    return [round(p - step * (i + 1), 6) for i in range(n)]


def run(bars, symbols=None, overrides=None, **cfg):
    return run_backtest(BacktestConfig(symbols=symbols or list(bars), **cfg), bars, production_args(overrides))


BASE = decline(101.0, 25) + [101.5]  # vela 25 = cruce alcista (BUY); fill en la apertura de la 26


def feats(df, window_start=0, fast=3, slow=7):
    ts = df.index.as_unit("ns").asi8
    return entry_features(ts, df["open"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float),
                          df["close"].to_numpy(float), df["volume"].to_numpy(float),
                          strategy_window_start=window_start, fast=fast, slow=slow, atr_window=14, tf_seconds=60)


def jump_bars(n_flat=40, start=None, jump_close=13.0, vols=(100.0, 300.0)):
    """n_flat velas planas en 10 (rango 1) y la vela de señal 10 -> jump_close (alto +0.5, bajo 9.5)."""
    start = start if start is not None else pd.Timestamp("2026-06-01 10:00", tz=NY).tz_convert("UTC")
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=i) for i in range(n_flat + 1)])
    return pd.DataFrame({"open": [10.0] * (n_flat + 1), "high": [10.5] * n_flat + [jump_close + 0.5],
                         "low": [9.5] * (n_flat + 1), "close": [10.0] * n_flat + [jump_close],
                         "volume": [vols[0]] * n_flat + [vols[1]]}, index=idx)


ATR = (13 * 1.0 + 4.0) / 14  # 13 velas con TR 1 + la de señal con TR = 13.5 - 9.5


# ---------------------------------------------------------------- geometría de medias
def test_ma_spread_and_normalizations():
    f = feats(jump_bars())
    assert f["fast_ma"] == pytest.approx(11.0) and f["slow_ma"] == pytest.approx(73 / 7)
    assert f["ma_spread"] == pytest.approx(4 / 7)
    assert f["atr"] == pytest.approx(ATR)
    assert f["ma_spread_atr"] == pytest.approx((4 / 7) / ATR)
    assert f["ma_spread_pct"] == pytest.approx((4 / 7) / 13 * 100)


def test_ma_slopes_use_only_past_points_and_are_atr_normalized():
    f = feats(jump_bars())
    assert f["fast_ma_slope_1bar"] == pytest.approx(1.0)       # 11 - 10
    assert f["fast_ma_slope_3bar"] == pytest.approx(1 / 3)     # (11 - 10) / 3
    assert f["slow_ma_slope_1bar"] == pytest.approx(3 / 7)
    assert f["slow_ma_slope_3bar"] == pytest.approx(1 / 7)
    assert f["slow_ma_slope_3bar_atr"] == pytest.approx((1 / 7) / ATR)
    assert f["slow_ma_slope_direction"] == "rising" and f["fast_ma_slope_direction"] == "rising"


def test_ma_values_match_the_strategy_on_the_same_window():
    df = make_bars(BASE)
    ws = 5
    f = feats(df, window_start=ws)
    res = MACrossover(3, 7).evaluate(df.iloc[ws:])
    assert res.signal == "BUY"
    assert f["fast_ma"] == res.values["ma_fast"] and f["slow_ma"] == res.values["ma_slow"]
    assert f["ma_spread"] == res.values["now_diff"] and f["prev_ma_spread"] == res.values["prev_diff"]


# ---------------------------------------------------------------- cruce / expansión
def test_crossover_on_signal_bar_is_age_zero_and_not_previously_separated():
    f = feats(jump_bars())
    assert f["bars_since_bullish_crossover"] == 0 and f["crossover_age_bucket"] == "0"
    assert f["ma_separated_before_bar"] is False and f["prev_ma_spread"] == pytest.approx(0.0)
    assert f["spread_expanding"] is True and f["spread_change_1bar"] == pytest.approx(4 / 7)
    assert f["crossovers_last_20_bars"] == 1


def test_crossover_age_counts_completed_bars_and_contracting_spread():
    df = jump_bars()
    extra = pd.DataFrame({"open": [13.0, 16.0], "high": [16.5, 16.5], "low": [12.5, 8.5], "close": [16.0, 9.0],
                          "volume": [100.0, 100.0]},
                         index=[df.index[-1] + pd.Timedelta(minutes=m) for m in (1, 2)])
    f = feats(pd.concat([df, extra]))
    assert f["bars_since_bullish_crossover"] == 2 and f["crossover_age_bucket"] == "2"
    assert f["ma_separated_before_bar"] is True
    sp = lambda closes: np.mean(closes[-3:]) - np.mean(closes[-7:])  # noqa: E731
    c = [10.0] * 40 + [13.0, 16.0, 9.0]
    assert f["spread_change_1bar"] == pytest.approx(sp(c) - sp(c[:-1]))
    assert f["spread_expanding"] is False and sp(c) < sp(c[:-1])  # contrayéndose


def test_crossover_age_unknown_with_insufficient_history_and_prior_run():
    f = feats(make_bars([10.0] * 7 + [11.0, 12.0]))  # el tramo positivo arranca donde el spread aún es NaN
    assert f["crossover_age_bucket"] in ("0", "1", "unknown")
    short = feats(make_bars([10.0, 10.0, 11.0]))
    assert short["bars_since_bullish_crossover"] is None and short["crossover_age_bucket"] == "unknown"
    assert short["atr"] is None and short["ma_spread_atr"] is None
    base = feats(make_bars(BASE))  # 25 velas bajando: spread <= 0 desde que existe
    assert base["bars_since_bullish_crossover"] == 0 and base["prior_bearish_run_bars"] is None  # llega al NaN


def test_prior_bearish_run_length():
    closes = [10.0] * 10 + [11.0] * 5 + decline(11.0, 12, 0.1) + [12.5]
    f = feats(make_bars(closes))
    ma = pd.Series(closes)
    sp = (ma.rolling(3).mean() - ma.rolling(7).mean()).to_numpy()
    k = len(closes) - 1
    run_len, j = 0, k - 1
    while sp[j] <= 0:
        run_len += 1
        j -= 1
    assert f["bars_since_bullish_crossover"] == 0 and f["prior_bearish_run_bars"] == run_len


# ---------------------------------------------------------------- extensión / vela / momentum
def test_price_extension():
    f = feats(jump_bars())
    assert f["close_minus_fast_ma"] == pytest.approx(2.0)
    assert f["close_minus_fast_ma_atr"] == pytest.approx(2.0 / ATR)
    assert f["close_minus_fast_ma_pct"] == pytest.approx(2.0 / 11 * 100)
    assert f["close_minus_slow_ma"] == pytest.approx(13 - 73 / 7)
    assert f["close_minus_slow_ma_atr"] == pytest.approx((13 - 73 / 7) / ATR)


def test_signal_bar_anatomy():
    f = feats(jump_bars())
    assert (f["signal_open"], f["signal_high"], f["signal_low"], f["signal_close"]) == (10.0, 13.5, 9.5, 13.0)
    assert f["bar_range"] == pytest.approx(4.0) and f["body_size"] == pytest.approx(3.0)
    assert f["upper_wick"] == pytest.approx(0.5) and f["lower_wick"] == pytest.approx(0.5)
    assert f["body_to_range_ratio"] == pytest.approx(0.75)
    assert f["bar_range_atr"] == pytest.approx(4.0 / ATR) and f["body_atr"] == pytest.approx(3.0 / ATR)
    assert f["close_location_in_range"] == pytest.approx(0.875) and f["close_location_bucket"] == "high"
    assert f["bar_direction"] == "up"


def test_momentum_fields():
    f = feats(jump_bars())
    for nb in (1, 3, 5, 10):
        assert f[f"return_{nb}bar"] == pytest.approx(30.0)
        assert f[f"return_{nb}bar_atr"] == pytest.approx(3.0 / ATR)
    assert f["consecutive_up_bars"] == 1 and f["consecutive_down_bars"] == 0
    assert f["dist_from_5bar_high_atr"] == pytest.approx(-0.5 / ATR)
    g = feats(make_bars([10.0] * 30 + [9.9, 9.8, 9.7]))
    assert g["consecutive_down_bars"] == 3 and g["consecutive_up_bars"] == 0


def test_volume_ratios_use_prior_bars_only():
    f = feats(jump_bars(vols=(100.0, 300.0)))
    assert f["signal_bar_volume"] == 300.0
    assert f["avg_volume_5"] == 100.0 and f["avg_volume_20"] == 100.0
    assert f["volume_ratio_5"] == pytest.approx(3.0) and f["volume_ratio_20"] == pytest.approx(3.0)


def test_atr_context():
    f = feats(jump_bars())
    assert f["atr_pct"] == pytest.approx(ATR / 13 * 100)
    assert f["atr_ratio_to_20bar_mean"] == pytest.approx(ATR / ((19 * 1.0 + ATR) / 20))
    rets = np.array([0.0] * 19 + [0.3])
    assert f["realized_vol_20bar_pct"] == pytest.approx(np.std(rets, ddof=1) * 100)


# ---------------------------------------------------------------- sesión / gap
@pytest.mark.parametrize("m,bucket", [(0.5, "09:30-09:45"), (14.9, "09:30-09:45"), (15, "09:45-10:00"),
                                      (29, "09:45-10:00"), (30, "10:00-10:30"), (60, "10:30-11:00"),
                                      (90, "after_11:00"), (-1, None)])
def test_session_buckets(m, bucket):
    assert session_bucket(m) == bucket


@pytest.mark.parametrize("day", ["2026-06-01", "2026-12-01"])  # EDT y EST
def test_minutes_since_open_use_decision_time_in_new_york(day):
    start = pd.Timestamp(f"{day} 09:14", tz=NY).tz_convert("UTC")  # vela de señal 09:44 -> decisión 09:45
    f = feats(jump_bars(n_flat=30, start=start))
    assert f["entry_time_et"] == "09:45" and f["minutes_since_market_open"] == pytest.approx(15.0)
    assert f["session_bucket"] == "09:45-10:00"
    assert (f["first_15_min"], f["first_30_min"], f["first_hour"]) == (False, True, True)


def _gap_frame(signal_hhmm="09:31"):
    rows = [("2026-05-29 15:58", 50.0), ("2026-05-29 15:59", 51.0), ("2026-05-29 16:30", 60.0),  # viernes
            ("2026-06-01 09:00", 55.0), ("2026-06-01 09:29", 53.0), ("2026-06-01 09:30", 52.0),  # lunes
            ("2026-06-01 09:31", 52.5)]
    rows = [r for r in rows if r[0] <= f"2026-06-01 {signal_hhmm}"]
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz=NY).tz_convert("UTC") for t, _ in rows])
    opens = [50.0, 50.5, 59.0, 54.0, 53.5, 51.5, 52.0][:len(rows)]
    closes = [c for _, c in rows]
    return pd.DataFrame({"open": opens, "high": [max(o, c) + .1 for o, c in zip(opens, closes)],
                         "low": [min(o, c) - .1 for o, c in zip(opens, closes)], "close": closes,
                         "volume": 1000.0}, index=idx)


def test_opening_gap_uses_last_regular_close_and_0930_open():
    f = feats(_gap_frame())
    assert f["gap_prev_close"] == 51.0  # 15:59 del viernes, no el post-market de 16:30
    assert f["gap_session_open"] == 51.5
    assert f["opening_gap_pct"] == pytest.approx((51.5 - 51.0) / 51.0 * 100)
    assert f["gap_direction"] == "up" and f["gap_same_direction_as_trade"] is True


def test_opening_gap_is_null_when_not_yet_known_or_no_previous_close():
    early = feats(_gap_frame("09:29"))  # decisión 09:30: la vela 09:30 aún no existe
    assert early["opening_gap_pct"] is None and early["gap_direction"] is None
    no_prev = feats(_gap_frame().iloc[3:])
    assert no_prev["opening_gap_pct"] is None and no_prev["gap_prev_close"] is None


# ---------------------------------------------------------------- sin look-ahead
def _trade_then(after):
    return BASE + [101.5, 102.1, 101.3] + after


def test_future_bars_change_outcome_but_not_entry_context():
    a = run({"AAA": make_bars(_trade_then([101.0] * 5))})
    b = run({"AAA": make_bars(BASE + [101.6, 104.0, 107.0, 110.0, 99.0] + [99.0] * 5)})
    ta, tb = a.trades[0], b.trades[0]
    assert ta["entry_signal_timestamp"] == tb["entry_signal_timestamp"]
    assert ta["entry_context"] == tb["entry_context"]
    assert ta["mfe_r"] != tb["mfe_r"]


def test_engine_passes_read_only_arrays_ending_at_the_signal_bar(monkeypatch):
    seen = []
    real = backtest_engine.entry_features

    def spy(ts, o, h, l, c, v, **kw):
        seen.append((int(ts[-1]), len(c), [x.flags.writeable for x in (ts, o, h, l, c, v)]))
        return real(ts, o, h, l, c, v, **kw)

    monkeypatch.setattr(backtest_engine, "entry_features", spy)
    df = make_bars(_trade_then([101.0] * 5))
    r = run({"AAA": df})
    [(last_ts, n, writeable)] = seen
    assert pd.Timestamp(last_ts, tz="UTC").isoformat() == r.trades[0]["entry_signal_timestamp"] == df.index[25].isoformat()
    assert n == 26 and not any(writeable)


def test_risk_atr_and_liquidity_are_the_risk_managers_own_values():
    r = run({"AAA": make_bars(_trade_then([101.0] * 5))})
    ctx = r.trades[0]["entry_context"]
    assert ctx["risk_atr"] == pytest.approx(ctx["atr"])
    assert ctx["risk_liquidity_dollar"] > 0 and ctx["risk_rr"] > 0
    ev = r.risk_evaluations[0]
    assert ev["stop_price"] == pytest.approx(ev["entry_price"] - production_args().atr_sl_mult * ctx["atr"], abs=0.01)


def _random_bars(seed, sym_n=3, n=390):
    rng = np.random.default_rng(seed)
    return {f"S{k}": make_bars([round(v, 4) for v in 100 + np.cumsum(rng.normal(0, 0.25, n))]) for k in range(sym_n)}


def _core(result):
    s = summarize(result)
    s.pop("entry_quality")
    return to_json({"summary": s, "equity": result.equity_curve, "risk": result.risk_evaluations,
                    "fills": result.fills, "counters": result.counters,
                    "trades": [{k: v for k, v in t.items() if k != "entry_context"} for t in result.trades]})


def test_diagnostics_do_not_change_any_decision_or_pnl(monkeypatch):
    bars = _random_bars(7)
    on = run(bars, entry_diagnostics=True)
    off = run(bars, entry_diagnostics=False)
    assert on.trades and all("entry_context" in t for t in on.trades)
    assert all("entry_context" not in t for t in off.trades)
    assert _core(on) == _core(off)
    # contexto absurdo: tampoco puede influir
    monkeypatch.setattr(backtest_engine, "entry_features", lambda *a, **k: {"ma_spread_atr": 1e9})
    assert _core(run(bars)) == _core(off)


def test_outcome_labels_are_computed_after_the_fact_only(monkeypatch):
    calls = []
    import src.backtest_entry_quality as eq
    monkeypatch.setattr(eq, "entry_quality_rows", lambda *a, **k: calls.append(1) or [])
    run(_random_bars(7))
    assert calls == []  # el motor nunca mira etiquetas de resultado


def test_baseline_artifacts_are_byte_identical_with_and_without_diagnostics(tmp_path):
    bars = _random_bars(11)
    for name, flag in (("on", True), ("off", False)):
        r = run(bars, entry_diagnostics=flag)
        write_outputs(r, summarize(r), tmp_path / name)
    for f in ("trades.csv", "daily_results.csv", "equity_curve.csv"):
        assert (tmp_path / "on" / f).read_bytes() == (tmp_path / "off" / f).read_bytes()
    a = json.loads((tmp_path / "on" / "summary.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "off" / "summary.json").read_text(encoding="utf-8"))
    assert a == b and "entry_quality" not in a


# ---------------------------------------------------------------- unión / agregados
def _t(tid, pnl, mfe, feat, cls=None, **ctx):
    res = "win" if pnl > 0.005 else "loss" if pnl < -0.005 else "breakeven"
    context = {"ma_spread_atr": feat, "slow_ma_slope_direction": ctx.get("dir", "rising"),
               "first_30_min": ctx.get("f30", False), "crossover_age_bucket": "0", "atr_pct": feat,
               "volume_ratio_20": feat, "bar_range_atr": feat, "close_minus_fast_ma_atr": feat}
    return {"trade_id": tid, "symbol": "AAA", "realized_pnl": pnl, "realized_r": pnl / 100, "mfe_r": mfe,
            "mae_r": 0.2, "exit_reason": "signal_exit", "result": res,
            "excursion_class": cls or ("clean_winner" if pnl > 0 else "never_worked"), "entry_context": context}


def test_outcome_join_and_labels():
    trades = [_t(1, -50, 0.1, 0.1), _t(2, 30, 0.7, 0.2), _t(3, 120, 1.6, 0.3), _t(4, 200, 2.1, 0.4),
              dict(_t(5, -10, None, 0.5), excursion_class="unclassified"), {"trade_id": 6, "realized_pnl": 1.0}]
    rows = entry_quality_rows(trades)
    assert [r["trade_id"] for r in rows] == [1, 2, 3, 4, 5]  # sin contexto -> fuera
    assert set(ROW_COLUMNS) == set(rows[0])
    r1, r2, r3, r4, r5 = rows
    assert (r1["never_worked"], r1["reached_0_5r"], r1["loser"], r1["outcome_group"]) == (True, False, True, "never_worked")
    assert (r2["never_worked"], r2["reached_0_5r"], r2["reached_1r"], r2["outcome_group"]) == (False, True, False,
                                                                                               "reached_0_5r_not_1r")
    assert r3["reached_1_5r"] and not r3["reached_2r"] and r4["reached_2r"] and r4["clean_winner"]
    assert r5["never_worked"] is None and r5["outcome_group"] is None
    assert r1["ma_spread_atr"] == 0.1 and r1["diagnostic_class"] == "never_worked" and r2["profitable"]


def test_bucket_aggregation():
    trades = [_t(i, (i - 5) * 10.0, i / 5, float(i)) for i in range(10)]
    df = pd.DataFrame(entry_quality_rows(trades), columns=ROW_COLUMNS)
    rows = bucket_tables(df)["ma_spread_atr"]
    assert [r["bucket"] for r in rows] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert [r["trades"] for r in rows] == [2] * 5
    q1, q5 = rows[0], rows[-1]
    assert (q1["lo"], q1["hi"]) == (0.0, 1.0)
    assert q1["win_rate_pct"] == 0.0 and q1["total_pnl"] == pytest.approx(-90.0)
    assert q5["win_rate_pct"] == 100.0 and q5["pct_reached_1r"] == 100.0
    assert q5["avg_realized_r"] == pytest.approx(0.35) and q5["avg_mfe_r"] == pytest.approx(1.7)
    assert q1["pct_reached_0_5r"] == 0.0 and q1["median_realized_r"] == pytest.approx(-0.45)
    cat = {r["bucket"]: r["trades"] for r in bucket_tables(df)["slow_ma_slope_direction"]}
    assert cat == {"rising": 10}


def test_crosstab_aggregation():
    trades = [_t(i, 10.0 if i % 2 else -10.0, 0.6, float(i), dir="rising" if i < 6 else "falling", f30=i < 3)
              for i in range(12)]
    df = pd.DataFrame(entry_quality_rows(trades), columns=ROW_COLUMNS)
    rows = crosstabs(df)
    first = [r for r in rows if r["crosstab"] == "first_30_min x slow_ma_slope_direction"]
    assert sum(r["trades"] for r in first) == 12
    cell = {(r["row_value"], r["col_value"]): r["trades"] for r in first}
    assert cell == {("True", "rising"): 3, ("False", "rising"): 3, ("False", "falling"): 6}
    for name in {r["crosstab"] for r in rows}:
        assert sum(r["trades"] for r in rows if r["crosstab"] == name) == 12


def test_correlation_output_and_cles():
    trades = [_t(i, i * 10.0 - 30, i ** 3 / 100, float(i)) for i in range(12)]
    df = pd.DataFrame(entry_quality_rows(trades), columns=ROW_COLUMNS)
    c = correlations(df)["ma_spread_atr"]
    assert c["realized_r"]["pearson"] == pytest.approx(1.0) and c["realized_r"]["spearman"] == pytest.approx(1.0)
    assert c["mfe_r"]["spearman"] == pytest.approx(1.0) and c["mfe_r"]["pearson"] < 0.99
    assert c["mae_r"]["pearson"] is None  # mae constante
    assert cles(pd.Series([1, 2]), pd.Series([3, 4])) == 1.0
    assert cles(pd.Series([3, 4]), pd.Series([1, 2])) == 0.0
    assert cles(pd.Series([1, 2]), pd.Series([1, 2])) == 0.5


def test_summary_report_and_files(tmp_path):
    r = run({"AAA": make_bars(_trade_then([101.0] * 5))})
    s = summarize(r)
    eq = s["entry_quality"]
    assert eq["accepted_entries"] == 1 and eq["completed_with_context"] == 1
    assert "CORRELATION IS NOT CAUSATION" in eq["disclaimer"]
    text = format_report(s)
    assert "ENTRY QUALITY DIAGNOSTICS" in text
    for banned in ("use this filter", "change this threshold", "best parameter"):
        assert banned not in text.lower()
    json.loads(to_json(s))
    write_outputs(r, s, tmp_path)
    rows = list(pd.read_csv(tmp_path / "entry_quality.csv").to_dict("records"))
    assert len(rows) == 1 and rows[0]["trade_id"] == r.trades[0]["trade_id"]
    assert list(pd.read_csv(tmp_path / "entry_quality.csv").columns) == ROW_COLUMNS
    saved = json.loads((tmp_path / "entry_quality_summary.json").read_text(encoding="utf-8"))
    assert set(saved["answers"]) >= {"q1_ma_separation", "q2_slow_ma_direction", "q5_crossover_age",
                                     "q8_separation_never_worked_vs_reached_1r"}
    assert (tmp_path / "entry_quality_buckets.csv").exists() and (tmp_path / "entry_quality_crosstabs.csv").exists()
    [row] = json.loads((tmp_path / "entry_quality.json").read_text(encoding="utf-8"))
    assert row["first_30_min"] is True and isinstance(row["ma_spread_atr"], float)


def test_empty_summary_is_safe():
    s = entry_quality_summary([], accepted_entries=0)
    assert s["outcomes"] == {} and "No completed trades" in format_entry_quality(s)


def test_entry_quality_outputs_are_deterministic(tmp_path):
    bars = _random_bars(5)
    for name in ("a", "b"):
        r = run(bars)
        write_outputs(r, summarize(r), tmp_path / name)
    for f in ("entry_quality.csv", "entry_quality.json", "entry_quality_summary.json",
              "entry_quality_buckets.csv", "entry_quality_crosstabs.csv"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
