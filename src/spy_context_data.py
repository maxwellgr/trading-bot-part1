# src/spy_context_data.py
"""
STRATEGY_V2_HYPOTHESIS_004 — preparación y auditoría de los datos de CONTEXTO SPY (sin implementar H004).
Spec congelado: research/strategy_v2_hypothesis_004.md (commit 31526c27436e53780cae865dde991f2fd9df01a5).

Orden obligatorio (cada paso exige el anterior):
    python -m src.spy_context_data sessions   # 1. lista congelada de sesiones requeridas (solo caché de acciones)
    python -m src.spy_context_data download   # 2. SPY 1Min IEX raw 2023-12-01 -> 2025-12-31 (nunca 2026)
    python -m src.spy_context_data audit      # 3. auditoría cruda + 15Min + soporte + cobertura + manifiesto

- SPY es solo contexto: se guarda en data/context_spy/1Min/SPY.csv, separado de la caché de acciones.
- Nunca se descarga ni se lee SPY posterior a 2025-12-31 (guardia sin bypass). Validación/known/forward: nunca.
- No calcula ningún resultado de trading de H004.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from . import historical_download as hd
from .historical_audit import (EARLY_CLOSE_MIN, EARLY_CLOSES, NYSE_HOLIDAYS, RTH_CLOSE_MIN, RTH_OPEN_MIN,
                               integrity, read_raw, sha256_file)
from .historical_data import symbol_path, validate_bars

HYPOTHESIS_ID = "STRATEGY_V2_HYPOTHESIS_004"
FROZEN_SPEC_COMMIT = "31526c27436e53780cae865dde991f2fd9df01a5"
NY = "America/New_York"
CONTEXT_SYMBOL = "SPY"
TRADABLE = ("NVDA", "AMD", "PLTR", "HOOD", "MARA", "INTC", "MU", "META")
DEV_START = date(2024, 1, 2)
DEV_END = date(2025, 12, 31)
DOWNLOAD_START = date(2023, 12, 1)
MAX_SPY_DATE = DEV_END              # guardia dura: nada de 2026 (Q3 opción A)
BUCKET_MINUTES = 15
SUPPORT_BARS = 200
SLOPE_BARS = 3
REQUIRED_SESSION_DEFINITION = ("Any Development calendar date for which at least one of the eight tradable symbols "
                               "has at least one valid regular-session bar in the existing stock cache.")
DEFAULT_STOCK_DIR = Path("data") / "historical"
DEFAULT_SPY_DIR = Path("data") / "context_spy"
DEFAULT_STOCK_MANIFEST = Path("research") / "historical_manifest_v1.json"
DEFAULT_MANIFEST = Path("research") / "context_manifest_spy_v1.json"
DEFAULT_AUDIT_DIR = Path("data") / "research_v1" / "h004_spy_context_audit"
SESSIONS_FILE = "required_sessions_development_v1.json"


class SpyContextError(RuntimeError):
    """Fallo duro de preparación/auditoría de SPY: detener H004."""


# ================================================================ utilidades de sesión
def close_minute(d: date) -> int:
    return EARLY_CLOSE_MIN if d in EARLY_CLOSES else RTH_CLOSE_MIN


def rth_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """Velas 1Min cuyo INICIO cae en [09:30, cierre) NY en día hábil (cierre 13:00 en EARLY_CLOSES)."""
    ny = index.tz_convert(NY)
    minute = ny.hour * 60 + ny.minute
    close = np.array([close_minute(d) for d in ny.date], dtype=int)
    return np.asarray((ny.weekday < 5) & (minute >= RTH_OPEN_MIN) & (minute < close))


def expected_bucket_starts(d: date, minutes: int = BUCKET_MINUTES) -> List[pd.Timestamp]:
    """Inicios (UTC) de las cubetas RTH esperadas de la sesión d, ancladas a 09:30."""
    base = pd.Timestamp(d).tz_localize(NY) + pd.Timedelta(minutes=RTH_OPEN_MIN)
    n = (close_minute(d) - RTH_OPEN_MIN) // minutes
    return [(base + pd.Timedelta(minutes=minutes * k)).tz_convert("UTC") for k in range(n)]


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch en nanosegundos, independiente de la unidad del índice (us/ns)."""
    return index.as_unit("ns").asi8


def dates_hash(dates: Sequence[date]) -> str:
    """sha256 de las fechas ISO ordenadas unidas por '\\n' (determinista)."""
    return hashlib.sha256("\n".join(d.isoformat() for d in sorted(dates)).encode("ascii")).hexdigest()


# ================================================================ 1. sesiones requeridas (solo acciones)
def stock_rth_dates(df1: pd.DataFrame, start: date, end: date) -> set:
    idx = df1.index
    ny_dates = idx.tz_convert(NY).date
    m = rth_mask(idx) & np.array([start <= d <= end for d in ny_dates], dtype=bool)
    return set(np.asarray(ny_dates)[m].tolist())


def required_sessions(stock_dir: Path, symbols: Sequence[str] = TRADABLE,
                      start: Optional[date] = None, end: Optional[date] = None,
                      loader: Optional[Callable[[str], pd.DataFrame]] = None) -> Dict[str, Any]:
    """
    Q8: fecha de desarrollo con >= 1 vela RTH válida de cualquiera de los 8 símbolos en la caché de acciones.
    Solo usa la caché de acciones (validate_bars). Las fechas excluidas se reportan contra los días hábiles
    no feriados de la lista estática del proyecto (NYSE_HOLIDAYS); esa lista NUNCA añade fechas requeridas.
    """
    def default_loader(sym: str) -> pd.DataFrame:
        path = symbol_path(stock_dir, "1Min", sym)
        return validate_bars(read_raw(path), sym, str(path))

    start, end = start or DEV_START, end or DEV_END
    load = loader or default_loader
    per_symbol: Dict[str, int] = {}
    req: set = set()
    for sym in symbols:
        ds = stock_rth_dates(load(sym), start, end)
        per_symbol[sym] = len(ds)
        req |= ds
    dates = sorted(req)
    weekdays = [start + pd.Timedelta(days=k).to_pytimedelta() for k in range((end - start).days + 1)]
    business = [d for d in weekdays if d.weekday() < 5 and d not in NYSE_HOLIDAYS]
    excluded = [d for d in business if d not in req]
    return {
        "definition": REQUIRED_SESSION_DEFINITION,
        "tradable_symbols": list(symbols),
        "range": [start.isoformat(), end.isoformat()],
        "required_session_count": len(dates),
        "first_required_date": dates[0].isoformat() if dates else None,
        "last_required_date": dates[-1].isoformat() if dates else None,
        "required_session_dates": [d.isoformat() for d in dates],
        "required_session_dates_sha256": dates_hash(dates),
        "hash_method": "sha256 of ISO dates (YYYY-MM-DD), ascending, joined by '\\n', ASCII, no trailing newline",
        "excluded_zero_stock_coverage_dates": [d.isoformat() for d in excluded],
        "excluded_method": ("non-holiday weekdays in the range (project static NYSE_HOLIDAYS list) with zero regular-session "
                            "bars across all eight symbols; reporting only, the list never adds required dates"),
        "required_dates_on_listed_holidays": [d.isoformat() for d in dates if d in NYSE_HOLIDAYS],
        "sessions_per_symbol": per_symbol,
    }


def stock_cache_check(stock_dir: Path, manifest_path: Path, symbols: Sequence[str] = TRADABLE) -> Dict[str, Any]:
    """Confirma que la caché de acciones es la del manifiesto histórico (sha256), sin modificar nada."""
    man = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    by_sym = {f["symbol"]: f for f in man["files"]}
    out = {}
    for s in symbols:
        got = sha256_file(symbol_path(stock_dir, "1Min", s))
        out[s] = {"sha256": got, "matches_historical_manifest": got == by_sym[s]["sha256"]}
    return {"files": out, "all_match": all(v["matches_historical_manifest"] for v in out.values())}


def freeze_sessions(stock_dir: Path, spy_dir: Path, stock_manifest: Path) -> Dict[str, Any]:
    """Paso 1: deriva la lista dos veces (determinismo), la congela ANTES de cualquier acceso a SPY."""
    if symbol_path(spy_dir, "1Min", CONTEXT_SYMBOL).exists():
        raise SpyContextError("SPY ya existe localmente: la lista de sesiones debe congelarse ANTES de acceder a SPY")
    cache = stock_cache_check(stock_dir, stock_manifest)
    if not cache["all_match"]:
        raise SpyContextError(f"la caché de acciones no coincide con {stock_manifest}: {cache['files']}")
    a = required_sessions(stock_dir)
    b = required_sessions(stock_dir)
    if a != b:
        raise SpyContextError("la derivación de sesiones requeridas no es determinista")
    frozen = dict(a, stock_cache=cache, frozen_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  derived_before_spy_access=True, determinism_check="two independent derivations identical")
    p = Path(spy_dir) / SESSIONS_FILE
    if p.exists():
        old = json.loads(p.read_text(encoding="utf-8"))
        if old["required_session_dates_sha256"] != a["required_session_dates_sha256"]:
            raise SpyContextError(f"{p} ya existe con otra lista congelada; no se sobrescribe")
        return old
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(frozen, indent=2), encoding="utf-8")
    return frozen


def load_frozen_sessions(spy_dir: Path) -> Dict[str, Any]:
    p = Path(spy_dir) / SESSIONS_FILE
    if not p.is_file():
        raise SpyContextError(f"falta {p}: ejecuta primero el paso 'sessions' (antes de acceder a SPY)")
    return json.loads(p.read_text(encoding="utf-8"))


# ================================================================ 2. descarga (guardada)
def check_spy_range(start: date, end: date) -> None:
    """Sin bypass: SPY solo 2023-12-01 -> 2025-12-31 hasta que H004 pase desarrollo y se congele (spec §5)."""
    if start < DOWNLOAD_START or end > MAX_SPY_DATE or start > end:
        raise SpyContextError(f"rango SPY {start}..{end} no permitido: solo {DOWNLOAD_START}..{MAX_SPY_DATE} "
                              "(los datos SPY de validación permanecen sin abrir)")


def download_spy(spy_dir: Path, get: Optional[Callable] = None, data_url: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Paso 2: GET /v2/stocks/SPY/bars 1Min feed=iex adjustment=raw. Se guarda tal cual (la auditoría valida)."""
    load_frozen_sessions(spy_dir)  # exige la lista congelada antes de tocar SPY
    if hd.FEED != "iex" or hd.ADJUSTMENT != "raw":
        raise SpyContextError(f"fuente no permitida: feed={hd.FEED} adjustment={hd.ADJUSTMENT}")
    check_spy_range(DOWNLOAD_START, MAX_SPY_DATE)
    path = symbol_path(spy_dir, "1Min", CONTEXT_SYMBOL)
    if path.exists():
        raise SpyContextError(f"{path} ya existe: no se sobrescriben datos de contexto")
    if get is None:
        import requests
        from .config import settings
        get = requests.get
        data_url = settings.alpaca_data_url
        headers = {"APCA-API-KEY-ID": settings.alpaca_api_key or "",
                   "APCA-API-SECRET-KEY": settings.alpaca_api_secret or ""}
    s_utc, e_utc = hd.date_range_utc(DOWNLOAD_START.isoformat(), MAX_SPY_DATE.isoformat())
    e_utc = e_utc - pd.Timedelta(seconds=1)  # fin inclusivo 2025-12-31 23:59:59 NY: nunca 2026
    bars = hd.fetch_bars(CONTEXT_SYMBOL, "1Min", s_utc, e_utc, get, data_url, headers or {})
    df = hd.bars_to_frame(CONTEXT_SYMBOL, bars)
    if df.empty:
        raise SpyContextError("la API no devolvió velas SPY IEX")
    ny_dates = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").dt.tz_convert(NY).dt.date
    if ny_dates.min() < DOWNLOAD_START or ny_dates.max() > MAX_SPY_DATE:
        raise SpyContextError(f"la respuesta sale del rango permitido ({ny_dates.min()}..{ny_dates.max()}); no se guarda")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)
    req = {"endpoint": f"GET {data_url}/stocks/{CONTEXT_SYMBOL}/bars", "symbol": CONTEXT_SYMBOL, "timeframe": "1Min",
           "feed": hd.FEED, "adjustment": hd.ADJUSTMENT, "sort": "asc",
           "start_utc": s_utc.isoformat(), "end_utc": e_utc.isoformat(),
           "requested_range_ny": [DOWNLOAD_START.isoformat(), MAX_SPY_DATE.isoformat()],
           "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": int(len(df))}
    (Path(spy_dir) / "download_request.json").write_text(json.dumps(req, indent=2), encoding="utf-8")
    return req


# ================================================================ 3. auditoría
def resample_rth(df1: pd.DataFrame, minutes: int = BUCKET_MINUTES) -> pd.DataFrame:
    """
    Misma convención que strategy_v2_h001.resample_rth_5min, parametrizada (spec H004 §6): solo velas 1Min cuyo
    INICIO cae en [09:30, cierre) NY; cubetas [t, t+minutes) ancladas a 09:30; first/max/min/last/sum;
    cubeta vacía = sin vela (>= 1 vela 1Min válida crea la cubeta; sin mínimo). Sin interpolación.
    """
    cols = ["open", "high", "low", "close", "volume", "n_minutes"]
    empty = pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
    if df1.empty:
        return empty
    rth = df1[rth_mask(df1.index)]
    if rth.empty:
        return empty
    ny = rth.index.tz_convert(NY)
    minute = ny.hour * 60 + ny.minute
    offset = (minute - RTH_OPEN_MIN) // minutes * minutes + RTH_OPEN_MIN - minute  # <= 0
    bucket = (ny + pd.to_timedelta(np.asarray(offset), unit="min")).floor("min").tz_convert("UTC")
    g = rth.groupby(bucket)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(),
                        "n_minutes": g["close"].size().astype(int)})
    out.index = pd.DatetimeIndex(out.index, name="timestamp")
    return out[cols]


def audit_raw(path: Path) -> Dict[str, Any]:
    raw = read_raw(path)
    integ = integrity(raw)
    syms = sorted(set(raw["symbol"].astype(str).str.upper()))
    ts = pd.to_datetime(raw["timestamp"], utc=True, format="ISO8601", errors="coerce")
    num = raw[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    nonfinite = int((~np.isfinite(num)).sum())
    ny_dates = ts.dropna().dt.tz_convert(NY).dt.date
    ok = (integ["duplicate_timestamps"] == 0 and integ["non_monotonic_timestamps"] == 0
          and integ["impossible_ohlc_bars"] == 0 and integ["non_positive_prices"] == 0
          and integ["negative_volume"] == 0 and integ["null_or_nan_total"] == 0 and nonfinite == 0
          and syms == [CONTEXT_SYMBOL] and len(raw) > 0
          and ny_dates.min() >= DOWNLOAD_START and ny_dates.max() <= MAX_SPY_DATE)
    return {"path": str(path).replace("\\", "/"), "sha256": sha256_file(path), "file_size_bytes": Path(path).stat().st_size,
            "row_count": int(len(raw)), "symbols": syms,
            "first_timestamp": ts.min().isoformat() if len(raw) else None,
            "last_timestamp": ts.max().isoformat() if len(raw) else None,
            "first_date_ny": ny_dates.min().isoformat(), "last_date_ny": ny_dates.max().isoformat(),
            **integ, "non_finite_values": nonfinite,
            "within_allowed_range": bool(ny_dates.min() >= DOWNLOAD_START and ny_dates.max() <= MAX_SPY_DATE),
            "integrity_pass": bool(ok)}


def audit_15min(b15: pd.DataFrame) -> Dict[str, Any]:
    idx = b15.index
    ny = idx.tz_convert(NY)
    minute = np.asarray(ny.hour * 60 + ny.minute)
    dates = np.asarray(ny.date)
    close = np.array([close_minute(d) for d in dates], dtype=int)
    anchored = ((minute - RTH_OPEN_MIN) % BUCKET_MINUTES == 0) & (minute >= RTH_OPEN_MIN) \
        & (minute + BUCKET_MINUTES <= close) & (np.asarray(ny.second) == 0)
    o, h, l, c, v = (b15[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    bad_ohlc = (l > o) | (o > h) | (l > c) | (c > h) | (l > h)
    ns = _ns(idx)
    early = np.array([d in EARLY_CLOSES for d in dates], dtype=bool)
    early_dates = sorted({d for d in dates if d in EARLY_CLOSES})
    last_early = {d.isoformat(): (pd.Timestamp(idx[dates == d][-1]).tz_convert(NY).strftime("%H:%M")) for d in early_dates}
    res = {"row_count": int(len(b15)), "monotonic_increasing": bool((np.diff(ns) > 0).all()),
           "duplicate_timestamps": int(pd.Index(ns).duplicated().sum()),
           "impossible_ohlc_bars": int(bad_ohlc.sum()), "non_positive_prices": int(((np.minimum.reduce([o, h, l, c])) <= 0).sum()),
           "negative_volume": int((v < 0).sum()), "non_anchored_buckets": int((~anchored).sum()),
           "n_minutes_min": int(b15["n_minutes"].min()), "n_minutes_max": int(b15["n_minutes"].max()),
           "early_close_sessions": [d.isoformat() for d in early_dates],
           "buckets_after_13_on_early_close": int((early & (minute >= EARLY_CLOSE_MIN)).sum()),
           "last_bucket_start_on_early_close": last_early}
    res["pass"] = bool(res["monotonic_increasing"] and res["duplicate_timestamps"] == 0 and res["impossible_ohlc_bars"] == 0
                       and res["non_positive_prices"] == 0 and res["negative_volume"] == 0
                       and res["non_anchored_buckets"] == 0 and res["buckets_after_13_on_early_close"] == 0
                       and 1 <= res["n_minutes_min"] and res["n_minutes_max"] <= BUCKET_MINUTES)
    return res


def support_bars(b15: pd.DataFrame, start: Optional[date] = None) -> Dict[str, Any]:
    start = start or DEV_START
    s_utc = pd.Timestamp(start).tz_localize(NY).tz_convert("UTC")
    before = b15[b15.index < s_utc]
    sup = before.iloc[-SUPPORT_BARS:]
    return {"required": SUPPORT_BARS, "available_before_development": int(len(before)), "support_bars": int(len(sup)),
            "first_support_timestamp": sup.index[0].isoformat() if len(sup) else None,
            "last_support_timestamp": sup.index[-1].isoformat() if len(sup) else None,
            "first_support_timestamp_ny": sup.index[0].tz_convert(NY).isoformat() if len(sup) else None,
            "last_support_timestamp_ny": sup.index[-1].tz_convert(NY).isoformat() if len(sup) else None,
            "pass": bool(len(sup) == SUPPORT_BARS)}


def session_coverage(df1: pd.DataFrame, b15: pd.DataFrame, required: Sequence[date]) -> Dict[str, Any]:
    """Velas 1Min RTH y cubetas 15Min esperadas/existentes por sesión requerida."""
    rth = df1[rth_mask(df1.index)]
    n1 = pd.Series(1, index=rth.index).groupby(np.asarray(rth.index.tz_convert(NY).date)).size()
    have = set(_ns(b15.index).tolist())
    rows = []
    for d in required:
        exp = expected_bucket_starts(d)
        miss = [t for t in exp if t.value not in have]
        rows.append({"date": d.isoformat(), "early_close": d in EARLY_CLOSES,
                     "expected_rth_minutes": close_minute(d) - RTH_OPEN_MIN, "spy_rth_1min_bars": int(n1.get(d, 0)),
                     "expected_15min_buckets": len(exp), "existing_15min_buckets": len(exp) - len(miss),
                     "missing_15min_buckets": len(miss),
                     "missing_bucket_starts_ny": [t.tz_convert(NY).strftime("%H:%M") for t in miss]})
    zero = [r["date"] for r in rows if r["spy_rth_1min_bars"] == 0]
    partial = [r for r in rows if r["spy_rth_1min_bars"] > 0 and r["missing_15min_buckets"] > 0]
    present = [r for r in rows if r["spy_rth_1min_bars"] > 0]
    req_set = set(required)
    spy_dates = sorted(set(np.asarray(rth.index.tz_convert(NY).date).tolist()))
    dev_non_required = [d.isoformat() for d in spy_dates if DEV_START <= d <= DEV_END and d not in req_set]
    return {"rows": rows,
            "required_sessions_with_zero_spy_data": zero,
            "required_sessions_with_partial_spy_gaps": [{"date": r["date"], "missing_15min_buckets": r["missing_15min_buckets"],
                                                         "missing_bucket_starts_ny": r["missing_bucket_starts_ny"]} for r in partial],
            "expected_buckets": int(sum(r["expected_15min_buckets"] for r in rows)),
            "existing_buckets": int(sum(r["existing_15min_buckets"] for r in rows)),
            "missing_buckets": int(sum(r["missing_15min_buckets"] for r in rows)),
            "sessions_with_missing_buckets": len(partial) + len(zero),
            "max_missing_buckets_in_one_present_session": int(max([r["missing_15min_buckets"] for r in present], default=0)),
            "spy_1min_rth_bars_in_required_sessions": int(sum(r["spy_rth_1min_bars"] for r in rows)),
            "expected_rth_minutes_in_required_sessions": int(sum(r["expected_rth_minutes"] for r in rows)),
            "min_spy_rth_1min_bars_in_present_session": int(min([r["spy_rth_1min_bars"] for r in present], default=0)),
            "development_dates_with_spy_but_not_required": dev_non_required}


def slope_gap_flags(b15: pd.DataFrame, expected_dates: Sequence[date]) -> np.ndarray:
    """
    Por vela existente k (k >= SLOPE_BARS): 1 si la ventana R−3..R de velas EXISTENTES salta al menos una cubeta
    esperada ausente (o toca una cubeta fuera de la línea de tiempo esperada), 0 si no; -1 si k < SLOPE_BARS.
    Línea de tiempo esperada = cubetas RTH de `expected_dates`. Solo diagnóstico (Q2): nunca afecta elegibilidad.
    """
    timeline = [t.value for d in sorted(expected_dates) for t in expected_bucket_starts(d)]
    pos = {v: k for k, v in enumerate(timeline)}
    p = np.array([pos.get(v, -1) for v in _ns(b15.index)], dtype=np.int64)
    out = np.full(len(p), -1, dtype=np.int64)
    for k in range(SLOPE_BARS, len(p)):
        w = p[k - SLOPE_BARS:k + 1]
        out[k] = int(bool((w < 0).any() or (np.diff(w) != 1).any()))
    return out


def slope_windows_spanning_missing(b15: pd.DataFrame, expected_dates: Sequence[date],
                                   start: Optional[date] = None, end: Optional[date] = None) -> Dict[str, Any]:
    """
    Diagnóstico (Q2; nunca afecta elegibilidad): ventanas R−3..R de velas 15Min EXISTENTES, con R en desarrollo,
    que saltan al menos una cubeta esperada ausente. La línea de tiempo esperada son las cubetas RTH de
    `expected_dates` (misma regla de cobertura de acciones). Se cuenta a nivel de cubeta (cada vela existente como R
    posible); el conteo por señal se reporta en la corrida de desarrollo de H004.
    """
    start, end = start or DEV_START, end or DEV_END
    timeline = {t.value for d in expected_dates for t in expected_bucket_starts(d)}
    flags = slope_gap_flags(b15, expected_dates)
    ns = _ns(b15.index)
    s_utc = pd.Timestamp(start).tz_localize(NY).tz_convert("UTC").value
    e_utc = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC").value
    sel = [k for k in range(SLOPE_BARS, len(ns)) if s_utc <= ns[k] < e_utc]
    off = sum(1 for k in sel if any(v not in timeline for v in ns[k - SLOPE_BARS:k + 1]))
    return {"spy_slope_windows_spanning_missing_bucket": int(sum(flags[k] for k in sel)),
            "development_15min_bars_evaluated": len(sel),
            "windows_touching_bucket_outside_expected_timeline": int(off),
            "level": "bucket-level (every existing development 15Min bar as a potential R); signal-level count is "
                     "reported by the H004 development run",
            "diagnostic_only": True}


def expected_dates_for_timeline(stock_dir: Path, loader=None) -> List[date]:
    """Fechas con cobertura de acciones desde el inicio de descarga hasta el fin de desarrollo (misma regla Q8)."""
    r = required_sessions(stock_dir, start=DOWNLOAD_START, end=DEV_END, loader=loader)
    return [date.fromisoformat(d) for d in r["required_session_dates"]]


def run_audit(stock_dir: Path, spy_dir: Path, stock_manifest: Path, loader=None) -> Dict[str, Any]:
    frozen = load_frozen_sessions(spy_dir)
    again = required_sessions(stock_dir, loader=loader)
    if again["required_session_dates_sha256"] != frozen["required_session_dates_sha256"]:
        raise SpyContextError("la lista de sesiones requeridas re-derivada no coincide con la congelada")
    cache = stock_cache_check(stock_dir, stock_manifest) if loader is None else {"all_match": None}
    path = symbol_path(spy_dir, "1Min", CONTEXT_SYMBOL)
    raw = audit_raw(path)
    failures: List[str] = []
    if not raw["integrity_pass"]:
        failures.append("raw SPY integrity/range checks failed")
        return {"frozen_sessions": frozen, "raw": raw, "failures": failures, "status": "HARD_FAIL"}
    df1 = validate_bars(read_raw(path), CONTEXT_SYMBOL, str(path))
    b15 = resample_rth(df1)
    a15 = audit_15min(b15)
    if not a15["pass"]:
        failures.append("15Min audit failed")
    sup = support_bars(b15)
    if not sup["pass"]:
        failures.append(f"only {sup['support_bars']} SPY 15Min support bars (< {SUPPORT_BARS})")
    required = [date.fromisoformat(d) for d in frozen["required_session_dates"]]
    cov = session_coverage(df1, b15, required)
    if cov["required_sessions_with_zero_spy_data"]:
        failures.append(f"required sessions with zero SPY RTH bars: {cov['required_sessions_with_zero_spy_data']}")
    slope = slope_windows_spanning_missing(b15, expected_dates_for_timeline(stock_dir, loader=loader))
    rth = df1[rth_mask(df1.index)]
    return {"frozen_sessions": frozen, "re_derived_hash_matches": True, "stock_cache": cache, "raw": raw,
            "rth_1min_rows": int(len(rth)), "extended_hours_1min_rows": int(len(df1) - len(rth)),
            "bars15": b15, "audit_15min": a15, "support": sup, "coverage": cov, "slope_diagnostic": slope,
            "failures": failures, "status": "HARD_FAIL" if failures else "PASS"}


# ================================================================ manifiesto
def build_manifest(res: Dict[str, Any], spy_dir: Path) -> Dict[str, Any]:
    fr = res["frozen_sessions"]
    req_path = Path(spy_dir) / "download_request.json"
    req = json.loads(req_path.read_text(encoding="utf-8")) if req_path.is_file() else None
    cov = res.get("coverage", {})
    return {
        "manifest_version": "context_manifest_spy_v1",
        "hypothesis_id": HYPOTHESIS_ID, "frozen_spec_commit": FROZEN_SPEC_COMMIT,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "role": "market context only (never traded, sized, in P&L or in D6)",
        "source": {"provider": "Alpaca Market Data API v2",
                   "endpoint": (req or {}).get("endpoint", "GET {ALPACA_DATA_URL}/stocks/SPY/bars"),
                   "symbol": CONTEXT_SYMBOL, "timeframe": "1Min", "feed": hd.FEED, "adjustment": hd.ADJUSTMENT,
                   "sip_fallback": False,
                   "request": req},
        "requested_range_ny": [DOWNLOAD_START.isoformat(), MAX_SPY_DATE.isoformat()],
        "validation_range_spy": "not downloaded, inspected or cached (spec §5, Q3 option A)",
        "raw_file": {k: res["raw"][k] for k in ("path", "sha256", "file_size_bytes", "row_count", "first_timestamp",
                                                 "last_timestamp", "first_date_ny", "last_date_ny")},
        "raw_integrity": {k: res["raw"][k] for k in ("duplicate_timestamps", "non_monotonic_timestamps", "impossible_ohlc_bars",
                                                      "non_positive_prices", "negative_volume", "null_or_nan",
                                                      "null_or_nan_total", "non_finite_values", "within_allowed_range",
                                                      "integrity_pass")},
        "rth_1min_rows": res.get("rth_1min_rows"), "extended_hours_1min_rows": res.get("extended_hours_1min_rows"),
        "audit_15min": res.get("audit_15min"),
        "support_bars": res.get("support"),
        "required_sessions": {
            "required_session_definition": fr["definition"],
            "tradable_symbols": fr["tradable_symbols"],
            "range": fr["range"],
            "required_session_count": fr["required_session_count"],
            "first_required_date": fr["first_required_date"], "last_required_date": fr["last_required_date"],
            "required_session_dates_sha256": fr["required_session_dates_sha256"], "hash_method": fr["hash_method"],
            "required_session_dates": fr["required_session_dates"],
            "excluded_zero_stock_coverage_dates": fr["excluded_zero_stock_coverage_dates"],
            "excluded_method": fr["excluded_method"],
            "derived_before_spy_access": fr["derived_before_spy_access"], "frozen_at": fr["frozen_at"],
            "determinism_check": fr["determinism_check"],
            "stock_cache_matches_historical_manifest": fr["stock_cache"]["all_match"],
        },
        "required_sessions_with_zero_spy_data": cov.get("required_sessions_with_zero_spy_data"),
        "required_sessions_with_partial_spy_gaps": cov.get("required_sessions_with_partial_spy_gaps"),
        "bucket_coverage_15min": {k: cov.get(k) for k in ("expected_buckets", "existing_buckets", "missing_buckets",
                                                          "sessions_with_missing_buckets",
                                                          "max_missing_buckets_in_one_present_session",
                                                          "spy_1min_rth_bars_in_required_sessions",
                                                          "expected_rth_minutes_in_required_sessions",
                                                          "min_spy_rth_1min_bars_in_present_session",
                                                          "development_dates_with_spy_but_not_required")},
        "diagnostics_only": res.get("slope_diagnostic"),
        "hard_fail_rules": ["structural/integrity failure", f"< {SUPPORT_BARS} valid pre-development SPY 15Min support bars",
                            "any required development session with zero usable regular-session SPY 1Min bars"],
        "failures": res["failures"],
        "h004_data_readiness": res["status"],
    }


def write_outputs(res: Dict[str, Any], manifest: Dict[str, Any], manifest_path: Path, audit_dir: Path) -> List[Path]:
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    Path(manifest_path).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    paths.append(Path(manifest_path))
    if "coverage" in res:
        rows = [dict(r, missing_bucket_starts_ny=";".join(r["missing_bucket_starts_ny"])) for r in res["coverage"]["rows"]]
        p = audit_dir / "spy_required_session_coverage.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        paths.append(p)
    if "bars15" in res:
        p = audit_dir / "spy_15min_rth.csv"
        b = res["bars15"].copy()
        b.index = b.index.strftime("%Y-%m-%dT%H:%M:%SZ")
        b.to_csv(p)
        paths.append(p)
    return paths


def format_report(m: Dict[str, Any]) -> str:
    rs, raw, ri = m["required_sessions"], m["raw_file"], m["raw_integrity"]
    a15, sup, cov, dg = m["audit_15min"] or {}, m["support_bars"] or {}, m["bucket_coverage_15min"], m["diagnostics_only"] or {}
    partial = m["required_sessions_with_partial_spy_gaps"] or []
    lines = [
        "H004 SPY CONTEXT DATA AUDIT (development + support only)",
        f"source: {m['source']['provider']} feed={m['source']['feed']} adjustment={m['source']['adjustment']} (no SIP)",
        f"required sessions: {rs['required_session_count']} ({rs['first_required_date']} .. {rs['last_required_date']}), "
        f"sha256 {rs['required_session_dates_sha256']}",
        f"excluded zero-stock dates: {rs['excluded_zero_stock_coverage_dates']}",
        f"SPY raw: {raw['row_count']} rows, {raw['first_timestamp']} .. {raw['last_timestamp']}, sha256 {raw['sha256']}",
        f"integrity: dup={ri['duplicate_timestamps']} out_of_order={ri['non_monotonic_timestamps']} "
        f"bad_ohlc={ri['impossible_ohlc_bars']} non_pos={ri['non_positive_prices']} neg_vol={ri['negative_volume']} "
        f"nan={ri['null_or_nan_total']} non_finite={ri['non_finite_values']} -> {'PASS' if ri['integrity_pass'] else 'FAIL'}",
        f"15Min: {a15.get('row_count')} bars, monotonic={a15.get('monotonic_increasing')} dup={a15.get('duplicate_timestamps')} "
        f"bad_ohlc={a15.get('impossible_ohlc_bars')} non_anchored={a15.get('non_anchored_buckets')} "
        f"after13_early={a15.get('buckets_after_13_on_early_close')} n_minutes={a15.get('n_minutes_min')}..{a15.get('n_minutes_max')} "
        f"-> {'PASS' if a15.get('pass') else 'FAIL'}",
        f"early-close last buckets: {a15.get('last_bucket_start_on_early_close')}",
        f"support: {sup.get('support_bars')}/{SUPPORT_BARS} ({sup.get('first_support_timestamp_ny')} .. {sup.get('last_support_timestamp_ny')})",
        f"zero-SPY required sessions: {m['required_sessions_with_zero_spy_data']}",
        f"partial-gap required sessions: {len(partial)} -> " + ", ".join(f"{p['date']}({p['missing_15min_buckets']})" for p in partial[:30]),
        f"buckets: expected {cov['expected_buckets']}, existing {cov['existing_buckets']}, missing {cov['missing_buckets']}, "
        f"sessions with missing {cov['sessions_with_missing_buckets']}, max missing in one present session "
        f"{cov['max_missing_buckets_in_one_present_session']}",
        f"spy_slope_windows_spanning_missing_bucket: {dg.get('spy_slope_windows_spanning_missing_bucket')} "
        f"of {dg.get('development_15min_bars_evaluated')} ({dg.get('level')})",
        f"H004 DATA READINESS: {m['h004_data_readiness']}" + (f"  failures: {m['failures']}" if m["failures"] else ""),
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="H004 SPY context data: sessions -> download -> audit (development only).")
    p.add_argument("step", choices=["sessions", "download", "audit"])
    p.add_argument("--stock-dir", type=Path, default=DEFAULT_STOCK_DIR)
    p.add_argument("--spy-dir", type=Path, default=DEFAULT_SPY_DIR)
    p.add_argument("--stock-manifest", type=Path, default=DEFAULT_STOCK_MANIFEST)
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    a = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if Path(a.manifest).resolve() == Path(a.stock_manifest).resolve():
        raise SpyContextError("el manifiesto SPY no puede sobrescribir el manifiesto histórico de acciones")
    if a.step == "sessions":
        fr = freeze_sessions(a.stock_dir, a.spy_dir, a.stock_manifest)
        print(json.dumps({k: fr[k] for k in ("required_session_count", "first_required_date", "last_required_date",
                                             "required_session_dates_sha256", "excluded_zero_stock_coverage_dates",
                                             "required_dates_on_listed_holidays")}, indent=2))
        print(f"stock cache matches historical manifest: {fr['stock_cache']['all_match']}")
        return 0
    if a.step == "download":
        req = download_spy(a.spy_dir)
        print(json.dumps(req, indent=2))
        return 0
    res = run_audit(a.stock_dir, a.spy_dir, a.stock_manifest)
    man = build_manifest(res, a.spy_dir)
    for f in write_outputs(res, man, a.manifest, a.audit_dir):
        print(f"wrote {f}")
    print(format_report(man))
    return 0 if res["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
