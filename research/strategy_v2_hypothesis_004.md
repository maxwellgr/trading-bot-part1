# STRATEGY_V2_HYPOTHESIS_004 — H003 + SPY broad-market regime gate (long-only)

| Field | Value |
|---|---|
| Hypothesis ID | `STRATEGY_V2_HYPOTHESIS_004` |
| Status | **SPECIFIED_NOT_IMPLEMENTED** (review decisions Q1–Q8 settled 2026-09-25, §21) |
| Date created | 2026-09-25 |
| Spec written at commit | `5bba9da3d6290ac770ce14896d30dc9b9e3d224e` (`5bba9da`, "feat: add shared management autopsy") |
| Frozen spec commit | `31526c27436e53780cae865dde991f2fd9df01a5` (`31526c2`, "docs: freeze h004 strategy specification") |
| Parent | `STRATEGY_V2_HYPOTHESIS_003`, frozen spec `6eb6078719d12e50542d7284d6e96f598c115f71` (REJECTED_AT_DEVELOPMENT) |
| Changed component | **One:** an entry-only SPY regime gate applied to H003's raw BUY signals |
| Protocol | `config/research_protocol_v1.json` |
| Registry | `research/strategy_registry_v1.json` |

Nothing here has been implemented or run. No SPY data has been downloaded or read, and no H004 result exists on any split. Review questions Q1–Q8 are settled (§21). Once this document is committed, it is the pre-registration: the rules, constants, gates and reporting are fixed **before** any H004 result or SPY data is seen.

---

## 1. Hypothesis and rationale

**Hypothesis:** some H003 long breakouts may fail because the stock setup occurs while the broad US equity market is not in a supportive upward regime. Requiring a simple upward SPY regime before an H003 BUY is allowed may reduce immediate failures, without changing H003's stock-level signal or the shared trade management.

**H004 = H003 raw BUY signal + SPY regime gate.** Everything else is H003, unchanged.

**Context (development evidence already viewed):**
- MA_BASELINE_V1 was negative in every split.
- H001, H002 and H003 were rejected at development.
- The shared trade-management autopsy (H001 vs H003, development only) found:
  - nearly identical outcome distributions for the two entry families (CLES 0.49–0.53);
  - about 47% immediate failures (never reached +0.25R and closed at a loss);
  - stop-hit trades almost never had favorable excursion first;
  - detection-to-fill latency was a small share of stop losses (about 0.04R per stop).
- Changing the entry pattern within the same single-symbol 5Min framework did not materially change expectancy. H004 therefore changes a **different dimension** (broad-market context) and leaves stop and exit management alone.

## 2. Contamination disclosure (binding)

1. **Development is hypothesis-development evidence, not an independent test.** The motivation for H004 (the immediate-failure share, and the finding that entry-pattern changes did not help) comes from already-viewed DEVELOPMENT research on H001, H002 and H003. H004 DEVELOPMENT is a **screening check**. **Validation is the first untouched H004 test.**
2. **The parent's development results are known.** H003's development trades, exits and per-symbol results have been viewed. H004's development result is therefore partly predictable from H003's, since every H004 trade comes from an H003 raw signal.
3. **The gate constants are not newly tuned.** The EMA span (50), the strict close-above-EMA test and the 3-bar slope all reuse H003's existing trend concepts. The **15Min SPY timeframe is new** and was chosen a priori. No SPY data of any kind has been examined for this project, and none of these constants were fitted.
4. **General market knowledge.** Broad US index behavior during 2024–2025 (development) is general public knowledge. So is at least part of the validation period (2026-01-02 → 2026-05-29), both to the researcher and to the model assisting with this spec. No regime statistic has been computed on any split. Still, Validation is **not blind at the macro level**, and this is disclosed rather than corrected.
5. **Known and forward** are never used for H004 selection.

## 3. Base strategy (frozen H003, unchanged)

H004 uses frozen H003 (`src/strategy_v2_h003.py`, spec `6eb6078`) **without reinterpretation** for:
- 5Min stock bars (`resample_rth_5min`), the 200 stock support bars, own-window EMA50, ATR14 and the 150-bar minimum history;
- the trend filter, the six-bar same-session consolidation window, the 2.00 ATR range limit, the breakout, the bullish trigger bar and close location ≥ 0.75;
- setup consumption on emission, and the fresh-six-bar re-arm (`T−6 > S`) with a reset each session;
- the stock universe: NVDA, AMD, PLTR, HOOD, MARA, INTC, MU, META;
- the RiskManager, sizing, stops, take profit, trailing stop, break-even, scale-outs, giveback, portfolio limits, halts and execution assumptions (H003 §9–§10).

**Invariant:** H003's raw BUY signals are **identical bar for bar** in H003 and H004. H003 raw signal generation is independent of portfolio state. H004 evaluates the SPY gate only after the frozen H003 strategy has generated its raw signal, and PASS, BLOCKED and UNAVAILABLE outcomes all consume the H003 setup. Therefore the SPY gate cannot alter the sequence of H003 raw signals.

## 4. Market-context symbol

**SPY is context only.** It is never:
- traded, sized, or given orders;
- counted in position limits, heat, exposure or leverage;
- included in P&L, equity, drawdown or R;
- included in the D6 concentration calculation.

SPY never enters the engine's tradable symbol set. It is supplied only to the gate as a read-only context series.

## 5. SPY data (obtained only after the spec is frozen)

- **Source:** Alpaca v2 stock bars, `feed=iex`, `adjustment=raw`, 1Min, via the existing `src/historical_download.py` path. This is the same source as the stock cache.
- **No silent switch to SIP.** If the SPY data audit hard-fails (§17), **stop and report**. The source is never changed under H004.
- **Storage:** raw data stays under the gitignored `data/` directory.
- **Coverage requested (Q3, option A):** SPY support + DEVELOPMENT only: 2023-12-01 → 2025-12-31, which contains the 200 pre-development support bars.
  - The VALIDATION-range SPY data (2026-01-02 onward) is **not** downloaded, inspected or cached unless all three hold: (1) H004 passes every development gate, (2) the H004 implementation is frozen, and (3) the frozen implementation commit is recorded.
  - Until then, validation-period SPY data stays unopened. Known and forward SPY data are never downloaded for H004.
- **Audit (§17):** the same integrity principles as the stock cache.
- **Manifest (Q4):** a new `research/context_manifest_spy_v1.json`. The stock manifest `research/historical_manifest_v1.json` used by H001–H003 is **not** modified. The SPY manifest records at minimum:
  - source endpoint, `feed=iex`, `adjustment=raw`;
  - requested range, and actual first and last timestamp;
  - checksum (sha256) and row count;
  - trading-session coverage;
  - duplicate and out-of-order counts;
  - OHLC integrity findings;
  - support-bar availability;
  - the required-session fields (Q8, §17): `required_session_definition`, `required_session_count`, `required_session_dates` or a deterministic checksum/hash of that date list, `excluded_zero_stock_coverage_dates`, `required_sessions_with_zero_spy_data`, `required_sessions_with_partial_spy_gaps`.
- H004 DEVELOPMENT does not proceed if SPY integrity is not acceptable.

## 6. SPY regime bars

- **Timeframe:** 15Min, regular session only, built from audited SPY 1Min bars.
- **Buckets:** `[t, t+15min)` anchored at 09:30 ET: 09:30–09:45, 09:45–10:00, 10:00–10:15, and so on.
- **Inputs:** only 1Min bars whose start lies in `[09:30, 16:00)`, or `[09:30, 13:00)` on the documented early closes (the `EARLY_CLOSES` list in `src/historical_audit.py`).
- **Aggregation:** open = first, high = max, low = min, close = last, volume = sum. This is the same convention as `resample_rth_5min`, parameterized to 15 minutes.
- **No pre-market or after-hours bars.** No interpolation. An empty bucket produces **no bar**.
- **Bucket existence (Q2):** a 15Min bucket exists if it contains **at least one** valid regular-session 1Min SPY bar. There is **no** minimum number of 1Min bars per bucket, so a partial bucket is a bar.
- **Early-close sessions** end at 13:00; the last bucket is 12:45–13:00.
- **Timestamps** are bucket starts. A bucket is **completed** at its scheduled end `start + 15min`.

## 7. SPY warm-up / support bars

- Exactly the **200** completed regular-session 15Min SPY bars immediately before each research split's first session.
- They feed the SPY indicators only. They cannot generate H004 decisions or metrics and never affect portfolio state.
- **Fail loudly** if fewer than 200 exist. There is no fallback window.

## 8. SPY regime indicator

Computed on SPY 15Min completed bar `R`:
- `EMA50_R`, inheriting **H003's EMA convention exactly (Q1)**: `close.ewm(span=50, adjust=False)` over each bar's own window of the 200 bars ending at it, with expanding history where fewer exist (`_own_window_ema`). The split-boundary behavior is also inherited: with exactly 200 support bars, EMAs of the first few split buckets use 197–199 bars. Wherever possible the same helper is reused. There is **no** special recursive seed and **no** SPY-specific EMA method.
  - The SPY series runs continuously across sessions, as the stock series does.
- `EMA50_slope_3 = EMA50_R − EMA50_{R−3}`, where `R−3` is **three existing completed** SPY 15Min bars back (Q2; the H003 slope convention). The slope window may therefore span a missing 15Min bucket.
  - Diagnostic only: `spy_slope_windows_spanning_missing_bucket` counts gate decisions whose `R−3 … R` window spans a missing bucket. It **never** affects eligibility.

Not included: EMA20, VWAP, volume, VIX, or any other market indicator.

## 9. Positive-regime rule (strict)

The SPY regime is **POSITIVE** only if **both** of these hold:
1. `close_R > EMA50_R`
2. `EMA50_R − EMA50_{R−3} > 0`

Both are strict, so equality fails. Otherwise the regime is **NEGATIVE**, unless it is **UNAVAILABLE** (§11). The comparisons use the literal formulas in floating point, with no rounding. Nothing here is optimized.

## 10. Time alignment: which SPY bar may be used

- **Stock decision time:** for an H003 signal bar starting at `t`, `D = t + 5min`. This is when the bar is completed and the engine decides.
- **Expected SPY bucket:** `R*` is the latest regular-session 15Min bucket of the **same session** whose end is ≤ `D`, i.e. `end(R*) = 09:30 + 15min × floor((D − 09:30) / 15min)`.
- A partially formed SPY bucket is **never** used.

| Stock signal bar | Decision `D` | Allowed SPY bar `R*` | Not allowed |
|---|---|---|---|
| 10:00–10:05 | 10:05 | 09:45–10:00 | 10:00–10:15 |
| 10:05–10:10 | 10:10 | 09:45–10:00 | 10:00–10:15 |
| 10:10–10:15 | 10:15 | **10:00–10:15** (completed at 10:15) | 10:15–10:30 |
| 12:55–13:00 (early close) | 13:00 | 12:45–13:00 | none later |

- H003's earliest decision is 10:05 (H003 §6), so `R*` always lies inside the stock signal's own session. It is never 09:30–09:45 (that bucket ends at 09:45) and never from a previous session.
- **Any violation of `end(R*) ≤ D`, or use of any bar other than `R*`, fails the research run** (§16).

## 11. Missing or stale SPY context

- **Only `R*` is checked for availability (Q2).** If `R*` does not exist in the SPY 15Min series (no valid 1Min SPY bar in that bucket), the regime is **UNAVAILABLE** and the BUY is blocked with routing reason **`MARKET_REGIME_UNAVAILABLE`**.
- An older bucket is **never** substituted, and no bar is fabricated.
- If `R*` exists but `EMA50_{R−3}` cannot be computed, the regime is also UNAVAILABLE. This cannot happen with 200 support bars, but it is defined for completeness.
- UNAVAILABLE occurrences are reported separately from NEGATIVE ones.
- Missing 1Min or 15Min buckets inside an otherwise present session are handled **exclusively** by this rule, and are counted and reported. They never cause a data-audit failure (§17).

## 12. Gate placement and routing

Evaluated once per stock decision bar where H003 emits a raw BUY:

1. **H003** runs exactly as frozen and gives the raw signal. A raw BUY is **consumed** at this point (§13).
2. **SPY gate:**
   - POSITIVE → the BUY continues along the **existing H003 path**, unchanged: open-position check → halt checks → daily-profit halt → RiskManager `assess_entry`.
   - NEGATIVE → the BUY is blocked **before** that path. Routing reason: `MARKET_REGIME_BLOCKED`.
   - UNAVAILABLE → the BUY is blocked **before** that path. Routing reason: `MARKET_REGIME_UNAVAILABLE`.
3. **Blocked signals never reach the RiskManager.**

Because the gate comes first, the downstream counters (position already open, halts, RiskManager accepts and rejects) count only regime-passed BUYs. The funnel in §20 reports every stage in that order.

**Mechanism (implementation boundary):** a research-only strategy wrapper runs frozen H003 and applies the gate to its raw signal. The engine receives no BUY for a blocked signal, so no shared-engine change is needed. The gate reason, the SPY bar used and the regime values are recorded per raw signal.

## 13. Setup consumption

The H003 setup is consumed **when H003 emits the raw BUY**, whatever the gate result:
- PASS consumes it;
- BLOCKED consumes it;
- UNAVAILABLE consumes it.

H004 may **not** wait for SPY to turn positive and then reuse the same consolidation. H003's fresh-six-bar re-arm (`T−6 > S`) applies unchanged. H003 does keep strategy-local consumption and re-arm state during the session. H004 evaluates the gate only after frozen H003 has emitted its raw signal, and every gate outcome consumes the setup, so that state evolves exactly as in H003. This is tested explicitly (§22).

## 14. Exits and management

- **No new exit.** There is no SPY-regime exit, FTP, market-close exit or EMA exit.
- The regime is **entry-only**. A later deterioration in the SPY regime never closes or modifies an open trade.
- Positions are managed exactly as in H003: the existing generic management and production risk values, unchanged.

## 15. Execution (identical to H003)

- **BUY:** at the next available regular-session 5Min bar open of the stock, + 5 bps.
- **SELL:** with the existing adverse slippage convention.
- **Commission:** $0.
- **Stops and targets:** checked on closes only.
- **Engine configuration:** identical to H003: `production_args({"lookback": 278})`, `window_hours_limit=False`, 5Min, and the protocol execution defaults.

## 16. Time-alignment audit (run-failing)

The audit writes one row per raw H003 BUY:
- symbol and stock signal-bar timestamp (start);
- stock decision time `D`;
- expected SPY bucket start and end;
- SPY bar actually used (start and end), or none;
- `spy_bar_completed = end ≤ D`;
- `used_bar_is_expected = used == R*`;
- SPY close, EMA50, EMA50_{R−3}, and the regime result.

The run **fails** if any row has a used bar that is partially formed (`end > D`), a used bar other than `R*`, or a gate decision taken with no audit row.

## 17. SPY data audit (before any H004 run)

The audit covers:
- first and last timestamp;
- duplicate timestamps;
- monotonic order;
- OHLC validity (`low ≤ min(open, close)`, `high ≥ max(open, close)`, `low ≤ high`);
- non-positive prices and negative volume;
- regular-session coverage per session, using the existing early-close list;
- missing sessions and truncated sessions, compared with the known stock-cache gaps (2025-03-10 absent; 2024-12-23 truncated at about 10:22);
- the file checksum (sha256);
- support-bar availability (≥ 200 valid 15Min bars before DEVELOPMENT);
- the count of development sessions with partial missing 1Min/15Min buckets (reported only);
- 15Min bucket counts per session;
- the source and feed recorded explicitly (`alpaca v2`, `iex`, `raw`).

**Hard-fail rule (fixed before any SPY data is seen).** The SPY data audit **hard-fails** before H004 DEVELOPMENT if any of these holds:
1. the required SPY data fail the structural/integrity checks above (duplicates, non-monotonic order, invalid OHLC, non-positive prices, negative volume, checksum mismatch);
2. fewer than **200** valid pre-DEVELOPMENT SPY 15Min support bars exist;
3. any **required H004 DEVELOPMENT session** (defined below) contains **zero** usable regular-session SPY 1Min bars.

**Required H004 DEVELOPMENT session (Q8):** "Any Development calendar date for which at least one of the eight tradable symbols has at least one valid regular-session bar in the existing stock cache."
- The tradable symbols are NVDA, AMD, PLTR, HOOD, MARA, INTC, MU and META.
- The required-session list is:
  - derived **only** from the existing stock cache;
  - created **before** any SPY data is downloaded or inspected;
  - independent of H004 outcomes, and of whether an H003 raw signal actually occurs that day;
  - frozen for the H004 DEVELOPMENT audit.
- No external exchange-calendar list is used to add dates with zero stock-cache coverage.
- **Consequences:**
  1. If none of the eight tradable symbols has any regular-session data on a development date, that date is **not** a required session. For example, the known 2025-03-10 all-stock IEX gap is excluded if the stock cache confirms zero regular-session bars across all eight symbols that day.
  2. If at least one tradable symbol has regular-session data, the date **is** a required session.
  3. A required session with **zero** usable regular-session SPY 1Min bars → the SPY audit **hard-fails**.
  4. A required session with SPY data but one or more expected 15Min buckets absent → **no** hard fail. Those individual signal decisions are handled only by the frozen `MARKET_REGIME_UNAVAILABLE` rule.

Partial missing 1Min or 15Min buckets inside an otherwise present session do **not** fail the audit. They are handled exclusively by the frozen `MARKET_REGIME_UNAVAILABLE` rule (§11), and are counted and reported. There is **no** percentage coverage threshold, and none may be invented after inspecting the data.

On a hard fail: stop and report. Do not change the source and do not proceed to H004 DEVELOPMENT.

## 18. Research splits and hygiene

| Split | Dates | Use for H004 |
|---|---|---|
| DEVELOPMENT | 2024-01-02 → 2025-12-31 | screening run only after spec **and** implementation are frozen; hypothesis-development evidence (§2) |
| VALIDATION | 2026-01-02 → 2026-05-29 | **first untouched test**; opened once, only if D1–D6 all pass and H004 is FROZEN; validation-range SPY is downloaded only then (§5) |
| KNOWN/CONTAMINATED | 2026-06-01 → 2026-09-23 | never used for H004; not run |
| FORWARD | 2026-09-24 → | chronological paper only, after validation passes |

- Any change to rules or constants after H004 development is viewed creates **H005**.
- A signal-changing bug fix after viewing also creates a new ID.
- The runner has a development-only hygiene guard with **no bypass flag**.

## 19. Pre-registered gates

**DEVELOPMENT (5 bps; identical to H001–H003):**

| # | Criterion |
|---|---|
| D1 | expectancy R > 0 |
| D2 | profit factor ≥ 1.10 |
| D3 | completed trades ≥ 150 |
| D4 | `abs(max drawdown %) ≤ 25` |
| D5 | total R > 0 |
| D6 | `P_s` = realized P&L of completed trades for each of the **8 tradable symbols** (SPY never appears); `Π = Σ max(P_s, 0)`. PASS only if `Π > 0` **and** `max_s max(P_s,0)/Π ≤ 0.50` (exact; 0.50 passes). `Π = 0` → undefined and **FAILS**. |

If **any** gate fails, H004 is **REJECTED_AT_DEVELOPMENT**. Validation, known and forward are never run, and no constant changes under H004.

**VALIDATION (run once; only after a full development PASS and a frozen implementation):**
- V1: expectancy R > 0
- V2: PF > 1.0
- V3: total R > 0
- V4: max DD ≤ 30%

A directional comparison with development is reported, not gated. Nothing changes after viewing, and a validation failure closes H004 permanently.

## 20. Required DEVELOPMENT report (reporting only; never used to change H004)

**Funnel:**
- raw H003 BUY signals (count, which must equal H003's raw count; §3 invariant);
- gate results:
  - POSITIVE / passed;
  - `MARKET_REGIME_BLOCKED`;
  - `MARKET_REGIME_UNAVAILABLE`;
  - % of raw signals passing;
- among passed signals: position already open, halt-blocked, profit-halt-blocked, reached RiskManager, accepts, rejects by reason, completed trades.

**Performance:** win rate, P&L, return, expectancy $ and R, PF, total R, max DD, max loss streak.

**Exits:** count and P&L for giveback, stop and take profit; scale-out leg count and P&L.

**By symbol:** trades, P&L, expectancy R, PF, and the D6 detail (`P_s`, `Π`, shares).

**SPY regime diagnostics (over all raw-signal timestamps):**
- % positive, % negative, % unavailable;
- median `(close_R − EMA50_R) / EMA50_R × 100`;
- median `(EMA50_R − EMA50_{R−3}) / EMA50_{R−3} × 100`.

**H003 vs H004 comparison** (stored H003 development, `data/research_v1/h003_development`, as parent):
- side by side: raw stock signals, completed trades, win rate, expectancy R, PF, total R, P&L, max DD, max loss streak, immediate-failure rate, and stop / giveback / take-profit counts and rates;
- **path decomposition (Q6)**, matched on `(symbol, raw H003 signal-bar timestamp)`, with count, P&L and total R per group:
  - **A.** an H003 trade also taken by H004 from the same signal;
  - **B.** an H003 trade whose raw signal was `MARKET_REGIME_BLOCKED` or `MARKET_REGIME_UNAVAILABLE` in H004;
  - **B′.** an H003 trade whose raw signal **passed** the SPY gate, but which H004 did not ultimately take, because H004's changed portfolio path caused another downstream block or rejection;
  - **C.** an H004 trade not present in H003, because the changed portfolio path made it possible;
  - **required:** A + B + B′ = the H003 completed-trade count; C is reported separately;
- the H003-vs-H004 P&L difference is **never** described as a pure market-filter effect without this decomposition.

**Immediate failures:** the established definition is "never reached +0.25R **and** final realized R < 0", using close-based MFE over the held bars, as in the shared management autopsy. Report the % for the H003 parent and for H004.

**Blocked-signal shadow diagnostic** (development only; run after the actual H004 run; read-only):
- **Method (Q5):** the same stateless initial-R reconstruction and shadow conventions already validated in the opportunity diagnostics (`reconstruct_risk_ps`, `shadow_fixed_horizon` in `src/h001_opportunity_autopsy.py`):
  - `R` per share from the `assess_entry` formula, using information known at the signal bar, with no RiskManager call;
  - entry at the next bar open + 5 bps;
  - close-based excursions.
- Metrics: 30m and 60m MFE R, 30m and 60m MAE R, and whether each reached +0.5R, +1R and −1R.
- **Groups:** all `MARKET_REGIME_BLOCKED` raw signals and all regime-passed raw signals, compared descriptively. UNAVAILABLE signals are reported separately. Each group is reported twice:
  - **A. all valid shadow observations;**
  - **B. the subset that independently passes the stateless reward-to-risk check and the stateless liquidity check.** Leverage, max-position, halt and other portfolio-state constraints are **not** included.
- **`weak_forward_excursion` = 60-minute shadow MFE < +0.25R** (Q5; pre-registered, never changed after viewing). Report the fraction meeting it for each group and subset.
- The diagnostic cannot change H004 under this ID.

**Also:**
- the time-alignment audit summary (rows, violations = 0);
- the SPY support-bar count;
- `spy_slope_windows_spanning_missing_bucket` (diagnostic only);
- time-of-day signal counts for passed and blocked signals, using the H003 buckets.

All of this is descriptive only.

## 21. Review decisions (Q1–Q8 settled 2026-09-25, before implementation; part of the frozen spec)

| # | Question | Decision |
|---|---|---|
| Q1 | SPY EMA convention | **Approved.** Inherit H003's EMA convention **exactly**, including its split-boundary behavior. No special recursive seed and no SPY-specific EMA method; reuse the H003 helper wherever possible (§8). |
| Q2 | SPY bucket availability | **Approved as proposed.** A bucket exists if it has ≥ 1 valid 1Min SPY bar, with no minimum count. Only the expected bucket `R*` is checked; if it has no usable bar the regime is UNAVAILABLE, with no older-bucket substitution. `R−3` = three existing completed bars back, so the slope window may span a missing bucket. Diagnostic only: `spy_slope_windows_spanning_missing_bucket`, never affecting eligibility (§6, §8, §11). |
| Q3 | SPY download range | **Option A.** Support + DEVELOPMENT only. Validation-range SPY is not downloaded, inspected or cached unless H004 passes every development gate, its implementation is frozen, **and** the frozen commit is recorded (§5). |
| Q4 | Manifest | **Approved.** New `research/context_manifest_spy_v1.json`, with the minimum fields listed in §5. The stock manifest is not modified. |
| Q5 | Shadow definitions | **Approved.** `weak_forward_excursion` = 60m shadow MFE < +0.25R, using the validated stateless initial-R reconstruction. Computed for all BLOCKED and all passed raw signals, each reported (A) over all valid observations and (B) over the stateless RR + liquidity subset, with no portfolio-state constraints. Descriptive only (§20). |
| Q6 | Path decomposition | **Approved.** Groups A / B / B′ / C matched on `(symbol, raw H003 signal-bar timestamp)`; A + B + B′ = H003 completed trades; C separate (§20). |
| Q7 | Reproduction mode | **Approved.** A regime-gate-disabled mode exists **only** for automated tests and the pre-run integrity check. It has no CLI or public bypass flag, must reproduce stored H003 DEVELOPMENT byte-identically, runs **before** the official H004 development run, and **aborts** H004 on any mismatch. It can never bypass the frozen gate in an official research run (§23). |

**Documentation correction (2026-09-25):** the earlier wording said H003 "replays each session without state". That was imprecise: H003 has strategy-local setup-consumption and re-arm state within a session. §3 and §13 now state the correct reason the gate cannot alter H003's raw signal sequence. This is a wording fix only and changes no behavior.

**Q8 (settled 2026-09-25): required sessions for audit rule 3.** **Option (a) approved**, with the exact definition in §17: "Any Development calendar date for which at least one of the eight tradable symbols has at least one valid regular-session bar in the existing stock cache." The list is derived only from the stock cache, before any SPY data is downloaded or inspected. It is independent of H004 outcomes and of whether an H003 raw signal occurs, and it is frozen for the development audit. No external exchange calendar adds zero-coverage dates. The manifest fields are listed in §5.

## 22. Test plan (all must pass before the DEVELOPMENT run)

**SPY data and bars**
1. SPY 1Min → 15Min resampling: buckets anchored at 09:30, OHLCV aggregation as first/max/min/last/sum, empty bucket → no bar.
2. Regular session only: pre-market and after-hours 1Min bars are excluded; early close ends at 13:00 (last bucket 12:45–13:00).
3. Exactly 200 SPY support bars; fewer fails loudly with no fallback.
3b. Bucket existence: one valid 1Min bar makes a 15Min bucket; zero makes none; no minimum count.
3c. Audit hard-fail rules (§17): each of the three conditions fails the audit on a constructed dataset; a partial missing bucket inside a present session does **not** fail it, and is counted.
3d. Required-session list (Q8), on constructed stock-cache fixtures:
  - a date with zero regular-session bars across all eight symbols is excluded and listed in `excluded_zero_stock_coverage_dates`;
  - a date with regular-session bars for one symbol is required;
  - out-of-session stock bars do not make a date required;
  - the list and its hash are deterministic and computed without reading any SPY data;
  - a required session with zero SPY bars hard-fails; one with partial SPY gaps does not, and is listed in `required_sessions_with_partial_spy_gaps`.
4. Support isolation: support bars produce no decision, metric or portfolio effect.

**Indicator and rule**

5. SPY EMA50 equals H003's convention exactly (Q1), including split-boundary truncation, by calling the same helper on a hand-built series.
6. SPY 3-bar slope: `EMA50_R − EMA50_{R−3}`, three existing bars back. A slope window spanning a missing bucket is counted in `spy_slope_windows_spanning_missing_bucket` and does not change eligibility.
7. `close_R == EMA50_R` fails; slightly above passes.
8. Slope == 0 fails; slope > 0 passes.

**Time alignment**

9. Decision 10:05 → SPY 09:45–10:00; the 10:00–10:15 bar is never used.
10. Decision 10:15 → SPY 10:00–10:15.
11. No partial bar: modifying any SPY 1Min bar at or after `D` leaves the regime at `D` unchanged.
12. Missing `R*` → UNAVAILABLE; the older bucket is not used.
13. Early close: a decision at 13:00 uses 12:45–13:00.

**Gate routing**

14. A blocked (NEGATIVE) signal does not reach the RiskManager (no `assess_entry` call).
15. An UNAVAILABLE signal does not reach the RiskManager.
16. A passed signal follows the H003 path exactly (open-position / halt / RiskManager).
17. A blocked signal still consumes the H003 setup; the next BUY needs `T−6 > S`.
18. An UNAVAILABLE signal still consumes the setup.
19. Fresh-six-bar re-arm is identical to H003 (`S+6` cannot signal; `S+7` can).
20. H003 raw signals are identical bar for bar with and without the gate.

**Exits and scope**

21. No SPY-triggered exit: a regime flip while a position is open changes nothing.
22. SPY is never tradable: no SPY order, position, P&L or exposure.
23. SPY is excluded from D6 (only the 8 symbols appear in `P_s`).

**Integrity and reporting**

24. The time-alignment audit catches an injected violation and fails the run.
25. Gate disabled → exact byte-identical reproduction of H003 (mini dataset and stored development; Q7).
25b. The official runner has no CLI or public option to disable the gate; a failed pre-run reproduction check aborts the H004 run.
26. Path-divergence reporting: A/B/B′/C partition on a constructed case; A + B + B′ = H003 trades.
26b. Shadow diagnostic reports each group twice (all valid; stateless RR + liquidity subset) and computes `weak_forward_excursion` as 60m MFE < +0.25R.
27. The blocked-signal shadow diagnostic cannot mutate the portfolio (engine outputs are unchanged with and without it).
28. Deterministic results on repeated runs.

**Hygiene**

29. The development-only guard accepts development.
29b. The SPY loader refuses to download or read SPY data after 2025-12-31 unless H004 is FROZEN with the development gate passed and the frozen commit recorded.
30. Validation is refused unless the registry status is FROZEN **and** the development gate passed **and** validation has not been viewed.
31. Known is refused.
32. Forward is refused.
33. H001, H002, H003, MA_BASELINE_V1 and the shared engine are unchanged; their tests pass unchanged.

## 23. Implementation boundary (for the later step)

- **New research-only modules:**
  - `src/market_context_spy.py`: 15Min resampler, support bars, regime and alignment;
  - `src/strategy_v2_h004.py`: gate wrapper around frozen H003;
  - `src/research_h004.py`: runner, hygiene guard, report, path decomposition and shadow diagnostic.
- **SPY download and audit** reuse `src/historical_download.py` and the `src/historical_audit.py` principles. The audit writes to `research/context_manifest_spy_v1.json` and never to the stock manifest (Q4).
- **Reproduction mode (Q7):** only a test/pre-check constructor path, with no CLI flag. The official runner always constructs H004 with the gate enabled.
- **Unchanged:** `src/strategy_v2_h003.py`, H001, H002, MA_BASELINE_V1, the shared engine, RiskManager, BrokerAlpaca, `run_paper`, order tracking, execution guards, sizing, stops and exits, halts, and `src/smoke_test.py`.
- **Freeze procedure:** as for H003. The user commits the frozen spec, the commit hash is recorded in this header and in the registry, and only then does implementation start.

## 24. Known limitations

1. Development is not untouched (§2), the parent's results are known, and general macro knowledge of 2024–2026 exists.
2. SPY on IEX is a partial-venue feed. Its prints differ from SIP, though SPY liquidity makes this minor for 15Min closes.
3. A 15Min regime can lag the 5Min stock decision by up to 15 minutes by construction: at 10:10, the latest completed bucket still ends at 10:00.
4. Filtering changes the portfolio path, so H004 − H003 is not a pure filter effect (§20 decomposition).
5. The inherited 5Min framework limitations (H003 §9) all remain: the looser liquidity filter, the implicit ATR% floor, the fixed-dollar halts, the day-boundary convention and overnight holds.
6. The universe is small, correlated and high-beta, long-only, with 2 years of development.

## 25. Change history

- 2026-09-25: proposed (SPECIFIED_NOT_IMPLEMENTED) at `5bba9da`. No code, no SPY download, no H004 run, and no H004 data produced or read. Validation, known and forward untouched. Review questions Q1–Q7 open.
- 2026-09-25: review decisions Q1–Q7 settled (§21).
  - The H003-state wording was corrected (§3, §13). Documentation only.
  - The SPY audit hard-fail rule was made explicit before any SPY data was seen (§17).
  - Q8 (definition of a required session) was raised.
  - No other rule, constant or gate changed. Still SPECIFIED_NOT_IMPLEMENTED; no code, no SPY data.
- 2026-09-25: Q8 settled, option (a): a required development session is any date on which at least one tradable symbol has a valid regular-session bar in the existing stock cache.
  - The list is derived from the stock cache before any SPY download, frozen for the audit, and recorded in the SPY manifest.
  - No other rule changed. Still SPECIFIED_NOT_IMPLEMENTED; no code, no SPY data.
