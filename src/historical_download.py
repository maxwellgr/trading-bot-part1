# src/historical_download.py
"""
Descargador OPCIONAL de barras históricas (solo lectura) con caché local.

    python -m src.historical_download --symbols NVDA,AMD --timeframe 1Min \
        --start 2026-06-01 --end 2026-09-23 --data-dir data/historical

- Solo hace GET a {ALPACA_DATA_URL}/stocks/{symbol}/bars (feed IEX, el mismo
  que usa el bot en vivo, sin ajuste por splits/dividendos, igual que en vivo).
  Nunca envía órdenes ni toca endpoints de trading.
- Escribe <data-dir>/<timeframe>/<SYMBOL>.csv en el formato que valida
  historical_data.py. Si el archivo ya existe, fusiona (unión por timestamp,
  prevalece lo recién descargado) para que las corridas repetidas no
  dupliquen nada. Después de descargar, los backtests son 100% offline.
- --start/--end son fechas de calendario de Nueva York, inclusivas.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import requests

from .historical_data import symbol_path, validate_bars

NY = "America/New_York"


def date_range_utc(start: str, end: str):
    """[start 00:00 NY, end+1 00:00 NY) en UTC."""
    s = pd.Timestamp(start).tz_localize(NY).tz_convert("UTC")
    e = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    return s, e


def fetch_bars(symbol: str, timeframe: str, start_utc: pd.Timestamp, end_utc: pd.Timestamp,
               get: Callable[..., requests.Response], data_url: str, headers: Dict[str, str]) -> List[dict]:
    """Pagina GET /stocks/{symbol}/bars (asc) hasta agotar next_page_token."""
    out: List[dict] = []
    params = {"timeframe": timeframe, "start": start_utc.isoformat().replace("+00:00", "Z"),
              "end": end_utc.isoformat().replace("+00:00", "Z"), "limit": 10000, "feed": "iex", "sort": "asc"}
    while True:
        r = get(f"{data_url}/stocks/{symbol}/bars", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            return out
        params = dict(params, page_token=token)


def bars_to_frame(symbol: str, bars: List[dict]) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [b["t"] for b in bars], "open": [b["o"] for b in bars], "high": [b["h"] for b in bars],
        "low": [b["l"] for b in bars], "close": [b["c"] for b in bars], "volume": [b["v"] for b in bars],
        "symbol": symbol.upper(),
    })


def merge_into_cache(path: Path, symbol: str, new: pd.DataFrame) -> int:
    """Fusiona con el CSV existente (si hay) y escribe atómicamente. Devuelve filas totales."""
    frames = []
    if path.is_file():
        frames.append(pd.read_csv(path, dtype={"timestamp": str, "symbol": str}))
    frames.append(new)
    df = pd.concat(frames, ignore_index=True)
    df["_ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    df = df.drop_duplicates("_ts", keep="last").sort_values("_ts")
    df["timestamp"] = df["_ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df = df.drop(columns="_ts")[["timestamp", "open", "high", "low", "close", "volume", "symbol"]]
    validate_bars(df.reset_index(drop=True), symbol, str(path))  # nunca se cachea algo inválido
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)
    return len(df)


def download(symbols: List[str], timeframe: str, start: str, end: str, data_dir: Path,
             get: Optional[Callable[..., requests.Response]] = None) -> Dict[str, int]:
    from .config import settings  # solo aquí: el motor de backtest no necesita credenciales

    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key or "", "APCA-API-SECRET-KEY": settings.alpaca_api_secret or ""}
    get = get or requests.get
    s_utc, e_utc = date_range_utc(start, end)
    result = {}
    for sym in symbols:
        bars = fetch_bars(sym, timeframe, s_utc, e_utc, get, settings.alpaca_data_url, headers)
        total = merge_into_cache(symbol_path(data_dir, timeframe, sym), sym, bars_to_frame(sym, bars)) if bars else 0
        result[sym] = len(bars)
        print(f"{sym}: {len(bars)} velas descargadas ({total} en caché)")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Descarga (solo lectura) barras históricas IEX de Alpaca a una caché CSV local.")
    p.add_argument("--symbols", required=True)
    p.add_argument("--timeframe", default="1Min")
    p.add_argument("--start", required=True, help="Fecha NY inclusiva YYYY-MM-DD")
    p.add_argument("--end", required=True, help="Fecha NY inclusiva YYYY-MM-DD")
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    a = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # consola Windows cp1252
    except Exception:
        pass
    syms =[s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    download(syms, a.timeframe, a.start, a.end, a.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
