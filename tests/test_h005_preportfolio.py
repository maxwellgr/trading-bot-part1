"""STRATEGY_V2_HYPOTHESIS_005 — pruebas SOLO con datos sintéticos (sin datos reales, sin red)."""
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
import requests

from src import broker_alpaca
from src import h005_preportfolio as h5
from src import preportfolio_screen as ps

NY = "America/New_York"
REG = "2024-03-12"          # sesión regular (martes)
EARLY = "2024-07-03"        # cierre anticipado congelado (13:00)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)
        monkeypatch.setattr(requests, fn, boom)


# ================================================================ constructores sintéticos
def ts(day, hhmm):
    return pd.Timestamp(f"{day} {hhmm}", tz=NY).tz_convert("UTC")


def bars(rows):
    """rows: [(day, 'HH:MM', open, close), ...] -> 30Min OHLC (high/low envuelven open/close)."""
    idx = [ts(d, t) for d, t, _, _ in rows]
    o = [float(r[2]) for r in rows]
    c = [float(r[3]) for r in rows]
    df = pd.DataFrame({"open": o, "high": [max(a, b) + 0.5 for a, b in zip(o, c)],
                       "low": [min(a, b) - 0.5 for a, b in zip(o, c)], "close": c, "volume": 1000.0},
                      index=pd.DatetimeIndex(idx, name="timestamp"))
    return df.sort_index()


SLOTS = [f"{9 + (30 + 30 * k) // 60:02d}:{(30 + 30 * k) % 60:02d}" for k in range(13)]   # 09:30 .. 15:30


def session(day, closes, open0=100.0, slots=None):
    """Una sesión: open de cada cubeta = cierre previo (la primera abre en open0)."""
    slots = slots or SLOTS[:len(closes)]
    rows, prev = [], open0
    for s, c in zip(slots, closes):
        rows.append((day, s, prev, c))
        prev = c
    return rows


def flat_spy(days, n=13, price=100.0, slots=None):
    rows = []
    for d in days:
        for s in (slots or SLOTS[:n]):
            rows.append((d, s, price, price))
    return bars(rows)


def table(stocks, spy, day=REG):
    d = date.fromisoformat(day)
    return h5.signal_table(stocks, spy, d, d)


def emitted_times(t):
    return [pd.Timestamp(x).tz_convert(NY).strftime("%H:%M") for x in t.loc[t["emitted"], "bar_timestamp"]]


BASE = [100.5, 100.2, 101.0, 101.5, 101.2, 101.8] + [101.0] * 7        # emite en la barra 10:30 (D = 11:00)


# ================================================================ SEÑAL
def test_all_conditions_true_emits_at_1100_decision():
    t = table({"AAA": bars(session(REG, BASE))}, flat_spy([REG]))
    assert emitted_times(t) == ["10:30"]
    row = t[t["emitted"]].iloc[0]
    assert row["decision_min"] == 11 * 60 and row["A"] and row["B"] and row["C"]


def test_a_false_blocks():
    t = table({"AAA": bars(session(REG, [98, 97, 99, 99.5] + [95] * 9))}, flat_spy([REG]))
    assert not t["emitted"].any() and t.loc[t["decision_min"] == 660, "C"].iloc[0] and not t.loc[t["decision_min"] == 660, "A"].iloc[0]


def test_b_false_blocks():
    spy = bars([(REG, s, 100.0, 102.0 if s >= "10:30" else 100.0) for s in SLOTS])
    t = table({"AAA": bars(session(REG, [100.5, 100.2, 101.0] + [100.0] * 10))}, spy)
    first = t[t["decision_min"] == 660].iloc[0]
    assert first["A"] and first["C"] and not first["B"] and not t["emitted"].any()


def test_c_false_blocks_and_pre_1100_bars_participate():
    t = table({"AAA": bars(session(REG, [100.5, 101.5, 101.0] + [100.0] * 10))}, flat_spy([REG]))
    first = t[t["decision_min"] == 660].iloc[0]
    assert first["A"] and first["B"] and not first["C"]          # 101.0 < 101.5 de la barra 10:00 (antes de 11:00)
    assert not t["emitted"].any()


def test_strict_equalities_do_not_pass():
    eq_c = table({"AAA": bars(session(REG, [100.5, 101.0, 101.0] + [100.0] * 10))}, flat_spy([REG]))
    assert not eq_c.loc[eq_c["decision_min"] == 660, "C"].iloc[0]
    eq_a = table({"AAA": bars(session(REG, [99.0, 99.5, 100.0] + [99.0] * 10))}, flat_spy([REG]))
    assert not eq_a.loc[eq_a["decision_min"] == 660, "A"].iloc[0]
    spy = bars([(REG, s, 200.0, 202.0 if s == "10:30" else 200.0) for s in SLOTS])          # 202/200 == 101/100
    eq_b = table({"AAA": bars(session(REG, [100.5, 100.2, 101.0] + [100.0] * 10))}, spy)
    row = eq_b[eq_b["decision_min"] == 660].iloc[0]
    assert row["A"] and row["C"] and not row["B"]


def test_pre_1100_qualifier_cannot_emit():
    t = table({"AAA": bars(session(REG, [100.5, 101.0, 100.8, 101.2] + [100.0] * 9))}, flat_spy([REG]))
    assert (t["decision_min"] >= 660).all()                     # la barra 10:00 (D=10:30) nunca es evaluable
    assert emitted_times(t) == ["11:00"]


def test_first_qualifier_emits_later_suppressed_one_per_session():
    t = table({"AAA": bars(session(REG, BASE))}, flat_spy([REG]))
    assert t["emitted"].sum() == 1
    sup = t[t["suppressed"]]
    assert list(sup["bar_timestamp"].map(lambda x: pd.Timestamp(x).tz_convert(NY).strftime("%H:%M"))) == ["11:00", "12:00"]
    assert not (sup["emitted"]).any() and sup["qualifies"].all()


def test_two_sessions_no_carry_over():
    rows = session(REG, BASE) + session("2024-03-13", BASE)
    t = h5.signal_table({"AAA": bars(rows)}, flat_spy([REG, "2024-03-13"]), date(2024, 3, 12), date(2024, 3, 13))
    assert t["emitted"].sum() == 2


# ================================================================ DATOS FALTANTES
def test_missing_stock_first_bucket_blocks_stock_session():
    rows = [r for r in session(REG, BASE) if r[1] != "09:30"]
    t = table({"AAA": bars(rows), "BBB": bars(session(REG, BASE))}, flat_spy([REG]))
    assert "AAA" not in set(t["symbol"]) and t.loc[t["symbol"] == "BBB", "emitted"].sum() == 1


def test_missing_spy_first_bucket_blocks_whole_session():
    spy = flat_spy([REG], slots=SLOTS[1:])
    t = table({"AAA": bars(session(REG, BASE)), "BBB": bars(session(REG, BASE))}, spy)
    assert t.empty


def test_missing_spy_t_bucket_blocks_that_evaluation_only():
    spy = flat_spy([REG], slots=[s for s in SLOTS if s != "10:30"])
    t = table({"AAA": bars(session(REG, BASE))}, spy)
    assert 660 not in set(t["decision_min"])
    assert emitted_times(t) == ["11:00"]                        # la siguiente barra evaluable puede emitir


def test_no_alternate_anchor():
    rows = [r for r in session(REG, BASE) if r[1] != "09:30"]  # 10:00 existe pero NO se usa como ancla
    assert table({"AAA": bars(rows)}, flat_spy([REG])).empty


# ================================================================ HORARIO
def _late(day, n, qualify_slot):
    closes = [100.0] * n
    k = SLOTS.index(qualify_slot)
    closes[k] = 110.0
    return session(day, closes, open0=100.0)


def test_latest_full_session_decision_1500_allowed_later_rejected():
    t = table({"AAA": bars(_late(REG, 13, "14:30"))}, flat_spy([REG]))
    assert emitted_times(t) == ["14:30"] and t["decision_min"].max() == 15 * 60
    t2 = table({"AAA": bars(_late(REG, 13, "15:00"))}, flat_spy([REG]))
    assert not t2["emitted"].any() and t2["decision_min"].max() == 15 * 60


def test_early_close_1200_allowed_later_rejected_and_schedule_governs():
    t = table({"AAA": bars(_late(EARLY, 7, "11:30"))}, flat_spy([EARLY], n=7), day=EARLY)
    assert emitted_times(t) == ["11:30"] and t["decision_min"].max() == 12 * 60
    rows = _late(EARLY, 9, "12:00")                                  # barras observadas después de 13:00 no cambian el horario
    t2 = table({"AAA": bars(rows)}, flat_spy([EARLY], n=9), day=EARLY)
    assert not t2["emitted"].any() and t2["decision_min"].max() == 12 * 60
    trunc = _late(REG, 7, "12:00")                                   # sesión regular truncada: cierre programado 16:00
    t3 = table({"AAA": bars(trunc)}, flat_spy([REG], n=7))
    assert emitted_times(t3) == ["12:00"]


def test_scheduled_close_from_frozen_list():
    assert h5.scheduled_close_min(date(2024, 7, 3)) == 13 * 60 and h5.scheduled_close_min(date(2024, 3, 12)) == 16 * 60
    assert h5.decision_in_window(date(2024, 3, 12), 660) and not h5.decision_in_window(date(2024, 3, 12), 630)
    assert h5.decision_in_window(date(2024, 7, 3), 720) and not h5.decision_in_window(date(2024, 7, 3), 750)


# ================================================================ ENTRADA / SALIDA
def _obs(df, bar_hhmm, day=REG):
    R = np.full(len(df), 2.0)
    pos = {int(v): k for k, v in enumerate(df.index.as_unit("ns").asi8)}
    t = list(df.index).index(ts(day, bar_hhmm))
    return h5.observation(df, t, R, pos)


def test_e1_e2_exact_buckets_and_cost_formula():
    df = bars(session(REG, BASE))
    o = _obs(df, "10:30")
    assert o["valid"] and o["open_E1"] == df.loc[ts(REG, "11:00"), "open"]
    assert o["close_E1"] == df.loc[ts(REG, "11:00"), "close"] and o["close_E2"] == df.loc[ts(REG, "11:30"), "close"]
    for bps in (0.0, 2.5, 5.0, 10.0):
        c = bps / 1e4
        assert h5.fwd_r(o, bps) == pytest.approx((o["close_E2"] * (1 - c) - o["open_E1"] * (1 + c)) / 2.0)
    x1, x2 = (o["close_E1"] - o["open_E1"]) / 2.0, (o["close_E2"] - o["open_E1"]) / 2.0
    assert o["mfe_60"] == max(x1, x2) and o["mae_60"] == -min(x1, x2)


def test_missing_e1_or_e2_invalid_without_substitution():
    base = session(REG, BASE)
    no_e1 = bars([r for r in base if r[1] != "11:00"])
    assert _obs(no_e1, "10:30") == {"valid": False, "invalid_reason": "missing_E1"}
    no_e2 = bars([r for r in base if r[1] != "11:30"])
    assert _obs(no_e2, "10:30") == {"valid": False, "invalid_reason": "missing_E2"}   # 12:00 no sustituye


def test_emitted_signal_with_missing_e1_stays_emitted_but_invalid():
    rows = [r for r in session(REG, BASE) if r[1] != "11:00"]
    u = h5.Universe({"AAA": bars(rows)}, flat_spy([REG]), date(2024, 3, 12), date(2024, 3, 12)).prepare()
    pop = h5.build_populations(u)
    assert len(pop["emitted"]) == 1 and pop["valid"] == []


# ================================================================ R
def test_atr14_exact_and_r_multiple():
    rows = []
    rng = np.random.default_rng(3)
    prices = 100 + np.cumsum(rng.normal(0, 1, 20))
    for k in range(20):
        rows.append((REG, SLOTS[k % 13] if k < 13 else SLOTS[k - 13], 0, 0))
    idx = pd.date_range(ts(REG, "09:30"), periods=20, freq="30min")
    h = prices + 1.3
    l = prices - 0.7
    c = prices + 0.2
    df = pd.DataFrame({"open": prices, "high": h, "low": l, "close": c}, index=idx)
    atr = h5.atr14(df)
    assert np.isnan(atr[:14]).all()
    for T in (14, 19):
        trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(T - 13, T + 1)]
        assert atr[T] == pytest.approx(sum(trs) / 14, rel=1e-12)
    assert h5.r_per_share(df)[19] == pytest.approx(2 * atr[19])


def test_r_uses_only_bars_through_t_and_invalid_r():
    idx = pd.date_range(ts(REG, "09:30"), periods=30, freq="30min")
    base = pd.DataFrame({"open": np.linspace(100, 110, 30), "high": np.linspace(101, 111, 30),
                         "low": np.linspace(99, 109, 30), "close": np.linspace(100.5, 110.5, 30)}, index=idx)
    mod = base.copy()
    mod.iloc[21:] *= 3.0
    assert h5.r_per_share(base)[20] == h5.r_per_share(mod)[20]
    flat = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}, index=idx)
    assert np.isnan(h5.r_per_share(flat)).all()                  # R = 0 -> inválida


def test_r_unchanged_across_costs():
    o = _obs(bars(session(REG, BASE)), "10:30")
    assert all(h5.fwd_r(dict(o), b) * o["R"] + o["open_E1"] * (1 + b / 1e4) == pytest.approx(o["close_E2"] * (1 - b / 1e4))
               for b in h5.PRECHECK_BPS)


# ================================================================ SIN MIRAR ADELANTE
def test_signal_invariant_to_data_after_d_and_outcome_can_change():
    a = session(REG, BASE)
    b = [(d, s, o, c * (2.0 if s >= "11:00" else 1.0)) for d, s, o, c in a]
    ta, tb = table({"AAA": bars(a)}, flat_spy([REG])), table({"AAA": bars(b)}, flat_spy([REG]))
    ea, eb = ta[ta["emitted"]], tb[tb["emitted"]]
    assert list(ea["bar_timestamp"]) == list(eb["bar_timestamp"])
    ra_, rb_ = ta[ta["decision_min"] == 660].iloc[0], tb[tb["decision_min"] == 660].iloc[0]
    assert (ra_["A"], ra_["B"], ra_["C"]) == (rb_["A"], rb_["B"], rb_["C"])
    oa, ob = _obs(bars(a), "10:30"), _obs(bars(b), "10:30")
    assert oa["close_E2"] != ob["close_E2"]


# ================================================================ UNIVERSO SINTÉTICO MULTISESIÓN
def _universe(n_days=40, syms=("AAA", "BBB", "CCC"), seed=11, start="2024-01-02"):
    days = [d.date().isoformat() for d in pd.bdate_range(start, periods=n_days) if d.date() not in (date(2024, 1, 15), date(2024, 2, 19))]
    rng = np.random.default_rng(seed)
    stocks = {}
    for k, s in enumerate(syms):
        rows, px = [], 100.0
        for d in days:
            for sl in SLOTS:
                o = px
                c = o * (1 + rng.normal(0.0004 * (k + 1), 0.004))
                rows.append((d, sl, o, c))
                px = c
        df = bars(rows)
        df["high"] = df[["open", "close"]].max(axis=1) * (1 + np.abs(rng.normal(0, 0.002, len(df))))
        df["low"] = df[["open", "close"]].min(axis=1) * (1 - np.abs(rng.normal(0, 0.002, len(df))))
        stocks[s] = df
    spy_rows, px = [], 400.0
    for d in days:
        for sl in SLOTS:
            c = px * (1 + rng.normal(0.0001, 0.002))
            spy_rows.append((d, sl, px, c))
            px = c
    return h5.Universe(stocks, bars(spy_rows), date.fromisoformat(days[0]), date.fromisoformat(days[-1]))


@pytest.fixture(scope="module")
def evaluated():
    return h5.evaluate(_universe(), n_rep=200, jaccard=0.9, integrity=True, no_lookahead=True, reproduction=True)


def test_population_separation_and_visibility(evaluated):
    pop, m = evaluated["populations"], evaluated["matching"]
    assert len(pop["valid"]) <= len(pop["emitted"])
    assert len(m["matched"]) + len(m["unmatched"]) == len(pop["valid"])
    tr = evaluated["transparency"]
    assert tr["emitted_signal_count"] == len(pop["emitted"]) and tr["valid_real_count"] == len(pop["valid"])
    assert tr["matched_real_count"] == len(m["matched"]) and tr["unmatched_real_count"] == len(m["unmatched"])
    for k in ("fallback_usage", "quintile_minus1_count", "suppressed_qualifier_count", "suppressed_qualifier_pool_share",
              "suppressed_qualifier_draw_count", "invalid_observation_counts", "symbol_distribution",
              "decision_time_distribution", "atr_pct_distribution", "unmatched_by_symbol", "unmatched_by_month",
              "unmatched_by_decision_time"):
        assert k in tr


def test_quintile_minus_one_in_short_history_goes_to_l4(evaluated):
    pop, m = evaluated["populations"], evaluated["matching"]
    first_ranked = h5.ATR_WINDOW + h5.ATR_RANK_WINDOW             # primer índice con 260 ATR% válidos previos
    assert all((o["quintile"] == -1) == (o["t"] < first_ranked) for o in pop["valid"])
    early = [o for o in m["matched"] + m["unmatched"] if o["quintile"] == -1]
    assert early and all(o["match_level"] in (4, -1) for o in early)
    assert any(o["quintile"] >= 0 for o in pop["valid"])
    tr = evaluated["transparency"]
    assert tr["quintile_minus1_count"] == sum(o["quintile"] == -1 for o in pop["valid"])


def test_candidates_exclude_emitted_keep_suppressed_and_ignore_abc(evaluated):
    pop = evaluated["populations"]
    em = {(o["symbol"], o["bar_timestamp"]) for o in pop["emitted"]}
    cand = pop["candidates"]
    assert not em & {(c["symbol"], c["bar_timestamp"]) for c in cand}
    tab = pop["table"]
    sup = {(s, b) for s, b in zip(tab.loc[tab["suppressed"], "symbol"], tab.loc[tab["suppressed"], "bar_timestamp"])}
    keys = {(c["symbol"], c["bar_timestamp"]) for c in cand}
    missing = sup - keys
    assert sup and missing != sup
    t_of = {(s, b): t for s, b, t in zip(tab["symbol"], tab["bar_timestamp"], tab["t"])}
    assert all(t_of[k] < h5.ATR_WINDOW for k in missing)            # solo faltan las de R inválida (sin historia ATR)
    nonq = tab[~tab["qualifies"]]
    assert any((s, b) in keys for s, b in zip(nonq["symbol"], nonq["bar_timestamp"]))   # A/B/C falsos también son candidatos


def test_p2_p3_use_only_matched_population(evaluated):
    m = evaluated["matching"]
    c0 = evaluated["gates"]["comparison_0bps"]
    assert c0["real"] == h5.sample_stats(m["matched"], 0.0)
    g = evaluated["gates"]["gates"]
    reps = [x["median_fwdR_60"] for x in c0["replicates"]]
    assert g["P2"]["value"] == ps.directional_percentile(c0["real"]["median_fwdR_60"], reps, True)
    assert [x["name"] for x in g["P3"]["metrics"]] == ["mean_fwdR_60", "median_mfe_60", "median_mae_60"]
    assert [x["higher_is_better"] for x in g["P3"]["metrics"]] == [True, True, False]


def test_replicates_one_draw_per_matched_and_deterministic(evaluated):
    reps, m = evaluated["replicates"], evaluated["matching"]
    assert len(reps) == 200 and all(len(d) == len(m["matched"]) for d in reps)
    again = h5.replicates(m, 200)
    assert [[(c["symbol"], c["bar_timestamp"]) for c in d] for d in again] == \
           [[(c["symbol"], c["bar_timestamp"]) for c in d] for d in reps]


def test_p4b_uses_same_population_and_draws(evaluated):
    g = evaluated["gates"]
    c5 = g["comparison_5bps"]
    assert c5["real"] == h5.sample_stats(evaluated["matching"]["matched"], 5.0)
    assert [x["n"] for x in c5["replicates"]] == [x["n"] for x in g["comparison_0bps"]["replicates"]]
    p4 = g["gates"]["P4"]
    reps5 = [x["median_fwdR_60"] for x in c5["replicates"]]
    assert p4["canonical_cost_primary_percentile"] == ps.directional_percentile(c5["real"]["median_fwdR_60"], reps5, True)
    assert p4["friction_over_favorable_move"] == pytest.approx(0.10 / h5.p4a_median_favorable_move(evaluated["populations"]["valid"]))


def test_admission_record_structure(evaluated):
    adm = evaluated["gates"]["admission"]
    assert set(adm["gate_outcomes"]) == {"P1", "P2", "P3", "P4", "P5", "P6"}
    assert adm["gate_outcomes"]["P1"] in ("PASS", "FAIL")
    assert evaluated["gates"]["gates"]["P5"]["outcome"] == "PASS"   # jaccard sintético 0.9, P5 aplicable


def test_unmeasured_gates_fail():
    ev = h5.evaluate(_universe(n_days=12), n_rep=5)
    adm = ev["gates"]["admission"]
    assert adm["gate_outcomes"]["P5"] == "FAIL" and adm["gate_outcomes"]["P6"] == "FAIL"


# ================================================================ EMPAREJAMIENTO (poblaciones construidas a mano)
def _o(sym, month, slot, q, **kw):
    base = {"symbol": sym, "bar_timestamp": f"{sym}-{month}-{slot}-{q}-{kw.get('tag', '')}", "month": month, "slot": slot,
            "quintile": q, "decision_min": 660 + 30 * slot, "suppressed": kw.get("suppressed", False), "valid": True,
            "R": 1.0, "open_E1": 100.0, "close_E1": 100.0 + kw.get("d", 0.0), "close_E2": 100.0 + kw.get("d", 0.0),
            "mfe_60": kw.get("d", 0.0), "mae_60": -kw.get("d", 0.0), "atr_pct": 1.0}
    return base


def test_match_levels_l0_to_l4_and_unmatched():
    cands = [_o("AAA", "2024-03", 2, 3, tag="c0"), _o("AAA", "2024-03", 1, 1, tag="c1"),
             _o("AAA", "2024-03", 5, 4, tag="c2"), _o("AAA", "2024-03", 7, 0, tag="c3"), _o("AAA", "2024-03", 8, 2, tag="c4")]
    real = [_o("AAA", "2024-03", 2, 3), _o("AAA", "2024-03", 2, 1), _o("AAA", "2024-03", 5, 3),
            _o("AAA", "2024-03", 6, 1), _o("AAA", "2024-03", 8, 4), _o("AAA", "2024-03", 8, -1), _o("AAA", "2024-04", 2, 3)]
    m = h5.match({"valid": real, "candidates": cands})
    lv = {(o["slot"], o["quintile"], o["month"]): o["match_level"] for o in m["matched"] + m["unmatched"]}
    assert lv[(2, 3, "2024-03")] == 0                    # L0 exacto
    assert lv[(2, 1, "2024-03")] == 1                    # L1: slot 1 mismo quintil
    assert lv[(5, 3, "2024-03")] == 2                    # L2: quintil 4 mismo slot
    assert lv[(6, 1, "2024-03")] == 3                    # L3: slot 7 quintil 0
    assert lv[(8, 4, "2024-03")] == 4                    # L4: slot 8 cualquier quintil
    assert lv[(8, -1, "2024-03")] == 4                   # quintil −1 -> directo a L4
    assert lv[(2, 3, "2024-04")] == -1                   # otro mes: sin emparejar (visible)
    assert len(m["unmatched"]) == 1 and m["fallback_usage"] == {"L0": 1, "L1": 1, "L2": 1, "L3": 1, "L4": 2, "unmatched": 1}


def test_quintile_minus_one_skips_l0_to_l3():
    cands = [_o("AAA", "2024-03", 2, -1, tag="exact"), _o("AAA", "2024-03", 2, 3, tag="other")]
    m = h5.match({"valid": [_o("AAA", "2024-03", 2, -1)], "candidates": cands})
    assert m["matched"][0]["match_level"] == 4 and len(m["by_symbol"]["AAA"]["pools"][0]) == 2


def test_replacement_seed_formula_and_symbol_index_j():
    cands = [_o("BBB", "2024-03", 0, 0, tag="only")] + [_o("CCC", "2024-03", 0, 0, tag=f"x{k}") for k in range(5)]
    real = [_o("AAA", "2024-09", 0, 0)] + [_o("BBB", "2024-03", 0, 0)] * 3 + [_o("CCC", "2024-03", 0, 0)] * 2
    m = h5.match({"valid": real, "candidates": cands})
    assert m["symbols"] == ["AAA", "BBB", "CCC"] and len(m["unmatched"]) == 1     # AAA sin pool pero conserva j = 0
    reps = h5.replicates(m, 3)
    assert all(len(d) == 5 for d in reps)
    assert [c["bar_timestamp"] for c in reps[0][:3]] == [cands[0]["bar_timestamp"]] * 3       # con reemplazo
    for r in range(3):
        rng = np.random.default_rng(20260925 + 1000 * r + 2)                          # CCC: j = 2
        u = rng.random(2)
        expect = [cands[1 + int(x * 5)]["bar_timestamp"] for x in u]
        assert [c["bar_timestamp"] for c in reps[r][3:]] == expect


# ================================================================ MÉTRICAS / COSTOS
def test_sample_stats_and_cost_rows():
    obs = [_o("A", "m", 0, 0, d=d) for d in (-1.0, 0.5, 2.0, 3.0)]
    s = h5.sample_stats(obs, 0.0)
    assert s["median_fwdR_60"] == 1.25 and s["mean_fwdR_60"] == 1.125
    assert s["median_mfe_60"] == 1.25 and s["median_mae_60"] == -1.25
    row = h5.cost_row(obs, 0.0)
    assert row["pf"] == pytest.approx(5.5 / 1.0) and row["win_rate_pct"] == 75.0
    c = h5.cost_row(obs, 10.0)
    assert c["mean_fwdR_60"] == pytest.approx(statistics_mean([h5.fwd_r(o, 10.0) for o in obs]))


def statistics_mean(xs):
    return sum(xs) / len(xs)


def test_break_even_interpolation_and_precheck():
    obs = [_o("A", "m", 0, 0, d=0.1)] * 3                  # fwdR = 0.1 − costos; R = 1, precios 100
    pc = h5.cost_precheck(obs)
    assert [r["slippage_bps"] for r in pc["rows"]] == [0.0, 2.5, 5.0, 7.5, 10.0]
    be = pc["break_even_mean"]["bps"]
    assert be is not None and h5.fwd_r(obs[0], be) == pytest.approx(0.0, abs=1e-9)


def test_p4a_uses_successful_valid_observations():
    obs = [_o("A", "m", 0, 0, d=2.0), _o("A", "m", 0, 0, d=-1.0), _o("A", "m", 0, 0, d=4.0)]
    assert h5.p4a_median_favorable_move(obs) == pytest.approx((2.0 * 1 / 100 * 100 + 4.0 * 1 / 100 * 100) / 2)


# ================================================================ RESAMPLER
def test_resample_30min_anchored_and_early_close():
    idx = pd.date_range(ts(EARLY, "09:30"), ts(EARLY, "14:00"), freq="1min", inclusive="left")
    df1 = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    b = h5.resample_30min(df1)
    times = [t.tz_convert(NY).strftime("%H:%M") for t in b.index]
    assert times[0] == "09:30" and times[-1] == "12:30" and len(b) == 7 and (b["n_minutes"] == 30).all()


# ================================================================ P5
def test_feed_overlap_jaccard_sets_and_near_match():
    a = {("AAA", ts(REG, "11:00").isoformat()), ("AAA", ts(REG, "13:00").isoformat()), ("BBB", ts(REG, "12:00").isoformat())}
    b = {("AAA", ts(REG, "11:00").isoformat()), ("AAA", ts(REG, "13:30").isoformat())}
    o = h5.feed_overlap(a, b)
    assert o["exact_matches"] == 1 and o["a_only_count"] == 2 and o["b_only_count"] == 1
    assert o["jaccard"] == pytest.approx(1 / 4) and o["band"] == "MATERIAL_FEED_SENSITIVITY"
    assert o["a_only_with_b_within_30min"] == 1 and o["b_only_with_a_within_30min"] == 1


def test_feed_bundles_never_mix_and_known_only():
    k = "2026-06-02"
    s_iex, spy_iex = bars(session(k, BASE)), flat_spy([k])
    s_sip, spy_sip = bars(session(k, BASE)), flat_spy([k])
    for df, f in ((s_iex, "iex"), (spy_iex, "iex"), (s_sip, "sip"), (spy_sip, "sip")):
        df.attrs["feed"] = f
    res = h5.p5_compare(h5.FeedBundle("iex", {"AAA": s_iex}, spy_iex), h5.FeedBundle("sip", {"AAA": s_sip}, spy_sip),
                        date(2026, 6, 2), date(2026, 6, 2))
    assert res["overlap"]["jaccard"] == 1.0 and "SPY" in res["common_bucket_ohlc"]
    with pytest.raises(h5.H005Error):
        h5.p5_compare(h5.FeedBundle("iex", {"AAA": s_iex}, spy_sip), h5.FeedBundle("sip", {"AAA": s_sip}, spy_sip),
                      date(2026, 6, 2), date(2026, 6, 2))
    with pytest.raises(h5.H005Error):
        h5.p5_compare(h5.FeedBundle("iex", {"AAA": s_iex}, spy_iex), h5.FeedBundle("iex", {"AAA": s_iex}, spy_iex),
                      date(2026, 6, 2), date(2026, 6, 2))
    with pytest.raises(h5.ra.AuditHygieneError):
        h5.p5_compare(h5.FeedBundle("iex", {"AAA": s_iex}, spy_iex), h5.FeedBundle("sip", {"AAA": s_sip}, spy_sip),
                      date(2026, 1, 5), date(2026, 1, 6))


def test_missing_bucket_diagnostics():
    k = "2026-06-02"
    b = h5.FeedBundle("iex", {"AAA": bars([r for r in session(k, BASE) if r[1] != "09:30"])}, flat_spy([k]))
    d = h5.missing_bucket_sessions(b, date(2026, 6, 2), date(2026, 6, 2))
    assert d["stock_first_bucket_missing"]["AAA"] == ["2026-06-02"] and d["spy_first_bucket_missing"] == []


# ================================================================ DETERMINISMO / HIGIENE / CONSTANTES
def test_deterministic_evaluation():
    a = h5.evaluate(_universe(n_days=15), n_rep=20)
    b = h5.evaluate(_universe(n_days=15), n_rep=20)
    assert a["transparency"] == b["transparency"]
    assert a["gates"]["comparison_0bps"] == b["gates"]["comparison_0bps"]


def test_frozen_constants():
    assert (h5.EARLIEST_DECISION_MIN, h5.LATEST_DECISION_BEFORE_CLOSE_MIN, h5.BAR_MINUTES) == (660, 60, 30)
    assert (h5.ATR_WINDOW, h5.R_MULTIPLE, h5.ATR_RANK_WINDOW) == (14, 2.0, 260)
    assert (h5.N_REPLICATES, h5.BASE_SEED, h5.CANONICAL_BPS, h5.PRECHECK_BPS) == (200, 20260925, 5.0, (0.0, 2.5, 5.0, 7.5, 10.0))
    assert h5.FRICTION_PCT == pytest.approx(0.10) and h5.FROZEN_SPEC_COMMIT.startswith("29e5648")
    assert h5.FEEDS == {"PRIMARY_RESEARCH_FEED": "iex", "INTENDED_LIVE_SIGNAL_FEED": "iex", "SECONDARY_ROBUSTNESS_FEED": "sip"}


def test_development_loader_refuses_outside_windows(monkeypatch):
    monkeypatch.setattr(h5, "DEV", (date(2026, 1, 2), date(2026, 5, 29)))
    with pytest.raises(h5.ra.AuditHygieneError):
        h5.load_development_universe(None, None, None, None)
