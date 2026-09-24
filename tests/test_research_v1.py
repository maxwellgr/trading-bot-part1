"""
Protocolo de investigación, auditoría de datos, manifiesto, cobertura entre
símbolos, benchmark por período y extensiones del descargador. Sin red.
"""
import copy
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src import broker_alpaca, historical_audit as ha, historical_download as hd, research_benchmark as rb
from src.backtest_engine import BacktestConfig, production_args, run_backtest
from src.backtest_report import summarize, write_outputs
from src.research_protocol import (
    ProtocolError, contaminated_overlap, evidence_label, get_split, load_protocol, require_use, validate_protocol,
)

NY = "America/New_York"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("sin red")
    for fn in ("get", "post", "delete"):
        monkeypatch.setattr(broker_alpaca.requests, fn, boom)
        monkeypatch.setattr(hd.requests, fn, boom)


@pytest.fixture
def protocol():
    return load_protocol()


# ================================================================ protocolo
def test_repo_protocol_parses_with_expected_splits(protocol):
    assert protocol["protocol_version"] == "research_protocol_v1"
    assert [s["name"] for s in protocol["splits"]] == ["warmup", "development", "validation", "known_diagnostic", "forward"]
    assert get_split(protocol, "known_diagnostic")["role"] == "contaminated"
    assert (get_split(protocol, "validation")["start"], get_split(protocol, "validation")["end"]) == ("2026-01-02", "2026-05-29")
    assert protocol["execution_defaults"] == {"fill_model": "next_bar_open", "slippage_bps": 5.0,
                                              "commission_per_fill": 0.0, "initial_equity": 100000.0}
    assert protocol["data_source"]["feed"] == "iex" and protocol["data_source"]["adjustment"] == "raw"
    assert protocol["universe"]["symbols"] == ["NVDA", "AMD", "PLTR", "HOOD", "MARA", "INTC", "MU", "META"]


def _mutate(protocol, fn):
    p = copy.deepcopy(protocol)
    fn(p)
    return p


@pytest.mark.parametrize("breaker,msg", [
    (lambda p: p["splits"][2].update(start="2025-12-15"), "cronológico"),              # solapa development
    (lambda p: p["splits"].reverse(), "cronológico|abierto|forward"),
    (lambda p: p["splits"][1].update(end="2023-12-15"), "end < start|cronológico"),
    (lambda p: p["splits"][3].update(role="validation"), "exactamente un split"),       # contaminado como validación
    (lambda p: p["splits"][2].update(role="oos"), "rol desconocido"),
    (lambda p: p["splits"][1].update(end=None), "solo forward"),
    (lambda p: p["splits"].pop(), "exactamente un split con rol 'forward'"),
    (lambda p: p["splits"][1].update(name="warmup"), "repetidos"),
    (lambda p: p.pop("hygiene_rules"), "hygiene_rules"),
    (lambda p: p["universe"].update(symbols=["A", "A"]), "duplicados"),
    (lambda p: p["splits"][1].update(start="2024-13-01"), "fecha inválida"),
])
def test_invalid_protocols_are_rejected(protocol, breaker, msg):
    with pytest.raises(ProtocolError, match=msg):
        validate_protocol(_mutate(protocol, breaker))


def test_contaminated_period_can_never_be_used_as_validation(protocol):
    with pytest.raises(ProtocolError, match="no puede usarse para 'frozen_validation'"):
        require_use(protocol, "known_diagnostic", "frozen_validation")
    with pytest.raises(ProtocolError):
        require_use(protocol, "known_diagnostic", "tuning")
    with pytest.raises(ProtocolError):
        require_use(protocol, "validation", "tuning")
    with pytest.raises(ProtocolError):
        require_use(protocol, "warmup", "benchmark")
    with pytest.raises(ProtocolError):
        require_use(protocol, "nope", "benchmark")
    assert require_use(protocol, "validation", "frozen_validation")["role"] == "validation"
    assert require_use(protocol, "development", "tuning")["role"] == "development"


def test_evidence_labels_and_contamination_overlap(protocol):
    labels = {s["name"]: evidence_label(s) for s in protocol["splits"]}
    assert labels["development"] == "DEVELOPMENT EVIDENCE" and labels["validation"] == "VALIDATION EVIDENCE"
    assert labels["known_diagnostic"] == "KNOWN/CONTAMINATED EVIDENCE" and labels["forward"] == "FORWARD EVIDENCE"
    assert contaminated_overlap(protocol, "2026-05-01", "2026-06-05") == ["known_diagnostic"]
    assert contaminated_overlap(protocol, "2026-01-02", "2026-05-29") == []
    assert contaminated_overlap(protocol, "2026-09-23", "2026-12-31") == ["known_diagnostic"]


def test_registry_baseline_matches_production_defaults():
    reg = json.loads(open("research/strategy_registry_v1.json", encoding="utf-8").read())
    [base] = [e for e in reg["entries"] if e["id"] == "MA_BASELINE_V1"]
    prod = vars(production_args())
    assert {k: prod[k] for k in base["parameters"]} == base["parameters"]
    assert set(reg["entry_schema"]) >= {"id", "description", "code_commit", "parameters", "development_result",
                                        "validation_result", "frozen_date", "validation_viewed_before_parameter_changes"}


# ================================================================ auditoría
def _utc(day, hhmm):
    return pd.Timestamp(f"{day} {hhmm}", tz=NY).tz_convert("UTC")


def _bars(rows):
    """rows: (day, 'HH:MM', close[, dict overrides])"""
    out = []
    for r in rows:
        day, hhmm, c = r[:3]
        rec = {"timestamp": _utc(day, hhmm).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c, "high": c + 0.1,
               "low": c - 0.1, "close": c, "volume": 100.0, "symbol": "AAA"}
        rec.update(r[3] if len(r) > 3 else {})
        out.append(rec)
    return pd.DataFrame(out)


def _session(day, minutes=390, start="09:30", price=10.0, step=1):
    t0 = pd.Timestamp(f"{day} {start}")
    return [(day, (t0 + pd.Timedelta(minutes=m)).strftime("%H:%M"), price) for m in range(0, minutes, step)]


def test_integrity_counts_duplicates_order_ohlc_prices_volume_and_nans():
    df = _bars([("2024-01-02", "09:30", 10.0), ("2024-01-02", "09:31", 10.0), ("2024-01-02", "09:31", 10.0),
                ("2024-01-02", "09:30", 10.0),
                ("2024-01-02", "09:33", 10.0, {"low": 10.5}),                 # low > open/close
                ("2024-01-02", "09:34", 10.0, {"high": 9.0}),                 # high < open y < low
                ("2024-01-02", "09:35", -1.0, {"high": 0.0, "low": -1.1}),   # precio <= 0
                ("2024-01-02", "09:36", 10.0, {"volume": -5}),
                ("2024-01-02", "09:37", 10.0, {"close": np.nan})])
    r = ha.integrity(df)
    assert r["duplicate_timestamps"] == 2
    assert r["non_monotonic_timestamps"] == 1
    assert r["impossible_ohlc_bars"] == 2
    assert r["non_positive_prices"] == 1
    assert r["negative_volume"] == 1
    assert r["null_or_nan"]["close"] == 1 and r["null_or_nan_total"] == 1


def test_clean_bars_have_no_integrity_issues():
    r = ha.integrity(_bars(_session("2024-01-02", 30)))
    assert all(r[k] == 0 for k in ("duplicate_timestamps", "non_monotonic_timestamps", "impossible_ohlc_bars",
                                   "non_positive_prices", "negative_volume", "null_or_nan_total"))


def test_rth_coverage_full_sparse_partial_and_early_close():
    rows = (_session("2024-01-02") + [("2024-01-02", "08:00", 10.0), ("2024-01-02", "16:30", 10.0)]
            + _session("2024-01-03", 390, step=3)              # 130 de 390
            + _session("2024-01-04", 60, start="11:00")         # parcial: arranca 11:00
            + _session("2024-07-03", 210))                      # cierre anticipado 13:00 completo
    cov = ha.coverage_by_date(ha._frame(_bars(rows))).set_index("date")
    d = lambda s: date.fromisoformat(s)  # noqa: E731
    full = cov.loc[d("2024-01-02")]
    assert full["rth_bars"] == 390 and full["bars_total"] == 392 and full["rth_coverage_pct"] == 100.0
    assert not full["sparse"] and not full["partial_session"] and full["max_rth_gap_minutes"] == 0
    sparse = cov.loc[d("2024-01-03")]
    assert sparse["rth_bars"] == 130 and sparse["rth_coverage_pct"] == pytest.approx(130 / 390 * 100) and sparse["sparse"]
    part = cov.loc[d("2024-01-04")]
    assert part["partial_session"] and part["max_rth_gap_minutes"] == 240 and part["rth_gaps_ge_30min"] == 2
    early = cov.loc[d("2024-07-03")]
    assert early["early_close"] and early["expected_rth_minutes"] == 210 and early["rth_coverage_pct"] == 100.0
    assert not early["partial_session"]


def test_discontinuity_flags_unadjusted_split():
    rows = [("2024-06-07", "15:59", 1200.0), ("2024-06-10", "09:30", 120.0), ("2024-06-11", "09:30", 125.0)]
    [disc] = ha.discontinuities(ha._frame(_bars(rows)))
    assert disc["date"] == "2024-06-10" and disc["change_pct"] == pytest.approx(-90.0)
    assert disc["ratio_prev_over_open"] == pytest.approx(10.0)


def _write(root, sym, rows):
    d = root / "1Min"
    d.mkdir(parents=True, exist_ok=True)
    _bars(rows).assign(symbol=sym).to_csv(d / f"{sym}.csv", index=False)


def test_cross_symbol_common_dates_missing_symbols_and_reliability(tmp_path):
    _write(tmp_path, "AAA", _session("2024-01-02") + _session("2024-01-03") + _session("2024-01-04"))
    _write(tmp_path, "BBB", _session("2024-01-03", 390, step=4) + _session("2024-01-04"))   # 01-03 escaso
    audit = ha.run_audit(tmp_path, "1Min", ["AAA", "BBB"])
    c = audit["cross_symbol"]
    assert c["dates_any_symbol"] == 3 and c["dates_all_symbols"] == 2
    assert c["first_common_date"] == "2024-01-03" and c["dates_missing_symbols"] == {"2024-01-02": ["BBB"]}
    assert c["reliable_dates"] == 1 and c["earliest_reliable_date"] == "2024-01-04"
    assert any("BBB: 1 sparse" in w for w in audit["warnings"])


def test_large_gaps_and_weekdays_without_bars(tmp_path):
    _write(tmp_path, "AAA", _session("2024-01-02", 30) + _session("2024-01-03", 30) + _session("2024-01-10", 30))
    a = ha.run_audit(tmp_path, "1Min", ["AAA"])["per_symbol"]["AAA"]
    assert a["large_chronological_gaps"] == [{"from": "2024-01-03", "to": "2024-01-10", "weekdays_without_bars": 4}]
    assert a["weekdays_without_bars"] == ["2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
    assert a["missing_trading_days"] == a["weekdays_without_bars"]  # ninguno es feriado


def test_holidays_are_not_missing_days_but_unexpected_gaps_are(tmp_path):
    # 2024-01-15 = MLK (feriado); 2024-01-17 falta sin ser feriado; 2024-02-19 (feriado) con velas
    days = ["2024-01-12", "2024-01-16", "2024-01-18", "2024-02-19"]
    _write(tmp_path, "AAA", [r for d in days for r in _session(d, 30)])
    audit = ha.run_audit(tmp_path, "1Min", ["AAA"])
    a = audit["per_symbol"]["AAA"]
    assert "2024-01-15" in a["weekdays_without_bars"] and "2024-01-15" not in a["missing_trading_days"]
    assert "2024-01-17" in a["missing_trading_days"] and a["holidays_with_bars"] == ["2024-02-19"]
    assert any("not NYSE holidays" in w for w in audit["warnings"])
    assert any("bars on listed NYSE holiday" in w for w in audit["warnings"])


def test_early_close_check_confirms_listed_and_flags_unlisted(tmp_path):
    days = ["2024-07-01", "2024-07-02", "2024-07-03", "2024-07-05", "2024-07-08"]
    rows = []
    for d in days:
        rows += _session(d, 210 if d in ("2024-07-03", "2024-07-08") else 390)  # 07-08: se apaga sin estar listado
    _write(tmp_path, "AAA", rows)
    ec = ha.run_audit(tmp_path, "1Min", ["AAA"])["early_close_check"]
    assert ec["listed"] == [{"date": "2024-07-03", "after_13_bars_all_symbols": 0, "median_normal_day": 180.0,
                             "confirmed_by_data": True}]
    assert [e["date"] for e in ec["unlisted_candidates"]] == ["2024-07-08"]


def test_manifest_checksums_and_outputs(tmp_path, protocol):
    _write(tmp_path / "h", "AAA", _session("2024-01-02", 60))
    _write(tmp_path / "h", "BBB", _session("2024-01-02", 60) + _session("2024-01-03", 60))
    audit = ha.run_audit(tmp_path / "h", "1Min", ["AAA", "BBB"], protocol)
    man = ha.build_manifest(tmp_path / "h", "1Min", audit, protocol)
    import hashlib
    raw = (tmp_path / "h" / "1Min" / "BBB.csv").read_bytes()
    bbb = next(f for f in man["files"] if f["symbol"] == "BBB")
    assert bbb["sha256"] == hashlib.sha256(raw).hexdigest() and bbb["file_size_bytes"] == len(raw)
    assert bbb["row_count"] == 120 and bbb["filename"] == "1Min/BBB.csv"
    assert man["requested_research_period"] == {"start": "2023-12-01", "end_of_backtest_splits": "2026-09-23",
                                                "protocol_version": "research_protocol_v1"}
    assert man["actual_available_period"]["first_common_trading_date"] == "2024-01-02"
    paths = ha.write_audit(audit, man, tmp_path / "out", tmp_path / "h" / "manifest_v1.json")
    assert {p.name for p in paths} == {"historical_audit.json", "historical_audit.csv",
                                       "historical_coverage_by_date.csv", "manifest_v1.json"}
    saved = json.loads((tmp_path / "out" / "historical_audit.json").read_text(encoding="utf-8"))
    assert "_per" not in saved and saved["split_coverage"][1]["split"] == "development"
    assert "HISTORICAL DATA AUDIT" in ha.format_audit(audit)
    # determinista salvo la marca de tiempo de generación
    again = ha.build_manifest(tmp_path / "h", "1Min", ha.run_audit(tmp_path / "h", "1Min", ["AAA", "BBB"], protocol), protocol)
    assert again["files"] == man["files"]


def test_audit_is_read_only(tmp_path):
    _write(tmp_path, "AAA", _session("2024-01-02", 30) + [("2024-01-02", "09:31", 10.0)])  # con duplicado
    before = (tmp_path / "1Min" / "AAA.csv").read_bytes()
    a = ha.run_audit(tmp_path, "1Min", ["AAA", "ZZZ"])
    assert (tmp_path / "1Min" / "AAA.csv").read_bytes() == before
    assert a["per_symbol"]["AAA"]["duplicate_timestamps"] == 1 and any("missing file" in w for w in a["warnings"])


# ================================================================ descargador
class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.payload


def test_downloader_sends_raw_adjustment_and_retries_rate_limits():
    calls, sleeps = [], []
    seq = [_Resp({}, 429), _Resp({}, 503), _Resp({"bars": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 1, "l": 1,
                                                             "c": 1, "v": 1}], "next_page_token": None})]

    def fake_get(url, headers, params, timeout):
        calls.append(dict(params))
        return seq[len(calls) - 1]

    s, e = hd.date_range_utc("2024-01-02", "2024-01-02")
    bars = hd.fetch_bars("AAA", "1Min", s, e, fake_get, "https://x/v2", {}, sleep=sleeps.append)
    assert len(bars) == 1 and len(calls) == 3 and sleeps == [1.0, 2.0]
    assert calls[0]["feed"] == "iex" and calls[0]["adjustment"] == "raw"


def test_merge_keep_existing_never_rewrites_cached_bars_and_reports_overlap(tmp_path):
    path = tmp_path / "1Min" / "AAA.csv"
    old = _bars([("2024-01-02", "09:30", 10.0), ("2024-01-02", "09:31", 11.0)])
    hd.merge_into_cache(path, "AAA", old)
    before = pd.read_csv(path)
    new = _bars([("2024-01-02", "09:29", 9.0), ("2024-01-02", "09:31", 99.0)])    # 09:31 difiere
    st = {}
    assert hd.merge_into_cache(path, "AAA", new, keep="existing", stats=st) == 3
    after = pd.read_csv(path)
    assert st == {"existing": 2, "downloaded": 2, "overlap": 1, "overlap_differing": 1, "added": 1}
    kept = after.set_index("timestamp").loc[before["timestamp"]]
    assert (kept["close"].to_numpy() == before["close"].to_numpy()).all()
    hd.merge_into_cache(path, "AAA", new)  # política por defecto: prevalece la descarga (sin cambios)
    assert 99.0 in pd.read_csv(path)["close"].tolist()
    with pytest.raises(ValueError):
        hd.merge_into_cache(path, "AAA", new, keep="other")


# ================================================================ benchmark
def _mini_protocol(protocol):
    p = copy.deepcopy(protocol)
    p["universe"]["symbols"] = ["S0", "S1"]
    p["splits"] = [{"name": "warmup", "role": "data_support", "start": "2026-05-01", "end": "2026-05-31"},
                   {"name": "development", "role": "development", "start": "2026-06-01", "end": "2026-06-01"},
                   {"name": "validation", "role": "validation", "start": "2026-06-02", "end": "2026-06-02"},
                   {"name": "known_diagnostic", "role": "contaminated", "start": "2026-06-03", "end": "2026-06-03"},
                   {"name": "forward", "role": "forward", "start": "2026-06-04", "end": None}]
    return validate_protocol(p)


def _random_day_bars(seed, day):
    rng = np.random.default_rng(seed)
    closes = [round(v, 4) for v in 100 + np.cumsum(rng.normal(0, 0.25, 390))]
    opens = [closes[0]] + closes[:-1]
    t0 = pd.Timestamp(f"{day} 09:30", tz=NY).tz_convert("UTC")
    return pd.DataFrame({"timestamp": [(t0 + pd.Timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(390)],
                         "open": opens, "high": [max(o, c) + .3 for o, c in zip(opens, closes)],
                         "low": [min(o, c) - .3 for o, c in zip(opens, closes)], "close": closes,
                         "volume": 10_000.0})


def _write_hist(root):
    d = root / "1Min"
    d.mkdir(parents=True)
    for k, sym in enumerate(["S0", "S1"]):
        frames = [_random_day_bars(10 * k + i, day) for i, day in enumerate(["2026-06-01", "2026-06-02", "2026-06-03"])]
        pd.concat(frames).assign(symbol=sym).to_csv(d / f"{sym}.csv", index=False)


def test_benchmark_runs_each_period_separately_and_reuses_only_identical_runs(tmp_path, protocol, monkeypatch, capsys):
    mp = _mini_protocol(protocol)
    (tmp_path / "p.json").write_text(json.dumps(mp), encoding="utf-8")
    _write_hist(tmp_path / "hist")
    # corrida "validada" del período contaminado para reutilizar
    from src.historical_data import load_universe
    cfg = rb._backtest_config(mp, get_split(mp, "known_diagnostic"))
    s, e = hd.date_range_utc(cfg["start"], cfg["end"])
    data = load_universe(tmp_path / "hist", "1Min", cfg["symbols"], s - pd.Timedelta(days=4), e)
    r = run_backtest(BacktestConfig(**cfg), data.bars)
    write_outputs(r, summarize(r), tmp_path / "known")
    rc = rb.main(["--protocol", str(tmp_path / "p.json"), "--data-dir", str(tmp_path / "hist"),
                  "--output-dir", str(tmp_path / "out"), "--jobs", "1",
                  "--reuse", f"known_diagnostic={tmp_path / 'known'}"])
    assert rc == 0
    text = capsys.readouterr().out
    for label in ("DEVELOPMENT EVIDENCE", "VALIDATION EVIDENCE", "KNOWN/CONTAMINATED EVIDENCE", "FORWARD EVIDENCE"):
        assert label in text
    rep = json.loads((tmp_path / "out" / "benchmark_periods.json").read_text(encoding="utf-8"))
    assert [p["period"] for p in rep["periods"]] == ["development", "validation", "known_diagnostic"]
    assert rep["periods"][2]["source"].startswith("reused") and rep["periods"][0]["source"] == "run"
    assert rep["periods"][2]["trades"] == len(r.trades)
    assert [v["split"] for v in rep["validation_views"]] == ["validation"]
    assert rep["strategy_id"] == "MA_BASELINE_V1" and "combined" not in json.dumps(rep["periods"])
    dev_summary = json.loads((tmp_path / "out" / "periods" / "development" / "summary.json").read_text(encoding="utf-8"))
    assert dev_summary["config"]["start"] == "2026-06-01" and dev_summary["config"]["end"] == "2026-06-01"
    assert dev_summary["config"]["slippage_bps"] == 5.0
    by_sym = pd.read_csv(tmp_path / "out" / "benchmark_by_symbol.csv")
    assert len(by_sym) == 3 * 2 and list(by_sym["symbol"][:2]) == ["S0", "S1"]
    for p in rep["periods"]:
        assert sum(by_sym[by_sym["period"] == p["period"]]["trades"]) == p["trades"]
    for bad in ("best", "optimal"):
        assert bad not in text.lower()


def test_reuse_is_refused_when_config_differs(tmp_path, protocol):
    mp = _mini_protocol(protocol)
    _write_hist(tmp_path / "hist")
    cfg = rb._backtest_config(mp, get_split(mp, "known_diagnostic"))
    from src.historical_data import load_universe
    s, e = hd.date_range_utc(cfg["start"], cfg["end"])
    data = load_universe(tmp_path / "hist", "1Min", cfg["symbols"], s - pd.Timedelta(days=4), e)
    r = run_backtest(BacktestConfig(**dict(cfg, slippage_bps=7.5)), data.bars)
    write_outputs(r, summarize(r), tmp_path / "other")
    with pytest.raises(ValueError, match="slippage_bps"):
        rb.check_reusable(tmp_path / "other", cfg)


def test_benchmark_refuses_non_benchmark_splits(tmp_path, protocol, capsys):
    (tmp_path / "p.json").write_text(json.dumps(_mini_protocol(protocol)), encoding="utf-8")
    for name in ("warmup", "forward"):
        assert rb.main(["--protocol", str(tmp_path / "p.json"), "--periods", name, "--output-dir", str(tmp_path / "o")]) == 2
        assert "no puede usarse para 'benchmark'" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_benchmark_aggregation_is_deterministic(tmp_path, protocol):
    mp = _mini_protocol(protocol)
    _write_hist(tmp_path / "hist")
    runs = {}
    for name in ("development", "validation"):
        out = tmp_path / name
        rb._run_split((rb._backtest_config(mp, get_split(mp, name)), str(tmp_path / "hist"), str(out)))
        runs[name] = (out, "run")
    a = rb.aggregate(mp, runs)
    b = rb.aggregate(mp, dict(reversed(list(runs.items()))))
    assert a == b and [p["period"] for p in a["periods"]] == ["development", "validation"]
