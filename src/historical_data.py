# src/historical_data.py
"""
Carga y validación de barras históricas LOCALES para el backtester.

Formato (CSV, uno por símbolo y timeframe):

    <data_dir>/<timeframe>/<SYMBOL>.csv
    columnas: timestamp, open, high, low, close, volume, symbol

- timestamp: ISO-8601 CON zona horaria (p. ej. 2026-09-24T13:35:00Z). Una
  marca sin zona se rechaza: no se adivina la zona.
- Filas estrictamente ascendentes; duplicados y retrocesos se rechazan con
  el número de línea (no se reordena ni se deduplica en silencio: un archivo
  así indica un problema en la descarga que hay que ver).
- Nunca se inventan velas faltantes: los huecos quedan como huecos.

Este módulo no hace red. La descarga (opcional) vive en historical_download.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "symbol")
PRICE_COLUMNS = ("open", "high", "low", "close")


class HistoricalDataError(ValueError):
    """Datos históricos ausentes o mal formados (mensaje apto para el usuario)."""


def symbol_path(data_dir: Path, timeframe: str, symbol: str) -> Path:
    return Path(data_dir) / timeframe / f"{symbol.upper()}.csv"


def validate_bars(df: pd.DataFrame, symbol: str, source: str = "<memoria>") -> pd.DataFrame:
    """
    Valida un DataFrame crudo (columnas REQUIRED_COLUMNS, timestamp como texto
    o datetime) y devuelve uno indexado por timestamp UTC con OHLCV float.
    Lanza HistoricalDataError con un mensaje claro ante cualquier problema.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise HistoricalDataError(f"{source}: faltan columnas {missing} (se requieren {list(REQUIRED_COLUMNS)})")
    if df.empty:
        raise HistoricalDataError(f"{source}: archivo sin filas para {symbol}")

    syms = set(df["symbol"].astype(str).str.upper())
    if syms != {symbol.upper()}:
        raise HistoricalDataError(f"{source}: la columna symbol contiene {sorted(syms)}, se esperaba solo {symbol.upper()}")

    raw_ts = df["timestamp"]
    if not pd.api.types.is_datetime64_any_dtype(raw_ts):
        raw_str = raw_ts.astype(str)
        naive = ~raw_str.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True)
        if naive.any():
            line = int(np.flatnonzero(naive.to_numpy())[0]) + 2  # +1 cabecera, +1 base 1
            raise HistoricalDataError(f"{source}: timestamp sin zona horaria en la línea {line} ({raw_str.iloc[line - 2]!r})")
        try:
            ts = pd.to_datetime(raw_str, utc=True, format="ISO8601")
        except (ValueError, TypeError) as e:
            raise HistoricalDataError(f"{source}: timestamp no parseable: {e}") from e
    else:
        if getattr(raw_ts.dt, "tz", None) is None:
            raise HistoricalDataError(f"{source}: timestamps sin zona horaria")
        ts = raw_ts.dt.tz_convert("UTC")

    values = {}
    for col in PRICE_COLUMNS + ("volume",):
        v = pd.to_numeric(df[col], errors="coerce")
        bad = v.isna() | ~np.isfinite(v.to_numpy(dtype=float, na_value=np.nan))
        if bad.any():
            line = int(np.flatnonzero(bad.to_numpy())[0]) + 2
            raise HistoricalDataError(f"{source}: valor no numérico/NaN en '{col}' (línea {line})")
        values[col] = v.astype(float).to_numpy()

    if (np.minimum.reduce([values[c] for c in PRICE_COLUMNS]) <= 0).any():
        raise HistoricalDataError(f"{source}: precios <= 0")
    if (values["volume"] < 0).any():
        raise HistoricalDataError(f"{source}: volumen negativo")
    inconsistent = (values["high"] < np.maximum(values["open"], values["close"])) | \
                   (values["low"] > np.minimum(values["open"], values["close"])) | (values["high"] < values["low"])
    if inconsistent.any():
        line = int(np.flatnonzero(inconsistent)[0]) + 2
        raise HistoricalDataError(f"{source}: vela OHLC incoherente (high/low no contienen open/close) en la línea {line}")

    ts_ns = ts.to_numpy(dtype="datetime64[ns]").astype("int64")
    diffs = np.diff(ts_ns)
    if (diffs == 0).any():
        i = int(np.flatnonzero(diffs == 0)[0]) + 1
        raise HistoricalDataError(f"{source}: timestamp duplicado {ts.iloc[i].isoformat()} (línea {i + 2})")
    if (diffs < 0).any():
        i = int(np.flatnonzero(diffs < 0)[0]) + 1
        raise HistoricalDataError(
            f"{source}: timestamp fuera de orden en la línea {i + 2} ({ts.iloc[i].isoformat()} < {ts.iloc[i - 1].isoformat()})")

    out = pd.DataFrame(values, index=pd.DatetimeIndex(ts, name="timestamp"))
    return out[["open", "high", "low", "close", "volume"]]


def load_symbol_bars(data_dir: Path, timeframe: str, symbol: str,
                     start_utc: Optional[pd.Timestamp] = None, end_utc: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Lee y valida el CSV de un símbolo; opcionalmente recorta a [start_utc, end_utc)."""
    path = symbol_path(data_dir, timeframe, symbol)
    if not path.is_file():
        raise HistoricalDataError(f"No hay datos locales para {symbol}: falta {path}")
    df = validate_bars(pd.read_csv(path, dtype={"timestamp": str, "symbol": str}), symbol, str(path))
    if start_utc is not None:
        df = df[df.index >= start_utc]
    if end_utc is not None:
        df = df[df.index < end_utc]
    return df


@dataclass
class LoadedData:
    bars: Dict[str, pd.DataFrame]
    warnings: List[str]


def load_universe(data_dir: Path, timeframe: str, symbols: List[str],
                  start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> LoadedData:
    """
    Carga todos los símbolos. Un archivo ausente es un error (con la lista
    completa de faltantes y cómo descargarlos); un símbolo sin velas dentro
    del rango solicitado se reporta como aviso y no participa.
    """
    missing = [s for s in symbols if not symbol_path(data_dir, timeframe, s).is_file()]
    if missing:
        raise HistoricalDataError(
            f"Faltan datos locales para {missing} en {Path(data_dir) / timeframe}. Descárgalos con:\n"
            f"  python -m src.historical_download --symbols {','.join(missing)} --timeframe {timeframe} "
            f"--start <YYYY-MM-DD> --end <YYYY-MM-DD> --data-dir {data_dir}")
    bars: Dict[str, pd.DataFrame] = {}
    warnings: List[str] = []
    for s in symbols:
        df = load_symbol_bars(data_dir, timeframe, s, start_utc, end_utc)
        if df.empty:
            warnings.append(f"{s}: sin velas entre {start_utc.isoformat()} y {end_utc.isoformat()}; no participa.")
            continue
        bars[s] = df
    return LoadedData(bars=bars, warnings=warnings)
