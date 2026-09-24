# src/strategy_v2_h001.py
"""
STRATEGY_V2_HYPOTHESIS_001 — 5Min RTH trend + pullback continuation (long-only).
Especificación congelada: research/strategy_v2_hypothesis_001.md (reglas y
constantes fijas; no se optimiza nada). SOLO investigación/backtest: no se usa
en run_paper ni toca comportamiento en vivo.

Contenido
---------
- resample_rth_5min(): velas 5Min desde la caché 1Min auditada (§3).
- TrendPullbackH001.evaluate(df): señal en la última vela de df (§4-§5).
- development_gates() / d6_concentration(): compuertas pre-registradas (§10).
- check_split_allowed(): guarda de higiene (§9): development sí; validation
  solo con H001 congelada y nunca vista; contaminado/forward nunca.

Nota de implementación (§4 + §5.5)
----------------------------------
El indicador de cada vela i se calcula sobre SU ventana: las últimas W=200
velas que terminan en i (EMA con ewm(span, adjust=False) sembrada al inicio
de esa ventana; ATR con RiskManager._atr sobre velas <= i). Es exactamente lo
que se calculó cuando i fue la vela de señal, así que al re-jugar la sesión
(§5.5) las señales "emitidas" coinciden con las que se emitieron en su momento
(consumo por emisión, Q6) y no dependen de dónde empieza la ventana del motor.
Por eso el motor pasa ENGINE_LOOKBACK = W + 78 = 278 velas: la ventana propia
(W) de la vela más vieja posible de la sesión más la propia sesión (<= 78 velas).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .historical_audit import EARLY_CLOSES
from .risk_manager_avanzado import RiskManager
from .strategy import StrategyResult

HYPOTHESIS_ID = "STRATEGY_V2_HYPOTHESIS_001"
NY = "America/New_York"

# ---- constantes pre-registradas (§5); NO optimizar
EMA_FAST = 20
EMA_SLOW = 50
SLOPE_BARS = 3
PULLBACK_BARS = 5
TOUCH_ATR_MULT = 0.50
ATR_WINDOW = 14
INDICATOR_WINDOW = 200
WARMUP_BARS = 150
MIN_SESSION_BAR = 6               # T debe ser >= 6a vela de su sesión
SUPPORT_BARS = 200                # velas de soporte antes de cada split (Q3)
TIMEFRAME = "5Min"
BAR_MINUTES = 5
MAX_SESSION_BARS = 78             # 390 min / 5
ENGINE_LOOKBACK = INDICATOR_WINDOW + MAX_SESSION_BARS   # ventana propia de la 1a vela de la sesión + la sesión
RTH_OPEN_MIN = 9 * 60 + 30
RTH_CLOSE_MIN = 16 * 60
EARLY_CLOSE_MIN = 13 * 60


# ================================================================ §3 resampleo
def resample_rth_5min(df1: pd.DataFrame) -> pd.DataFrame:
    """
    1Min (índice UTC = inicio de vela) -> 5Min RTH. Solo velas 1Min cuyo INICIO cae en [09:30, 16:00) NY
    ([09:30, 13:00) en EARLY_CLOSES). Cubetas [t, t+5) alineadas a 09:30. Cubeta vacía = sin vela.
    Columnas: open, high, low, close, volume, n_minutes (auditoría).
    """
    cols = ["open", "high", "low", "close", "volume", "n_minutes"]
    if df1.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
    ny = df1.index.tz_convert(NY)
    minute = ny.hour * 60 + ny.minute
    close_min = np.where([d in EARLY_CLOSES for d in ny.date], EARLY_CLOSE_MIN, RTH_CLOSE_MIN)
    keep = (ny.weekday < 5) & (minute >= RTH_OPEN_MIN) & (minute < close_min)
    rth = df1[np.asarray(keep)]
    if rth.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
    ny_rth = rth.index.tz_convert(NY)
    # 09:30 es múltiplo de 5 min desde medianoche: floor de reloj == alineado a 09:30
    bucket = ny_rth.floor(f"{BAR_MINUTES}min").tz_convert("UTC")
    g = rth.groupby(bucket)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(),
                        "n_minutes": g["close"].size().astype(int)})
    out.index = pd.DatetimeIndex(out.index, name="timestamp")
    return out[cols]


# ================================================================ §4 indicadores
def _own_window_ema(close: np.ndarray, idx: Sequence[int], span: int) -> np.ndarray:
    """EMA de cada vela i en idx sobre SU ventana (hasta INDICATOR_WINDOW velas que terminan en i)."""
    out = np.empty(len(idx))
    full = [i for i in idx if i + 1 >= INDICATOR_WINDOW]
    if full:
        # una columna por vela: pandas aplica a cada columna el mismo ewm que a una Series
        win = np.lib.stride_tricks.sliding_window_view(close, INDICATOR_WINDOW)[[i - INDICATOR_WINDOW + 1 for i in full]]
        vals = pd.DataFrame(win.T).ewm(span=span, adjust=False).mean().iloc[-1].to_numpy()
        lookup = dict(zip(full, vals))
    else:
        lookup = {}
    for k, i in enumerate(idx):
        if i in lookup:
            out[k] = lookup[i]
        else:  # historia más corta que W (solo durante el calentamiento inicial)
            out[k] = pd.Series(close[: i + 1]).ewm(span=span, adjust=False).mean().iloc[-1]
    return out


def _atr_at(h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int) -> float:
    v = RiskManager._atr(h[: i + 1], l[: i + 1], c[: i + 1], ATR_WINDOW)
    return float("nan") if v is None else float(v)


def session_ids(index: pd.DatetimeIndex) -> np.ndarray:
    """Fecha NY de cada vela (las velas son RTH, así que fecha = sesión)."""
    return np.asarray(index.tz_convert(NY).date)


# ================================================================ §5 reglas
def conditions_at(j: int, s0: int, o, h, l, c, e20, e50, atr) -> Dict[str, bool]:
    """
    Condiciones §5.1-§5.4 en la vela j (índices en los arrays; s0 = primera vela de la sesión de j).
    Los arrays de indicadores deben estar definidos para j-5..j (y j-3).
    """
    p = range(j - PULLBACK_BARS, j)
    pos_ok = j - s0 >= MIN_SESSION_BAR - 1
    if not pos_ok:
        return {"session_position": False}
    return {
        "session_position": True,
        "ema20_gt_ema50": e20[j] > e50[j],
        "ema20_slope_pos": (e20[j] - e20[j - SLOPE_BARS]) > 0,
        "ema50_slope_nonneg": (e50[j] - e50[j - SLOPE_BARS]) >= 0,
        "close_gt_ema50": c[j] > e50[j],
        "pullback_touch": any(l[i] <= e20[i] + TOUCH_ATR_MULT * atr[i] for i in p),
        "pullback_hold": all(c[i] >= e50[i] for i in p),
        "trigger_close_gt_prev_high": c[j] > h[j - 1],
        "trigger_close_gt_ema20": c[j] > e20[j],
        "trigger_close_gt_open": c[j] > o[j],
    }


def replay_session(s0: int, t: int, o, h, l, c, e20, e50, atr, eligible) -> List[int]:
    """
    §5.5: re-juega la sesión [s0, t] y devuelve los índices donde se EMITE BUY.
    Una BUY en S consume el pullback; una BUY posterior exige una vela de toque con índice > S
    dentro de su propia ventana de pullback. El estado no cruza sesiones (arranca vacío en s0).
    """
    emitted: List[int] = []
    last: Optional[int] = None
    for j in range(s0, t + 1):
        if not eligible[j]:
            continue
        cond = conditions_at(j, s0, o, h, l, c, e20, e50, atr)
        if not all(cond.values()):
            continue
        if last is not None and not any(i > last and l[i] <= e20[i] + TOUCH_ATR_MULT * atr[i]
                                        for i in range(j - PULLBACK_BARS, j)):
            continue  # re-arm no cumplido: mismo pullback
        emitted.append(j)
        last = j
    return emitted


class TrendPullbackH001:
    """Estrategia de investigación H001. evaluate(df): df = velas 5Min RTH completas, la última es T."""

    id = HYPOTHESIS_ID
    min_bars = WARMUP_BARS

    def evaluate(self, df: pd.DataFrame) -> StrategyResult:
        n = len(df)
        if n < WARMUP_BARS:
            return StrategyResult(None, f"Warm-up insuficiente ({n}/{WARMUP_BARS} velas)", warmup_ok=False)
        t = n - 1
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        sess = session_ids(df.index[max(0, t - MAX_SESSION_BARS):])
        off = max(0, t - MAX_SESSION_BARS)
        s0 = t
        while s0 - 1 >= off and sess[s0 - 1 - off] == sess[t - off]:
            s0 -= 1
        if t - s0 < MIN_SESSION_BAR - 1:
            return StrategyResult(None, "Antes de la 6a vela de la sesión")
        # prefiltro barato (mismas reglas; si fallan en T no hay BUY en T)
        if not (c[t] > h[t - 1] and c[t] > o[t]):
            return StrategyResult(None, "Sin gatillo de continuación")
        idx = list(range(s0, t + 1))
        e20 = np.full(n, np.nan)
        e50 = np.full(n, np.nan)
        atr = np.full(n, np.nan)
        e20[idx] = _own_window_ema(c, idx, EMA_FAST)
        e50[idx] = _own_window_ema(c, idx, EMA_SLOW)
        for i in idx:
            atr[i] = _atr_at(h, l, c, i)
        # elegible = ventana propia de j con >= WARMUP_BARS velas (lo que valía cuando j fue evaluada)
        eligible = np.zeros(n, dtype=bool)
        eligible[idx] = [i + 1 >= WARMUP_BARS for i in idx]
        emitted = replay_session(s0, t, o, h, l, c, e20, e50, atr, eligible)
        values = {"ema20": float(e20[t]), "ema50": float(e50[t]), "atr": float(atr[t]),
                  "session_bar": int(t - s0 + 1), "session_signals_before": [int(j - s0 + 1) for j in emitted if j < t]}
        if emitted and emitted[-1] == t:
            return StrategyResult("BUY", "H001: tendencia + pullback + gatillo de continuación", values)
        return StrategyResult(None, "H001: sin señal", values)

    def signal(self, df: pd.DataFrame) -> Optional[str]:
        return self.evaluate(df).signal


# ================================================================ §10 compuertas
def d6_concentration(trades: Sequence[Dict[str, Any]], symbols: Sequence[str]) -> Dict[str, Any]:
    """D6 (Q7): P_s = Σ realized_pnl de trades COMPLETADOS; Π = Σ max(P_s,0); pasa sii Π > 0 y max share <= 0.50."""
    p = {s: 0.0 for s in symbols}
    for t in trades:
        p[t["symbol"]] = p.get(t["symbol"], 0.0) + float(t["realized_pnl"])
    pool = sum(max(v, 0.0) for v in p.values())
    if pool > 0:
        shares = {s: max(v, 0.0) / pool for s, v in p.items()}
        top = max(shares, key=lambda s: (shares[s], s))
        return {"per_symbol_pnl": p, "positive_pool": pool, "shares": shares, "max_share": shares[top],
                "max_share_symbol": top, "threshold": 0.50, "passed": shares[top] <= 0.50, "note": None}
    return {"per_symbol_pnl": p, "positive_pool": pool, "shares": None, "max_share": None, "max_share_symbol": None,
            "threshold": 0.50, "passed": False, "note": "undefined: empty positive pool"}


def development_gates(summary: Dict[str, Any], trades: Sequence[Dict[str, Any]],
                      symbols: Sequence[str]) -> Dict[str, Any]:
    t, p = summary["trades"], summary["portfolio"]
    exp_r, pf, n, dd, tot = t["expectancy_r"], t["profit_factor"], t["trades"], p["max_drawdown_pct"], t["total_r"]

    def g(value, rule, passed):
        return {"value": value, "rule": rule, "passed": bool(passed)}

    gates = {
        "D1_expectancy_r": g(exp_r, "> 0", exp_r is not None and exp_r > 0),
        "D2_profit_factor": g(pf, ">= 1.10", pf is not None and pf >= 1.10),
        "D3_completed_trades": g(n, ">= 150", n >= 150),
        "D4_max_drawdown_pct": g(dd, "abs <= 25", abs(dd) <= 25),
        "D5_total_r": g(tot, "> 0", tot is not None and tot > 0),
    }
    d6 = d6_concentration(trades, symbols)
    gates["D6_symbol_concentration"] = dict(g(d6["max_share"], "positive-pool max share <= 0.50 (empty pool = FAIL)",
                                              d6["passed"]), detail=d6)
    return {"gates": gates, "all_passed": all(v["passed"] for v in gates.values()),
            "progression_to_validation": "PASS" if all(v["passed"] for v in gates.values()) else "FAIL"}


# ================================================================ §9 higiene
class HygieneError(RuntimeError):
    pass


def check_split_allowed(entry: Dict[str, Any], split: Dict[str, Any], dev_gate_passed: Optional[bool] = None) -> None:
    """Development: permitido. Validation: solo FROZEN, compuerta D aprobada y nunca vista. Resto: prohibido."""
    role = split["role"]
    if role == "development":
        if entry.get("status") in ("REJECTED_AT_DEVELOPMENT", "REJECTED_AT_VALIDATION", "VALIDATION_VIEWED"):
            raise HygieneError(f"{entry['id']} está {entry['status']}: no se re-corre development")
        return
    if role == "validation":
        if entry.get("status") != "FROZEN":
            raise HygieneError(f"validation requiere {entry['id']} FROZEN (estado actual: {entry.get('status')})")
        if entry.get("validation_viewed_at"):
            raise HygieneError(f"validation de {entry['id']} ya fue vista ({entry['validation_viewed_at']}); una sola vez")
        if dev_gate_passed is not True:
            raise HygieneError("validation requiere la compuerta de development aprobada")
        return
    raise HygieneError(f"split '{split['name']}' (rol {role}) no está permitido para {entry['id']}")


# ================================================================ datos por split
def split_bars(bars5: pd.DataFrame, start: str, end: str) -> Dict[str, Any]:
    """Soporte = las SUPPORT_BARS velas 5Min inmediatamente anteriores a la primera sesión del split (Q3)."""
    s_utc = pd.Timestamp(start).tz_localize(NY).tz_convert("UTC")
    e_utc = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    before = bars5[bars5.index < s_utc]
    inside = bars5[(bars5.index >= s_utc) & (bars5.index < e_utc)]
    support = before.iloc[-SUPPORT_BARS:]
    return {"bars": pd.concat([support, inside]), "support_bars": int(len(support)),
            "support_first": support.index[0].isoformat() if len(support) else None,
            "support_last": support.index[-1].isoformat() if len(support) else None,
            "split_bars": int(len(inside))}

