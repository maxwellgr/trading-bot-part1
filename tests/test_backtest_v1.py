"""
Backtester de portafolio v1: datos, ausencia de look-ahead, broker simulado,
portafolio compartido, reglas de producción (riesgo/halts), métricas,
reproducibilidad, CLI y validación contra sesiones grabadas.

Sin red: requests y BrokerAlpaca bloqueados en todo el módulo.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import backtest as backtest_cli
from src import broker_alpaca, historical_download, run_paper
from src.backtest_engine import BacktestConfig, BacktestEngine, production_args, run_backtest
from src.backtest_report import daily_results, drawdown_series, format_report, summarize, to_json, trade_metrics, write_outputs
from src.backtest_validation import validate_session
from src.historical_data import HistoricalDataError, load_universe, validate_bars
from src.sim_broker import SimBroker
from src.strategy import MACrossover

NY = "America/New_York"
T0 = pd.Timestamp("2026-06-01 09:30", tz=NY).tz_convert("UTC")  # lunes, apertura


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("el backtest no debe usar la red ni el broker real")
    for fn in ("get", "post", "delete", "patch", "put"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)
    monkeypatch.setattr(broker_alpaca.BrokerAlpaca, "__init__", boom)


# ---------------------------------------------------------------- helpers
def make_bars(closes, opens=None, lows=None, highs=None, vol=10_000, start=T0, spread=0.3):
    n = len(closes)
    opens = list(opens) if opens is not None else [closes[0]] + list(closes[:-1])
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=i) for i in range(n)])
    hi = [max(o, c) + spread for o, c in zip(opens, closes)]
    lo = [min(o, c) - spread for o, c in zip(opens, closes)]
    for k, v in (lows or {}).items():
        lo[k] = v
    for k, v in (highs or {}).items():
        hi[k] = v
    return pd.DataFrame({"open": opens, "high": hi, "low": lo, "close": list(closes), "volume": float(vol)}, index=idx)


def decline(p, n, step=0.02):
    return [round(p - step * (i + 1), 6) for i in range(n)]


def base_closes():
    """25 velas bajando (MA3<MA7), vela 25 salta +1 (cruce alcista -> BUY)."""
    c = decline(101.0, 25)
    x = c[-1]
    return c + [x + 1.0], x


def run(bars, symbols=None, overrides=None, **cfg):
    symbols = symbols or list(bars)
    config = BacktestConfig(symbols=symbols, record_evaluations=True, **cfg)
    return run_backtest(config, bars, production_args(overrides))


def ts(i, start=T0):
    return (start + pd.Timedelta(minutes=i)).isoformat()


def csv_frame(df, symbol):
    out = df.reset_index().rename(columns={"index": "timestamp"})
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["symbol"] = symbol
    return out


def write_csv(tmp_path, symbol, df, timeframe="1Min"):
    d = tmp_path / timeframe
    d.mkdir(parents=True, exist_ok=True)
    csv_frame(df, symbol).to_csv(d / f"{symbol}.csv", index=False)


# ---------------------------------------------------------------- datos
def test_ascending_bars_are_accepted_and_indexed_utc(tmp_path):
    closes, _ = base_closes()
    write_csv(tmp_path, "AAA", make_bars(closes))
    data = load_universe(tmp_path, "1Min", ["AAA"], T0 - pd.Timedelta(days=1), T0 + pd.Timedelta(days=1))
    df = data.bars["AAA"]
    assert len(df) == len(closes) and str(df.index.tz) == "UTC" and df.index.is_monotonic_increasing


def _raw(rows):
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "symbol"])


def test_duplicate_timestamp_is_rejected():
    rows = [("2026-06-01T13:30:00Z", 1, 1, 1, 1, 10, "AAA"), ("2026-06-01T13:30:00Z", 1, 1, 1, 1, 10, "AAA")]
    with pytest.raises(HistoricalDataError, match="duplicado"):
        validate_bars(_raw(rows), "AAA")


def test_timestamp_regression_is_rejected():
    rows = [("2026-06-01T13:31:00Z", 1, 1, 1, 1, 10, "AAA"), ("2026-06-01T13:30:00Z", 1, 1, 1, 1, 10, "AAA")]
    with pytest.raises(HistoricalDataError, match="fuera de orden"):
        validate_bars(_raw(rows), "AAA")


def test_naive_timestamps_and_malformed_rows_are_rejected():
    with pytest.raises(HistoricalDataError, match="sin zona"):
        validate_bars(_raw([("2026-06-01T13:30:00", 1, 1, 1, 1, 10, "AAA")]), "AAA")
    with pytest.raises(HistoricalDataError, match="incoherente"):
        validate_bars(_raw([("2026-06-01T13:30:00Z", 1, 0.5, 0.4, 1, 10, "AAA")]), "AAA")
    with pytest.raises(HistoricalDataError, match="no numérico"):
        validate_bars(_raw([("2026-06-01T13:30:00Z", "x", 1, 1, 1, 10, "AAA")]), "AAA")
    with pytest.raises(HistoricalDataError, match="symbol"):
        validate_bars(_raw([("2026-06-01T13:30:00Z", 1, 1, 1, 1, 10, "BBB")]), "AAA")


def test_missing_symbol_data_fails_clearly_and_empty_range_warns(tmp_path):
    closes, _ = base_closes()
    write_csv(tmp_path, "AAA", make_bars(closes))
    with pytest.raises(HistoricalDataError) as e:
        load_universe(tmp_path, "1Min", ["AAA", "ZZZ"], T0, T0 + pd.Timedelta(days=1))
    assert "ZZZ" in str(e.value) and "historical_download" in str(e.value)
    write_csv(tmp_path, "BBB", make_bars(closes, start=T0 + pd.Timedelta(days=30)))
    data = load_universe(tmp_path, "1Min", ["AAA", "BBB"], T0 - pd.Timedelta(days=1), T0 + pd.Timedelta(days=1))
    assert list(data.bars) == ["AAA"] and "BBB" in data.warnings[0]


def test_downloader_paginates_with_get_only_and_merges_cache_without_duplicates(tmp_path):
    calls = []

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    pages = [{"bars": [{"t": "2026-06-01T13:30:00Z", "o": 1, "h": 1.1, "l": 0.9, "c": 1, "v": 5}], "next_page_token": "p2"},
             {"bars": [{"t": "2026-06-01T13:31:00Z", "o": 1, "h": 1.1, "l": 0.9, "c": 1.05, "v": 6}], "next_page_token": None}]

    def fake_get(url, headers, params, timeout):
        calls.append(dict(params))
        return Resp(pages[len(calls) - 1])

    s, e = historical_download.date_range_utc("2026-06-01", "2026-06-01")
    bars = historical_download.fetch_bars("AAA", "1Min", s, e, fake_get, "https://data.example/v2", {})
    assert len(bars) == 2 and calls[1]["page_token"] == "p2" and calls[0]["feed"] == "iex"
    path = tmp_path / "1Min" / "AAA.csv"
    frame = historical_download.bars_to_frame("AAA", bars)
    assert historical_download.merge_into_cache(path, "AAA", frame) == 2
    assert historical_download.merge_into_cache(path, "AAA", frame) == 2  # re-descarga: sin duplicados
    assert len(pd.read_csv(path)) == 2


# ---------------------------------------------------------------- sin look-ahead
def test_signal_on_bar_n_fills_at_bar_n_plus_1_open_with_slippage():
    closes, x = base_closes()
    closes = closes + [x + 1.0] * 3
    opens = [closes[0]] + closes[:-1]
    opens[26] = 101.9  # apertura distinta al cierre de la vela de la señal
    r = run({"AAA": make_bars(closes, opens=opens)})
    risk = r.risk_evaluations[0]
    assert risk["bar_timestamp"] == ts(25) and risk["decision"] == "ACCEPT"
    entry = r.fills[0]
    assert entry["signal_bar_ts"] == ts(25) and entry["fill_ts"] == ts(26)
    assert entry["reference_open"] == 101.9
    assert entry["price"] == pytest.approx(101.9 * 1.0005)
    assert entry["delay_seconds"] == 0.0  # la vela N+1 abre justo cuando cierra N


def test_decisions_only_see_bars_up_to_n():
    closes, x = base_closes()
    df = make_bars(closes + [x + 1.0, x - 0.5, x - 0.5, x - 0.5])
    engine = BacktestEngine(BacktestConfig(symbols=["AAA"]), {"AAA": df}, production_args())
    seen = []
    real = engine.strategy

    class Spy:
        def evaluate(self, window):
            t = window.index[-1]
            # la ventana termina en la vela que se decide y ningún fill proviene de una vela posterior
            assert all(pd.Timestamp(f.fill_ts) <= t for f in engine.sim.fills)
            assert window.index.is_monotonic_increasing and (window.index <= t).all()
            seen.append(t)
            return real.evaluate(window)
    engine.strategy = Spy()
    result = engine.run()
    assert seen and seen == list(df.index[df.index.get_loc(seen[0]):])  # cada vela una vez, en orden
    assert len(result.fills) == 2  # hubo entrada y salida durante la corrida vigilada


def test_future_bars_cannot_change_past_decisions():
    closes, x = base_closes()
    tail = [x + 1.0, x + 2.6, x + 2.6, x + 0.5, x - 1.0] + decline(x - 1.0, 12) + [x + 0.5, x - 2.0, x - 2.0]
    full = make_bars(closes + tail)
    cut = 30
    r_full = run({"AAA": full})
    r_cut = run({"AAA": full.iloc[:cut + 1]})
    cutoff = full.index[cut].isoformat()
    assert r_cut.evaluations == [e for e in r_full.evaluations if e["bar_timestamp"] <= cutoff]
    assert r_cut.risk_evaluations == [e for e in r_full.risk_evaluations if e["bar_timestamp"] <= cutoff]
    orders_cut = [(u["purpose"], u["signal_bar_timestamp"]) for u in r_cut.unfilled_orders] + \
                 [(f["purpose"], f["signal_bar_ts"]) for f in r_cut.fills]
    orders_full = [(f["purpose"], f["signal_bar_ts"]) for f in r_full.fills if f["signal_bar_ts"] <= cutoff] + \
                  [(u["purpose"], u["signal_bar_timestamp"]) for u in r_full.unfilled_orders if u["signal_bar_timestamp"] <= cutoff]
    assert sorted(orders_cut) == sorted(orders_full)


def test_stop_uses_bar_close_not_intrabar_low():
    closes, x = base_closes()
    # vela 26: low perfora el stop pero cierra arriba -> sin salida; vela 27 cierra bajo el stop -> salida en la 28
    closes = closes + [x + 1.0, x - 0.5, x - 0.5, x - 0.5]
    r = run({"AAA": make_bars(closes, lows={26: x - 1.0})})
    exits = [f for f in r.fills if f["side"] == "sell"]
    assert len(exits) == 1
    assert exits[0]["purpose"] == "stop_hit" and exits[0]["signal_bar_ts"] == ts(27) and exits[0]["fill_ts"] == ts(28)
    assert exits[0]["price"] == pytest.approx((x - 0.5) * (1 - 0.0005))


# ---------------------------------------------------------------- broker simulado
def _delay(a, b):
    return 0.0


def test_sim_broker_entry_scale_out_full_exit_and_weighted_pnl():
    b = SimBroker(10_000, slippage_bps=0, commission=0)
    b.submit("AAA", "buy", 10, "entry", "t0", "t0")
    [f] = b.fill_pending("AAA", "t1", 100.0, _delay)
    assert f.price == 100.0 and b.position_qty("AAA") == 10 and b.cash == 9_000
    b.submit("AAA", "sell", 4, "scale_out", "t1", "t1")
    [f] = b.fill_pending("AAA", "t2", 110.0, _delay)
    assert f.realized_pnl == pytest.approx(40.0) and b.position_qty("AAA") == 6 and b.cost_basis("AAA") == 100.0
    b.submit("AAA", "sell", 6, "stop_hit", "t2", "t2")
    [f] = b.fill_pending("AAA", "t3", 95.0, _delay)
    assert f.realized_pnl == pytest.approx(-30.0) and b.position_qty("AAA") == 0
    assert b.cash == pytest.approx(10_000 + 40 - 30) and b.equity() == b.cash


def test_sim_broker_slippage_direction_and_commission():
    b = SimBroker(10_000, slippage_bps=10, commission=1.5)
    b.submit("AAA", "buy", 10, "entry", "t0", "t0")
    [buy] = b.fill_pending("AAA", "t1", 100.0, _delay)
    assert buy.price == pytest.approx(100.1) and buy.realized_pnl == -1.5
    b.submit("AAA", "sell", 10, "signal_exit", "t1", "t1")
    [sell] = b.fill_pending("AAA", "t2", 100.0, _delay)
    assert sell.price == pytest.approx(99.9)
    assert sell.realized_pnl == pytest.approx((99.9 - 100.1) * 10 - 1.5)
    assert b.cash == pytest.approx(10_000 - 100.1 * 10 - 1.5 + 99.9 * 10 - 1.5)
    b.submit("AAA", "sell", 1, "signal_exit", "t", "t")
    with pytest.raises(ValueError, match="shorts no soportados"):
        b.fill_pending("AAA", "t3", 100.0, _delay)  # vender sin posición nunca abre un corto implícito


def test_unrealized_pnl_and_equity_mark_to_last_close():
    b = SimBroker(10_000)
    b.submit("AAA", "buy", 10, "entry", "t0", "t0")
    b.fill_pending("AAA", "t1", 100.0, _delay)
    b.mark("AAA", 104.0)
    assert b.unrealized_pnl() == pytest.approx(40.0) and b.equity() == pytest.approx(10_040.0)


# ---------------------------------------------------------------- portafolio compartido
def test_shared_leverage_and_same_timestamp_ordering_follow_symbol_order():
    closes, x = base_closes()
    df = make_bars(closes + [x + 1.0] * 3)
    for order, winner, loser in ((["AAA", "BBB"], "AAA", "BBB"), (["BBB", "AAA"], "BBB", "AAA")):
        r = run({"AAA": df, "BBB": df.copy()}, symbols=order, overrides={"max_leverage": 0.5})
        dec = {e["symbol"]: e for e in r.risk_evaluations}
        assert dec[winner]["decision"] == "ACCEPT"
        assert dec[loser]["decision"] == "REJECT" and dec[loser]["reason_code"] == "LEVERAGE_EXCEEDED"
        assert [e["symbol"] for e in r.risk_evaluations] == order


def test_equity_is_shared_across_symbols_for_sizing():
    closes, x = base_closes()
    df = make_bars(closes + [x + 1.0] * 3)
    solo = run({"AAA": df}).risk_evaluations[0]
    pair = run({"AAA": df, "BBB": df.copy()}, symbols=["AAA", "BBB"]).risk_evaluations
    assert pair[0]["position_size"] == solo["position_size"]
    assert pair[1]["equity"] == pytest.approx(pair[0]["equity"])  # misma equity compartida en el mismo instante


# ---------------------------------------------------------------- estrategia / riesgo
def test_accepted_entry_produces_complete_trade_ledger():
    closes, x = base_closes()
    closes = closes + [x + 1.0, x - 0.5, x - 0.5, x - 0.5]
    r = run({"AAA": make_bars(closes)})
    [t] = r.trades
    assert t["entry_signal_timestamp"] == ts(25) and t["entry_fill_timestamp"] == ts(26)
    assert t["exit_reason"] == "stop_hit" and t["result"] == "loss" and t["initial_qty"] == t["legs"][0]["qty"]
    assert t["realized_pnl"] == pytest.approx((t["exit_fill_price"] - t["entry_fill_price"]) * t["initial_qty"])
    assert t["realized_r"] == pytest.approx(t["realized_pnl"] / (t["risk_per_share_modeled"] * t["initial_qty"]))
    assert t["holding_seconds"] == 120.0


def test_rr_rejection_with_tiny_atr():
    closes, x = base_closes()
    r = run({"AAA": make_bars(closes, spread=0.0)})
    assert r.risk_evaluations[0]["reason_code"] == "RR_BELOW_MINIMUM" and not r.fills


def test_liquidity_rejection_with_low_volume():
    closes, _ = base_closes()
    r = run({"AAA": make_bars(closes, vol=10)})
    assert r.risk_evaluations[0]["reason_code"] == "LIQUIDITY_BELOW_MINIMUM"


def _loss_cycles(n_cycles, start=101.0):
    c = decline(start, 20)
    for _ in range(n_cycles):
        p = c[-1]
        c += [p + 1.0, p - 0.8] + decline(p - 0.8, 12)
    return c


def test_loss_streak_halt_blocks_new_entries():
    r = run({"AAA": make_bars(_loss_cycles(4))})
    assert [t["result"] for t in r.trades] == ["loss", "loss", "loss"]
    assert r.counters["risk"]["ACCEPT"] == 3
    assert r.counters["circuit_breaker_blocked_entries"] == {"Racha negativa": 1}
    assert r.loss_streak_halt_dates == ["2026-06-01"]


def test_loss_streak_resets_on_next_trading_day():
    day1 = make_bars(_loss_cycles(3))
    closes2, _ = base_closes()
    day2 = make_bars(closes2 + [closes2[-1]] * 3, start=T0 + pd.Timedelta(days=1))
    r = run({"AAA": pd.concat([day1, day2])})
    accepted_days = {e["bar_timestamp"][:10] for e in r.risk_evaluations if e["decision"] == "ACCEPT"}
    assert "2026-06-02" in accepted_days  # start_of_day() reinicia la racha, como el reinicio diario en vivo


def test_daily_profit_halt_uses_fills_blocks_entries_and_keeps_managing():
    closes, x = base_closes()
    # 25 BUY | 26 fill entrada | 27 scale-out (R>=1) | 28 fill scale-out (+halt) | 29 salida decidida
    closes = closes + [x + 1.0, x + 2.5, x + 2.5, x + 0.5]
    closes += decline(x + 0.5, 12)
    closes += [closes[-1] + 1.0, closes[-1] + 1.0, closes[-1] + 1.0]  # nuevo BUY -> bloqueado
    r = run({"AAA": make_bars(closes)}, overrides={"daily_profit_halt": 50.0})
    scale = [f for f in r.fills if f["purpose"] == "scale_out"][0]
    assert scale["signal_bar_ts"] == ts(27) and scale["fill_ts"] == ts(28) and scale["realized_pnl"] > 50
    assert r.daily_profit_halt_dates == ["2026-06-01"]
    exit_after_halt = [f for f in r.fills if f["side"] == "sell" and f["purpose"] != "scale_out"]
    assert exit_after_halt and exit_after_halt[0]["signal_bar_ts"] == ts(29)  # gestión sigue tras el halt
    assert r.counters["daily_profit_halt_blocked_entries"] == 1
    assert r.counters["risk"]["ACCEPT"] == 1  # la entrada bloqueada ni llega al RiskManager
    assert len(r.trades) == 1 and r.trades[0]["scale_outs"] == 1 and len(r.trades[0]["legs"]) == 2


def test_no_decisions_outside_regular_hours():
    closes, _ = base_closes()
    pre = T0 - pd.Timedelta(minutes=60)  # 08:30 NY, premarket
    r = run({"AAA": make_bars(closes, start=pre)})  # última vela 08:55 NY
    assert r.counters["decision_bars"] == 0 and r.counters["warmup_skips"] == 0
    assert not r.risk_evaluations and not r.fills  # el cruce ocurre con el mercado cerrado
    # la misma serie desplazada para que la vela 25 cierre a las 09:30 sí decide
    shifted = run({"AAA": make_bars(closes, start=T0 - pd.Timedelta(minutes=26))})
    assert [e["bar_timestamp"] for e in shifted.risk_evaluations] == [(T0 - pd.Timedelta(minutes=1)).isoformat()]


# ---------------------------------------------------------------- métricas
def _t(pnl, r=None, sym="AAA", res=None):
    return {"symbol": sym, "realized_pnl": pnl, "realized_r": r, "holding_seconds": 60.0,
            "result": res or ("win" if pnl > 0.005 else "loss" if pnl < -0.005 else "breakeven")}


def test_trade_metrics_win_rate_expectancy_profit_factor_streaks_and_r():
    trades = [_t(100, 1.0), _t(-50, -0.5), _t(-50, -0.5), _t(-20, -0.2), _t(0.0, 0.0), _t(200, 2.0)]
    m = trade_metrics(trades)
    assert m["wins"] == 2 and m["losses"] == 3 and m["breakevens"] == 1
    assert m["win_rate"] == pytest.approx(2 / 6)
    assert m["expectancy"] == pytest.approx(180 / 6)
    assert m["profit_factor"] == pytest.approx(300 / 120)
    assert m["max_consecutive_losses"] == 3 and m["max_consecutive_wins"] == 1
    assert m["avg_win"] == 150 and m["avg_loss"] == pytest.approx(-40)
    assert m["total_r"] == pytest.approx(1.8) and m["median_r"] == pytest.approx(-0.1)
    assert trade_metrics([])["trades"] == 0 and trade_metrics([_t(5)])["profit_factor"] == float("inf")


def test_drawdown_series():
    dd = drawdown_series([100, 110, 99, 105, 120], start_equity=100)
    assert dd == pytest.approx([0.0, 0.0, 99 / 110 - 1, 105 / 110 - 1, 0.0])
    assert min(drawdown_series([90, 95], start_equity=100)) == pytest.approx(-0.1)


def test_summary_per_symbol_daily_results_and_outputs(tmp_path):
    closes, x = base_closes()
    df = make_bars(closes + [x + 1.0, x - 0.5, x - 0.5, x - 0.5])
    r = run({"AAA": df, "BBB": make_bars(decline(101.0, 30))}, symbols=["AAA", "BBB"])
    s = summarize(r)
    assert s["per_symbol"]["AAA"]["trades"] == 1 and s["per_symbol"]["BBB"]["trades"] == 0
    assert s["per_symbol"]["AAA"]["realized_pnl"] == pytest.approx(r.trades[0]["realized_pnl"])
    assert s["portfolio"]["ending_equity"] == pytest.approx(r.initial_equity + r.trades[0]["realized_pnl"])
    [day] = daily_results(r)
    assert day["losses"] == 1 and day["realized_pnl"] == pytest.approx(r.trades[0]["realized_pnl"])
    assert "BACKTEST SUMMARY" in format_report(s)
    json.loads(to_json(s))
    names = {p.name for p in write_outputs(r, s, tmp_path)}
    assert names == {"trades.csv", "trades.json", "daily_results.csv", "equity_curve.csv", "summary.json"}


# ---------------------------------------------------------------- reproducibilidad / config
def _random_bars(seed, sym_n=3, n=390):
    rng = np.random.default_rng(seed)
    out = {}
    for k in range(sym_n):
        c = list(100 + np.cumsum(rng.normal(0, 0.25, n)))
        out[f"S{k}"] = make_bars([round(v, 4) for v in c])
    return out


def test_same_data_and_config_give_identical_results():
    a = summarize(run(_random_bars(3)))
    b = summarize(run(_random_bars(3)))
    assert to_json(a) == to_json(b)
    assert a["execution"]["signals"]["BUY"] > 0


def test_baseline_uses_exact_production_defaults():
    args = production_args()
    live = run_paper.build_arg_parser().parse_args([])
    assert vars(args) == vars(live)
    engine = BacktestEngine(BacktestConfig(symbols=["AAA"]), {"AAA": make_bars(decline(101.0, 30))})
    assert engine.risk_cfg == run_paper.build_risk_config(live)
    assert (engine.args.fast, engine.args.slow, engine.args.min_rr, engine.args.daily_profit_halt) == (3, 7, 1.3, 300.0)
    with pytest.raises(ValueError, match="ensemble_mode"):
        production_args({"ensemble_mode": "weighted"})
    with pytest.raises(ValueError, match="allow_shorts"):
        production_args({"allow_shorts": True})


# ---------------------------------------------------------------- CLI
def test_cli_runs_offline_and_writes_artifacts(tmp_path, capsys):
    closes, x = base_closes()
    write_csv(tmp_path / "hist", "AAA", make_bars(closes + [x + 1.0, x - 0.5, x - 0.5, x - 0.5]))
    rc = backtest_cli.main(["--symbols", "AAA", "--start", "2026-06-01", "--end", "2026-06-01",
                            "--data-dir", str(tmp_path / "hist"), "--output-dir", str(tmp_path / "out"), "--json"])
    assert rc == 0
    s = json.loads(capsys.readouterr().out)
    assert s["trades"]["trades"] == 1 and (tmp_path / "out" / "trades.csv").is_file()


def test_cli_missing_data_exits_2_with_clear_message(tmp_path, capsys):
    rc = backtest_cli.main(["--symbols", "NOPE", "--start", "2026-06-01", "--end", "2026-06-01", "--data-dir", str(tmp_path)])
    assert rc == 2 and "NOPE" in capsys.readouterr().err


# ---------------------------------------------------------------- validación contra sesión grabada
def test_validate_session_compares_signals_risk_and_lifecycle(tmp_path):
    closes, x = base_closes()
    df = make_bars(closes + [x + 1.0, x - 0.5, x - 0.5, x - 0.5])
    write_csv(tmp_path / "hist", "AAA", df)
    args = production_args()
    cfg = dict(vars(args), symbols="AAA", symbols_parsed=["AAA"], starting_equity=100_000.0)
    start = df.index[21] + pd.Timedelta(seconds=65)
    lines = [{"event_type": "session_start", "timestamp": start.isoformat(), "session_id": "val", "config": cfg}]
    strat = MACrossover(3, 7)
    for i in range(22, 29):
        close = float(df["close"].iloc[i]) + (0.37 if i == 23 else 0.0)  # vela 23: la vista en vivo difería
        sig = strat.evaluate(df.iloc[:i + 1]).signal
        lines.append({"event_type": "strategy_evaluation", "timestamp": (df.index[i] + pd.Timedelta(seconds=70)).isoformat(),
                      "symbol": "AAA", "bar_timestamp": df.index[i].isoformat(), "bar_close": close, "signal": sig})
    lines.append({"event_type": "risk_evaluation", "timestamp": ts(26), "symbol": "AAA", "bar_timestamp": ts(25),
                  "decision": "ACCEPT", "reason_code": "ACCEPTED"})
    lines.append({"event_type": "position_management", "timestamp": ts(28), "symbol": "AAA", "action": "exit",
                  "exit_kind": "stop_hit", "bar_timestamp": ts(27)})
    lines.append({"event_type": "session_end", "timestamp": (df.index[28] + pd.Timedelta(seconds=70)).isoformat()})
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    v = validate_session(path, tmp_path / "hist")
    b = v["bars"]
    assert b["live_unique_bars"] == 7 and b["matched"] == 6
    assert [m["bar_timestamp"] for m in b["close_mismatches"]] == [ts(23)]
    assert v["impossible_fills"] == 0
    assert ("AAA", ts(25), "entry") in v["lifecycle"]["matched"]
    assert ("AAA", ts(27), "stop_hit") in v["lifecycle"]["matched"]
    assert v["risk"][0]["decision_match"] is True
    assert isinstance(v["live_fill_comparison"], str)
