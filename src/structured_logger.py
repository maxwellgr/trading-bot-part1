# src/structured_logger.py
"""
Structured Trading Diagnostics / Event Logger (Fase B — observability only).

Escribe un evento JSON por línea (JSONL, UTF-8) por cada tick relevante del
bot: evaluación de estrategia, decisión de ensemble, evaluación de riesgo,
envío/resultado de orden y gestión de posición. Un archivo por sesión en
logs/sessions/, para que un análisis posterior con pandas no dependa de
parsear el log humano con emojis (--explain / consola).

Reglas de esta fase:
- Solo observa. No decide nada, no cambia ninguna señal ni ninguna orden.
- No inventa valores: si un dato no está disponible, se registra como None.
- No toca strategy.py / ensemble.py / risk_manager_avanzado.py / broker_alpaca.py.
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


SESSIONS_DIR = Path("logs") / "sessions"


def new_session_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"session_{ts}_{uuid.uuid4().hex[:8]}"


def get_git_commit() -> Optional[str]:
    """Best-effort: hash corto del commit actual, o None si no se puede
    determinar (no es un repo git, git no está en PATH, etc.). Nunca lanza."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


# Mapeo de texto humano (RiskDecision.reason / guard strings de
# risk_manager_avanzado.py) a códigos estructurados. Si el texto no calza con
# ningún patrón conocido, se cae a UNCLASSIFIED — nunca se inventa un código.
_RISK_REASON_PATTERNS = [
    (re.compile(r"^RR\s+([\d.]+)\s*<\s*min\s+([\d.]+)"), "RR_BELOW_MINIMUM"),
    (re.compile(r"^Liquidez insuficiente\s*\(\$?([\d.,]+)\s*<\s*\$?([\d.,]+)\)"), "LIQUIDITY_BELOW_MINIMUM"),
    (re.compile(r"^Max posiciones"), "MAX_POSITIONS_REACHED"),
    (re.compile(r"^Max por símbolo"), "MAX_POSITIONS_PER_SYMBOL_REACHED"),
    (re.compile(r"^Límite de pérdida diaria"), "DAILY_LOSS_LIMIT_HIT"),
    (re.compile(r"^Racha negativa"), "MAX_CONSECUTIVE_LOSSES_HIT"),
    (re.compile(r"^Calor de portafolio"), "PORTFOLIO_HEAT_EXCEEDED"),
    (re.compile(r"^Apalancamiento"), "LEVERAGE_EXCEEDED"),
    (re.compile(r"^Exposición por símbolo"), "SYMBOL_EXPOSURE_EXCEEDED"),
    (re.compile(r"^Riesgo por acción inválido"), "INVALID_RISK_PER_SHARE"),
    (re.compile(r"^Qty calculada = 0"), "ZERO_QTY"),
    (re.compile(r"^OK$"), "ACCEPTED"),
]


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def classify_risk_reason(reason: str) -> Dict[str, Any]:
    """
    Traduce el reason humano de RiskDecision a un código estructurado más los
    valores numéricos que ya estaban en el texto (no se inventa nada: si el
    patrón no calza, code=UNCLASSIFIED y los valores quedan en None).

    Devuelve: {"reason_code": str, "risk_reward": float|None, "min_risk_reward": float|None,
               "liquidity": float|None, "min_liquidity": float|None}
    """
    out: Dict[str, Any] = {
        "reason_code": "UNCLASSIFIED",
        "risk_reward": None,
        "min_risk_reward": None,
        "liquidity": None,
        "min_liquidity": None,
    }
    if not reason:
        return out
    for pattern, code in _RISK_REASON_PATTERNS:
        m = pattern.match(reason.strip())
        if not m:
            continue
        out["reason_code"] = code
        if code == "RR_BELOW_MINIMUM":
            out["risk_reward"] = _to_float(m.group(1))
            out["min_risk_reward"] = _to_float(m.group(2))
        elif code == "LIQUIDITY_BELOW_MINIMUM":
            out["liquidity"] = _to_float(m.group(1))
            out["min_liquidity"] = _to_float(m.group(2))
        return out
    return out


def _json_default(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "value"):  # Enums (Side.LONG -> "LONG")
        return obj.value
    return str(obj)


class SessionLogger:
    """
    Un archivo JSONL por sesión de bot. Cada write() es un objeto JSON
    independiente en su propia línea, con flush inmediato: si el proceso
    muere a mitad de sesión, todo lo escrito hasta ese punto sigue siendo
    JSON válido línea por línea (no se pierde ni se corrompe el resto).
    """

    def __init__(self, session_id: Optional[str] = None, directory: Optional[Path] = None):
        self.session_id = session_id or new_session_id()
        self.directory = directory or SESSIONS_DIR
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{self.session_id}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        self._seq = 0

    def _write(self, event_type: str, fields: Dict[str, Any]) -> None:
        self._seq += 1
        record = {
            "observation_id": self._seq,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "event_type": event_type,
        }
        record.update(fields)
        self._fh.write(json.dumps(record, ensure_ascii=False, default=_json_default))
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    # ---------------- eventos de sesión ----------------
    def session_start(self, config: Dict[str, Any]) -> None:
        self._write("session_start", {"config": config, "git_commit": get_git_commit()})

    def session_end(self, reason: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        fields: Dict[str, Any] = {"reason": reason}
        if extra:
            fields.update(extra)
        self._write("session_end", fields)

    # ---------------- eventos de estrategia/ensemble ----------------
    def strategy_evaluation(
        self,
        symbol: str,
        timeframe: str,
        bar_timestamp: Optional[str],
        bar_close: Optional[float],
        strategy: str,
        signal: Optional[str],
        raw_signal: Optional[str],
        reason: str,
        warmup_ok: bool,
        gated_by_regime: bool = False,
        indicator_values: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._write("strategy_evaluation", {
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_timestamp": bar_timestamp,
            "bar_close": bar_close,
            "strategy": strategy,
            "signal": signal,
            "raw_signal": raw_signal,
            "reason": reason,
            "warmup_ok": warmup_ok,
            "gated_by_regime": gated_by_regime,
            "indicator_values": indicator_values or {},
        })

    def ensemble_decision(
        self,
        symbol: str,
        timeframe: str,
        bar_timestamp: Optional[str],
        bar_close: Optional[float],
        ensemble_mode: str,
        signal: Optional[str],
        votes: Dict[str, int],
        score: float,
        threshold: float,
        reason: str,
        any_warmup_pending: bool = False,
    ) -> None:
        self._write("ensemble_decision", {
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_timestamp": bar_timestamp,
            "bar_close": bar_close,
            "ensemble_mode": ensemble_mode,
            "signal": signal,
            "votes": votes,
            "score": score,
            "threshold": threshold,
            "reason": reason,
            "any_warmup_pending": any_warmup_pending,
        })

    # ---------------- riesgo ----------------
    def risk_evaluation(
        self,
        symbol: str,
        side: Optional[str],
        signal: Optional[str],
        decision: str,  # "ACCEPT" / "REJECT"
        reason: str,
        entry_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        take_profit: Optional[float] = None,
        position_size: Optional[float] = None,
        atr: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        classified = classify_risk_reason(reason)
        fields: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "signal": signal,
            "decision": decision,
            "reason": reason,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "take_profit": take_profit,
            "position_size": position_size,
            "atr": atr,
        }
        fields.update(classified)
        if extra:
            fields.update(extra)
        self._write("risk_evaluation", fields)

    # ---------------- ejecución ----------------
    def order_submission(
        self,
        symbol: str,
        side: str,
        requested_qty: float,
        order_type: str = "market",
    ) -> None:
        self._write("order_submission", {
            "symbol": symbol,
            "side": side,
            "requested_qty": requested_qty,
            "order_type": order_type,
        })

    def order_result(self, symbol: str, order: Optional[Dict[str, Any]]) -> None:
        order = order or {}
        self._write("order_result", {
            "symbol": symbol,
            "order_id": order.get("id"),
            "side": order.get("side"),
            "status": order.get("status"),
            "order_type": order.get("type"),
            "submitted_at": order.get("submitted_at"),
            "filled_at": order.get("filled_at"),
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
        })

    # ---------------- gestión de posición ----------------
    def position_management(
        self,
        symbol: str,
        action: str,  # "trailing_stop_update" / "break_even" / "scale_out" / "giveback_close" / "exit"
        details: Dict[str, Any],
    ) -> None:
        fields: Dict[str, Any] = {"symbol": symbol, "action": action}
        fields.update(details)
        self._write("position_management", fields)
