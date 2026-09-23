"""
Pruebas del preflight de solo lectura (src/preflight.py).

Toda la red está simulada: FakeAlpaca reemplaza requests.get (lo usan tanto
ReadOnlyAlpaca como BrokerAlpaca) y cualquier verbo mutante (POST/PUT/PATCH/
DELETE) hace fallar el test, así que cada prueba verifica también que el
preflight es estrictamente de solo lectura.
"""
import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import requests

from src import preflight
from src.config import settings

KEY = "PKTESTKEYID12345"
SECRET = "sEcReTvAlUe987654"
NOW = datetime(2026, 9, 23, 15, 30, 30, tzinfo=timezone.utc)
SYMS = ["NVDA", "AMD", "MARA"]
ORIGINAL_STATE_PATH = preflight.STATE_PATH  # antes de que el fixture autouse lo redirija


class _Resp:
    def __init__(self, url, status=200, payload=None):
        self.url, self.status_code, self._payload = url, status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}", response=self)


def make_bars(last: datetime, n: int = 150):
    return [{"t": (last - timedelta(minutes=n - 1 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": 100.0 + i, "v": 1000} for i in range(n)]


class FakeAlpaca:
    """Router HTTP que imita los endpoints GET de Alpaca usados por el preflight."""

    def __init__(self, is_open=True, now=NOW, bars_last=None):
        self.calls = []
        self.account = {"account_number": "PA1234567890", "status": "ACTIVE", "trading_blocked": False,
                        "account_blocked": False, "trade_suspended_by_user": False,
                        "equity": "100000", "buying_power": "200000", "cash": "100000", "last_equity": "99000"}
        self.account_status_code = 200
        self.clock = {"is_open": is_open, "timestamp": now.isoformat(),
                      "next_open": "2026-09-24T13:30:00Z", "next_close": "2026-09-23T20:00:00Z"}
        last = bars_last or (now.replace(second=0) - timedelta(minutes=1))
        self.bars = {s: make_bars(last) for s in SYMS}
        self.assets = {s: {"symbol": s, "tradable": True, "status": "active"} for s in SYMS}
        self.bar_errors = {}  # symbol -> Exception
        self.all_status = None  # forzar un status para todo

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append(url)
        params = params or {}
        path = urlparse(url).path
        if self.all_status:
            return _Resp(url, self.all_status, {"message": f"request is not authorized ({KEY})"})
        if path == "/v2/account":
            return _Resp(url, self.account_status_code, self.account if self.account_status_code == 200 else {"message": "unauthorized."})
        if path == "/v2/clock":
            return _Resp(url, 200, self.clock)
        if path.startswith("/v2/assets/"):
            sym = path.rsplit("/", 1)[-1]
            return _Resp(url, 200, self.assets[sym]) if sym in self.assets else _Resp(url, 404, {"message": "asset not found"})
        if path == "/v2/positions":
            return _Resp(url, 200, [])
        if path == "/v2/orders":
            return _Resp(url, 200, [])
        if path.startswith("/v2/stocks/") and path.endswith("/bars"):
            sym = path.split("/")[3]
            if sym in self.bar_errors:
                raise self.bar_errors[sym]
            rows = [b for b in self.bars.get(sym, []) if b["t"] >= params.get("start", "")]
            rows = sorted(rows, key=lambda b: b["t"], reverse=params.get("sort") == "desc")[: int(params.get("limit", 1000))]
            return _Resp(url, 200, {"bars": rows or None})
        return _Resp(url, 404, {"message": "unknown"})


def _forbidden(*a, **k):
    raise AssertionError("El preflight intentó una petición NO-GET")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "APCA_API_KEY_ID", KEY)
    monkeypatch.setattr(settings, "APCA_API_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "APCA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(settings, "APCA_DATA_BASE_URL", "https://data.alpaca.markets/v2")
    monkeypatch.setattr(preflight, "STATE_PATH", tmp_path / "state.json")
    for verb in ("post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(requests, verb, _forbidden)
    yield tmp_path


def _install(monkeypatch, fake):
    monkeypatch.setattr(requests, "get", fake)
    return fake


def _run(fake, monkeypatch, env, symbols=SYMS, now=NOW):
    _install(monkeypatch, fake)
    return preflight.run_preflight(symbols, "1Min", 120, now_fn=lambda: now, session_dir=env / "sessions")


def _check(report, cid):
    return next(c for c in report.checks if c.id == cid)


# ---------------- cuenta / entorno ----------------
def test_healthy_paper_account(monkeypatch, env):
    r = _run(FakeAlpaca(), monkeypatch, env)
    assert r.overall == "PASS" and r.exit_code == 0, [(c.id, c.level, c.message) for c in r.checks if c.level != "PASS"]
    assert _check(r, "paper_endpoint").level == "PASS"
    assert _check(r, "paper_account").level == "PASS"
    assert _check(r, "account_status").level == "PASS"
    assert _check(r, "account_balances").details["buying_power"] == 200000.0
    assert _check(r, "session_log_dir").level == "PASS"
    assert not (env / "sessions").exists()  # la prueba de escritura no deja rastro
    assert list(env.iterdir()) == [] or all(not p.name.startswith(".preflight_") for p in env.iterdir())


@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "http://paper-api.alpaca.markets",
                                 "https://paper-api.alpaca.markets.evil.com", ""])
def test_non_paper_environment_rejected_without_network(monkeypatch, env, url):
    monkeypatch.setattr(settings, "APCA_BASE_URL", url)
    fake = FakeAlpaca()
    r = _run(fake, monkeypatch, env)
    assert _check(r, "paper_endpoint").level == "FAIL"
    assert r.exit_code == 2
    assert fake.calls == []  # ni siquiera un GET contra un entorno no verificado


def test_live_endpoint_message_is_explicit(monkeypatch, env):
    monkeypatch.setattr(settings, "APCA_BASE_URL", "https://api.alpaca.markets")
    r = _run(FakeAlpaca(), monkeypatch, env)
    assert "LIVE" in _check(r, "paper_endpoint").message


def test_missing_credentials_fail_without_network(monkeypatch, env):
    monkeypatch.setattr(settings, "APCA_API_SECRET_KEY", None)
    fake = FakeAlpaca()
    r = _run(fake, monkeypatch, env)
    assert _check(r, "credentials").level == "FAIL" and r.exit_code == 2
    assert fake.calls == []


def test_invalid_credentials_api_failure(monkeypatch, env):
    fake = FakeAlpaca()
    fake.all_status = 401
    r = _run(fake, monkeypatch, env)
    acct = _check(r, "account")
    assert acct.level == "FAIL" and "HTTP 401" in acct.message and "credenciales" in acct.message
    assert _check(r, "paper_account").level == "FAIL"
    assert all(r.symbols[s]["level"] == "FAIL" for s in SYMS)  # se siguen revisando todos
    assert r.exit_code == 2


def test_trading_blocked_account(monkeypatch, env):
    fake = FakeAlpaca()
    fake.account["trading_blocked"] = True
    r = _run(fake, monkeypatch, env)
    assert _check(r, "trading_blocked").level == "FAIL"
    assert r.exit_code == 2


def test_inactive_account_fails_but_low_buying_power_does_not(monkeypatch, env):
    fake = FakeAlpaca()
    fake.account.update(buying_power="3.50", cash="3.50")
    r = _run(fake, monkeypatch, env)
    assert _check(r, "account_balances").level == "INFO" and r.exit_code == 0
    fake.account["status"] = "ACCOUNT_UPDATED"
    r = _run(fake, monkeypatch, env)
    assert _check(r, "account_status").level == "FAIL"


def test_data_url_without_v2_fails(monkeypatch, env):
    monkeypatch.setattr(settings, "APCA_DATA_BASE_URL", "https://data.alpaca.markets")
    r = _run(FakeAlpaca(), monkeypatch, env)
    assert _check(r, "data_endpoint").level == "FAIL" and "/v2" in _check(r, "data_endpoint").message


# ---------------- reloj / frescura ----------------
def test_open_market_fresh_data(monkeypatch, env):
    r = _run(FakeAlpaca(is_open=True), monkeypatch, env)
    for s in SYMS:
        d = r.symbols[s]
        assert d["freshness"] == "FRESH" and d["level"] == "PASS"
        assert d["bars"] == 120 and d["bar_age_seconds"] == 90.0
        assert d["latest_bar_timestamp"] == "2026-09-23T15:29:00+00:00"
        assert d["latest_close"] == 249.0
    clock = _check(r, "market_clock")
    assert clock.details["is_open"] is True and clock.details["next_close"] == "2026-09-23T20:00:00Z"
    assert _check(r, "clock_skew").level == "PASS"


def test_open_market_all_stale_is_fail(monkeypatch, env):
    fake = FakeAlpaca(is_open=True, bars_last=NOW - timedelta(minutes=45))
    r = _run(fake, monkeypatch, env)
    assert all(r.symbols[s]["freshness"] == "STALE" and r.symbols[s]["level"] == "WARN" for s in SYMS)
    assert _check(r, "market_data_summary").level == "FAIL"
    assert r.exit_code == 2


def test_open_market_one_stale_symbol_is_warn(monkeypatch, env):
    fake = FakeAlpaca(is_open=True)
    fake.bars["MARA"] = make_bars(NOW - timedelta(minutes=20))
    r = _run(fake, monkeypatch, env)
    assert r.symbols["MARA"]["freshness"] == "STALE"
    assert r.symbols["NVDA"]["freshness"] == "FRESH"
    assert _check(r, "market_data_summary").level == "WARN"
    assert r.overall == "WARN" and r.exit_code == 0


def test_closed_market_old_bar_not_marked_stale(monkeypatch, env):
    friday_close = datetime(2026, 9, 18, 19, 59, tzinfo=timezone.utc)
    weekend_now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    fake = FakeAlpaca(is_open=False, now=weekend_now, bars_last=friday_close)
    r = _run(fake, monkeypatch, env, now=weekend_now)
    for s in SYMS:
        d = r.symbols[s]
        assert d["freshness"] == "CLOSED_MARKET" and d["level"] == "INFO"
        assert d["bar_age_seconds"] > 40 * 3600  # edad reportada, pero no es un error
    assert r.exit_code == 0
    assert "CERRADO" in _check(r, "market_clock").message


def test_clock_skew_warns(monkeypatch, env):
    fake = FakeAlpaca()
    fake.clock["timestamp"] = (NOW - timedelta(seconds=90)).isoformat()
    r = _run(fake, monkeypatch, env)
    assert _check(r, "clock_skew").level == "WARN"


# ---------------- símbolos ----------------
def test_one_bad_symbol_while_others_succeed(monkeypatch, env):
    r = _run(FakeAlpaca(), monkeypatch, env, symbols=["NVDA", "ZZZZ", "AMD"])
    assert r.symbols["ZZZZ"]["level"] == "FAIL" and r.symbols["ZZZZ"]["freshness"] == "INVALID_SYMBOL"
    assert "HTTP 404" in r.symbols["ZZZZ"]["message"]
    assert r.symbols["NVDA"]["freshness"] == "FRESH" and r.symbols["AMD"]["freshness"] == "FRESH"
    summary = _check(r, "market_data_summary")
    assert summary.details["symbols_with_data"] == 2 and summary.details["failed"] == ["ZZZZ"]
    assert r.exit_code == 2


def test_symbol_network_error_isolated(monkeypatch, env):
    fake = FakeAlpaca()
    fake.bar_errors["AMD"] = requests.ConnectionError("boom")
    r = _run(fake, monkeypatch, env)
    assert r.symbols["AMD"]["freshness"] == "DATA_ERROR" and "conectar" in r.symbols["AMD"]["message"]
    assert r.symbols["NVDA"]["freshness"] == "FRESH" and r.symbols["MARA"]["freshness"] == "FRESH"


def test_not_tradable_symbol_fails(monkeypatch, env):
    fake = FakeAlpaca()
    fake.assets["MARA"]["tradable"] = False
    r = _run(fake, monkeypatch, env)
    assert r.symbols["MARA"]["freshness"] == "NOT_TRADABLE" and r.symbols["MARA"]["level"] == "FAIL"


def test_no_data_returned(monkeypatch, env):
    fake = FakeAlpaca()
    fake.bars["NVDA"] = []
    r = _run(fake, monkeypatch, env)
    assert r.symbols["NVDA"]["freshness"] == "NO_DATA" and r.symbols["NVDA"]["level"] == "FAIL"
    assert r.symbols["AMD"]["freshness"] == "FRESH"

    for s in SYMS:
        fake.bars[s] = []
    r = _run(fake, monkeypatch, env)
    assert _check(r, "market_data_summary").level == "FAIL"
    assert "ningún símbolo" in _check(r, "market_data_summary").message


def test_uses_bot_data_path_newest_first(monkeypatch, env):
    """Las barras pasan por BrokerAlpaca.get_bars (sort=desc + reversa), igual que el bot."""
    seen = []
    fake = FakeAlpaca()

    def spy(url, headers=None, params=None, timeout=None):
        if url.endswith("/bars"):
            seen.append(params)
        return fake(url, headers=headers, params=params, timeout=timeout)

    _install(monkeypatch, spy)
    preflight.run_preflight(["NVDA"], "1Min", 120, now_fn=lambda: NOW, session_dir=env / "s")
    assert seen and seen[0]["sort"] == "desc" and seen[0]["limit"] == 120 and seen[0]["feed"] == "iex"


def test_keyboard_interrupt_is_not_caught(env):
    class Client:
        def get_account(self): return FakeAlpaca().account
        def get_clock(self): return FakeAlpaca().clock
        def get_positions(self): return []
        def get_open_orders(self): return []
        def get_asset(self, s): return {"tradable": True}
        def get_bars(self, *a): raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        preflight.run_preflight(["NVDA"], client_factory=Client, now_fn=lambda: NOW, session_dir=env / "s")


# ---------------- CLI / salida / secretos ----------------
def _live_fake():
    now = datetime.now(timezone.utc)
    return FakeAlpaca(is_open=True, now=now, bars_last=now.replace(second=0, microsecond=0) - timedelta(minutes=1))


def test_json_output_and_exit_code_0(monkeypatch, env, capsys):
    _install(monkeypatch, _live_fake())
    code = preflight.main(["--symbols", "nvda, amd", "--timeframe", "1Min", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["exit_code"] == 0 and out["overall"] in ("PASS", "WARN")
    assert out["config"]["symbols"] == ["NVDA", "AMD"] and out["config"]["lookback"] == 120
    assert set(out["symbols"]) == {"NVDA", "AMD"}
    assert {c["level"] for c in out["checks"]} <= {"PASS", "WARN", "FAIL", "INFO"}


def test_exit_code_2_on_fail(monkeypatch, env, capsys):
    _install(monkeypatch, _live_fake())
    assert preflight.main(["--symbols", "NVDA,ZZZZ"]) == 2
    text = capsys.readouterr().out
    assert "RESULTADO: FAIL" in text and "ZZZZ" in text


def test_secrets_never_appear_in_output(monkeypatch, env, capsys):
    fake = _live_fake()
    fake.account_status_code = 401
    fake.bar_errors["NVDA"] = RuntimeError(f"weird failure with {SECRET} and {KEY}")
    fake.bar_errors["AMD"] = requests.ConnectionError(f"proxy said {KEY}")
    _install(monkeypatch, fake)
    for argv in (["--symbols", "NVDA,AMD"], ["--symbols", "NVDA,AMD", "--json"]):
        preflight.main(argv)
        out = capsys.readouterr()
        blob = out.out + out.err
        assert SECRET not in blob and KEY not in blob
        assert "***" in blob or "conectar" in blob


def test_preflight_has_no_order_mutation_code():
    """Análisis estático (AST): ni imports de lógica de trading ni llamadas mutantes."""
    tree = ast.parse(inspect.getsource(preflight))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for mod in ("run_paper", "strategy", "ensemble", "risk_manager_avanzado", "execution_guards", "logger"):
        assert mod not in imported, mod
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for bad in ("post", "put", "patch", "delete", "place_order", "place_order_market", "cancel_open_orders",
                "assess_entry", "decide", "evaluate"):
        assert bad not in called, bad
    ro = [n for n in dir(preflight.ReadOnlyAlpaca) if not n.startswith("__")]
    assert set(ro) == {"_get", "get_account", "get_clock", "get_asset", "get_positions", "get_open_orders", "get_bars"}


def test_state_path_matches_run_paper():
    from src import run_paper
    assert ORIGINAL_STATE_PATH == run_paper.STATE_PATH
