# src/preportfolio_screen.py
"""
Research Protocol V2 — herramientas reutilizables de ADMISIÓN PRE-PORTAFOLIO (solo investigación).
Documento: research/research_protocol_v2.md; config: config/research_protocol_v2.json (decisiones Q1–Q7 cerradas).

No implementa ninguna hipótesis. Provee:
- carga/validación del protocolo V2 y guardas de ventana (DEVELOPMENT para estrategia, KNOWN para feeds; nunca
  Validation ni Forward; sin bypass);
- evaluadores de P1–P6 con resultado PASS / FAIL / NOT_APPLICABLE por compuerta y la decisión de admisión;
- una guarda que impide el backtest de portafolio completo si la admisión no es PRE_PORTFOLIO_PASS.
Reutiliza los ayudantes congelados de Research Sanity Audit V1 (pools/réplicas emparejadas, percentil empírico,
métricas sombra de horizonte fijo, simulador aislado, interpolación de equilibrio).

Convención de percentil (congelada; P2 y P4): ver PERCENTILE_METHOD.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from .research_protocol import load_protocol
from .research_sanity_audit import (AuditHygieneError, break_even, build_pools, check_window, draw_replicate,  # noqa: F401
                                    forward_row, isolated_stats, percentile_of, run_isolated)

DEFAULT_PROTOCOL_V2 = Path("config") / "research_protocol_v2.json"
DEFAULT_MIN_SIGNALS = 300
PRIMARY_PERCENTILE = 95.0
MIN_SUPPORTING = 2
FRICTION_SHARE = 0.25
FEED_BANDS = ((0.90, "HIGH_STABILITY"), (0.80, "ACCEPTABLE_MODERATE"), (0.75, "CAUTION"))
FEED_FAIL_BELOW = 0.75
FEED_EXCEPTION_FIELDS = ("why_sensitivity_expected", "authoritative_feed", "why_operationally_meaningful")
PASS, FAIL, NOT_APPLICABLE = "PASS", "FAIL", "NOT_APPLICABLE"
STATUS_PASS = "PRE_PORTFOLIO_PASS"
STATUS_UMBRELLA = "REJECTED_PRE_PORTFOLIO"
STATUS_BY_GATE = {"P1": "REJECTED_AT_RAW_SIGNAL_SCREEN", "P2": "REJECTED_AT_RAW_SIGNAL_SCREEN",
                  "P3": "REJECTED_AT_RAW_SIGNAL_SCREEN", "P4": "REJECTED_AT_ECONOMIC_SCALE",
                  "P5": "REJECTED_AT_FEED_ROBUSTNESS", "P6": STATUS_UMBRELLA}
REQUIRED_GATES = ("P1", "P2", "P3", "P4", "P5", "P6")
PERCENTILE_METHOD = ("empirical percentile = (count(random < real) + 0.5 * count(random == real)) / n * 100, over the "
                     "non-null replicate values (n = their count); exact float comparison, no interpolation; for a "
                     "lower-is-better metric the oriented percentile is 100 - that value; a missing real value or zero "
                     "replicates gives no percentile (the gate is then FAIL)")


class PortfolioAdmissionError(RuntimeError):
    """Se intentó un backtest de portafolio completo sin PRE_PORTFOLIO_PASS."""


def load_protocol_v2(path: Path = DEFAULT_PROTOCOL_V2) -> Dict[str, Any]:
    p = load_protocol(path)
    if p.get("protocol_version") != "research_protocol_v2" or "pre_portfolio_gates" not in p:
        raise ValueError(f"{path} no es un protocolo V2")
    return p


def gate(outcome: str, value: Any, rule: str, **detail) -> Dict[str, Any]:
    if outcome not in (PASS, FAIL, NOT_APPLICABLE):
        raise ValueError(outcome)
    return {"outcome": outcome, "passed": outcome == PASS, "value": value, "rule": rule, **detail}


def _pf(ok: Optional[bool]) -> str:
    return PASS if ok is True else FAIL          # no medido (None) == FAIL


# ================================================================ P1
def gate_p1(n_valid_forward_observations: int, preregistered_minimum: Optional[int] = None,
            reason: Optional[str] = None) -> Dict[str, Any]:
    """Cuenta observaciones de avance VÁLIDAS (no señales emitidas). >= 300 salvo mínimo preregistrado con razón."""
    minimum = DEFAULT_MIN_SIGNALS
    if preregistered_minimum is not None:
        if not reason:
            raise ValueError("un mínimo P1 distinto requiere una razón preregistrada")
        minimum = int(preregistered_minimum)
    n = n_valid_forward_observations
    return gate(_pf(None if n is None else n >= minimum), n, f">= {minimum} valid raw-signal forward observations",
                minimum=minimum, preregistered_override=preregistered_minimum is not None)


# ================================================================ P2 / P3
def directional_percentile(real: Optional[float], replicates: Sequence[Optional[float]],
                           higher_is_better: bool = True) -> Optional[float]:
    """PERCENTILE_METHOD, orientado a la dirección favorable preregistrada (100 = mejor que todas las réplicas)."""
    pct = percentile_of(real, list(replicates))
    if pct is None:
        return None
    return pct if higher_is_better else 100.0 - pct


def gate_p2(name: str, real: Optional[float], replicates: Sequence[Optional[float]], higher_is_better: bool = True,
            threshold: float = PRIMARY_PERCENTILE) -> Dict[str, Any]:
    pct = directional_percentile(real, replicates, higher_is_better)
    return gate(_pf(None if pct is None else pct >= threshold), pct,
                f"primary '{name}' oriented percentile >= {threshold} vs matched random (0 bps)",
                metric=name, real=real, higher_is_better=higher_is_better,
                replicates=sum(x is not None for x in replicates), percentile_method=PERCENTILE_METHOD)


def gate_p3(supporting: Sequence[Dict[str, Any]], minimum: int = MIN_SUPPORTING) -> Dict[str, Any]:
    """Cada métrica {'name','real','replicates','higher_is_better'}: cuenta si real es ESTRICTAMENTE mejor que la
    mediana de las réplicas en su dirección favorable preregistrada (dirección-consciente)."""
    rows = []
    for m in supporting:
        reps = [float(x) for x in m["replicates"] if x is not None]
        med = statistics.median(reps) if reps else None
        ok = None if med is None or m["real"] is None else (m["real"] > med if m["higher_is_better"] else m["real"] < med)
        rows.append({"name": m["name"], "real": m["real"], "random_median": med, "higher_is_better": m["higher_is_better"],
                     "expected_direction": bool(ok)})
    n_ok = sum(r["expected_direction"] for r in rows)
    return gate(_pf(n_ok >= minimum), n_ok, f">= {minimum} supporting metrics strictly better than the random median "
                                             "in their preregistered favorable direction", metrics=rows)


# ================================================================ P4
def direction_normalized(value: Optional[float], side: str) -> Optional[float]:
    """Retorno en la dirección de la señal: long = +valor; short = −valor."""
    if value is None:
        return None
    if side not in ("long", "short"):
        raise ValueError(f"side desconocido {side!r}")
    return value if side == "long" else -value


def median_successful_favorable_move_pct(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    """
    Q2: éxito = retorno dirección-normalizado en el horizonte PRIMARIO > 0 a 0 bps. Cada fila:
    {'side': 'long'|'short', 'return_0bps': retorno de precio (marco long) en el horizonte primario,
     'favorable_move_pct': MFE % en la dirección de la señal en el horizonte primario (>= 0)}.
    Devuelve la mediana de favorable_move_pct entre los éxitos (None si no hay).
    """
    succ = [r["favorable_move_pct"] for r in rows
            if r.get("return_0bps") is not None and r.get("favorable_move_pct") is not None
            and direction_normalized(r["return_0bps"], r["side"]) > 0]
    return statistics.median(succ) if succ else None


def gate_p4(roundtrip_friction_pct: Optional[float], median_success_favorable_move_pct: Optional[float],
            primary_name: str, real_primary_canonical: Optional[float],
            random_primary_canonical: Sequence[Optional[float]], higher_is_better: bool = True,
            primary_is_signed_return: bool = False, threshold: float = PRIMARY_PERCENTILE,
            friction_share: float = FRICTION_SHARE) -> Dict[str, Any]:
    """
    FAIL solo si A Y B:
    A: fricción ida-vuelta % >= 25% de la mediana del movimiento favorable dirección-normalizado de los éxitos.
    B: recomputando real Y los MISMOS controles aleatorios congelados a costo canónico, el métrico PRIMARIO real ya no
       cumple el mismo requisito de percentil de P2 en su dirección preregistrada.
    Si el primario es un retorno/expectativa con signo, se reporta además (descriptivo) si queda <= 0.
    """
    if roundtrip_friction_pct is None:
        return gate(FAIL, None, "roundtrip friction not measured", condition_a=None, condition_b=None)
    if median_success_favorable_move_pct is None or median_success_favorable_move_pct <= 0:
        share, a = None, True            # sin movimiento favorable medible: la fricción domina
    else:
        share = roundtrip_friction_pct / median_success_favorable_move_pct
        a = share >= friction_share
    pct = directional_percentile(real_primary_canonical, random_primary_canonical, higher_is_better)
    b = pct is None or pct < threshold
    detail = {"condition_a": bool(a), "condition_b": bool(b), "friction_over_favorable_move": share,
              "canonical_cost_primary_percentile": pct, "primary_metric": primary_name,
              "percentile_method": PERCENTILE_METHOD}
    if primary_is_signed_return:
        detail["descriptive_primary_nonpositive_at_canonical_cost"] = (None if real_primary_canonical is None
                                                                      else real_primary_canonical <= 0)
    return gate(_pf(not (a and b)), share,
                f"FAIL iff friction/median successful favorable move >= {friction_share} AND the primary metric's "
                f"canonical-cost oriented percentile vs the same frozen random controls < {threshold}", **detail)


# ================================================================ P5
def feed_band(jaccard: Optional[float]) -> Optional[str]:
    if jaccard is None:
        return None
    for lo, name in FEED_BANDS:
        if jaccard >= lo:
            return name
    return "MATERIAL_FEED_SENSITIVITY"


def p5_applicable(primary_research_feed: str, intended_live_signal_feed: str, threshold_sensitive: bool,
                  secondary_feed_available: bool) -> bool:
    """Q1: obligatorio si los feeds de investigación y vivo difieren; si coinciden, aplica a reglas sensibles a
    umbrales cuando hay un feed secundario disponible. Derivado SOLO de las declaraciones congeladas."""
    return primary_research_feed != intended_live_signal_feed or bool(threshold_sensitive and secondary_feed_available)


def gate_p5(primary_research_feed: str, intended_live_signal_feed: str, threshold_sensitive: bool,
            secondary_feed_available: bool, jaccard: Optional[float] = None,
            mismatch_justification: Optional[str] = None, operationally_authoritative_feed: Optional[str] = None,
            preregistered_exception: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    decl = {"primary_research_feed": primary_research_feed, "intended_live_signal_feed": intended_live_signal_feed,
            "threshold_sensitive": threshold_sensitive, "secondary_feed_available": secondary_feed_available}
    if not p5_applicable(primary_research_feed, intended_live_signal_feed, threshold_sensitive, secondary_feed_available):
        return gate(NOT_APPLICABLE, None, "not applicable under V2: research feed == intended live feed and "
                                          "(rule not threshold-sensitive or no secondary feed available)", **decl)
    mismatch = primary_research_feed != intended_live_signal_feed
    if mismatch and not (mismatch_justification and
                         operationally_authoritative_feed in (primary_research_feed, intended_live_signal_feed)):
        return gate(FAIL, jaccard, "research/live feed mismatch not preregistered with justification and "
                                   "operationally authoritative feed", feed_mismatch=True, **decl)
    if jaccard is None:
        return gate(FAIL, None, "feed robustness applicable but not measured", feed_mismatch=mismatch, **decl)
    exc_ok = bool(preregistered_exception) and all((preregistered_exception or {}).get(k) for k in FEED_EXCEPTION_FIELDS)
    ok = jaccard >= FEED_FAIL_BELOW or exc_ok
    return gate(_pf(ok), jaccard, f"Jaccard >= {FEED_FAIL_BELOW} unless a complete preregistered exception exists",
                band=feed_band(jaccard), feed_mismatch=mismatch,
                exception_used=bool(jaccard < FEED_FAIL_BELOW and exc_ok), **decl)


# ================================================================ P6
def gate_p6(data_integrity: Optional[bool], no_lookahead: Optional[bool],
            deterministic_reproduction: Optional[bool]) -> Dict[str, Any]:
    ok = data_integrity is True and no_lookahead is True and deterministic_reproduction is True
    return gate(_pf(ok), ok, "integrity, no-look-ahead and deterministic reproduction all PASS",
                data_integrity=data_integrity, no_lookahead=no_lookahead, deterministic_reproduction=deterministic_reproduction)


# ================================================================ admisión
def _accepted(name: str, g: Dict[str, Any]) -> bool:
    if g.get("outcome") == PASS:
        return True
    # NOT_APPLICABLE solo vale para P5 y solo si gate_p5 lo derivó de las declaraciones congeladas
    return (name == "P5" and g.get("outcome") == NOT_APPLICABLE and "primary_research_feed" in g
            and not p5_applicable(g["primary_research_feed"], g["intended_live_signal_feed"],
                                  g["threshold_sensitive"], g["secondary_feed_available"]))


def admission(hypothesis_id: str, gates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Q4: estado más específico cuando la causa es UNA etapa identificable; REJECTED_PRE_PORTFOLIO si falla P6 o
    fallan compuertas de más de una etapa. Todo rechazo lleva pre_portfolio = REJECTED_PRE_PORTFOLIO y el resultado
    PASS/FAIL/NOT_APPLICABLE de P1–P6. Una compuerta obligatoria no medida es FAIL.
    """
    missing = [g for g in REQUIRED_GATES if g not in gates]
    if missing:
        raise ValueError(f"faltan compuertas {missing}")
    failed = [g for g in REQUIRED_GATES if not _accepted(g, gates[g])]
    outcomes = {g: (gates[g].get("outcome") if _accepted(g, gates[g]) else FAIL) for g in REQUIRED_GATES}
    stages = sorted({STATUS_BY_GATE[g] for g in failed})
    if not failed:
        status = STATUS_PASS
    elif "P6" in failed or len(stages) > 1:
        status = STATUS_UMBRELLA
    else:
        status = stages[0]
    return {"hypothesis_id": hypothesis_id, "gates": {g: gates[g] for g in REQUIRED_GATES}, "gate_outcomes": outcomes,
            "failed_gates": failed, "status": status, "pre_portfolio": STATUS_PASS if not failed else STATUS_UMBRELLA,
            "full_portfolio_allowed": not failed, "percentile_method": PERCENTILE_METHOD}


def require_admitted(adm: Dict[str, Any]) -> None:
    ok = (adm.get("status") == STATUS_PASS and adm.get("pre_portfolio") == STATUS_PASS and adm.get("full_portfolio_allowed")
          and not adm.get("failed_gates") and all(v in (PASS, NOT_APPLICABLE) for v in (adm.get("gate_outcomes") or {}).values())
          and set(adm.get("gate_outcomes") or {}) == set(REQUIRED_GATES))
    if not ok:
        raise PortfolioAdmissionError(f"{adm.get('hypothesis_id')}: {adm.get('status')} — no full portfolio backtest")


def run_full_development_if_admitted(adm: Dict[str, Any], runner: Callable[..., Any], *args, **kwargs) -> Any:
    """Único camino de la herramienta hacia un backtest de portafolio completo: exige PRE_PORTFOLIO_PASS."""
    require_admitted(adm)
    return runner(*args, **kwargs)


def write_admission(adm: Dict[str, Any], path: Path) -> Path:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(adm, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return Path(path)
