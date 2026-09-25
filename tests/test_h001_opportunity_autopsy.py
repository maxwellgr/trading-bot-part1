"""
H001 OPPORTUNITY / SIGNAL-SELECTION AUTOPSY: ruteo, H001 sin mutación, entrada sombra en la siguiente
vela + 5 bps, reconstrucción exacta de R_ps, horizontes 3/6/12/fin de sesión, vela siguiente faltante,
sombras que no alteran el portafolio real, trades sombra aislados validados, grupos de competencia,
orden de procesamiento, etiqueta del arrepentimiento ex-post, exclusión de símbolo-ya-abierto, halts,
mapeo H002, higiene (solo development) y salida determinista. Sin red.
"""
import json

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca
from src import h001_opportunity_autopsy as oa
from src.backtest_engine import BacktestConfig, BacktestEngine, production_args
from src.research_protocol import load_protocol, validate_protocol
from src.run_paper import build_risk_config
from src.strategy import StrategyResult
from src.strategy_v2_h001 import HygieneError

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)


# ================================================================ ruteo / higiene
@pytest.mark.parametrize("stage,reason,route", [
    ("risk_accepted", None, "accepted"),
    ("risk_rejected", "RR_BELOW_MINIMUM", "rejected:RR_BELOW_MINIMUM"),
    ("risk_rejected", "LEVERAGE_EXCEEDED", "rejected:LEVERAGE_EXCEEDED"),
    ("in_position", None, "blocked:symbol_already_open"),
    ("daily_profit_halt", None, "blocked:daily_profit_halt"),
    ("circuit_breaker", "Racha negativa", "blocked:loss_streak_halt"),
    ("circuit_breaker", "Límite diario alcanzado", "blocked:circuit_breaker:Límite diario alcanzado"),
    ("unclassified", None, "unclassified:unclassified"),
])
def test_route_classification(stage, reason, route):
    assert oa.route_of(stage, reason) == route


def test_hygiene_development_only(tmp_path, capsys):
    p = load_protocol()
    assert oa.development_scope(p)["name"] == "development"
    for n in ("validation", "known_diagnostic", "forward", "warmup"):
        with pytest.raises(HygieneError):
            oa.development_scope(p, n)
        assert oa.main(["--split", n, "--output-dir", str(tmp_path / "o")]) == 2
    assert not (tmp_path / "o").exists()
    with pytest.raises(HygieneError):
        oa._guard_out_dir(oa.H001_STORED)
    with pytest.raises(HygieneError):
        oa._guard_out_dir(oa.H002_STORED)


# ================================================================ sombra: entrada, horizontes
def _ind(opens, closes, dates):
    n = len(closes)
    return {"o": np.asarray(opens, float), "c": np.asarray(closes, float), "date": np.asarray(dates),
            "ts": np.asarray([f"t{k}" for k in range(n)])}


def test_shadow_entry_next_bar_open_plus_5bps_and_horizons():
    closes = [100, 101, 102, 99, 100, 103, 98, 100, 100, 100, 100, 100, 104, 90, 90]
    opens = [100, 100.0] + closes[1:-1]
    ind = _ind(opens, closes, ["d1"] * 15)
    s = oa.shadow_fixed_horizon(ind, 0, risk_ps=2.0)
    f = 100.0 * 1.0005
    assert s["shadow_valid"] and s["shadow_entry_ts"] == "t1" and s["shadow_entry_price"] == pytest.approx(f)
    # 3 velas = cierres de E..E+2 = [101, 102, 99]
    assert s["mfe_r_15m"] == pytest.approx((102 - f) / 2) and s["mae_r_15m"] == pytest.approx((f - 99) / 2)
    assert s["close_r_15m"] == pytest.approx((99 - f) / 2)
    # 6 velas = [101,102,99,100,103,98]; 12 velas = cierres 1..12 (incluye 104)
    assert s["mfe_r_30m"] == pytest.approx((103 - f) / 2) and s["mfe_r_60m"] == pytest.approx((104 - f) / 2)
    assert s["hit_p100_60m"] is True and s["hit_p050_15m"] is True and s["hit_p100_15m"] is False
    assert s["hit_m050_15m"] is True and s["hit_m100_15m"] is False          # MAE15 = (F-99)/2 = 0.525R
    assert s["hit_m100_30m"] is True and s["crosses_session_60m"] is False   # MAE30 incluye 98 -> 1.025R
    # fin de sesión: todos los cierres del día de E (hasta 90)
    assert s["eos_bars"] == 14 and s["mae_r_eos"] == pytest.approx((f - 90) / 2) and s["hit_m100_eos"] is True


def test_session_end_horizon_and_session_crossing_flag():
    closes = [100.0] * 8
    ind = _ind(closes, closes, ["d1"] * 4 + ["d2"] * 4)
    s = oa.shadow_fixed_horizon(ind, 1, risk_ps=1.0)
    assert s["eos_bars"] == 2                       # E = idx 2 -> cierres 2,3 del día 1
    assert s["crosses_session_15m"] is True         # 2,3,4 -> cruza a d2
    assert s["mfe_r_60m"] is None                   # no hay 12 velas: nulo, nada inventado


def test_missing_next_bar_is_invalid_and_no_risk_is_invalid():
    ind = _ind([100.0, 100.0], [100.0, 100.0], ["d1", "d1"])
    assert oa.shadow_fixed_horizon(ind, 1, 2.0)["shadow_valid"] is False
    assert oa.shadow_fixed_horizon(ind, 0, None)["shadow_valid"] is False


# ================================================================ R_ps reconstruido == R real
class Buy:
    min_bars = 1

    def __init__(self, when):
        self.when = set(when)

    def evaluate(self, df):
        return StrategyResult("BUY" if df.index[-1] in self.when else None, "t")


def _synth(seed, sessions, start="2024-03-04", vol=0.35):
    rng = np.random.default_rng(seed)
    rows, idx, px = [], [], 100.0
    for d in pd.bdate_range(start, periods=sessions):
        for k in range(78):
            o = px + rng.normal(0, 0.1)
            c = o + 0.08 + rng.normal(0, vol)
            rows.append((o, max(o, c) + abs(rng.normal(0, .15)), min(o, c) - abs(rng.normal(0, .15)), c, 20_000.0))
            idx.append(pd.Timestamp(f"{d.date()} 09:30", tz=NY).tz_convert("UTC") + pd.Timedelta(minutes=5 * k))
            px = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))


def test_reconstructed_risk_ps_matches_engine_exactly():
    df = _synth(3, 3)
    when = list(df.index[60::7])
    cfg = BacktestConfig(symbols=["AAA"], timeframe="5Min", window_hours_limit=False)
    r = BacktestEngine(cfg, {"AAA": df}, production_args({"lookback": 278}), Buy(when)).run()
    rc = build_risk_config(production_args())
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    pos = {ts.isoformat(): k for k, ts in enumerate(df.index)}
    assert len(r.trades) >= 3
    for t in r.trades:
        k = pos[t["entry_signal_timestamp"]]
        assert oa.reconstruct_risk_ps(float(c[k]), h, l, c, k, rc) == t["risk_per_share_modeled"]


# ================================================================ competencia / orden / arrepentimiento
def _sig_rows():
    base = {"open_positions_before": 3, "gross_exposure_before": 1e5, "equity_before": 1e5, "shadow_status": "closed"}
    rows = [
        dict(base, bar_timestamp="T1", symbol="A", buy_rank_in_timestamp=1, symbol_order=0, route="accepted",
             mfe_r_60m=0.4, hit_p100_60m=False, actual_realized_r=0.2, shadow_realized_r=0.2),
        dict(base, bar_timestamp="T1", symbol="B", buy_rank_in_timestamp=2, symbol_order=1,
             route="rejected:MAX_POSITIONS_REACHED", mfe_r_60m=1.5, hit_p100_60m=True, actual_realized_r=None,
             shadow_realized_r=1.1),
        dict(base, bar_timestamp="T1", symbol="C", buy_rank_in_timestamp=3, symbol_order=2,
             route="rejected:RR_BELOW_MINIMUM", mfe_r_60m=0.1, hit_p100_60m=False, actual_realized_r=None,
             shadow_realized_r=None, shadow_status="not_entered"),
        dict(base, bar_timestamp="T2", symbol="A", buy_rank_in_timestamp=1, symbol_order=0, route="accepted",
             mfe_r_60m=0.9, hit_p100_60m=False, actual_realized_r=0.5, shadow_realized_r=0.5),
        dict(base, bar_timestamp="T2", symbol="B", buy_rank_in_timestamp=2, symbol_order=1, route="accepted",
             mfe_r_60m=0.2, hit_p100_60m=False, actual_realized_r=-1.0, shadow_realized_r=-1.0),
        dict(base, bar_timestamp="T3", symbol="C", buy_rank_in_timestamp=1, symbol_order=2, route="accepted",
             mfe_r_60m=0.3, hit_p100_60m=False, actual_realized_r=0.1, shadow_realized_r=0.1),
        dict(base, bar_timestamp="T3", symbol="D", buy_rank_in_timestamp=1, symbol_order=3,
             route="blocked:symbol_already_open", mfe_r_60m=2.0, hit_p100_60m=True, actual_realized_r=None,
             shadow_realized_r=None, shadow_status=None),
    ]
    return pd.DataFrame(rows)


def test_competition_grouping_regret_label_and_exclusion_of_non_reached():
    groups, cands, summ = oa.competition(_sig_rows())
    assert [g["bar_timestamp"] for g in groups] == ["T1", "T2"]                 # T3: solo 1 llegó al riesgo
    g1 = groups[0]
    assert (g1["candidates"], g1["accepted"], g1["rejected"], g1["rejected_capacity"], g1["focus_group"]) == (3, 1, 2, 1, True)
    assert g1["ex_post_regret_mfe_r_60m"] == pytest.approx(1.1) and g1["ex_post_regret_warning"] == oa.REGRET_WARNING
    assert g1["ex_post_regret_realized_r"] == pytest.approx(0.9)
    assert groups[1]["focus_group"] is False
    assert summ["candidate_count_distribution"] == {2: 1, 3: 1}
    reg = summ["ex_post_selection_regret"]
    assert reg["WARNING"] == "THIS USES FUTURE INFORMATION AND CANNOT BE USED AS A LIVE RULE."
    assert summ["capacity_rejected"]["candidates"] == 1 and summ["accepted"]["candidates"] == 1
    assert all(c["symbol"] != "D" for c in cands)


def test_processing_order_tables():
    sig = _sig_rows()
    _, cands, _ = oa.competition(sig)
    po = oa.processing_order(sig, cands)
    ranks = {r["rank_in_group"]: r for r in po["by_rank_in_competition_group"]}
    assert ranks[1]["candidates"] == 2 and ranks[1]["accepted_pct"] == 100.0
    assert ranks[2]["accepted_pct"] == 50.0 and ranks[3]["accepted_pct"] == 0.0
    assert [r["symbol_order"] for r in po["by_symbol_order"]] == [0, 1, 2]     # D (ya abierto) no llegó al riesgo


def test_in_position_analysis_links_host_trade():
    sig = pd.DataFrame([{"route": "blocked:symbol_already_open", "symbol": "A", "bar_timestamp": "2024-03-04T15:00:00+00:00",
                         "signal_close": 101.0},
                        {"route": "blocked:symbol_already_open", "symbol": "A", "bar_timestamp": "2024-03-04T18:00:00+00:00",
                         "signal_close": 99.0}])
    trades = [{"trade_id": 1, "symbol": "A", "entry_signal_timestamp": "2024-03-04T14:30:00+00:00",
               "exit_fill_timestamp": "2024-03-04T16:00:00+00:00", "entry_fill_price": 100.0, "risk_per_share_modeled": 2.0,
               "result": "win", "realized_r": 0.8, "exit_reason": "giveback_close", "excursion_bars": 10},
              {"trade_id": 2, "symbol": "A", "entry_signal_timestamp": "2024-03-04T17:00:00+00:00",
               "exit_fill_timestamp": "2024-03-04T19:00:00+00:00", "entry_fill_price": 100.0, "risk_per_share_modeled": 2.0,
               "result": "loss", "realized_r": -1.0, "exit_reason": "stop_hit", "excursion_bars": 20}]
    r = oa.in_position_analysis(sig, trades)
    assert r["host_found"] == 2 and r["signals_in_eventual_winners"] == 1 and r["signals_in_eventual_losers"] == 1
    assert r["pct_signals_host_above_entry"] == 50.0 and r["signals_per_held_bar_win"] == pytest.approx(0.1)
    assert r["signals_per_held_bar_loss"] == pytest.approx(0.05)


def test_route_quality_halts_and_isolated_fields():
    sig = pd.DataFrame([
        {"route": "accepted", "shadow_valid": True, **{f"mfe_r_{h}": 0.5 for h in ("15m", "30m", "60m", "eos")},
         "mae_r_60m": 0.2, "hit_p050_60m": True, "hit_p100_60m": False, "hit_m100_60m": False, "close_r_eos": 0.1,
         "shadow_status": "closed", "shadow_realized_r": 1.0, "shadow_exit_reason": "giveback_close"},
        {"route": "blocked:loss_streak_halt", "shadow_valid": True, **{f"mfe_r_{h}": 0.1 for h in ("15m", "30m", "60m", "eos")},
         "mae_r_60m": 1.2, "hit_p050_60m": False, "hit_p100_60m": False, "hit_m100_60m": True, "close_r_eos": -1.0,
         "shadow_status": "closed", "shadow_realized_r": -1.0, "shadow_exit_reason": "stop_hit"},
        {"route": "blocked:loss_streak_halt", "shadow_valid": False, **{f"mfe_r_{h}": None for h in ("15m", "30m", "60m", "eos")},
         "mae_r_60m": None, "hit_p050_60m": None, "hit_p100_60m": None, "hit_m100_60m": None, "close_r_eos": None,
         "shadow_status": "not_entered", "shadow_realized_r": None, "shadow_exit_reason": None}])
    q = {r["route"]: r for r in oa.route_quality(sig)}
    assert list(q) == ["accepted", "blocked:loss_streak_halt"]
    h = q["blocked:loss_streak_halt"]
    assert (h["signals"], h["valid_shadow"], h["pct_hit_m100_60m"], h["isolated_closed"], h["isolated_not_entered"]) == (2, 1, 100.0, 1, 1)
    assert q["accepted"]["isolated_expectancy_r"] == 1.0


def test_h002_route_mapping(tmp_path):
    sig = pd.DataFrame([{"symbol": "A", "bar_timestamp": "t1", "route": "accepted"},
                        {"symbol": "A", "bar_timestamp": "t2", "route": "blocked:symbol_already_open"},
                        {"symbol": "B", "bar_timestamp": "t3", "route": "rejected:LEVERAGE_EXCEEDED"}])
    h1 = [{"symbol": "A", "entry_signal_timestamp": "t1"}]
    h2 = [{"symbol": "A", "entry_signal_timestamp": "t1", "realized_pnl": 5.0, "realized_r": 0.1, "result": "win"},
          {"symbol": "A", "entry_signal_timestamp": "t2", "realized_pnl": -10.0, "realized_r": -0.5, "result": "loss"},
          {"symbol": "B", "entry_signal_timestamp": "t3", "realized_pnl": 4.0, "realized_r": 0.2, "result": "win"}]
    (tmp_path / "trades.json").write_text(json.dumps(h2), encoding="utf-8")
    m = oa.h002_mapping(sig, h1, tmp_path)
    assert m["h002_only_total"] == 2 and m["h002_only_pnl"] == -6.0
    assert {r["h001_route"]: r["h002_only_trades"] for r in m["by_h001_route"]} == {
        "blocked:symbol_already_open": 1, "rejected:LEVERAGE_EXCEEDED": 1}
    assert oa.h002_mapping(sig, h1, tmp_path / "nope") is None


# ================================================================ extremo a extremo (mini dataset)
def _write_1min(root, sym, df5, seed):
    rng = np.random.default_rng(seed)
    rows = []
    for ts, r in df5.iterrows():
        path = np.linspace(r.open, r.close, 5) + rng.normal(0, 0.01, 5)
        path[0], path[-1] = r.open, r.close
        for k in range(5):
            o = path[k - 1] if k else r.open
            rows.append({"timestamp": (ts + pd.Timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": o,
                         "high": max(o, path[k]) + 0.01, "low": min(o, path[k]) - 0.01, "close": path[k],
                         "volume": 5000.0, "symbol": sym})
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(d / f"{sym}.csv", index=False)


def _mini_protocol(symbols):
    p = load_protocol()
    p["universe"]["symbols"] = symbols
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2024-01-01", "end": "2024-03-03"},
                   {"name": "development", "role": "development", "start": "2024-03-11", "end": "2024-03-13"},
                   {"name": "validation", "role": "validation", "start": "2024-03-14", "end": "2024-03-15"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2024-05-01", "end": "2024-05-31"},
                   {"name": "forward", "role": "forward", "start": "2024-06-03", "end": None}]
    return validate_protocol(p)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    from src import research_h001 as r1
    root = tmp_path_factory.mktemp("opp")
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    for k, s in enumerate(syms):
        _write_1min(root / "hist", s, _synth(60 + k, 10), 70 + k)
    p = _mini_protocol(syms)
    r1.build_report(r1.run_split(p, "development", root / "hist", {"id": "X", "status": "IMPLEMENTED"}), p, root / "h001")
    return root, p


def _build(mini, out, workers=0):
    root, p = mini
    return oa.build(p, oa.development_scope(p), root / "hist", out, stored=root / "h001",
                    h002_stored=root / "none", workers=workers)


def test_end_to_end_h001_unchanged_routes_complete_and_shadows_validated(mini, tmp_path):
    res = _build(mini, tmp_path / "o")
    s, sig = res["summary"], res["signals"]
    assert s["h001_rerun_identical"]["identical"] is True                       # H001 sin mutación
    assert len(sig) > 0 and not sig["route"].str.startswith("unclassified").any()
    stored = json.loads((mini[0] / "h001" / "h001_report.json").read_text(encoding="utf-8"))
    assert len(sig) == sum(stored["signals_emitted"].values())                 # una ruta por señal emitida
    chk = s["risk_ps_reconstruction_check"]
    assert chk["accepted"] > 0 and chk["exact_matches"] == chk["accepted"]
    iv = s["isolated_shadow_validation"]
    assert iv["validated"] is True and iv["accepted_trades_checked"] == chk["accepted"]
    # símbolo ya abierto: nunca un trade sombra aislado
    assert sig.loc[sig["route"] == "blocked:symbol_already_open", "shadow_status"].isna().all()
    # orden de procesamiento determinista: dentro de un timestamp el rango sigue el orden de --symbols
    for _, g in sig.groupby("bar_timestamp"):
        g = g.sort_values("buy_rank_in_timestamp")
        assert list(g["symbol_order"]) == sorted(g["symbol_order"])
        assert list(g["buy_rank_in_timestamp"]) == list(range(1, len(g) + 1))


def test_shadows_cannot_alter_the_actual_portfolio(mini, tmp_path):
    root, p = mini
    res = _build(mini, tmp_path / "o")
    stored = (root / "h001" / "trades.json").read_bytes()
    assert (tmp_path / "o" / "h001_rerun_check" / "trades.json").read_bytes() == stored
    # el observador y los trades sombra corren después y en motores separados: re-correr no cambia nada
    res2 = _build(mini, tmp_path / "o2")
    assert (tmp_path / "o2" / "h001_rerun_check" / "trades.json").read_bytes() == stored
    assert res["summary"]["funnel"] == res2["summary"]["funnel"]


def test_parallel_and_serial_isolated_shadows_agree(mini, tmp_path):
    a = _build(mini, tmp_path / "a", workers=0)["signals"]
    b = _build(mini, tmp_path / "b", workers=2)["signals"]
    cols = ["key", "shadow_status", "shadow_exit_reason", "shadow_realized_r"]
    pd.testing.assert_frame_equal(a[cols].reset_index(drop=True), b[cols].reset_index(drop=True))


def test_outputs_deterministic_and_report_labels(mini, tmp_path):
    for name in ("x", "y"):
        oa.write(_build(mini, tmp_path / name), tmp_path / name)
    files = sorted(p.name for p in (tmp_path / "x").iterdir() if p.is_file())
    assert {"opportunity_summary.json", "signal_routes.csv", "shadow_fixed_horizon.csv", "route_quality.csv",
            "risk_reject_quality.csv", "halt_quality.csv", "competition_groups.csv", "competition_candidates.csv",
            "processing_order.csv", "signal_feature_routes.csv", "shadow_trades.csv", "shadow_trade_quality.csv"} <= set(files)
    for f in files:
        assert (tmp_path / "x" / f).read_bytes() == (tmp_path / "y" / f).read_bytes()
    text = oa.format_report(json.loads((tmp_path / "x" / "opportunity_summary.json").read_text(encoding="utf-8")))
    assert "WARNING: THIS USES FUTURE INFORMATION AND CANNOT BE USED AS A LIVE RULE." in text
    assert text.lower().count("h003") == text.lower().count("no h003")        # solo aparece como "no H003"
    for banned in ("optimal", "best filter", "use this"):
        assert banned not in text.lower()
