"""
Pruebas para la persistencia y reconciliación de posiciones agregadas a
src/run_paper.py — la parte que corrige el bug crítico de "si el bot se
reinicia con una posición abierta en el broker, se queda sin stop/trailing
porque el estado vivía solo en memoria".

No hace llamadas de red: usa un broker falso en memoria.
"""
import json

import pytest

from src import run_paper
from src.risk_manager_avanzado import Side


class FakeBroker:
    def __init__(self, positions_qty=None, last_price=50.0):
        # symbol -> qty (positivo=long, negativo=short, 0/ausente=flat)
        self.positions_qty = positions_qty or {}
        self.last_price = last_price

    def get_position_qty(self, symbol: str) -> int:
        return self.positions_qty.get(symbol, 0)

    def get_bars(self, symbol, timeframe="1Min", limit=5):
        return [{"c": self.last_price}]

    def get_account(self):
        return {"last_equity": "10000.0"}


@pytest.fixture(autouse=True)
def isolated_state_path(tmp_path, monkeypatch):
    """Nunca toques data/state.json real durante los tests."""
    monkeypatch.setattr(run_paper, "STATE_PATH", tmp_path / "state.json")
    yield


def test_save_and_load_roundtrip_preserves_side_enum_and_scaled_set():
    book = {
        "AAPL": {
            "side": Side.LONG, "qty": 10, "entry": 100.0, "stop": 95.0, "take": 110.0,
            "risk_ps": 5.0, "be_done": True, "scaled": {"R1.0"},
            "peak_px": 105.0, "peak_pnl": 50.0,
        }
    }
    run_paper.save_position_book(book)
    assert run_paper.STATE_PATH.exists()

    loaded = run_paper.load_position_book()
    assert loaded["AAPL"]["side"] == Side.LONG
    assert isinstance(loaded["AAPL"]["side"], Side)
    assert loaded["AAPL"]["scaled"] == {"R1.0"}
    assert isinstance(loaded["AAPL"]["scaled"], set)
    assert loaded["AAPL"]["qty"] == 10
    assert loaded["AAPL"]["stop"] == 95.0


def test_load_position_book_missing_file_returns_empty_dict():
    assert run_paper.load_position_book() == {}


def test_load_position_book_corrupt_file_returns_empty_dict_and_warns():
    run_paper.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    run_paper.STATE_PATH.write_text("{not valid json", encoding="utf-8")
    assert run_paper.load_position_book() == {}


def test_reconcile_drops_local_position_when_broker_reports_flat():
    broker = FakeBroker(positions_qty={"AAPL": 0})
    book = {"AAPL": {"side": Side.LONG, "qty": 10, "entry": 100.0, "stop": 95.0,
                      "take": None, "risk_ps": 5.0, "be_done": False, "scaled": set(),
                      "peak_px": 100.0, "peak_pnl": 0.0}}
    run_paper.reconcile_positions(broker, book, ["AAPL"])
    assert "AAPL" not in book


def test_reconcile_reconstructs_orphaned_broker_position():
    """El caso crítico: el broker tiene una posición que el bot desconoce
    (por ejemplo, se reinició el proceso). No debe quedar sin stop."""
    broker = FakeBroker(positions_qty={"AAPL": 25}, last_price=200.0)
    book = {}
    run_paper.reconcile_positions(broker, book, ["AAPL"])
    assert "AAPL" in book
    meta = book["AAPL"]
    assert meta["side"] == Side.LONG
    assert meta["qty"] == 25
    assert meta["stop"] is not None
    assert meta["stop"] < meta["entry"]  # stop conservador por debajo del entry para un long


def test_reconcile_reconstructs_orphaned_short_position():
    broker = FakeBroker(positions_qty={"AAPL": -25}, last_price=200.0)
    book = {}
    run_paper.reconcile_positions(broker, book, ["AAPL"])
    meta = book["AAPL"]
    assert meta["side"] == Side.SHORT
    assert meta["qty"] == 25
    assert meta["stop"] > meta["entry"]  # stop conservador por encima del entry para un short


def test_reconcile_adjusts_qty_mismatch_to_broker_truth():
    broker = FakeBroker(positions_qty={"AAPL": 30})
    book = {"AAPL": {"side": Side.LONG, "qty": 10, "entry": 100.0, "stop": 95.0,
                      "take": None, "risk_ps": 5.0, "be_done": False, "scaled": set(),
                      "peak_px": 100.0, "peak_pnl": 0.0}}
    run_paper.reconcile_positions(broker, book, ["AAPL"])
    assert book["AAPL"]["qty"] == 30


def test_reconcile_leaves_matching_position_untouched():
    broker = FakeBroker(positions_qty={"AAPL": 10})
    original = {"side": Side.LONG, "qty": 10, "entry": 100.0, "stop": 95.0,
                "take": 110.0, "risk_ps": 5.0, "be_done": True, "scaled": {"R1.0"},
                "peak_px": 103.0, "peak_pnl": 30.0}
    book = {"AAPL": dict(original)}
    run_paper.reconcile_positions(broker, book, ["AAPL"])
    assert book["AAPL"]["stop"] == 95.0
    assert book["AAPL"]["take"] == 110.0
    assert book["AAPL"]["be_done"] is True


def test_reconcile_persists_to_disk():
    broker = FakeBroker(positions_qty={})
    book = {}
    run_paper.reconcile_positions(broker, book, ["AAPL"])
    assert run_paper.STATE_PATH.exists()
    on_disk = json.loads(run_paper.STATE_PATH.read_text(encoding="utf-8"))
    assert on_disk == {}
