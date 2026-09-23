# src/session_summary.py
"""
Resumen de fin de sesión (observability only).

SessionSummary se alimenta EXCLUSIVAMENTE de los eventos que SessionLogger
ya escribió en el JSONL, así que cada contador coincide con lo que hay en el
archivo: no se infiere ni se inventa nada, y no participa en ninguna
decisión de trading.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

# Eventos que representan la evaluación de una vela concreta. Varias
# estrategias (MA/MACD/RSI/BBANDS) o varios polls sobre la misma vela
# comparten (symbol, bar_timestamp) y cuentan UNA sola vez.
_BAR_EVENTS = ("strategy_evaluation", "ensemble_decision")

_ENSEMBLE_SIGNALS = ("BUY", "SELL", "HOLD")


def _normalize_signal(signal: Any) -> str:
    return "HOLD" if signal in (None, "", "HOLD") else str(signal)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Nunca comparar naive con aware: una marca sin zona se asume UTC solo para ordenar.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SessionSummary:
    def __init__(self, session_id: str, started_at: Optional[datetime] = None):
        self.session_id = session_id
        self.started_at = started_at or datetime.now(timezone.utc)
        self.configured_symbols: Optional[List[str]] = None
        self.event_counts: Counter = Counter()
        self._bars: Dict[str, Set[str]] = {}
        self.ensemble_per_evaluation: Counter = Counter()
        self._ensemble_bar_signals: Set[tuple] = set()
        self.guard_counts: Counter = Counter()
        self.freshness_counts: Counter = Counter()
        self.risk_decisions: Counter = Counter()
        self.risk_rejections_by_reason: Counter = Counter()
        self.order_submissions = 0
        self.order_results_by_status: Counter = Counter()

    # ---------------- alimentación ----------------
    def observe(self, event_type: str, fields: Dict[str, Any]) -> None:
        self.event_counts[event_type] += 1
        symbol = fields.get("symbol")
        bar_ts = fields.get("bar_timestamp")

        if event_type == "session_start":
            syms = (fields.get("config") or {}).get("symbols_parsed")
            if isinstance(syms, list):
                self.configured_symbols = [str(s) for s in syms]

        if event_type in _BAR_EVENTS and symbol and bar_ts:
            self._bars.setdefault(symbol, set()).add(str(bar_ts))

        if event_type == "ensemble_decision":
            sig = _normalize_signal(fields.get("signal"))
            self.ensemble_per_evaluation[sig] += 1
            if symbol and bar_ts:
                self._ensemble_bar_signals.add((symbol, str(bar_ts), sig))
        elif event_type == "execution_guard":
            self.guard_counts[str(fields.get("guard"))] += 1
        elif event_type == "data_freshness":
            self.freshness_counts[str(fields.get("status"))] += 1
        elif event_type == "risk_evaluation":
            decision = str(fields.get("decision"))
            self.risk_decisions[decision] += 1
            if decision == "REJECT":
                self.risk_rejections_by_reason[str(fields.get("reason_code") or "UNCLASSIFIED")] += 1
        elif event_type == "order_submission":
            self.order_submissions += 1
        elif event_type == "order_result":
            status = fields.get("status")
            self.order_results_by_status[str(status) if status else "status_unavailable"] += 1

    # ---------------- salida ----------------
    def _per_symbol(self) -> Dict[str, Dict[str, Any]]:
        symbols = list(self.configured_symbols or [])
        symbols += sorted(s for s in self._bars if s not in symbols)
        out: Dict[str, Dict[str, Any]] = {}
        for sym in symbols:
            bars = self._bars.get(sym, set())
            # Orden por datetime real (no lexicográfico); si alguno no parsea, se cae al texto.
            ordered = sorted(bars, key=lambda t: (_parse_ts(t) is None, _parse_ts(t) or datetime.min.replace(tzinfo=timezone.utc), t))
            out[sym] = {
                "first_bar_timestamp": ordered[0] if ordered else None,
                "last_bar_timestamp": ordered[-1] if ordered else None,
                "unique_bar_count": len(bars),
            }
        return out

    def to_dict(self, ended_at: Optional[datetime] = None) -> Dict[str, Any]:
        ended_at = ended_at or datetime.now(timezone.utc)
        per_bar = Counter(sig for (_, _, sig) in self._ensemble_bar_signals)
        per_symbol = self._per_symbol()
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "ended_at": ended_at.isoformat(timespec="milliseconds"),
            "runtime_seconds": round((ended_at - self.started_at).total_seconds(), 3),
            "symbols": list(per_symbol.keys()),
            "per_symbol": per_symbol,
            "unique_bar_count_total": sum(v["unique_bar_count"] for v in per_symbol.values()),
            "ensemble": {
                # una por ensemble_decision escrito (incluye polls repetidos de la misma vela)
                "evaluations": sum(self.ensemble_per_evaluation.values()),
                "per_evaluation": {s: self.ensemble_per_evaluation.get(s, 0) for s in _ENSEMBLE_SIGNALS}
                                  | {s: n for s, n in self.ensemble_per_evaluation.items() if s not in _ENSEMBLE_SIGNALS},
                # distintas (símbolo, vela, señal): una vela repetida con la misma señal cuenta una vez
                "per_unique_bar": {s: per_bar.get(s, 0) for s in _ENSEMBLE_SIGNALS}
                                  | {s: n for s, n in per_bar.items() if s not in _ENSEMBLE_SIGNALS},
            },
            "execution_guards": dict(self.guard_counts),
            "data_freshness": dict(self.freshness_counts),
            "risk": {
                "total": sum(self.risk_decisions.values()),
                "accept": self.risk_decisions.get("ACCEPT", 0),
                "reject": self.risk_decisions.get("REJECT", 0),
                "rejections_by_reason_code": dict(self.risk_rejections_by_reason),
            },
            "orders": {
                "submissions": self.order_submissions,
                "results_by_status": dict(self.order_results_by_status),
            },
            "event_counts": dict(self.event_counts),
        }


def format_summary(summary: Dict[str, Any], reason: str = "") -> str:
    """Resumen corto para consola (unas pocas líneas, una por símbolo)."""
    ens, risk, orders = summary["ensemble"], summary["risk"], summary["orders"]
    pe = ens["per_evaluation"]
    lines = [
        f"📊 Resumen de sesión {summary['session_id']} ({reason or 'fin'}) — duración {summary['runtime_seconds']:.0f}s",
        f"   Velas únicas: {summary['unique_bar_count_total']} | Ensemble (evaluaciones): "
        f"BUY={pe.get('BUY', 0)} SELL={pe.get('SELL', 0)} HOLD={pe.get('HOLD', 0)}",
        f"   Riesgo: {risk['total']} (ACCEPT={risk['accept']} REJECT={risk['reject']})"
        + (f" | rechazos: {_fmt_counts(risk['rejections_by_reason_code'])}" if risk["rejections_by_reason_code"] else ""),
        f"   Órdenes enviadas: {orders['submissions']}"
        + (f" | resultados: {_fmt_counts(orders['results_by_status'])}" if orders["results_by_status"] else "")
        + (f" | guardas: {_fmt_counts(summary['execution_guards'])}" if summary["execution_guards"] else ""),
    ]
    for sym, d in summary["per_symbol"].items():
        lines.append(f"   · {sym:<6} velas={d['unique_bar_count']:<4} {d['first_bar_timestamp'] or '-'} → {d['last_bar_timestamp'] or '-'}")
    return "\n".join(lines)


def _fmt_counts(counts: Dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
