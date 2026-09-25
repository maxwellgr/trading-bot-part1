# src/research_h004.py
"""
Corrida de investigación de STRATEGY_V2_HYPOTHESIS_004 (solo backtest, sin red).

    python -m src.research_h004 --split development --output-dir data/research_v1/h004_development

Orden obligatorio (spec §16–§23, decisiones Q1–Q8):
1. Guardas: development permitido; validation solo FROZEN + compuerta D aprobada + nunca vista; known/forward
   siempre rechazados. Sin flag de bypass.
2. Contexto SPY: solo el archivo auditado (checksum == research/context_manifest_spy_v1.json, readiness PASS);
   15Min RTH; exactamente 200 velas de soporte + split. Nunca SPY posterior a 2025-12-31.
3. Chequeo previo: H003 congelado reproducido (compuerta desactivada, uso interno) byte a byte contra la corrida
   guardada de H003 DEVELOPMENT. Si falla, se aborta H004.
4. Corrida oficial con la compuerta activa; auditoría de alineación temporal (cualquier violación falla).
5. Reporte: embudo, métricas, salidas, símbolos, D1–D6, fallas inmediatas, descomposición A/B/B′/C,
   diagnóstico sombra (solo lectura), diagnósticos de régimen. Descriptivo; nada modifica H004.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from . import spy_context_data as scd
from .backtest_engine import BacktestConfig, BacktestEngine, production_args
from .backtest_report import _profit_factor, summarize, to_json, write_outputs
from .h001_autopsy import TIME_BUCKETS, load_development_bars, time_bucket
from .h001_opportunity_autopsy import HORIZONS, reconstruct_risk_ps, shadow_fixed_horizon
from .historical_audit import sha256_file
from .historical_data import symbol_path, validate_bars
from .research_benchmark import _git_state
from .research_h003 import exit_report, symbol_report
from .research_protocol import DEFAULT_PROTOCOL_PATH, get_split, load_protocol
from .risk_manager_avanzado import RiskManager
from .run_paper import build_risk_config
from .shared_management_autopsy import verify_identical
from .strategy_v2_h001 import ENGINE_LOOKBACK, HygieneError, check_split_allowed, development_gates
from .strategy_v2_h004 import (BLOCKED, FROZEN_SPEC_COMMIT, HYPOTHESIS_ID, POSITIVE, UNAVAILABLE, AlignmentError,
                               RegimeGatedH004, SpyRegimeContext, _parent_reproduction_strategy)

NY = "America/New_York"
DEFAULT_REGISTRY = Path("research") / "strategy_registry_v1.json"
DEFAULT_SPY_DIR = scd.DEFAULT_SPY_DIR
DEFAULT_SPY_MANIFEST = scd.DEFAULT_MANIFEST
DEFAULT_STORED_H003 = Path("data") / "research_v1" / "h003_development"
PROTECTED_OUTPUTS = [Path("data") / "research_v1" / f"h00{k}_development" for k in (1, 2, 3)]
IMMEDIATE_FAIL_R = 0.25
WEAK_FORWARD_MFE_R = 0.25
TRUNCATED_SESSION = "2024-12-23"


class ReproductionError(RuntimeError):
    """El chequeo previo de reproducción de H003 falló: H004 no se ejecuta."""


class ContextError(RuntimeError):
    """El contexto SPY no es el auditado/permitido."""


def registry_entry(path: Path = DEFAULT_REGISTRY) -> Dict[str, Any]:
    reg = json.loads(Path(path).read_text(encoding="utf-8"))
    e = next(x for x in reg["entries"] if x["id"] == HYPOTHESIS_ID)
    if (e.get("spec_frozen") or {}).get("frozen_spec_commit") != FROZEN_SPEC_COMMIT:
        raise HygieneError(f"el registro no apunta al spec congelado {FROZEN_SPEC_COMMIT}")
    return e


def _guard_out_dir(out_dir: Path) -> None:
    for p in PROTECTED_OUTPUTS:
        if Path(out_dir).resolve() == p.resolve():
            raise HygieneError(f"{out_dir} es una salida guardada protegida; no se sobrescribe")


# ================================================================ contexto SPY
def build_context(b15: pd.DataFrame, split: Dict[str, Any], expected_dates: Sequence[date]) -> Dict[str, Any]:
    """200 velas 15Min de soporte + split (misma convención de corte que las acciones); banderas de pendiente."""
    s_utc = pd.Timestamp(split["start"]).tz_localize(NY).tz_convert("UTC")
    e_utc = (pd.Timestamp(split["end"]) + pd.Timedelta(days=1)).tz_localize(NY).tz_convert("UTC")
    before = b15[b15.index < s_utc]
    support = before.iloc[-scd.SUPPORT_BARS:]
    if len(support) < scd.SUPPORT_BARS:
        raise ContextError(f"solo {len(support)} velas SPY 15Min de soporte (se requieren {scd.SUPPORT_BARS})")
    inside = b15[(b15.index >= s_utc) & (b15.index < e_utc)]
    if (b15.index >= e_utc).any():
        raise ContextError("hay velas SPY posteriores al fin del split")
    series = pd.concat([support, inside])
    flags_full = pd.Series(scd.slope_gap_flags(b15, expected_dates), index=b15.index)
    flags = flags_full.reindex(series.index).to_numpy()
    ctx = SpyRegimeContext(series[["open", "high", "low", "close", "volume"]], slope_gap=flags)
    bucket_level = scd.slope_windows_spanning_missing(b15, expected_dates,
                                                      date.fromisoformat(split["start"]), date.fromisoformat(split["end"]))
    return {"context": ctx, "support_bars": int(len(support)), "support_first": support.index[0].isoformat(),
            "support_last": support.index[-1].isoformat(), "split_bars": int(len(inside)),
            "bucket_level_slope_gap_windows": bucket_level["spy_slope_windows_spanning_missing_bucket"]}


def load_spy_context(spy_dir: Path, manifest_path: Path, split: Dict[str, Any],
                     stock_bars: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """Solo el SPY auditado: readiness PASS, checksum idéntico, spec congelado, rango <= 2025-12-31."""
    if date.fromisoformat(split["end"]) > scd.MAX_SPY_DATE:
        raise ContextError("el contexto SPY auditado solo cubre development; SPY de validación sigue sin abrir")
    man = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if man.get("h004_data_readiness") != "PASS" or man.get("frozen_spec_commit") != FROZEN_SPEC_COMMIT:
        raise ContextError("el manifiesto SPY no está en PASS para el spec congelado de H004")
    if man["source"]["feed"] != "iex" or man["source"]["adjustment"] != "raw":
        raise ContextError("fuente SPY no permitida")
    path = symbol_path(spy_dir, "1Min", scd.CONTEXT_SYMBOL)
    got = sha256_file(path)
    if got != man["raw_file"]["sha256"]:
        raise ContextError(f"checksum SPY {got} != manifiesto {man['raw_file']['sha256']}")
    df1 = validate_bars(pd.read_csv(path, dtype={"timestamp": str, "symbol": str}), scd.CONTEXT_SYMBOL, str(path))
    b15 = scd.resample_rth(df1)
    req = {date.fromisoformat(d) for d in man["required_sessions"]["required_session_dates"]}
    stock_dates = {d for df in stock_bars.values() for d in df.index.tz_convert(NY).date}
    out = build_context(b15, split, sorted(req | stock_dates))
    out["manifest"] = {"path": str(manifest_path).replace("\\", "/"), "sha256_spy_file": got,
                       "h004_data_readiness": man["h004_data_readiness"],
                       "required_session_dates_sha256": man["required_sessions"]["required_session_dates_sha256"],
                       "manifest_bucket_level_slope_gap_windows":
                           man["diagnostics_only"]["spy_slope_windows_spanning_missing_bucket"]}
    return out


# ================================================================ motor
def _config(protocol: Dict[str, Any], split: Dict[str, Any]) -> BacktestConfig:
    ex = protocol["execution_defaults"]
    return BacktestConfig(symbols=list(protocol["universe"]["symbols"]), timeframe="5Min", start=split["start"],
                          end=split["end"], initial_equity=float(ex["initial_equity"]),
                          slippage_bps=float(ex["slippage_bps"]), commission=float(ex["commission_per_fill"]),
                          record_evaluations=True, window_hours_limit=False)


def run_engine(protocol, split, bars, strategy):
    return BacktestEngine(_config(protocol, split), bars, production_args({"lookback": ENGINE_LOOKBACK}), strategy).run()


def reproduce_h003(protocol, split, bars, stored_h003: Path, check_dir: Path) -> Dict[str, Any]:
    """Chequeo previo interno (Q7): compuerta desactivada == H003 congelado, byte a byte."""
    result = run_engine(protocol, split, bars, _parent_reproduction_strategy())
    rep = verify_identical(result, Path(check_dir), Path(stored_h003))
    if not rep["identical"]:
        raise ReproductionError(f"H003 no se reproduce byte a byte: {rep['files_identical']}")
    return rep


def gate_rows(result, strategy: RegimeGatedH004) -> List[Dict[str, Any]]:
    """Une cada decisión de compuerta con la evaluación del motor (símbolo) por índice de llamada."""
    rows = []
    for g in strategy.gate_log:
        ev = result.evaluations[g["call_index"]]
        if ev["bar_timestamp"] != g["bar_timestamp"]:
            raise AlignmentError(f"registro de compuerta desalineado en la llamada {g['call_index']}")
        rows.append({"symbol": ev["symbol"], **{k: v for k, v in g.items() if k != "call_index"}})
    return rows


def alignment_audit(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """§16/§22: una fila por señal cruda; cualquier cubeta parcial/futura o distinta de R* falla la corrida."""
    bad = []
    for r in rows:
        dec = pd.Timestamp(r["decision_time"])
        r["stock_decision_time"] = r["decision_time"]
        exp_end = pd.Timestamp(r["expected_spy_bucket_end"])
        ok = exp_end <= dec
        if r["spy_bar_start"] is not None:
            ok &= r["spy_bar_start"] == r["expected_spy_bucket_start"] and pd.Timestamp(r["spy_bar_end"]) <= dec
        else:
            ok &= r["status"] == UNAVAILABLE
        r["spy_bar_completed_at_decision"] = bool(ok)
        if pd.Timestamp(r["bar_timestamp"]) + pd.Timedelta(minutes=5) != dec:
            ok = False
        if not ok:
            bad.append(r)
    if bad:
        raise AlignmentError(f"{len(bad)} violaciones de alineación temporal, p. ej. {bad[0]}")
    return {"rows": len(rows), "violations": 0, "pass": True}


def run_split(protocol: Dict[str, Any], split_name: str, data_dir: Path, entry: Dict[str, Any],
              spy_dir: Path, spy_manifest: Path, stored_h003: Path, check_dir: Path,
              dev_gate_passed: Optional[bool] = None, context_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    split = get_split(protocol, split_name)
    check_split_allowed(entry, split, dev_gate_passed)
    loaded = load_development_bars(data_dir, list(protocol["universe"]["symbols"]), split)
    ctx = context_override or load_spy_context(spy_dir, spy_manifest, split, loaded["bars"])
    repro = reproduce_h003(protocol, split, loaded["bars"], stored_h003, check_dir)
    strategy = RegimeGatedH004(ctx["context"])
    result = run_engine(protocol, split, loaded["bars"], strategy)
    s_utc = pd.Timestamp(split["start"]).tz_localize(NY).tz_convert("UTC")
    early = [e["bar_timestamp"] for e in result.evaluations if pd.Timestamp(e["bar_timestamp"]) < s_utc]
    early += [ts for ts, _ in result.equity_curve if pd.Timestamp(ts) < s_utc]
    if early:
        raise HygieneError(f"actividad antes del inicio del split: {early[:3]}")
    rows = gate_rows(result, strategy)
    audit = alignment_audit(rows)
    return {"split": split, "result": result, "bars": loaded["bars"], "support": loaded["info"], "gate": rows,
            "alignment": audit, "reproduction": repro, "context": {k: v for k, v in ctx.items() if k != "context"}}


# ================================================================ reportes
def funnel(rows: List[Dict[str, Any]], result) -> Dict[str, Any]:
    c = result.counters
    n = {s: sum(r["status"] == s for r in rows) for s in (POSITIVE, BLOCKED, UNAVAILABLE)}
    raw = len(rows)
    cb = dict(c["circuit_breaker_blocked_entries"])
    dph = c["daily_profit_halt_blocked_entries"]
    acc, rej = c["risk"]["ACCEPT"], c["risk"]["REJECT"]
    engine_buys = sum(v["BUY"] for v in c["signals"].values())
    already = n[POSITIVE] - acc - rej - sum(cb.values()) - dph
    rj = dict(sorted(c["rejects_by_reason"].items()))
    out = {
        "raw_h003_buy_signals": raw,
        "market_regime_blocked": n[BLOCKED], "market_regime_unavailable": n[UNAVAILABLE],
        "market_regime_passed": n[POSITIVE],
        "identity_raw_eq_blocked_plus_unavailable_plus_passed": raw == n[BLOCKED] + n[UNAVAILABLE] + n[POSITIVE],
        "engine_buy_signals_equal_passed": engine_buys == n[POSITIVE],
        "downstream_of_passed": {
            "symbol_already_open": already, "daily_profit_halt": dph,
            "circuit_breaker": cb,
            "loss_streak_halt": sum(v for k, v in cb.items() if "Racha" in k),
            "reached_risk_manager": acc + rej,
            "rr_rejects": rj.get("RR_BELOW_MINIMUM", 0), "liquidity_rejects": rj.get("LIQUIDITY_BELOW_MINIMUM", 0),
            "leverage_rejects": rj.get("LEVERAGE_EXCEEDED", 0), "max_position_rejects": rj.get("MAX_POSITIONS_REACHED", 0),
            "rejects_by_reason": rj, "accepts": acc, "completed_trades": len(result.trades),
        },
    }
    out["identity_passed_eq_downstream"] = (n[POSITIVE] == already + dph + sum(cb.values()) + acc + rej and already >= 0)
    return out


def headline(summary: Dict[str, Any]) -> Dict[str, Any]:
    t, p = summary["trades"], summary["portfolio"]
    return {"completed_trades": t["trades"], "wins": t["wins"], "losses": t["losses"], "win_rate_pct": t["win_rate"] * 100,
            "realized_pnl": p["realized_pnl_closed_trades"], "return_pct": p["return_pct"], "expectancy": t["expectancy"],
            "expectancy_r": t["expectancy_r"], "profit_factor": t["profit_factor"], "total_r": t["total_r"],
            "max_drawdown_pct": p["max_drawdown_pct"], "max_consecutive_losses": t["max_consecutive_losses"],
            "open_positions_at_end": p["open_positions_at_end"], "unfilled_orders_at_end": p["unfilled_orders_at_end"]}


def immediate_failure(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Definición establecida: nunca alcanzó +0.25R (MFE por cierres) Y R final < 0."""
    rs = [t for t in trades if t.get("realized_r") is not None and t.get("mfe_r") is not None]
    imm = [t for t in rs if t["mfe_r"] < IMMEDIATE_FAIL_R and t["realized_r"] < 0]
    return {"trades": len(rs), "immediate_failures": len(imm),
            "rate_pct": len(imm) / len(rs) * 100 if rs else None,
            "pnl": float(sum(t["realized_pnl"] for t in imm)), "total_r": float(sum(t["realized_r"] for t in imm))}


def exit_rates(trades) -> Dict[str, Any]:
    n = len(trades)
    out = {}
    for k in ("giveback_close", "stop_hit", "take_profit_hit"):     # códigos reales de exit_reason del motor
        m = sum(t["exit_reason"] == k for t in trades)
        out[k] = {"count": m, "rate_pct": m / n * 100 if n else None}
    return out


def path_decomposition(h003_trades, h004_trades, rows) -> Dict[str, Any]:
    """Q6: A/B/B′/C por (símbolo, timestamp de la vela de señal H003 cruda); A + B + B′ = trades H003."""
    key = lambda t: (t["symbol"], t["entry_signal_timestamp"])   # noqa: E731
    k3 = {key(t): t for t in h003_trades}
    k4 = {key(t): t for t in h004_trades}
    status = {(r["symbol"], r["bar_timestamp"]): r["status"] for r in rows}
    groups: Dict[str, List[Dict[str, Any]]] = {"A": [], "B": [], "B_prime": [], "C": []}
    for k, t in k3.items():
        st = status.get(k)
        if k in k4:
            g = "A"
        elif st in (BLOCKED, UNAVAILABLE):
            g = "B"
        elif st == POSITIVE:
            g = "B_prime"
        else:
            raise AlignmentError(f"trade H003 {k} sin decisión de compuerta en H004 (señal cruda no idéntica)")
        groups[g].append({"group": g, "symbol": k[0], "signal_bar": k[1], "gate_status": st,
                          "h003_pnl": t["realized_pnl"], "h003_r": t["realized_r"],
                          "h004_pnl": k4[k]["realized_pnl"] if k in k4 else None,
                          "h004_r": k4[k]["realized_r"] if k in k4 else None})
    for k, t in k4.items():
        if k not in k3:
            groups["C"].append({"group": "C", "symbol": k[0], "signal_bar": k[1], "gate_status": status.get(k),
                                "h003_pnl": None, "h003_r": None, "h004_pnl": t["realized_pnl"], "h004_r": t["realized_r"]})

    def agg(g, col_pnl, col_r):
        xs = groups[g]
        return {"count": len(xs), "pnl": float(sum(x[col_pnl] or 0.0 for x in xs)),
                "total_r": float(sum(x[col_r] or 0.0 for x in xs))}
    summ = {"A_in_h003": agg("A", "h003_pnl", "h003_r"), "A_in_h004": agg("A", "h004_pnl", "h004_r"),
            "B": agg("B", "h003_pnl", "h003_r"), "B_prime": agg("B_prime", "h003_pnl", "h003_r"),
            "C": agg("C", "h004_pnl", "h004_r")}
    h3 = float(sum(t["realized_pnl"] for t in h003_trades))
    h4 = float(sum(t["realized_pnl"] for t in h004_trades))
    summ["identity_A_B_Bprime_eq_h003_trades"] = (len(groups["A"]) + len(groups["B"]) + len(groups["B_prime"])
                                                   == len(h003_trades))
    summ["identity_A_C_eq_h004_trades"] = len(groups["A"]) + len(groups["C"]) == len(h004_trades)
    bridge = {"h003_pnl": h3, "minus_B": -summ["B"]["pnl"], "minus_B_prime": -summ["B_prime"]["pnl"],
              "A_change_h004_minus_h003": summ["A_in_h004"]["pnl"] - summ["A_in_h003"]["pnl"],
              "plus_C": summ["C"]["pnl"], "h004_pnl": h4}
    bridge["reconciles"] = math.isclose(h3 + bridge["minus_B"] + bridge["minus_B_prime"] + bridge["A_change_h004_minus_h003"]
                                        + bridge["plus_C"], h4, abs_tol=1e-6)
    div = summ["B_prime"]["count"] + summ["C"]["count"]
    summ["bridge"] = bridge
    summ["path_divergence_trades_B_prime_plus_C"] = div
    return {"summary": summ, "rows": [r for g in ("A", "B", "B_prime", "C") for r in groups[g]]}


def stateless_rr_liquidity(risk_cfg, h, l, c, v, t: int) -> Dict[str, Any]:
    """Mismas fórmulas que RiskManager.assess_entry para RR y liquidez, sin estado de portafolio (Q5, subconjunto B)."""
    liq = RiskManager._estimate_liquidity_dollar(None, list(c[:t + 1]), list(v[:t + 1]), risk_cfg.liq_window)
    liq_ok = liq is None or liq >= risk_cfg.min_liquidity_dollar
    price = float(c[t])
    atr = RiskManager._atr(list(h[:t + 1]), list(l[:t + 1]), list(c[:t + 1]), risk_cfg.atr_window) \
        if risk_cfg.use_atr_based_stop else None
    slip = price * risk_cfg.slippage_pct
    est = price + slip
    stop = est - risk_cfg.atr_multiple_sl * atr if atr is not None else est * (1 - risk_cfg.default_sl_pct)
    tp = est + risk_cfg.atr_multiple_tp * atr if atr is not None else est * (1 + risk_cfg.default_tp_pct)
    fees = 2 * risk_cfg.fee_per_share
    rr = max(0.0, abs(tp - est) - fees - slip) / max(1e-9, abs(est - stop) + fees + slip)
    return {"stateless_liquidity": liq, "stateless_liquidity_ok": bool(liq_ok), "stateless_rr": rr,
            "stateless_rr_ok": bool(rr >= risk_cfg.min_rr), "stateless_eligible": bool(liq_ok and rr >= risk_cfg.min_rr)}


def shadow_diagnostic(rows, bars: Dict[str, pd.DataFrame], risk_evals) -> Dict[str, Any]:
    """Solo lectura (metodología validada de la autopsia de oportunidades); nunca toca el portafolio real."""
    risk_cfg = build_risk_config(production_args())
    ind = {s: {"o": df["open"].to_numpy(float), "h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
               "c": df["close"].to_numpy(float), "v": df["volume"].to_numpy(float),
               "date": np.asarray(df.index.tz_convert(NY).date), "ts": np.asarray([t.isoformat() for t in df.index]),
               "pos": {t.isoformat(): k for k, t in enumerate(df.index)}} for s, df in bars.items()}
    actual = {(r["symbol"], r["bar_timestamp"]): r["reason_code"] if r["decision"] == "REJECT" else "ACCEPT"
              for r in risk_evals}
    out, agree, checked = [], 0, 0
    for r in rows:
        a = ind[r["symbol"]]
        t = a["pos"][r["bar_timestamp"]]
        rps = reconstruct_risk_ps(float(a["c"][t]), a["h"], a["l"], a["c"], t, risk_cfg)
        sh = shadow_fixed_horizon(a, t, rps)
        st = stateless_rr_liquidity(risk_cfg, a["h"], a["l"], a["c"], a["v"], t)
        row = {"symbol": r["symbol"], "bar_timestamp": r["bar_timestamp"], "gate_status": r["status"], **st,
               **{k: (v.item() if hasattr(v, "item") else v) for k, v in sh.items()}}
        code = actual.get((r["symbol"], r["bar_timestamp"]))
        row["actual_risk_decision"] = code
        if code in ("ACCEPT", "RR_BELOW_MINIMUM", "LIQUIDITY_BELOW_MINIMUM", "LEVERAGE_EXCEEDED"):
            checked += 1
            expect = {"RR_BELOW_MINIMUM": (st["stateless_liquidity_ok"] and not st["stateless_rr_ok"]),
                      "LIQUIDITY_BELOW_MINIMUM": not st["stateless_liquidity_ok"]}.get(code, st["stateless_eligible"])
            agree += bool(expect)
        out.append(row)
    df = pd.DataFrame(out)

    def block(sub: pd.DataFrame) -> Dict[str, Any]:
        v = sub[sub["shadow_valid"]]
        res: Dict[str, Any] = {"valid_observations": int(len(v))}
        for col in ("mfe_r_30m", "mfe_r_60m", "mae_r_30m", "mae_r_60m"):
            x = pd.to_numeric(v.get(col), errors="coerce").dropna() if col in v else pd.Series(dtype=float)
            res[f"median_{col}"] = float(x.median()) if len(x) else None
            res[f"mean_{col}"] = float(x.mean()) if len(x) else None
        h60 = v[pd.to_numeric(v.get("mfe_r_60m"), errors="coerce").notna()] if "mfe_r_60m" in v else v.iloc[0:0]
        n60 = len(h60)
        for name, col in (("reached_+0.5R_60m_pct", "hit_p050_60m"), ("reached_+1R_60m_pct", "hit_p100_60m"),
                          ("reached_-1R_60m_pct", "hit_m100_60m")):
            res[name] = float(h60[col].astype(bool).mean() * 100) if n60 else None
        res["observations_with_60m_horizon"] = int(n60)
        res["weak_forward_excursion_pct"] = (float((pd.to_numeric(h60["mfe_r_60m"]) < WEAK_FORWARD_MFE_R).mean() * 100)
                                             if n60 else None)
        return res

    groups = {}
    for name, stt in (("MARKET_REGIME_BLOCKED", BLOCKED), ("REGIME_PASSED", POSITIVE), ("MARKET_REGIME_UNAVAILABLE", UNAVAILABLE)):
        sub = df[df["gate_status"] == stt] if len(df) else df
        groups[name] = {"A_all_valid": block(sub) if len(sub) else {"valid_observations": 0},
                        "B_stateless_rr_liquidity_eligible": block(sub[sub["stateless_eligible"]]) if len(sub) else
                        {"valid_observations": 0}}
    return {"groups": groups, "horizons": [n for _, n in HORIZONS],
            "reached_flags_horizon": "60m (12 bars; close-based)",
            "weak_forward_excursion_definition": "60-minute shadow MFE < +0.25R",
            "stateless_check_vs_actual_risk_manager": {"checked": checked, "agree": agree},
            "note": "descriptive only; shadow outcomes never alter H004", "rows": df}


def regime_diagnostics(rows, ctx_info) -> Dict[str, Any]:
    n = len(rows)
    pos = [r for r in rows if r["status"] == POSITIVE]
    dist = [r["spy_close_vs_ema50_pct"] for r in rows if r["spy_close_vs_ema50_pct"] is not None]
    slope = [r["spy_ema50_slope3_pct"] for r in rows if r["spy_ema50_slope3_pct"] is not None]
    gap = [r for r in rows if r["slope_window_spans_missing_bucket"]]
    trunc_day = [r for r in rows if pd.Timestamp(r["bar_timestamp"]).tz_convert(NY).date().isoformat() == TRUNCATED_SESSION]
    on_after = [r for r in rows if pd.Timestamp(r["bar_timestamp"]).tz_convert(NY).date().isoformat() >= TRUNCATED_SESSION]
    return {
        "raw_signals": n,
        "positive_pct": len(pos) / n * 100 if n else None,
        "negative_pct": sum(r["status"] == BLOCKED for r in rows) / n * 100 if n else None,
        "unavailable_pct": sum(r["status"] == UNAVAILABLE for r in rows) / n * 100 if n else None,
        "median_spy_close_vs_ema50_pct": statistics.median(dist) if dist else None,
        "median_spy_ema50_slope3_pct": statistics.median(slope) if slope else None,
        "median_spy_close_vs_ema50_pct_by_status": {s: (statistics.median([r["spy_close_vs_ema50_pct"] for r in rows
                                                                            if r["status"] == s and r["spy_close_vs_ema50_pct"] is not None])
                                                        if any(r["status"] == s and r["spy_close_vs_ema50_pct"] is not None for r in rows) else None)
                                                    for s in (POSITIVE, BLOCKED)},
        "spy_slope_windows_spanning_missing_bucket_signal_level": len(gap),
        "signal_level_gap_signals": [{"symbol": r["symbol"], "bar_timestamp": r["bar_timestamp"], "status": r["status"]} for r in gap],
        "bucket_level_slope_gap_windows_recomputed": ctx_info.get("bucket_level_slope_gap_windows"),
        "bucket_level_slope_gap_windows_manifest": (ctx_info.get("manifest") or {}).get("manifest_bucket_level_slope_gap_windows"),
        "truncated_session_2024_12_23": {
            "raw_signals_on_session": len(trunc_day),
            "unavailable_on_session": sum(r["status"] == UNAVAILABLE for r in trunc_day),
            "status_on_session": {s: sum(r["status"] == s for r in trunc_day) for s in (POSITIVE, BLOCKED, UNAVAILABLE)},
            "unavailable_on_or_after_truncation": sum(r["status"] == UNAVAILABLE for r in on_after),
            "last_signal_decision_on_session_et": (max(pd.Timestamp(r["decision_time"]).tz_convert(NY).strftime("%H:%M")
                                                       for r in trunc_day) if trunc_day else None)},
        "time_of_day": {s: _tod([r for r in rows if r["status"] == s]) for s in (POSITIVE, BLOCKED, UNAVAILABLE)},
        "unavailable_signals": [{"symbol": r["symbol"], "bar_timestamp": r["bar_timestamp"],
                                 "expected_spy_bucket_start": r["expected_spy_bucket_start"]}
                                for r in rows if r["status"] == UNAVAILABLE],
    }


def _tod(rows) -> Dict[str, int]:
    out = {name: 0 for name, _, _ in TIME_BUCKETS}
    for r in rows:
        d = pd.Timestamp(r["decision_time"]).tz_convert(NY)
        b = time_bucket(d.hour * 60 + d.minute) or "other"
        out[b] = out.get(b, 0) + 1
    return out


def build_report(run: Dict[str, Any], protocol: Dict[str, Any], out_dir: Path, stored_h003: Path) -> Dict[str, Any]:
    _guard_out_dir(out_dir)
    out_dir = Path(out_dir)
    result, split, rows = run["result"], run["split"], run["gate"]
    summary = summarize(result)
    symbols = list(protocol["universe"]["symbols"])
    write_outputs(result, summary, out_dir)
    h003_trades = json.loads((Path(stored_h003) / "trades.json").read_text(encoding="utf-8"))
    h003_summary = json.loads((Path(stored_h003) / "summary.json").read_text(encoding="utf-8"))
    fn = funnel(rows, result)
    if not (fn["identity_raw_eq_blocked_plus_unavailable_plus_passed"] and fn["engine_buy_signals_equal_passed"]
            and fn["identity_passed_eq_downstream"]):
        raise AlignmentError(f"identidades del embudo no cuadran: {fn}")
    pdc = path_decomposition(h003_trades, result.trades, rows)
    if not (pdc["summary"]["identity_A_B_Bprime_eq_h003_trades"] and pdc["summary"]["bridge"]["reconciles"]):
        raise AlignmentError("la descomposición A/B/B′/C no cuadra")
    sh = shadow_diagnostic(rows, run["bars"], result.risk_evaluations)
    rd = regime_diagnostics(rows, run["context"])
    gates = development_gates(summary, result.trades, symbols) if split["role"] == "development" else None
    h3_raw = None
    sig_csv = Path(stored_h003) / "h003_signals.csv"
    if sig_csv.is_file():
        h3_raw = int(len(pd.read_csv(sig_csv)))
    report = {
        "hypothesis_id": HYPOTHESIS_ID, "frozen_spec_commit": FROZEN_SPEC_COMMIT, "split": split["name"],
        "role": split["role"], "dates": [split["start"], split["end"]],
        "evidence": ("DEVELOPMENT: hypothesis-development evidence only (motivated by already-viewed H001/H002/H003 "
                     "development research); VALIDATION is the first untouched test"),
        "protocol_version": protocol["protocol_version"], "code": _git_state(), "execution": protocol["execution_defaults"],
        "stock_support_bars": run["support"], "spy_context": run["context"],
        "h003_reproduction_check": run["reproduction"], "time_alignment_audit": run["alignment"],
        "funnel": fn, "headline": headline(summary), "exits": exit_report(result.trades), "exit_rates": exit_rates(result.trades),
        "by_symbol": symbol_report(result.trades, symbols),
        "gates": gates,
        "immediate_failure": {"H003_stored": immediate_failure(h003_trades), "H004": immediate_failure(result.trades),
                              "definition": "never reached +0.25R (close-based MFE) AND final realized R < 0; not a gate"},
        "comparison_h003": {"H003_stored": {"raw_signals": h3_raw, **headline(h003_summary),
                                            "exit_rates": exit_rates(h003_trades)},
                            "H004": {"raw_signals": fn["raw_h003_buy_signals"], **headline(summary),
                                     "exit_rates": exit_rates(result.trades)},
                            "raw_signals_identical_count": h3_raw == fn["raw_h003_buy_signals"] if h3_raw is not None else None},
        "path_decomposition": pdc["summary"],
        "blocked_signal_shadow": {k: v for k, v in sh.items() if k != "rows"},
        "regime_diagnostics": rd,
    }
    audit_cols = ["symbol", "bar_timestamp", "stock_decision_time", "expected_spy_bucket_start", "expected_spy_bucket_end",
                  "spy_bar_start", "spy_bar_end", "spy_bar_completed_at_decision", "status", "spy_close", "spy_ema50",
                  "spy_ema50_r_minus_3", "spy_close_vs_ema50_pct", "spy_ema50_slope3_pct", "slope_window_spans_missing_bucket"]
    pd.DataFrame(rows)[audit_cols].to_csv(out_dir / "market_regime_audit.csv", index=False)
    (out_dir / "signal_funnel.json").write_text(to_json(fn), encoding="utf-8")
    flat = [("raw_h003_buy_signals", fn["raw_h003_buy_signals"]), ("market_regime_blocked", fn["market_regime_blocked"]),
            ("market_regime_unavailable", fn["market_regime_unavailable"]), ("market_regime_passed", fn["market_regime_passed"])]
    flat += [(k, v) for k, v in fn["downstream_of_passed"].items() if not isinstance(v, dict)]
    pd.DataFrame(flat, columns=["stage", "count"]).to_csv(out_dir / "signal_funnel.csv", index=False)
    pd.DataFrame(pdc["rows"]).to_csv(out_dir / "path_decomposition.csv", index=False)
    sh["rows"].to_csv(out_dir / "blocked_signal_shadow.csv", index=False)
    (out_dir / "regime_diagnostics.json").write_text(to_json(rd), encoding="utf-8")
    (out_dir / "h004_report.json").write_text(to_json(report), encoding="utf-8")
    return report


def _n(x, fmt="{:+.4f}"):
    return "-" if x is None else fmt.format(x)


def format_report(r: Dict[str, Any]) -> str:
    h, f, d = r["headline"], r["funnel"], r["funnel"]["downstream_of_passed"]
    L = [f"{r['hypothesis_id']} — {r['split'].upper()} {r['dates'][0]} → {r['dates'][1]} (frozen spec "
         f"{r['frozen_spec_commit'][:7]}; H003 + SPY 15Min regime gate; next-bar open + 5 bps; $0)", "─" * 76, r["evidence"],
         f"H003 reproduction check: {'IDENTICAL' if r['h003_reproduction_check']['identical'] else 'FAILED'} "
         f"{r['h003_reproduction_check']['files_identical']}",
         f"Time alignment: {r['time_alignment_audit']}",
         f"Raw H003 BUY {f['raw_h003_buy_signals']} = blocked {f['market_regime_blocked']} + unavailable "
         f"{f['market_regime_unavailable']} + passed {f['market_regime_passed']}",
         f"Passed → already open {d['symbol_already_open']} | profit halt {d['daily_profit_halt']} | circuit breaker "
         f"{d['circuit_breaker']} | RiskManager {d['reached_risk_manager']} (accepts {d['accepts']}; rejects {d['rejects_by_reason']}) "
         f"| completed {d['completed_trades']}",
         f"Trades {h['completed_trades']} (W {h['wins']} / L {h['losses']}) win {h['win_rate_pct']:.1f}% | P&L "
         f"${h['realized_pnl']:,.2f} | return {h['return_pct']:.2f}%",
         f"Expectancy ${_n(h['expectancy'], '{:.2f}')} / {_n(h['expectancy_r'])}R | PF {_n(h['profit_factor'], '{:.4f}')} | "
         f"total R {_n(h['total_r'], '{:+.2f}')} | max DD {h['max_drawdown_pct']:.2f}% | max loss streak {h['max_consecutive_losses']}",
         f"Exits {r['exits']}",
         f"Immediate failures: H003 {_n(r['immediate_failure']['H003_stored']['rate_pct'], '{:.1f}')}% vs H004 "
         f"{_n(r['immediate_failure']['H004']['rate_pct'], '{:.1f}')}%",
         f"Path decomposition {json.dumps({k: v for k, v in r['path_decomposition'].items()}, default=str)}"]
    L.append(f"  {'symbol':<7}{'trades':>7}{'P&L':>12}{'exp R':>9}{'PF':>7}")
    for row in r["by_symbol"]:
        L.append(f"  {row['symbol']:<7}{row['trades']:>7}{row['pnl']:>12,.0f}{_n(row['expectancy_r'], '{:+.3f}'):>9}"
                 f"{_n(row['profit_factor'], '{:.2f}'):>7}")
    if r.get("gates"):
        L.append("")
        for k, g in r["gates"]["gates"].items():
            L.append(f"  {k:<26} value={g['value']}  rule {g['rule']}  -> {'PASS' if g['passed'] else 'FAIL'}")
        L.append(f"\nProgression to validation: {r['gates']['progression_to_validation']} (validation NOT run)")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="python -m src.research_h004", description=f"Corrida de investigación de {HYPOTHESIS_ID}.")
    p.add_argument("--split", required=True)
    p.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL_PATH)
    p.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    p.add_argument("--data-dir", type=Path, default=Path("data") / "historical")
    p.add_argument("--spy-dir", type=Path, default=DEFAULT_SPY_DIR)
    p.add_argument("--spy-manifest", type=Path, default=DEFAULT_SPY_MANIFEST)
    p.add_argument("--stored-h003", type=Path, default=DEFAULT_STORED_H003)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args(argv)
    protocol = load_protocol(a.protocol)
    try:
        _guard_out_dir(a.output_dir)
        run = run_split(protocol, a.split, a.data_dir, registry_entry(a.registry), a.spy_dir, a.spy_manifest,
                        a.stored_h003, a.output_dir / "h003_reproduction_check")
    except (HygieneError, ContextError, ReproductionError, AlignmentError) as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(format_report(build_report(run, protocol, a.output_dir, a.stored_h003)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
