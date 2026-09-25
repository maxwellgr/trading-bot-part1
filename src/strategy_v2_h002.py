# src/strategy_v2_h002.py
"""
STRATEGY_V2_HYPOTHESIS_002 = H001 congelada + UNA regla: salida Failure-to-Progress (FTP).
Spec congelado: research/strategy_v2_hypothesis_002.md (commit 6b0ac231388c3a7bce9135782aab5af4d2576fd3).
SOLO investigación/backtest: sin contraparte en vivo; el motor compartido no cambia.

Regla (§5 del spec)
-------------------
- F = precio real del fill de entrada; R_ps = risk_per_share_modeled (denominador de realized_r/mfe_r).
- Cierres post-fill: el cierre de la vela del fill E es el 1º; luego las siguientes velas existentes.
  El tracker de excursión del motor (cierres con acciones en mano, a través de scale-outs) los cuenta.
- Checkpoint único al 3er cierre post-fill: dispara si (max cierre − F)/R_ps < 0.25 (estricto) y
  cierre actual <= F (igualdad dispara). Sale TODO lo restante a la próxima apertura disponible
  (venta con slippage normal del simulador), motivo "failure_to_progress".
- Ruta A (decisión permitida): después de la secuencia _manage de H001 SIN CAMBIOS; solo si esa
  misma decisión no envió ninguna orden de salida y no hay una salida pendiente en vuelo.
- Ruta B (el 3er cierre es la vela 15:55, conocida a las 16:00, sin decisión del motor): se evalúa en
  ese cierre (paso de marca/excursión del motor, que corre en toda vela) con exactamente los 3 cierres;
  si dispara, la venta queda PENDIENTE y se llena en la próxima apertura regular disponible.
- Contabilidad: fill normal -> P&L, record_close, rachas, halts y la convención de frontera de día
  heredada del motor (fills antes de la primera decisión del día se aplican antes del reset).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .backtest_engine import BREAKEVEN_EPS, BacktestEngine
from .historical_audit import EARLY_CLOSES

HYPOTHESIS_ID = "STRATEGY_V2_HYPOTHESIS_002"
FROZEN_SPEC_COMMIT = "6b0ac231388c3a7bce9135782aab5af4d2576fd3"
FTP_CLOSES = 3
FTP_MFE_R = 0.25
FTP_PURPOSE = "failure_to_progress"
GAP_REPORT_R = 0.75
NY = "America/New_York"


def ftp_triggered(max_close: float, current_close: float, fill: float, risk_ps: float) -> bool:
    """Condición congelada: MFE por cierres < +0.25R (estricto) Y cierre actual <= F (igualdad dispara)."""
    return (max_close - fill) / risk_ps < FTP_MFE_R and current_close <= fill


class FTPEngine(BacktestEngine):
    """Motor de portafolio + FTP (solo investigación). ftp_enabled=False == BacktestEngine exacto."""

    def __init__(self, *args, ftp_enabled: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.ftp_enabled = ftp_enabled
        self.ftp_events: List[Dict[str, Any]] = []
        self._ftp_checked: set = set()

    # ---- candidato: trade abierto con exactamente 3 cierres post-fill y sin chequeo previo
    def _ftp_candidate(self, sd, i) -> Optional[Dict[str, Any]]:
        trade = self.open_trades.get(sd.name)
        if (trade is None or trade["entry_fill_price"] is None or trade["trade_id"] in self._ftp_checked
                or self.sim.position_qty(sd.name) <= 0):
            return None
        return trade if trade["_excursion"]["bars"] == FTP_CLOSES else None

    def _ftp_check(self, sd, i, trade: Dict[str, Any], path: str, blocked: Optional[str]) -> None:
        self._ftp_checked.add(trade["trade_id"])
        fill, rps = float(trade["entry_fill_price"]), float(trade["risk_per_share_modeled"])
        max_close, cur = float(trade["_excursion"]["max_close"]), float(sd.close[i])
        cond = ftp_triggered(max_close, cur, fill, rps)
        ev = {"trade_id": trade["trade_id"], "symbol": sd.name, "path": path, "checkpoint_bar": sd.iso[i],
              "decision_timestamp": sd.decision_iso[i], "entry_fill_price": fill, "risk_per_share": rps,
              "max_close": max_close, "mfe_r": (max_close - fill) / rps, "current_close": cur,
              "condition_met": cond, "blocked_by": blocked, "triggered": bool(cond and blocked is None), "qty": None}
        if ev["triggered"]:
            qty = int(self.book[sd.name]["qty"])
            ev["qty"] = qty
            self._submit_exit(sd, i, qty, FTP_PURPOSE)
        self.ftp_events.append(ev)

    # ---- Ruta A: decisión permitida, después de la gestión de H001 sin cambios
    def _manage(self, sd, i, sig, price, bd) -> None:
        if not self.ftp_enabled:
            super()._manage(sd, i, sig, price, bd)
            return
        pending_before = self.sim.has_pending(sd.name)
        n_orders = len(self.sim.orders)
        super()._manage(sd, i, sig, price, bd)
        trade = self._ftp_candidate(sd, i)
        if trade is None:
            return
        blocked = ("existing_exit_same_decision" if len(self.sim.orders) > n_orders
                   else ("pending_exit_in_flight" if pending_before else None))
        self._ftp_check(sd, i, trade, "decision", blocked)

    # ---- Ruta B: 3er cierre en una vela sin decisión permitida (15:55 -> 16:00)
    def _observe_excursion(self, sd, i) -> None:
        super()._observe_excursion(sd, i)
        if not self.ftp_enabled or sd.decision_ok[i]:
            return
        trade = self._ftp_candidate(sd, i)
        if trade is None:
            return
        self._ftp_check(sd, i, trade, "close_without_decision",
                        "pending_exit_in_flight" if self.sim.has_pending(sd.name) else None)


# ================================================================ reportes
def _ny_date(iso: str):
    return pd.Timestamp(iso).tz_convert(NY).date()


def _ftp_leg(trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return next((l for l in trade["legs"] if l["purpose"] == FTP_PURPOSE), None)


def checkpoint_path(event: Optional[Dict[str, Any]], leg: Dict[str, Any]) -> str:
    if event is not None and event["path"] == "close_without_decision":
        return "16:00"
    dec = pd.Timestamp(leg["decision_timestamp"]).tz_convert(NY)
    if dec.date() in EARLY_CLOSES and dec.hour == 13 and dec.minute == 0:
        return "early_close"
    return "gap_or_other"


def overnight_ftp_metrics(trades: Sequence[Dict[str, Any]], events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Overnight = fecha NY del fill FTP != fecha NY de la decisión FTP que lo envió (spec §10)."""
    ev = {e["trade_id"]: e for e in events if e["triggered"]}
    legs = []
    for t in trades:
        leg = _ftp_leg(t)
        if leg is not None and _ny_date(leg["fill_timestamp"]) != _ny_date(leg["decision_timestamp"]):
            legs.append((t, leg))
    pnl = [l["realized_pnl"] for _, l in legs]
    paths: Dict[str, int] = {}
    for t, l in legs:
        k = checkpoint_path(ev.get(t["trade_id"]), l)
        paths[k] = paths.get(k, 0) + 1
    return {"overnight_ftp_fills": len(legs), "overnight_ftp_realized_pnl": float(sum(pnl)),
            "overnight_ftp_losses": sum(1 for p in pnl if p < -BREAKEVEN_EPS),
            "overnight_ftp_wins": sum(1 for p in pnl if p > BREAKEVEN_EPS),
            "overnight_ftp_breakevens": sum(1 for p in pnl if -BREAKEVEN_EPS <= p <= BREAKEVEN_EPS),
            "by_checkpoint_path": dict(sorted(paths.items()))}


def gap_trades(trades: Sequence[Dict[str, Any]]) -> int:
    """Trades con fill > 0.75·R_ps por encima de la entrada modelada (spec §5.6; esperado 0)."""
    n = 0
    for t in trades:
        rps = t.get("risk_per_share_modeled") or 0
        if rps > 0 and t.get("entry_fill_price") is not None and (t["entry_fill_price"] - t["modeled_entry"]) / rps > GAP_REPORT_R:
            n += 1
    return n


def ftp_summary(trades: Sequence[Dict[str, Any]], events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ftp_trades = [t for t in trades if t["exit_reason"] == FTP_PURPOSE]
    legs = [l for t in trades for l in t["legs"] if l["purpose"] == FTP_PURPOSE]
    blocked: Dict[str, int] = {}
    for e in events:
        if e["blocked_by"]:
            blocked[e["blocked_by"]] = blocked.get(e["blocked_by"], 0) + 1
    by_path: Dict[str, int] = {}
    for e in events:
        if e["triggered"]:
            by_path[e["path"]] = by_path.get(e["path"], 0) + 1
    return {"checkpoints_evaluated": len(events), "condition_met": sum(1 for e in events if e["condition_met"]),
            "triggered": sum(1 for e in events if e["triggered"]), "triggered_by_path": by_path,
            "blocked": blocked, "ftp_exit_trades": len(ftp_trades),
            "ftp_trades_realized_pnl": float(sum(t["realized_pnl"] for t in ftp_trades)),
            "ftp_legs_realized_pnl": float(sum(l["realized_pnl"] for l in legs)),
            "ftp_trades_avg_r": (sum(t["realized_r"] for t in ftp_trades) / len(ftp_trades)) if ftp_trades else None,
            "ftp_exits_after_scale_out": sum(1 for t in ftp_trades if t.get("scale_outs")),
            "gap_trades_fill_gt_0_75r_above_modeled": gap_trades(trades)}


def _exit_table(trades: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        r = out.setdefault(t["exit_reason"], {"count": 0, "pnl": 0.0})
        r["count"] += 1
        r["pnl"] += t["realized_pnl"]
    return dict(sorted(out.items()))


def compare_with_h001(h001_trades: Sequence[Dict[str, Any]], h001_summary: Dict[str, Any],
                      h002_trades: Sequence[Dict[str, Any]], h002_summary: Dict[str, Any]) -> Dict[str, Any]:
    """Comparación DEVELOPMENT requerida (spec §10): agregados, pareo, pérdidas evitadas, ganadores sacrificados, puente exacto."""
    def agg(s):
        t, p = s["trades"], s["portfolio"]
        return {"trades": t["trades"], "win_rate_pct": t["win_rate"] * 100, "expectancy_r": t["expectancy_r"],
                "profit_factor": t["profit_factor"], "total_r": t["total_r"], "pnl": p["realized_pnl_closed_trades"],
                "max_drawdown_pct": p["max_drawdown_pct"], "max_consecutive_losses": t["max_consecutive_losses"]}
    key = lambda t: (t["symbol"], t["entry_signal_timestamp"])  # noqa: E731
    a = {key(t): t for t in h001_trades}
    b = {key(t): t for t in h002_trades}
    common = sorted(a.keys() & b.keys(), key=lambda k: (k[1], k[0]))
    only_a, only_b = sorted(a.keys() - b.keys()), sorted(b.keys() - a.keys())

    def dr(k):
        return b[k]["realized_r"] - a[k]["realized_r"]

    def dp(k):
        return b[k]["realized_pnl"] - a[k]["realized_pnl"]
    ftp_common = [k for k in common if b[k]["exit_reason"] == FTP_PURPOSE]
    avoided = [k for k in ftp_common if a[k]["exit_reason"] == "stop_hit"]
    sacrificed = [k for k in ftp_common if a[k]["result"] == "win"]
    by_prev: Dict[str, Dict[str, Any]] = {}
    for k in ftp_common:
        g = by_prev.setdefault(f"{a[k]['exit_reason']}|{a[k]['result']}", {"count": 0, "delta_r": 0.0, "delta_pnl": 0.0})
        g["count"] += 1
        g["delta_r"] += dr(k)
        g["delta_pnl"] += dp(k)
    matched_change = sum(dp(k) for k in common)
    pnl_only_b = sum(b[k]["realized_pnl"] for k in only_b)
    pnl_only_a = sum(a[k]["realized_pnl"] for k in only_a)
    total_a = sum(t["realized_pnl"] for t in h001_trades)
    total_b = sum(t["realized_pnl"] for t in h002_trades)
    bridge_sum = matched_change + pnl_only_b - pnl_only_a
    sac_exit: Dict[str, int] = {}
    for k in sacrificed:
        sac_exit[a[k]["exit_reason"]] = sac_exit.get(a[k]["exit_reason"], 0) + 1
    return {
        "evidence": "DEVELOPMENT only (screening/resubstitution; FTP constants were derived on this split)",
        "aggregate": {"H001": agg(h001_summary), "H002": agg(h002_summary)},
        "exit_reasons": {"H001": _exit_table(h001_trades), "H002": _exit_table(h002_trades)},
        "matching": {"key": "(symbol, entry_signal_timestamp)", "matched": len(common), "only_h001": len(only_a),
                     "only_h002": len(only_b), "matched_closed_by_ftp_in_h002": len(ftp_common)},
        "losses_avoided_h001_stop_hit_closed_by_ftp": {
            "count": len(avoided), "sum_delta_r": float(sum(dr(k) for k in avoided)),
            "mean_delta_r": float(sum(dr(k) for k in avoided) / len(avoided)) if avoided else None,
            "delta_pnl": float(sum(dp(k) for k in avoided))},
        "winners_sacrificed_h001_win_closed_by_ftp": {
            "count": len(sacrificed), "r_given_up": float(-sum(dr(k) for k in sacrificed)),
            "pnl_given_up": float(-sum(dp(k) for k in sacrificed)), "h001_exit_reasons": dict(sorted(sac_exit.items()))},
        "ftp_closed_matched_by_h001_exit_and_result": dict(sorted(by_prev.items())),
        "pnl_bridge": {"h001_total_pnl": total_a, "h002_total_pnl": total_b, "difference": total_b - total_a,
                       "matched_trades_change": matched_change, "h002_only_trades_pnl": pnl_only_b,
                       "h001_only_trades_pnl": pnl_only_a, "bridge_sum": bridge_sum,
                       "bridge_residual": (total_b - total_a) - bridge_sum,
                       "bridge_exact": math.isclose(bridge_sum, total_b - total_a, rel_tol=0, abs_tol=1e-6)},
    }
