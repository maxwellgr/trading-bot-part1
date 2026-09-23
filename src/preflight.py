# src/preflight.py
"""
Preflight / doctor de SOLO LECTURA antes de arrancar una sesión de paper.

    python -m src.preflight --symbols NVDA,AMD,PLTR --timeframe 1Min [--json]

Garantías:
- Nunca envía, modifica ni cancela órdenes: solo peticiones GET, a través de
  ReadOnlyAlpaca, que no expone ningún método mutante.
- No ejecuta estrategia, ensemble ni riesgo. No importa run_paper.
- Si el entorno no puede verificarse como PAPER, FAIL y no se contacta la red.
- No escribe archivos permanentes (la prueba de escritura usa un temporal
  que se borra solo). No importa src.logger (que crea logs/bot.log).
- Nunca imprime claves: todo texto de salida pasa por _redact().

Exit code: 0 si no hay ningún FAIL, 2 si hay al menos uno.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from . import structured_logger
from .analyze_session import parse_ts, stale_threshold_seconds, timeframe_to_seconds
from .broker_alpaca import BrokerAlpaca, _headers
from .config import settings

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_LEVEL_RANK = {INFO: 0, PASS: 0, WARN: 1, FAIL: 2}

PAPER_TRADING_HOST = "paper-api.alpaca.markets"
DATA_HOST = "data.alpaca.markets"
# Mismo archivo que run_paper.STATE_PATH (el test lo verifica); no se importa
# run_paper para no arrastrar src.logger.
STATE_PATH = Path("data") / "state.json"
MIN_PYTHON = (3, 10)  # el código usa anotaciones `X | None` evaluadas en tiempo de definición


# ---------------------------------------------------------------- modelo
@dataclass
class Check:
    id: str
    category: str
    level: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    symbols: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)

    def add(self, id: str, category: str, level: str, message: str, **details: Any) -> Check:
        c = Check(id, category, level, message, details)
        self.checks.append(c)
        return c

    @property
    def overall(self) -> str:
        worst = max((_LEVEL_RANK[c.level] for c in self.checks), default=0)
        return {0: PASS, 1: WARN, 2: FAIL}[worst]

    @property
    def exit_code(self) -> int:
        return 2 if any(c.level == FAIL for c in self.checks) else 0

    def to_dict(self) -> Dict[str, Any]:
        return {"overall": self.overall, "exit_code": self.exit_code, "config": self.config,
                "checks": [asdict(c) for c in self.checks], "symbols": self.symbols}


# ---------------------------------------------------------------- secretos / errores
def _secrets() -> List[str]:
    return [s for s in (settings.APCA_API_KEY_ID, settings.APCA_API_SECRET_KEY) if s and len(s) >= 4]


def _redact(text: str) -> str:
    for s in _secrets():
        text = text.replace(s, "***")
    return text


def describe_error(e: BaseException) -> str:
    """Mensaje legible y sin secretos para errores de red/API."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        r = e.response
        path = urlparse(getattr(r, "url", "") or "").path or "?"
        api_msg = ""
        try:
            body = r.json()
            if isinstance(body, dict) and body.get("message"):
                api_msg = f" — {body['message']}"
        except Exception:
            pass
        hint = {401: " (credenciales inválidas)", 403: " (acceso denegado: ¿claves de otro entorno o plan de datos?)",
                404: " (no encontrado)", 422: " (petición inválida)", 429: " (rate limit)"}.get(r.status_code, "")
        msg = f"HTTP {r.status_code}{hint} en {path}{api_msg}"
    elif isinstance(e, requests.Timeout):
        msg = "timeout contactando a Alpaca"
    elif isinstance(e, requests.ConnectionError):
        msg = "no se pudo conectar con Alpaca (red/DNS/firewall)"
    else:
        msg = f"{type(e).__name__}: {e}"
    return _redact(msg)


# ---------------------------------------------------------------- cliente de solo lectura
class ReadOnlyAlpaca:
    """
    Fachada GET-only. Para barras y cuenta delega en BrokerAlpaca (mismo
    camino de datos que usa el bot); reloj, assets y órdenes abiertas se
    consultan con GET directo. No existe ningún método que envíe, modifique
    o cancele órdenes.
    """

    def __init__(self) -> None:
        self._broker = BrokerAlpaca()
        self._base = settings.alpaca_base_url

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = requests.get(f"{self._base}{path}", headers=_headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_account(self) -> Dict[str, Any]:
        return self._broker.get_account()

    def get_clock(self) -> Dict[str, Any]:
        return self._get("/v2/clock")

    def get_asset(self, symbol: str) -> Dict[str, Any]:
        return self._get(f"/v2/assets/{symbol}")

    def get_positions(self) -> List[Dict[str, Any]]:
        return self._broker.get_positions()

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return self._get("/v2/orders", params={"status": "open", "limit": 500})

    def get_bars(self, symbol: str, timeframe: str, limit: int, start_iso: str) -> List[Dict[str, Any]]:
        return self._broker.get_bars(symbol, timeframe=timeframe, limit=limit, start_iso=start_iso)


# ---------------------------------------------------------------- checks locales
def _check_local(report: Report, session_dir: Path) -> None:
    v = sys.version_info
    report.add("python_version", "local", INFO if (v.major, v.minor) >= MIN_PYTHON else FAIL,
               f"Python {v.major}.{v.minor}.{v.micro}" + ("" if (v.major, v.minor) >= MIN_PYTHON
                                                        else f" (se requiere ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"))

    commit = structured_logger.get_git_commit()
    dirty = _git_dirty_count()
    msg = f"commit {commit or 'desconocido'}"
    if dirty:
        msg += f" (working tree con {dirty} archivo(s) modificado(s))"
    report.add("git", "local", INFO, msg, commit=commit, dirty_files=dirty)

    # Escribibilidad del directorio de sesiones sin dejar archivos: temporal auto-borrado
    target = session_dir if session_dir.exists() else next((p for p in session_dir.parents if p.exists()), Path("."))
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".preflight_", delete=True):
            pass
        note = "existe y es escribible" if session_dir.exists() else f"no existe aún; se creará (padre {target} escribible)"
        report.add("session_log_dir", "local", PASS, f"{session_dir}: {note}", path=str(session_dir))
    except OSError as e:
        report.add("session_log_dir", "local", FAIL, f"{session_dir}: no escribible ({type(e).__name__})", path=str(session_dir))

    if STATE_PATH.exists():
        try:
            book = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            report.add("persisted_positions", "local", INFO,
                       f"{STATE_PATH}: {len(book)} posición(es) persistida(s) {sorted(book) if book else ''}".rstrip()
                       + " — el bot las reconciliará contra el broker al arrancar", symbols=sorted(book))
        except Exception:
            report.add("persisted_positions", "local", WARN, f"{STATE_PATH} existe pero no es JSON válido; el bot lo ignorará")
    else:
        report.add("persisted_positions", "local", INFO, f"{STATE_PATH}: sin estado persistido")


def _git_dirty_count() -> Optional[int]:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5)
        return len([ln for ln in out.stdout.splitlines() if ln.strip()]) if out.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------- configuración / entorno
def _check_config(report: Report) -> bool:
    """Devuelve True si es seguro contactar la red (credenciales + entorno paper)."""
    missing = [n for n, v in (("APCA_API_KEY_ID", settings.APCA_API_KEY_ID),
                              ("APCA_API_SECRET_KEY", settings.APCA_API_SECRET_KEY)) if not v]
    if missing:
        report.add("credentials", "config", FAIL, f"Faltan credenciales: {', '.join(missing)} (.env o entorno)")
    else:
        report.add("credentials", "config", PASS, "API key y secret configurados (valores ocultos)")

    base = urlparse(settings.APCA_BASE_URL or "")
    paper_host = base.scheme == "https" and base.hostname == PAPER_TRADING_HOST
    if paper_host:
        report.add("paper_endpoint", "config", PASS, f"Trading endpoint es PAPER ({base.scheme}://{base.hostname})")
    else:
        live = base.hostname == "api.alpaca.markets"
        report.add("paper_endpoint", "config", FAIL,
                   f"Trading endpoint NO es paper: {base.scheme}://{base.hostname}"
                   + (" (¡LIVE!)" if live else " (no se puede verificar como paper)")
                   + " — no se contactará la red.", host=base.hostname)

    data = urlparse(settings.APCA_DATA_BASE_URL or "")
    if data.hostname != DATA_HOST or data.scheme != "https":
        report.add("data_endpoint", "config", FAIL, f"Data endpoint inesperado: {data.scheme}://{data.hostname}", host=data.hostname)
    elif data.path.rstrip("/") != "/v2":
        # get_bars construye {data_base}/stocks/...: sin /v2 cada petición de barras da 404.
        report.add("data_endpoint", "config", FAIL,
                   f"APCA_DATA_BASE_URL debe terminar en /v2 (actual: '{data.path or '/'}'); ej. https://{DATA_HOST}/v2")
    else:
        report.add("data_endpoint", "config", PASS, f"Data endpoint https://{DATA_HOST}/v2 (feed iex)")

    return not missing and paper_host


# ---------------------------------------------------------------- cuenta / reloj
def _check_account(report: Report, client: Any) -> bool:
    try:
        acct = client.get_account()
    except Exception as e:
        report.add("account", "alpaca", FAIL, f"No se pudo obtener la cuenta: {describe_error(e)}")
        report.add("paper_account", "alpaca", FAIL, "No se pudo confirmar la cuenta paper (cuenta inaccesible)")
        return False

    number = str(acct.get("account_number") or "")
    masked = (number[:2] + "…" + number[-3:]) if len(number) > 5 else ("…" if number else None)
    # La cuenta respondió en el host de paper: las claves son de paper.
    report.add("paper_account", "alpaca", PASS,
               f"Cuenta accesible en {PAPER_TRADING_HOST} (claves de paper)"
               + (f"; nº {masked}" if masked else ""),
               account_number_prefix_pa=number.upper().startswith("PA") if number else None)

    status = acct.get("status")
    report.add("account_status", "alpaca", PASS if status == "ACTIVE" else FAIL, f"Estado de cuenta: {status}", status=status)
    for flag, label in (("trading_blocked", "trading bloqueado"), ("account_blocked", "cuenta bloqueada"),
                        ("trade_suspended_by_user", "trading suspendido por el usuario")):
        if flag in acct:
            blocked = bool(acct.get(flag))
            report.add(flag, "alpaca", FAIL if blocked else PASS, f"{label}: {'SÍ' if blocked else 'no'}")

    money = {k: _to_float(acct.get(k)) for k in ("equity", "buying_power", "cash", "last_equity")}
    shown = ", ".join(f"{k}={v:,.2f}" for k, v in money.items() if v is not None)
    equity = money.get("equity")
    level = WARN if equity is not None and equity <= 0 else INFO
    report.add("account_balances", "alpaca", level, shown or "saldos no disponibles", **money,
               pattern_day_trader=acct.get("pattern_day_trader"), shorting_enabled=acct.get("shorting_enabled"))
    return True


def _check_clock(report: Report, client: Any, now: datetime) -> Optional[bool]:
    try:
        clock = client.get_clock()
    except Exception as e:
        report.add("market_clock", "market", WARN, f"No se pudo leer el reloj de mercado: {describe_error(e)}")
        return None
    is_open = bool(clock.get("is_open"))
    report.add("market_clock", "market", INFO,
               f"Mercado {'ABIERTO' if is_open else 'CERRADO'} | hora Alpaca {clock.get('timestamp')} | "
               f"próxima apertura {clock.get('next_open')} | próximo cierre {clock.get('next_close')}",
               is_open=is_open, timestamp=clock.get("timestamp"),
               next_open=clock.get("next_open"), next_close=clock.get("next_close"))
    server = parse_ts(clock.get("timestamp"))
    if server is not None:
        skew = (now - server).total_seconds()
        # La guarda de frescura compara contra el reloj LOCAL: un desfase grande la distorsiona.
        report.add("clock_skew", "market", WARN if abs(skew) > 30 else PASS,
                   f"Desfase reloj local vs Alpaca: {skew:+.1f}s", skew_seconds=round(skew, 1))
    return is_open


def _check_exposure(report: Report, client: Any) -> None:
    try:
        positions = client.get_positions()
        syms = sorted(str(p.get("symbol")) for p in positions)
        report.add("open_positions", "alpaca", INFO, f"{len(positions)} posición(es) abierta(s) {syms if syms else ''}".rstrip(),
                   symbols=syms)
    except Exception as e:
        report.add("open_positions", "alpaca", WARN, f"No se pudieron leer posiciones: {describe_error(e)}")
    try:
        orders = client.get_open_orders()
        report.add("open_orders", "alpaca", INFO, f"{len(orders)} orden(es) abierta(s)", count=len(orders))
    except Exception as e:
        report.add("open_orders", "alpaca", WARN, f"No se pudieron leer órdenes abiertas: {describe_error(e)}")


# ---------------------------------------------------------------- datos de mercado
def _check_symbol(client: Any, symbol: str, timeframe: str, lookback: int, start_iso: str,
                  now: datetime, market_open: Optional[bool], threshold: Optional[float]) -> Dict[str, Any]:
    res: Dict[str, Any] = {"symbol": symbol, "tradable": None, "bars": 0, "latest_bar_timestamp": None,
                           "latest_close": None, "bar_age_seconds": None, "freshness": None,
                           "level": FAIL, "message": ""}
    try:
        asset = client.get_asset(symbol)
        res["tradable"] = bool(asset.get("tradable"))
        res["asset_status"] = asset.get("status")
    except Exception as e:
        res["message"] = f"asset no disponible: {describe_error(e)}"
        res["freshness"] = "INVALID_SYMBOL"
        return res
    if not res["tradable"]:
        res["message"] = f"no es tradable (status={res.get('asset_status')}); el bot lo omitiría en cada tick"
        res["freshness"] = "NOT_TRADABLE"
        return res

    try:
        bars = client.get_bars(symbol, timeframe, lookback, start_iso)
    except Exception as e:
        res["message"] = f"error pidiendo barras: {describe_error(e)}"
        res["freshness"] = "DATA_ERROR"
        return res
    res["bars"] = len(bars)
    if not bars:
        res["message"] = f"sin barras en la ventana consultada (desde {start_iso})"
        res["freshness"] = "NO_DATA"
        return res

    last = bars[-1]  # get_bars devuelve orden cronológico: la última es la más reciente
    ts = parse_ts(last.get("t"))
    res["latest_bar_timestamp"] = ts.isoformat() if ts else last.get("t")
    res["latest_close"] = _to_float(last.get("c"))
    age = (now - ts).total_seconds() if ts else None
    res["bar_age_seconds"] = round(age, 1) if age is not None else None

    if market_open is None:
        res.update(level=WARN, freshness="UNKNOWN_MARKET_STATE",
                   message="estado del mercado desconocido: no se puede juzgar la frescura")
    elif not market_open:
        res.update(level=INFO, freshness="CLOSED_MARKET", message="mercado cerrado: una vela antigua es normal")
    elif age is None or threshold is None:
        res.update(level=WARN, freshness="UNKNOWN", message="no se pudo calcular la edad de la vela")
    elif age <= threshold:
        res.update(level=PASS, freshness="FRESH", message=f"vela fresca (umbral {threshold:.0f}s)")
    else:
        res.update(level=WARN, freshness="STALE",
                   message=f"mercado abierto pero la última vela tiene {age:.0f}s (> {threshold:.0f}s): "
                           f"la guarda STALE_DATA bloquearía sus señales")
    return res


def _check_market_data(report: Report, client: Any, symbols: List[str], timeframe: str, lookback: int,
                       history_days: int, now: datetime, market_open: Optional[bool]) -> None:
    threshold = stale_threshold_seconds(timeframe)
    start_iso = (now - timedelta(days=history_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    for sym in symbols:
        try:
            res = _check_symbol(client, sym, timeframe, lookback, start_iso, now, market_open, threshold)
        except Exception as e:  # nunca dejar que un símbolo tumbe a los demás (KeyboardInterrupt no es Exception)
            res = {"symbol": sym, "level": FAIL, "freshness": "ERROR", "message": describe_error(e), "bars": 0}
        res["message"] = _redact(res["message"])
        report.symbols[sym] = res
        report.add(f"data:{sym}", "market_data", res["level"], f"{sym}: {res['message']}",
                   **{k: v for k, v in res.items() if k not in ("symbol", "level", "message")})

    with_data = [s for s, r in report.symbols.items() if r.get("bars")]
    fresh = [s for s in with_data if report.symbols[s].get("freshness") == "FRESH"]
    stale = [s for s in with_data if report.symbols[s].get("freshness") == "STALE"]
    failed = [s for s, r in report.symbols.items() if r.get("level") == FAIL]
    if not with_data:
        level, msg = FAIL, "ningún símbolo devolvió datos"
    elif market_open and stale and not fresh:
        level, msg = FAIL, "mercado abierto y TODOS los símbolos con datos están obsoletos (feed congelado?)"
    elif failed or stale:
        level, msg = WARN, f"{len(with_data)}/{len(symbols)} con datos"
    else:
        level, msg = PASS, f"{len(with_data)}/{len(symbols)} con datos"
    report.add("market_data_summary", "market_data", level,
               msg + (f" | fallidos: {', '.join(failed)}" if failed else "") + (f" | obsoletos: {', '.join(stale)}" if stale else ""),
               symbols_with_data=len(with_data), failed=failed, stale=stale, fresh=fresh)


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- orquestación
def run_preflight(symbols: List[str], timeframe: str = "1Min", lookback: int = 120, history_days: int = 7,
                  client_factory: Callable[[], Any] = ReadOnlyAlpaca,
                  now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                  session_dir: Optional[Path] = None) -> Report:
    report = Report()
    report.config = {"symbols": symbols, "timeframe": timeframe, "lookback": lookback, "history_days": history_days,
                     "stale_threshold_seconds": stale_threshold_seconds(timeframe)}

    _check_local(report, session_dir or structured_logger.SESSIONS_DIR)
    if timeframe_to_seconds(timeframe) is None:
        report.add("timeframe", "config", FAIL, f"Timeframe no reconocido: {timeframe!r} (ej. 1Min, 5Min, 1Hour, 1Day)")
    else:
        report.add("timeframe", "config", INFO,
                   f"Símbolos: {', '.join(symbols)} | timeframe {timeframe} | lookback {lookback} | "
                   f"umbral de obsolescencia {stale_threshold_seconds(timeframe):.0f}s")
    if not symbols:
        report.add("symbols", "config", FAIL, "No se indicaron símbolos")

    network_ok = _check_config(report)
    if not network_ok:
        report.add("network_checks", "alpaca", INFO,
                   "Checks de red OMITIDOS: el entorno no está verificado como paper o faltan credenciales")
        return report

    try:
        client = client_factory()
    except Exception as e:
        report.add("client", "alpaca", FAIL, f"No se pudo inicializar el cliente: {describe_error(e)}")
        return report

    _check_account(report, client)
    now = now_fn()
    market_open = _check_clock(report, client, now)
    _check_exposure(report, client)
    if symbols and timeframe_to_seconds(timeframe) is not None:
        _check_market_data(report, client, symbols, timeframe, lookback, history_days, now_fn(), market_open)
    return report


# ---------------------------------------------------------------- salida
def format_report(report: Report) -> str:
    L: List[str] = ["=== Preflight paper trading (solo lectura) ==="]
    current = None
    names = {"config": "Configuración", "local": "Local", "alpaca": "Alpaca", "market": "Mercado",
             "market_data": "Datos de mercado"}
    for c in report.checks:
        if c.category != current:
            current = c.category
            L.append(f"\n-- {names.get(current, current)} --")
        L.append(f"[{c.level:<4}] {c.message}")
    if report.symbols:
        L.append("")
        L.append(f"{'símbolo':<7} {'estado':<20} {'barras':>6} {'última vela (UTC)':<26} {'close':>10} {'edad':>8}")
        for s, r in report.symbols.items():
            close = f"{r['latest_close']:.2f}" if r.get("latest_close") is not None else "-"
            age = f"{r['bar_age_seconds']:.0f}s" if r.get("bar_age_seconds") is not None else "-"
            L.append(f"{s:<7} {str(r.get('freshness')):<20} {r.get('bars', 0):>6} "
                     f"{str(r.get('latest_bar_timestamp') or '-'):<26} {close:>10} {age:>8}")
    n = {lvl: sum(c.level == lvl for c in report.checks) for lvl in (PASS, WARN, FAIL, INFO)}
    L.append(f"\nRESULTADO: {report.overall}  (PASS={n[PASS]} WARN={n[WARN]} FAIL={n[FAIL]} INFO={n[INFO]})")
    return _redact("\n".join(L))


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Preflight de SOLO LECTURA antes de una sesión de paper trading.")
    p.add_argument("--symbols", type=str, required=True, help="Lista separada por comas, ej. NVDA,AMD,PLTR")
    p.add_argument("--timeframe", type=str, default="1Min")
    p.add_argument("--lookback", type=int, default=120, help="Barras a pedir por símbolo (igual que run_paper)")
    p.add_argument("--history-days", type=int, default=7,
                   help="Ventana hacia atrás para encontrar la última vela (cubre fines de semana/feriados)")
    p.add_argument("--json", action="store_true", help="Salida JSON legible por máquina")
    args = p.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    report = run_preflight(symbols, args.timeframe, args.lookback, args.history_days)
    if args.json:
        print(_redact(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str)))
    else:
        print(format_report(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
