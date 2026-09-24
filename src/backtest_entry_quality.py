# src/backtest_entry_quality.py
"""
Diagnóstico de CALIDAD DE ENTRADA (v1). SOLO medición: nada de aquí participa
en señales, riesgo, sizing, fills ni P&L.

Sin look-ahead
--------------
entry_features() recibe arrays que TERMINAN en la vela de señal (el motor pasa
vistas de solo lectura [:i+1]); las velas futuras no le llegan. Todo se calcula
con información conocida en decision_ts = inicio de la vela de señal + timeframe.

- Medias: rolling de pandas sobre la MISMA ventana en vivo que vio la estrategia
  (lo:i+1), así fast_ma/slow_ma coinciden con MACrossover. Cruces/whipsaws solo
  se buscan dentro de esa ventana (lo que el bot "sabía").
- ATR: RiskManager._atr (misma fórmula que el stop), ventana atr_window.
- Resto (momentum, volumen, gap): historia local hasta la vela de señal
  inclusive; incluye velas extendidas, como las que ve la estrategia.
- Pendientes: cambio medio por vela, (ma[k] - ma[k-N]) / N; *_atr = / ATR.
- *_pct y return_Nbar están en PORCENTAJE.
- avg_volume_N: media de las N velas ANTERIORES a la de señal (sin incluirla).
- opening_gap_pct: (open de la vela 09:30 de hoy - cierre de la última vela
  regular del día hábil previo) / ese cierre * 100. Si la vela 09:30 aún no se
  conoce o falta el cierre previo -> null (no se inventa).
- Hora de sesión: decision_ts en America/New_York.

Etiquetas de resultado (después del hecho, SOLO análisis)
---------------------------------------------------------
- never_worked: MFE < +0.5R (por cierres). OJO: distinto de la clase MFE/MAE
  "never_worked" (que además exige P&L <= 0); esa va en diagnostic_class.
- reached_0_5r / reached_1r / reached_1_5r / reached_2r: MFE >= nivel.
- profitable: result == "win"; loser: result == "loss".
- clean_winner: diagnostic_class == "clean_winner".

Nada de ML, clasificadores ni selección de variables: estadística descriptiva.
CORRELACIÓN NO ES CAUSALIDAD.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .risk_manager_avanzado import RiskManager

NY = "America/New_York"
FLAT_SLOPE_ATR = 0.02        # |pendiente lenta 3 velas| < 0.02 ATR/vela = "flat" (convención descriptiva)
WHIPSAW_WINDOW = 20
ATR_MEAN_WINDOW = 20
GAP_LOOKBACK_DAYS = 7
SESSION_BUCKETS = ("09:30-09:45", "09:45-10:00", "10:00-10:30", "10:30-11:00", "after_11:00")
CROSSOVER_AGE_BUCKETS = ("0", "1", "2", "3+", "unknown")
QUANTILES = 5
TERCILES = 3
DISCLAIMER = "Descriptive only. CORRELATION IS NOT CAUSATION. No bucket or feature is a recommendation."

FEATURE_COLUMNS = [
    # A. geometría de medias
    "fast_ma", "slow_ma", "ma_spread", "ma_spread_pct", "ma_spread_atr",
    "fast_ma_slope_1bar", "fast_ma_slope_3bar", "slow_ma_slope_1bar", "slow_ma_slope_3bar",
    "fast_ma_slope_1bar_atr", "fast_ma_slope_3bar_atr", "slow_ma_slope_1bar_atr", "slow_ma_slope_3bar_atr",
    "fast_ma_slope_direction", "slow_ma_slope_direction",
    # B. edad del cruce
    "bars_since_bullish_crossover", "crossover_age_bucket", "prev_ma_spread", "prev_ma_spread_atr",
    "ma_separated_before_bar", "prior_bearish_run_bars", "crossovers_last_20_bars",
    "spread_change_1bar", "spread_change_3bar", "spread_change_1bar_atr", "spread_change_3bar_atr",
    "spread_expanding",
    # C. extensión
    "close_minus_fast_ma", "close_minus_slow_ma", "close_minus_fast_ma_atr", "close_minus_slow_ma_atr",
    "close_minus_fast_ma_pct", "close_minus_slow_ma_pct",
    # D. vela de señal
    "signal_open", "signal_high", "signal_low", "signal_close", "bar_direction", "bar_range", "body_size",
    "upper_wick", "lower_wick", "body_to_range_ratio", "bar_range_atr", "body_atr", "upper_wick_atr",
    "lower_wick_atr", "close_location_in_range", "close_location_bucket",
    # E. momentum
    "return_1bar", "return_3bar", "return_5bar", "return_10bar",
    "return_1bar_atr", "return_3bar_atr", "return_5bar_atr", "return_10bar_atr",
    "consecutive_up_bars", "consecutive_down_bars", "dist_from_5bar_high_atr", "dist_from_10bar_high_atr",
    # F. volatilidad
    "atr", "atr_pct", "atr_ratio_to_20bar_mean", "realized_vol_20bar_pct",
    # G. volumen / liquidez
    "signal_bar_volume", "avg_volume_5", "avg_volume_20", "volume_ratio_5", "volume_ratio_20",
    "risk_liquidity_dollar", "risk_atr", "risk_rr",
    # H. sesión
    "entry_time_et", "minutes_since_market_open", "session_bucket", "first_15_min", "first_30_min", "first_hour",
    # I. gap
    "opening_gap_pct", "gap_direction", "gap_same_direction_as_trade", "gap_prev_close",
    "gap_prev_close_timestamp", "gap_session_open",
]

NUMERIC_FEATURES = [
    "ma_spread_atr", "ma_spread_pct", "prev_ma_spread_atr",
    "fast_ma_slope_1bar_atr", "fast_ma_slope_3bar_atr", "slow_ma_slope_1bar_atr", "slow_ma_slope_3bar_atr",
    "spread_change_1bar_atr", "spread_change_3bar_atr",
    "bars_since_bullish_crossover", "prior_bearish_run_bars", "crossovers_last_20_bars",
    "close_minus_fast_ma_atr", "close_minus_slow_ma_atr", "close_minus_fast_ma_pct", "close_minus_slow_ma_pct",
    "bar_range_atr", "body_atr", "upper_wick_atr", "lower_wick_atr", "body_to_range_ratio",
    "close_location_in_range",
    "return_1bar", "return_3bar", "return_5bar", "return_10bar", "return_3bar_atr", "return_5bar_atr",
    "consecutive_up_bars", "consecutive_down_bars", "dist_from_5bar_high_atr", "dist_from_10bar_high_atr",
    "atr_pct", "atr_ratio_to_20bar_mean", "realized_vol_20bar_pct",
    "volume_ratio_5", "volume_ratio_20", "risk_liquidity_dollar", "risk_rr",
    "minutes_since_market_open", "opening_gap_pct",
]

CATEGORICAL_FEATURES = [
    "crossover_age_bucket", "slow_ma_slope_direction", "fast_ma_slope_direction", "spread_expanding",
    "ma_separated_before_bar", "bar_direction", "close_location_bucket", "session_bucket",
    "first_15_min", "first_30_min", "first_hour", "gap_direction", "gap_same_direction_as_trade", "symbol",
]

BUCKET_FEATURES = [
    "ma_spread_atr", "fast_ma_slope_3bar_atr", "slow_ma_slope_3bar_atr", "close_minus_fast_ma_atr",
    "bar_range_atr", "body_atr", "atr_pct", "atr_ratio_to_20bar_mean", "volume_ratio_20",
    "return_3bar", "return_5bar", "prior_bearish_run_bars", "opening_gap_pct",
]
CATEGORICAL_BUCKETS = ["crossover_age_bucket", "slow_ma_slope_direction", "session_bucket",
                       "close_location_bucket", "gap_direction", "first_30_min"]

OUTCOME_COLUMNS = ["realized_pnl", "realized_r", "mfe_r", "mae_r", "exit_reason", "diagnostic_class",
                   "never_worked", "reached_0_5r", "reached_1r", "reached_1_5r", "reached_2r",
                   "profitable", "loser", "clean_winner", "outcome_group"]
ID_COLUMNS = ["trade_id", "symbol", "entry_signal_timestamp", "entry_decision_timestamp", "entry_fill_timestamp"]
ROW_COLUMNS = ID_COLUMNS + FEATURE_COLUMNS + OUTCOME_COLUMNS

OUTCOME_LEVELS = (("reached_0_5r", 0.5), ("reached_1r", 1.0), ("reached_1_5r", 1.5), ("reached_2r", 2.0))


# ================================================================ features por entrada
def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else x


def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return _num(a / b)


def _pct_of(a: Optional[float], b: Optional[float]) -> Optional[float]:
    r = _div(a, b)
    return None if r is None else r * 100


def _direction(slope_atr: Optional[float]) -> Optional[str]:
    if slope_atr is None:
        return None
    if abs(slope_atr) < FLAT_SLOPE_ATR:
        return "flat"
    return "rising" if slope_atr > 0 else "falling"


def _age_bucket(age: Optional[int]) -> str:
    if age is None:
        return "unknown"
    return str(age) if age < 3 else "3+"


def session_bucket(minutes_since_open: Optional[float]) -> Optional[str]:
    if minutes_since_open is None or minutes_since_open < 0:
        return None
    if minutes_since_open < 15:
        return "09:30-09:45"
    if minutes_since_open < 30:
        return "09:45-10:00"
    if minutes_since_open < 60:
        return "10:00-10:30"
    if minutes_since_open < 90:
        return "10:30-11:00"
    return "after_11:00"


def _ny_ns(day, hm) -> int:
    return pd.Timestamp(datetime.combine(day, time(*hm))).tz_localize(NY).value


def _opening_gap(ts_ns: np.ndarray, o: np.ndarray, c: np.ndarray) -> Dict[str, Any]:
    """Gap con velas ya conocidas: la vela 09:30 de hoy debe estar en el pasado (o ser la de señal)."""
    out = {"opening_gap_pct": None, "gap_direction": None, "gap_same_direction_as_trade": None,
           "gap_prev_close": None, "gap_prev_close_timestamp": None, "gap_session_open": None}
    day = pd.Timestamp(int(ts_ns[-1]), tz="UTC").tz_convert(NY).date()
    open_ns = _ny_ns(day, (9, 30))
    j = int(np.searchsorted(ts_ns, open_ns, side="left"))
    if j >= len(ts_ns) or ts_ns[j] != open_ns:
        return out
    prev_close = prev_ts = None
    for back in range(1, GAP_LOOKBACK_DAYS + 1):
        d = day - timedelta(days=back)
        k = int(np.searchsorted(ts_ns, _ny_ns(d, (16, 0)), side="left")) - 1
        if k >= 0 and ts_ns[k] >= _ny_ns(d, (9, 30)):
            prev_close, prev_ts = float(c[k]), pd.Timestamp(int(ts_ns[k]), tz="UTC").isoformat()
            break
    if prev_close is None or prev_close <= 0:
        return out
    sess_open = float(o[j])
    gap = (sess_open - prev_close) / prev_close * 100
    direction = "up" if gap > 0 else ("down" if gap < 0 else "flat")
    out.update(opening_gap_pct=gap, gap_direction=direction, gap_same_direction_as_trade=direction == "up",
               gap_prev_close=prev_close, gap_prev_close_timestamp=prev_ts, gap_session_open=sess_open)
    return out


def entry_features(ts_ns: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
                   v: np.ndarray, *, strategy_window_start: int, fast: int, slow: int, atr_window: int,
                   tf_seconds: int) -> Dict[str, Any]:
    """Contexto de una entrada LARGA. Todos los arrays terminan en la vela de señal (índice -1)."""
    k = len(c) - 1
    close = float(c[k])
    f: Dict[str, Any] = {name: None for name in FEATURE_COLUMNS}

    # ---- ATR (misma fórmula que el stop del RiskManager) y su serie reciente
    def atr_at(m: int) -> Optional[float]:
        return _num(RiskManager._atr(h[:m + 1], l[:m + 1], c[:m + 1], atr_window)) if m >= atr_window else None

    atr = atr_at(k)
    f["atr"] = atr
    f["atr_pct"] = _pct_of(atr, close)
    if k - ATR_MEAN_WINDOW + 1 >= atr_window:
        series = [atr_at(m) for m in range(k - ATR_MEAN_WINDOW + 1, k + 1)]
        f["atr_ratio_to_20bar_mean"] = _div(atr, float(np.mean(series)))
    if k >= 20:
        rets = c[k - 19:k + 1] / c[k - 20:k] - 1.0
        f["realized_vol_20bar_pct"] = _num(np.std(rets, ddof=1) * 100)

    # ---- medias sobre la ventana de la estrategia
    ws = max(0, int(strategy_window_start))
    cw = pd.Series(np.asarray(c[ws:k + 1], dtype=float))
    fm = cw.rolling(fast).mean().to_numpy()
    sm = cw.rolling(slow).mean().to_numpy()
    sp = fm - sm
    n = len(cw)
    w = n - 1  # índice de la vela de señal dentro de la ventana

    def at(arr: np.ndarray, back: int) -> Optional[float]:
        return _num(arr[w - back]) if w - back >= 0 else None

    fast_ma, slow_ma = at(fm, 0), at(sm, 0)
    f["fast_ma"], f["slow_ma"] = fast_ma, slow_ma
    spread = at(sp, 0)
    f["ma_spread"] = spread
    f["ma_spread_pct"] = _pct_of(spread, close)
    f["ma_spread_atr"] = _div(spread, atr)
    for name, arr in (("fast_ma", fm), ("slow_ma", sm)):
        for nb in (1, 3):
            cur, past = at(arr, 0), at(arr, nb)
            slope = None if cur is None or past is None else (cur - past) / nb
            f[f"{name}_slope_{nb}bar"] = slope
            f[f"{name}_slope_{nb}bar_atr"] = _div(slope, atr)
    f["fast_ma_slope_direction"] = _direction(f["fast_ma_slope_3bar_atr"])
    f["slow_ma_slope_direction"] = _direction(f["slow_ma_slope_3bar_atr"])

    prev = at(sp, 1)
    f["prev_ma_spread"] = prev
    f["prev_ma_spread_atr"] = _div(prev, atr)
    f["ma_separated_before_bar"] = None if prev is None else bool(prev > 0)
    for nb in (1, 3):
        past = at(sp, nb)
        chg = None if spread is None or past is None else spread - past
        f[f"spread_change_{nb}bar"] = chg
        f[f"spread_change_{nb}bar_atr"] = _div(chg, atr)
    f["spread_expanding"] = None if f["spread_change_1bar"] is None else bool(f["spread_change_1bar"] > 0)

    # Edad del cruce alcista: inicio del tramo actual con spread > 0 (misma regla que MACrossover:
    # cruce = spread previo <= 0 y actual > 0). Si el tramo llega al borde de la ventana/NaN -> desconocido.
    age = None
    if spread is not None and spread > 0:
        s = w
        while s - 1 >= 0 and not np.isnan(sp[s - 1]) and sp[s - 1] > 0:
            s -= 1
        if s - 1 >= 0 and not np.isnan(sp[s - 1]):
            age = w - s
    f["bars_since_bullish_crossover"] = age
    f["crossover_age_bucket"] = _age_bucket(age)
    # Largo del tramo previo con spread <= 0 (cuánto estuvo abajo la rápida antes de cruzar).
    if age is not None:
        e = w - age - 1
        s = e
        while s - 1 >= 0 and not np.isnan(sp[s - 1]) and sp[s - 1] <= 0:
            s -= 1
        f["prior_bearish_run_bars"] = (e - s + 1) if (s - 1 >= 0 and not np.isnan(sp[s - 1])) else None
    if w - WHIPSAW_WINDOW >= 0 and not np.isnan(sp[w - WHIPSAW_WINDOW:w + 1]).any():
        pos = sp[w - WHIPSAW_WINDOW:w + 1] > 0
        f["crossovers_last_20_bars"] = int(np.count_nonzero(pos[1:] != pos[:-1]))

    # ---- extensión
    for name, ma in (("fast_ma", fast_ma), ("slow_ma", slow_ma)):
        d = None if ma is None else close - ma
        f[f"close_minus_{name}"] = d
        f[f"close_minus_{name}_atr"] = _div(d, atr)
        f[f"close_minus_{name}_pct"] = _pct_of(d, ma)

    # ---- vela de señal
    bo, bh, bl = float(o[k]), float(h[k]), float(l[k])
    rng = bh - bl
    body = abs(close - bo)
    f.update(signal_open=bo, signal_high=bh, signal_low=bl, signal_close=close, bar_range=rng, body_size=body,
             upper_wick=bh - max(bo, close), lower_wick=min(bo, close) - bl,
             bar_direction="up" if close > bo else ("down" if close < bo else "doji"))
    f["body_to_range_ratio"] = _div(body, rng)
    f["bar_range_atr"] = _div(rng, atr)
    f["body_atr"] = _div(body, atr)
    f["upper_wick_atr"] = _div(f["upper_wick"], atr)
    f["lower_wick_atr"] = _div(f["lower_wick"], atr)
    loc = _div(close - bl, rng)
    f["close_location_in_range"] = loc
    f["close_location_bucket"] = None if loc is None else ("low" if loc < 1 / 3 else ("middle" if loc < 2 / 3 else "high"))

    # ---- momentum
    for nb in (1, 3, 5, 10):
        if k - nb >= 0 and c[k - nb] > 0:
            f[f"return_{nb}bar"] = (close / float(c[k - nb]) - 1.0) * 100
            f[f"return_{nb}bar_atr"] = _div(close - float(c[k - nb]), atr)
    up = down = 0
    m = k
    while m >= 1 and c[m] > c[m - 1]:
        up += 1
        m -= 1
    m = k
    while m >= 1 and c[m] < c[m - 1]:
        down += 1
        m -= 1
    f["consecutive_up_bars"], f["consecutive_down_bars"] = (up, down) if k >= 1 else (None, None)
    for nb in (5, 10):
        if k - nb + 1 >= 0:
            f[f"dist_from_{nb}bar_high_atr"] = _div(close - float(np.max(h[k - nb + 1:k + 1])), atr)

    # ---- volumen (medias de velas ANTERIORES a la de señal)
    vol = float(v[k])
    f["signal_bar_volume"] = vol
    for nb in (5, 20):
        if k - nb >= 0:
            avg = float(np.mean(v[k - nb:k]))
            f[f"avg_volume_{nb}"] = avg
            f[f"volume_ratio_{nb}"] = _div(vol, avg)

    # ---- sesión (instante de decisión en NY)
    decision_ns = int(ts_ns[k]) + int(tf_seconds) * 10**9
    dec = pd.Timestamp(decision_ns, tz="UTC").tz_convert(NY)
    mins = (decision_ns - _ny_ns(dec.date(), (9, 30))) / 60e9
    f["entry_time_et"] = dec.strftime("%H:%M")
    f["minutes_since_market_open"] = mins
    f["session_bucket"] = session_bucket(mins)
    f["first_15_min"], f["first_30_min"], f["first_hour"] = mins < 15, mins < 30, mins < 60

    f.update(_opening_gap(ts_ns, o, c))
    return f


# ================================================================ unión con el resultado
def entry_quality_rows(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Una fila por trade COMPLETADO con contexto de entrada + resultado (join por trade_id)."""
    rows = []
    for t in trades:
        ctx = t.get("entry_context")
        if ctx is None:
            continue
        row = {k: t.get(k) for k in ID_COLUMNS}
        row.update({k: ctx.get(k) for k in FEATURE_COLUMNS})
        mfe = t.get("mfe_r")
        row.update(realized_pnl=t.get("realized_pnl"), realized_r=t.get("realized_r"), mfe_r=mfe,
                   mae_r=t.get("mae_r"), exit_reason=t.get("exit_reason"),
                   diagnostic_class=t.get("excursion_class"))
        row["never_worked"] = None if mfe is None else bool(mfe < 0.5)
        for name, level in OUTCOME_LEVELS:
            row[name] = None if mfe is None else bool(mfe >= level)
        row["profitable"] = t.get("result") == "win"
        row["loser"] = t.get("result") == "loss"
        row["clean_winner"] = t.get("excursion_class") == "clean_winner"
        row["outcome_group"] = (None if mfe is None else "reached_1r" if mfe >= 1.0
                                else "reached_0_5r_not_1r" if mfe >= 0.5 else "never_worked")
        rows.append(row)
    return rows


# ================================================================ agregados
def _pct(num: int, den: int) -> Optional[float]:
    return num / den * 100 if den else None


def numeric_stats(s: pd.Series) -> Dict[str, Any]:
    s = pd.to_numeric(s, errors="coerce").dropna().astype(float)
    if s.empty:
        return {"count": 0, "mean": None, "median": None, "p25": None, "p75": None}
    return {"count": int(len(s)), "mean": _num(s.mean()), "median": _num(s.median()),
            "p25": _num(s.quantile(0.25)), "p75": _num(s.quantile(0.75))}


def cles(a: pd.Series, b: pd.Series) -> Optional[float]:
    """P(valor de B > valor de A) + 0.5·P(empate): 0.5 = sin separación. Descriptivo (U de Mann-Whitney / nA·nB)."""
    a = pd.to_numeric(a, errors="coerce").dropna().astype(float)
    b = pd.to_numeric(b, errors="coerce").dropna().astype(float)
    if a.empty or b.empty:
        return None
    ranks = pd.concat([a, b], ignore_index=True).rank(method="average")
    u = ranks.iloc[len(a):].sum() - len(b) * (len(b) + 1) / 2
    return _num(u / (len(a) * len(b)))


def outcome_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    n = len(df)
    with_r = df[df["mfe_r"].notna()]
    rr = pd.to_numeric(df["realized_r"], errors="coerce").dropna()
    return {
        "trades": n,
        "win_rate_pct": _pct(int(df["profitable"].sum()), n),
        "pct_reached_0_5r": _pct(int(with_r["reached_0_5r"].astype(bool).sum()), len(with_r)),
        "pct_reached_1r": _pct(int(with_r["reached_1r"].astype(bool).sum()), len(with_r)),
        "avg_realized_r": _num(rr.mean()) if len(rr) else None,
        "median_realized_r": _num(rr.median()) if len(rr) else None,
        "avg_mfe_r": _num(with_r["mfe_r"].astype(float).mean()) if len(with_r) else None,
        "avg_mae_r": _num(with_r["mae_r"].astype(float).mean()) if len(with_r) else None,
        "total_pnl": _num(df["realized_pnl"].astype(float).sum()) if n else 0.0,
    }


def _key(v: Any) -> str:
    return "missing" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)


def _quantile_labels(s: pd.Series, q: int, prefix: str) -> pd.Series:
    """Q1..Qn por rango (empates repartidos de forma estable); nulos -> 'missing'."""
    out = pd.Series("missing", index=s.index, dtype=object)
    x = pd.to_numeric(s, errors="coerce").astype(float)
    ok = x.notna()
    if ok.sum() == 0:
        return out
    try:
        cats = pd.qcut(x[ok], q=q, labels=False, duplicates="drop")
    except ValueError:
        cats = pd.Series(0, index=x[ok].index)
    cats = cats.fillna(0)  # todos iguales: qcut no puede cortar -> un solo bucket
    out[ok] = [f"{prefix}{int(c) + 1}" for c in cats]
    return out


COMPARISONS = {
    "A_never_worked_vs_reached_0_5r": ("never_worked", "reached_0_5r"),
    "B_never_worked_vs_reached_1r": ("never_worked", "reached_1r"),
    "C_losers_vs_winners": ("loser", "profitable"),
    "D_others_vs_clean_winner": ("not_clean_winner", "clean_winner"),
}


def _group_mask(df: pd.DataFrame, name: str) -> pd.Series:
    if name == "never_worked":
        return df["never_worked"] == True  # noqa: E712 (None != True)
    if name in ("reached_0_5r", "reached_1r"):
        return df[name] == True  # noqa: E712
    if name == "not_clean_winner":
        return df["clean_winner"] == False  # noqa: E712
    return df[name] == True  # noqa: E712


def compare(df: pd.DataFrame, a_name: str, b_name: str) -> Dict[str, Any]:
    ma, mb = _group_mask(df, a_name), _group_mask(df, b_name)
    a, b = df[ma], df[mb]
    numeric = {}
    for feat in NUMERIC_FEATURES:
        sa, sb = numeric_stats(a[feat]), numeric_stats(b[feat])
        pooled = pd.to_numeric(pd.concat([a[feat], b[feat]]), errors="coerce").dropna().astype(float)
        iqr = float(pooled.quantile(0.75) - pooled.quantile(0.25)) if len(pooled) else 0.0
        diff = None if sa["median"] is None or sb["median"] is None else sb["median"] - sa["median"]
        numeric[feat] = {a_name: sa, b_name: sb, "median_diff_b_minus_a": diff,
                         "median_diff_over_pooled_iqr": _div(diff, iqr) if iqr > 0 else None,
                         "cles_b_gt_a": cles(a[feat], b[feat])}
    categorical = {}
    both = df[ma | mb]
    for feat in CATEGORICAL_FEATURES:
        values = sorted({_key(x) for x in both[feat]})
        table = {}
        for val in values:
            ca = int((a[feat].map(_key) == val).sum())
            cb = int((b[feat].map(_key) == val).sum())
            table[val] = {f"count_{a_name}": ca, f"pct_{a_name}": _pct(ca, len(a)),
                          f"count_{b_name}": cb, f"pct_{b_name}": _pct(cb, len(b)),
                          f"rate_{b_name}_among_value": _pct(cb, ca + cb)}
        categorical[feat] = table
    return {"groups": [a_name, b_name], f"n_{a_name}": int(len(a)), f"n_{b_name}": int(len(b)),
            "numeric": numeric, "categorical": categorical}


def bucket_tables(df: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for feat in BUCKET_FEATURES:
        labels = _quantile_labels(df[feat], QUANTILES, "Q")
        rows = []
        for lab in sorted(set(labels), key=lambda s: (s == "missing", s)):
            sub = df[labels == lab]
            vals = pd.to_numeric(sub[feat], errors="coerce").dropna()
            rows.append(dict(feature=feat, bucket=lab, lo=_num(vals.min()) if len(vals) else None,
                             hi=_num(vals.max()) if len(vals) else None, **outcome_metrics(sub)))
        out[feat] = rows
    for feat in CATEGORICAL_BUCKETS:
        keys = df[feat].map(_key)
        order = {"crossover_age_bucket": CROSSOVER_AGE_BUCKETS, "session_bucket": SESSION_BUCKETS,
                 "slow_ma_slope_direction": ("falling", "flat", "rising"),
                 "close_location_bucket": ("low", "middle", "high")}.get(feat, ())
        vals = [x for x in order if x in set(keys)] + sorted(x for x in set(keys) if x not in order)
        out[feat] = [dict(feature=feat, bucket=val, lo=None, hi=None, **outcome_metrics(df[keys == val]))
                     for val in vals]
    return out


CROSSTABS = [
    ("ma_spread_atr_tercile", "slow_ma_slope_direction"),
    ("crossover_age_bucket", "close_minus_fast_ma_atr_tercile"),
    ("prior_bearish_run_bars_tercile", "close_minus_fast_ma_atr_tercile"),
    ("volume_ratio_20_tercile", "bar_range_atr_tercile"),
    ("first_30_min", "slow_ma_slope_direction"),
    ("first_30_min", "ma_spread_atr_tercile"),
]


def crosstabs(df: pd.DataFrame) -> List[Dict[str, Any]]:
    d = df.copy()
    for feat in ("ma_spread_atr", "close_minus_fast_ma_atr", "prior_bearish_run_bars", "volume_ratio_20",
                 "bar_range_atr"):
        d[f"{feat}_tercile"] = _quantile_labels(d[feat], TERCILES, "T")
    rows = []
    for rf, cf in CROSSTABS:
        rk, ck = d[rf].map(_key), d[cf].map(_key)
        for rv in sorted(set(rk)):
            for cv in sorted(set(ck)):
                sub = d[(rk == rv) & (ck == cv)]
                if len(sub):
                    m = outcome_metrics(sub)
                    rows.append({"crosstab": f"{rf} x {cf}", "row_feature": rf, "row_value": rv,
                                 "col_feature": cf, "col_value": cv,
                                 **{k: m[k] for k in ("trades", "win_rate_pct", "pct_reached_0_5r",
                                                      "pct_reached_1r", "avg_realized_r", "total_pnl")}})
    return rows


CORR_TARGETS = ("realized_r", "mfe_r", "mae_r")


def correlations(df: pd.DataFrame) -> Dict[str, Any]:
    out = {}
    for feat in NUMERIC_FEATURES:
        x = pd.to_numeric(df[feat], errors="coerce").astype(float)
        entry = {}
        for tgt in CORR_TARGETS:
            y = pd.to_numeric(df[tgt], errors="coerce").astype(float)
            ok = x.notna() & y.notna()
            n = int(ok.sum())
            if n < 10 or x[ok].nunique() < 2 or y[ok].nunique() < 2:
                entry[tgt] = {"n": n, "pearson": None, "spearman": None}
                continue
            # Spearman = Pearson sobre rangos promedio (sin depender de scipy).
            entry[tgt] = {"n": n, "pearson": _num(x[ok].corr(y[ok])),
                          "spearman": _num(x[ok].rank().corr(y[ok].rank()))}
        out[feat] = entry
    return out


def _sorted_abs(corr: Dict[str, Any], tgt: str, method: str) -> List[Dict[str, Any]]:
    items = [{"feature": f, method: v[tgt][method], "n": v[tgt]["n"]} for f, v in corr.items()
             if v[tgt][method] is not None]
    return sorted(items, key=lambda r: (-abs(r[method]), r["feature"]))


def entry_quality_summary(trades: Sequence[Dict[str, Any]], accepted_entries: Optional[int] = None) -> Dict[str, Any]:
    rows = entry_quality_rows(trades)
    base = {"disclaimer": DISCLAIMER, "accepted_entries": accepted_entries, "completed_with_context": len(rows),
            "definitions": {
                "no_look_ahead": "features use only bars up to and including the signal bar (known at decision_ts)",
                "never_worked": "MFE < +0.5R on bar closes (differs from excursion class, which also needs P&L<=0)",
                "slope": "(ma[k]-ma[k-N])/N per bar; *_atr divided by ATR(RiskManager formula)",
                "slope_direction": f"flat if |slope_3bar_atr| < {FLAT_SLOPE_ATR}, else rising/falling",
                "pct_units": "*_pct, return_Nbar and opening_gap_pct are percentages",
                "avg_volume_N": "mean of the N bars BEFORE the signal bar",
                "cles_b_gt_a": "P(B value > A value) + 0.5 P(tie); 0.5 = no separation",
                "buckets": f"quintiles by rank over completed trades (Q1 lowest); crosstab terciles T1..T{TERCILES}",
                "session": "decision timestamp (signal bar start + timeframe) in America/New_York",
                "crossover": "MACrossover BUYs only on the bar where spread goes from <=0 to >0, so the "
                             "crossover age of an accepted BUY is structurally 0",
            }}
    if not rows:
        return dict(base, outcomes={}, comparisons={}, buckets={}, crosstabs=[], correlations={},
                    correlation_sorted_for_inspection={}, answers={})
    df = pd.DataFrame(rows, columns=ROW_COLUMNS)
    with_r = df[df["mfe_r"].notna()]
    outcomes = {"completed": len(df), "with_r": len(with_r),
                "never_worked": int((with_r["never_worked"] == True).sum()),  # noqa: E712
                **{lvl: int((with_r[lvl] == True).sum()) for lvl, _ in OUTCOME_LEVELS},  # noqa: E712
                "profitable": int(df["profitable"].sum()), "losers": int(df["loser"].sum()),
                "clean_winner": int(df["clean_winner"].sum())}
    comps = {name: compare(df, a, b) for name, (a, b) in COMPARISONS.items()}
    buckets = bucket_tables(df)
    xt = crosstabs(df)
    corr = correlations(df)
    sorted_corr = {f"{tgt}_{m}": _sorted_abs(corr, tgt, m) for tgt in CORR_TARGETS for m in ("pearson", "spearman")}
    summary = dict(base, outcomes=outcomes, comparisons=comps, buckets=buckets, crosstabs=xt, correlations=corr,
                   correlation_sorted_for_inspection=sorted_corr)
    summary["answers"] = _answers(df, comps, buckets, xt)
    return summary


# ================================================================ respuestas (descriptivas)
def _med(comp: Dict[str, Any], feat: str) -> Dict[str, Any]:
    a, b = comp["groups"]
    n = comp["numeric"][feat]
    return {a: n[a]["median"], b: n[b]["median"], "cles_b_gt_a": n["cles_b_gt_a"]}


def _share(df: pd.DataFrame, mask: pd.Series, cond: pd.Series) -> Optional[float]:
    sub = cond[mask]
    return _pct(int(sub.sum()), int(mask.sum()))


def _answers(df, comps, buckets, xt) -> Dict[str, Any]:
    A, B, C = (comps["A_never_worked_vs_reached_0_5r"], comps["B_never_worked_vs_reached_1r"],
               comps["C_losers_vs_winners"])
    nw, r1 = _group_mask(df, "never_worked"), _group_mask(df, "reached_1r")
    los, win = df["loser"] == True, df["profitable"] == True  # noqa: E712
    vr = pd.to_numeric(df["volume_ratio_20"], errors="coerce")
    rising = df["slow_ma_slope_direction"] == "rising"
    chg3 = pd.to_numeric(df["spread_change_3bar_atr"], errors="coerce") > 0

    def by(feat: str) -> Dict[str, Any]:
        return {r["bucket"]: {k: r[k] for k in ("trades", "win_rate_pct", "pct_reached_0_5r", "pct_reached_1r",
                                               "avg_realized_r", "total_pnl")} for r in buckets[feat]}

    def split(mask_hi: pd.Series) -> Dict[str, Any]:
        return {"yes": outcome_metrics(df[mask_hi]), "no": outcome_metrics(df[~mask_hi])}

    q6 = {}
    for row in xt:
        if row["row_feature"] == "first_30_min":
            q6.setdefault(row["col_feature"], {}).setdefault(row["col_value"], {})[
                "first_30_min" if row["row_value"] == "True" else "later"] = {
                k: row[k] for k in ("trades", "win_rate_pct", "pct_reached_1r", "avg_realized_r")}
    atr_q = pd.to_numeric(df["atr_pct"], errors="coerce")
    top_atr = atr_q >= atr_q.quantile(0.8) if atr_q.notna().any() else pd.Series(False, index=df.index)
    ages = df["crossover_age_bucket"].map(_key).value_counts().to_dict()
    sep = []
    for feat, v in B["numeric"].items():
        if v["cles_b_gt_a"] is None:
            continue
        sep.append({"feature": feat, "median_never_worked": v["never_worked"]["median"],
                    "median_reached_1r": v["reached_1r"]["median"], "cles_reached_1r_gt_never_worked": v["cles_b_gt_a"],
                    "median_diff_over_pooled_iqr": v["median_diff_over_pooled_iqr"]})
    sep.sort(key=lambda r: (-abs(r["cles_reached_1r_gt_never_worked"] - 0.5), r["feature"]))
    return {
        "q1_ma_separation": {"ma_spread_atr": {"A": _med(A, "ma_spread_atr"), "B": _med(B, "ma_spread_atr"),
                                               "C": _med(C, "ma_spread_atr")},
                             "ma_spread_pct": {"B": _med(B, "ma_spread_pct"), "C": _med(C, "ma_spread_pct")},
                             "buckets_ma_spread_atr": by("ma_spread_atr")},
        "q2_slow_ma_direction": {"by_direction": by("slow_ma_slope_direction"),
                                 "slow_ma_slope_3bar_atr": {"B": _med(B, "slow_ma_slope_3bar_atr"),
                                                            "C": _med(C, "slow_ma_slope_3bar_atr")}},
        "q3_extension": {"close_minus_fast_ma_atr": {"B": _med(B, "close_minus_fast_ma_atr"),
                                                     "C": _med(C, "close_minus_fast_ma_atr")},
                         "close_minus_slow_ma_atr": {"B": _med(B, "close_minus_slow_ma_atr"),
                                                     "C": _med(C, "close_minus_slow_ma_atr")}},
        "q4_success_context": {
            "spread_expanding_1bar_pct": {"never_worked": _share(df, nw, df["spread_expanding"] == True),  # noqa: E712
                                          "reached_1r": _share(df, r1, df["spread_expanding"] == True)},  # noqa: E712
            "spread_change_3bar_positive_pct": {"never_worked": _share(df, nw, chg3), "reached_1r": _share(df, r1, chg3),
                                                "losers": _share(df, los, chg3), "winners": _share(df, win, chg3)},
            "slow_ma_rising_pct": {"never_worked": _share(df, nw, rising), "reached_1r": _share(df, r1, rising),
                                   "losers": _share(df, los, rising), "winners": _share(df, win, rising)},
            "volume_ratio_20_above_1_pct": {"never_worked": _share(df, nw, vr > 1), "reached_1r": _share(df, r1, vr > 1),
                                            "losers": _share(df, los, vr > 1), "winners": _share(df, win, vr > 1)},
            "volume_ratio_20": {"B": _med(B, "volume_ratio_20"), "C": _med(C, "volume_ratio_20")},
            "body_atr": {"B": _med(B, "body_atr"), "C": _med(C, "body_atr")},
            "close_location_in_range": {"B": _med(B, "close_location_in_range"), "C": _med(C, "close_location_in_range")},
            "spread_change_3bar_atr": {"B": _med(B, "spread_change_3bar_atr"), "C": _med(C, "spread_change_3bar_atr")},
        },
        "q5_crossover_age": {"age_bucket_counts": ages,
                             "note": "BUY fires only on the crossing bar, so age is 0 by construction; "
                                     "prior_bearish_run_bars and crossovers_last_20_bars shown instead",
                             "prior_bearish_run_bars": {"B": _med(B, "prior_bearish_run_bars"),
                                                        "C": _med(C, "prior_bearish_run_bars")},
                             "crossovers_last_20_bars": {"B": _med(B, "crossovers_last_20_bars"),
                                                         "C": _med(C, "crossovers_last_20_bars")},
                             "buckets_prior_bearish_run_bars": by("prior_bearish_run_bars")},
        "q6_first_30_min_stratified": {"overall": split(df["first_30_min"] == True), **q6},  # noqa: E712
        "q7_volatility": {"top_quintile_atr_pct_vs_rest": split(top_atr), "buckets_atr_pct": by("atr_pct"),
                          "buckets_atr_ratio_to_20bar_mean": by("atr_ratio_to_20bar_mean")},
        "q8_separation_never_worked_vs_reached_1r": {
            "note": "sorted by |CLES - 0.5| for inspection only; descriptive, not a ranking of 'best' features",
            "features": sep},
    }


# ================================================================ consola
def _v(x: Optional[float], fmt: str = "{:+.3f}") -> str:
    return "-" if x is None else fmt.format(x)


def _p(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.1f}%"


MEDIAN_TABLE = [("MA spread / ATR", "ma_spread_atr"), ("Prev spread / ATR", "prev_ma_spread_atr"),
                ("Fast slope 3b / ATR", "fast_ma_slope_3bar_atr"), ("Slow slope 3b / ATR", "slow_ma_slope_3bar_atr"),
                ("Spread chg 3b / ATR", "spread_change_3bar_atr"),
                ("Price ext. fast / ATR", "close_minus_fast_ma_atr"), ("Bar range / ATR", "bar_range_atr"),
                ("Body / ATR", "body_atr"), ("Close loc. in range", "close_location_in_range"),
                ("Return 5 bar %", "return_5bar"), ("ATR %", "atr_pct"), ("ATR / 20-bar mean", "atr_ratio_to_20bar_mean"),
                ("Volume ratio 20", "volume_ratio_20"), ("Bars since crossover", "bars_since_bullish_crossover"),
                ("Prior bearish run", "prior_bearish_run_bars"), ("Minutes since open", "minutes_since_market_open")]


def format_entry_quality(s: Dict[str, Any]) -> str:
    if not s.get("outcomes"):
        return "ENTRY QUALITY DIAGNOSTICS\n" + "─" * 44 + "\nNo completed trades with entry context."
    o, a = s["outcomes"], s["answers"]
    B = s["comparisons"]["B_never_worked_vs_reached_1r"]["numeric"]
    L = ["ENTRY QUALITY DIAGNOSTICS (descriptive only; features known at decision time)", "─" * 44,
         f"Accepted entries: {s['accepted_entries']} | completed with context: {s['completed_with_context']}",
         "", "Outcome (MFE on bar closes):",
         f"  Never worked (<+0.5R): {o['never_worked']}",
         f"  Reached +0.5R: {o['reached_0_5r']} | +1R: {o['reached_1r']} | +1.5R: {o['reached_1_5r']} | +2R: {o['reached_2r']}",
         f"  Profitable: {o['profitable']} | clean winners: {o['clean_winner']}",
         "", f"{'Median feature':<24}{'never_worked':>14}{'reached_1R':>12}{'CLES':>7}"]
    for label, feat in MEDIAN_TABLE:
        v = B[feat]
        L.append(f"{label:<24}{_v(v['never_worked']['median']):>14}{_v(v['reached_1r']['median']):>12}"
                 f"{_v(v['cles_b_gt_a'], '{:.2f}'):>7}")
    L.append("(CLES = P(reached_1R value > never_worked value); 0.50 = no separation)")

    q2 = a["q2_slow_ma_direction"]["by_direction"]
    L += ["", f"{'slow MA (3b)':<14}{'n':>5}{'win%':>7}{'≥0.5R':>7}{'≥1R':>7}{'avgR':>8}{'P&L':>12}"]
    for k, r in q2.items():
        L.append(f"{k:<14}{r['trades']:>5}{_p(r['win_rate_pct']):>7}{_p(r['pct_reached_0_5r']):>7}"
                 f"{_p(r['pct_reached_1r']):>7}{_v(r['avg_realized_r'], '{:+.2f}'):>8}{r['total_pnl']:>12,.0f}")
    L += ["", f"{'MA spread/ATR':<14}{'range':>17}{'n':>5}{'win%':>7}{'≥1R':>7}{'avgR':>8}"]
    for r in s["buckets"]["ma_spread_atr"]:
        rng = "-" if r["lo"] is None else f"{r['lo']:.3f}..{r['hi']:.3f}"
        L.append(f"{r['bucket']:<14}{rng:>17}{r['trades']:>5}{_p(r['win_rate_pct']):>7}{_p(r['pct_reached_1r']):>7}"
                 f"{_v(r['avg_realized_r'], '{:+.2f}'):>8}")

    q4, q5, q6, q7 = a["q4_success_context"], a["q5_crossover_age"], a["q6_first_30_min_stratified"], a["q7_volatility"]
    sep = a["q8_separation_never_worked_vs_reached_1r"]["features"][:6]
    top, rest = q7["top_quintile_atr_pct_vs_rest"]["yes"], q7["top_quintile_atr_pct_vs_rest"]["no"]
    f30, lat = q6["overall"]["yes"], q6["overall"]["no"]
    L += ["", "Observations (descriptive, not recommendations):",
          f"  - Crossover age buckets: {q5['age_bucket_counts']} ({q5['note'].split(';')[0]}).",
          f"  - Slow MA rising: never_worked {_p(q4['slow_ma_rising_pct']['never_worked'])} vs reached_1R "
          f"{_p(q4['slow_ma_rising_pct']['reached_1r'])}; losers {_p(q4['slow_ma_rising_pct']['losers'])} vs winners "
          f"{_p(q4['slow_ma_rising_pct']['winners'])}.",
          f"  - Spread 3-bar change > 0: never_worked {_p(q4['spread_change_3bar_positive_pct']['never_worked'])} vs "
          f"reached_1R {_p(q4['spread_change_3bar_positive_pct']['reached_1r'])} (1-bar expansion is "
          f"{_p(q4['spread_expanding_1bar_pct']['never_worked'])} in both by construction of the crossover).",
          f"  - Volume ratio 20 > 1: never_worked {_p(q4['volume_ratio_20_above_1_pct']['never_worked'])} vs "
          f"reached_1R {_p(q4['volume_ratio_20_above_1_pct']['reached_1r'])}.",
          f"  - First 30 min: n={f30['trades']} win {_p(f30['win_rate_pct'])} ≥1R {_p(f30['pct_reached_1r'])} "
          f"avgR {_v(f30['avg_realized_r'], '{:+.2f}')} | later: n={lat['trades']} win {_p(lat['win_rate_pct'])} "
          f"≥1R {_p(lat['pct_reached_1r'])} avgR {_v(lat['avg_realized_r'], '{:+.2f}')}.",
          f"  - Top-quintile ATR%: n={top['trades']} ≥1R {_p(top['pct_reached_1r'])} avgR "
          f"{_v(top['avg_realized_r'], '{:+.2f}')} | rest: n={rest['trades']} ≥1R {_p(rest['pct_reached_1r'])} "
          f"avgR {_v(rest['avg_realized_r'], '{:+.2f}')}.",
          "  - Largest |CLES-0.5| never_worked vs reached_1R (inspection only): "
          + ", ".join(f"{r['feature']} {r['cles_reached_1r_gt_never_worked']:.2f}" for r in sep) + ".",
          f"  {DISCLAIMER} Details: entry_quality_summary.json / _buckets.csv / _crosstabs.csv"]
    return "\n".join(L)
