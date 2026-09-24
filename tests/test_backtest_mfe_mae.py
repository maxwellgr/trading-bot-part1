"""
Diagnóstico MFE/MAE del backtester: cálculo por trade (solo cierres conocidos),
timestamps, scale-outs, clases, eficiencia de salida, nulls sin R,
agregados (motivo de salida / símbolo / franja horaria) y que la medición
no cambie ninguna decisión ni el P&L. Sin red.
"""
import json

import pandas as pd
import pytest

from src import backtest_engine, broker_alpaca
from src.backtest_engine import BacktestConfig, production_args, run_backtest
from src.backtest_excursion import (
    classify, diagnostics, entry_time_bucket, excursion_fields, excursion_stats, format_diagnostics, new_tracker, observe,
)
from src.backtest_report import format_report, summarize, to_json, write_outputs

NY = "America/New_York"
T0 = pd.Timestamp("2026-06-01 09:30", tz=NY).tz_convert("UTC")
SLIP = 1.0005


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


def known(i, start=T0):
    """Instante en que se conoce el cierre de la vela i (inicio + 1 min)."""
    return (start + pd.Timedelta(minutes=i + 1)).isoformat()


def run(bars, symbols=None, overrides=None):
    return run_backtest(BacktestConfig(symbols=symbols or list(bars)), bars, production_args(overrides))


# base: 25 velas bajando, vela 25 = cruce alcista (BUY) a 101.5; fill de entrada en la apertura de la 26
BASE = decline(101.0, 25) + [101.5]


def giveback_trade_closes(after_exit=(80.0, 200.0)):
    # 26: 101.5 | 27: 102.1 (pico) | 28: 101.3 -> giveback decidido | 29: fill de salida en su apertura
    return BASE + [101.5, 102.1, 101.3] + list(after_exit)


# ---------------------------------------------------------------- cálculo por trade
def test_mfe_mae_values_timestamps_and_minutes_for_a_long_trade():
    r = run({"AAA": make_bars(giveback_trade_closes())})
    [t] = r.trades
    entry = t["entry_fill_price"]
    assert entry == pytest.approx(101.5 * SLIP) and t["entry_fill_timestamp"] == (T0 + pd.Timedelta(minutes=26)).isoformat()
    assert t["exit_reason"] == "giveback_close" and t["exit_fill_timestamp"] == (T0 + pd.Timedelta(minutes=29)).isoformat()
    assert t["excursion_bars"] == 3  # cierres de 26, 27 y 28; la vela 29 abre ya sin posición
    assert t["mfe_dollars_per_share"] == pytest.approx(102.1 - entry)
    assert t["mae_dollars_per_share"] == pytest.approx(entry - 101.3)
    assert t["mfe_pct"] == pytest.approx((102.1 - entry) / entry * 100)
    assert t["mfe_r"] == pytest.approx((102.1 - entry) / t["risk_per_share_modeled"])
    assert t["mae_r"] == pytest.approx((entry - 101.3) / t["risk_per_share_modeled"])
    assert t["mfe_timestamp"] == known(27) and t["mae_timestamp"] == known(28)
    assert t["minutes_to_mfe"] == 2.0 and t["minutes_to_mae"] == 3.0
    assert t["mae_before_mfe"] is False
    assert t["reached_0_5r"] is False and t["reached_1_0r"] is False


def test_intrabar_fields_are_diagnostic_only_and_separate():
    r = run({"AAA": make_bars(giveback_trade_closes())})
    [t] = r.trades
    assert t["intrabar_mfe_dollars_per_share_diag"] == pytest.approx(102.1 + 0.3 - t["entry_fill_price"])
    assert t["intrabar_mfe_dollars_per_share_diag"] > t["mfe_dollars_per_share"]  # high/low no alimentan las métricas primarias


def test_no_look_ahead_bars_after_the_exit_never_count():
    a = run({"AAA": make_bars(giveback_trade_closes(after_exit=(80.0, 200.0)))}).trades[0]
    b = run({"AAA": make_bars(giveback_trade_closes(after_exit=(150.0, 20.0)))}).trades[0]
    keys = [k for k in a if k.startswith(("mfe", "mae", "minutes_to", "reached", "excursion", "intrabar"))]
    assert {k: a[k] for k in keys} == {k: b[k] for k in keys}
    assert a["mfe_timestamp"] <= a["exit_fill_timestamp"] and a["mae_timestamp"] <= a["exit_fill_timestamp"]


def test_tracker_only_sees_prices_in_the_order_they_become_known():
    tr = new_tracker()
    observe(tr, 10.0, 10.5, 9.5, "2026-06-01T13:31:00+00:00")
    snapshot = dict(tr)
    observe(tr, 12.0, 12.2, 11.9, "2026-06-01T13:32:00+00:00")
    assert snapshot["max_close"] == 10.0 and tr["max_close"] == 12.0 and tr["max_close_ts"] == "2026-06-01T13:32:00+00:00"
    observe(tr, 12.0, 12.1, 11.8, "2026-06-01T13:33:00+00:00")  # empate: se conserva la primera vez
    assert tr["max_close_ts"] == "2026-06-01T13:32:00+00:00"


def test_excursion_spans_the_whole_trade_across_scale_outs():
    # 27: 103.0 -> scale-out (R>=1) | 28: fill del scale-out en la apertura, 103.0 | 29: 101.0 -> salida | 30: fill final
    closes = BASE + [101.5, 103.0, 103.0, 101.0] + decline(101.0, 5)
    r = run({"AAA": make_bars(closes)})
    [t] = r.trades
    assert t["scale_outs"] == 1 and [l["purpose"] for l in t["legs"]][0] == "scale_out"
    assert t["legs"][0]["fill_timestamp"] == (T0 + pd.Timedelta(minutes=28)).isoformat()
    assert t["excursion_bars"] == 4  # 26..29: sigue midiendo después del scale-out
    assert t["mfe_dollars_per_share"] == pytest.approx(103.0 - t["entry_fill_price"])
    assert t["mfe_timestamp"] == known(27)  # empate con la vela 28: cuenta la primera
    assert t["mae_timestamp"] == known(29) and t["mae_dollars_per_share"] == pytest.approx(t["entry_fill_price"] - 103.0 + 2.0)
    assert t["reached_1_0r"] is True and t["realized_pnl"] > 0


def test_never_reached_half_r_losing_trade_is_never_worked():
    c = decline(101.0, 20)
    p = c[-1]
    c += [p + 1.0, p - 0.8] + decline(p - 0.8, 5)
    [t] = run({"AAA": make_bars(c)}).trades
    assert t["result"] == "loss" and t["mfe_r"] < 0.5 and t["reached_0_5r"] is False
    assert t["excursion_class"] == "never_worked"


def test_same_timestamp_symbols_do_not_leak_into_each_others_excursion():
    solo = run({"AAA": make_bars(giveback_trade_closes())}).trades[0]
    other = make_bars([p * 3 for p in giveback_trade_closes(after_exit=(10.0, 999.0))])
    pair = run({"AAA": make_bars(giveback_trade_closes()), "BBB": other}, symbols=["AAA", "BBB"]).trades
    aaa = [t for t in pair if t["symbol"] == "AAA"][0]
    keys = [k for k in solo if k.startswith(("mfe", "mae", "excursion", "minutes_to"))]
    assert {k: solo[k] for k in keys} == {k: aaa[k] for k in keys}


def test_measurement_does_not_change_decisions_or_pnl(monkeypatch):
    bars = {"AAA": make_bars(BASE + [101.5, 103.0, 103.0, 101.0] + decline(101.0, 5)),
            "BBB": make_bars(giveback_trade_closes())}
    with_mfe = run(bars, symbols=["AAA", "BBB"])
    monkeypatch.setattr(backtest_engine, "observe", lambda *a, **k: None)
    without = run(bars, symbols=["AAA", "BBB"])
    core = ["symbol", "entry_fill_price", "exit_fill_price", "exit_reason", "realized_pnl", "realized_r", "legs"]
    assert [{k: t[k] for k in core} for t in with_mfe.trades] == [{k: t[k] for k in core} for t in without.trades]
    assert with_mfe.equity_curve == without.equity_curve and with_mfe.risk_evaluations == without.risk_evaluations


# ---------------------------------------------------------------- eficiencia / clases / nulls
def _trade(pnl, realized_r, entry=100.0, risk=1.0, closes=(), start="2026-06-01T13:30:00+00:00", **kw):
    tr = new_tracker()
    for k, c in enumerate(closes):
        observe(tr, c, c, c, (pd.Timestamp(start) + pd.Timedelta(minutes=k + 1)).isoformat())
    t = {"symbol": kw.get("symbol", "AAA"), "entry_fill_price": entry, "risk_per_share_modeled": risk,
         "realized_pnl": pnl, "realized_r": realized_r, "entry_fill_timestamp": start,
         "exit_reason": kw.get("exit_reason", "signal_exit"),
         "result": "win" if pnl > 0.005 else "loss" if pnl < -0.005 else "breakeven"}
    t.update(excursion_fields(t, tr))
    return t


def test_exit_efficiency_and_left_on_table():
    t = _trade(50.0, 0.5, closes=(100.4, 101.0, 100.6))
    assert t["mfe_r"] == pytest.approx(1.0)
    assert t["exit_efficiency"] == pytest.approx(0.5) and t["realized_vs_mfe_r"] == pytest.approx(0.5)
    assert t["mfe_left_on_table_r"] == pytest.approx(0.5)
    assert t["excursion_class"] == "clean_winner"
    gave_back = _trade(20.0, 0.2, closes=(101.0,))
    assert gave_back["excursion_class"] == "profitable_but_gave_back"
    negative = _trade(-30.0, -0.3, closes=(99.8, 99.5))
    assert negative["mfe_r"] < 0 and negative["exit_efficiency"] is None  # MFE <= 0: eficiencia no válida


def test_reached_1r_then_lost_and_almost_worked_classes():
    lost = _trade(-10.0, -0.1, closes=(100.5, 101.2, 99.9))
    assert lost["reached_1_0r"] is True and lost["excursion_class"] == "reached_1R_then_lost"
    almost = _trade(-10.0, -0.1, closes=(100.7, 99.9))
    assert almost["excursion_class"] == "almost_worked"
    assert excursion_stats([lost, almost])["pct_reached_1r_then_lost"] == pytest.approx(50.0)
    assert classify(5.0, None, None) == "unclassified"


def test_null_r_when_initial_risk_is_unavailable():
    for risk in (None, 0.0):
        t = _trade(10.0, None, risk=risk, closes=(101.0, 99.0))
        assert t["mfe_dollars_per_share"] == pytest.approx(1.0) and t["mae_dollars_per_share"] == pytest.approx(1.0)
        assert t["mfe_r"] is None and t["mae_r"] is None and t["reached_1_0r"] is None
        assert t["exit_efficiency"] is None and t["excursion_class"] == "unclassified"
    stats = excursion_stats([_trade(10.0, None, risk=None, closes=(101.0,)), _trade(10.0, 1.0, closes=(102.0,))])
    assert stats["trades"] == 2 and stats["trades_with_r"] == 1 and stats["median_mfe_r"] == pytest.approx(2.0)


# ---------------------------------------------------------------- agregados
def test_aggregation_by_exit_reason_symbol_and_time_bucket():
    trades = [
        _trade(-40.0, -0.4, closes=(99.8, 99.6), exit_reason="signal_exit", symbol="AAA", start="2026-06-01T13:45:00+00:00"),
        _trade(-120.0, -1.2, closes=(99.0, 98.8), exit_reason="stop_hit", symbol="AAA", start="2026-06-01T14:15:00+00:00"),
        _trade(150.0, 1.5, closes=(101.0, 101.8), exit_reason="take_profit_hit", symbol="BBB", start="2026-06-01T14:45:00+00:00"),
        _trade(30.0, 0.3, closes=(100.6, 100.2), exit_reason="giveback_close", symbol="BBB", start="2026-06-01T15:30:00+00:00"),
    ]
    d = diagnostics(trades, ["BBB", "AAA"])
    assert list(d["by_exit_reason"]) == ["signal_exit", "giveback_close", "stop_hit", "take_profit_hit"]
    assert d["by_exit_reason"]["stop_hit"]["median_mae_r"] == pytest.approx(1.2)
    assert d["by_exit_reason"]["take_profit_hit"]["pct_reached_1_0r"] == 100.0
    assert list(d["by_symbol"]) == ["BBB", "AAA"]
    assert d["by_symbol"]["AAA"]["trades"] == 2 and d["by_symbol"]["BBB"]["win_rate_pct"] == 100.0
    assert {k: v["trades"] for k, v in d["by_entry_time_bucket"].items()} == \
        {"09:30-10:00": 1, "10:00-10:30": 1, "10:30-11:00": 1, "after_11:00": 1}
    assert d["losing_trades"]["count"] == 2 and d["losing_trades"]["pct_never_reached_0_5r"] == 100.0
    a = d["answers"]
    assert a["q4_stop_hit"]["trades"] == 1 and a["q3_signal_exit"]["pct_reached_1r"] == 0.0
    assert a["q6_first_30_min_vs_later"]["09:30-10:00"]["trades"] == 1
    assert "MFE / MAE DIAGNOSTICS" in format_diagnostics(d)


def test_entry_time_buckets_use_new_york_session_time():
    assert entry_time_bucket("2026-06-01T13:30:00+00:00") == "09:30-10:00"   # EDT
    assert entry_time_bucket("2026-06-01T13:59:59+00:00") == "09:30-10:00"
    assert entry_time_bucket("2026-06-01T14:00:00+00:00") == "10:00-10:30"
    assert entry_time_bucket("2026-06-01T15:00:00+00:00") == "after_11:00"
    assert entry_time_bucket("2026-12-01T14:45:00+00:00") == "09:30-10:00"   # EST (UTC-5)
    assert entry_time_bucket(None) is None


# ---------------------------------------------------------------- salida / determinismo
def test_report_json_and_ledger_include_the_new_fields(tmp_path):
    r = run({"AAA": make_bars(giveback_trade_closes())})
    s = summarize(r)
    assert s["excursion"]["overall"]["trades"] == 1
    assert "MFE / MAE DIAGNOSTICS" in format_report(s)
    json.loads(to_json(s))
    write_outputs(r, s, tmp_path)
    header = (tmp_path / "trades.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    for col in ("mfe_r", "mae_r", "mfe_timestamp", "minutes_to_mae", "reached_1_0r", "exit_efficiency", "excursion_class"):
        assert col in header
    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert "answers" in saved["excursion"]
    [ledger] = json.loads((tmp_path / "trades.json").read_text(encoding="utf-8"))
    assert ledger["reached_0_5r"] is False and ledger["mae_before_mfe"] is False  # booleanos JSON reales, no "False"
    assert isinstance(ledger["mfe_r"], float)


def test_diagnostics_are_deterministic():
    bars = {"AAA": make_bars(BASE + [101.5, 103.0, 103.0, 101.0] + decline(101.0, 5)),
            "BBB": make_bars(giveback_trade_closes())}
    a = to_json(summarize(run(bars, symbols=["AAA", "BBB"]))["excursion"])
    b = to_json(summarize(run(bars, symbols=["AAA", "BBB"]))["excursion"])
    assert a == b
