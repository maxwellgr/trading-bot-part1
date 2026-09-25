"""Tests de preparación/auditoría de datos de contexto SPY para H004 (sin red, sin datos reales)."""
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import spy_context_data as scd
from src.historical_audit import sha256_file
from src.strategy_v2_h001 import resample_rth_5min

NY = "America/New_York"


# ---------------------------------------------------------------- fixtures
def minutes_frame(day: str, start: str = "09:30", end: str = "16:00", skip=(), symbol="SPY", price=100.0,
                  extra_ts=()) -> pd.DataFrame:
    """Velas 1Min [start, end) NY de un día (sin las horas en `skip`)."""
    idx = pd.date_range(pd.Timestamp(f"{day} {start}").tz_localize(NY),
                        pd.Timestamp(f"{day} {end}").tz_localize(NY), freq="1min", inclusive="left")
    idx = idx[[t.strftime("%H:%M") not in skip for t in idx]]
    idx = idx.append(pd.DatetimeIndex([pd.Timestamp(f"{day} {x}").tz_localize(NY) for x in extra_ts]))
    idx = idx.sort_values().tz_convert("UTC")
    n = len(idx)
    c = price + np.arange(n) * 0.01
    return pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": np.full(n, 100.0)},
                        index=pd.DatetimeIndex(idx, name="timestamp"))


def to_csv_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    out = df.reset_index()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["symbol"] = symbol
    return out[["timestamp", "open", "high", "low", "close", "volume", "symbol"]]


def write_csv(path: Path, df: pd.DataFrame, symbol: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    to_csv_frame(df, symbol).to_csv(path, index=False)


def stock_loader(days_by_symbol):
    """loader(sym) -> frame con sesiones RTH en los días indicados (y una vela pre-market cada día)."""
    def load(sym):
        frames = [minutes_frame(d, "09:30", "09:40", extra_ts=("08:00",)) for d in days_by_symbol.get(sym, [])]
        return pd.concat(frames) if frames else minutes_frame("2024-01-02", "08:00", "08:05")
    return load


# ---------------------------------------------------------------- 1. sesiones requeridas
def test_required_sessions_union_exclusion_and_hash():
    days = {"NVDA": ["2024-01-02", "2024-01-03"], "AMD": ["2024-01-03", "2024-01-05"]}
    r = scd.required_sessions(Path("unused"), start=date(2024, 1, 2), end=date(2024, 1, 5), loader=stock_loader(days))
    assert r["required_session_dates"] == ["2024-01-02", "2024-01-03", "2024-01-05"]
    assert r["required_session_count"] == 3
    assert r["excluded_zero_stock_coverage_dates"] == ["2024-01-04"]  # día hábil sin velas RTH de ningún símbolo
    assert r["required_session_dates_sha256"] == scd.dates_hash([date(2024, 1, 5), date(2024, 1, 2), date(2024, 1, 3)])
    assert r["definition"] == scd.REQUIRED_SESSION_DEFINITION


def test_out_of_session_stock_bars_do_not_make_a_date_required():
    def load(sym):
        return minutes_frame("2024-01-04", "07:00", "09:29") if sym == "NVDA" else minutes_frame("2024-01-04", "16:00", "17:00")
    r = scd.required_sessions(Path("x"), start=date(2024, 1, 4), end=date(2024, 1, 4), loader=load)
    assert r["required_session_dates"] == [] and r["excluded_zero_stock_coverage_dates"] == ["2024-01-04"]


def test_holiday_calendar_never_adds_dates_and_derivation_is_deterministic():
    days = {"MU": ["2024-01-02"]}
    load = stock_loader(days)
    a = scd.required_sessions(Path("x"), start=date(2024, 1, 1), end=date(2024, 1, 2), loader=load)
    b = scd.required_sessions(Path("x"), start=date(2024, 1, 1), end=date(2024, 1, 2), loader=load)
    assert a == b
    assert a["required_session_dates"] == ["2024-01-02"]
    assert "2024-01-01" not in a["excluded_zero_stock_coverage_dates"]  # feriado listado: ni requerido ni excluido


def test_dates_hash_is_order_independent_and_documented():
    d = [date(2024, 1, 3), date(2024, 1, 2)]
    assert scd.dates_hash(d) == scd.dates_hash(sorted(d))
    import hashlib
    assert scd.dates_hash(d) == hashlib.sha256(b"2024-01-02\n2024-01-03").hexdigest()


# ---------------------------------------------------------------- 2. rango y descarga
@pytest.mark.parametrize("s,e", [(date(2023, 12, 1), date(2026, 1, 2)), (date(2023, 11, 30), date(2025, 12, 31)),
                                 (date(2026, 1, 2), date(2026, 5, 29)), (date(2026, 6, 1), date(2026, 9, 23))])
def test_spy_range_guard_refuses_outside_development_support(s, e):
    with pytest.raises(scd.SpyContextError):
        scd.check_spy_range(s, e)


def test_spy_range_guard_accepts_frozen_range():
    scd.check_spy_range(date(2023, 12, 1), date(2025, 12, 31))


class FakeResp:
    def __init__(self, bars):
        self.status_code, self._bars = 200, bars

    def raise_for_status(self):
        pass

    def json(self):
        return {"bars": self._bars, "next_page_token": None}


def fake_get_factory(bars, calls):
    def get(url, headers=None, params=None, timeout=None):
        calls.append((url, dict(params)))
        return FakeResp(bars)
    return get


def api_bars(df):
    f = to_csv_frame(df, "SPY")
    return [{"t": r.timestamp, "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume} for r in f.itertuples()]


def test_download_requires_frozen_session_list_first(tmp_path):
    with pytest.raises(scd.SpyContextError, match="sessions"):
        scd.download_spy(tmp_path, get=fake_get_factory([], []), data_url="u", headers={})


def test_download_uses_iex_raw_and_never_requests_2026(tmp_path):
    (tmp_path / scd.SESSIONS_FILE).write_text(json.dumps({"required_session_dates_sha256": "x"}), encoding="utf-8")
    calls = []
    req = scd.download_spy(tmp_path, get=fake_get_factory(api_bars(minutes_frame("2024-01-02")), calls),
                           data_url="https://data.example/v2", headers={})
    url, params = calls[0]
    assert url == "https://data.example/v2/stocks/SPY/bars"
    assert params["feed"] == "iex" and params["adjustment"] == "raw" and params["timeframe"] == "1Min"
    end_ny = pd.Timestamp(params["end"]).tz_convert(NY)
    assert end_ny.date() == date(2025, 12, 31) and pd.Timestamp(params["start"]).tz_convert(NY).date() == date(2023, 12, 1)
    assert req["feed"] == "iex" and req["adjustment"] == "raw"
    assert (tmp_path / "1Min" / "SPY.csv").is_file()
    with pytest.raises(scd.SpyContextError, match="sobrescriben"):  # nunca sobrescribe
        scd.download_spy(tmp_path, get=fake_get_factory([], []), data_url="u", headers={})


def test_download_rejects_response_outside_range(tmp_path):
    (tmp_path / scd.SESSIONS_FILE).write_text("{}", encoding="utf-8")
    bars = api_bars(minutes_frame("2026-01-02", "09:30", "09:35"))
    with pytest.raises(scd.SpyContextError, match="rango"):
        scd.download_spy(tmp_path, get=fake_get_factory(bars, []), data_url="u", headers={})
    assert not (tmp_path / "1Min" / "SPY.csv").exists()


def test_freeze_sessions_refuses_if_spy_already_present(tmp_path):
    write_csv(tmp_path / "spy" / "1Min" / "SPY.csv", minutes_frame("2024-01-02"), "SPY")
    with pytest.raises(scd.SpyContextError, match="ANTES"):
        scd.freeze_sessions(tmp_path / "stock", tmp_path / "spy", tmp_path / "m.json")


# ---------------------------------------------------------------- 3. auditoría cruda
def test_raw_audit_counts_every_problem(tmp_path):
    f = to_csv_frame(minutes_frame("2024-01-02", "09:30", "09:40"), "SPY")
    f.loc[1, "timestamp"] = f.loc[0, "timestamp"]          # duplicado
    f.loc[5, "timestamp"], f.loc[6, "timestamp"] = f.loc[6, "timestamp"], f.loc[5, "timestamp"]  # fuera de orden
    f.loc[2, "high"] = f.loc[2, "low"] - 1                  # OHLC imposible
    f.loc[3, "low"] = 0.0                                   # precio no positivo (y OHLC)
    f.loc[4, "volume"] = -1                                 # volumen negativo
    f.loc[7, "close"] = np.nan                              # NaN
    p = tmp_path / "SPY.csv"
    f.to_csv(p, index=False)
    a = scd.audit_raw(p)
    assert a["duplicate_timestamps"] == 1 and a["non_monotonic_timestamps"] >= 1
    assert a["impossible_ohlc_bars"] >= 1 and a["non_positive_prices"] == 1 and a["negative_volume"] == 1
    assert a["null_or_nan_total"] == 1 and a["non_finite_values"] == 1 and a["integrity_pass"] is False


def test_raw_audit_passes_clean_file_and_flags_out_of_range(tmp_path):
    p = tmp_path / "SPY.csv"
    write_csv(p, minutes_frame("2024-01-02"), "SPY")
    a = scd.audit_raw(p)
    assert a["integrity_pass"] and a["row_count"] == 390 and a["sha256"] == sha256_file(p)
    write_csv(p, pd.concat([minutes_frame("2025-12-31"), minutes_frame("2026-01-02", "09:30", "09:31")]), "SPY")
    assert scd.audit_raw(p)["integrity_pass"] is False


# ---------------------------------------------------------------- 4. 15Min
def test_resample_generic_matches_frozen_5min_resampler():
    df = pd.concat([minutes_frame("2024-01-02", "08:00", "17:00", skip=("09:31", "10:07", "10:08", "15:59")),
                    minutes_frame("2024-07-03", "09:30", "16:00"), minutes_frame("2024-12-23", "09:30", "10:22")])
    pd.testing.assert_frame_equal(scd.resample_rth(df, 5), resample_rth_5min(df))


def test_15min_anchoring_rth_only_and_one_bar_makes_a_bucket():
    df = pd.concat([minutes_frame("2024-01-02", "04:00", "20:00", skip=tuple(f"10:{m:02d}" for m in range(0, 14))),
                    minutes_frame("2024-01-03", "09:44", "09:45")])
    b = scd.resample_rth(df)
    ny = b.index.tz_convert(NY)
    d2 = b[ny.date == date(2024, 1, 2)]
    assert len(d2) == 26  # 09:30..15:45; ext-hours excluidas
    assert d2.index.tz_convert(NY)[0].strftime("%H:%M") == "09:30" and d2.index.tz_convert(NY)[-1].strftime("%H:%M") == "15:45"
    t10 = pd.Timestamp("2024-01-02 10:00").tz_localize(NY).tz_convert("UTC")
    assert b.loc[t10, "n_minutes"] == 1  # 10:14 sola crea la cubeta 10:00–10:15 (sin mínimo)
    d3 = b[ny.date == date(2024, 1, 3)]
    assert len(d3) == 1 and d3.index.tz_convert(NY)[0].strftime("%H:%M") == "09:30"  # 09:44 -> 09:30–09:45
    first = df[df.index.tz_convert(NY).date == date(2024, 1, 2)]
    w = first[(first.index >= pd.Timestamp("2024-01-02 09:30", tz=NY)) & (first.index < pd.Timestamp("2024-01-02 09:45", tz=NY))]
    row = b.iloc[0]
    assert (row.open, row.high, row.low, row.close, row.volume) == (w.open.iloc[0], w.high.max(), w.low.min(),
                                                                    w.close.iloc[-1], w.volume.sum())
    assert scd.audit_15min(b)["pass"]


def test_early_close_ends_at_13_and_audit_detects_problems():
    b = scd.resample_rth(minutes_frame("2024-07-03", "09:30", "16:00"))
    a = scd.audit_15min(b)
    assert len(b) == 14 and a["last_bucket_start_on_early_close"] == {"2024-07-03": "12:45"}
    assert a["buckets_after_13_on_early_close"] == 0 and a["pass"]
    bad = b.copy()
    bad.index = bad.index + pd.Timedelta(minutes=5)  # desanclada
    assert scd.audit_15min(bad)["non_anchored_buckets"] == len(bad) and not scd.audit_15min(bad)["pass"]


def test_expected_buckets_full_and_early_close():
    assert len(scd.expected_bucket_starts(date(2024, 1, 2))) == 26
    e = scd.expected_bucket_starts(date(2024, 7, 3))
    assert len(e) == 14 and e[-1].tz_convert(NY).strftime("%H:%M") == "12:45"


# ---------------------------------------------------------------- 5. soporte
def sessions_15min(days):
    return scd.resample_rth(pd.concat([minutes_frame(d) for d in days]))


def test_support_exactly_200_before_development():
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-12-11", "2023-12-29") if d.date() != date(2023, 12, 25)]
    days += ["2024-01-02"]
    b = sessions_15min(days)
    s = scd.support_bars(b)
    assert s["support_bars"] == 200 and s["pass"] and s["available_before_development"] == 26 * 14
    assert pd.Timestamp(s["last_support_timestamp"]).tz_convert(NY).strftime("%Y-%m-%d %H:%M") == "2023-12-29 15:45"


def test_support_fewer_than_200_fails():
    b = sessions_15min(["2023-12-27", "2023-12-28", "2023-12-29", "2024-01-02"])
    s = scd.support_bars(b)
    assert s["support_bars"] == 78 and not s["pass"]


# ---------------------------------------------------------------- 6/7. cobertura
def test_session_coverage_zero_and_partial_gaps():
    df = pd.concat([minutes_frame("2024-01-02"),
                    minutes_frame("2024-01-03", skip=tuple(f"11:{m:02d}" for m in range(15, 45))),
                    minutes_frame("2024-01-05", "09:30", "10:22")])
    b = scd.resample_rth(df)
    req = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    c = scd.session_coverage(df, b, req)
    assert c["required_sessions_with_zero_spy_data"] == ["2024-01-04"]
    partial = {p["date"]: p for p in c["required_sessions_with_partial_spy_gaps"]}
    assert partial["2024-01-03"]["missing_15min_buckets"] == 2 and partial["2024-01-03"]["missing_bucket_starts_ny"] == ["11:15", "11:30"]
    assert partial["2024-01-05"]["missing_15min_buckets"] == 26 - 4
    assert c["expected_buckets"] == 26 * 4 and c["missing_buckets"] == 2 + 26 + 22
    assert c["max_missing_buckets_in_one_present_session"] == 22 and c["sessions_with_missing_buckets"] == 3


# ---------------------------------------------------------------- 8. diagnóstico de pendiente
def test_slope_window_spanning_missing_bucket_is_counted():
    df = pd.concat([minutes_frame("2024-01-02"), minutes_frame("2024-01-03", skip=tuple(f"10:{m:02d}" for m in range(0, 15)))])
    b = scd.resample_rth(df)
    d = scd.slope_windows_spanning_missing(b, [date(2024, 1, 2), date(2024, 1, 3)])
    # faltante 2024-01-03 10:00: R en 10:15, 10:30, 10:45 tienen ventanas que lo saltan
    assert d["spy_slope_windows_spanning_missing_bucket"] == 3 and d["diagnostic_only"]
    assert d["development_15min_bars_evaluated"] == len(b) - 3


def test_slope_window_across_non_required_date_is_not_a_gap():
    df = pd.concat([minutes_frame("2024-01-02"), minutes_frame("2024-01-04")])
    b = scd.resample_rth(df)
    d = scd.slope_windows_spanning_missing(b, [date(2024, 1, 2), date(2024, 1, 4)])  # 01-03 sin cobertura: no esperado
    assert d["spy_slope_windows_spanning_missing_bucket"] == 0


# ---------------------------------------------------------------- end-to-end en miniatura
def build_env(tmp_path, spy_days, stock_days=("2024-01-02", "2024-01-03")):
    stock = tmp_path / "stock"
    files = []
    for s in scd.TRADABLE:
        p = stock / "1Min" / f"{s}.csv"
        write_csv(p, pd.concat([minutes_frame(d, "09:30", "09:35") for d in stock_days]), s)
        files.append({"symbol": s, "sha256": sha256_file(p)})
    man = tmp_path / "hist_manifest.json"
    man.write_text(json.dumps({"files": files}), encoding="utf-8")
    spy_dir = tmp_path / "spy"
    return stock, spy_dir, man, spy_days


def patch_range(monkeypatch):
    monkeypatch.setattr(scd, "DEV_START", date(2024, 1, 2))
    monkeypatch.setattr(scd, "DEV_END", date(2024, 1, 3))
    monkeypatch.setattr(scd, "DOWNLOAD_START", date(2023, 12, 1))
    for f in ("required_sessions", "support_bars", "slope_windows_spanning_missing"):
        fn = getattr(scd, f)
        defaults = list(fn.__defaults__)
        defaults = [date(2024, 1, 2) if x == date(2024, 1, 2) else date(2024, 1, 3) if x == date(2025, 12, 31) else x
                    for x in defaults]
        monkeypatch.setattr(fn, "__defaults__", tuple(defaults))


def support_days():
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-12-11", "2023-12-29") if d.date() != date(2023, 12, 25)]


def test_end_to_end_pass_and_manifest(tmp_path, monkeypatch):
    patch_range(monkeypatch)
    stock, spy_dir, man, _ = build_env(tmp_path, None)
    fr = scd.freeze_sessions(stock, spy_dir, man)
    assert fr["required_session_dates"] == ["2024-01-02", "2024-01-03"] and fr["stock_cache"]["all_match"]
    df = pd.concat([minutes_frame(d) for d in support_days()] +
                   [minutes_frame("2024-01-02"), minutes_frame("2024-01-03", skip=("12:00",) + tuple(f"12:{m:02d}" for m in range(1, 15)))])
    write_csv(spy_dir / "1Min" / "SPY.csv", df, "SPY")
    res = scd.run_audit(stock, spy_dir, man)
    assert res["status"] == "PASS", res["failures"]
    m = scd.build_manifest(res, spy_dir)
    assert m["source"]["feed"] == "iex" and m["source"]["adjustment"] == "raw" and m["source"]["sip_fallback"] is False
    rs = m["required_sessions"]
    for k in ("required_session_definition", "required_session_count", "required_session_dates_sha256",
              "required_session_dates", "excluded_zero_stock_coverage_dates"):
        assert k in rs
    assert m["required_sessions_with_zero_spy_data"] == []
    assert [p["date"] for p in m["required_sessions_with_partial_spy_gaps"]] == ["2024-01-03"]
    assert m["support_bars"]["support_bars"] == 200 and m["h004_data_readiness"] == "PASS"
    out = tmp_path / "ctx_manifest.json"
    paths = scd.write_outputs(res, m, out, tmp_path / "audit")
    assert json.loads(out.read_text(encoding="utf-8"))["manifest_version"] == "context_manifest_spy_v1"
    assert len(paths) == 3 and "H004 DATA READINESS: PASS" in scd.format_report(m)


def test_end_to_end_hard_fail_on_zero_spy_required_session(tmp_path, monkeypatch):
    patch_range(monkeypatch)
    stock, spy_dir, man, _ = build_env(tmp_path, None)
    scd.freeze_sessions(stock, spy_dir, man)
    write_csv(spy_dir / "1Min" / "SPY.csv", pd.concat([minutes_frame(d) for d in support_days()] + [minutes_frame("2024-01-02")]), "SPY")
    res = scd.run_audit(stock, spy_dir, man)
    assert res["status"] == "HARD_FAIL" and res["coverage"]["required_sessions_with_zero_spy_data"] == ["2024-01-03"]


def test_end_to_end_hard_fail_on_support_and_integrity(tmp_path, monkeypatch):
    patch_range(monkeypatch)
    stock, spy_dir, man, _ = build_env(tmp_path, None)
    scd.freeze_sessions(stock, spy_dir, man)
    write_csv(spy_dir / "1Min" / "SPY.csv", pd.concat([minutes_frame(d) for d in ("2023-12-29", "2024-01-02", "2024-01-03")]), "SPY")
    res = scd.run_audit(stock, spy_dir, man)
    assert res["status"] == "HARD_FAIL" and any("support" in f for f in res["failures"])
    f = to_csv_frame(pd.concat([minutes_frame(d) for d in support_days() + ["2024-01-02", "2024-01-03"]]), "SPY")
    f.loc[10, "volume"] = -5
    f.to_csv(spy_dir / "1Min" / "SPY.csv", index=False)
    res = scd.run_audit(stock, spy_dir, man)
    assert res["status"] == "HARD_FAIL" and res["failures"] == ["raw SPY integrity/range checks failed"]


def test_frozen_list_mismatch_and_stock_cache_mismatch_are_detected(tmp_path, monkeypatch):
    patch_range(monkeypatch)
    stock, spy_dir, man, _ = build_env(tmp_path, None)
    scd.freeze_sessions(stock, spy_dir, man)
    p = spy_dir / scd.SESSIONS_FILE
    fr = json.loads(p.read_text(encoding="utf-8"))
    fr["required_session_dates_sha256"] = "0" * 64
    p.write_text(json.dumps(fr), encoding="utf-8")
    write_csv(spy_dir / "1Min" / "SPY.csv", minutes_frame("2024-01-02"), "SPY")
    with pytest.raises(scd.SpyContextError, match="congelada"):
        scd.run_audit(stock, spy_dir, man)
    man.write_text(json.dumps({"files": [{"symbol": s, "sha256": "bad"} for s in scd.TRADABLE]}), encoding="utf-8")
    with pytest.raises(scd.SpyContextError):
        scd.freeze_sessions(stock, tmp_path / "spy2", man)


def test_cli_refuses_to_overwrite_stock_manifest(tmp_path):
    m = tmp_path / "m.json"
    with pytest.raises(scd.SpyContextError, match="manifiesto"):
        scd.main(["audit", "--manifest", str(m), "--stock-manifest", str(m)])


def test_module_has_no_bypass_and_frozen_constants():
    assert scd.MAX_SPY_DATE == date(2025, 12, 31) and scd.DOWNLOAD_START == date(2023, 12, 1)
    assert scd.SUPPORT_BARS == 200 and scd.BUCKET_MINUTES == 15
    assert scd.FROZEN_SPEC_COMMIT == "31526c27436e53780cae865dde991f2fd9df01a5"
    import inspect
    src = inspect.getsource(scd.main)
    assert "--allow" not in src and "--force" not in src and "--end" not in src
