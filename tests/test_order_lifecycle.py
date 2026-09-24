"""
Ciclo de vida de órdenes y P&L realizado (run_paper + order_tracking).

Regla que se verifica: el P&L realizado y el objetivo diario salen SOLO de
fills confirmados (filled_qty / filled_avg_price de Alpaca), nunca del precio
de la vela, del precio modelado con slippage ni del acuse pending_new.

Sin red: broker falso en memoria y requests bloqueado.
"""
import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest

from src import analyze_session as az
from src import broker_alpaca
from src import order_tracking
from src import run_paper
from src import session_summary
from src.order_tracking import OrderTracker, TrackedOrder, parse_alpaca_ts
from src.risk_manager_avanzado import RiskDecision, Side
from src.strategy import StrategyResult
from src.structured_logger import SessionLogger

SUBMITTED_AT = "2026-09-24T13:39:39.724088489Z"


# ---------------------------------------------------------------- fakes
class FakeBroker:
    """Órdenes en memoria. Una orden queda pending_new hasta que el test (o
    `next_fills`, consumido al enviar) le asigna fills."""

    def __init__(self, positions=None, bars=None):
        self.positions = dict(positions or {})
        self.bars = bars or {}
        self.orders = {}
        self.submitted = []          # (symbol, side, qty)
        self.next_fills = []         # [(filled_qty|None=todo, avg_price, status)]
        self.get_order_calls = 0

    # mercado
    def get_asset_tradable(self, symbol):
        return True

    def get_bars(self, symbol, timeframe="1Min", limit=120, start_iso=None):
        return self.bars[symbol][-limit:]

    def get_position_qty(self, symbol):
        return self.positions.get(symbol, 0)

    def get_clock_is_open(self):
        return True

    # órdenes
    def cancel_open_orders(self, symbol):
        pass

    def place_order_market(self, symbol, side, qty, tif="day"):
        oid = f"ord{len(self.submitted) + 1}"
        self.submitted.append((symbol, side, qty))
        self.orders[oid] = {"id": oid, "symbol": symbol, "side": side, "qty": str(qty), "type": "market",
                            "status": "pending_new", "filled_qty": "0", "filled_avg_price": None,
                            "submitted_at": SUBMITTED_AT, "filled_at": None}
        ack = dict(self.orders[oid])
        if self.next_fills:
            filled, avg, status = self.next_fills.pop(0)
            self.fill(oid, qty if filled is None else filled, avg, status)
        return ack  # el acuse del POST SIEMPRE es pending_new, como en la sesión real

    def fill(self, oid, filled_qty_total, avg_price, status="filled"):
        o = self.orders[oid]
        prev = float(o["filled_qty"])
        delta = filled_qty_total - prev
        sign = 1 if o["side"] == "buy" else -1
        self.positions[o["symbol"]] = self.positions.get(o["symbol"], 0) + sign * int(delta)
        o.update(status=status, filled_qty=str(filled_qty_total),
                 filled_avg_price=None if avg_price is None else str(avg_price),
                 filled_at="2026-09-24T13:39:41.644901887Z" if filled_qty_total else None)

    def set_status(self, oid, status):
        self.orders[oid]["status"] = status

    def get_order(self, order_id):
        self.get_order_calls += 1
        return dict(self.orders[order_id])


class FakeRisk:
    def __init__(self, decisions=None, trailing_step=0.0):
        self.decisions = decisions or {}
        self.entry_calls = []
        self.closes = []
        self.trailing_step = trailing_step

    def assess_entry(self, symbol, side, price, bars_dict):
        self.entry_calls.append(symbol)
        qty, stop, take = self.decisions.get(symbol, (10, price - 1, price + 2))
        return RiskDecision(allow=True, qty=qty, entry=round(price * 1.0005, 2), stop=stop, take_profit=take,
                            reason="OK")

    def update_trailing_stop(self, side, price, stop, bars_dict):
        return stop + self.trailing_step if side == Side.LONG else stop - self.trailing_step

    def should_halt_trading(self):
        return False, ""

    def record_close(self, symbol, side, qty, entry, stop, take_profit, pnl):
        self.closes.append({"symbol": symbol, "qty": qty, "entry": entry, "pnl": pnl})


class FakeStrategy:
    def __init__(self, signal):
        self.signal = signal

    def evaluate(self, df):
        return StrategyResult(signal=self.signal, reason="test")


def _bars(price, n=30):
    last = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)
    first = last - timedelta(minutes=n - 1)
    out = []
    for i in range(n):
        c = price if i == n - 1 else price - 0.01
        out.append({"t": (first + timedelta(minutes=i)).isoformat(), "o": c, "h": c + 0.5, "l": c - 0.5, "c": c, "v": 10_000})
    return out


def _args(**kw):
    base = dict(strategy="ma", fast=3, slow=7, explain=False, debug_ma=False, allow_shorts=False,
                enter_when_above=False, exit_when_below=False, enter_short_when_below=False,
                exit_short_when_above=False, be_at_r=99.0, max_giveback_pct=0.0, daily_profit_halt=300.0)
    base.update(kw)
    return Namespace(**base)


def _position(entry, cost_basis, qty, stop, take, risk_ps=1.0, side=Side.LONG):
    return {"side": side, "qty": qty, "entry": entry, "stop": stop, "take": take, "risk_ps": risk_ps,
            "be_done": False, "scaled": set(), "peak_px": entry, "peak_pnl": 0.0,
            "cost_basis": cost_basis, "entry_filled_qty": qty, "realized_pnl": 0.0}


class Harness:
    def __init__(self, tmp_path, positions=None, decisions=None, halt=300.0, trailing_step=0.0):
        self.broker = FakeBroker(positions=positions)
        self.risk = FakeRisk(decisions, trailing_step)
        self.book = {}
        self.session = {"pnl_today": 0.0, "halted": False}
        self.orders = OrderTracker()
        self.slog = SessionLogger(session_id="lifecycle_test", directory=tmp_path)
        self.halt = halt

    def tick(self, symbol, price, signal=None, scale_out=(), **args_kw):
        self.broker.bars[symbol] = _bars(price)
        run_paper.trade_one_symbol(
            broker=self.broker, risk=self.risk, strat=FakeStrategy(signal), symbol=symbol, timeframe="1Min",
            lookback=120, start_iso="x", args=_args(daily_profit_halt=self.halt, **args_kw),
            position_book=self.book, ensemble=None, wrappers=None, scale_out_levels=list(scale_out),
            session=self.session, session_logger=self.slog, guards=None, orders=self.orders,
        )

    def ctx(self):
        return run_paper.OrderContext(broker=self.broker, orders=self.orders, position_book=self.book,
                                      risk=self.risk, session=self.session, daily_profit_halt=self.halt,
                                      session_logger=self.slog)

    def last_oid(self):
        return f"ord{len(self.broker.submitted)}"

    def events(self, etype=None):
        with open(self.slog.path, encoding="utf-8") as fh:
            ev = [json.loads(line) for line in fh if line.strip()]
        return [e for e in ev if etype is None or e["event_type"] == etype]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(run_paper, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(run_paper, "FILL_REFRESH_SECONDS", 0.0)  # una sola consulta, sin sleep

    def no_network(*a, **k):
        raise AssertionError("los tests no deben usar la red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, no_network)


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.slog.close()


# ---------------------------------------------------------------- entradas
def test_entry_cost_basis_is_confirmed_fill_not_decision_entry(h):
    h.risk.decisions["AMD"] = (89, 606.41, 619.28)
    h.broker.next_fills.append((None, 612.493258, "filled"))
    h.tick("AMD", 611.25, "BUY")
    meta = h.book["AMD"]
    assert meta["entry"] == pytest.approx(611.56)          # modelado: sigue alimentando R/stops
    assert meta["cost_basis"] == pytest.approx(612.493258)  # contable: fill confirmado
    assert meta["entry_filled_qty"] == 89 and meta["qty"] == 89
    assert len(h.orders) == 0 and h.session["pnl_today"] == 0.0


def test_entry_still_pending_has_no_cost_basis_and_holds_symbol(h):
    h.tick("AMD", 100.0, "BUY")
    assert h.book["AMD"]["cost_basis"] is None and h.orders.has_open("AMD")
    h.tick("AMD", 100.0, "BUY")  # sigue pending: no se re-evalúa ni se envía otra orden
    assert len(h.broker.submitted) == 1 and h.risk.entry_calls == ["AMD"]
    h.broker.fill("ord1", 10, 100.2)
    h.tick("AMD", 100.0, None)
    assert h.book["AMD"]["cost_basis"] == pytest.approx(100.2) and len(h.orders) == 0


@pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
def test_entry_with_zero_fill_leaves_no_position(h, status):
    h.tick("AMD", 100.0, "BUY")
    h.broker.set_status("ord1", status)
    h.tick("AMD", 100.0, None)
    assert "AMD" not in h.book and len(h.orders) == 0


def test_entry_partially_filled_then_canceled_manages_only_filled_qty(h):
    h.tick("AMD", 100.0, "BUY")
    h.broker.fill("ord1", 4, 100.1, "canceled")
    h.tick("AMD", 100.0, None)
    assert h.book["AMD"]["qty"] == 4 and h.book["AMD"]["entry_filled_qty"] == 4


# ---------------------------------------------------------------- salidas completas
@pytest.mark.parametrize("fill_px,expected", [(105.0, 50.0), (97.0, -30.0)])
def test_full_exit_books_confirmed_pnl(h, fill_px, expected):
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=99.9, cost_basis=100.0, qty=10, stop=90.0, take=500.0)
    h.broker.next_fills.append((None, fill_px, "filled"))
    h.tick("AMD", 101.0, "SELL")
    assert h.session["pnl_today"] == pytest.approx(expected)
    assert "AMD" not in h.book and h.broker.positions["AMD"] == 0
    assert h.risk.closes == [{"symbol": "AMD", "qty": 10, "entry": 100.0, "pnl": pytest.approx(expected)}]


def test_pltr_sign_reversal_uses_fill_not_estimate(h):
    """Sesión real: la estimación (vela 190.875 - modelado 190.80) daba +23.62; los fills dan -78.75."""
    h.broker.positions["PLTR"] = 315
    h.book["PLTR"] = _position(entry=190.80, cost_basis=191.00, qty=315, stop=189.70, take=192.97, risk_ps=1.45)
    h.broker.next_fills.append((None, 190.75, "filled"))
    h.tick("PLTR", 190.875, "SELL")
    assert h.session["pnl_today"] == pytest.approx(-78.75)
    assert h.risk.closes[0]["pnl"] == pytest.approx(-78.75)  # la racha de pérdidas la ve como pérdida


def test_pending_new_exit_books_zero_and_keeps_position(h):
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)  # take-profit con una estimación enorme de ganancia
    assert h.broker.submitted == [("AMD", "sell", 10)]
    assert h.session["pnl_today"] == 0.0
    assert h.book["AMD"]["qty"] == 10 and h.orders.has_open("AMD")
    h.tick("AMD", 150.0, None)  # sigue pending: no se envía una segunda venta
    assert len(h.broker.submitted) == 1


@pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
def test_zero_fill_terminal_exit_books_zero_and_keeps_position_managed(h, status):
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)
    h.broker.set_status("ord1", status)
    h.tick("AMD", 150.0, None)  # reconcilia (0 fills) y, libre de órdenes, vuelve a gestionar: nueva salida
    assert h.session["pnl_today"] == 0.0 and h.risk.closes == []
    assert h.book["AMD"]["qty"] == 10
    assert h.broker.submitted == [("AMD", "sell", 10), ("AMD", "sell", 10)]


def test_partial_full_exit_keeps_unfilled_remainder_managed(h):
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)
    h.broker.fill("ord1", 4, 105.0, "canceled")  # 4 llenadas, luego cancelada
    h.tick("AMD", 150.0, None)
    assert h.session["pnl_today"] == pytest.approx(20.0)   # solo las 4 confirmadas
    assert h.risk.closes == []                             # el trade no terminó
    assert h.broker.submitted[-1] == ("AMD", "sell", 6)    # el remanente sigue gestionado
    h.broker.fill(h.last_oid(), 6, 106.0)
    h.tick("AMD", 150.0, None)
    assert h.session["pnl_today"] == pytest.approx(20.0 + 36.0)
    assert "AMD" not in h.book and h.risk.closes[0]["pnl"] == pytest.approx(56.0)


def test_delayed_fill_is_reconciled_on_a_later_loop(h):
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)
    assert h.session["pnl_today"] == 0.0
    h.broker.fill("ord1", 10, 111.0)
    run_paper.reconcile_pending_orders(h.ctx())  # lo que hace _run_loop al inicio de cada pasada
    assert h.session["pnl_today"] == pytest.approx(110.0) and "AMD" not in h.book
    upd = [e for e in h.events("order_update") if e["newly_filled_qty"]][0]
    assert upd["status"] == "filled" and upd["latency_seconds"] == pytest.approx(1.921, abs=1e-3)


def test_same_fill_is_never_counted_twice(h):
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)
    ctx = h.ctx()
    h.broker.fill("ord1", 4, 105.0, "partially_filled")
    for _ in range(3):
        run_paper.reconcile_pending_orders(ctx)
    assert h.session["pnl_today"] == pytest.approx(20.0) and h.book["AMD"]["qty"] == 6
    h.broker.fill("ord1", 10, 106.0, "filled")  # media acumulada: las 6 nuevas salen a 106.67
    for _ in range(3):
        run_paper.reconcile_pending_orders(ctx)
    assert h.session["pnl_today"] == pytest.approx((106.0 - 100.0) * 10)
    fills = [e for e in h.events("order_update") if e["newly_filled_qty"]]
    assert [e["newly_filled_qty"] for e in fills] == [4.0, 6.0]
    assert fills[1]["fill_price"] == pytest.approx((1060 - 420) / 6)


def test_short_cover_pnl_uses_fill(h):
    h.broker.positions["AMD"] = -10
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=110.0, take=90.0, side=Side.SHORT)
    h.broker.next_fills.append((None, 89.5, "filled"))
    h.tick("AMD", 89.0, None)
    assert h.broker.submitted == [("AMD", "buy", 10)]
    assert h.session["pnl_today"] == pytest.approx(105.0)


def test_exit_without_confirmed_cost_basis_books_nothing(h):
    """Posición reconstruida/huérfana sin fill de entrada confirmado: no se estima P&L."""
    h.broker.positions["AMD"] = 10
    h.book["AMD"] = _position(entry=100.0, cost_basis=None, qty=10, stop=90.0, take=110.0)
    h.broker.next_fills.append((None, 120.0, "filled"))
    h.tick("AMD", 150.0, None)
    assert h.session["pnl_today"] == 0.0 and h.risk.closes == [] and "AMD" not in h.book
    assert [e["note"] for e in h.events("order_update")] == ["cost_basis_unavailable"]


# ---------------------------------------------------------------- scale-outs
def test_scale_out_realized_pnl_counts_toward_pnl_today(h):
    h.broker.positions["AMD"] = 89
    h.book["AMD"] = _position(entry=611.56, cost_basis=612.493258, qty=89, stop=606.41, take=619.28, risk_ps=5.15)
    h.broker.next_fills.append((None, 617.728863, "filled"))
    h.tick("AMD", 616.91, None, scale_out=[(1.0, 0.5)])
    assert h.broker.submitted == [("AMD", "sell", 44)]
    assert h.session["pnl_today"] == pytest.approx((617.728863 - 612.493258) * 44)
    assert h.book["AMD"]["qty"] == 45 and h.risk.closes == []  # un parcial no es un trade cerrado


def test_partial_scale_out_keeps_remainder_managed(h):
    h.broker.positions["AMD"] = 89
    h.book["AMD"] = _position(entry=611.56, cost_basis=612.0, qty=89, stop=606.41, take=700.0, risk_ps=5.15)
    h.tick("AMD", 616.91, None, scale_out=[(1.0, 0.5)])
    h.broker.fill("ord1", 20, 617.0, "canceled")
    h.tick("AMD", 605.0, None, scale_out=[(1.0, 0.5)])  # reconcilia y luego salta el stop
    assert h.session["pnl_today"] == pytest.approx(5.0 * 20)
    assert h.broker.submitted[-1] == ("AMD", "sell", 69)  # el remanente sigue con stop activo


# ---------------------------------------------------------------- objetivo diario
def test_daily_halt_uses_only_confirmed_pnl(tmp_path):
    h = Harness(tmp_path, positions={"AMD": 10}, halt=40.0)
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 200.0, None)  # estimación de vela: +1000
    assert h.session["halted"] is False and h.session["pnl_today"] == 0.0
    h.broker.fill("ord1", 10, 103.0)
    run_paper.reconcile_pending_orders(h.ctx())
    assert h.session["pnl_today"] == pytest.approx(30.0) and h.session["halted"] is False
    h.slog.close()


def test_halt_blocks_later_entry_in_same_pass(tmp_path):
    h = Harness(tmp_path, positions={"AMD": 10}, halt=40.0)
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.broker.next_fills.append((None, 105.0, "filled"))
    h.tick("AMD", 111.0, None)                  # fill inmediato: +50 -> objetivo alcanzado
    assert h.session["halted"] is True
    h.tick("MU", 50.0, "BUY")                   # símbolo siguiente, misma pasada
    assert h.risk.entry_calls == [] and all(s != "MU" for s, _, _ in h.broker.submitted)
    guard = h.events("execution_guard")
    assert [g["guard"] for g in guard] == ["DAILY_PROFIT_HALT"] and guard[0]["symbol"] == "MU"
    halt_upd = [e for e in h.events("order_update") if e["halt_triggered"]]
    assert len(halt_upd) == 1 and halt_upd[0]["pnl_today"] == pytest.approx(50.0)
    h.slog.close()


def test_halted_session_keeps_managing_open_positions(tmp_path):
    h = Harness(tmp_path, positions={"AMD": 10}, trailing_step=0.5)
    h.session.update(halted=True, pnl_today=400.0)
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=95.0, take=500.0)
    h.tick("AMD", 101.0, None)                  # trailing sigue actualizándose
    assert h.book["AMD"]["stop"] == pytest.approx(95.5)
    h.broker.next_fills.append((None, 94.0, "filled"))
    h.tick("AMD", 94.0, None)                   # y el stop sigue cerrando
    assert h.broker.submitted == [("AMD", "sell", 10)] and "AMD" not in h.book
    assert h.session["pnl_today"] == pytest.approx(400.0 - 60.0)
    assert any(e["action"] == "trailing_stop_update" for e in h.events("position_management"))
    h.slog.close()


def test_run_loop_does_not_skip_ticks_when_halted(tmp_path, monkeypatch):
    calls = []

    def fake_tick(*, symbol, orders, **kw):
        calls.append((symbol, orders))
    monkeypatch.setattr(run_paper, "trade_one_symbol", fake_tick)
    monkeypatch.setattr(run_paper.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    args = run_paper.build_arg_parser().parse_args(["--symbols", "AMD,MU", "--poll-seconds", "0"])
    session = {"pnl_today": 500.0, "halted": True}
    orders = OrderTracker()
    run_paper._run_loop(args, ["AMD", "MU"], FakeBroker(), FakeRisk(), None, {}, None, None, [],
                        session, None, None, orders)
    assert [c[0] for c in calls] == ["AMD", "MU"] and all(c[1] is orders for c in calls)


# ---------------------------------------------------------------- réplica de la sesión 20260924
def test_replay_session_20260924_with_confirmed_fills(tmp_path):
    """Mismos precios de vela y fills reales que la sesión auditada: el P&L
    que dispara el objetivo es 436.78 (fills), no 402.97 (estimación sin scale-out)."""
    h = Harness(tmp_path, decisions={"AMD": (89, 606.41, 619.28), "PLTR": (315, 189.35, 192.97)})
    sc = [(1.0, 0.5), (2.0, 0.5)]
    kw = dict(scale_out=sc, be_at_r=1.0, max_giveback_pct=0.5)
    h.broker.next_fills.append((None, 612.493258, "filled"))
    h.tick("AMD", 611.25, "BUY", **kw)
    h.broker.next_fills.append((None, 191.00, "filled"))
    h.tick("PLTR", 190.705, "BUY", **kw)
    h.broker.next_fills.append((None, 617.728863, "filled"))
    h.tick("AMD", 616.91, None, **kw)            # scale-out 44
    h.broker.next_fills.append((None, 190.75, "filled"))
    h.tick("PLTR", 190.875, "SELL", **kw)        # salida por señal
    assert h.session["halted"] is False
    h.broker.next_fills.append((None, 618.830222, "filled"))
    h.tick("AMD", 619.99, None, **kw)            # take-profit con las 45 restantes

    assert h.broker.submitted == [("AMD", "buy", 89), ("PLTR", "buy", 315), ("AMD", "sell", 44),
                                  ("PLTR", "sell", 315), ("AMD", "sell", 45)]
    assert h.session["pnl_today"] == pytest.approx(436.78, abs=0.01)
    assert h.session["halted"] is True
    closes = {c["symbol"]: c["pnl"] for c in h.risk.closes}
    assert closes["PLTR"] == pytest.approx(-78.75)
    assert closes["AMD"] == pytest.approx(230.37 + 285.16, abs=0.01)
    assert h.book == {} and len(h.orders) == 0

    # logging consistente: cada orden tiene submission + result + update terminal
    subs, results = h.events("order_submission"), h.events("order_result")
    assert [s["purpose"] for s in subs] == ["entry", "entry", "scale_out", "signal_exit", "take_profit_hit"]
    assert len(results) == 5 and all(r["status"] == "pending_new" for r in results)
    finals = [e for e in h.events("order_update") if e["terminal"]]
    assert sorted(e["order_id"] for e in finals) == [f"ord{i}" for i in range(1, 6)]
    assert all(e["status"] == "filled" for e in finals)

    # resumen y analizador coinciden y separan acuse de estado final
    summary = h.slog.session_end("test")
    h.slog.close()
    o = summary["orders"]
    assert o["submissions"] == 5 and o["results_by_status"] == {"pending_new": 5}
    assert o["final_status_by_order"] == {"filled": 5} and o["unresolved_orders"] == 0
    assert o["fills"] == 5 and o["filled_qty_total"] == 808.0
    assert o["confirmed_realized_pnl"] == pytest.approx(436.78, abs=0.01)
    a = az.analyze_file(h.slog.path)
    assert a["summary_reconciliation"]["status"] == "MATCH", a["summary_reconciliation"]["mismatches"]
    assert a["orders"]["unresolved_orders"] == [] and a["orders"]["fills"] == 5


# ---------------------------------------------------------------- logging / resumen
def test_every_order_path_emits_submission_and_result(tmp_path):
    h = Harness(tmp_path, positions={"X1": 10, "X2": 10, "X3": 10})
    h.book["X1"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=500.0)
    h.tick("X1", 101.0, None, max_giveback_pct=0.5)          # sin pico todavía
    h.book["X1"]["peak_pnl"] = 100.0
    h.tick("X1", 100.5, None, max_giveback_pct=0.5)          # giveback_close
    h.book["X2"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=99.0, take=500.0)
    h.tick("X2", 98.0, None)                                  # stop_hit
    h.tick("X3", 100.0, "SELL")                               # salida sin registro local (fallback)
    h.tick("X4", 100.0, "BUY")                                # entrada
    purposes = [e["purpose"] for e in h.events("order_submission")]
    assert purposes == ["giveback_close", "stop_hit", "signal_exit", "entry"]
    assert [e["purpose"] for e in h.events("order_result")] == purposes
    h.slog.close()


def test_unresolved_orders_at_session_end_are_listed_not_invented(tmp_path, capsys):
    h = Harness(tmp_path, positions={"AMD": 10})
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)
    run_paper._finish_session(h.slog, "manual_stop", h.orders)
    assert h.session["pnl_today"] == 0.0 and h.book["AMD"]["qty"] == 10
    end = h.events("session_end")[0]
    assert [u["order_id"] for u in end["unresolved_orders"]] == ["ord1"]
    assert end["summary"]["orders"]["unresolved_orders"] == 1
    assert "sin estado final" in capsys.readouterr().out
    a = az.analyze_file(h.slog.path)
    assert a["summary_reconciliation"]["status"] == "MATCH"
    assert a["orders"]["unresolved_orders"][0]["purpose"] == "take_profit_hit"


def test_partial_fill_counts_in_summary_and_analyzer(tmp_path):
    h = Harness(tmp_path, positions={"AMD": 10})
    h.book["AMD"] = _position(entry=100.0, cost_basis=100.0, qty=10, stop=90.0, take=110.0)
    h.tick("AMD", 150.0, None)
    h.broker.fill("ord1", 4, 105.0, "partially_filled")
    run_paper.reconcile_pending_orders(h.ctx())
    h.broker.fill("ord1", 4, 105.0, "canceled")
    run_paper.reconcile_pending_orders(h.ctx())
    s = h.slog.session_end("test")
    h.slog.close()
    assert s["orders"]["partially_filled_orders"] == 1
    assert s["orders"]["final_status_by_order"] == {"canceled": 1}
    assert s["orders"]["filled_qty_total"] == 4.0 and s["orders"]["confirmed_realized_pnl"] == pytest.approx(20.0)
    a = az.analyze_file(h.slog.path)
    assert a["summary_reconciliation"]["status"] == "MATCH", a["summary_reconciliation"]["mismatches"]
    assert a["orders"]["partially_filled_orders"] == ["ord1"]


def test_legacy_summary_without_lifecycle_fields_still_matches(tmp_path):
    slog = SessionLogger(session_id="legacy", directory=tmp_path)
    slog.order_submission(symbol="AMD", side="buy", requested_qty=1, bar_timestamp="2026-09-24T13:35:00+00:00")
    slog.order_result(symbol="AMD", order={"id": "o1", "status": "pending_new"},
                      bar_timestamp="2026-09-24T13:35:00+00:00")
    slog.session_end("manual_stop")
    slog.close()
    lines = slog.path.read_text(encoding="utf-8").splitlines()
    end = json.loads(lines[-1])
    end["summary"]["orders"] = {k: end["summary"]["orders"][k] for k in ("submissions", "results_by_status")}
    lines[-1] = json.dumps(end)
    slog.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rec = az.analyze_file(slog.path)["summary_reconciliation"]
    assert rec["status"] == "MATCH"
    assert "orders.<ciclo de vida>" in rec["not_compared"]


# ---------------------------------------------------------------- unidades
def test_terminal_status_copies_match():
    assert session_summary._TERMINAL_STATUSES == order_tracking.TERMINAL_STATUSES
    assert az._TERMINAL_STATUSES == order_tracking.TERMINAL_STATUSES


def test_tracked_order_incremental_price_and_idempotence():
    t = TrackedOrder("o", "AMD", "sell", "exit", "LONG", 10)
    d1 = t.apply({"status": "partially_filled", "filled_qty": "4", "filled_avg_price": "105"})
    assert d1.new_qty == 4 and d1.fill_price == pytest.approx(105.0)
    again = t.apply({"status": "partially_filled", "filled_qty": "4", "filled_avg_price": "105"})
    assert again.changed is False and again.new_qty == 0
    d2 = t.apply({"status": "filled", "filled_qty": "10", "filled_avg_price": "106"})
    assert d2.new_qty == 6 and d2.fill_price == pytest.approx(640 / 6) and d2.terminal


def test_parse_alpaca_nanosecond_timestamps():
    assert parse_alpaca_ts(SUBMITTED_AT) == datetime(2026, 9, 24, 13, 39, 39, 724088, tzinfo=timezone.utc)
    assert parse_alpaca_ts(None) is None and parse_alpaca_ts("nope") is None


def test_get_order_is_read_only_lookup(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "abc", "status": "filled"}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return Resp()
    monkeypatch.setattr(broker_alpaca.requests, "get", fake_get)
    b = broker_alpaca.BrokerAlpaca.__new__(broker_alpaca.BrokerAlpaca)
    b.base = "https://paper-api.example"
    assert b.get_order("abc")["status"] == "filled"
    assert seen["url"] == "https://paper-api.example/v2/orders/abc"
