# STRATEGY_V2_HYPOTHESIS_003 — 5Min trend-filtered consolidation breakout (long-only)

| Field | Value |
|---|---|
| Hypothesis ID | `STRATEGY_V2_HYPOTHESIS_003` |
| Status | **SPECIFIED_NOT_IMPLEMENTED** (review decisions Q1–Q5 settled 2026-09-25, §17) |
| Frozen spec commit | `6eb6078719d12e50542d7284d6e96f598c115f71` (`6eb6078`, "docs: freeze h003 strategy specification") |
| Date created | 2026-09-25 |
| Spec written at commit | `ed4967a883de4a87846713bb329674470c2e1343` (`ed4967a`, "feat: add h001 opportunity autopsy") |
| Signal family | **New.** It is neither H001's pullback-continuation family nor H002's FTP exit. |
| Protocol | `config/research_protocol_v1.json` |
| Registry | `research/strategy_registry_v1.json` |

Nothing here has been implemented or run. No H003 result exists on any split. The review questions (§17) are settled; once this document is committed it is the pre-registration. Rules, constants, gates and reporting are fixed **before** any H003 result is seen.

---

## 1. Hypothesis and rationale

**Hypothesis:** a 5-minute breakout from a recent price consolidation, made while a broader upward trend is already established, may produce larger directional moves and fewer low-quality entries than MA crossover or pullback-continuation signals.

**Context (development evidence already viewed):**
- MA_BASELINE_V1 was negative in every split.
- H001 (pullback continuation) and H002 (H001 plus the FTP exit) were rejected at development.
- The H001 entry autopsy found no strong entry-feature separation (CLES about 0.44–0.57).
- The H001 opportunity autopsy found no meaningful selection or allocation edge.
- H002's path divergence came mainly from same-symbol re-entries.

**Why this family:** a breakout from a tight range is a different entry mechanism from buying a retracement. The breakout bar closing near its high is the directional commitment, and the requirement for a *fresh* consolidation before any re-arm is meant to avoid repeated entries from the same structure.

**Not reused:** H001's pullback requirement, H001's EMA20 conditions, H002's FTP exit, and any MA-crossover SELL exit.

## 2. Contamination disclosure (binding)

1. **Development is hypothesis-development evidence, not an untouched test.** The general direction of H003 (a different entry family, no FTP, no same-structure re-entry) was motivated by DEVELOPMENT research on H001 and H002. Development is therefore a **screening check**. **Validation is the first untouched test of H003.**
2. **Close location was already examined on development.** The H001 development autopsy reported trigger close location in pre-fixed buckets, with a top-quarter boundary of 0.75: top quarter −0.044R versus below midpoint −0.174R. H003's close-location ≥ 0.75 requirement coincides with that **already-viewed** boundary.
   - H003 DEVELOPMENT is therefore **hypothesis-development evidence, not an independent test** of that rule.
   - **Validation is the first untouched test.**
   - The Entry Quality diagnostics also examined close location on the KNOWN period. That period is never used for H003 in any case.
3. **Other constants:** the 6-bar consolidation, the 2.00 ATR range limit, EMA50 and the 3-bar slope were **not** derived from any H003-specific data. No H003 data has been produced. They are fixed now and never tuned.
4. **Known and forward** are never used for H003 selection.

## 3. Data (inherited H001 conventions; unchanged)

- **Source:** the audited 1Min Alpaca IEX **raw** cache, with checksums in `research/historical_manifest_v1.json`.
- **5Min bars:** resampled exactly as in the H001 spec §3 (`resample_rth_5min`):
  - buckets `[t, t+5min)` aligned to 09:30 ET;
  - only 1Min bars whose start is in `[09:30, 16:00)`, or `[09:30, 13:00)` on the documented early closes;
  - OHLCV aggregated as first / max / min / last / sum;
  - an empty bucket produces **no bar**.
- **Regular session only.** Pre-market and after-hours bars are never used.
- **Known gaps kept as-is:** 2025-03-10 has no IEX bars; 2024-12-23 IEX stops at about 10:22 ET.
- **NVDA 10:1 split (2024-06-10):** raw/unadjusted, with **no special handling**.

## 4. Warm-up, support bars and indicators

**Support bars:** exactly the **200** completed regular-session 5Min bars immediately before each research split's first session (the H001 convention).
- They are indicator history only: no signals, trades, orders, equity points or research metrics come from them.
- Each split starts flat, with no strategy state carried in.
- **Fail loudly** if fewer than 200 are available; never fall back to the engine's generic warm-up window.

**Indicator conventions (inherited from the H001 implementation):**
- **EMA50:** `close.ewm(span=50, adjust=False)`.
  - Each bar's value is computed over **its own window**: the last `W = 200` completed regular-session bars ending at that bar, seeded at the window start.
  - The value is therefore what was computed when that bar was the signal bar.
  - This keeps the session replay of §7 consistent with signals emitted earlier (review decision Q3).
- **ATR14:** `RiskManager._atr(high, low, close, 14)` on bars up to and including the stated bar. This is the simple mean of 14 true ranges, where the previous close may come from the prior session.
- **Minimum history:** no signal unless at least **150** completed bars are available (inherited H001 guard; review decision Q3). The official research run still **requires exactly 200 pre-split support bars**, so this guard never binds inside a split.
- **Engine window:** the engine passes the last `200 + 78 = 278` bars, so every bar of the current session has its full own-window history. There is no 24h limit (`window_hours_limit=False`).

## 5. Signal rules (evaluated at the close of each completed 5Min bar T)

**Constants (fixed; never tuned):**
- EMA span 50
- slope lookback 3 bars
- consolidation length 6 bars
- range limit 2.00 × ATR
- close location ≥ 0.75
- ATR window 14

A **BUY** is emitted at `T` only if **all** of A–F hold. Definitions:
- **Session:** bars with the same America/New_York date. Bars are regular-session only.
- **Window:** `C(T) = {T−6, …, T−1}`, the six existing 5Min bars immediately before `T` in that symbol's series.

**A. Trend filter at T**
1. `close_T > EMA50_T`
2. `EMA50_T − EMA50_{T−3} > 0` (strictly). This is the change over the three bar intervals ending at `T`, including `T`, which is the H001 slope convention.

**B. Same-session window (§6)**

All six bars of `C(T)` and `T` belong to the **same session**. So `T` is at least the **7th existing** bar of its session, and no bar from a previous session may enter the window.
- `C(T)` is the six **existing completed** bars immediately before `T`. They need **not** occupy six consecutive clock slots if an IEX 5Min bucket is absent (review decision Q2).
- Missing bars are never fabricated or interpolated.

**C. Valid consolidation**
- `consolidation_high = max(high_i for i in C(T))`
- `consolidation_low = min(low_i for i in C(T))`
- `consolidation_range = consolidation_high − consolidation_low`
- `consolidation_range_atr = consolidation_range / ATR_{T−1}`, where `ATR_{T−1}` is ATR14 on bars up to and including `T−1`.
- Valid only if `consolidation_range_atr ≤ 2.00`. **Exactly 2.00 passes.**
- If `ATR_{T−1}` is **zero, non-finite or unavailable**, the consolidation is **invalid** and no signal may be generated. **There is no fallback value** (review decision Q4).
- The signal bar `T` is never part of the consolidation.

**D. Breakout**

`close_T > consolidation_high`, strictly. Equality fails.

**E. Bullish breakout bar**

`close_T > open_T`, strictly.

**F. Close location**
- `close_location = (close_T − low_T) / (high_T − low_T)`, required to be `≥ 0.75`. **Exactly 0.75 passes.**
- If `high_T == low_T`, the trigger **fails**.

**G. Not a consumed setup:** the re-arm rule of §7 is satisfied.

**Not included in H003:** no volume filter, no breakout-distance or chasing filter, and no pullback requirement.

**Comparisons use the literal formulas** (`range / ATR ≤ 2.0`, `(close − low)/(high − low) ≥ 0.75`) in floating point, with no rounding. Boundary tests use exactly representable values.

## 6. Session timing

- The consolidation window must lie entirely in `T`'s session, with no overnight windows.
- **Earliest signal: exact timing (review decision Q1).** On a full session the earliest possible signal bar `T` is the 5Min bar **starting at 10:00 ET**, covering [10:00, 10:05).
  - It becomes known **only at its close, 10:05 ET**, and the BUY decision is made then. **No decision is made before the 10:00–10:05 bar closes.**
  - If it generates a BUY, the entry fills at the **next bar's open, the 10:05 ET bar open**, + 5 bps.
  - "10:00 signal" always means the bar that *starts* at 10:00 and is decided at 10:05, never a decision at 10:00. Its consolidation window is the six bars starting 09:30 through 09:55.
- **Missing bars (review decision Q2).** The window is the six **existing completed** bars before `T`, all in `T`'s session. Six contiguous 5-minute clock slots are **not** required, and absent IEX buckets are never fabricated or interpolated.
  - A window is **non-contiguous** when its six bars span more than six 5-minute slots, i.e. `start(T−1) − start(T−6) > 25 minutes`.
  - Non-contiguity is reported as a **diagnostic only** (§14). It is **never** used as a filter.
- **Session end:** the last bar (15:55–16:00) is never evaluated, because its close is known at 16:00 and there is no engine decision then (inherited). On an early-close day the engine treats the 13:00 close as a permitted decision (inherited), so a signal there fills at the next session's open. The same applies to a signal on the truncated 2024-12-23 session. This is documented, not altered.

## 7. Setup consumption and re-arm (deterministic, portfolio-independent)

- **Consumed by emission.** A BUY emitted at bar `S` consumes that setup whether or not the RiskManager accepts it, and whether or not a position is already open or a halt blocks it. Portfolio state never determines whether a setup is used.
- **Re-arm.** After a BUY at `S`, the next BUY for that symbol **in the same session** requires a window `C(T)` whose six bars all occur **strictly after S** (`T−6 > S`, so the earliest is `T = S + 7`). That window must satisfy B–F on its own.
  - This requires a **fresh** consolidation. The same structure can never re-trigger, even if the first trade closes quickly.
  - It is not a separate cooldown parameter: it follows from the 6-bar window.
- **Session reset.** State resets at the start of every session, and nothing carries overnight.
- **Stateless implementation.** State is derived by **replaying A–G from the first bar of the current session** on every evaluation, using each bar's own-window indicators (§4). This is deterministic and identical in the backtest and any later use.

## 8. Exits

- **No H003-specific exit signal.** The strategy never emits SELL/EXIT. It does not inherit MA-crossover SELL exits or H002's FTP, and the `signal_exit` reason cannot occur.
- Positions are managed only by the **existing generic trade management, unchanged**, in the backtester's existing order: trailing-stop update → break-even → scale-out → giveback → stop / take profit. This covers the initial ATR stop, ATR take profit, trailing stop, break-even, scale-outs, giveback and all portfolio and risk safeguards.

## 9. Risk and portfolio (production values, unchanged; not re-tuned for 5Min)

| Setting | Value |
|---|---|
| risk per trade | 0.005 |
| min RR | 1.3 |
| ATR stop / take multipliers | 2.0 / 3.0 |
| trailing ATR multiplier | 1.5 |
| max positions | 4 |
| max leverage | 1.5 |
| max portfolio heat | 0.2 |
| max symbol exposure | 0.1 |
| min liquidity | $200k |
| daily loss limit | 3% |
| max consecutive losses | 3 |
| break-even at | 1R |
| scale-outs | `1.0:0.5,2.0:0.5` |
| max giveback | 0.5 |
| daily profit halt | $300 |

The RiskManager's stop and target use ATR14 at the **signal bar T**, which is the existing `assess_entry` behavior. The consolidation test uses `ATR_{T−1}`. Both are as specified.

**Known semantic limitations on 5Min** (documented and inherited; **not** corrected under H003):
- **Liquidity filter:** fixed at $200k average over 20 bars, which is roughly 5× looser on 5Min bars.
- **Implicit ATR% floor:** the RR check with 5 bps modeled slippage rejects signals with ATR% below about 0.29%. This was observed exactly in the H001 development funnel.
- **Fixed-dollar profit halt:** the $300 halt was calibrated for the 1Min benchmark.
- **Loss-streak halt:** stops the day after 3 consecutive losses.
- **Day boundary:** the engine applies fills before a day's first decision ahead of the daily reset (inherited convention).
- **Overnight holds:** positions are carried overnight.

## 10. Execution model (canonical; existing simulator, unchanged)

- **Signal:** on a completed 5Min bar `T`, decided at `T`'s close.
- **Entry:** at the **next available regular-session 5Min bar open** of that symbol, `open × (1 + 5 bps)`.
- **Sells:** at the next available open with the existing adverse slippage, `open × (1 − 5 bps)`.
- **Commission:** $0.
- **Stops and targets:** checked on closes only, with no intrabar fills.
- **Missing next bar:** the fill happens at the next existing bar and counts as a delayed fill.

The engine configuration is identical to H001's, apart from the strategy object: `production_args({"lookback": 278})`, `window_hours_limit=False`, 5Min, the protocol execution defaults, and the strategy's minimum-bars value of 150. It uses the existing strategy injection. **No shared-engine change is needed.**

## 11. Research splits and hygiene

| Split | Dates | Use for H003 |
|---|---|---|
| DEVELOPMENT | 2024-01-02 → 2025-12-31 | first screening run, only after spec **and** implementation are frozen; hypothesis-development evidence (§2) |
| VALIDATION | 2026-01-02 → 2026-05-29 | **first untouched test**; opened once, only if D1–D6 all pass and H003 is FROZEN (spec + code + commit recorded) |
| KNOWN/CONTAMINATED | 2026-06-01 → 2026-09-23 | never used for H003 selection; not run |
| FORWARD | 2026-09-24 → | chronological paper only, after validation passes |

- Any change to rules or constants after H003 development is viewed creates **H004**.
- A signal-changing bug fix after viewing also creates a new ID.

## 12. Pre-registered DEVELOPMENT gate (5 bps; identical to H001 and H002)

| # | Criterion |
|---|---|
| D1 | expectancy R > 0 (mean `realized_r` of completed trades) |
| D2 | profit factor ≥ 1.10 |
| D3 | completed trades ≥ 150 |
| D4 | `abs(max drawdown %) ≤ 25` |
| D5 | total R > 0 |
| D6 | `P_s` = sum of `realized_pnl` of completed trades for symbol `s`; `Π = Σ max(P_s, 0)`. PASS only if `Π > 0` **and** `max_s max(P_s,0)/Π ≤ 0.50` (exact; 0.50 passes). If `Π = 0`, D6 is **undefined and FAILS**. `P_s`, `Π` and every share are always reported. |

**Failure rule:** if **any** gate fails, H003 is **REJECTED_AT_DEVELOPMENT**:
- validation, known and forward are never run;
- no constant changes under H003;
- any follow-up is **H004**.

If all gates pass, the implementation and commit hash are frozen **before** validation is opened.

## 13. Pre-registered VALIDATION gate (run once)

- V1: expectancy R > 0
- V2: PF > 1.0
- V3: total R > 0
- V4: max DD ≤ 30%

**Directional comparison with development** (reported, not gated): trade count, expectancy, PF, drawdown, and D6 concentration. Nothing is tuned after viewing, and a failure closes H003 permanently.

## 14. Required DEVELOPMENT report (reported only; never used to change H003)

- **Overall:**
  - generated BUY signals; risk accepts and rejects (by reason); pre-risk blocks (halts, symbol already open);
  - completed trades, win rate, P&L, return, expectancy $ and R, PF, total R, max DD, max loss streak.
- **By exit:** count and P&L for giveback, stop and take profit, plus scale-out leg counts and P&L.
- **By symbol:** trades, P&L, expectancy R, PF, and the D6 detail (`P_s`, `Π`, shares).
- **Signal structure** (all generated signals and accepted trades):
  - median `consolidation_range_atr`;
  - median breakout-bar range / `ATR_{T−1}`, i.e. `(high_T − low_T) / ATR_{T−1}` (review decision Q5; reporting only);
  - median breakout distance `(close_T − consolidation_high) / ATR_{T−1}` (review decision Q5; reporting only);
  - `signals_with_noncontiguous_consolidation_window` and `trades_with_noncontiguous_consolidation_window` (review decision Q2; diagnostic only, **never a filter**);
  - signal count by time of day, using the H001 time buckets on decision ET: 10:00–10:30, 10:30–11:00, 11:00–12:00, 12:00–14:00, 14:00–16:00.
- **Also:** the number of NVDA signals from 2024-06-10 through 2024-06-14, for transparency on the unadjusted split; support-bar counts per symbol; and a same-session re-arm count (signals that were the second or later in their session).

## 15. H003 versus existing DEVELOPMENT benchmarks (descriptive; no ranking score)

**Development only, using the stored results:**
- MA_BASELINE_V1: `data/research_v1/benchmark_periods.json`, development row;
- H001: `data/research_v1/h001_development`;
- H002 (context only; not H003's parent): `data/research_v1/h002_development`.

**Metrics:** trades, win rate, expectancy R, PF, total R, max DD, and max loss streak.

## 16. Test plan (all must pass before the DEVELOPMENT run)

**Data and warm-up**
1. 5Min resampling is identical to the H001 resampler, using the same function.
2. Exactly 200 support bars per symbol. Fewer fails loudly, with no fallback.
3. Support-bar isolation: no signal, order, trade, equity point or metric before the split start. Support bars affect indicators only.

**Rules (hand-built bars / indicator arrays)**

4. EMA50 trend: `close_T > EMA50_T` is required, and equality fails.
5. 3-bar EMA50 slope: `EMA50_T − EMA50_{T−3} > 0`. Zero fails.
6. Exact 6-bar window: the window is `T−6 … T−1`. `T` and `T−7` are excluded (changing them doesn't change the window's high and low).
7. Range < 2 ATR passes.
8. Range == 2 ATR exactly passes.
9. Range > 2 ATR fails.
10. `ATR_{T−1}` is used, not `ATR_T`. ATR that is 0, non-finite or unavailable fails (see also 24b).
11. Breakout `close_T > consolidation_high` passes.
12. Breakout equality fails.
13. Bullish bar required: `close_T == open_T` or `close_T < open_T` fails.
14. Close location > 0.75 passes.
15. Close location == 0.75 exactly passes.
16. A zero-range trigger bar fails.

**Session and state**

17. Same-session window only: no bar from the previous session may enter the window.
18. The earliest signal is the bar **starting** 10:00:
    - it is decided at its close, 10:05 ET, and fills at the 10:05 bar's open;
    - no decision exists before 10:05;
    - the 6th bar of a session (09:55–10:00) can never signal.
19. A setup is consumed on a generated BUY.
20. A BUY rejected by the RiskManager, or blocked by a halt or an open position, still consumes its setup. The next BUY needs `T−6 > S`.
21. A fresh six post-signal bars are required before re-arm. `T = S+6` cannot signal; `T = S+7` can, if B–F hold.
22. No same-setup re-entry: a trade that closes shortly after a breakout does not allow re-entry from the original consolidation.
23. Session reset: no state carries overnight.
24. Missing 5Min bar inside the window (Q2):
    - the six **existing** bars are used, all from the same session, and nothing is fabricated;
    - such a window is flagged non-contiguous (`start(T−1) − start(T−6) > 25 min`);
    - the flag changes no signal;
    - `signals_with_noncontiguous_consolidation_window` and `trades_with_noncontiguous_consolidation_window` count correctly.
24b. Invalid ATR (Q4): `ATR_{T−1}` equal to 0, NaN or infinite, or unavailable, gives no signal and uses no fallback.

**Execution and exits**

25. No strategy SELL signal is ever emitted, and `signal_exit` never occurs.
26. Next-bar-open fill: entry at the next available bar open + 5 bps; sells use the existing adverse slippage.

**Integrity**

27. No look-ahead: the signal at `T` is invariant to modifying any bar after `T`.
28. Session replay: replayed signals equal those emitted at the time, and are independent of the engine window start. This is checked against an independent sequential reference implementation, as for H001.
29. Deterministic: identical outputs on repeated runs.

**Hygiene and invariance**

30. Development-only hygiene: the runner accepts the development split.
31. Validation refused unless the registry status is FROZEN **and** the development gate passed **and** validation has not been viewed.
32. Known refused.
33. Forward refused.
34. H001 and MA_BASELINE_V1 unchanged: H001's tests pass unchanged, and the shared engine and MA defaults are untouched. No shared code is modified.

## 17. Review decisions (settled 2026-09-25, before implementation; part of the frozen spec)

| # | Question | Decision |
|---|---|---|
| Q1 | Earliest signal timing | **Confirmed.** The earliest signal bar is the 5Min bar **starting 10:00 ET**; it closes and becomes known at **10:05 ET**. A BUY fills at the **10:05 ET bar open**. No decision is ever made before the 10:00–10:05 bar closes (§6). |
| Q2 | Missing bars inside the window | **Approved.** Use the six existing completed bars immediately before `T`, all in the same session; consecutive clock slots are not required, and nothing is fabricated or interpolated. Diagnostics only: `signals_with_noncontiguous_consolidation_window` and `trades_with_noncontiguous_consolidation_window`, never a filter (§5B, §6, §14). |
| Q3 | Indicator conventions | **Approved.** Inherit H001: each bar's EMA over the 200-bar window ending at that bar, the 278-bar engine context, and the 150-bar minimum-history guard. The official run still requires exactly 200 pre-split support bars (§4). |
| Q4 | Invalid `ATR_{T−1}` | **Approved.** Zero, non-finite or unavailable means an invalid consolidation and no signal. There is no fallback value (§5C). |
| Q5 | Report normalization | **Approved.** Breakout-bar range and breakout distance use `ATR_{T−1}`. Reporting only (§14). |

## 18. Implementation boundary (for the later step)

- **New research-only module:** `src/strategy_v2_h003.py` (strategy class and signal-structure metrics) and `src/research_h003.py` (runner, hygiene guard and report).
- **Reused as-is:** the H001 resampler and support-bar helpers, the bounded development data loader, the `development_gates` / D6 functions and `check_split_allowed`.
- **Unchanged:** the shared engine, RiskManager, H001, H002, MA_BASELINE_V1, `run_paper`, BrokerAlpaca, order tracking, execution guards, live sizing, stops and exits, daily halt logic, and `src/smoke_test.py`.

## 19. Known limitations

1. **Development is not untouched:** it is hypothesis-development evidence (§2), and the close-location threshold coincides with an already-viewed bucket boundary.
2. **Inherited 5Min framework limitations (§9):** the looser liquidity filter, the implicit ATR% floor from the RR check, the fixed-dollar halts, the day-boundary convention, and overnight holds.
3. **Data:** IEX only, meaning sparse volume and prints that differ from SIP. The known feed gaps and the unadjusted NVDA split are carried as-is.
4. **Close-based stops:** stops and targets react only at 5Min closes, with the fill at the next open.
5. **Signals decided at 13:00 on early-close days** (and at 10:20–10:25 on 2024-12-23) fill at the next session's open.
6. **Small, correlated universe:** 8 correlated high-beta symbols, long-only, 2 years of development. Passing gates would be weak evidence that needs validation and forward confirmation.

## 20. Change history

- 2026-09-25: proposed (SPECIFIED_NOT_IMPLEMENTED). No code, no backtest, no H003 data produced or read. Validation, known and forward untouched.
- 2026-09-25: review decisions Q1–Q5 settled (§17).
  - Explicit 10:00-bar / 10:05-decision timing.
  - Non-contiguous window allowed, plus diagnostic counters.
  - H001 indicator semantics inherited.
  - Invalid ATR gives no signal, with no fallback.
  - `ATR_{T−1}` report normalization.
  - Close-location disclosure made explicit.
  - No other rule or constant changed. Still SPECIFIED_NOT_IMPLEMENTED; no code, no data.
