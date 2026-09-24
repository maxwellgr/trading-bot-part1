# src/analyze_session.py
"""
Analizador OFFLINE de sesiones JSONL (logs/sessions/*.jsonl).

    python -m src.analyze_session <session.jsonl> [--json]

Solo lee el archivo: no llama a Alpaca, no importa run_paper ni ninguna
lógica de trading/riesgo, y no escribe nada. Funciona con sesiones completas
o parciales (proceso caído, sin session_end, última línea truncada).

Principio: no inventar errores. Si un chequeo no tiene evidencia suficiente
(p. ej. una risk_evaluation antigua sin bar_timestamp), se reporta como
"no verificable", nunca como fallo.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Copia local (sin importar execution_guards, que arrastra src.logger y sus
# handlers de archivo). tests/test_analyze_session.py verifica que coincide
# con los valores de la guarda en vivo.
_TF_RE = re.compile(r"^\s*(\d+)\s*(Min|T|Hour|H|Day|D|Week|W|Month|M)\s*$", re.IGNORECASE)
_TF_UNIT_SECONDS = {"min": 60, "t": 60, "hour": 3600, "h": 3600, "day": 86400, "d": 86400,
                    "week": 604800, "w": 604800, "month": 2592000, "m": 2592000}
STALE_AFTER_BARS = 5
MIN_STALE_SECONDS = 300

# Eventos que corresponden a la evaluación de una vela.
_BAR_EVENTS = ("strategy_evaluation", "ensemble_decision")
_SIGNALS = ("BUY", "SELL", "HOLD")
_MAX_EXAMPLES = 5
# Copia local de order_tracking.TERMINAL_STATUSES (verificada en tests).
_TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})


def timeframe_to_seconds(timeframe: Optional[str]) -> Optional[int]:
    m = _TF_RE.match(timeframe or "")
    return int(m.group(1)) * _TF_UNIT_SECONDS[m.group(2).lower()] if m else None


def stale_threshold_seconds(timeframe: Optional[str]) -> Optional[float]:
    tf = timeframe_to_seconds(timeframe)
    return float(max(STALE_AFTER_BARS * tf, MIN_STALE_SECONDS)) if tf else None


def parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm_signal(sig: Any) -> str:
    return "HOLD" if sig in (None, "", "HOLD") else str(sig)


# ---------------------------------------------------------------- carga
def load_session(path: Path) -> Tuple[List[Dict[str, Any]], List[str], List[int]]:
    """
    Devuelve (records, warnings, corrupt_line_numbers).
    - Última línea incompleta/corrupta: se omite con un warning (típico de
      un proceso que murió a mitad de escritura).
    - Línea corrupta en medio del archivo: se omite, se reporta como
      problema de integridad; nunca se descartan los eventos válidos.
    """
    records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    corrupt: List[int] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    # índice de la última línea no vacía
    last_idx = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
            if not isinstance(rec, dict):
                raise ValueError("no es un objeto JSON")
        except ValueError:
            if i == last_idx:
                warnings.append(f"Última línea ({i + 1}) incompleta o corrupta: se omitió "
                                f"(probable cierre abrupto). Los {len(records)} eventos anteriores se conservan.")
            else:
                corrupt.append(i + 1)
            continue
        records.append(rec)
    if corrupt:
        warnings.append(f"{len(corrupt)} línea(s) corruptas en medio del archivo se omitieron: {corrupt[:_MAX_EXAMPLES]}")
    return records, warnings, corrupt


# ---------------------------------------------------------------- análisis
def analyze(records: List[Dict[str, Any]], warnings: Optional[List[str]] = None,
            corrupt_lines: Optional[List[int]] = None) -> Dict[str, Any]:
    warnings = list(warnings or [])
    by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_type[str(r.get("event_type"))].append(r)

    start = by_type["session_start"][0] if by_type["session_start"] else None
    ends = by_type["session_end"]
    end = ends[-1] if ends else None
    config = (start or {}).get("config") or {}
    timeframe = config.get("timeframe")
    tf_secs = timeframe_to_seconds(timeframe)
    threshold = stale_threshold_seconds(timeframe)
    ignore_clock = config.get("ignore_clock")

    if start is None:
        warnings.append("No hay evento session_start: metadatos de configuración no disponibles.")
    if len(ends) > 1:
        warnings.append(f"Hay {len(ends)} eventos session_end (se esperaba 1); se usa el último.")

    symbols = _symbols(config, records)
    market = _market_data(by_type, symbols, tf_secs, threshold)
    signals = _signals(by_type, symbols)
    risk = _risk(by_type)
    orders = _orders(by_type)
    integrity = _integrity(by_type, market, threshold, ignore_clock, corrupt_lines or [])
    metadata = _metadata(records, start, end, config, symbols, timeframe, corrupt_lines or [])
    reconciliation = _reconcile(records, end, config)

    return {
        "metadata": metadata,
        "market_data": market,
        "signals": signals,
        "execution_integrity": integrity,
        "risk": risk,
        "orders": orders,
        "summary_reconciliation": reconciliation,
        "warnings": warnings,
    }


def _symbols(config: Dict[str, Any], records: Iterable[Dict[str, Any]]) -> List[str]:
    configured = config.get("symbols_parsed")
    if not isinstance(configured, list):
        raw = config.get("symbols") or config.get("symbol") or ""
        configured = [s.strip().upper() for s in str(raw).split(",") if s.strip()]
    out = [str(s) for s in configured]
    seen = sorted({str(r["symbol"]) for r in records if r.get("symbol")} - set(out))
    return out + seen


def _metadata(records, start, end, config, symbols, timeframe, corrupt_lines) -> Dict[str, Any]:
    first_ts = parse_ts(records[0].get("timestamp")) if records else None
    last_ts = parse_ts(records[-1].get("timestamp")) if records else None
    started = parse_ts((start or {}).get("timestamp")) or first_ts
    ended = parse_ts((end or {}).get("timestamp"))
    runtime_end = ended or last_ts
    return {
        "session_id": (start or (records[0] if records else {})).get("session_id"),
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "last_event_at": last_ts.isoformat() if last_ts else None,
        "runtime_seconds": round((runtime_end - started).total_seconds(), 3) if started and runtime_end else None,
        "runtime_basis": "session_end" if ended else "last_event (sin session_end)",
        "git_commit": (start or {}).get("git_commit"),
        "symbols": symbols,
        "timeframe": timeframe,
        "ensemble_mode": config.get("ensemble_mode"),
        "ignore_clock": config.get("ignore_clock"),
        "total_records": len(records),
        "corrupt_lines": len(corrupt_lines),
        "has_session_end": end is not None,
        "end_reason": (end or {}).get("reason"),
    }


def _poll_events(by_type) -> Tuple[str, List[Dict[str, Any]]]:
    """Una observación = un poll. En modo ensemble cada poll escribe N
    strategy_evaluation + 1 ensemble_decision; se usa este último para no
    contar N veces el mismo poll."""
    if by_type["ensemble_decision"]:
        return "ensemble_decision", by_type["ensemble_decision"]
    return "strategy_evaluation", by_type["strategy_evaluation"]


def _market_data(by_type, symbols, tf_secs, threshold) -> Dict[str, Any]:
    source, polls = _poll_events(by_type)
    unique: Dict[str, set] = defaultdict(set)
    for etype in _BAR_EVENTS:
        for r in by_type[etype]:
            if r.get("symbol") and r.get("bar_timestamp"):
                unique[r["symbol"]].add(r["bar_timestamp"])

    obs: Counter = Counter()
    regressions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    prev: Dict[str, Tuple[datetime, str]] = {}
    max_age: Dict[str, float] = {}
    over_threshold: Counter = Counter()
    for r in polls:
        sym, bts = r.get("symbol"), r.get("bar_timestamp")
        bar_dt = parse_ts(bts)
        if not sym or bar_dt is None:
            continue
        obs[sym] += 1
        if sym in prev and bar_dt < prev[sym][0]:
            regressions[sym].append({"observation_id": r.get("observation_id"),
                                     "previous_bar": prev[sym][1], "bar": bts})
        prev[sym] = (bar_dt, bts)
        logged = parse_ts(r.get("timestamp"))
        if logged is not None:
            age = (logged - bar_dt).total_seconds()
            max_age[sym] = max(max_age.get(sym, age), age)
            if threshold is not None and age > threshold:
                over_threshold[sym] += 1

    stale_guards = Counter(r.get("symbol") for r in by_type["execution_guard"] if r.get("guard") == "STALE_DATA")
    freshness_warn = Counter(r.get("symbol") for r in by_type["data_freshness"]
                             if r.get("status") in ("stale", "still_stale"))

    per_symbol: Dict[str, Any] = {}
    for sym in symbols:
        bars = sorted((d, s) for s in unique.get(sym, ()) if (d := parse_ts(s)) is not None)
        gaps = _gaps(bars, tf_secs)
        n_obs = obs.get(sym, 0)
        per_symbol[sym] = {
            "first_bar_timestamp": bars[0][1] if bars else None,
            "last_bar_timestamp": bars[-1][1] if bars else None,
            "unique_bar_count": len(unique.get(sym, ())),
            "observations": n_obs,
            "repeated_observations": max(0, n_obs - len(unique.get(sym, ()))),
            "timestamp_regressions": len(regressions.get(sym, [])),
            "regression_examples": regressions.get(sym, [])[:_MAX_EXAMPLES],
            "missing_bars": gaps["missing_bars"],
            "gap_count": gaps["gap_count"],
            "largest_gaps": gaps["largest"],
            "max_bar_age_seconds": round(max_age[sym], 1) if sym in max_age else None,
            "observations_older_than_threshold": over_threshold.get(sym, 0),
            "stale_guard_blocks": stale_guards.get(sym, 0),
            "stale_warnings": freshness_warn.get(sym, 0),
        }
    return {
        "observation_source": source,
        "timeframe_seconds": tf_secs,
        "stale_threshold_seconds": threshold,
        "per_symbol": per_symbol,
    }


def _gaps(bars: List[Tuple[datetime, str]], tf_secs: Optional[int]) -> Dict[str, Any]:
    """Velas faltantes entre velas únicas consecutivas. Informativo: IEX es
    disperso y los cierres de mercado generan huecos legítimos."""
    if not tf_secs or len(bars) < 2:
        return {"missing_bars": 0 if tf_secs else None, "gap_count": 0 if tf_secs else None, "largest": []}
    gaps = []
    for (a, sa), (b, sb) in zip(bars, bars[1:]):
        missing = int(round((b - a).total_seconds() / tf_secs)) - 1
        if missing > 0:
            gaps.append({"after": sa, "before": sb, "missing_bars": missing})
    gaps.sort(key=lambda g: -g["missing_bars"])
    return {"missing_bars": sum(g["missing_bars"] for g in gaps), "gap_count": len(gaps), "largest": gaps[:3]}


def _signals(by_type, symbols) -> Dict[str, Any]:
    events = by_type["ensemble_decision"]
    per_eval: Dict[str, Counter] = defaultdict(Counter)
    per_bar: Dict[str, set] = defaultdict(set)
    for r in events:
        sym, sig = r.get("symbol"), _norm_signal(r.get("signal"))
        per_eval[sym][sig] += 1
        if r.get("bar_timestamp"):
            per_bar[sym].add((r["bar_timestamp"], sig))
    per_symbol = {}
    for sym in symbols:
        bar_counts = Counter(sig for _, sig in per_bar.get(sym, ()))
        per_symbol[sym] = {
            "evaluations": {s: per_eval[sym].get(s, 0) for s in _SIGNALS},
            "unique_bars": {s: bar_counts.get(s, 0) for s in _SIGNALS},
        }
    return {"has_ensemble_events": bool(events), "per_symbol": per_symbol}


def _risk(by_type) -> Dict[str, Any]:
    events = by_type["risk_evaluation"]
    decisions = Counter(str(r.get("decision")) for r in events)
    reasons = Counter(str(r.get("reason_code") or "UNCLASSIFIED") for r in events if r.get("decision") == "REJECT")
    per_symbol: Dict[str, Counter] = defaultdict(Counter)
    for r in events:
        per_symbol[str(r.get("symbol"))][str(r.get("decision"))] += 1
    return {
        "total": len(events),
        "accept": decisions.get("ACCEPT", 0),
        "reject": decisions.get("REJECT", 0),
        "rejections_by_reason_code": dict(reasons.most_common()),
        "per_symbol": {s: {"total": sum(c.values()), "accept": c.get("ACCEPT", 0), "reject": c.get("REJECT", 0)}
                       for s, c in sorted(per_symbol.items())},
    }


def _order_lifecycle(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Estado final por order_id (último order_result/order_update visto) y fills confirmados."""
    last: Dict[str, Dict[str, Any]] = {}
    partial: set = set()
    fills, qty, pnl = 0, 0.0, 0.0
    for r in events:
        et = r.get("event_type")
        if et not in ("order_result", "order_update"):
            continue
        oid = r.get("order_id")
        if oid:
            last[str(oid)] = r
            if r.get("status") == "partially_filled":
                partial.add(str(oid))
        if et == "order_update":
            if r.get("newly_filled_qty"):
                fills += 1
                qty += float(r["newly_filled_qty"])
            if r.get("realized_pnl") is not None:
                pnl += float(r["realized_pnl"])
    final = Counter(str(r.get("status")) if r.get("status") else "status_unavailable" for r in last.values())
    unresolved = sorted(oid for oid, r in last.items() if r.get("status") not in _TERMINAL_STATUSES)
    return {"last": last, "final": final, "partial": sorted(partial), "fills": fills,
            "filled_qty_total": round(qty, 6), "confirmed_realized_pnl": round(pnl, 6), "unresolved": unresolved}


def _orders(by_type) -> Dict[str, Any]:
    subs, results, updates = by_type["order_submission"], by_type["order_result"], by_type["order_update"]
    status = Counter(str(r.get("status")) if r.get("status") else "status_unavailable" for r in results)
    per_symbol: Dict[str, Counter] = defaultdict(Counter)
    for r in subs:
        per_symbol[str(r.get("symbol"))]["submissions"] += 1
    for r in results:
        per_symbol[str(r.get("symbol"))]["results"] += 1
    for r in updates:
        if r.get("newly_filled_qty"):
            per_symbol[str(r.get("symbol"))]["fills"] += 1
    life = _order_lifecycle(sorted(results + updates, key=lambda r: r.get("observation_id") or 0))
    note = None if (subs or results) else "Sin órdenes en esta sesión (no es un error)."
    if results and not updates:
        note = ("Sin eventos order_update: el log no contiene confirmación de fills "
                "(los estados de order_result son solo el acuse del envío).")
    return {
        "submissions": len(subs),
        "results": len(results),
        "results_by_status": dict(status.most_common()),
        "updates": len(updates),
        "final_status_by_order": dict(life["final"].most_common()),
        "partially_filled_orders": life["partial"],
        "fills": life["fills"],
        "filled_qty_total": life["filled_qty_total"],
        "confirmed_realized_pnl": life["confirmed_realized_pnl"],
        "unresolved_orders": [{"order_id": oid, "symbol": life["last"][oid].get("symbol"),
                               "purpose": life["last"][oid].get("purpose"), "status": life["last"][oid].get("status")}
                              for oid in life["unresolved"]],
        "per_symbol": {s: {"submissions": c.get("submissions", 0), "results": c.get("results", 0), "fills": c.get("fills", 0)}
                       for s, c in sorted(per_symbol.items())},
        "note": note,
    }


def _integrity(by_type, market, threshold, ignore_clock, corrupt_lines) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    not_verifiable: List[Dict[str, Any]] = []

    # 1) misma (símbolo, vela, lado) evaluada por riesgo más de una vez
    risk = by_type["risk_evaluation"]
    keyed: Dict[Tuple[str, str, str], List[Any]] = defaultdict(list)
    missing_bar = [r for r in risk if not r.get("bar_timestamp")]
    for r in risk:
        if r.get("bar_timestamp"):
            side = str(r.get("signal") or r.get("side"))
            keyed[(str(r.get("symbol")), r["bar_timestamp"], side)].append(r.get("observation_id"))
    dups = [{"symbol": k[0], "bar_timestamp": k[1], "side": k[2], "risk_evaluations": len(v), "observation_ids": v}
            for k, v in keyed.items() if len(v) > 1]
    if dups:
        issues.append({"check": "duplicate_signal_reached_risk", "count": len(dups), "examples": dups[:_MAX_EXAMPLES]})
    if missing_bar:
        issues.append({"check": "risk_evaluation_missing_bar_timestamp", "count": len(missing_bar),
                       "examples": [r.get("observation_id") for r in missing_bar[:_MAX_EXAMPLES]]})
        not_verifiable.append({"check": "duplicate_signal_reached_risk", "records": len(missing_bar),
                               "why": "risk_evaluation sin bar_timestamp: no se puede asociar a una vela"})

    # 2) órdenes sin contexto trazable
    for etype, required in (("order_submission", ("symbol", "bar_timestamp", "side")),
                            ("order_result", ("symbol", "bar_timestamp", "order_id"))):
        bad = [r for r in by_type[etype] if any(not r.get(f) for f in required)]
        if bad:
            fields = Counter(f for r in bad for f in required if not r.get(f))
            issues.append({"check": f"{etype}_missing_context", "count": len(bad), "missing_fields": dict(fields),
                           "examples": [r.get("observation_id") for r in bad[:_MAX_EXAMPLES]]})

    # 3) intentos de ejecución con datos obsoletos (edad de la vela al llegar a riesgo/orden)
    attempts = [r for r in risk + by_type["order_submission"] if r.get("bar_timestamp")]
    stale_attempts = []
    for r in attempts:
        bar_dt, logged = parse_ts(r.get("bar_timestamp")), parse_ts(r.get("timestamp"))
        if bar_dt and logged and threshold is not None:
            age = (logged - bar_dt).total_seconds()
            if age > threshold:
                stale_attempts.append({"event_type": r.get("event_type"), "observation_id": r.get("observation_id"),
                                       "symbol": r.get("symbol"), "bar_timestamp": r.get("bar_timestamp"),
                                       "age_seconds": round(age, 1)})
    if stale_attempts:
        if ignore_clock is False:
            # Sin --ignore-clock el loop solo opera con el mercado abierto: la evidencia es suficiente.
            issues.append({"check": "stale_data_execution_attempt", "count": len(stale_attempts),
                           "threshold_seconds": threshold, "examples": stale_attempts[:_MAX_EXAMPLES]})
        else:
            not_verifiable.append({"check": "stale_data_execution_attempt", "records": len(stale_attempts),
                                   "why": "ignore_clock activo o desconocido: una vela vieja puede ser legítima con mercado cerrado"})
    elif threshold is None and attempts:
        not_verifiable.append({"check": "stale_data_execution_attempt", "records": len(attempts),
                               "why": "timeframe desconocido: sin umbral de obsolescencia"})

    # 4) regresiones de timestamp
    regs = {s: d["timestamp_regressions"] for s, d in market["per_symbol"].items() if d["timestamp_regressions"]}
    if regs:
        issues.append({"check": "bar_timestamp_regression", "count": sum(regs.values()), "per_symbol": regs})

    # 5) líneas corruptas en medio del archivo
    if corrupt_lines:
        issues.append({"check": "corrupt_jsonl_lines", "count": len(corrupt_lines), "lines": corrupt_lines[:_MAX_EXAMPLES]})

    guards = Counter(str(r.get("guard")) for r in by_type["execution_guard"])
    return {
        "status": "ISSUES" if issues else "OK",
        "issues": issues,
        "not_verifiable": not_verifiable,
        "execution_guards": dict(guards),
    }


# ---------------------------------------------------------------- reconciliación
# Campos de session_end.summary que no se pueden reconstruir exactamente desde el JSONL.
_NOT_RECONSTRUCTABLE = {
    "started_at": "hora de creación del logger (no es un evento del JSONL)",
    "ended_at": "calculada justo antes de escribir session_end",
    "runtime_seconds": "depende de started_at/ended_at",
}


def reconstruct_summary(records: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstrucción INDEPENDIENTE (no usa SessionSummary) de las métricas
    del resumen, a partir de los eventos anteriores al primer session_end."""
    events: List[Dict[str, Any]] = []
    for r in records:
        if r.get("event_type") == "session_end":
            break
        events.append(r)
    bars: Dict[str, set] = defaultdict(set)
    ens_eval: Counter = Counter()
    ens_bar: set = set()
    guards, fresh, decisions, reasons, statuses, etypes = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    subs = 0
    updates = 0
    for r in events:
        et = r.get("event_type")
        etypes[et] += 1
        sym, bts = r.get("symbol"), r.get("bar_timestamp")
        if et in _BAR_EVENTS and sym and bts:
            bars[sym].add(str(bts))
        if et == "ensemble_decision":
            sig = _norm_signal(r.get("signal"))
            ens_eval[sig] += 1
            if sym and bts:
                ens_bar.add((sym, str(bts), sig))
        elif et == "execution_guard":
            guards[str(r.get("guard"))] += 1
        elif et == "data_freshness":
            fresh[str(r.get("status"))] += 1
        elif et == "risk_evaluation":
            decisions[str(r.get("decision"))] += 1
            if str(r.get("decision")) == "REJECT":
                reasons[str(r.get("reason_code") or "UNCLASSIFIED")] += 1
        elif et == "order_submission":
            subs += 1
        elif et == "order_result":
            statuses[str(r.get("status")) if r.get("status") else "status_unavailable"] += 1
        elif et == "order_update":
            updates += 1
    life = _order_lifecycle(events)

    configured = config.get("symbols_parsed") if isinstance(config.get("symbols_parsed"), list) else []
    symbols = [str(s) for s in configured] + sorted(s for s in bars if s not in configured)
    per_symbol = {}
    for s in symbols:
        ordered = sorted(bars.get(s, ()), key=lambda t: (parse_ts(t) is None, parse_ts(t) or datetime.min.replace(tzinfo=timezone.utc), t))
        per_symbol[s] = {"first_bar_timestamp": ordered[0] if ordered else None,
                         "last_bar_timestamp": ordered[-1] if ordered else None,
                         "unique_bar_count": len(ordered)}
    per_bar = Counter(sig for _, _, sig in ens_bar)
    return {
        "session_id": (events[0] if events else {}).get("session_id"),
        "symbols": symbols,
        "per_symbol": per_symbol,
        "unique_bar_count_total": sum(v["unique_bar_count"] for v in per_symbol.values()),
        "ensemble": {
            "evaluations": sum(ens_eval.values()),
            "per_evaluation": {s: ens_eval.get(s, 0) for s in _SIGNALS} | {k: v for k, v in ens_eval.items() if k not in _SIGNALS},
            "per_unique_bar": {s: per_bar.get(s, 0) for s in _SIGNALS} | {k: v for k, v in per_bar.items() if k not in _SIGNALS},
        },
        "execution_guards": dict(guards),
        "data_freshness": dict(fresh),
        "risk": {"total": sum(decisions.values()), "accept": decisions.get("ACCEPT", 0),
                 "reject": decisions.get("REJECT", 0), "rejections_by_reason_code": dict(reasons)},
        "orders": {"submissions": subs, "results_by_status": dict(statuses),
                   "updates": updates,
                   "final_status_by_order": dict(life["final"]),
                   "partially_filled_orders": len(life["partial"]),
                   "fills": life["fills"],
                   "filled_qty_total": life["filled_qty_total"],
                   "unresolved_orders": len(life["unresolved"]),
                   "confirmed_realized_pnl": life["confirmed_realized_pnl"]},
        "event_counts": dict(etypes),
    }


def _flatten(d: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(d, dict) and d:
        out: Dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: d}


_ABSENT = "<ausente>"

# Campos añadidos al resumen después de que existieran sesiones grabadas: si el
# session_end.summary no trae NINGUNO (sesión anterior), no se comparan en vez
# de reportarlos como discrepancia.
_LATER_SUMMARY_FIELDS = ("orders.updates", "orders.final_status_by_order", "orders.partially_filled_orders",
                         "orders.fills", "orders.filled_qty_total", "orders.unresolved_orders",
                         "orders.confirmed_realized_pnl")


def _is_later_field(field: str) -> bool:
    return any(field == f or field.startswith(f + ".") for f in _LATER_SUMMARY_FIELDS)


def _norm_empty(v: Any) -> Any:
    return {} if v == _ABSENT else v


def _reconcile(records, end, config) -> Dict[str, Any]:
    summary = (end or {}).get("summary")
    if not isinstance(summary, dict):
        why = "sin session_end" if end is None else "session_end sin summary (sesión anterior al resumen)"
        return {"status": "UNAVAILABLE", "reason": why, "mismatches": [], "compared_fields": 0}
    rebuilt = _flatten(reconstruct_summary(records, config))
    logged = {k: v for k, v in _flatten(summary).items() if k.split(".")[0] not in _NOT_RECONSTRUCTABLE}
    not_compared = dict(_NOT_RECONSTRUCTABLE)
    if not any(_is_later_field(k) for k in logged):
        legacy = [f for f in rebuilt if _is_later_field(f)]
        for f in legacy:
            rebuilt.pop(f)
        if legacy:
            not_compared["orders.<ciclo de vida>"] = "resumen anterior a order_update: campos ausentes en session_end"
    mismatches = []
    for field in sorted(set(rebuilt) | set(logged)):
        a, b = logged.get(field, _ABSENT), rebuilt.get(field, _ABSENT)
        # un contador vacío ({}) equivale a un campo ausente
        if _norm_empty(a) == _norm_empty(b):
            continue
        mismatches.append({"field": field, "session_end_summary": a, "reconstructed": b})
    return {
        "status": "MISMATCH" if mismatches else "MATCH",
        "compared_fields": len(set(rebuilt) | set(logged)),
        "mismatches": mismatches,
        "not_compared": not_compared,
    }


# ---------------------------------------------------------------- salida
def format_report(a: Dict[str, Any]) -> str:
    m, md, sig, integ, risk, orders, rec = (a["metadata"], a["market_data"], a["signals"], a["execution_integrity"],
                                           a["risk"], a["orders"], a["summary_reconciliation"])
    L: List[str] = []
    L.append(f"=== Sesión {m['session_id']} ===")
    L.append(f"Inicio: {m['started_at']} | Fin: {m['ended_at'] or '-'} | Duración: "
             f"{_fmt_secs(m['runtime_seconds'])} ({m['runtime_basis']})")
    L.append(f"Commit: {m['git_commit'] or '-'} | Timeframe: {m['timeframe'] or '-'} | Ensemble: {m['ensemble_mode'] or '-'}"
             f" | ignore_clock: {m['ignore_clock']}")
    L.append(f"Símbolos: {', '.join(m['symbols']) or '-'}")
    L.append(f"Registros: {m['total_records']} | session_end: {'sí (' + str(m['end_reason']) + ')' if m['has_session_end'] else 'NO'}")
    for w in a["warnings"]:
        L.append(f"AVISO: {w}")

    L.append("")
    L.append(f"--- Datos de mercado (umbral obsoleto {_fmt_secs(md['stale_threshold_seconds'])}; polls vía {md['observation_source']}) ---")
    L.append(f"{'símbolo':<7} {'primera':<12} {'última':<12} {'únicas':>6} {'repet.':>6} {'regr.':>5} "
             f"{'faltan':>6} {'edad máx':>8} {'>umbral':>7} {'bloq.':>5}")
    for s, d in md["per_symbol"].items():
        L.append(f"{s:<7} {_short(d['first_bar_timestamp']):<12} {_short(d['last_bar_timestamp']):<12} "
                 f"{d['unique_bar_count']:>6} {d['repeated_observations']:>6} {d['timestamp_regressions']:>5} "
                 f"{_dash(d['missing_bars']):>6} {_fmt_secs(d['max_bar_age_seconds']):>8} "
                 f"{d['observations_older_than_threshold']:>7} {d['stale_guard_blocks']:>5}")
    L.append("(repet. = polls de una vela ya vista: normal. faltan = velas ausentes entre únicas: informativo. "
             ">umbral = polls con vela más vieja que el umbral. bloq. = bloqueos STALE_DATA.)")

    L.append("")
    L.append("--- Señales ensemble (evaluaciones | velas únicas) ---")
    if not sig["has_ensemble_events"]:
        L.append("Sin eventos ensemble_decision (modo de estrategia única o sesión vacía).")
    else:
        for s, d in sig["per_symbol"].items():
            e, u = d["evaluations"], d["unique_bars"]
            L.append(f"{s:<7} BUY {e['BUY']:>4}|{u['BUY']:<4} SELL {e['SELL']:>4}|{u['SELL']:<4} HOLD {e['HOLD']:>5}|{u['HOLD']:<4}")

    L.append("")
    L.append(f"--- Integridad de ejecución: {integ['status']} ---")
    for i in integ["issues"]:
        L.append(f"[!] {i['check']}: {i['count']}" + (f" | ej: {i.get('examples')}" if i.get("examples") else "")
                 + (f" | por símbolo: {i['per_symbol']}" if i.get("per_symbol") else ""))
    for n in integ["not_verifiable"]:
        L.append(f"[?] {n['check']}: no verificable para {n['records']} registro(s) — {n['why']}")
    if integ["execution_guards"]:
        L.append(f"Guardas activadas: {_fmt_counts(integ['execution_guards'])}")

    L.append("")
    L.append(f"--- Riesgo: {risk['total']} evaluaciones (ACCEPT={risk['accept']} REJECT={risk['reject']}) ---")
    if risk["rejections_by_reason_code"]:
        L.append(f"Rechazos: {_fmt_counts(risk['rejections_by_reason_code'])}")
    if risk["per_symbol"]:
        L.append("Por símbolo: " + ", ".join(f"{s} {d['total']} (A{d['accept']}/R{d['reject']})" for s, d in risk["per_symbol"].items()))

    L.append("")
    L.append(f"--- Órdenes: {orders['submissions']} enviadas, {orders['results']} resultados ---")
    if not (orders["submissions"] or orders["results"]):
        L.append(orders["note"])
    else:
        if orders["results_by_status"]:
            L.append(f"Acuse de envío (order_result): {_fmt_counts(orders['results_by_status'])}")
        if orders["final_status_by_order"]:
            L.append(f"Estado final por orden: {_fmt_counts(orders['final_status_by_order'])}")
        L.append(f"Actualizaciones: {orders['updates']} | fills confirmados: {orders['fills']} "
                 f"(qty {orders['filled_qty_total']:g}) | parcialmente llenadas: {len(orders['partially_filled_orders'])} "
                 f"| P&L realizado confirmado: {orders['confirmed_realized_pnl']:+.2f}")
        if orders["unresolved_orders"]:
            L.append(f"[!] Órdenes sin estado final: {len(orders['unresolved_orders'])} | ej: "
                     + ", ".join(f"{u['symbol']} {u['purpose'] or '-'} {u['status']} {u['order_id']}"
                                 for u in orders["unresolved_orders"][:_MAX_EXAMPLES]))
        L.append("Por símbolo (envíos/acuses/fills): " + ", ".join(
            f"{s} {d['submissions']}/{d['results']}/{d['fills']}" for s, d in orders["per_symbol"].items()))
        if orders["note"]:
            L.append(orders["note"])

    L.append("")
    L.append(f"--- Reconciliación con session_end.summary: {rec['status']} ---")
    if rec["status"] == "UNAVAILABLE":
        L.append(f"No disponible: {rec['reason']}.")
    else:
        L.append(f"Campos comparados: {rec['compared_fields']} (no comparables: {', '.join(rec['not_compared'])})")
        for mm in rec["mismatches"][:20]:
            L.append(f"  ≠ {mm['field']}: summary={mm['session_end_summary']!r} reconstruido={mm['reconstructed']!r}")
        if len(rec["mismatches"]) > 20:
            L.append(f"  … y {len(rec['mismatches']) - 20} más (usa --json)")
    return "\n".join(L)


def _short(ts: Optional[str]) -> str:
    dt = parse_ts(ts)
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M") + "Z" if dt else "-"


def _dash(v: Any) -> str:
    return "-" if v is None else str(v)


def _fmt_secs(v: Optional[float]) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v < 120:
        return f"{v:.0f}s"
    if v < 7200:
        return f"{v / 60:.1f}m"
    return f"{v / 3600:.1f}h"


def _fmt_counts(c: Dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def analyze_file(path: Path) -> Dict[str, Any]:
    records, warnings, corrupt = load_session(path)
    result = analyze(records, warnings, corrupt)
    result["metadata"]["path"] = str(path)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Analiza offline un JSONL de sesión de paper trading (solo lectura).")
    p.add_argument("path", type=Path, help="Ruta al archivo logs/sessions/<session>.jsonl")
    p.add_argument("--json", action="store_true", help="Salida JSON legible por máquina")
    args = p.parse_args(argv)
    if not args.path.is_file():
        print(f"No existe el archivo: {args.path}", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    result = analyze_file(args.path)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str) if args.json else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
