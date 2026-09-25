"""
H001 DEVELOPMENT AUTOPSY: guarda de higiene (solo development, sin bypass),
features con valores a mano y bordes exactos de buckets, métricas de gatillo y
de persecución, sin look-ahead, embudo de señales (clasificación exacta y
separación aceptadas/rechazadas), MFE/MAE en 5Min con scale-out, agregados,
reproducción de la corrida guardada y salidas deterministas. Sin red.
"""
import json
import shutil

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca
from src import h001_autopsy as ap
from src import strategy_v2_h001 as h
from src.backtest_engine import BacktestConfig, BacktestEngine, production_args
from src.research_protocol import load_protocol, validate_protocol
from src.strategy import StrategyResult

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


def _utc(day, hhmm):
    return pd.Timestamp(f"{day} {hhmm}", tz=NY).tz_convert("UTC")


# ================================================================ higiene
def test_scope_accepts_only_development():
    p = load_protocol()
    assert ap.development_scope(p)["start"] == "2024-01-02"
    for name in ("validation", "known_diagnostic", "forward", "warmup", "nope"):
        with pytest.raises(h.HygieneError):
            ap.development_scope(p, name)


def test_scope_rejects_a_protocol_whose_development_name_has_other_role():
    p = load_protocol()
    p["splits"][1]["role"] = "validation"
    p["splits"][2]["role"] = "development"
    with pytest.raises(h.HygieneError, match="solo acepta DEVELOPMENT"):
        ap.development_scope(validate_protocol(p))


def test_cli_refuses_non_development_without_bypass(tmp_path, capsys):
    for name in ("validation", "known_diagnostic", "forward"):
        assert ap.main(["--split", name, "--output-dir", str(tmp_path / "o")]) == 2
        assert "prohibido" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
    import inspect
    src = inspect.getsource(ap.main)
    assert "--start" not in src and "--end" not in src and "--force" not in src


def test_writer_refuses_to_overwrite_original_results():
    with pytest.raises(h.HygieneError):
        ap.write_autopsy({"summary": {}, "trades": pd.DataFrame(), "signals": pd.DataFrame()}, ap.STORED_RUN)


# ================================================================ features a mano
def _ind(n=40):
    """Indicadores sintéticos: EMA20=100, EMA50=98, ATR=2, velas planas en 101 salvo lo que ajuste el test."""
    d = {"o": np.full(n, 101.0), "h": np.full(n, 102.0), "l": np.full(n, 101.0), "c": np.full(n, 101.0),
         "v": np.full(n, 100.0), "e20": np.full(n, 100.0), "e50": np.full(n, 98.0), "atr": np.full(n, 2.0),
         "minute": np.full(n, 10 * 60), "date": np.array(["d"] * n)}
    return d


@pytest.mark.parametrize("low,dist,group", [(100.0, 0.0, "penetrated"), (99.0, -0.5, "penetrated"),
                                            (100.5, 0.25, "close"), (100.02, 0.01, "close"),
                                            (100.52, 0.26, "loose"), (101.0, 0.5, "loose"), (101.02, 0.51, "beyond_0_50")])
def test_pullback_depth_value_and_exact_bucket_boundaries(low, dist, group):
    d = _ind()
    t = 30
    d["l"][t - 5:t] = 101.5
    d["l"][t - 3] = low
    f = ap.signal_features(d, t)
    assert f["pullback_min_distance_ema20_atr"] == pytest.approx(dist)
    assert f["depth_group"] == group
    assert f["pullback_touched_or_crossed_ema20"] is (dist <= 0)
    assert f["pullback_crossed_below_ema20"] is (dist < 0)


def test_pullback_structure_and_direction_boundaries():
    d = _ind()
    t = 30
    d["o"][t - 5:t] = [104, 103, 102, 101.5, 101]
    d["c"][t - 6:t] = [101, 103, 102, 101.5, 101, 100.5]      # t-6 = referencia de la 1a caída
    d["h"][t - 5:t] = [105, 104, 103, 102, 101.5]
    d["l"][t - 5:t] = [102.5, 101.5, 101, 100.5, 100.2]
    f = ap.signal_features(d, t)
    assert f["n_red"] == 5 and f["n_green"] == 0
    assert f["n_down_closes"] == 4 and f["consecutive_down_before_trigger"] == 4
    assert f["pullback_range_atr"] == pytest.approx((105 - 100.2) / 2)
    assert f["max_pullback_5bar_high_atr"] == pytest.approx((105 - 100.2) / 2)
    assert f["pullback_net_return_atr"] == pytest.approx((100.5 - 104) / 2)
    assert f["pullback_net_return_pct"] == pytest.approx((100.5 / 104 - 1) * 100)
    assert f["pullback_direction"] == "down" and f["all_pullback_closes_above_ema20"] is True
    for close_last, label in ((104 - 0.5, "down"), (104 - 0.49, "sideways"), (104 + 0.5, "up"), (104 + 0.49, "sideways")):
        d2 = _ind()
        d2["o"][t - 5] = 104
        d2["c"][t - 1] = close_last
        assert ap.signal_features(d2, t)["pullback_direction"] == label


def test_trend_age_and_maturity_metrics():
    d = _ind()
    t = 30
    d["e20"][: t - 7] = 97.0            # EMA20 < EMA50 hasta t-8; > desde t-7 -> 8 velas continuas
    f = ap.signal_features(d, t)
    assert f["trend_age_bars"] == 8 and f["trend_age_bucket"] == "6-12" and f["bars_since_bullish_crossover"] == 7
    assert f["trend_age_truncated"] is False
    assert f["spread_atr"] == pytest.approx(1.0) and f["close_above_ema20_atr"] == pytest.approx(0.5)
    assert f["close_above_ema50_atr"] == pytest.approx(1.5)
    g = ap.signal_features(_ind(), t)                    # EMA20 > EMA50 en toda la historia
    assert g["trend_age_bars"] == 31 and g["trend_age_truncated"] is True and g["bars_since_bullish_crossover"] is None
    for n, b in ((5, "0-5"), (6, "6-12"), (12, "6-12"), (13, "13-24"), (24, "13-24"), (25, "25-48"), (48, "25-48"), (49, "49+")):
        assert ap._trend_age_bucket(n) == b


def test_ema50_slope_direction_uses_existing_flat_convention():
    d = _ind()
    t = 30
    d["e50"][t] = 98.0 + 0.02 * 3 * 2.0 - 1e-9    # justo por debajo de 0.02 ATR/vela
    assert ap.signal_features(d, t)["ema50_slope_direction"] == "flat"
    d["e50"][t] = 98.0 + 0.02 * 3 * 2.0
    assert ap.signal_features(d, t)["ema50_slope_direction"] == "rising"


@pytest.mark.parametrize("close,loc,bucket", [(103.0, 1.0, "top_quarter"), (102.25, 0.75, "top_quarter"),
                                              (102.1, 0.70, "upper_half"), (101.5, 0.5, "upper_half"),
                                              (101.35, 0.45, "below_mid")])
def test_trigger_metrics_and_location_buckets(close, loc, bucket):
    d = _ind()
    t = 30
    d["o"][t], d["l"][t], d["h"][t], d["c"][t] = 101.0, 100.0, 103.0, close
    d["h"][t - 1] = 101.5
    d["v"][t] = 300.0
    f = ap.signal_features(d, t)
    assert f["trigger_close_location"] == pytest.approx(loc)
    assert f["trigger_location_bucket"] == bucket
    assert f["trigger_in_upper_25"] is (loc >= 0.75) and f["trigger_below_mid"] is (loc < 0.5)
    assert f["trigger_body_atr"] == pytest.approx(abs(close - 101.0) / 2)
    assert f["trigger_range_atr"] == pytest.approx(1.5)
    assert f["breakout_distance_atr"] == pytest.approx((close - 101.5) / 2)
    assert f["trigger_return_pct"] == pytest.approx((close / 101.0 - 1) * 100)
    assert f["volume_ratio_5"] == pytest.approx(3.0) and f["volume_ratio_20"] == pytest.approx(3.0)
    assert f["atr_pct"] == pytest.approx(2.0 / close * 100)


@pytest.mark.parametrize("minute,bucket", [(600, "10:00-10:30"), (629, "10:00-10:30"), (630, "10:30-11:00"),
                                           (660, "11:00-12:00"), (720, "12:00-14:00"), (839, "12:00-14:00"),
                                           (840, "14:00-16:00"), (959, "14:00-16:00"), (960, None), (599, None)])
def test_time_buckets_exact_boundaries(minute, bucket):
    assert ap.time_bucket(minute) == bucket


def test_decision_time_is_signal_bar_start_plus_five_minutes():
    d = _ind()
    d["minute"][30] = 10 * 60 + 25      # vela 10:25 -> decisión 10:30
    assert ap.signal_features(d, 30)["time_bucket"] == "10:30-11:00"


def test_features_have_no_look_ahead():
    rng = np.random.default_rng(3)
    d = _ind(60)
    for k in ("o", "h", "l", "c", "v"):
        d[k] = d[k] + rng.normal(0, 0.3, 60)
    d["h"] = np.maximum.reduce([d["h"], d["o"], d["c"]]) + 0.1
    d["l"] = np.minimum.reduce([d["l"], d["o"], d["c"]]) - 0.1
    base = ap.signal_features(d, 40)
    for k in ("o", "h", "l", "c", "v", "e20", "e50", "atr"):
        d[k][41:] = d[k][41:] * 3 + 11
    assert ap.signal_features(d, 40) == base


def test_entry_chase_and_cost_metrics():
    feat = {"atr": 2.0, "signal_close": 100.0, "ema20": 99.0, "ema50": 97.0}
    trade = {"entry_fill_price": 101.05, "risk_per_share_modeled": 4.0, "initial_qty": 10,
             "legs": [{"reference_open": 110.0, "price": 109.945, "qty": 10}]}
    fill = {"price": 101.05, "reference_open": 101.0, "qty": 10}
    m = ap.trade_execution_metrics(trade, feat, fill)
    assert m["entry_fill_distance_atr"] == pytest.approx(0.525)
    assert m["next_bar_gap_atr"] == pytest.approx(0.5)
    assert m["entry_above_ema20_atr"] == pytest.approx(1.025) and m["entry_above_ema50_atr"] == pytest.approx(2.025)
    assert m["slippage_cost_usd"] == pytest.approx(0.05 * 10 + 0.055 * 10)
    assert m["slippage_cost_r"] == pytest.approx((0.5 + 0.55) / 40)


# ================================================================ agregados
def _tr_df():
    rows = []
    specs = [("A", "stop_hit", -100, -1.0, 0.1, 1.0, "penetrated", 0.2), ("A", "giveback_close", 50, 0.5, 1.2, 0.2, "close", 0.3),
             ("B", "take_profit_hit", 300, 3.0, 3.1, 0.1, "loose", 0.4), ("B", "giveback_close", -20, -0.2, 0.3, 0.4, "loose", 0.5),
             ("C", "stop_hit", -80, -0.8, 0.6, 0.9, "penetrated", 0.6)]
    for k, (sym, ex, pnl, r, mfe, mae, dg, atrp) in enumerate(specs):
        row = {"trade_id": k + 1, "symbol": sym, "exit_reason": ex, "realized_pnl": pnl, "realized_r": r,
               "mfe_r": mfe, "mae_r": mae, "result": "win" if pnl > 0 else "loss", "depth_group": dg,
               "atr_pct": atrp, "exit_efficiency": (r / mfe) if mfe > 0 else None, "mfe_left_on_table_r": mfe - r,
               "minutes_to_mae": 10.0 * (k + 1), "mae_before_mfe": k % 2 == 0}
        for name, lvl in ap.REACH:
            row[name] = mfe >= lvl
        rows.append(row)
    return pd.DataFrame(rows)


def test_outcome_metrics_and_group_tables():
    tr = _tr_df()
    m = ap.outcome_metrics(tr)
    assert m["trades"] == 5 and m["pnl"] == 150 and m["win_rate_pct"] == 40.0
    assert m["expectancy_r"] == pytest.approx(1.5 / 5) and m["profit_factor"] == pytest.approx(350 / 200)
    assert m["pct_reached_0_5r"] == 60.0 and m["pct_reached_1r"] == 40.0
    g = {r["depth_group"]: r for r in ap.group_table(tr, "depth_group", list(ap.DEPTH_GROUPS))}
    assert (g["penetrated"]["trades"], g["close"]["trades"], g["loose"]["trades"]) == (2, 1, 2)
    assert g["loose"]["pnl"] == 280
    empty = ap.group_table(tr[tr["symbol"] == "A"], "depth_group", list(ap.DEPTH_GROUPS))
    assert [r["trades"] for r in empty] == [1, 1, 0]


def test_symbol_exit_and_loss_aggregation():
    tr = _tr_df()
    sy = {r["symbol"]: r for r in ap.symbol_table(tr, ["A", "B", "C", "D"])}
    assert sy["B"]["take_profit_hit_pnl"] == 300 and sy["B"]["giveback_close_pnl"] == -20 and sy["D"]["trades"] == 0
    ex = ap.exit_reason_autopsy(tr)
    assert ex["A_giveback_aggregate_pnl"] == 30
    assert ex["C_stop_hit"]["pct_immediate_failure_mfe_lt_0_25r"] == 50.0
    assert ex["D_take_profit"]["share_of_gross_profit_pct"] == pytest.approx(300 / 350 * 100)
    lb = ap.loss_behavior(tr)
    assert lb["losing_trades"] == 3 and lb["q1_pct_never_reached_0_25r"] == pytest.approx(100 / 3)
    assert lb["q2_pct_never_reached_0_50r"] == pytest.approx(200 / 3) and lb["q3_pct_reached_1r_then_negative"] == 0.0


def test_quantiles_crosstabs_and_separations():
    tr = _tr_df()
    tr["atr_pct_quintile"] = ap.quantile_labels(tr["atr_pct"], 5, "Q")
    assert list(tr["atr_pct_quintile"]) == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    tr["trigger_strength"] = ap.quantile_labels(pd.Series([1, 2, 3, 4, 5]), 3, "T")
    for col in ap.NUMERIC + ap.EXECUTION_NUMERIC:
        tr[col] = np.arange(5, dtype=float)
    for col in ap.CATEGORICAL:
        if col not in tr:
            tr[col] = "x"
    tr["trend_age_bucket"] = ["0-5", "6-12", "0-5", "6-12", "0-5"]
    tr["ema50_slope_direction"] = "rising"
    rows = ap.crosstabs(tr)
    for name in {r["crosstab"] for r in rows}:
        assert sum(r["trades"] for r in rows if r["crosstab"] == name) == 5
    sep = ap.separations(tr)
    assert sep["comparisons"]["A_losers_vs_winners"]["n_losers"] == 3
    assert sep["comparisons"]["E_loose_vs_penetrated"]["n_penetrated"] == 2


# ================================================================ motor: embudo, MFE/MAE 5Min
class _ScriptedBuy:
    """Estrategia de prueba: BUY en los timestamps indicados (para ejercitar el embudo y el scale-out)."""
    min_bars = 1

    def __init__(self, when):
        self.when = set(when)

    def evaluate(self, df):
        return StrategyResult("BUY" if df.index[-1] in self.when else None, "test")


def _bars5(closes, day="2024-03-04", spread=0.05):
    t0 = _utc(day, "09:30")
    opens = [closes[0]] + list(closes[:-1])
    idx = pd.DatetimeIndex([t0 + pd.Timedelta(minutes=5 * i) for i in range(len(closes))])
    return pd.DataFrame({"open": opens, "high": [max(o, c) + spread for o, c in zip(opens, closes)],
                         "low": [min(o, c) - spread for o, c in zip(opens, closes)], "close": list(closes),
                         "volume": 1_000_000.0}, index=idx)


def test_5min_mfe_mae_continues_through_scale_out():
    closes = [100.0] * 20 + [100.0, 100.4, 101.2, 102.4, 103.0, 102.0, 101.0, 100.5, 100.0, 100.0]
    df = _bars5(closes, spread=0.4)
    cfg = BacktestConfig(symbols=["AAA"], timeframe="5Min", window_hours_limit=False, record_evaluations=True)
    eng = ap._ObservingEngine(cfg, {"AAA": df}, production_args({"lookback": 50}), _ScriptedBuy([df.index[20]]))
    r = eng.run()
    [t] = r.trades
    assert t["scale_outs"] >= 1 and len(t["legs"]) >= 2
    entry, risk = t["entry_fill_price"], t["risk_per_share_modeled"]
    held = [c for ts, c in zip(df.index, closes) if t["entry_fill_timestamp"] <= ts.isoformat() < t["exit_fill_timestamp"]]
    assert t["mfe_r"] == pytest.approx((max(held) - entry) / risk)
    assert t["mae_r"] == pytest.approx((entry - min(held)) / risk)
    first_leg = t["legs"][0]["fill_timestamp"]
    assert t["mfe_timestamp"] > first_leg          # el pico llega DESPUÉS del scale-out: se siguió midiendo
    assert t["minutes_to_mfe"] % 5 == 0            # cierres de velas de 5 minutos


def test_funnel_classification_matches_engine_counters_and_separates_accepted_from_rejected():
    rng = np.random.default_rng(11)
    bars, when = {}, {}
    for k, sym in enumerate(["AAA", "BBB", "CCC"]):
        closes = list(100 + np.cumsum(rng.normal(0, 0.4, 78 * 3)))
        df = pd.concat([_bars5(closes[d * 78:(d + 1) * 78], day) for d, day in
                        enumerate(["2024-03-04", "2024-03-05", "2024-03-06"])])
        if sym == "CCC":
            df["volume"] = 10.0                      # rechazos por liquidez
        bars[sym] = df
        when[sym] = set(df.index[20::3])
    strat = _ScriptedBuy(set().union(*when.values()))
    cfg = BacktestConfig(symbols=list(bars), timeframe="5Min", window_hours_limit=False, record_evaluations=True)
    eng = ap._ObservingEngine(cfg, bars, production_args({"lookback": 50}), strat)
    r = eng.run()
    st = pd.DataFrame(eng.signal_stages)
    n_buy = sum(1 for e in r.evaluations if e["signal"] == "BUY")
    assert len(st) == n_buy and "unclassified" not in set(st["stage"])
    assert (st["stage"] == "risk_accepted").sum() == r.counters["risk"]["ACCEPT"]
    assert (st["stage"] == "risk_rejected").sum() == r.counters["risk"]["REJECT"]
    assert (st["stage"] == "circuit_breaker").sum() == sum(r.counters["circuit_breaker_blocked_entries"].values())
    assert (st["stage"] == "daily_profit_halt").sum() == r.counters["daily_profit_halt_blocked_entries"]
    assert (st["stage"] == "in_position").sum() > 0 and (st["stage"] == "risk_rejected").sum() > 0
    acc = {(s, ts) for s, ts, stg in zip(st["symbol"], st["bar_timestamp"], st["stage"]) if stg == "risk_accepted"}
    assert acc == {(t["symbol"], t["entry_signal_timestamp"]) for t in r.trades} | {
        (p["symbol"], p["trade"]["entry_signal_timestamp"]) for p in r.open_positions}
    rej = st[st["stage"] == "risk_rejected"]
    assert set(rej["symbol"]) >= {"CCC"} and set(rej["reason"]) >= {"LIQUIDITY_BELOW_MINIMUM"}
    f = ap.funnel(st.assign(reason=st["reason"]), len(r.trades))
    assert f["generated"] == n_buy and f["reached_risk"] == f["accepted"] + sum(f["rejected_by_reason"].values())


def test_observing_engine_does_not_change_results():
    rng = np.random.default_rng(5)
    closes = list(100 + np.cumsum(rng.normal(0, 0.4, 78 * 2)))
    df = pd.concat([_bars5(closes[:78], "2024-03-04"), _bars5(closes[78:], "2024-03-05")])
    strat = _ScriptedBuy(set(df.index[10::4]))
    cfg = BacktestConfig(symbols=["AAA"], timeframe="5Min", window_hours_limit=False, record_evaluations=True)
    a = ap._ObservingEngine(cfg, {"AAA": df}, production_args({"lookback": 50}), strat).run()
    b = BacktestEngine(cfg, {"AAA": df}, production_args({"lookback": 50}), strat).run()
    assert a.trades == b.trades and a.equity_curve == b.equity_curve and a.counters == b.counters


# ================================================================ extremo a extremo (mini dataset)
def _synth_5min(seed, sessions, start="2024-03-04"):
    rng = np.random.default_rng(seed)
    rows, idx, px = [], [], 100.0
    for d in pd.bdate_range(start, periods=sessions):
        for k in range(78):
            o = px + rng.normal(0, 0.1)
            c = o + 0.08 + rng.normal(0, 0.35)          # ATR% ~0.4-0.5%: por encima del mínimo implícito del chequeo RR
            rows.append((o, max(o, c) + abs(rng.normal(0, .15)), min(o, c) - abs(rng.normal(0, .15)), c, 1000.0))
            idx.append(_utc(d.date(), "09:30") + pd.Timedelta(minutes=5 * k))
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
                         "volume": 5000.0, "symbol": sym})
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(d / f"{sym}.csv", index=False)


def _mini_protocol():
    p = load_protocol()
    p["universe"]["symbols"] = ["AAA", "BBB"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": "2024-03-11", "end": "2024-03-13"},
                   {"name": "validation", "role": "validation", "start": "2024-03-14", "end": "2024-03-15"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    return validate_protocol(p)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    root = tmp_path_factory.mktemp("autopsy")
    for k, sym in enumerate(["AAA", "BBB"]):
        _write_1min(root, sym, _synth_5min(40 + k, 10), 50 + k)   # 2024-03-04 .. 2024-03-15 (incl. "validation")
    return root


def test_end_to_end_reproduces_stored_run_and_reads_no_later_bars(mini, tmp_path, monkeypatch):
    from src.research_h001 import build_report, run_split
    proto = _mini_protocol()
    stored = tmp_path / "stored"
    build_report(run_split(proto, "development", mini, {"id": h.HYPOTHESIS_ID, "status": "IMPLEMENTED"}), proto, stored)
    seen = []
    real = ap.load_symbol_bars

    def spy(data_dir, tf, sym, start=None, end=None):
        seen.append(end)
        return real(data_dir, tf, sym, start, end)
    monkeypatch.setattr(ap, "load_symbol_bars", spy)
    split = ap.development_scope(proto)
    res = ap.build_autopsy(proto, split, mini, stored_run=stored, bench_path=tmp_path / "none.json")
    s = res["summary"]
    assert s["data_access"]["reproduction_of_stored_run"]["trades_json_identical"] is True
    assert seen and all(e == ap._end_utc("2024-03-13") for e in seen)      # nunca se lee más allá de development
    assert s["consistency"]["signals_violating_h001_rules_on_recomputed_indicators"] == 0
    assert s["consistency"]["depth_groups_beyond_0_50"] == 0
    sig = res["signals"]
    assert len(sig) > 0 and sig["bar_timestamp"].map(pd.Timestamp).max() < ap._end_utc("2024-03-13")
    f = s["funnel"]
    assert f["generated"] == len(sig) and f["completed"] == len(res["trades"]) and f["completed"] > 0
    assert sum(f["not_reaching_risk"].values()) + f["reached_risk"] == f["generated"]


def test_zero_completed_trades_is_handled(tmp_path):
    rows = [{"symbol": "A", "bar_timestamp": "t", "stage": "risk_rejected", "reason": "RR_BELOW_MINIMUM",
             **{k: 0.0 for k in ap.NUMERIC}, **{k: "x" for k in ap.CATEGORICAL if k != "symbol"}}]
    sig = pd.DataFrame(rows)
    assert ap.funnel(sig, 0)["completed"] == 0 and ap.funnel(sig, 0)["rejected_by_reason"] == {"RR_BELOW_MINIMUM": 1}


def test_outputs_are_deterministic(mini, tmp_path):
    proto = _mini_protocol()
    split = ap.development_scope(proto)
    for name in ("a", "b"):
        res = ap.build_autopsy(proto, split, mini, verify_stored=False, bench_path=tmp_path / "none.json")
        ap.write_autopsy(res, tmp_path / name)
    files = sorted(p.name for p in (tmp_path / "a").iterdir())
    assert "h001_autopsy_summary.json" in files and "h001_autopsy_signal_funnel.csv" in files
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    text = ap.format_autopsy(json.loads((tmp_path / "a" / "h001_autopsy_summary.json").read_text(encoding="utf-8")))
    for banned in ("use this threshold", "best filter", "optimal", "should definitely"):
        assert banned not in text.lower()
