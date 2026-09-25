"""
STRATEGY_V2_HYPOTHESIS_002 (H001 + Failure-to-Progress). Pruebas del plan §11 del spec congelado
(commit 6b0ac23): conteo de 3 cierres post-fill, bordes del disparo, ejecución, sin highs intrabarra,
sin look-ahead, sesiones/overnight/feriados, checkpoint de las 16:00, frontera de día heredada,
salida pendiente en vuelo, scale-out, precedencia, referencia al fill, contabilidad, determinismo,
invariancia de H001 con FTP apagado, motor compartido intacto, reportes e higiene. Sin red.
"""
import inspect
import json

import numpy as np
import pandas as pd
import pytest

from src import backtest_engine, broker_alpaca
from src import strategy_v2_h002 as h2
from src.backtest_engine import BacktestConfig, BacktestEngine, production_args
from src.backtest_report import daily_results, summarize
from src.research_protocol import load_protocol, validate_protocol
from src.strategy import StrategyResult
from src.strategy_v2_h001 import HygieneError, TrendPullbackH001

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


class Buy:
    """Estrategia de prueba: BUY en timestamps dados (aísla la gestión de la lógica de entrada)."""
    min_bars = 1

    def __init__(self, when):
        self.when = set(when)

    def evaluate(self, df):
        return StrategyResult("BUY" if df.index[-1] in self.when else None, "test")


def sess(day, n, start="09:30"):
    t0 = pd.Timestamp(f"{day} {start}", tz=NY).tz_convert("UTC")
    return [t0 + pd.Timedelta(minutes=5 * k) for k in range(n)]


def mk(ts, closes, opens=None, highs=None, lows=None):
    opens = list(opens) if opens is not None else [closes[0]] + list(closes[:-1])
    hi = list(highs) if highs is not None else [max(o, c) + 0.5 for o, c in zip(opens, closes)]
    lo = list(lows) if lows is not None else [min(o, c) - 0.5 for o, c in zip(opens, closes)]
    return pd.DataFrame({"open": opens, "high": hi, "low": lo, "close": list(closes), "volume": 1e6},
                        index=pd.DatetimeIndex(ts))


def run(df, signal_ts, ftp=True, engine_cls=h2.FTPEngine, overrides=None, symbols=("AAA",), bars=None, **cfg):
    config = BacktestConfig(symbols=list(symbols), timeframe="5Min", window_hours_limit=False, **cfg)
    args = production_args(dict({"lookback": 50}, **(overrides or {})))
    eng = engine_cls(config, bars or {"AAA": df}, args, Buy(signal_ts), ftp_enabled=ftp)
    return eng, eng.run()


def base(after, day="2024-03-04", opens=None, highs=None):
    """40 velas planas en 100 (rango 1 -> ATR 1, R_ps 2.0); señal en la vela 39; E = vela 40."""
    closes = [100.0] * 40 + list(after)
    ts = sess(day, len(closes))
    return mk(ts, closes, opens=opens, highs=highs), ts


def only_trade(r):
    [t] = r.trades
    return t


# ================================================================ regla pura (bordes exactos)
@pytest.mark.parametrize("max_close,cur,expected", [
    (100.9, 99.0, True),     # MFE < 0.25R y cierre < F
    (100.9, 100.0, True),    # cierre == F dispara
    (101.0, 99.0, False),    # MFE == +0.25R exacto: NO
    (101.5, 99.0, False),    # MFE > 0.25R: NO
    (100.9, 100.01, False),  # cierre > F: NO
])
def test_trigger_boundaries_exact(max_close, cur, expected):
    assert h2.ftp_triggered(max_close, cur, fill=100.0, risk_ps=4.0) is expected


# ================================================================ 1-2 / 3-4 / 8: conteo, disparo, ejecución
def test_exactly_three_post_fill_closes_fill_bar_counts_and_exit_next_open():
    df, ts = base([100.0, 99.9, 99.8, 99.7, 99.7, 99.7])
    eng, r = run(df, [ts[39]])
    t = only_trade(r)
    assert t["entry_fill_timestamp"] == ts[40].isoformat()                       # E = vela 40
    [ev] = eng.ftp_events
    assert ev["checkpoint_bar"] == ts[42].isoformat() and ev["path"] == "decision"   # 3er cierre = E+2
    assert ev["decision_timestamp"] == (ts[42] + pd.Timedelta(minutes=5)).isoformat()
    [leg] = t["legs"]
    assert t["exit_reason"] == h2.FTP_PURPOSE and leg["fill_timestamp"] == ts[43].isoformat()  # E+3 open
    assert leg["price"] == pytest.approx(99.8 * (1 - 5 / 10_000)) and leg["commission"] == 0.0
    assert leg["qty"] == t["initial_qty"] and ev["mfe_r"] < 0.25 and ev["current_close"] <= ev["entry_fill_price"]


def test_no_ftp_after_one_or_two_closes_even_deeply_negative_but_stop_still_fires():
    df, ts = base([100.0, 99.0, 99.2, 99.3, 99.3])         # -0.5R en el 2º cierre, sin FTP antes del 3º
    eng, _ = run(df, [ts[39]])
    assert [e["checkpoint_bar"] for e in eng.ftp_events] == [ts[42].isoformat()]
    df2, ts2 = base([99.5, 96.0, 96.0, 96.0])               # stop en el 2º cierre
    eng2, r2 = run(df2, [ts2[39]])
    assert only_trade(r2)["exit_reason"] == "stop_hit" and eng2.ftp_events == []


def test_close_equal_to_fill_triggers():
    df, ts = base([100.0, 100.0, 100.0, 100.0], opens=[100.0] * 44)
    eng, r = run(df, [ts[39]], slippage_bps=0.0)             # F = open exacto = 100.0
    ev = eng.ftp_events[0]
    assert ev["entry_fill_price"] == 100.0 and ev["current_close"] == 100.0 and ev["triggered"]
    assert only_trade(r)["exit_reason"] == h2.FTP_PURPOSE


def test_mfe_at_or_above_quarter_r_does_not_trigger_in_engine():
    # R_ps = 2.0 exacto; F = 100.0 (slippage 0) -> cierre 100.5 = +0.25R exacto
    for peak in (100.5, 100.7):
        df, ts = base([peak, 99.9, 99.9, 99.9, 99.9], opens=[100.0] * 41 + [peak, 99.9, 99.9, 99.9])
        eng, r = run(df, [ts[39]], slippage_bps=0.0, overrides={"max_giveback_pct": 0.5})
        ev = eng.ftp_events[0] if eng.ftp_events else None
        assert ev is None or ev["triggered"] is False
        assert all(l["purpose"] != h2.FTP_PURPOSE for t in r.trades for l in t["legs"])


def test_close_above_fill_no_trigger_and_never_retested():
    df, ts = base([100.1, 100.1, 100.1, 99.0, 98.9, 98.8, 98.7])
    eng, r = run(df, [ts[39]])
    assert len(eng.ftp_events) == 1 and eng.ftp_events[0]["condition_met"] is False
    assert all(l["purpose"] != h2.FTP_PURPOSE for t in r.trades for l in t["legs"])


# ================================================================ 9-10: sin highs intrabarra, pre-fill ignorado, sin look-ahead
def test_intrabar_high_and_prefill_prices_are_ignored():
    after = [100.0, 99.9, 99.8, 99.7, 99.7]
    closes = [100.0] * 40 + after
    ts = sess("2024-03-04", len(closes))
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes)]
    highs[40] = 101.5                                      # high de E: +0.72R
    highs[39] = 103.0                                      # vela de señal (pre-fill)
    eng, r = run(mk(ts, closes, opens, highs), [ts[39]])
    ev = eng.ftp_events[0]
    assert ev["max_close"] == 100.0 and ev["triggered"] and only_trade(r)["exit_reason"] == h2.FTP_PURPOSE


def test_no_look_ahead_bars_after_checkpoint_do_not_change_decision():
    df, ts = base([100.0, 99.9, 99.8, 99.7, 99.7, 99.7])
    a, _ = run(df, [ts[39]])
    df2 = df.copy()
    df2.iloc[43:, :4] = df2.iloc[43:, :4] + 50.0
    b, _ = run(df2, [ts[39]])
    assert a.ftp_events == b.ftp_events


# ================================================================ 11-13: sesiones, fines de semana, 16:00, cierre anticipado
def _two_days(day1, day2, n1, closes_tail_day1, closes_day2, n2=10):
    """Día 1 completo hasta 15:55 (78 velas) terminando con closes_tail_day1; día 2 con closes_day2."""
    ts1 = sess(day1, 78)
    c1 = [100.0] * (78 - len(closes_tail_day1)) + list(closes_tail_day1)
    ts2 = sess(day2, len(closes_day2))
    return mk(ts1 + ts2, c1 + list(closes_day2)), ts1, ts2


def test_near_close_entry_counts_into_next_session_without_forced_exit():
    # señal 15:45 (idx 75) -> E = 15:50 (idx 76): cierres 15:50, 15:55, día2 09:30 -> checkpoint 09:35
    df, ts1, ts2 = _two_days("2024-03-05", "2024-03-06", 78, [100.0, 100.0, 99.9, 99.9], [99.8, 99.7, 99.7, 99.7])
    eng, r = run(df, [ts1[75]])
    t = only_trade(r)
    assert t["entry_fill_timestamp"] == ts1[76].isoformat()
    [ev] = eng.ftp_events
    assert ev["checkpoint_bar"] == ts2[0].isoformat() and ev["path"] == "decision" and ev["triggered"]
    assert t["legs"][0]["fill_timestamp"] == ts2[1].isoformat()     # 09:35 open del día 2 (sin salida forzada día 1)


def test_weekend_and_missing_bars_are_skipped_chronologically():
    # viernes 2024-03-08: E = 15:50; lunes 2024-03-11 sin la vela 09:30 -> 3er cierre = 09:35
    ts1 = sess("2024-03-08", 78)
    ts2 = sess("2024-03-11", 6)[1:]
    c1 = [100.0] * 76 + [100.0, 99.9]
    df = mk(ts1 + ts2, c1 + [99.8, 99.7, 99.7, 99.7, 99.7])
    eng, r = run(df, [ts1[75]])
    [ev] = eng.ftp_events
    assert ev["checkpoint_bar"] == ts2[0].isoformat()               # 09:35 del lunes
    assert only_trade(r)["legs"][0]["fill_timestamp"] == ts2[1].isoformat()


def test_1600_checkpoint_uses_exactly_three_closes_and_pending_exit_fills_next_open():
    # señal 15:40 (idx 74) -> E = 15:45: cierres 15:45, 15:50, 15:55 (3º, conocido a las 16:00)
    df, ts1, ts2 = _two_days("2024-03-05", "2024-03-06", 78, [100.0, 100.0, 99.9, 99.8, 99.7], [99.0, 99.0, 99.0])
    eng, r = run(df, [ts1[74]])
    t = only_trade(r)
    [ev] = eng.ftp_events
    assert ev["path"] == "close_without_decision" and ev["checkpoint_bar"] == ts1[77].isoformat()
    assert ev["decision_timestamp"].endswith("21:00:00+00:00") and ev["current_close"] == 99.7 and ev["triggered"]
    leg = t["legs"][0]
    assert leg["purpose"] == h2.FTP_PURPOSE and leg["fill_timestamp"] == ts2[0].isoformat()   # 09:30 del día 2
    assert leg["reference_open"] == 99.7 and leg["price"] == pytest.approx(99.7 * (1 - 5 / 10_000))  # open 09:30 = cierre previo
    # los cierres posteriores no cambian la decisión
    df2 = df.copy()
    df2.loc[df2.index >= ts2[0], ["open", "high", "low", "close"]] += 30
    eng2, _ = run(df2, [ts1[74]])
    assert eng2.ftp_events == eng.ftp_events


def test_1600_non_trigger_is_never_retested_and_weekend_pending_fills_monday():
    df, ts1, ts2 = _two_days("2024-03-05", "2024-03-06", 78, [100.0, 100.0, 100.3, 100.3, 100.3], [99.0, 98.9, 98.8, 98.7])
    eng, r = run(df, [ts1[74]])
    assert len(eng.ftp_events) == 1 and eng.ftp_events[0]["triggered"] is False
    assert all(l["purpose"] != h2.FTP_PURPOSE for t in r.trades for l in t["legs"])
    dfw, t1, t2 = _two_days("2024-03-08", "2024-03-11", 78, [100.0, 100.0, 99.9, 99.8, 99.7], [99.0, 99.0])
    engw, rw = run(dfw, [t1[74]])
    assert only_trade(rw)["legs"][0]["fill_timestamp"] == t2[0].isoformat()   # lunes 09:30


def test_early_close_checkpoint_follows_normal_path_and_is_overnight():
    ts1 = sess("2024-07-03", 42)                          # 09:30 .. 12:55 (cierre anticipado)
    ts2 = sess("2024-07-05", 3)                           # 07-04 feriado
    c1 = [100.0] * 39 + [100.0, 99.9, 99.8]               # señal 12:40 (idx 38) -> E = 12:45
    df = mk(ts1 + ts2, c1 + [99.5, 99.5, 99.5])
    eng, r = run(df, [ts1[38]])
    [ev] = eng.ftp_events
    assert ev["path"] == "decision" and ev["decision_timestamp"] == (ts1[41] + pd.Timedelta(minutes=5)).isoformat()
    t = only_trade(r)
    assert t["legs"][0]["fill_timestamp"] == ts2[0].isoformat()
    m = h2.overnight_ftp_metrics(r.trades, eng.ftp_events)
    assert m["overnight_ftp_fills"] == 1 and m["by_checkpoint_path"] == {"early_close": 1}


# ================================================================ 13b: frontera de día heredada (pruebas explícitas)
class Probe(h2.FTPEngine):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rolls = []

    def _roll_day(self, day):
        before = {"day": str(day), "losses_before": self.risk.consecutive_losses,
                  "pnl_before": self.session.get("pnl_today", 0.0), "equity": self.sim.equity()}
        new = day != self.current_day
        super()._roll_day(day)
        if new:
            before.update(losses_after=self.risk.consecutive_losses, pnl_after=self.session["pnl_today"],
                          halted_after=self.session["halted"], day_start_equity=self.risk.day_start_equity)
            self.rolls.append(before)


def _check_day_boundary(eng, r, fill_day):
    t = only_trade(r)
    leg = t["legs"][-1]
    assert str(pd.Timestamp(leg["fill_timestamp"]).tz_convert(NY).date()) == fill_day
    assert t["realized_pnl"] < 0                                                      # pérdida overnight
    # 1) ledger / equity / total
    assert r.final_equity == pytest.approx(r.initial_equity + t["realized_pnl"])
    assert summarize(r)["portfolio"]["realized_pnl_closed_trades"] == pytest.approx(t["realized_pnl"])
    # 2) daily_results en la fecha del fill
    rows = {d["date"]: d for d in daily_results(r)}
    assert rows[fill_day]["realized_pnl"] == pytest.approx(leg["realized_pnl"])
    roll = next(x for x in eng.rolls if x["day"] == fill_day)
    # 3) la pérdida se aplicó ANTES del reset y no pasa al nuevo día
    assert roll["losses_before"] == 1 and roll["losses_after"] == 0
    # 4) el equity inicial del nuevo día ya incluye la pérdida (no cuenta para su límite diario)
    assert roll["day_start_equity"] == pytest.approx(r.initial_equity + t["realized_pnl"])
    # 5) P&L diario / halt de ganancia del nuevo día arranca en 0 y excluye el fill
    assert roll["pnl_before"] == pytest.approx(leg["realized_pnl"]) and roll["pnl_after"] == 0.0 and roll["halted_after"] is False


def test_day_boundary_overnight_ftp_loss_is_booked_before_reset():
    df, ts1, ts2 = _two_days("2024-03-05", "2024-03-06", 78, [100.0, 100.0, 99.9, 99.8, 99.7], [99.0, 99.0, 99.0])
    eng, r = run(df, [ts1[74]], engine_cls=Probe)
    assert only_trade(r)["exit_reason"] == h2.FTP_PURPOSE
    _check_day_boundary(eng, r, "2024-03-06")


def test_day_boundary_is_the_engines_rule_for_any_fill_before_first_decision():
    # 6) H001 (FTP apagado): stop decidido a las 13:00 de un cierre anticipado, fill al día hábil siguiente 09:30
    ts1 = sess("2024-07-03", 42)
    ts2 = sess("2024-07-05", 3)
    c1 = [100.0] * 39 + [100.0, 99.9, 96.0]
    df = mk(ts1 + ts2, c1 + [96.0, 96.0, 96.0])
    eng, r = run(df, [ts1[38]], ftp=False, engine_cls=Probe)
    assert only_trade(r)["exit_reason"] == "stop_hit" and eng.ftp_events == []
    _check_day_boundary(eng, r, "2024-07-05")


def test_overnight_metrics_count_wins_losses_breakevens_and_paths():
    def trade(tid, pnl, dec, fill):
        return {"trade_id": tid, "legs": [{"purpose": h2.FTP_PURPOSE, "decision_timestamp": dec, "fill_timestamp": fill,
                                           "realized_pnl": pnl}]}
    trades = [trade(1, -10.0, "2024-03-05T21:00:00+00:00", "2024-03-06T14:30:00+00:00"),      # 16:00 -> 09:30
              trade(2, 5.0, "2024-07-03T17:00:00+00:00", "2024-07-05T13:30:00+00:00"),        # cierre anticipado
              trade(3, 0.001, "2024-12-23T15:25:00+00:00", "2024-12-24T14:30:00+00:00"),      # hueco
              trade(4, -3.0, "2024-03-05T18:00:00+00:00", "2024-03-05T18:05:00+00:00"),       # mismo día: no
              {"trade_id": 5, "legs": [{"purpose": "stop_hit", "decision_timestamp": "2024-03-05T21:00:00+00:00",
                                        "fill_timestamp": "2024-03-06T14:30:00+00:00", "realized_pnl": -1.0}]}]
    events = [{"trade_id": 1, "triggered": True, "path": "close_without_decision"},
              {"trade_id": 2, "triggered": True, "path": "decision"}, {"trade_id": 3, "triggered": True, "path": "decision"}]
    m = h2.overnight_ftp_metrics(trades, events)
    assert (m["overnight_ftp_fills"], m["overnight_ftp_losses"], m["overnight_ftp_wins"], m["overnight_ftp_breakevens"]) == (3, 1, 1, 1)
    assert m["overnight_ftp_realized_pnl"] == pytest.approx(-4.999)
    assert m["by_checkpoint_path"] == {"16:00": 1, "early_close": 1, "gap_or_other": 1}


# ================================================================ 13c: salida pendiente en vuelo
class PendingProbe(h2.FTPEngine):
    """Inyecta una venta pendiente justo antes del checkpoint (ruta A o B) para ejercitar la regla defensiva."""
    target = None

    def _manage(self, sd, i, sig, price, bd):
        if sd.iso[i] == self.target:
            self.sim.submit(sd.name, "sell", 1, "test_pending", sd.iso[i], sd.decision_iso[i])
        super()._manage(sd, i, sig, price, bd)

    def _observe_excursion(self, sd, i):
        if sd.iso[i] == self.target and not sd.decision_ok[i]:
            self.sim.submit(sd.name, "sell", 1, "test_pending", sd.iso[i], sd.decision_iso[i])
        super()._observe_excursion(sd, i)


def test_pending_exit_in_flight_consumes_ftp_check_on_both_paths():
    df, ts = base([100.0, 99.9, 99.8, 99.7, 99.7, 99.7])
    PendingProbe.target = ts[42].isoformat()
    eng, r = run(df, [ts[39]], engine_cls=PendingProbe)
    [ev] = eng.ftp_events
    assert ev["condition_met"] and ev["blocked_by"] == "pending_exit_in_flight" and not ev["triggered"]
    df2, ts1, _ = _two_days("2024-03-05", "2024-03-06", 78, [100.0, 100.0, 99.9, 99.8, 99.7], [99.0, 99.0, 99.0])
    PendingProbe.target = ts1[77].isoformat()
    eng2, _ = run(df2, [ts1[74]], engine_cls=PendingProbe)
    [ev2] = eng2.ftp_events
    assert ev2["path"] == "close_without_decision" and ev2["blocked_by"] == "pending_exit_in_flight"


# ================================================================ 14-16: scale-out, precedencia, referencia al fill
def test_scaled_out_trade_cannot_trigger_ftp():
    df, ts = base([100.0, 102.3, 100.2, 100.1, 100.1])      # scale-out en E+1 (r_now >= 1 desde la entrada modelada)
    eng, r = run(df, [ts[39]])
    t = r.trades[0] if r.trades else r.open_positions[0]["trade"]
    assert t["legs"][0]["purpose"] == "scale_out"
    assert all(not e["triggered"] for e in eng.ftp_events)
    assert all(l["purpose"] != h2.FTP_PURPOSE for l in t["legs"])


def test_ftp_after_scale_out_uses_original_f_and_r_and_only_remaining_shares():
    # Caso de hueco (fill > 0.75 R_ps sobre la entrada modelada), giveback apagado SOLO en esta prueba
    after = [102.3, 102.1, 102.0, 102.0, 102.0]
    closes = [100.0] * 40 + after
    ts = sess("2024-03-04", len(closes))
    opens = [100.0] * 40 + [102.0, 102.3, 102.1, 102.0, 102.0]
    eng, r = run(mk(ts, closes, opens), [ts[39]], overrides={"max_giveback_pct": 0.0})
    t = r.trades[0]
    ev = next(e for e in eng.ftp_events if e["triggered"])
    assert ev["entry_fill_price"] == t["entry_fill_price"] == pytest.approx(102.0 * 1.0005)
    assert ev["risk_per_share"] == t["risk_per_share_modeled"]
    assert ev["max_close"] == 102.3                          # MFE desde la entrada original (incluye E)
    scale = [l for l in t["legs"] if l["purpose"] == "scale_out"]
    ftp = [l for l in t["legs"] if l["purpose"] == h2.FTP_PURPOSE]
    assert scale and ftp and ftp[0]["qty"] == t["initial_qty"] - sum(l["qty"] for l in scale) == ev["qty"]
    assert h2.gap_trades(r.trades) == 1


def test_existing_stop_at_checkpoint_takes_precedence():
    df, ts = base([99.9, 99.0, 96.0, 96.0, 96.0])
    eng, r = run(df, [ts[39]])
    [ev] = eng.ftp_events
    assert ev["condition_met"] and ev["blocked_by"] == "existing_exit_same_decision" and not ev["triggered"]
    assert only_trade(r)["exit_reason"] == "stop_hit"


def test_existing_giveback_at_checkpoint_takes_precedence():
    # F > entrada modelada (apertura con hueco chico): giveback (desde la modelada) y FTP coinciden en E+2
    closes = [100.0] * 40 + [100.25, 100.2, 100.1, 100.1]
    opens = [100.0] * 40 + [100.25, 100.25, 100.2, 100.1]
    ts = sess("2024-03-04", len(closes))
    eng, r = run(mk(ts, closes, opens), [ts[39]])
    [ev] = eng.ftp_events
    assert ev["condition_met"] and ev["blocked_by"] == "existing_exit_same_decision"
    assert only_trade(r)["exit_reason"] == "giveback_close"


def test_position_closed_before_checkpoint_means_no_ftp():
    df, ts = base([99.5, 96.0, 96.0, 96.0])                   # stop decidido en E+1, fill en E+2 open
    eng, r = run(df, [ts[39]])
    assert eng.ftp_events == [] and only_trade(r)["exit_reason"] == "stop_hit"


def test_ftp_reference_is_actual_fill_not_modeled_entry():
    # modelada 100.05; F = 100.5 * 1.0005; cierre E+2 = 100.5: > modelada pero <= F -> dispara
    closes = [100.0] * 40 + [100.4, 100.45, 100.5, 100.5]
    opens = [100.0] * 40 + [100.5, 100.4, 100.45, 100.5]
    ts = sess("2024-03-04", len(closes))
    eng, r = run(mk(ts, closes, opens), [ts[39]])
    t = only_trade(r)
    assert t["modeled_entry"] == 100.05 and 100.5 > t["modeled_entry"] and eng.ftp_events[0]["triggered"]
    assert t["exit_reason"] == h2.FTP_PURPOSE


def test_ftp_loss_feeds_loss_streak_and_daily_pnl():
    df, ts = base([100.0, 99.9, 99.8, 99.7, 99.7, 99.7])
    eng, r = run(df, [ts[39]])
    t = only_trade(r)
    assert t["realized_pnl"] < 0 and eng.risk.consecutive_losses == 1
    assert eng.risk.trades[-1].pnl == pytest.approx(t["realized_pnl"])
    assert eng.session["pnl_today"] == pytest.approx(t["realized_pnl"])


# ================================================================ 18-20: determinismo, invariancia H001, motor intacto
def _synth(seed, sessions, start="2024-03-04"):
    rng = np.random.default_rng(seed)
    rows, idx, px = [], [], 100.0
    for d in pd.bdate_range(start, periods=sessions):
        for k in range(78):
            o = px + rng.normal(0, 0.1)
            c = o + 0.08 + rng.normal(0, 0.35)
            rows.append((o, max(o, c) + abs(rng.normal(0, .15)), min(o, c) - abs(rng.normal(0, .15)), c, 20_000.0))
            idx.append(pd.Timestamp(f"{d.date()} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k))
            px = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))


@pytest.fixture(scope="module")
def synth_runs():
    bars = {"S0": _synth(1, 6), "S1": _synth(2, 6)}
    cfg = BacktestConfig(symbols=["S0", "S1"], timeframe="5Min", window_hours_limit=False, record_evaluations=True,
                         start="2024-03-07", end="2024-03-11")
    args = production_args({"lookback": 278})
    base_ = BacktestEngine(cfg, bars, args, TrendPullbackH001()).run()
    off = h2.FTPEngine(cfg, bars, args, TrendPullbackH001(), ftp_enabled=False)
    on = h2.FTPEngine(cfg, bars, args, TrendPullbackH001(), ftp_enabled=True)
    return base_, off.run(), off, on.run(), on


def test_ftp_off_reproduces_h001_engine_exactly(synth_runs):
    base_, off, off_eng, _, _ = synth_runs
    assert base_.trades and off.trades == base_.trades and off.fills == base_.fills
    assert off.equity_curve == base_.equity_curve and off.counters == base_.counters and off_eng.ftp_events == []


def test_ftp_on_keeps_entry_signals_identical_on_every_bar(synth_runs):
    base_, _, _, on, on_eng = synth_runs
    assert [(e["symbol"], e["bar_timestamp"], e["signal"]) for e in on.evaluations] == \
           [(e["symbol"], e["bar_timestamp"], e["signal"]) for e in base_.evaluations]
    assert len(on_eng.ftp_events) > 0


def test_deterministic(synth_runs):
    bars = {"S0": _synth(1, 6), "S1": _synth(2, 6)}
    cfg = BacktestConfig(symbols=["S0", "S1"], timeframe="5Min", window_hours_limit=False, start="2024-03-07", end="2024-03-11")
    args = production_args({"lookback": 278})
    runs = [h2.FTPEngine(cfg, bars, args, TrendPullbackH001()) for _ in range(2)]
    outs = [e.run() for e in runs]
    assert outs[0].trades == outs[1].trades and runs[0].ftp_events == runs[1].ftp_events


def test_shared_engine_and_live_code_untouched():
    assert "failure_to_progress" not in inspect.getsource(backtest_engine)
    from src import run_paper
    assert "FTPEngine" not in inspect.getsource(run_paper) and "failure_to_progress" not in inspect.getsource(run_paper)
    assert production_args().lookback == 120 and production_args().hours_back == 24


# ================================================================ comparación / puente / resúmenes
def _t(sym, ts, reason, pnl, r, result):
    return {"symbol": sym, "entry_signal_timestamp": ts, "exit_reason": reason, "realized_pnl": pnl,
            "realized_r": r, "result": result, "legs": [], "scale_outs": 0}


def _summ(trades):
    return {"trades": {"trades": len(trades), "win_rate": 0.5, "expectancy_r": 0.0, "profit_factor": 1.0, "total_r": 0.0,
                       "max_consecutive_losses": 1}, "portfolio": {"realized_pnl_closed_trades": sum(t["realized_pnl"] for t in trades),
                                                                   "max_drawdown_pct": -1.0}}


def test_comparison_losses_avoided_winners_sacrificed_and_exact_bridge():
    h1 = [_t("A", "t1", "stop_hit", -100, -1.0, "loss"), _t("A", "t2", "giveback_close", 50, 0.5, "win"),
          _t("B", "t3", "take_profit_hit", 300, 3.0, "win"), _t("B", "t4", "stop_hit", -90, -0.9, "loss")]
    h2t = [_t("A", "t1", h2.FTP_PURPOSE, -20, -0.2, "loss"), _t("A", "t2", h2.FTP_PURPOSE, -5, -0.05, "loss"),
           _t("B", "t3", "take_profit_hit", 300, 3.0, "win"), _t("C", "t9", "giveback_close", 40, 0.4, "win")]
    c = h2.compare_with_h001(h1, _summ(h1), h2t, _summ(h2t))
    assert c["matching"] == {"key": "(symbol, entry_signal_timestamp)", "matched": 3, "only_h001": 1, "only_h002": 1,
                             "matched_closed_by_ftp_in_h002": 2}
    la = c["losses_avoided_h001_stop_hit_closed_by_ftp"]
    assert la["count"] == 1 and la["sum_delta_r"] == pytest.approx(0.8) and la["delta_pnl"] == 80
    ws = c["winners_sacrificed_h001_win_closed_by_ftp"]
    assert ws["count"] == 1 and ws["pnl_given_up"] == 55 and ws["r_given_up"] == pytest.approx(0.55)
    assert ws["h001_exit_reasons"] == {"giveback_close": 1}
    b = c["pnl_bridge"]
    assert b["difference"] == (315 - 160) and b["bridge_exact"] and b["matched_trades_change"] == 25
    assert b["h002_only_trades_pnl"] == 40 and b["h001_only_trades_pnl"] == -90


def test_ftp_summary_counts():
    trades = [dict(_t("A", "t1", h2.FTP_PURPOSE, -20, -0.2, "loss"), legs=[{"purpose": h2.FTP_PURPOSE, "realized_pnl": -20}],
                   entry_fill_price=100.0, modeled_entry=100.0, risk_per_share_modeled=2.0),
              dict(_t("A", "t2", "stop_hit", -50, -1.0, "loss"), legs=[{"purpose": "stop_hit", "realized_pnl": -50}],
                   entry_fill_price=102.0, modeled_entry=100.0, risk_per_share_modeled=2.0)]
    events = [{"triggered": True, "condition_met": True, "blocked_by": None, "path": "decision"},
              {"triggered": False, "condition_met": True, "blocked_by": "existing_exit_same_decision", "path": "decision"}]
    s = h2.ftp_summary(trades, events)
    assert s["triggered"] == 1 and s["ftp_exit_trades"] == 1 and s["ftp_trades_realized_pnl"] == -20
    assert s["blocked"] == {"existing_exit_same_decision": 1} and s["gap_trades_fill_gt_0_75r_above_modeled"] == 1


# ================================================================ 21-23: higiene
def test_hygiene_development_only_validation_and_known_forward_refused(tmp_path, capsys):
    from src import research_h002 as rh
    p = load_protocol()
    split = lambda n: next(s for s in p["splits"] if s["name"] == n)  # noqa: E731
    entry = {"id": h2.HYPOTHESIS_ID, "status": "IMPLEMENTED", "validation_viewed_at": None}
    from src.strategy_v2_h001 import check_split_allowed
    check_split_allowed(entry, split("development"))
    with pytest.raises(HygieneError, match="FROZEN"):
        check_split_allowed(entry, split("validation"), True)
    with pytest.raises(HygieneError, match="compuerta"):
        check_split_allowed(dict(entry, status="FROZEN"), split("validation"), False)
    for n in ("known_diagnostic", "forward"):
        with pytest.raises(HygieneError):
            check_split_allowed(dict(entry, status="FROZEN"), split(n), True)
    for n in ("validation", "known_diagnostic", "forward"):
        assert rh.main(["--split", n, "--output-dir", str(tmp_path / "o")]) == 2
    assert rh.main(["--split", "validation", "--ftp-off-check", "--output-dir", str(tmp_path / "o")]) == 2
    assert not (tmp_path / "o").exists()


# ================================================================ extremo a extremo (mini dataset + H001 guardado)
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


def test_end_to_end_report_ftp_off_check_and_bridge(tmp_path):
    from src import research_h001 as r1
    from src import research_h002 as rh
    for k, sym in enumerate(["AAA", "BBB"]):
        _write_1min(tmp_path / "hist", sym, _synth(40 + k, 10), 50 + k)
    p = load_protocol()
    p["universe"]["symbols"] = ["AAA", "BBB"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": "2024-03-11", "end": "2024-03-13"},
                   {"name": "validation", "role": "validation", "start": "2024-03-14", "end": "2024-03-15"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    p = validate_protocol(p)
    h1dir = tmp_path / "h001"
    r1.build_report(r1.run_split(p, "development", tmp_path / "hist", {"id": "X", "status": "IMPLEMENTED"}), p, h1dir)
    entry = {"id": h2.HYPOTHESIS_ID, "status": "IMPLEMENTED"}
    off = rh.ftp_off_check(rh.run_split(p, "development", tmp_path / "hist", entry, ftp_enabled=False),
                           tmp_path / "off", h1dir)
    assert off["identical"] is True
    rep = rh.build_report(rh.run_split(p, "development", tmp_path / "hist", entry), p, tmp_path / "h002", h1dir)
    assert rep["comparison_vs_h001"]["pnl_bridge"]["bridge_exact"] is True
    assert set(rep["overnight_ftp"]) >= {"overnight_ftp_fills", "overnight_ftp_realized_pnl", "overnight_ftp_losses",
                                         "overnight_ftp_wins"}
    assert rep["gates"]["progression_to_validation"] in ("PASS", "FAIL")
    assert "screening/resubstitution" in rep["evidence"]
    assert "H002" in rh.format_report(rep) or h2.HYPOTHESIS_ID in rh.format_report(rep)
    rep2 = rh.build_report(rh.run_split(p, "development", tmp_path / "hist", entry), p, tmp_path / "h002b", h1dir)
    for f in ("trades.json", "h002_ftp_events.json"):
        assert (tmp_path / "h002" / f).read_bytes() == (tmp_path / "h002b" / f).read_bytes()
