"""
Regresión del incidente de datos congelados (sesión paper 2026-09-23):
get_bars pedía start=ahora-6h + limit=120 sin `sort`, Alpaca ordena asc por
defecto y `limit` corta la PRIMERA página -> el bot recibía siempre las 120
velas MÁS ANTIGUAS del intervalo (13:30..15:29 UTC) y dejó de avanzar.

Sin red: FakeAlpacaBars reproduce la semántica relevante del endpoint
/v2/stocks/{symbol}/bars (filtro start, sort asc|desc, limit = una página).
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import broker_alpaca
from src.broker_alpaca import BrokerAlpaca
from src.data import bars_to_df


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_bars(first: datetime, n: int, base_price: float = 100.0):
    """n velas de 1Min consecutivas a partir de `first`, en orden cronológico."""
    return [
        {"t": _iso(first + timedelta(minutes=i)), "o": base_price + i, "h": base_price + i + 0.5,
         "l": base_price + i - 0.5, "c": base_price + i, "v": 1000 + i}
        for i in range(n)
    ]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeAlpacaBars:
    """Imita la paginación de Alpaca: filtra por start, ordena según `sort`
    (asc por defecto) y devuelve solo la primera página de `limit` barras."""

    def __init__(self, bars):
        self.bars = list(bars)
        self.calls = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        rows = self.bars
        if "start" in params:
            start = params["start"]
            rows = [b for b in rows if b["t"] >= start]
        rows = sorted(rows, key=lambda b: b["t"], reverse=(params.get("sort", "asc") == "desc"))
        page = rows[: int(params.get("limit", 1000))]
        return _Resp({"bars": page or None, "symbol": url.rsplit("/", 2)[-2], "next_page_token": None})


@pytest.fixture
def broker():
    b = BrokerAlpaca()
    b.data_base = "https://data.alpaca.markets/v2"
    return b


def _install(monkeypatch, bars) -> FakeAlpacaBars:
    fake = FakeAlpacaBars(bars)
    monkeypatch.setattr(broker_alpaca.requests, "get", fake)
    return fake


OPEN = datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc)
START_6H = "2026-09-23T10:30:00Z"  # ahora (16:30) - 6h: antes de la primera vela del día


def test_request_asks_for_newest_page_first(monkeypatch, broker):
    fake = _install(monkeypatch, make_bars(OPEN, 10))
    broker.get_bars("NVDA", timeframe="1Min", limit=120, start_iso=START_6H)
    url, params = fake.calls[-1]
    assert url.endswith("/stocks/NVDA/bars")
    assert params == {"timeframe": "1Min", "limit": 120, "feed": "iex", "sort": "desc", "start": START_6H}


def test_fake_reproduces_root_cause_without_sort(monkeypatch):
    """Documenta el bug: sin sort=desc la primera página son las 120 más antiguas."""
    fake = _install(monkeypatch, make_bars(OPEN, 180))
    page = fake("https://x/v2/stocks/NVDA/bars", params={"timeframe": "1Min", "limit": 120, "start": START_6H}).json()["bars"]
    assert page[-1]["t"] == "2026-09-23T15:29:00Z"


def test_more_than_lookback_returns_newest_not_oldest(monkeypatch, broker):
    all_bars = make_bars(OPEN, 180)  # 13:30 .. 16:29
    _install(monkeypatch, all_bars)
    df = bars_to_df(broker.get_bars("NVDA", limit=120, start_iso=START_6H))
    assert len(df) == 120
    assert df.index[-1] == datetime(2026, 9, 23, 16, 29, tzinfo=timezone.utc)
    assert df.index[0] == datetime(2026, 9, 23, 14, 30, tzinfo=timezone.utc)
    assert df["close"].iloc[-1] == all_bars[-1]["c"]


def test_window_rolls_forward_when_new_bar_appears(monkeypatch, broker):
    # Poll 1: incluye pre-market, >120 velas, la última es 15:29.
    pre = make_bars(datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc), 30, base_price=90.0)
    session = make_bars(OPEN, 120)  # 13:30 .. 15:29
    _install(monkeypatch, pre + session)
    df1 = bars_to_df(broker.get_bars("NVDA", limit=120, start_iso=START_6H))

    # Poll 2: aparece la vela 15:30.
    _install(monkeypatch, pre + session + make_bars(datetime(2026, 9, 23, 15, 30, tzinfo=timezone.utc), 1, 500.0))
    df2 = bars_to_df(broker.get_bars("NVDA", limit=120, start_iso=START_6H))

    assert len(df1) == len(df2) == 120
    assert df1.index[-1] == datetime(2026, 9, 23, 15, 29, tzinfo=timezone.utc)
    assert df2.index[-1] == datetime(2026, 9, 23, 15, 30, tzinfo=timezone.utc)
    assert df2["close"].iloc[-1] == 500.0
    # la ventana rodó exactamente una vela
    assert df2.index[0] == df1.index[1]


def test_newest_window_is_chronological_oldest_to_newest(monkeypatch, broker):
    _install(monkeypatch, make_bars(OPEN, 300))
    raw = broker.get_bars("NVDA", limit=120, start_iso=START_6H)
    assert [b["t"] for b in raw] == sorted(b["t"] for b in raw)  # la lista cruda ya viene asc
    df = bars_to_df(raw)
    assert df.index.is_monotonic_increasing
    assert df.index.is_unique


def test_fewer_bars_than_lookback_returns_all(monkeypatch, broker):
    bars = make_bars(OPEN, 50)
    _install(monkeypatch, bars)
    df = bars_to_df(broker.get_bars("NVDA", limit=120, start_iso=START_6H))
    assert len(df) == 50
    assert df.index[0] == OPEN
    assert df.index[-1] == OPEN + timedelta(minutes=49)
    assert df.index.is_monotonic_increasing


def test_no_bars_returns_empty_list(monkeypatch, broker):
    _install(monkeypatch, [])
    assert broker.get_bars("NVDA", limit=120, start_iso=START_6H) == []


def test_without_start_last_element_is_latest_bar(monkeypatch, broker):
    """reconcile_positions usa bars[-1]['c'] como precio de referencia: debe ser la vela más reciente."""
    bars = make_bars(OPEN, 30)
    _install(monkeypatch, bars)
    out = broker.get_bars("NVDA", timeframe="1Min", limit=5)
    assert out[-1]["t"] == bars[-1]["t"]
