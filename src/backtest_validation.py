# src/backtest_validation.py
"""
Validación del backtester contra una sesión REAL de paper trading (JSONL).

Reproduce la ventana de la sesión con su misma config (session_start.config)
y equity inicial, sobre las barras históricas locales, y compara:
  - velas/señales: mismo cierre y misma señal para cada (símbolo, vela)
  - decisiones de riesgo donde los datos de entrada coinciden
  - ciclo de vida: entradas, scale-outs y salidas (símbolo, vela, motivo)
  - fills del backtest: nunca en la vela de la señal ni antes de la decisión
  - precios de fill reales (solo si la sesión tiene eventos order_update)

NO se espera paridad exacta de P&L. Fuentes de divergencia esperadas:
  1) el bot en vivo evalúa la vela más reciente en cada poll (~10 s); si
     la vela del histórico IEX difiere de la vista en vivo (vela aún
     formándose o corregida después), el cierre y la señal pueden diferir;
  2) fills: el backtest llena a la apertura de la vela siguiente + slippage;
     Alpaca llena a mercado segundos después de la decisión;
  3) latencia/orden de polls: en vivo cada símbolo se procesa ~4 s después
     del anterior y una orden en curso retiene el símbolo;
  4) equity: Alpaca valúa con el último trade; el backtest con el último cierre.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .analyze_session import load_session, timeframe_to_seconds
from .backtest_engine import BacktestConfig, production_args, run_backtest
from .historical_data import load_universe

PRICE_TOL = 1e-6
_EXIT_ACTIONS = ("scale_out", "giveback_close", "exit")


def _live_evaluations(records: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Tuple[float, Optional[str]]]]:
    src = "ensemble_decision" if any(r.get("event_type") == "ensemble_decision" for r in records) else "strategy_evaluation"
    out: Dict[Tuple[str, str], List[Tuple[float, Optional[str]]]] = {}
    for r in records:
        if r.get("event_type") == src and r.get("symbol") and r.get("bar_timestamp"):
            out.setdefault((r["symbol"], r["bar_timestamp"]), []).append((r.get("bar_close"), r.get("signal")))
    return out


def _live_actions(records: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    acts = []
    for r in records:
        et = r.get("event_type")
        if et == "risk_evaluation" and r.get("decision") == "ACCEPT":
            acts.append((r["symbol"], r.get("bar_timestamp"), "entry"))
        elif et == "position_management" and r.get("action") in _EXIT_ACTIONS:
            kind = r.get("exit_kind") if r["action"] == "exit" else r["action"]
            acts.append((r["symbol"], r.get("bar_timestamp"), kind))
    return acts


def _close(a: Optional[float], b: Optional[float], tol: float = 0.011) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def validate_session(session_path: Path, data_dir: Path, slippage_bps: float = 5.0, commission: float = 0.0) -> Dict[str, Any]:
    records, load_warnings, _ = load_session(Path(session_path))
    start = next((r for r in records if r.get("event_type") == "session_start"), None)
    if start is None:
        raise ValueError("La sesión no tiene session_start: no se puede reconstruir su config.")
    cfg = start.get("config") or {}
    symbols = cfg.get("symbols_parsed") or [s.strip().upper() for s in str(cfg.get("symbols", "")).split(",") if s.strip()]
    timeframe = cfg.get("timeframe", "1Min")
    tf = pd.Timedelta(seconds=timeframe_to_seconds(timeframe) or 60)
    end = next((r for r in records if r.get("event_type") == "session_end"), None) or records[-1]
    t0, t1 = pd.Timestamp(start["timestamp"]), pd.Timestamp(end["timestamp"])
    args = production_args(cfg)

    data = load_universe(data_dir, timeframe, symbols, t0 - pd.Timedelta(hours=args.hours_back + 24), t1 + pd.Timedelta(hours=1))
    config = BacktestConfig(symbols=symbols, timeframe=timeframe, initial_equity=float(cfg.get("starting_equity") or 100_000.0),
                            slippage_bps=slippage_bps, commission=commission,
                            decision_start_utc=t0 - tf, decision_end_utc=t1, record_evaluations=True)
    result = run_backtest(config, data.bars, args)

    # ---- velas y señales
    live = _live_evaluations(records)
    engine = {(e["symbol"], e["bar_timestamp"]): e for e in result.evaluations}
    matched, close_mm, signal_mm, live_only, changing = 0, [], [], [], []
    for key, obs in sorted(live.items()):
        if len({o for o in obs}) > 1:
            changing.append({"symbol": key[0], "bar_timestamp": key[1], "live_observations": obs})
        e = engine.get(key)
        if e is None:
            live_only.append({"symbol": key[0], "bar_timestamp": key[1]})
            continue
        live_close, live_sig = obs[-1]
        if live_close is None or abs(float(live_close) - e["bar_close"]) > PRICE_TOL:
            close_mm.append({"symbol": key[0], "bar_timestamp": key[1], "live_close": live_close, "historical_close": e["bar_close"],
                             "live_signal": live_sig, "backtest_signal": e["signal"]})
        elif (live_sig or None) != (e["signal"] or None):
            signal_mm.append({"symbol": key[0], "bar_timestamp": key[1], "live_signal": live_sig, "backtest_signal": e["signal"]})
        else:
            matched += 1
    engine_only = [{"symbol": k[0], "bar_timestamp": k[1]} for k in sorted(engine) if k not in live]

    # ---- riesgo
    live_risk = {(r["symbol"], r.get("bar_timestamp")): r for r in records if r.get("event_type") == "risk_evaluation"}
    bt_risk = {(r["symbol"], r["bar_timestamp"]): r for r in result.risk_evaluations}
    risk_rows = []
    for key in sorted(set(live_risk) | set(bt_risk), key=lambda k: (str(k[1]), k[0])):
        lr, br = live_risk.get(key), bt_risk.get(key)
        row = {"symbol": key[0], "bar_timestamp": key[1],
               "live": None if lr is None else {k: lr.get(k) for k in ("decision", "reason_code", "entry_price", "stop_price", "take_profit", "position_size")},
               "backtest": None if br is None else {k: br.get(k) for k in ("decision", "reason_code", "entry_price", "stop_price", "take_profit", "position_size")}}
        if lr and br:
            row["decision_match"] = lr.get("decision") == br["decision"] and lr.get("reason_code") == br["reason_code"]
            row["levels_match"] = all(_close(lr.get(k), br.get(k)) for k in ("entry_price", "stop_price", "take_profit")) \
                if lr.get("decision") == "ACCEPT" == br["decision"] else None
        risk_rows.append(row)

    # ---- ciclo de vida
    live_acts = _live_actions(records)
    bt_acts = [(o["symbol"], o["signal_bar_ts"], o["purpose"]) for o in
               [dict(symbol=f["symbol"], signal_bar_ts=f["signal_bar_ts"], purpose=f["purpose"]) for f in result.fills]
               + [dict(symbol=u["symbol"], signal_bar_ts=u["signal_bar_timestamp"], purpose=u["purpose"]) for u in result.unfilled_orders]]
    bt_acts_sorted = sorted(set(bt_acts), key=lambda a: (a[1], a[0]))

    # ---- fills imposibles (el backtest nunca puede llenar en la vela de la señal ni antes de decidir)
    impossible = [f for f in result.fills
                  if pd.Timestamp(f["fill_ts"]) <= pd.Timestamp(f["signal_bar_ts"]) or pd.Timestamp(f["fill_ts"]) < pd.Timestamp(f["decision_ts"])]

    # ---- fills reales (solo sesiones con order_update)
    live_fills = [r for r in records if r.get("event_type") == "order_update" and r.get("newly_filled_qty")]
    fill_cmp = []
    for lf in live_fills:
        cand = [f for f in result.fills if f["symbol"] == lf["symbol"] and f["purpose"] == lf.get("purpose")
                and f["signal_bar_ts"] == lf.get("bar_timestamp")]
        fill_cmp.append({"symbol": lf["symbol"], "purpose": lf.get("purpose"), "bar_timestamp": lf.get("bar_timestamp"),
                         "live_fill_price": lf.get("fill_price"), "backtest_fill_price": cand[0]["price"] if cand else None})

    return {
        "session": str(session_path), "session_id": start.get("session_id"),
        "window": {"start": t0.isoformat(), "end": t1.isoformat()}, "symbols": symbols,
        "initial_equity": config.initial_equity, "warnings": load_warnings + data.warnings,
        "bars": {"live_unique_bars": len(live), "matched": matched, "close_mismatches": close_mm,
                 "signal_mismatches": signal_mm, "live_only": live_only, "backtest_only": engine_only,
                 "bars_that_changed_during_live_polling": changing},
        "risk": risk_rows,
        "lifecycle": {"live": live_acts, "backtest": bt_acts_sorted,
                      "matched": sorted(set(live_acts) & set(bt_acts_sorted), key=lambda a: (str(a[1]), a[0])),
                      "live_only": sorted(set(live_acts) - set(bt_acts_sorted), key=lambda a: (str(a[1]), a[0])),
                      "backtest_only": sorted(set(bt_acts_sorted) - set(live_acts), key=lambda a: (str(a[1]), a[0]))},
        "backtest_fills": [{k: f[k] for k in ("symbol", "purpose", "signal_bar_ts", "fill_ts", "qty", "price", "realized_pnl")}
                           for f in result.fills],
        "impossible_fills": len(impossible),
        "live_fill_comparison": fill_cmp if live_fills else "no disponible: la sesión no tiene eventos order_update (anterior al registro de fills)",
    }


def format_validation(v: Dict[str, Any]) -> str:
    b, lc = v["bars"], v["lifecycle"]
    L = [f"SESSION VALIDATION {v['session_id']}", "─" * 44,
         f"Window: {v['window']['start']} → {v['window']['end']} | equity {v['initial_equity']:,.2f}",
         f"Bars: {b['live_unique_bars']} live unique | {b['matched']} identical close+signal | "
         f"{len(b['close_mismatches'])} close mismatch | {len(b['signal_mismatches'])} signal mismatch | "
         f"{len(b['live_only'])} live-only | {len(b['backtest_only'])} backtest-only"]
    for m in b["close_mismatches"][:10]:
        L.append(f"  ≠ close {m['symbol']} {m['bar_timestamp']}: live {m['live_close']} vs hist {m['historical_close']}"
                 f" (signal {m['live_signal']} vs {m['backtest_signal']})")
    for m in b["signal_mismatches"][:10]:
        L.append(f"  ≠ signal {m['symbol']} {m['bar_timestamp']}: live {m['live_signal']} vs backtest {m['backtest_signal']}")
    if b["bars_that_changed_during_live_polling"]:
        L.append(f"  Bars whose live close/signal changed between polls: {len(b['bars_that_changed_during_live_polling'])}")
    L.append("Risk decisions:")
    for r in v["risk"]:
        lv, bt = r["live"], r["backtest"]
        L.append(f"  {r['symbol']:<5} {r['bar_timestamp']}: live {lv and lv['decision']}/{lv and lv['reason_code']} "
                 f"qty {lv and lv['position_size']} | backtest {bt and bt['decision']}/{bt and bt['reason_code']} "
                 f"qty {bt and bt['position_size']}"
                 + (f" | levels {'match' if r.get('levels_match') else 'differ'}" if r.get("levels_match") is not None else ""))
    L.append(f"Lifecycle: {len(lc['matched'])} matched | live-only {lc['live_only']} | backtest-only {lc['backtest_only']}")
    for a in lc["matched"]:
        L.append(f"  = {a[0]:<5} {a[1]} {a[2]}")
    L.append(f"Impossible fills (same bar / before decision): {v['impossible_fills']}")
    lf = v["live_fill_comparison"]
    L.append(f"Live fill comparison: {lf}" if isinstance(lf, str) else f"Live fill comparison: {len(lf)} fills")
    for w in v["warnings"]:
        L.append(f"WARNING: {w}")
    L.append("Expected divergences: live polls see forming/revised bars; real fills vs next-open model; "
             "per-symbol poll latency; equity valuation. Exact P&L parity is NOT expected.")
    return "\n".join(L)
