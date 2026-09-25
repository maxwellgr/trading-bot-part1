"""
SHARED TRADE-MANAGEMENT AUTOPSY (H001 vs H003, DEVELOPMENT): reproducción exacta de las corridas
guardadas, excursiones por cierres, detección vs fill del stop, latencia en R, sobrepaso de −1R,
trayectoria post-stop 1/2/3/6/12, sin look-ahead en la gestión, agrupaciones (giveback, TP,
scale-out, ciclo de vida, hora, símbolo), geometría, descomposición, determinismo e higiene. Sin red.
"""
import json

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca
from src import shared_management_autopsy as sm
from src.backtest_engine import BacktestConfig, BacktestEngine, production_args
from src.research_protocol import load_protocol, validate_protocol
from src.strategy import StrategyResult
from src.strategy_v2_h001 import HygieneError

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


class Buy:
    min_bars = 1

    def __init__(self, when):
        self.when = set(when)

    def evaluate(self, df):
        return StrategyResult("BUY" if df.index[-1] in self.when else None, "t")


def _frame(after_closes, after_opens):
    closes = [100.0] * 40 + list(after_closes)
    opens = [100.0] * 40 + list(after_opens)
    t0 = pd.Timestamp("2024-03-04 09:30", tz=NY).tz_convert("UTC")
    idx = pd.DatetimeIndex([t0 + pd.Timedelta(minutes=5 * k) for k in range(len(closes))])
    return pd.DataFrame({"open": opens, "high": [max(o, c) + 0.5 for o, c in zip(opens, closes)],
                         "low": [min(o, c) - 0.5 for o, c in zip(opens, closes)], "close": closes,
                         "volume": 1e6}, index=idx)


def _run(df, signal_idx=39):
    cfg = BacktestConfig(symbols=["AAA"], timeframe="5Min", window_hours_limit=False)
    eng = sm._ExitObserver(cfg, {"AAA": df}, production_args({"lookback": 50}), Buy([df.index[signal_idx]]))
    return eng, eng.run()


def _ind(df):
    ny = df.index.tz_convert(NY)
    return {"AAA": {"o": df["open"].to_numpy(float), "h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
                    "c": df["close"].to_numpy(float), "ts": np.asarray([t.isoformat() for t in df.index]),
                    "date": np.asarray(ny.date)}}


# ================================================================ stop: detección, fill, latencia
@pytest.fixture(scope="module")
def stop_case():
    # E = vela 40 (fill 100.05); cierre 97.5 en E+1 -> detección; fill en E+2 a la apertura 97.0 con slippage
    df = _frame([99.5, 97.5, 96.8, 96.0, 96.0, 96.0, 96.0, 97.0, 99.0, 101.0, 102.0, 102.0, 102.0, 102.0, 102.0],
                [100.0, 99.5, 97.0, 96.8, 96.0, 96.0, 96.0, 96.0, 97.0, 99.0, 101.0, 102.0, 102.0, 102.0, 102.0])
    eng, r = _run(df)
    rows = sm.trade_rows("T", r.trades, eng.exit_submits, _ind(df))
    return df, eng, r, rows


def test_stop_detection_and_fill_timestamps_and_prices(stop_case):
    df, eng, r, [row] = stop_case
    assert row["final_exit_reason"] == "stop_hit"
    assert row["detection_bar"] == df.index[41].isoformat()                      # cierre que dispara el stop
    assert row["detection_decision_ts"] == df.index[42].isoformat()             # conocido en start+5
    assert row["final_fill_ts"] == df.index[42].isoformat()                      # fill en la apertura siguiente
    assert row["detection_bar_close"] == 97.5 and row["next_bar_open"] == 97.0
    assert row["final_exit_fill"] == pytest.approx(97.0 * (1 - 5 / 10_000))
    assert row["stop_price_at_detection"] >= row["initial_stop_price"] and row["detection_bar_close"] <= row["stop_price_at_detection"]


def test_detection_fill_and_latency_r(stop_case):
    _, _, r, [row] = stop_case
    f, rps = row["entry_fill_price"], row["initial_risk_per_share"]
    assert row["detection_r"] == pytest.approx((97.5 - f) / rps)
    assert row["fill_r"] == pytest.approx((97.0 * 0.9995 - f) / rps)
    assert row["latency_cost_r"] == pytest.approx(row["fill_r"] - row["detection_r"])
    assert row["latency_cost_r"] < 0                                             # peor fill = costo adverso (negativo)
    assert row["latency_cost_usd"] == pytest.approx((97.0 * 0.9995 - 97.5) * row["final_leg_qty"])
    assert row["fill_r"] == pytest.approx(r.trades[0]["realized_r"])            # un solo tramo: fill_r == R realizado
    assert row["detection_gap_r"] <= 0


def test_shared_excursions_are_close_based_and_match_engine(stop_case):
    _, _, r, [row] = stop_case
    f, rps = row["entry_fill_price"], row["initial_risk_per_share"]
    assert row["bars_held"] == 2                                                 # cierres de E y E+1
    assert row["mfe_r_recomputed"] == pytest.approx((99.5 - f) / rps) == pytest.approx(row["mfe_r"])
    assert row["mae_r_recomputed"] == pytest.approx((f - 97.5) / rps) == pytest.approx(row["mae_r"])
    assert row["reached_0_25r"] is False and row["minutes_to_0_25r"] is None


def test_post_stop_paths_and_future_warning(stop_case):
    df, _, _, [row] = stop_case
    ind = _ind(df)
    pos = {"AAA": {t: k for k, t in enumerate(ind["AAA"]["ts"])}}
    p = sm.post_stop_path(ind, pos, row)
    f, rps = row["entry_fill_price"], row["initial_risk_per_share"]
    assert p["warning"] == sm.FUTURE_WARNING
    closes_after = [96.8, 96.0, 96.0, 96.0, 96.0, 97.0, 99.0, 101.0, 102.0, 102.0, 102.0, 102.0]   # desde la vela del fill
    for k in (1, 2, 3, 6, 12):
        r = [(c - f) / rps for c in closes_after[:k]]
        assert p[f"close_r_{k}"] == pytest.approx(r[-1]) and p[f"best_r_{k}"] == pytest.approx(max(r))
        assert p[f"worst_r_{k}"] == pytest.approx(min(r))
    assert p["recovered_above_entry_6"] is False and p["recovered_above_entry_12"] is True
    assert p["below_exit_1"] is True and p["hit_p050_12"] is (max((c - f) / rps for c in closes_after) >= 0.5)


def test_future_path_cannot_alter_real_trade_and_no_lookahead_in_management(stop_case):
    df, _, r, [row] = stop_case
    alt = df.copy()
    alt.iloc[43:, :4] = alt.iloc[43:, :4] + 40.0                                # futuro tras el fill de salida
    _, r2 = _run(alt)
    assert r2.trades[0]["exit_fill_timestamp"] == r.trades[0]["exit_fill_timestamp"]
    assert r2.trades[0]["realized_r"] == r.trades[0]["realized_r"]


def test_observer_does_not_change_engine_results(stop_case):
    df, _, r, _ = stop_case
    cfg = BacktestConfig(symbols=["AAA"], timeframe="5Min", window_hours_limit=False)
    plain = BacktestEngine(cfg, {"AAA": df}, production_args({"lookback": 50}), Buy([df.index[39]])).run()
    assert plain.trades == r.trades and plain.equity_curve == r.equity_curve


# ================================================================ agregados (valores a mano)
def _df(rows):
    base = {"initial_qty": 100, "initial_risk_per_share": 1.0, "holding_minutes": 30.0, "bars_held": 6, "scale_out_legs": 0,
            "scale_out_pnl": 0.0, "latency_cost_r": None, "latency_cost_usd": None, "detection_gap_r": None,
            "detection_r": None, "fill_r": None, "be_done_at_detection": None, "minutes_to_1_00r": None, "cost_r": 0.1,
            "mae_r": 0.3, "result": None, "symbol": "A", "atr_pct": 0.5, "stop_distance_pct": 1.0}
    out = []
    for r in rows:
        d = dict(base, **r)
        d["result"] = "win" if d["realized_pnl"] > 0.005 else "loss" if d["realized_pnl"] < -0.005 else "breakeven"
        for nm, lvl in sm.REACH:
            d[nm] = d["mfe_r"] >= lvl
        out.append(d)
    return pd.DataFrame(out)


def test_stop_overshoot_buckets_and_loss_split():
    r = pd.Series([-0.8, -1.0, -1.2, -1.3, -1.6, -2.1])
    o = sm.overshoot_buckets(r)
    assert o["pct_better_than_-1.0"] == pytest.approx(100 / 6) and o["pct_-1.0_to_-1.25"] == pytest.approx(200 / 6)
    assert o["pct_-1.25_to_-1.5"] == pytest.approx(100 / 6) and o["pct_worse_than_-1.5"] == pytest.approx(200 / 6)
    assert o["pct_worse_than_-2.0"] == pytest.approx(100 / 6) and o["largest_loss_r"] == -2.1
    df = _df([{"realized_pnl": -80.0, "realized_r": -0.8, "mfe_r": 0.1}, {"realized_pnl": -150.0, "realized_r": -1.5, "mfe_r": 0.0},
              {"realized_pnl": 50.0, "realized_r": 0.5, "mfe_r": 0.7}])
    ls = sm.loss_split(df)
    assert ls == {"losing_trades": 2, "gross_loss": -230.0, "loss_up_to_minus_1r": -180.0, "overshoot_beyond_minus_1r": -50.0}


@pytest.mark.parametrize("mfe,r,bucket", [(0.1, -0.5, "A_immediate_failure"), (0.1, 0.0, "F_no_progress_nonnegative"),
                                          (0.25, -1.0, "B_weak_progress"), (0.49, 0.2, "B_weak_progress"),
                                          (0.5, -0.3, "C_moderate_progress"), (1.0, 0.1, "D_reached_1r"),
                                          (1.99, 1.0, "D_reached_1r"), (2.0, 1.5, "E_reached_2r")])
def test_lifecycle_classification_boundaries(mfe, r, bucket):
    assert sm.lifecycle(mfe, r) == bucket


def test_giveback_takeprofit_scaleout_grouping_and_decomposition():
    df = _df([{"final_exit_reason": "giveback_close", "realized_pnl": -10.0, "realized_r": -0.1, "mfe_r": 0.2},
              {"final_exit_reason": "giveback_close", "realized_pnl": 10.0, "realized_r": 0.1, "mfe_r": 0.4},
              {"final_exit_reason": "giveback_close", "realized_pnl": 30.0, "realized_r": 0.3, "mfe_r": 0.8},
              {"final_exit_reason": "giveback_close", "realized_pnl": 70.0, "realized_r": 0.7, "mfe_r": 1.6, "scale_out_legs": 1, "scale_out_pnl": 50.0},
              {"final_exit_reason": "giveback_close", "realized_pnl": 120.0, "realized_r": 1.2, "mfe_r": 2.2},
              {"final_exit_reason": "take_profit_hit", "realized_pnl": 300.0, "realized_r": 3.0, "mfe_r": 3.1, "scale_out_legs": 2,
               "scale_out_pnl": 150.0, "minutes_to_1_00r": 20.0},
              {"final_exit_reason": "stop_hit", "realized_pnl": -130.0, "realized_r": -1.3, "mfe_r": 0.1, "latency_cost_r": -0.2,
               "latency_cost_usd": -20.0}])
    g = sm.giveback_autopsy(df)
    assert [(b["band"], b["count"]) for b in g["bands"]] == [("A_loss", 1), ("B_0_to_0.25", 1), ("C_0.25_to_0.5", 1),
                                                             ("D_0.5_to_1", 1), ("E_ge_1", 1)]
    assert g["pnl"] == 220.0 and g["aggregate_capture_realized_over_mfe"] == pytest.approx(2.2 / 5.2)
    ts = sm.tp_scaleout(df)
    assert ts["take_profit"]["count"] == 1 and ts["take_profit"]["median_minutes_to_1r"] == 20.0
    assert ts["scale_out"]["trades_with_scale_out"] == 2 and ts["scale_out"]["partial_exit_legs"] == 3
    assert ts["scale_out"]["partial_pnl"] == 200.0 and ts["scale_out"]["pct_final_take_profit"] == 50.0
    d = sm.decomposition(df)
    assert d["total_pnl"] == d["giveback_close_trades_pnl"] + d["stop_hit_trades_pnl"] + d["take_profit_hit_trades_pnl"] == 390.0
    assert d["stop_latency_usd_included_above"] == -20.0 and d["stop_overshoot_beyond_minus_1r_usd_included_above"] == -30.0
    assert d["gross_win"] == 530.0 and d["gross_loss"] == -140.0 and d["profit_factor"] == pytest.approx(530 / 140)


def test_time_bucket_and_symbol_aggregation():
    df = _df([{"final_exit_reason": "stop_hit", "realized_pnl": -100.0, "realized_r": -1.0, "mfe_r": 0.0, "symbol": "A",
               "time_bucket": "10:00-10:30", "latency_cost_r": -0.1},
              {"final_exit_reason": "giveback_close", "realized_pnl": 50.0, "realized_r": 0.5, "mfe_r": 0.9, "symbol": "A",
               "time_bucket": "10:00-10:30"},
              {"final_exit_reason": "take_profit_hit", "realized_pnl": 300.0, "realized_r": 3.0, "mfe_r": 3.0, "symbol": "B",
               "time_bucket": "14:00-16:00"}])
    sym = {r["symbol"]: r for r in sm.group_table(df, "symbol", extra=True)}
    assert sym["A"]["trades"] == 2 and sym["A"]["stop_hit_pct"] == 50.0 and sym["A"]["stop_hit_pnl"] == -100.0
    assert sym["A"]["giveback_close_pnl"] == 50.0 and sym["B"]["take_profit_hit_pnl"] == 300.0
    tb = {r["time_bucket"]: r for r in sm.group_table(df, "time_bucket", extra=True)}
    assert tb["10:00-10:30"]["stop_median_latency_r"] == -0.1 and tb["14:00-16:00"]["avg_realized_r"] == 3.0


def test_risk_geometry_on_real_trade(stop_case):
    _, _, _, [row] = stop_case
    assert row["stop_distance_atr"] == pytest.approx(row["initial_risk_per_share"] / row["atr_at_signal"])
    assert row["take_distance_atr"] == pytest.approx((row["initial_take_profit_price"] - row["modeled_entry"]) / row["atr_at_signal"])
    assert row["stop_distance_pct"] == pytest.approx(row["initial_risk_per_share"] / row["modeled_entry"] * 100)
    assert row["cost_r"] > 0 and row["entry_gap_r"] == pytest.approx((row["entry_fill_price"] - row["modeled_entry"]) / row["initial_risk_per_share"])


# ================================================================ higiene
def test_hygiene_development_only(tmp_path):
    p = load_protocol()
    for n in ("validation", "known_diagnostic", "forward"):
        with pytest.raises(HygieneError):
            sm.development_scope(p, n)
        assert sm.main(["--split", n, "--output-dir", str(tmp_path / "o")]) == 2
    assert not (tmp_path / "o").exists()
    for _, stored in sm.STRATEGIES.values():
        with pytest.raises(HygieneError):
            sm._guard(stored)


# ================================================================ extremo a extremo: reproducción exacta
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
    from src import research_h001 as r1, research_h003 as r3
    root = tmp_path_factory.mktemp("sm")
    for k, sym in enumerate(["AAA", "BBB", "CCC"]):
        _write_1min(root / "hist", sym, _synth(120 + k, 12), 130 + k)
    p = load_protocol()
    p["universe"]["symbols"] = ["AAA", "BBB", "CCC"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": "2024-03-11", "end": "2024-03-19"},
                   {"name": "validation", "role": "validation", "start": "2024-03-20", "end": "2024-03-21"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    p = validate_protocol(p)
    e = {"id": "X", "status": "IMPLEMENTED"}
    r1.build_report(r1.run_split(p, "development", root / "hist", e), p, root / "h001")
    r3.build_report(r3.run_split(p, "development", root / "hist", e), p, root / "h003", compare=False)
    return root, p


def _build(mini, out, workers=1):
    root, p = mini
    return sm.build(p, sm.development_scope(p), root / "hist", out, stored={"H001": root / "h001", "H003": root / "h003"},
                    workers=workers)


def test_exact_reproduction_of_h001_and_h003_and_excursion_consistency(mini, tmp_path):
    res = _build(mini, tmp_path / "o")
    rep = res["summary"]["reproduction"]
    assert rep["H001"]["identical"] and rep["H003"]["identical"]
    assert set(rep["H001"]["files_identical"]) == {"trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv", "summary.json"}
    for n in ("H001", "H003"):
        chk = res["summary"]["strategies"][n]["excursion_recompute_check"]
        assert chk["max_abs_diff_mfe"] < 1e-9 and chk["max_abs_diff_mae"] < 1e-9
        df = res["trades"][n]
        assert len(df) > 0 and set(df["lifecycle"]) <= {"A_immediate_failure", "B_weak_progress", "C_moderate_progress",
                                                          "D_reached_1r", "E_reached_2r", "F_no_progress_nonnegative"}
        st = df[df["final_exit_reason"] == "stop_hit"]
        assert st["detection_bar_close"].notna().all()                       # cada stop tiene su detección observada


def test_outputs_deterministic_and_labels(mini, tmp_path):
    for n in ("a", "b"):
        sm.write(_build(mini, tmp_path / n, workers=1 if n == "a" else 2), tmp_path / n)
    files = sorted(p.name for p in (tmp_path / "a").iterdir() if p.is_file())
    assert {"shared_management_summary.json", "trade_lifecycle.csv", "stop_autopsy.csv", "stop_latency.csv",
            "post_stop_paths.csv", "giveback_autopsy.csv", "takeprofit_scaleout.csv", "risk_geometry.csv", "by_symbol.csv",
            "by_time_bucket.csv", "correlations.csv", "h001_vs_h003.csv"} <= set(files)
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    text = sm.format_report(json.loads((tmp_path / "a" / "shared_management_summary.json").read_text(encoding="utf-8")))
    assert sm.FUTURE_WARNING in text
    for banned in ("change stop to", "best stop", "optimal exit", "h004 should"):
        assert banned not in text.lower()
