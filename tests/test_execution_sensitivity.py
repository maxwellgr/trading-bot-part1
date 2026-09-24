"""
Sensibilidad a costos de ejecución: runner de escenarios (orden determinista,
solo cambia el slippage), igualdad exacta del escenario de referencia con una
corrida directa, métricas por escenario, marginales, break-even, desgloses
(símbolo / quintil ATR% / motivo de salida), dependencia de camino, salidas
JSON/CSV/CLI y que no se toque nada de producción. Sin red.
"""
import json

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca, execution_sensitivity as es
from src.backtest_engine import BacktestConfig, production_args, run_backtest
from src.backtest_report import summarize, write_outputs
from src.run_paper import build_risk_config

NY = "America/New_York"
T0 = pd.Timestamp("2026-06-01 09:30", tz=NY).tz_convert("UTC")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


def make_bars(closes, start=T0, spread=0.3, vol=10_000):
    opens = [closes[0]] + list(closes[:-1])
    idx = pd.DatetimeIndex([start + pd.Timedelta(minutes=i) for i in range(len(closes))])
    return pd.DataFrame({"open": opens, "high": [max(o, c) + spread for o, c in zip(opens, closes)],
                         "low": [min(o, c) - spread for o, c in zip(opens, closes)], "close": list(closes),
                         "volume": float(vol)}, index=idx)


def random_bars(seed=3, sym_n=3, n=390):
    rng = np.random.default_rng(seed)
    return {f"S{k}": make_bars([round(v, 4) for v in 100 + np.cumsum(rng.normal(0, 0.25, n))]) for k in range(sym_n)}


SYMS = ["S0", "S1", "S2"]


@pytest.fixture(scope="module")
def study_inputs():
    bars = random_bars()
    scen = es.run_scenarios(bars, [5, 0, 10, 2.5, 5], symbols=SYMS)
    return bars, scen


# ---------------------------------------------------------------- runner
def test_parse_bps_sorts_dedupes_and_rejects_negatives():
    assert es.parse_bps("10, 0,2.5,5,5") == [0.0, 2.5, 5.0, 10.0]
    with pytest.raises(ValueError):
        es.parse_bps("0,-1")
    with pytest.raises(ValueError):
        es.parse_bps(" , ")
    assert es.scenario_dirname(2.5) == "slippage_2_5bps" and es.scenario_dirname(5.0) == "slippage_5bps"


def test_scenarios_are_ordered_deduped_and_carry_their_slippage(study_inputs):
    _, scen = study_inputs
    assert [b for b, _ in scen] == [0.0, 2.5, 5.0, 10.0]
    for bps, r in scen:
        assert r.config["slippage_bps"] == bps and r.config["commission"] == 0.0
        for f in r.fills:  # propagación hasta el fill: open ± bps
            sign = 1 if f["side"] == "buy" else -1
            assert f["price"] == pytest.approx(f["reference_open"] * (1 + sign * bps / 10_000))


def test_reference_scenario_equals_a_direct_backtest(study_inputs):
    bars, scen = study_inputs
    direct = run_backtest(BacktestConfig(symbols=SYMS, slippage_bps=5.0), bars)
    ref = dict(scen)[5.0]
    assert ref.trades == direct.trades and ref.equity_curve == direct.equity_curve
    assert ref.risk_evaluations == direct.risk_evaluations and ref.counters == direct.counters


def test_parallel_runner_gives_identical_results(study_inputs):
    bars, scen = study_inputs
    par = es.run_scenarios(bars, [0, 2.5, 5, 10], jobs=2, symbols=SYMS)
    assert [b for b, _ in par] == [b for b, _ in scen]
    for (_, a), (_, b) in zip(par, scen):
        assert a.trades == b.trades and a.equity_curve == b.equity_curve


def test_runner_refuses_to_override_slippage_in_config():
    with pytest.raises(ValueError):
        es.run_scenarios(random_bars(), [0], symbols=SYMS, slippage_bps=3)


def test_only_execution_slippage_changes_not_the_risk_model(study_inputs):
    _, scen = study_inputs
    assert build_risk_config(production_args()).slippage_pct == 0.0005  # supuesto de producción intacto
    first = {b: r.risk_evaluations[0] for b, r in scen}
    # la primera decisión ocurre antes de cualquier fill: idéntica en todos los escenarios
    assert len({json.dumps(v, sort_keys=True, default=str) for v in first.values()}) == 1
    acc = {b: next(e for e in r.risk_evaluations if e["decision"] == "ACCEPT") for b, r in scen}
    assert len({(e["entry_price"], e["stop_price"], e["position_size"]) for e in acc.values()}) == 1


# ---------------------------------------------------------------- métricas
def test_scenario_metrics_collect_summary_fields(study_inputs):
    _, scen = study_inputs
    bps, r = scen[2]
    s = summarize(r)
    row = es.scenario_metrics(bps, r, s)
    assert row["slippage_bps"] == 5.0 and row["trades"] == s["trades"]["trades"]
    assert row["realized_pnl"] == s["portfolio"]["realized_pnl_closed_trades"]
    assert row["risk_accepts"] == s["execution"]["risk_accept"]
    assert sum(row[f"exit_{x}"] for x in es.EXIT_REASONS) == row["trades"]
    assert row["slippage_paid"] == pytest.approx(sum(abs(f["price"] - f["reference_open"]) * f["qty"] for f in r.fills))
    assert set(es.SCENARIO_COLUMNS) <= set(row)
    zero = es.scenario_metrics(0.0, scen[0][1], summarize(scen[0][1]))
    assert zero["slippage_paid"] == 0.0


def _row(bps, pnl, exp, pf, trades=10, **kw):
    return dict(slippage_bps=bps, realized_pnl=pnl, return_pct=pnl / 1000, expectancy=exp, expectancy_r=exp / 100,
                profit_factor=pf, max_drawdown_pct=-5 - bps, total_r=pnl / 100, trades=trades, **kw)


def test_marginal_calculations_and_sensitivity():
    rows = [_row(0, 100.0, 10.0, 1.2), _row(2.5, 50.0, 5.0, 1.1, trades=11), _row(5, -50.0, -5.0, 0.9),
            _row(10, -250.0, -25.0, float("inf"))]
    m = es.marginal_rows(rows)
    assert [(x["from_bps"], x["to_bps"]) for x in m] == [(0, 2.5), (2.5, 5), (5, 10)]
    assert m[0]["delta_pnl"] == -50.0 and m[0]["pnl_change_per_bps"] == -20.0 and m[0]["delta_trades"] == 1
    assert m[1]["delta_profit_factor"] == pytest.approx(-0.2) and m[1]["delta_max_drawdown_pct"] == -2.5
    assert m[2]["delta_profit_factor"] is None  # inf -> no se resta
    se = es.sensitivity_estimate(rows, m)
    assert se["least_squares_pnl_per_bps"] == pytest.approx(np.polyfit([0, 2.5, 5, 10], [100, 50, -50, -250], 1)[0])
    assert se["min_transition_pnl_per_bps"] == -40.0 and "not a universal constant" in se["note"]


def test_break_even_interpolates_only_inside_the_tested_range():
    rows = [_row(0, 100.0, 10.0, 1.2), _row(2.5, 50.0, 5.0, 1.1), _row(5, -50.0, -5.0, 0.9)]
    be = es.break_even(rows, "realized_pnl", 0.0)
    [c] = be["crossings"]
    assert c["bps"] == pytest.approx(2.5 + 50 / 100 * 2.5) and c["between"] == [2.5, 5]
    assert "interpolation" in c["method"]
    assert es.break_even(rows, "profit_factor", 1.0)["crossings"][0]["bps"] == pytest.approx(3.75)
    neg = [_row(0, -1.0, -1.0, 0.9), _row(5, -9.0, -2.0, 0.8)]
    assert es.break_even(neg, "realized_pnl", 0.0)["status"] == "no break-even slippage observed within tested range"
    pos = [_row(0, 5.0, 1.0, 1.5), _row(5, 1.0, 0.5, 1.1)]
    assert "above" in es.break_even(pos, "profit_factor", 1.0)["status"]
    assert es.break_even([_row(0, 0.0, 0.0, 1.0)], "realized_pnl", 0.0)["crossings"][0]["method"] == "observed"


def test_zero_cost_answers_signs():
    z = es.zero_cost([_row(0, -10.0, -1.0, 0.95), _row(5, -50.0, -5.0, 0.8)])
    assert z["total_pnl_sign"] == "negative" and z["expectancy_sign"] == "negative"
    assert z["profit_factor_vs_1"] == "below" and z["total_r_sign"] == "negative"
    assert es.zero_cost([_row(5, 1.0, 1.0, 1.1)])["available"] is False


# ---------------------------------------------------------------- desgloses
def _t(sym, ts, pnl, r, reason="signal_exit", atr=0.5, cost=1.0, cost_r=0.1):
    return {"symbol": sym, "entry_signal_timestamp": ts, "exit_reason": reason, "realized_pnl": pnl, "realized_r": r,
            "result": "win" if pnl > 0.005 else "loss" if pnl < -0.005 else "breakeven",
            "slippage_cost": cost, "slippage_cost_r": cost_r, "gross_r": r + cost_r, "atr_pct": atr}


def test_per_symbol_aggregation_and_degradation():
    scen = [(0.0, [_t("A", "t1", 100, 1.0), _t("A", "t2", -50, -0.5), _t("B", "t3", 10, 0.1)]),
            (5.0, [_t("A", "t1", 80, 0.8), _t("B", "t3", -10, -0.1)]),
            (10.0, [_t("A", "t1", 60, 0.6)])]
    rows = es.per_symbol_rows(scen, ["A", "B"])
    a0 = next(r for r in rows if r["symbol"] == "A" and r["slippage_bps"] == 0)
    assert (a0["trades"], a0["pnl"], a0["win_rate_pct"], a0["expectancy"]) == (2, 50, 50.0, 25.0)
    assert a0["profit_factor"] == 2.0 and a0["avg_r"] == pytest.approx(0.25)
    b10 = next(r for r in rows if r["symbol"] == "B" and r["slippage_bps"] == 10)
    assert b10["trades"] == 0 and b10["pnl"] == 0 and b10["avg_r"] is None
    deg = {d["symbol"]: d for d in es._degradation(rows, "symbol", [(0.0, 5.0), (5.0, 10.0)])}
    assert deg["A"]["pnl_change_0_to_5_bps"] == 30 and deg["A"]["pnl_change_5_to_10_bps"] == -20
    assert deg["B"]["pnl_change_0_to_5_bps"] == -20


def test_atr_quintiles_use_fixed_reference_edges():
    ref = [_t("A", f"t{i}", 1, 0.1, atr=float(i)) for i in range(1, 11)]
    edges = es.atr_edges(ref)
    assert edges == pytest.approx([2.8, 4.6, 6.4, 8.2])
    labels = [es.atr_label(t["atr_pct"], edges) for t in ref]
    assert labels == ["Q1", "Q1", "Q2", "Q2", "Q3", "Q3", "Q4", "Q4", "Q5", "Q5"]
    # coincide con qcut (lo que usa Entry Quality) sobre el mismo conjunto
    assert labels == [f"Q{c + 1}" for c in pd.qcut([t["atr_pct"] for t in ref], 5, labels=False)]
    other = [_t("A", "x", -5, -0.5, atr=100.0, cost_r=0.3), _t("A", "y", 5, 0.5, atr=None)]
    rows = es.per_atr_rows([(7.5, other)], edges)
    assert [(r["atr_quintile"], r["trades"]) for r in rows] == [("Q5", 1), ("missing", 1)]
    assert rows[0]["avg_slippage_cost_r"] == 0.3 and rows[0]["median_r"] == -0.5
    assert es.atr_edges(ref[:3]) is None and es.atr_label(1.0, None) == "missing"


def test_exit_reason_aggregation_includes_all_reasons():
    rows = es.per_exit_rows([(0.0, [_t("A", "t1", 10, 0.1, "stop_hit"), _t("A", "t2", 5, 0.2, "odd")])])
    assert [r["exit_reason"] for r in rows] == list(es.EXIT_REASONS) + ["odd"]
    stop = next(r for r in rows if r["exit_reason"] == "stop_hit")
    assert stop["trades"] == 1 and stop["pnl"] == 10 and stop["avg_r"] == 0.1
    assert next(r for r in rows if r["exit_reason"] == "signal_exit")["trades"] == 0


def test_decomposition_components_sum_exactly_and_path_dependence():
    scen = [(0.0, [_t("A", "t1", 100, 1), _t("A", "t2", -30, -0.3), _t("B", "t3", 20, 0.2)]),
            (5.0, [_t("A", "t1", 90, 0.9), _t("B", "t3", 15, 0.15), _t("B", "t4", -40, -0.4)])]
    d = es.decomposition(scen)
    c = d["components"]
    assert d["difference_attributable_to_execution_cost_assumption"] == (90 + 15 - 40) - (100 - 30 + 20)
    assert c["common_trades"] == 2 and c["common_trades_pnl_change"] == -15
    assert c["their_pnl_removed"] == 30 and c["their_pnl_added"] == -40
    assert c["common_trades_pnl_change"] + c["their_pnl_removed"] + c["their_pnl_added"] == \
        d["difference_attributable_to_execution_cost_assumption"]
    pd_ = es.path_dependence(scen, 5.0)["by_scenario"]
    assert pd_["0"]["common_with_reference"] == 2 and pd_["0"]["only_in_scenario"] == 1
    assert pd_["0"]["first_divergent_entry"] == {"symbol": "A", "entry_signal_timestamp": "t2"}
    assert pd_["5"]["first_divergent_entry"] is None
    assert es.decomposition([(5.0, [])])["available"] is False


def test_trade_slippage_cost_matches_fills(study_inputs):
    _, scen = study_inputs
    bps, r = scen[-1]
    enriched = es.enrich_trades(r)
    assert enriched and all(t["slippage_cost"] > 0 for t in enriched)
    closed_fill_cost = sum(abs(f["price"] - f["reference_open"]) * f["qty"] for f in r.fills
                           if not any(p["symbol"] == f["symbol"] for p in r.open_positions))
    assert sum(t["slippage_cost"] for t in enriched) == pytest.approx(closed_fill_cost)
    t0, raw = enriched[0], r.trades[0]
    assert t0["slippage_cost_r"] == pytest.approx(t0["slippage_cost"] / (raw["risk_per_share_modeled"] * raw["initial_qty"]))
    assert t0["atr_pct"] == raw["entry_context"]["atr_pct"]


# ---------------------------------------------------------------- estudio / salidas
def test_study_json_csv_outputs_and_report(study_inputs, tmp_path):
    _, scen = study_inputs
    study = es.build_study(scen, SYMS)
    study["baseline_match"] = {"checked": False, "note": "n/a"}
    assert [r["slippage_bps"] for r in study["scenarios"]] == [0.0, 2.5, 5.0, 10.0]
    assert len(study["marginal"]) == 3 and study["zero_cost"]["available"]
    paths = es.write_study(study, tmp_path)
    assert {p.name for p in paths} == {"execution_sensitivity_summary.json", "execution_sensitivity.csv",
                                       "execution_sensitivity_marginal.csv", "execution_sensitivity_by_symbol.csv",
                                       "execution_sensitivity_by_atr_quintile.csv",
                                       "execution_sensitivity_by_exit_reason.csv"}
    saved = json.loads((tmp_path / "execution_sensitivity_summary.json").read_text(encoding="utf-8"))
    assert saved["scenarios_bps"] == [0.0, 2.5, 5.0, 10.0] and "break_even" in saved
    csv_main = pd.read_csv(tmp_path / "execution_sensitivity.csv")
    assert list(csv_main.columns) == es.SCENARIO_COLUMNS and list(csv_main["slippage_bps"]) == [0.0, 2.5, 5.0, 10.0]
    assert len(pd.read_csv(tmp_path / "execution_sensitivity_by_symbol.csv")) == 4 * len(SYMS)
    text = es.format_study(study)
    for head in ("EXECUTION SENSITIVITY", "ZERO-COST RESULT", "BASELINE MATCH", "ATR% QUINTILE", "PER-SYMBOL",
                 "EXIT-REASON"):
        assert head in text
    assert "best" not in text.lower().replace("no scenario is 'best'", "")


def test_study_is_deterministic(study_inputs, tmp_path):
    bars, scen = study_inputs
    again = es.run_scenarios(bars, [0, 2.5, 5, 10], symbols=SYMS)
    for name, sc in (("a", scen), ("b", again)):
        es.write_study(es.build_study(sc, SYMS), tmp_path / name)
    for f in (tmp_path / "a").iterdir():
        assert f.read_bytes() == (tmp_path / "b" / f.name).read_bytes()


def test_baseline_match_detects_identity_and_differences(study_inputs, tmp_path):
    bars, scen = study_inputs
    r5 = dict(scen)[5.0]
    write_outputs(r5, summarize(r5), tmp_path / "scen")
    direct = run_backtest(BacktestConfig(symbols=SYMS, slippage_bps=5.0), bars)
    write_outputs(direct, summarize(direct), tmp_path / "base")
    ok = es.baseline_match(tmp_path / "scen", tmp_path / "base")
    assert ok["exact_match"] is True and set(ok["files"].values()) == {"identical"}
    r0 = dict(scen)[0.0]
    write_outputs(r0, summarize(r0), tmp_path / "other")
    bad = es.baseline_match(tmp_path / "other", tmp_path / "base")
    assert bad["exact_match"] is False and "DIFFERENT" in bad["files"].values()
    assert es.baseline_match(tmp_path / "scen", tmp_path / "nope")["checked"] is False


def _write_csv(root, symbol, df):
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    out = df.reset_index().rename(columns={"index": "timestamp"})
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["symbol"] = symbol
    out.to_csv(d / f"{symbol}.csv", index=False)


def test_cli_end_to_end_with_baseline_reproduction(tmp_path, capsys):
    from src import backtest as backtest_cli
    for sym, df in random_bars(sym_n=2).items():
        _write_csv(tmp_path / "hist", sym, df)
    common = ["--symbols", "S0,S1", "--start", "2026-06-01", "--end", "2026-06-01", "--data-dir", str(tmp_path / "hist")]
    assert backtest_cli.main(common + ["--output-dir", str(tmp_path / "baseline")]) == 0
    capsys.readouterr()
    rc = es.main(common + ["--output-dir", str(tmp_path / "sens"), "--slippage-bps", "0,5,10", "--jobs", "1",
                           "--baseline-dir", str(tmp_path / "baseline")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "EXACT MATCH" in out and "ZERO-COST RESULT" in out
    saved = json.loads((tmp_path / "sens" / "execution_sensitivity_summary.json").read_text(encoding="utf-8"))
    assert saved["baseline_match"]["exact_match"] is True
    assert [p.name for p in sorted((tmp_path / "sens" / "scenarios").iterdir())] == \
        ["slippage_0bps", "slippage_10bps", "slippage_5bps"]
    # la baseline no se sobrescribe
    with pytest.raises(SystemExit):
        es.main(common + ["--output-dir", str(tmp_path / "baseline"), "--baseline-dir", str(tmp_path / "baseline")])
