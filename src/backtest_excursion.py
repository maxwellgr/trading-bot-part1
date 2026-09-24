# src/backtest_excursion.py
"""
Diagnóstico MFE/MAE (Maximum Favorable / Adverse Excursion) por trade y
agregado. SOLO medición: nada de aquí participa en decisiones, fills ni P&L.

Definiciones (largos)
---------------------
- Fuente primaria: CIERRES de vela, que es lo único que ven los stops por
  software del bot en vivo. Cuentan los cierres de las velas durante las que
  el trade tenía acciones al cierre: desde la vela del fill de entrada (el
  fill ocurre en su apertura) hasta la última vela antes del fill de salida
  final (esa vela abre ya sin posición). Los scale-outs no cortan la
  medición: sigue hasta que se cierra la última acción.
- mfe_dollars_per_share = max(cierre) - entry_fill_price   (puede ser < 0)
- mae_dollars_per_share = entry_fill_price - min(cierre)   (positivo = en contra; puede ser < 0)
- *_timestamp = momento en que ese cierre fue conocido (inicio de vela + timeframe);
  empates: la primera vez. minutes_to_* se mide desde el fill de entrada.
- R = / risk_per_share_modeled (|entrada modelada - stop inicial|, el mismo
  denominador que realized_r). Sin riesgo inicial válido -> null.
- intrabar_*_diag: con high/low de las mismas velas, SOLO diagnóstico.

Eficiencia de salida: exit_efficiency = realized_vs_mfe_r = realized_r / mfe_r
(solo si mfe_r > 0; si no, null). mfe_left_on_table_r = mfe_r - realized_r.

Clases (umbrales simples, después del hecho; P&L <= 0 cuenta como no ganador):
- never_worked:             P&L <= 0 y MFE < 0.5R  (mae_before_mfe indica si el
                            peor punto llegó antes que el mejor)
- almost_worked:            P&L <= 0 y 0.5R <= MFE < 1R
- reached_1R_then_lost:     P&L <= 0 y MFE >= 1R
- profitable_but_gave_back: P&L > 0 y eficiencia < 0.5
- clean_winner:             P&L > 0 y eficiencia >= 0.5 (o MFE por cierres <= 0:
                            la salida capturó más que cualquier cierre)
- unclassified:             sin R (no se inventa)
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

NY = "America/New_York"
REACH_LEVELS = (("reached_0_5r", 0.5), ("reached_1_0r", 1.0), ("reached_1_5r", 1.5), ("reached_2_0r", 2.0))
EFFICIENCY_CLEAN = 0.5
IMMEDIATE_FAIL_MFE_R = 0.25  # "falló de inmediato": nunca cerró ni +0.25R a favor
TIME_BUCKETS = ("09:30-10:00", "10:00-10:30", "10:30-11:00", "after_11:00")
MATERIAL_MFE_R = 0.15        # diferencia "material" de MFE mediana por símbolo (solo descriptivo)
MATERIAL_REACH_PP = 10.0     # o de % que llega a +1R, en puntos porcentuales
MIN_SYMBOL_TRADES = 20
EXIT_REASONS = ("signal_exit", "giveback_close", "stop_hit", "take_profit_hit")


# ---------------- seguimiento durante el trade (lo alimenta el motor) ----------------
def new_tracker() -> Dict[str, Any]:
    return {"bars": 0, "max_close": None, "max_close_ts": None, "min_close": None, "min_close_ts": None,
            "max_high": None, "min_low": None}


def observe(tracker: Dict[str, Any], close: float, high: float, low: float, known_at: str) -> None:
    """Un cierre de vela con posición abierta, en el instante en que se conoce."""
    tracker["bars"] += 1
    if tracker["max_close"] is None or close > tracker["max_close"]:
        tracker["max_close"], tracker["max_close_ts"] = close, known_at
    if tracker["min_close"] is None or close < tracker["min_close"]:
        tracker["min_close"], tracker["min_close_ts"] = close, known_at
    tracker["max_high"] = high if tracker["max_high"] is None else max(tracker["max_high"], high)
    tracker["min_low"] = low if tracker["min_low"] is None else min(tracker["min_low"], low)


# ---------------- campos por trade ----------------
def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or not b or b <= 0 else a / b


def _minutes(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    return (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 60.0


def excursion_fields(trade: Dict[str, Any], tracker: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # Tipos nativos: el precio de fill viene de arrays numpy y np.bool_ se serializaría como "False".
    entry = None if trade.get("entry_fill_price") is None else float(trade["entry_fill_price"])
    risk_ps = None if trade.get("risk_per_share_modeled") is None else float(trade["risk_per_share_modeled"])
    t = tracker or new_tracker()
    has = entry is not None and t["bars"] > 0
    mfe = t["max_close"] - entry if has else None
    mae = entry - t["min_close"] if has else None
    mfe_r, mae_r = _div(mfe, risk_ps), _div(mae, risk_ps)
    realized_r = trade.get("realized_r")
    eff = realized_r / mfe_r if (mfe_r is not None and mfe_r > 0 and realized_r is not None) else None
    out = {
        "excursion_bars": t["bars"],
        "mfe_dollars_per_share": mfe,
        "mae_dollars_per_share": mae,
        "mfe_pct": mfe / entry * 100 if has else None,
        "mae_pct": mae / entry * 100 if has else None,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "mfe_timestamp": t["max_close_ts"] if has else None,
        "mae_timestamp": t["min_close_ts"] if has else None,
        "minutes_to_mfe": _minutes(trade.get("entry_fill_timestamp"), t["max_close_ts"]) if has else None,
        "minutes_to_mae": _minutes(trade.get("entry_fill_timestamp"), t["min_close_ts"]) if has else None,
        "mae_before_mfe": (pd.Timestamp(t["min_close_ts"]) < pd.Timestamp(t["max_close_ts"])) if has else None,
    }
    for name, level in REACH_LEVELS:
        out[name] = None if mfe_r is None else bool(mfe_r >= level)
    out["exit_efficiency"] = eff
    out["realized_vs_mfe_r"] = eff
    out["mfe_left_on_table_r"] = (mfe_r - realized_r) if (mfe_r is not None and realized_r is not None) else None
    hi = t["max_high"] - entry if has else None
    lo = entry - t["min_low"] if has else None
    out.update({"intrabar_mfe_dollars_per_share_diag": hi, "intrabar_mae_dollars_per_share_diag": lo,
                "intrabar_mfe_r_diag": _div(hi, risk_ps), "intrabar_mae_r_diag": _div(lo, risk_ps)})
    out["excursion_class"] = classify(trade.get("realized_pnl"), mfe_r, eff)
    return out


def classify(pnl: Optional[float], mfe_r: Optional[float], efficiency: Optional[float]) -> str:
    if pnl is None or mfe_r is None:
        return "unclassified"
    if pnl > 0:
        return "clean_winner" if (efficiency is None or efficiency >= EFFICIENCY_CLEAN) else "profitable_but_gave_back"
    if mfe_r >= 1.0:
        return "reached_1R_then_lost"
    if mfe_r >= 0.5:
        return "almost_worked"
    return "never_worked"


def entry_time_bucket(entry_fill_ts: Optional[str]) -> Optional[str]:
    if not entry_fill_ts:
        return None
    t = pd.Timestamp(entry_fill_ts).tz_convert(NY)
    m = t.hour * 60 + t.minute
    if m < 9 * 60 + 30:
        return "before_09:30"
    if m < 10 * 60:
        return "09:30-10:00"
    if m < 10 * 60 + 30:
        return "10:00-10:30"
    if m < 11 * 60:
        return "10:30-11:00"
    return "after_11:00"


# ---------------- agregados ----------------
def _vals(trades: Iterable[Dict[str, Any]], key: str) -> List[float]:
    return [t[key] for t in trades if t.get(key) is not None]


def _mean(v: List[float]) -> Optional[float]:
    return statistics.fmean(v) if v else None


def _median(v: List[float]) -> Optional[float]:
    return statistics.median(v) if v else None


def _pct(num: int, den: int) -> Optional[float]:
    return num / den * 100 if den else None


def excursion_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    with_r = [t for t in trades if t.get("mfe_r") is not None]
    nr = len(with_r)
    wins = [t for t in trades if t.get("result") == "win"]
    stats = {
        "trades": n,
        "trades_with_r": nr,
        "win_rate_pct": _pct(len(wins), n),
        "realized_pnl": sum(t["realized_pnl"] for t in trades),
        "avg_realized_r": _mean(_vals(trades, "realized_r")),
        "avg_mfe_r": _mean(_vals(with_r, "mfe_r")),
        "median_mfe_r": _median(_vals(with_r, "mfe_r")),
        "avg_mae_r": _mean(_vals(with_r, "mae_r")),
        "median_mae_r": _median(_vals(with_r, "mae_r")),
        "median_minutes_to_mfe": _median(_vals(trades, "minutes_to_mfe")),
        "median_minutes_to_mae": _median(_vals(trades, "minutes_to_mae")),
    }
    for name, _ in REACH_LEVELS:
        stats[f"pct_{name}"] = _pct(sum(1 for t in with_r if t[name]), nr)
    stats["pct_reached_1r_then_lost"] = _pct(sum(1 for t in with_r if t["mfe_r"] >= 1.0 and t["realized_pnl"] <= 0), nr)
    # La media de realized_r/mfe_r la dominan los MFE diminutos (MFE +0.02R y -0.3R realizado = -15),
    # así que se reportan también la mediana y el cociente agregado Σrealized_R / ΣMFE_R.
    stats["avg_exit_efficiency"] = _mean(_vals(trades, "exit_efficiency"))
    stats["median_exit_efficiency"] = _median(_vals(trades, "exit_efficiency"))
    pos = [t for t in with_r if t["mfe_r"] > 0 and t.get("realized_r") is not None]
    mfe_sum = sum(t["mfe_r"] for t in pos)
    stats["aggregate_capture_ratio"] = sum(t["realized_r"] for t in pos) / mfe_sum if mfe_sum > 0 else None
    stats["avg_mfe_left_on_table_r"] = _mean(_vals(trades, "mfe_left_on_table_r"))
    return stats


def _group(trades: List[Dict[str, Any]], key) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        groups.setdefault(str(key(t)), []).append(t)
    return {k: excursion_stats(v) for k, v in groups.items()}


def diagnostics(trades: List[Dict[str, Any]], symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    losers = [t for t in trades if t.get("result") == "loss"]
    losers_r = [t for t in losers if t.get("mfe_r") is not None]
    winners = [t for t in trades if t.get("result") == "win"]
    overall = excursion_stats(trades)
    overall["avg_exit_efficiency_winners"] = _mean(_vals(winners, "exit_efficiency"))
    overall["median_exit_efficiency_winners"] = _median(_vals(winners, "exit_efficiency"))

    by_reason = _group(trades, lambda t: t["exit_reason"])
    reason_order = [r for r in EXIT_REASONS if r in by_reason] + sorted(r for r in by_reason if r not in EXIT_REASONS)
    by_reason = {r: by_reason[r] for r in reason_order}
    by_symbol_raw = _group(trades, lambda t: t["symbol"])
    sym_order = [s for s in (symbols or []) if s in by_symbol_raw] + sorted(s for s in by_symbol_raw if s not in (symbols or []))
    by_symbol = {s: by_symbol_raw[s] for s in sym_order}
    buckets_raw = _group(trades, lambda t: entry_time_bucket(t.get("entry_fill_timestamp")))
    buckets = {b: buckets_raw[b] for b in TIME_BUCKETS if b in buckets_raw}
    buckets.update({b: v for b, v in buckets_raw.items() if b not in TIME_BUCKETS})
    classes_raw = _group(trades, lambda t: t.get("excursion_class"))
    classes = {c: classes_raw[c] for c in ("never_worked", "almost_worked", "reached_1R_then_lost",
                                          "profitable_but_gave_back", "clean_winner", "unclassified") if c in classes_raw}

    return {
        "definitions": {
            "price_source": "bar closes while shares were held (entry-fill bar through the bar before the final exit fill)",
            "r_denominator": "risk_per_share_modeled (|modeled entry - initial stop|)",
            "mae_sign": "positive = adverse",
            "classes": {"never_worked": "P&L<=0 and MFE<0.5R", "almost_worked": "P&L<=0 and 0.5R<=MFE<1R",
                        "reached_1R_then_lost": "P&L<=0 and MFE>=1R",
                        "profitable_but_gave_back": f"P&L>0 and efficiency<{EFFICIENCY_CLEAN}",
                        "clean_winner": f"P&L>0 and efficiency>={EFFICIENCY_CLEAN} (or close-based MFE<=0)"},
        },
        "overall": overall,
        "losing_trades": {
            "count": len(losers),
            "pct_never_reached_0_5r": _pct(sum(1 for t in losers_r if t["mfe_r"] < 0.5), len(losers_r)),
            "pct_reached_1r_before_losing": _pct(sum(1 for t in losers_r if t["mfe_r"] >= 1.0), len(losers_r)),
        },
        "by_exit_reason": by_reason,
        "by_symbol": by_symbol,
        "by_entry_time_bucket": buckets,
        "by_class": classes,
        "answers": _answers(trades, overall, by_symbol, losers_r, winners),
    }


def _answers(trades, overall, by_symbol, losers_r, winners) -> Dict[str, Any]:
    sig = [t for t in trades if t["exit_reason"] == "signal_exit" and t.get("mfe_r") is not None]
    stop = [t for t in trades if t["exit_reason"] == "stop_hit" and t.get("mfe_r") is not None]
    first = [t for t in trades if entry_time_bucket(t.get("entry_fill_timestamp")) == "09:30-10:00"]
    later = [t for t in trades if entry_time_bucket(t.get("entry_fill_timestamp")) not in ("09:30-10:00", None)]
    win_mfe = _vals(winners, "mfe_r")
    win_real = _vals([t for t in winners if t.get("mfe_r") is not None], "realized_r")
    material = []
    for s, st in by_symbol.items():
        if st["trades"] < MIN_SYMBOL_TRADES or st["median_mfe_r"] is None or overall["median_mfe_r"] is None:
            continue
        d_mfe = st["median_mfe_r"] - overall["median_mfe_r"]
        d_reach = (st["pct_reached_1_0r"] or 0) - (overall["pct_reached_1_0r"] or 0)
        if abs(d_mfe) >= MATERIAL_MFE_R or abs(d_reach) >= MATERIAL_REACH_PP:
            material.append({"symbol": s, "trades": st["trades"], "median_mfe_r": st["median_mfe_r"],
                             "delta_median_mfe_r": d_mfe, "pct_reached_1_0r": st["pct_reached_1_0r"],
                             "delta_reach_1r_pp": d_reach, "avg_realized_r": st["avg_realized_r"]})
    return {
        "q1_pct_losers_never_reached_0_5r": _pct(sum(1 for t in losers_r if t["mfe_r"] < 0.5), len(losers_r)),
        "q2_pct_losers_reached_1r_before_losing": _pct(sum(1 for t in losers_r if t["mfe_r"] >= 1.0), len(losers_r)),
        "q3_signal_exit": {
            "trades": len(sig), "avg_mfe_r": _mean(_vals(sig, "mfe_r")), "median_mfe_r": _median(_vals(sig, "mfe_r")),
            "pct_reached_1r": _pct(sum(1 for t in sig if t["mfe_r"] >= 1.0), len(sig)),
            "avg_mfe_left_on_table_r": _mean(_vals(sig, "mfe_left_on_table_r")),
            "median_mfe_left_on_table_r": _median(_vals(sig, "mfe_left_on_table_r")),
        },
        "q4_stop_hit": {
            "trades": len(stop),
            f"pct_failed_immediately_mfe_below_{IMMEDIATE_FAIL_MFE_R}r": _pct(sum(1 for t in stop if t["mfe_r"] < IMMEDIATE_FAIL_MFE_R), len(stop)),
            "pct_reached_0_5r_first": _pct(sum(1 for t in stop if t["mfe_r"] >= 0.5), len(stop)),
            "pct_reached_1r_first": _pct(sum(1 for t in stop if t["mfe_r"] >= 1.0), len(stop)),
            "median_mfe_r": _median(_vals(stop, "mfe_r")),
            "median_minutes_to_mae": _median(_vals(stop, "minutes_to_mae")),
            "pct_mae_before_mfe": _pct(sum(1 for t in stop if t.get("mae_before_mfe")), len(stop)),
        },
        "q5_profitable_trades": {
            "trades": len(winners),
            "avg_exit_efficiency": _mean(_vals(winners, "exit_efficiency")),
            "median_exit_efficiency": _median(_vals(winners, "exit_efficiency")),
            "total_realized_r_over_total_mfe_r": (sum(win_real) / sum(win_mfe)) if win_mfe and sum(win_mfe) > 0 else None,
        },
        "q6_first_30_min_vs_later": {"09:30-10:00": excursion_stats(first), "after_10:00": excursion_stats(later)},
        "q7_symbols_materially_different": {
            "rule": f">= {MIN_SYMBOL_TRADES} trades and |median MFE R - overall| >= {MATERIAL_MFE_R} "
                    f"or |%reach +1R - overall| >= {MATERIAL_REACH_PP}pp (descriptive only)",
            "symbols": material,
        },
    }


# ---------------- salida de consola ----------------
def _r(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:+.2f}R"


def _p(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1f}%"


def _f(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.2f}"


def format_diagnostics(d: Dict[str, Any]) -> str:
    o, lt, a = d["overall"], d["losing_trades"], d["answers"]
    L = ["MFE / MAE DIAGNOSTICS (bar closes; R = initial risk/share)", "─" * 44,
         f"Median MFE: {_r(o['median_mfe_r'])}  (avg {_r(o['avg_mfe_r'])})",
         f"Median MAE: {_r(o['median_mae_r'])}  (avg {_r(o['avg_mae_r'])}; positive = adverse)",
         f"Reached +0.5R: {_p(o['pct_reached_0_5r'])} | +1.0R: {_p(o['pct_reached_1_0r'])} | "
         f"+1.5R: {_p(o['pct_reached_1_5r'])} | +2.0R: {_p(o['pct_reached_2_0r'])}",
         f"Reached +1R then closed <= 0: {_p(o['pct_reached_1r_then_lost'])}",
         f"Winners captured (realized/MFE): avg {o['avg_exit_efficiency_winners'] or 0:.2f} | "
         f"median {o['median_exit_efficiency_winners'] or 0:.2f} | avg MFE left on table {_r(o['avg_mfe_left_on_table_r'])}",
         "",
         f"Losing trades ({lt['count']}):",
         f"  Never reached +0.5R: {_p(lt['pct_never_reached_0_5r'])}",
         f"  Reached +1R before losing: {_p(lt['pct_reached_1r_before_losing'])}",
         "",
         f"{'exit reason':<17}{'n':>5}{'win%':>7}{'P&L':>13}{'avgR':>8}{'MFE med':>9}{'MAE med':>9}{'≥1R':>7}"
         f"{'1R→lost':>9}{'eff med':>9}{'ΣR/ΣMFE':>9}"]
    for k, s in d["by_exit_reason"].items():
        L.append(f"{k:<17}{s['trades']:>5}{_p(s['win_rate_pct']):>7}{s['realized_pnl']:>13,.2f}{_r(s['avg_realized_r']):>8}"
                 f"{_r(s['median_mfe_r']):>9}{_r(s['median_mae_r']):>9}{_p(s['pct_reached_1_0r']):>7}"
                 f"{_p(s['pct_reached_1r_then_lost']):>9}{_f(s['median_exit_efficiency']):>9}{_f(s['aggregate_capture_ratio']):>9}")
    L.append("(eff = realized R / MFE R, only when MFE > 0; the mean is in summary.json but tiny MFEs make it unstable)")
    L += ["", f"{'entry (NY)':<17}{'n':>5}{'win%':>7}{'avgR':>8}{'MFE med':>9}{'MAE med':>9}{'≥1R':>7}"]
    for k, s in d["by_entry_time_bucket"].items():
        L.append(f"{k:<17}{s['trades']:>5}{_p(s['win_rate_pct']):>7}{_r(s['avg_realized_r']):>8}"
                 f"{_r(s['median_mfe_r']):>9}{_r(s['median_mae_r']):>9}{_p(s['pct_reached_1_0r']):>7}")
    L += ["", f"{'symbol':<17}{'n':>5}{'win%':>7}{'avgR':>8}{'MFE med':>9}{'MAE med':>9}{'≥1R':>7}"]
    for k, s in d["by_symbol"].items():
        L.append(f"{k:<17}{s['trades']:>5}{_p(s['win_rate_pct']):>7}{_r(s['avg_realized_r']):>8}"
                 f"{_r(s['median_mfe_r']):>9}{_r(s['median_mae_r']):>9}{_p(s['pct_reached_1_0r']):>7}")
    L += ["", "Classes: " + ", ".join(f"{k}={v['trades']}" for k, v in d["by_class"].items())]
    q3, q4 = a["q3_signal_exit"], a["q4_stop_hit"]
    fail_key = next(k for k in q4 if k.startswith("pct_failed_immediately"))
    L += ["", "Answers:",
          f"  1) Losers that never reached +0.5R: {_p(a['q1_pct_losers_never_reached_0_5r'])}",
          f"  2) Losers that reached +1R first: {_p(a['q2_pct_losers_reached_1r_before_losing'])}",
          f"  3) signal_exit: median MFE {_r(q3['median_mfe_r'])}, reached +1R {_p(q3['pct_reached_1r'])}, "
          f"avg MFE left {_r(q3['avg_mfe_left_on_table_r'])}",
          f"  4) stop_hit: failed immediately (<+{IMMEDIATE_FAIL_MFE_R}R) {_p(q4[fail_key])}, reached +0.5R first "
          f"{_p(q4['pct_reached_0_5r_first'])}, +1R first {_p(q4['pct_reached_1r_first'])}, "
          f"median minutes to MAE {q4['median_minutes_to_mae'] if q4['median_minutes_to_mae'] is not None else '-'}",
          f"  5) Winners captured: median {a['q5_profitable_trades']['median_exit_efficiency'] or 0:.2f} of MFE "
          f"(total realized R / total MFE R = {a['q5_profitable_trades']['total_realized_r_over_total_mfe_r'] or 0:.2f})"]
    f30, lat = a["q6_first_30_min_vs_later"]["09:30-10:00"], a["q6_first_30_min_vs_later"]["after_10:00"]
    L.append(f"  6) First 30 min: n={f30['trades']} MFE med {_r(f30['median_mfe_r'])} MAE med {_r(f30['median_mae_r'])} "
             f"avgR {_r(f30['avg_realized_r'])} | later: n={lat['trades']} MFE med {_r(lat['median_mfe_r'])} "
             f"MAE med {_r(lat['median_mae_r'])} avgR {_r(lat['avg_realized_r'])}")
    mat = a["q7_symbols_materially_different"]["symbols"]
    L.append("  7) Symbols with materially different excursion: "
             + (", ".join(f"{m['symbol']} (n={m['trades']}, MFE med {_r(m['median_mfe_r'])}, ≥1R {_p(m['pct_reached_1_0r'])})"
                          for m in mat) or "none by the stated rule"))
    return "\n".join(L)
