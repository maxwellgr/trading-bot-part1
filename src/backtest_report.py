# src/backtest_report.py
"""
Métricas y salidas del backtester de portafolio (sin matplotlib: CSV/JSON).
Funciones puras sobre BacktestResult; nada aquí influye en la simulación.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .backtest_engine import NY, BacktestResult
from .backtest_excursion import diagnostics, format_diagnostics


# ---------------- métricas básicas ----------------
def _max_streak(results: Sequence[str], kind: str) -> int:
    best = cur = 0
    for r in results:
        cur = cur + 1 if r == kind else 0
        best = max(best, cur)
    return best


def _profit_factor(pnls: Sequence[float]) -> Optional[float]:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return None if gains == 0 else math.inf
    return gains / losses


def drawdown_series(equity: Sequence[float], start_equity: float) -> List[float]:
    """Drawdown (≤0, fracción) respecto del máximo previo, incluyendo la equity inicial."""
    peak = start_equity
    out = []
    for e in equity:
        peak = max(peak, e)
        out.append(e / peak - 1.0 if peak > 0 else 0.0)
    return out


def trade_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [t["realized_pnl"] for t in trades]
    results = [t["result"] for t in trades]
    wins = [p for p, r in zip(pnls, results) if r == "win"]
    losses = [p for p, r in zip(pnls, results) if r == "loss"]
    n = len(trades)
    rs = [t["realized_r"] for t in trades if t.get("realized_r") is not None]
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": results.count("breakeven"),
        "win_rate": len(wins) / n if n else 0.0,
        "loss_rate": len(losses) / n if n else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": (avg_win / abs(avg_loss)) if wins and losses else None,
        "expectancy": statistics.fmean(pnls) if pnls else 0.0,
        "profit_factor": _profit_factor(pnls),
        "gross_profit": sum(wins),
        "gross_loss": sum(losses),
        "largest_win": max(pnls) if pnls else 0.0,
        "largest_loss": min(pnls) if pnls else 0.0,
        "max_consecutive_wins": _max_streak(results, "win"),
        "max_consecutive_losses": _max_streak(results, "loss"),
        "avg_r": statistics.fmean(rs) if rs else None,
        "median_r": statistics.median(rs) if rs else None,
        "total_r": sum(rs) if rs else None,
        "expectancy_r": statistics.fmean(rs) if rs else None,
        "avg_holding_minutes": statistics.fmean(t["holding_seconds"] for t in trades) / 60 if trades else None,
    }


def per_symbol(result: BacktestResult) -> Dict[str, Any]:
    out = {}
    for sym in result.config["symbols"]:
        tr = [t for t in result.trades if t["symbol"] == sym]
        m = trade_metrics(tr)
        out[sym] = {
            "trades": m["trades"], "wins": m["wins"], "losses": m["losses"],
            "realized_pnl": sum(t["realized_pnl"] for t in tr), "win_rate": m["win_rate"],
            "avg_trade": m["expectancy"], "profit_factor": m["profit_factor"] if m["trades"] >= 2 else None,
            "signals": result.counters["signals"].get(sym, {}),
        }
    return out


def _ny_date(iso: str) -> str:
    return str(pd.Timestamp(iso).tz_convert(NY).date())


def daily_results(result: BacktestResult) -> List[Dict[str, Any]]:
    by_day_eq: Dict[str, List[float]] = {}
    for ts, eq in result.equity_curve:
        by_day_eq.setdefault(_ny_date(ts), []).append(eq)
    realized: Dict[str, float] = {}
    for f in result.fills:
        d = _ny_date(f["fill_ts"])
        realized[d] = realized.get(d, 0.0) + f["realized_pnl"]
    closed: Dict[str, List[Dict[str, Any]]] = {}
    for t in result.trades:
        closed.setdefault(_ny_date(t["exit_fill_timestamp"]), []).append(t)
    halts = set(result.daily_profit_halt_dates)
    streaks = set(result.loss_streak_halt_dates)
    rows = []
    prev_end = result.initial_equity
    for d in sorted(by_day_eq):
        eqs = by_day_eq[d]
        dd = drawdown_series(eqs, prev_end)
        tr = closed.get(d, [])
        rows.append({
            "date": d, "starting_equity": prev_end, "ending_equity": eqs[-1],
            "realized_pnl": realized.get(d, 0.0), "trades_closed": len(tr),
            "wins": sum(1 for t in tr if t["result"] == "win"),
            "losses": sum(1 for t in tr if t["result"] == "loss"),
            "daily_profit_halt": d in halts, "loss_streak_halt": d in streaks,
            "max_intraday_drawdown": min(dd) if dd else 0.0,
        })
        prev_end = eqs[-1]
    return rows


def summarize(result: BacktestResult) -> Dict[str, Any]:
    eq = [e for _, e in result.equity_curve]
    dd = drawdown_series(eq, result.initial_equity)
    tm = trade_metrics(result.trades)
    realized = sum(t["realized_pnl"] for t in result.trades)
    unrealized = sum(p["unrealized_pnl"] for p in result.open_positions)
    c = result.counters
    return {
        "period": {"start": result.config["start"], "end": result.config["end"],
                   "first_equity_point": result.equity_curve[0][0] if result.equity_curve else None,
                   "last_equity_point": result.equity_curve[-1][0] if result.equity_curve else None},
        "config": result.config,
        "strategy": {k: result.strategy_config.get(k) for k in (
            "strategy", "fast", "slow", "lookback", "hours_back", "risk_per_trade", "min_rr", "atr_sl_mult",
            "atr_tp_mult", "trailing_atr_mult", "max_leverage", "max_positions", "max_portfolio_heat",
            "max_symbol_exposure", "min_liquidity", "daily_loss_limit_pct", "max_consecutive_losses",
            "be_at_r", "scale_out", "max_giveback_pct", "daily_profit_halt", "allow_shorts", "ensemble_mode")},
        "portfolio": {
            "initial_equity": result.initial_equity,
            "ending_equity": result.final_equity,
            "return_pct": (result.final_equity / result.initial_equity - 1.0) * 100 if result.initial_equity else 0.0,
            "realized_pnl_closed_trades": realized,
            "unrealized_pnl_open_positions": unrealized,
            "max_drawdown_pct": min(dd) * 100 if dd else 0.0,
            "open_positions_at_end": len(result.open_positions),
            "unfilled_orders_at_end": len(result.unfilled_orders),
        },
        "trades": tm,
        "per_symbol": per_symbol(result),
        "execution": {
            "decision_bars": c["decision_bars"], "warmup_skips": c["warmup_skips"],
            "signals": {k: sum(v[k] for v in c["signals"].values()) for k in ("BUY", "SELL")},
            "risk_accept": c["risk"]["ACCEPT"], "risk_reject": c["risk"]["REJECT"],
            "rejects_by_reason": dict(sorted(c["rejects_by_reason"].items(), key=lambda kv: -kv[1])),
            "circuit_breaker_blocked_entries": c["circuit_breaker_blocked_entries"],
            "daily_profit_halt_blocked_entries": c["daily_profit_halt_blocked_entries"],
            "daily_profit_halt_days": len(set(result.daily_profit_halt_dates)),
            "loss_streak_halts": len(result.loss_streak_halt_dates),
            "sell_signals_while_flat_shorts_disabled": c["sell_signals_flat_shorts_disabled"],
            "duplicate_signals_prevented": c["duplicate_signals_prevented"],
            "trailing_stop_updates": c["trailing_stop_updates"], "break_evens": c["break_evens"],
            "scale_outs": c["scale_outs"],
            "exit_orders_by_reason": c["exit_orders"],
            "completed_trade_exit_reasons": _count(t["exit_reason"] for t in result.trades),
            "delayed_fills": c["delayed_fills"],
        },
        "excursion": diagnostics(result.trades, result.config["symbols"]),
        "warnings": result.warnings,
    }


def _count(items) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


# ---------------- salidas ----------------
def _money(v: Optional[float]) -> str:
    return "-" if v is None else f"${v:,.2f}"


def _num(v: Optional[float], fmt: str = "{:.2f}") -> str:
    if v is None:
        return "-"
    if v == math.inf:
        return "inf"
    return fmt.format(v)


def format_report(s: Dict[str, Any]) -> str:
    p, t, e = s["portfolio"], s["trades"], s["execution"]
    line = "─" * 44
    L = ["BACKTEST SUMMARY", line,
         f"Period: {s['period']['start']} → {s['period']['end']}",
         f"Symbols: {len(s['config']['symbols'])} ({','.join(s['config']['symbols'])})",
         f"Timeframe: {s['config']['timeframe']} | fills: next-bar open + {s['config']['slippage_bps']:g} bps"
         f" | commission {_money(s['config']['commission'])}/fill",
         "",
         f"{'Initial equity:':<22}{_money(p['initial_equity'])}",
         f"{'Ending equity:':<22}{_money(p['ending_equity'])}",
         f"{'Return:':<22}{p['return_pct']:.2f}%",
         f"{'Realized P&L:':<22}{_money(p['realized_pnl_closed_trades'])}",
         f"{'Unrealized (open):':<22}{_money(p['unrealized_pnl_open_positions'])} ({p['open_positions_at_end']} open)",
         "",
         f"{'Trades:':<22}{t['trades']}  (W {t['wins']} / L {t['losses']} / BE {t['breakevens']})",
         f"{'Win rate:':<22}{t['win_rate'] * 100:.1f}%",
         f"{'Avg win:':<22}{_money(t['avg_win'])}",
         f"{'Avg loss:':<22}{_money(t['avg_loss'])}",
         f"{'Expectancy:':<22}{_money(t['expectancy'])}  ({_num(t['expectancy_r'])} R)",
         f"{'Profit factor:':<22}{_num(t['profit_factor'])}",
         f"{'Total R:':<22}{_num(t['total_r'])}  (median {_num(t['median_r'])} R)",
         f"{'Max drawdown:':<22}{p['max_drawdown_pct']:.2f}%",
         f"{'Max loss streak:':<22}{t['max_consecutive_losses']}",
         f"{'Largest win/loss:':<22}{_money(t['largest_win'])} / {_money(t['largest_loss'])}",
         "",
         f"Signals: BUY {e['signals']['BUY']} / SELL {e['signals']['SELL']} | "
         f"Risk ACCEPT {e['risk_accept']} / REJECT {e['risk_reject']}"]
    if e["rejects_by_reason"]:
        L.append("Risk rejects:")
        L += [f"  {k:<32}{v}" for k, v in e["rejects_by_reason"].items()]
    if e["circuit_breaker_blocked_entries"]:
        L.append("Entries blocked by circuit breakers: "
                 + ", ".join(f"{k}={v}" for k, v in e["circuit_breaker_blocked_entries"].items()))
    L.append(f"Daily profit halt: {e['daily_profit_halt_days']} day(s), {e['daily_profit_halt_blocked_entries']} entries blocked"
             f" | loss-streak halts: {e['loss_streak_halts']}")
    L.append(f"Scale-outs: {e['scale_outs']} | exits: "
             + (", ".join(f"{k}={v}" for k, v in e["completed_trade_exit_reasons"].items()) or "-")
             + f" | delayed fills: {e['delayed_fills']}")
    L += ["", f"{'symbol':<7}{'trades':>7}{'W/L':>9}{'win%':>7}{'P&L':>14}{'avg':>11}{'PF':>7}"]
    for sym, d in s["per_symbol"].items():
        L.append(f"{sym:<7}{d['trades']:>7}{str(d['wins']) + '/' + str(d['losses']):>9}{d['win_rate'] * 100:>6.0f}%"
                 f"{_money(d['realized_pnl']):>14}{_money(d['avg_trade']):>11}{_num(d['profit_factor']):>7}")
    if p["unfilled_orders_at_end"]:
        L.append(f"\nUnfilled orders at end (no fill invented): {p['unfilled_orders_at_end']}")
    for w in s["warnings"]:
        L.append(f"WARNING: {w}")
    if t["trades"] < 30:
        L.append(f"\nNote: only {t['trades']} completed trades — metrics are statistically noisy.")
    if s.get("excursion") and t["trades"]:
        L += ["", format_diagnostics(s["excursion"])]
    return "\n".join(L)


def _json_default(o: Any) -> Any:
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return str(o)
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _clean(o: Any) -> Any:
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return str(o)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def to_json(obj: Any) -> str:
    return json.dumps(_clean(obj), indent=2, ensure_ascii=False, default=_json_default)


_TRADE_COLUMNS = ["trade_id", "symbol", "side", "entry_signal_timestamp", "entry_decision_timestamp",
                  "entry_fill_timestamp", "entry_fill_price", "modeled_entry", "initial_stop", "initial_take",
                  "initial_qty", "max_qty", "exit_fill_timestamp", "exit_fill_price", "exit_reason", "scale_outs",
                  "realized_pnl", "realized_r", "commission", "holding_seconds", "result",
                  # diagnóstico MFE/MAE (cierres de vela; ver backtest_excursion.py)
                  "excursion_bars", "mfe_dollars_per_share", "mae_dollars_per_share", "mfe_pct", "mae_pct",
                  "mfe_r", "mae_r", "mfe_timestamp", "mae_timestamp", "minutes_to_mfe", "minutes_to_mae",
                  "mae_before_mfe", "reached_0_5r", "reached_1_0r", "reached_1_5r", "reached_2_0r",
                  "exit_efficiency", "realized_vs_mfe_r", "mfe_left_on_table_r", "excursion_class",
                  "intrabar_mfe_dollars_per_share_diag", "intrabar_mae_dollars_per_share_diag",
                  "intrabar_mfe_r_diag", "intrabar_mae_r_diag"]


def write_outputs(result: BacktestResult, summary: Dict[str, Any], out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    p = out_dir / "trades.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_TRADE_COLUMNS + ["legs"])
        w.writeheader()
        for t in result.trades:
            row = {k: t.get(k) for k in _TRADE_COLUMNS}
            row["legs"] = json.dumps(t["legs"])
            w.writerow(row)
    written.append(p)

    p = out_dir / "trades.json"
    p.write_text(to_json(result.trades), encoding="utf-8")
    written.append(p)

    p = out_dir / "daily_results.csv"
    rows = daily_results(result)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        fields = ["date", "starting_equity", "ending_equity", "realized_pnl", "trades_closed", "wins", "losses",
                  "daily_profit_halt", "loss_streak_halt", "max_intraday_drawdown"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    written.append(p)

    p = out_dir / "equity_curve.csv"
    dd = drawdown_series([e for _, e in result.equity_curve], result.initial_equity)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "equity", "drawdown"])
        for (ts, eq), d in zip(result.equity_curve, dd):
            w.writerow([ts, f"{eq:.6f}", f"{d:.8f}"])
    written.append(p)

    p = out_dir / "summary.json"
    p.write_text(to_json(dict(summary, daily_results=rows, open_positions=result.open_positions,
                              unfilled_orders=result.unfilled_orders)), encoding="utf-8")
    written.append(p)
    return written
