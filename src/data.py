# src/data.py
import pandas as pd
from typing import List, Dict

def bars_to_df(bars: List[Dict]) -> pd.DataFrame:
    """Convierte lista de barras (JSON) a DataFrame con índice datetime y columnas OHLCV."""
    if not bars:
        raise ValueError("Sin barras de datos para convertir.")
    df = pd.DataFrame(bars).copy()
    # Normaliza nombres comunes
    rename = {"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    df = df.rename(columns=rename)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    return df[cols]

def load_csv(path: str) -> pd.DataFrame:
    """Carga CSV local con columnas: timestamp, open, high, low, close, volume."""
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp").sort_index()
