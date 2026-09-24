# src/research_protocol.py
"""
Protocolo de investigación (splits cronológicos + reglas de higiene).
Solo lectura de un JSON (config/research_protocol_v1.json); no toca trading.

Roles y usos permitidos
-----------------------
- data_support:  warm-up de indicadores; nunca se reportan resultados.
- development:   formar/ajustar hipótesis; benchmark.
- validation:    SOLO versiones congeladas (una mirada por versión); benchmark.
- contaminated:  período ya examinado (diagnóstico, referencia de benchmark).
                 NUNCA es validación ni out-of-sample intacto.
- forward:       observaciones paper/live cronológicas.

require_use() hace cumplir esto: p. ej. pedir "validation" sobre el período
contaminado lanza ProtocolError en vez de dejar pasar el error en silencio.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PROTOCOL_PATH = Path("config") / "research_protocol_v1.json"

ROLES = ("data_support", "development", "validation", "contaminated", "forward")
ALLOWED_USES = {
    "data_support": {"warmup"},
    "development": {"hypothesis_formation", "tuning", "benchmark"},
    "validation": {"frozen_validation", "benchmark"},
    "contaminated": {"diagnostics", "benchmark"},
    "forward": {"forward_observation"},
}
EVIDENCE_LABELS = {
    "data_support": "DATA SUPPORT (no evidence)",
    "development": "DEVELOPMENT EVIDENCE",
    "validation": "VALIDATION EVIDENCE",
    "contaminated": "KNOWN/CONTAMINATED EVIDENCE",
    "forward": "FORWARD EVIDENCE",
}
SINGLE_ROLES = ("development", "validation", "forward")


class ProtocolError(ValueError):
    """Protocolo inválido o uso prohibido de un split."""


def _d(value: Optional[str], where: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise ProtocolError(f"{where}: fecha inválida {value!r}") from e


def validate_protocol(p: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("protocol_version", "created", "universe", "data_source", "splits", "execution_defaults",
                "benchmark_strategy", "hygiene_rules"):
        if key not in p:
            raise ProtocolError(f"falta la clave '{key}'")
    syms = p["universe"].get("symbols") or []
    if not syms or len(set(syms)) != len(syms):
        raise ProtocolError("universe.symbols vacío o con duplicados")
    splits = p["splits"]
    names = [s.get("name") for s in splits]
    if len(set(names)) != len(names) or not all(names):
        raise ProtocolError("nombres de split vacíos o repetidos")
    prev_end: Optional[date] = None
    for s in splits:
        where = f"split '{s['name']}'"
        if s.get("role") not in ROLES:
            raise ProtocolError(f"{where}: rol desconocido {s.get('role')!r}")
        start, end = _d(s.get("start"), where), _d(s.get("end"), where)
        if start is None:
            raise ProtocolError(f"{where}: falta start")
        if end is None and s["role"] != "forward":
            raise ProtocolError(f"{where}: solo forward puede no tener fin")
        if end is not None and end < start:
            raise ProtocolError(f"{where}: end < start")
        if prev_end is None and s is not splits[0]:
            raise ProtocolError(f"{where}: aparece después de un split abierto (forward debe ser el último)")
        if prev_end is not None and start <= prev_end:
            raise ProtocolError(f"{where}: no es cronológico o se solapa con el split anterior")
        prev_end = end
    for role in SINGLE_ROLES:
        if sum(1 for s in splits if s["role"] == role) != 1:
            raise ProtocolError(f"debe haber exactamente un split con rol '{role}'")
    if splits[-1]["role"] != "forward":
        raise ProtocolError("el último split debe ser forward")
    return p


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> Dict[str, Any]:
    return validate_protocol(json.loads(Path(path).read_text(encoding="utf-8")))


def get_split(p: Dict[str, Any], name: str) -> Dict[str, Any]:
    for s in p["splits"]:
        if s["name"] == name:
            return s
    raise ProtocolError(f"split desconocido '{name}' (hay: {[s['name'] for s in p['splits']]})")


def evidence_label(split: Dict[str, Any]) -> str:
    return EVIDENCE_LABELS[split["role"]]


def require_use(p: Dict[str, Any], name: str, use: str) -> Dict[str, Any]:
    """Devuelve el split si `use` está permitido para su rol; si no, ProtocolError."""
    s = get_split(p, name)
    if use not in ALLOWED_USES[s["role"]]:
        raise ProtocolError(f"el split '{name}' (rol {s['role']}) no puede usarse para '{use}'; "
                            f"usos permitidos: {sorted(ALLOWED_USES[s['role']])}")
    return s


def contaminated_overlap(p: Dict[str, Any], start: str, end: str) -> List[str]:
    """Nombres de splits contaminados que se solapan con [start, end] (fechas NY inclusivas)."""
    a, b = _d(start, "rango"), _d(end, "rango")
    out = []
    for s in p["splits"]:
        if s["role"] != "contaminated":
            continue
        cs, ce = _d(s["start"], s["name"]), _d(s["end"], s["name"])
        if a <= ce and cs <= b:
            out.append(s["name"])
    return out
