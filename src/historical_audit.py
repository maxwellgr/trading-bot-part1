# src/historical_audit.py
"""
Auditoría de SOLO LECTURA de la caché histórica (data/historical/<tf>/<SYM>.csv)
y manifiesto reproducible (checksums). No modifica ni "arregla" datos: cuenta,
marca y reporta. Sin red.

    python -m src.historical_audit --data-dir data/historical --timeframe 1Min \
        --protocol config/research_protocol_v1.json --output-dir data/research_v1 \
        --manifest data/historical/manifest_v1.json

Qué se mide (por símbolo)
-------------------------
- primer/último timestamp, filas, fechas con velas (fecha NY), velas por fecha
- duplicados, retrocesos, OHLC imposibles (low<=open<=high, low<=close<=high,
  low<=high), precios <= 0, volumen negativo, nulos/NaN
- huecos cronológicos grandes (días hábiles sin velas; huecos intradía en RTH)
- discontinuidades de apertura vs cierre regular previo (> 40%): típicamente
  splits NO ajustados (el feed se guarda crudo) o errores de datos
- cobertura RTH por fecha: velas observadas en [09:30, cierre) / minutos esperados
  (390; 210 en cierres anticipados de la lista EARLY_CLOSES). El feed IEX es por
  operaciones: un minuto sin operación no tiene vela, así que < 100% es normal.

Feriados: lista estática NYSE_HOLIDAYS (cierres completos), también verificada con
los datos: un feriado con velas o un día hábil sin velas que NO es feriado
("missing trading day", p. ej. un hueco del feed) se reportan como avisos.

Cierres anticipados: sin dependencia de calendario. EARLY_CLOSES es una lista
estática (13:00 ET, calendario NYSE publicado) y se VERIFICA con los datos: en
esas fechas la actividad RTH después de las 13:00 debe desplomarse. Además se
marcan fechas no listadas donde todos los símbolos se apagan tras las 13:00.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

NY = "America/New_York"
RTH_OPEN_MIN = 9 * 60 + 30
RTH_CLOSE_MIN = 16 * 60
EARLY_CLOSE_MIN = 13 * 60
FULL_SESSION_MINUTES = 390
EARLY_SESSION_MINUTES = 210
EARLY_CLOSES = frozenset(date.fromisoformat(d) for d in (
    "2023-07-03", "2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24"))
NYSE_HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
    "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25"))
SPARSE_COVERAGE_PCT = 50.0          # fecha "escasa" (bandera descriptiva, no descarte)
PARTIAL_START_MIN = 10 * 60         # primera vela RTH después de 10:00 -> sesión parcial
PARTIAL_END_SLACK = 30              # última vela RTH > 30 min antes del cierre -> sesión parcial
INTRADAY_GAP_MIN = 30               # hueco RTH entre velas consecutivas >= 30 min
DISCONTINUITY_PCT = 40.0            # |open regular / cierre regular previo - 1|
EARLY_CLOSE_AFTER13_RATIO = 0.2     # verificación empírica de cierre anticipado


# ================================================================ lectura
def read_raw(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"timestamp": str, "symbol": str})


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ================================================================ integridad
def integrity(df: pd.DataFrame) -> Dict[str, Any]:
    """Conteos de problemas sobre el CSV crudo (no se detiene en el primero)."""
    ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601", errors="coerce")
    num = {c: pd.to_numeric(df[c], errors="coerce") for c in ("open", "high", "low", "close", "volume")}
    o, h, l, c, v = (num[k] for k in ("open", "high", "low", "close", "volume"))
    nulls = {k: int(s.isna().sum()) for k, s in num.items()}
    nulls["timestamp"] = int(ts.isna().sum())
    ns = ts.dropna().astype("int64").to_numpy()
    diffs = np.diff(ns)
    bad_ohlc = (l > o) | (o > h) | (l > c) | (c > h) | (l > h)
    return {
        "duplicate_timestamps": int(ts.duplicated().sum()),
        "non_monotonic_timestamps": int((diffs < 0).sum()),
        "impossible_ohlc_bars": int(bad_ohlc.fillna(False).sum()),
        "non_positive_prices": int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).fillna(False).sum()),
        "negative_volume": int((v < 0).fillna(False).sum()),
        "null_or_nan": nulls,
        "null_or_nan_total": int(sum(nulls.values())),
    }


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    """Vista NY con fecha y minuto del día (solo filas con timestamp válido)."""
    ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601", errors="coerce")
    ok = ts.notna()
    ny = ts[ok].dt.tz_convert(NY)
    out = pd.DataFrame({"ts": ts[ok].to_numpy(), "date": ny.dt.date.to_numpy(),
                        "minute": (ny.dt.hour * 60 + ny.dt.minute).to_numpy(),
                        "weekday": ny.dt.weekday.to_numpy(),
                        "open": pd.to_numeric(df.loc[ok, "open"], errors="coerce").to_numpy(),
                        "close": pd.to_numeric(df.loc[ok, "close"], errors="coerce").to_numpy(),
                        "volume": pd.to_numeric(df.loc[ok, "volume"], errors="coerce").to_numpy()})
    return out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def session_close_min(d: date) -> int:
    return EARLY_CLOSE_MIN if d in EARLY_CLOSES else RTH_CLOSE_MIN


def coverage_by_date(f: pd.DataFrame) -> pd.DataFrame:
    """Una fila por fecha con velas: velas totales/RTH, cobertura RTH, banderas descriptivas."""
    rows = []
    for d, g in f.groupby("date", sort=True):
        close_min = session_close_min(d)
        expected = EARLY_SESSION_MINUTES if d in EARLY_CLOSES else FULL_SESSION_MINUTES
        rth = g[(g["minute"] >= RTH_OPEN_MIN) & (g["minute"] < close_min)]
        mins = rth["minute"].to_numpy()
        gaps = np.diff(np.concatenate([[RTH_OPEN_MIN - 1], mins, [close_min]])) - 1 if len(mins) else np.array([])
        max_gap = int(gaps.max()) if len(gaps) else None
        cov = len(rth) / expected * 100
        rows.append({
            "date": d, "bars_total": len(g), "rth_bars": len(rth),
            "extended_hours_bars": int(((g["minute"] < RTH_OPEN_MIN) | (g["minute"] >= RTH_CLOSE_MIN)).sum()),
            "expected_rth_minutes": expected,
            "rth_coverage_pct": cov, "early_close": d in EARLY_CLOSES,
            "first_rth_minute": int(mins[0]) if len(mins) else None,
            "last_rth_minute": int(mins[-1]) if len(mins) else None,
            "max_rth_gap_minutes": max_gap,
            "rth_gaps_ge_30min": int((gaps >= INTRADAY_GAP_MIN).sum()) if len(gaps) else 0,
            "rth_volume": float(rth["volume"].sum()),
            "after_13_rth_bars": int(((g["minute"] >= EARLY_CLOSE_MIN) & (g["minute"] < RTH_CLOSE_MIN)).sum()),
            "sparse": cov < SPARSE_COVERAGE_PCT,
            "partial_session": (not len(mins)) or mins[0] > PARTIAL_START_MIN
                               or mins[-1] < close_min - PARTIAL_END_SLACK,
        })
    return pd.DataFrame(rows)


def discontinuities(f: pd.DataFrame) -> List[Dict[str, Any]]:
    """Apertura regular vs último cierre regular previo: saltos > DISCONTINUITY_PCT (p. ej. split crudo)."""
    rth = f[(f["minute"] >= RTH_OPEN_MIN) & (f["minute"] < RTH_CLOSE_MIN)]
    if rth.empty:
        return []
    first = rth.groupby("date").first()
    last = rth.groupby("date").last()
    out = []
    dates = list(first.index)
    for prev, cur in zip(dates, dates[1:]):
        pc, op = float(last.loc[prev, "close"]), float(first.loc[cur, "open"])
        if pc > 0:
            chg = (op / pc - 1) * 100
            if abs(chg) > DISCONTINUITY_PCT:
                out.append({"date": str(cur), "prev_date": str(prev), "prev_rth_close": pc, "rth_open": op,
                            "change_pct": chg, "ratio_prev_over_open": pc / op if op else None,
                            "likely": "unadjusted split or data error (feed stored raw)"})
    return out


def _stats(values: Sequence[float]) -> Dict[str, Any]:
    v = list(values)
    if not v:
        return {"min": None, "median": None, "mean": None, "max": None}
    return {"min": min(v), "median": statistics.median(v), "mean": statistics.fmean(v), "max": max(v)}


def weekday_dates(start: date, end: date) -> List[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


def audit_symbol(path: Path, symbol: str) -> Dict[str, Any]:
    raw = read_raw(path)
    integ = integrity(raw)
    f = _frame(raw)
    cov = coverage_by_date(f)
    trading = [d for d in cov["date"]] if len(cov) else []
    missing_weekdays = sorted(set(weekday_dates(trading[0], trading[-1])) - set(trading)) if trading else []
    big_gaps = []
    for a, b in zip(trading, trading[1:]):
        skipped = len(weekday_dates(a, b)) - 2
        if skipped >= 2:  # >= 2 días hábiles seguidos sin velas (un feriado suelto es normal)
            big_gaps.append({"from": str(a), "to": str(b), "weekdays_without_bars": skipped})
    rth_cov = cov["rth_coverage_pct"].tolist() if len(cov) else []
    return {
        "symbol": symbol, "file": str(path),
        "first_timestamp": f["ts"].iloc[0].isoformat() if len(f) else None,
        "last_timestamp": f["ts"].iloc[-1].isoformat() if len(f) else None,
        "total_bars": int(len(raw)),
        "trading_dates": len(trading),
        "first_date": str(trading[0]) if trading else None, "last_date": str(trading[-1]) if trading else None,
        "bars_per_date": _stats(cov["bars_total"].tolist() if len(cov) else []),
        "rth_bars_per_date": _stats(cov["rth_bars"].tolist() if len(cov) else []),
        "rth_coverage_pct": _stats(rth_cov),
        **integ,
        "weekdays_without_bars": [str(d) for d in missing_weekdays],
        "missing_trading_days": [str(d) for d in missing_weekdays if d not in NYSE_HOLIDAYS],
        "holidays_with_bars": [str(d) for d in trading if d in NYSE_HOLIDAYS],
        "large_chronological_gaps": big_gaps,
        "dates_with_rth_gap_ge_30min": int((cov["rth_gaps_ge_30min"] > 0).sum()) if len(cov) else 0,
        "sparse_dates": [str(d) for d in cov.loc[cov["sparse"], "date"]] if len(cov) else [],
        "partial_session_dates": [str(d) for d in cov.loc[cov["partial_session"], "date"]] if len(cov) else [],
        "price_discontinuities": discontinuities(f),
        "_coverage": cov,
    }


# ================================================================ universo
def cross_symbol(per: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    cov = {s: a["_coverage"].set_index("date") for s, a in per.items() if len(a["_coverage"])}
    all_dates = sorted(set().union(*(set(c.index) for c in cov.values()))) if cov else []
    missing = {}
    reliable = []
    for d in all_dates:
        absent = [s for s in per if s not in cov or d not in cov[s].index]
        if absent:
            missing[str(d)] = absent
            continue
        if not any(bool(cov[s].loc[d, "sparse"]) for s in cov):
            reliable.append(d)
    common = [d for d in all_dates if str(d) not in missing]
    return {
        "dates_any_symbol": len(all_dates),
        "dates_all_symbols": len(common),
        "first_common_date": str(common[0]) if common else None,
        "last_common_date": str(common[-1]) if common else None,
        "dates_missing_symbols": missing,
        "reliable_dates": len(reliable),
        "reliable_rule": f"all symbols present and none below {SPARSE_COVERAGE_PCT:g}% RTH coverage",
        "earliest_reliable_date": str(reliable[0]) if reliable else None,
        "_common": common, "_reliable": reliable,
    }


def early_close_check(per: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Verifica EARLY_CLOSES con los datos y marca candidatos no listados."""
    frames = [a["_coverage"][["date", "after_13_rth_bars"]] for a in per.values() if len(a["_coverage"])]
    if not frames:
        return {"listed": [], "unlisted_candidates": []}
    tot = pd.concat(frames).groupby("date")["after_13_rth_bars"].sum()
    normal = [v for d, v in tot.items() if d not in EARLY_CLOSES]
    med = statistics.median(normal) if normal else 0
    listed = [{"date": str(d), "after_13_bars_all_symbols": int(tot[d]), "median_normal_day": med,
               "confirmed_by_data": bool(med and tot[d] < EARLY_CLOSE_AFTER13_RATIO * med)}
              for d in sorted(EARLY_CLOSES) if d in tot.index]
    unlisted = [{"date": str(d), "after_13_bars_all_symbols": int(v), "median_normal_day": med}
                for d, v in tot.items() if d not in EARLY_CLOSES and med and v < EARLY_CLOSE_AFTER13_RATIO * med]
    return {"method": f"after-13:00 RTH bars summed over symbols < {EARLY_CLOSE_AFTER13_RATIO:g} x median of other days",
            "listed": listed, "unlisted_candidates": unlisted}


def split_coverage(protocol: Optional[Dict[str, Any]], per: Dict[str, Dict[str, Any]],
                   cross: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not protocol:
        return []
    rows = []
    last = max((a["last_date"] for a in per.values() if a["last_date"]), default=None)
    with_data = set().union(*(set(a["_coverage"]["date"]) for a in per.values() if len(a["_coverage"]))) if per else set()
    for s in protocol["splits"]:
        start = date.fromisoformat(s["start"])
        end = date.fromisoformat(s["end"]) if s.get("end") else (date.fromisoformat(last) if last else start)
        wd = weekday_dates(start, end) if end >= start else []
        any_d = [d for d in wd if d in with_data]
        common = [d for d in cross["_common"] if start <= d <= end]
        reliable = [d for d in cross["_reliable"] if start <= d <= end]
        per_sym = {}
        for sym, a in per.items():
            c = a["_coverage"]
            sub = c[(c["date"] >= start) & (c["date"] <= end)] if len(c) else c
            per_sym[sym] = {"dates": int(len(sub)),
                            "median_rth_coverage_pct": float(sub["rth_coverage_pct"].median()) if len(sub) else None,
                            "median_extended_hours_bars": float(sub["extended_hours_bars"].median()) if len(sub) else None,
                            "pct_dates_with_extended_hours_bars":
                                float((sub["extended_hours_bars"] > 0).mean() * 100) if len(sub) else None,
                            "sparse_dates": int(sub["sparse"].sum()) if len(sub) else 0}
        rows.append({"split": s["name"], "role": s["role"], "start": s["start"], "end": s.get("end"),
                     "evaluated_through": str(end), "weekdays": len(wd), "dates_with_any_data": len(any_d),
                     "dates_all_symbols": len(common), "reliable_dates": len(reliable),
                     "reliable_pct_of_data_dates": len(reliable) / len(any_d) * 100 if any_d else None,
                     "per_symbol": per_sym})
    return rows


def run_audit(data_dir: Path, timeframe: str, symbols: Sequence[str],
              protocol: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    per = {}
    missing_files = []
    for s in symbols:
        path = Path(data_dir) / timeframe / f"{s.upper()}.csv"
        if not path.is_file():
            missing_files.append(str(path))
            continue
        per[s] = audit_symbol(path, s)
    cross = cross_symbol(per)
    warnings = [f"missing file: {m}" for m in missing_files]
    for s, a in per.items():
        for k in ("duplicate_timestamps", "non_monotonic_timestamps", "impossible_ohlc_bars", "non_positive_prices",
                  "negative_volume", "null_or_nan_total"):
            if a[k]:
                warnings.append(f"{s}: {k}={a[k]}")
        for disc in a["price_discontinuities"]:
            warnings.append(f"{s}: RTH open {disc['date']} vs prior close {disc['change_pct']:+.1f}% "
                            f"(prev/open ratio {disc['ratio_prev_over_open']:.3f}; {disc['likely']})")
        if a["missing_trading_days"]:
            warnings.append(f"{s}: weekday(s) without bars that are not NYSE holidays: {a['missing_trading_days']}")
        if a["holidays_with_bars"]:
            warnings.append(f"{s}: bars on listed NYSE holiday(s): {a['holidays_with_bars']}")
        if a["large_chronological_gaps"]:
            warnings.append(f"{s}: {len(a['large_chronological_gaps'])} gap(s) of >= 2 weekdays without bars")
        if a["sparse_dates"]:
            warnings.append(f"{s}: {len(a['sparse_dates'])} sparse RTH date(s) (< {SPARSE_COVERAGE_PCT:g}% coverage)")
    ec = early_close_check(per)
    for e in ec["listed"]:
        if not e["confirmed_by_data"]:
            warnings.append(f"early close {e['date']} (static list) NOT confirmed by data")
    for e in ec["unlisted_candidates"]:
        warnings.append(f"{e['date']}: activity after 13:00 collapsed on all symbols but date is not in EARLY_CLOSES")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_dir": str(data_dir), "timeframe": timeframe, "symbols": list(symbols),
        "thresholds": {"sparse_coverage_pct": SPARSE_COVERAGE_PCT, "partial_start_after_min": PARTIAL_START_MIN,
                       "partial_end_slack_min": PARTIAL_END_SLACK, "intraday_gap_min": INTRADAY_GAP_MIN,
                       "discontinuity_pct": DISCONTINUITY_PCT},
        "notes": ["IEX bars are trade-based: minutes without an IEX trade have no bar; coverage < 100% is expected.",
                  "Weekdays without bars are compared with a static NYSE holiday list (no calendar dependency).",
                  "Early closes use a static NYSE list verified against the data (see early_close_check).",
                  "Flags are descriptive; nothing is discarded or repaired."],
        "per_symbol": {s: {k: v for k, v in a.items() if not k.startswith("_")} for s, a in per.items()},
        "cross_symbol": {k: v for k, v in cross.items() if not k.startswith("_")},
        "early_close_check": ec,
        "split_coverage": split_coverage(protocol, per, cross),
        "warnings": warnings,
        "_per": per,
    }


# ================================================================ manifiesto
def build_manifest(data_dir: Path, timeframe: str, audit: Dict[str, Any],
                   protocol: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    files = []
    for s, a in audit["per_symbol"].items():
        path = Path(a["file"])
        files.append({"filename": f"{timeframe}/{path.name}", "symbol": s, "timeframe": timeframe,
                      "first_timestamp": a["first_timestamp"], "last_timestamp": a["last_timestamp"],
                      "row_count": a["total_bars"], "file_size_bytes": path.stat().st_size,
                      "sha256": sha256_file(path)})
    firsts = [f["first_timestamp"] for f in files if f["first_timestamp"]]
    lasts = [f["last_timestamp"] for f in files if f["last_timestamp"]]
    requested = None
    if protocol:
        requested = {"start": protocol["splits"][0]["start"],
                     "end_of_backtest_splits": max(s["end"] for s in protocol["splits"] if s.get("end")),
                     "protocol_version": protocol["protocol_version"]}
    return {
        "manifest_version": "historical_manifest_v1",
        "generated_at": audit["generated_at"],
        "data_dir": str(data_dir), "timeframe": timeframe,
        "source": (protocol or {}).get("data_source"),
        "requested_research_period": requested,
        "actual_available_period": {"all_symbols_from": max(firsts) if firsts else None,
                                    "all_symbols_until": min(lasts) if lasts else None,
                                    "first_common_trading_date": audit["cross_symbol"]["first_common_date"],
                                    "last_common_trading_date": audit["cross_symbol"]["last_common_date"]},
        "files": files,
        "audit_warnings": audit["warnings"],
    }


# ================================================================ salidas
AUDIT_CSV = ["symbol", "first_timestamp", "last_timestamp", "total_bars", "trading_dates", "bars_per_date_min",
             "bars_per_date_median", "bars_per_date_mean", "bars_per_date_max", "rth_coverage_median_pct",
             "duplicate_timestamps", "non_monotonic_timestamps", "impossible_ohlc_bars", "non_positive_prices",
             "negative_volume", "null_or_nan_total", "large_chronological_gaps", "sparse_dates",
             "partial_session_dates", "dates_with_rth_gap_ge_30min", "price_discontinuities"]


def audit_csv_rows(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for s, a in audit["per_symbol"].items():
        b = a["bars_per_date"]
        rows.append({"symbol": s, "first_timestamp": a["first_timestamp"], "last_timestamp": a["last_timestamp"],
                     "total_bars": a["total_bars"], "trading_dates": a["trading_dates"],
                     "bars_per_date_min": b["min"], "bars_per_date_median": b["median"],
                     "bars_per_date_mean": b["mean"], "bars_per_date_max": b["max"],
                     "rth_coverage_median_pct": a["rth_coverage_pct"]["median"],
                     **{k: a[k] for k in ("duplicate_timestamps", "non_monotonic_timestamps", "impossible_ohlc_bars",
                                          "non_positive_prices", "negative_volume", "null_or_nan_total",
                                          "dates_with_rth_gap_ge_30min")},
                     "large_chronological_gaps": len(a["large_chronological_gaps"]),
                     "sparse_dates": len(a["sparse_dates"]), "partial_session_dates": len(a["partial_session_dates"]),
                     "price_discontinuities": len(a["price_discontinuities"])})
    return rows


def write_audit(audit: Dict[str, Any], manifest: Dict[str, Any], out_dir: Path, manifest_path: Path) -> List[Path]:
    from .backtest_report import to_json
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    p = out_dir / "historical_audit.json"
    p.write_text(to_json({k: v for k, v in audit.items() if not k.startswith("_")}), encoding="utf-8")
    written.append(p)
    p = out_dir / "historical_audit.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=AUDIT_CSV)
        w.writeheader()
        w.writerows(audit_csv_rows(audit))
    written.append(p)
    p = out_dir / "historical_coverage_by_date.csv"
    frames = [a["_coverage"].assign(symbol=s) for s, a in audit["_per"].items() if len(a["_coverage"])]
    cov = pd.concat(frames) if frames else pd.DataFrame()
    if len(cov):
        cov = cov[["symbol"] + [c for c in cov.columns if c != "symbol"]]
    cov.to_csv(p, index=False)
    written.append(p)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(to_json(manifest), encoding="utf-8")
    written.append(manifest_path)
    return written


def _pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1f}%"


def format_audit(audit: Dict[str, Any]) -> str:
    L = ["HISTORICAL DATA AUDIT (read-only)", "─" * 72,
         f"{'symbol':<7}{'first':>12}{'last':>12}{'bars':>10}{'dates':>7}{'bars/d med':>11}{'RTH cov med':>12}"
         f"{'dup':>5}{'ohlc':>5}{'nan':>5}{'sparse':>7}{'disc':>5}"]
    for s, a in audit["per_symbol"].items():
        L.append(f"{s:<7}{(a['first_date'] or '-'):>12}{(a['last_date'] or '-'):>12}{a['total_bars']:>10,}"
                 f"{a['trading_dates']:>7}{a['bars_per_date']['median'] or 0:>11.0f}"
                 f"{_pct(a['rth_coverage_pct']['median']):>12}{a['duplicate_timestamps']:>5}"
                 f"{a['impossible_ohlc_bars']:>5}{a['null_or_nan_total']:>5}{len(a['sparse_dates']):>7}"
                 f"{len(a['price_discontinuities']):>5}")
    c = audit["cross_symbol"]
    L += ["", f"Dates with all {len(audit['symbols'])} symbols: {c['dates_all_symbols']} of {c['dates_any_symbol']} "
              f"({c['first_common_date']} → {c['last_common_date']}); reliable ({c['reliable_rule']}): "
              f"{c['reliable_dates']}, earliest {c['earliest_reliable_date']}",
          f"Dates missing one or more symbols: {len(c['dates_missing_symbols'])}"]
    if audit["split_coverage"]:
        L += ["", f"{'split':<18}{'role':<14}{'weekdays':>9}{'data d':>8}{'all 8':>7}{'reliable':>9}"]
        for r in audit["split_coverage"]:
            L.append(f"{r['split']:<18}{r['role']:<14}{r['weekdays']:>9}{r['dates_with_any_data']:>8}"
                     f"{r['dates_all_symbols']:>7}{r['reliable_dates']:>9}")
    ec = audit["early_close_check"]
    L += ["", "Early closes (static list, data check): "
          + (", ".join(f"{e['date']} {'confirmed' if e['confirmed_by_data'] else 'NOT confirmed'}" for e in ec["listed"])
             or "none in range")]
    if ec["unlisted_candidates"]:
        L.append("Unlisted early-close candidates: " + ", ".join(e["date"] for e in ec["unlisted_candidates"]))
    L += ["", f"Warnings ({len(audit['warnings'])}):"] + [f"  - {w}" for w in audit["warnings"][:40]]
    if len(audit["warnings"]) > 40:
        L.append(f"  ... {len(audit['warnings']) - 40} more in historical_audit.json")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    from .research_protocol import DEFAULT_PROTOCOL_PATH, load_protocol
    p = argparse.ArgumentParser(prog="python -m src.historical_audit",
                                description="Auditoría de solo lectura de la caché histórica + manifiesto con checksums.")
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--timeframe", default=None, help="Default: el del protocolo")
    p.add_argument("--symbols", default=None, help="Default: los del protocolo")
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--output-dir", type=Path, default=Path("data") / "research_v1")
    p.add_argument("--manifest", type=Path, default=None, help="Default: <data-dir>/manifest_v1.json")
    a = p.parse_args(argv)
    protocol = load_protocol(a.protocol) if a.protocol and a.protocol.is_file() else None
    tf = a.timeframe or (protocol["universe"]["timeframe"] if protocol else "1Min")
    syms = ([s.strip().upper() for s in a.symbols.split(",") if s.strip()] if a.symbols
            else (protocol["universe"]["symbols"] if protocol else []))
    if not syms:
        p.error("--symbols es obligatorio sin protocolo")
    audit = run_audit(a.data_dir, tf, syms, protocol)
    manifest = build_manifest(a.data_dir, tf, audit, protocol)
    paths = write_audit(audit, manifest, a.output_dir, a.manifest or a.data_dir / "manifest_v1.json")
    print(format_audit(audit))
    print("\nArchivos: " + ", ".join(str(x) for x in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
