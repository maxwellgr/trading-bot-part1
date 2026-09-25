# STRATEGY_V2_HYPOTHESIS_001 — 5Min trend + pullback continuation (long-only)

| Field | Value |
|---|---|
| Hypothesis ID | `STRATEGY_V2_HYPOTHESIS_001` |
| Status | **SPECIFIED_NOT_IMPLEMENTED** |
| Date created | 2026-09-24 |
| Spec written at commit | `cfe33dd705ea0636d64d3d08809210e160fb4958` (`cfe33dd`, "feat: add research protocol and historical benchmark") |
| Protocol | `config/research_protocol_v1.json` |
| Benchmark | `MA_BASELINE_V1` (frozen; not modified by this hypothesis) |
| Registry | `research/strategy_registry_v1.json` |

Nothing in this document has been implemented or backtested. No development, validation or known-period result exists for this hypothesis. This document is the pre-registration: the rules, constants and gates below are fixed **before** any result is seen.

---

## 1. Hypothesis

A trend + pullback continuation strategy on 5-minute regular-session bars may produce:

- fewer and higher-quality entries than the 1-minute MA 3/7 crossover benchmark;
- larger favorable excursions per entry;

so that modeled execution costs are a smaller fraction of R.

Long-only in v1.

## 2. Rationale

- **MA_BASELINE_V1 loses about −0.11R per trade in every evaluated split** (development −0.105R / PF 0.64; validation −0.119R / PF 0.60; known −0.107R / PF 0.65).
- **Disclosure.** The hypothesis was motivated by diagnostics on the KNOWN/CONTAMINATED period (2026-06-01 → 2026-09-23):
  - most losing entries never reached +0.5R;
  - fixed-bps costs consumed a large fraction of R on low-ATR, 1-minute trades.

  Because those observations shaped the idea, **the known period provides no evidence for or against this hypothesis** and must not be used to select or tune it.
- **Idea.**
  - Buy continuation after a retracement inside an established uptrend, instead of the first crossover of two very short averages.
  - Use a 5-minute timeframe so the ATR, and therefore the initial stop and R, is larger relative to a fixed 5 bps cost.

## 3. Data and timeframe

- **Source:** Alpaca v2 bars, `feed=iex`, `adjustment=raw`. This is the audited local cache `data/historical/1Min/*.csv`, whose checksums are in `research/historical_manifest_v1.json`.
- **Timeframe:** 5Min, built by **resampling the audited 1Min cache** (review decision Q2).
  - Buckets are `[t, t+5min)` aligned to 09:30 America/New_York.
  - Only 1Min bars whose **start** falls in the regular session are used: `[09:30, 16:00)`, or `[09:30, 13:00)` on the early closes listed in `src/historical_audit.EARLY_CLOSES`.
  - `open` = first 1Min open; `high` = max; `low` = min; `close` = last 1Min close; `volume` = sum.
  - A 5Min bar exists if at least one 1Min bar exists in its bucket. Empty buckets produce **no bar**; nothing is filled or interpolated.
  - The implementation records the number of constituent 1Min bars per 5Min bar, for audit only.
- **Session:** strategy calculations use regular-session bars only. Pre-market and after-hours bars are **never** used.
- **Timestamps:** bar start, in UTC. A 5Min bar is known at `start + 5min`, its decision time.
- **Known data issues, carried from the protocol.** All are left as-is and nothing is fabricated.
  - **2025-03-10:** no IEX bars for any symbol. For this strategy the session simply does not exist.
  - **2024-12-23:** IEX bars stop at about 10:22 ET, so that session ends early for all symbols.
  - **NVDA 10:1 split on 2024-06-10, stored unadjusted:**
    - Because the indicator window spans sessions (§4), the pre-split prices (~$1,190) enter the EMAs and ATR after the split.
    - The expected effect is that NVDA's trend conditions fail, since close is below EMA50, until the EMAs decay. That suppresses signals rather than creating them.
    - The implementation **must report every NVDA signal from 2024-06-10 through 2024-06-14** so the effect is visible. It must not special-case the split.
  - **Extended-hours bars:** sparse historically, and irrelevant here because they aren't used.

## 4. Indicator window and definitions

All indicators use **completed** 5Min regular-session bars only. Bar `T` is the signal bar, evaluated at its decision time. Bars `T−1`, `T−2`, … are earlier completed bars in the continuous regular-session series; that series crosses overnight and weekend gaps.

- **Indicator window:** the last `W = 200` completed regular-session 5Min bars up to and including `T`, about 2.6 sessions. This replaces the benchmark's 1Min window (`lookback=120` bars within `hours_back=24` h). The 24h limit is not used here, because on 5Min regular-session data it would drop the previous session every Monday and after every holiday (review decision Q3).
- **Warm-up:** no signal unless the window contains at least **150** completed bars, which is 3 × the EMA50 span.
- **Warm-up support bars before each evaluation split (review decision Q3):**
  - Each evaluation split (development, validation, known) is preceded by **support bars**: the **200 completed regular-session 5Min bars immediately before the split's first session**, built by the same resampling (§3).
  - Support bars are used **only** as indicator history (EMA20, EMA50, ATR, slopes). No decisions, signals, orders, trades, equity points or metrics come from them.
  - Every split starts flat, with no positions and no strategy state carried in. By §5.1, the first possible signal is the 6th bar of the split's first session.
  - Sources:
    - development: 2023-12 warmup split
    - validation: last sessions of development (Dec 2025)
    - known: last sessions of validation (May 2026)

    Using earlier bars as indicator history is not look-ahead and does not view any results of the earlier split.
  - The implementation must **assert at least 200 support bars per symbol**, or fail loudly for that split. It must **not** fall back to the engine's generic "4 calendar days before start" window, which can hold fewer than 150 bars (e.g. only 2023-12-29 before 2024-01-02).
  - Support-bar counts per symbol and split are written to the run's output.
- **EMA_N:** `close.ewm(span=N, adjust=False).mean()` over the window, seeded at the first bar of the window. This is the same convention as the repo's `MACDStrategy`.
  - With `W = 200` the seed's residual weight on EMA50 is `(49/51)^200 ≈ 0.03%`.
  - Values are deterministic, but can differ negligibly from an infinite-history EMA.
- **ATR_i:** the project's existing convention, `RiskManager._atr(high, low, close, 14)` evaluated on bars up to `i`.
  - It is the simple mean of the last 14 true ranges, `TR = max(h−l, |h−prev_close|, |l−prev_close|)`, where `prev_close` may be the previous session's close.
  - The RiskManager uses the same formula for the initial stop and the trailing stop.
- **Slopes:**
  - `EMA20_slope3(T) = EMA20_T − EMA20_{T−3}`
  - `EMA50_slope3(T) = EMA50_T − EMA50_{T−3}`

  This is the change over the last three completed bar intervals ending at the signal bar (review decision Q4).

## 5. Signal rules (evaluated at the close of each completed 5Min bar T)

**Fixed constants.** These are pre-registered and not to be optimized:
- EMA spans: 20 and 50
- slope lookback: 3 bars
- pullback window: 5 bars
- touch distance: 0.50 × ATR
- ATR window: 14
- indicator window: 200 bars
- warm-up: 150 bars

A BUY signal is emitted at `T` if **all** of the following hold.

### 5.1 Session position
- `T` is at least the **6th** completed bar of its session, so the five pullback bars `T−5 … T−1` all belong to the **same session** as `T`. On a normal day the earliest possible signal bar is 09:55–10:00, decided at 10:00 ET.
- No pullback bar or trigger reference bar comes from a previous session (review decision Q5).

### 5.2 Trend (at T)
1. `EMA20_T > EMA50_T`
2. `EMA20_slope3(T) > 0`
3. `EMA50_slope3(T) ≥ 0`
4. `close_T > EMA50_T`

### 5.3 Pullback (window `P = {T−5, …, T−1}`, same session)
1. **Touch:** there is at least one `i ∈ P` with `low_i ≤ EMA20_i + 0.50 × ATR_i`, using the pullback bar's **own** EMA20 and ATR (review decision Q8). A low at or below EMA20 counts, because its distance is ≤ 0.
2. **Hold:** for every `i ∈ P`, `close_i ≥ EMA50_i`. No pullback bar closes below EMA50.

### 5.4 Continuation trigger (bar T)
1. `close_T > high_{T−1}`
2. `close_T > EMA20_T`
3. `close_T > open_T`

### 5.5 One entry per pullback sequence (state / reset)
- **Setup consumed:** once a BUY is emitted at bar `S`, that pullback sequence is used up.
- **Re-arm rule:** a later BUY at `T > S` in the same session also requires a **touch bar with index strictly greater than `S`** inside `T`'s pullback window, i.e. `∃ i ∈ P(T), i > S, low_i ≤ EMA20_i + 0.50 × ATR_i`.
  - This means a new retracement must occur after the previous signal.
  - Consecutive bars that keep breaking higher highs after a signal cannot re-trigger.
- **Session reset:** state never carries across sessions. At the first bar of each session there is no previous signal, and §5.1 already requires the whole pullback window to be inside the current session.
- **Stateless implementation:** state is **derived by replaying §5.1–§5.5 from the first bar of the current session** on every evaluation.
  - This is deterministic, and identical in the backtest and in any later live use.
  - It does not depend on where the indicator window starts.
- **Consumed by emission, not by execution:** a setup is consumed when the BUY is **emitted**, whether or not the entry is taken (review decision Q6). An entry may not be taken because:
  - a position is already open;
  - the RiskManager rejects it;
  - a halt blocks it.

  This keeps signal generation independent of portfolio state, like `MACrossover`.

### 5.6 Exit signal
- **The strategy emits no SELL/EXIT signal** (review decision Q1). Positions are closed only by the existing trade management in §6, so the `signal_exit` exit reason cannot occur.
- This isolates the entry logic. The recommendation is **not** based on the known-period finding that the benchmark's `signal_exit` trades lost money; that period may not be used for selection.

### 5.7 Short side
None. The strategy is long-only in v1.

## 6. Risk and trade management (reused unchanged)

The existing RiskManager and position-management sequence of the portfolio backtester (the mirror of `run_paper.trade_one_symbol`) are reused with the **exact production values** listed for `MA_BASELINE_V1` in the registry:

- **Sizing:** `risk_per_trade 0.005`
- **Initial stop and target:** `min_rr 1.3`, `atr_sl_mult 2.0`, `atr_tp_mult 3.0` (based on the 14-bar ATR of the **5Min** bars)
- **Trailing stop:** `trailing_atr_mult 1.5`
- **Exposure limits:** `max_positions 4`, `max_leverage 1.5`, `max_portfolio_heat 0.2`, `max_symbol_exposure 0.1`
- **Liquidity:** `min_liquidity 200000` (see limitations: it becomes a looser filter on 5Min bars)
- **Daily safeguards:** `daily_loss_limit_pct 0.03`, `max_consecutive_losses 3`, `daily_profit_halt 300`
- **Break-even:** `be_at_r 1.0`
- **Scale-outs:** `scale_out 1.0:0.5,2.0:0.5`
- **Giveback:** `max_giveback_pct 0.5`

Management is evaluated at each completed 5Min bar close. Positions can be held overnight, as in the existing framework.

The only non-risk settings that differ from the benchmark:
- the timeframe (5Min);
- the indicator window (§4: 200 bars, no 24h limit);
- the strategy itself.

## 7. Execution model

- **Fills:** every order fills at the **open of the next 5Min bar of that symbol** + 5 bps (canonical), with a commission of $0 per fill.
- **Missing next bar:** the order fills at the next bar that exists, and it is counted as a delayed fill.
- **Decisions:** only when the decision time is inside the regular session. The last bar of the day (15:55–16:00, decided at 16:00) is therefore never acted on. This is the existing engine rule.
- **Stops and targets:** checked on bar **closes** only. No intrabar high/low fills.
- **Descriptive extras on development only:** execution sensitivity at 0/2.5/10/15 bps, MFE/MAE and exit reasons may be reported for development. They are **not** gates.

## 8. Evaluation splits (from `research_protocol_v1`)

| Split | Dates | Use for H001 |
|---|---|---|
| warmup | 2023-12-01 → 2023-12-31 | indicator warm-up only |
| DEVELOPMENT | 2024-01-02 → 2025-12-31 | may be viewed; the development gate is decided here |
| VALIDATION | 2026-01-02 → 2026-05-29 | one look only, after freezing (§9) |
| KNOWN/CONTAMINATED | 2026-06-01 → 2026-09-23 | never used for selection; may be reported only as KNOWN/CONTAMINATED EVIDENCE after validation |
| FORWARD | 2026-09-24 → | chronological paper observations, only after H001 is frozen and has passed validation |

## 9. Research hygiene (binding for H001)

1. Development may be run and viewed only after the implementation is complete and its no-look-ahead tests pass.
2. **Validation is not run** until all of the following are true:
   - the implementation is complete;
   - the parameters are frozen (they are this document);
   - the development result has been reviewed and passes §10;
   - H001 is marked frozen in the registry, with the commit hash.
3. **Any change to logic or constants after the development result is viewed creates a new ID** (`STRATEGY_V2_HYPOTHESIS_002`, …). H001 is never silently edited.
4. A bug fix that changes signals after development was viewed also creates a new ID. H001's development result is kept and labeled as affected by the bug.
5. Validation is run **exactly once**. After it is viewed, H001 is frozen permanently, whatever the outcome.
6. The known/contaminated period is never used for model selection.
7. Symbols are fixed to the protocol universe (NVDA, AMD, PLTR, HOOD, MARA, INTC, MU, META). None are added or removed based on any result.

## 10. Pre-registered DEVELOPMENT gate (5 bps, canonical)

H001 proceeds to validation **only if all** of the following hold on DEVELOPMENT (2024-01-02 → 2025-12-31):

| # | Criterion | Definition (existing backtester metrics) |
|---|---|---|
| D1 | expectancy R > 0 | mean `realized_r` over completed trades (`trades.expectancy_r`) |
| D2 | profit factor ≥ 1.10 | gross realized profit / gross realized loss (`trades.profit_factor`) |
| D3 | completed trades ≥ 150 | `trades.trades` |
| D4 | max drawdown ≤ 25% | `abs(portfolio.max_drawdown_pct) ≤ 25`: mark-to-market peak-to-trough on the equity curve, including initial equity |
| D5 | total R > 0 | `trades.total_r` |
| D6 | no symbol > 50% of positive P&L | Positive-pool share ≤ 0.50; exact formula below (review decision Q7) |

**D6: exact formula (review decision Q7).**

- **Per-symbol P&L:** for each protocol symbol `s`, `P_s` = sum of `realized_pnl` over the split's **completed** trades in `s`. Commissions are included; open positions at the end are excluded.
- **Positive pool:** `Π = Σ_s max(P_s, 0)`.
- **Shares:** if `Π > 0`, each symbol's share is `share_s = max(P_s, 0) / Π`, and D6 **passes iff `max_s share_s ≤ 0.50`**.
  - Exactly 0.50 passes.
  - Compare exact floating-point values; no rounding.
  - Only one positive symbol means share = 1.0, so D6 fails.
- **Zero denominator:** if `Π = 0` (no symbol has positive net P&L, including zero trades), the shares are **undefined** and **D6 FAILS**. It is reported as `undefined: empty positive pool`, never as 0% or as a pass.
- **Reporting:** `P_s`, `Π` and every `share_s` are reported whatever the outcome.

These are research gates, not claims of future profitability. They must not be altered after development results are seen.

## 11. Pre-registered VALIDATION gate (run once, only if §10 passes and H001 is frozen)

| # | Criterion |
|---|---|
| V1 | expectancy R > 0 |
| V2 | profit factor > 1.0 |
| V3 | total R > 0 |
| V4 | max drawdown ≤ 30% |

**Directional comparison with development.** This is reported, not gated:
- trade count per trading day
- expectancy R
- PF
- max drawdown
- per-symbol concentration (the D6 positive-pool share, with the same formula and zero-denominator rule)

No tuning is allowed after validation is viewed.

## 12. Failure conditions and outcomes

- **REJECTED_AT_DEVELOPMENT:** any of D1–D6 fails. Validation is never run for H001. A follow-up idea gets a new ID with its own pre-registration, and it may not be justified by H001's validation (which was never seen).
- **REJECTED_AT_VALIDATION:** any of V1–V4 fails. H001 is frozen permanently, and validation is contaminated for any close variant.
- **Invalid result:** any of the following voids the affected result, which is recorded in the research log:
  - a look-ahead or data-leak bug is discovered;
  - the MA_BASELINE_V1 baseline is no longer bit-identical after the implementation;
  - the 5Min construction is found to use pre-market data.
- **Passing both gates** only permits chronological **paper** forward testing. It implies no real-money deployment.

## 13. Implementation acceptance requirements (before any development run)

- **Live code untouched:** the strategy lives in backtest/research code only. `run_paper`, `BrokerAlpaca`, the RiskManager, execution guards, order tracking and live sizing, stops and exits are not modified. `src/smoke_test.py` is not touched.
- **Benchmark unaffected:** `MA_BASELINE_V1` stays bit-identical on the known period, measured against `data/backtests/baseline_v1_entry_quality`.
- **No look-ahead, proven by tests:**
  - signals at `T` are invariant to modifying any bar after `T`;
  - the 5Min resampler never uses a 1Min bar after the bucket's end, or outside the regular session;
  - pre-market bars have no effect on any indicator.
- **Rule unit tests:** each rule in §5 (trend, touch, hold, trigger, 6th-bar rule, re-arm and session reset) is tested with hand-built bars.
- **Determinism:** identical outputs on repeated runs.
- **Split transparency:** NVDA signals from 2024-06-10 through 2024-06-14 are listed in the development report.
- **Warm-up support:** tests show that support bars (§4) affect indicators only. There are no signals, trades or metrics before the split start, and a split with fewer than 200 support bars fails loudly.
- **D6 tests:** normal case, exactly 0.50, a single positive symbol, and an empty positive pool.

## 14. Known limitations

1. **IEX only:** bars reflect only IEX trades. Real fills and SIP prices differ. The 5 bps assumption was optimistic relative to one recorded paper session.
2. **Data gaps:** the 2025-03-10 session is absent and 2024-12-23 is truncated. Neither is repaired.
3. **Unadjusted NVDA split:** it contaminates NVDA's indicators for a few sessions after 2024-06-10 (§3).
4. **Resampled bars:** these may differ slightly from Alpaca's native 5Min IEX bars (Q2).
5. **EMA seed:** EMAs are seeded at the window start (§4). The residual effect is negligible but non-zero.
6. **Looser liquidity filter:** the RiskManager's liquidity filter is a fixed $200k mean over 20 bars. On 5Min bars each bar holds about 5× the dollar volume, so the same threshold is roughly 5× looser than on 1Min. It is kept unchanged on purpose (entry isolation).
7. **Fixed-dollar safeguards:** `daily_profit_halt $300` is fixed in dollars and was calibrated for the benchmark. Its interaction with larger 5Min trades is untested.
8. **Stop latency:** stops react only at 5Min bar closes, with the fill at the next bar's open. Overnight holds carry gap risk, as in the existing framework.
9. **Loose "pullback" definition:** a bar hugging EMA20 inside a steady trend counts as a pullback even without any price retracement. This follows the rules as specified.
10. **Limited sample:** 2 years of development, 8 correlated high-beta US equities, long-only. Passing the gates is weak evidence and needs forward confirmation.
11. **Pre-registration is procedural:** it depends on following §9. The registry and research log are the audit trail.

## 15. Review decisions (settled 2026-09-24, before implementation; part of the frozen H001 spec)

| # | Question | Decision |
|---|---|---|
| Q1 | Exit signal | **No strategy exit signal.** Exits come only from existing trade management (§5.6). |
| Q2 | 5Min source | **Resample the audited, checksummed 1Min IEX raw cache**, regular session only (§3). |
| Q3 | Indicator window | **Last 200 regular-session 5Min bars across sessions**, no 24h limit, 150-bar warm-up, **plus 200 warm-up support bars before each evaluation split** (indicators only; §4). |
| Q4 | Slope window | `EMA_T − EMA_{T−3}`, including signal bar T (§4). |
| Q5 | Session boundary | Pullback window and `T−1` in the same session; first signal at the 6th bar (§5.1). |
| Q6 | Setup consumption | Consumed on BUY **emission**, regardless of execution (§5.5). |
| Q7 | D6 concentration | **Positive-pool share** `max(P_s,0)/Σ max(P_s,0) ≤ 0.50`; an empty pool (`Π = 0`) means **FAIL** (undefined) (§10). |
| Q8 | Touch reference | EMA20 and ATR of the pullback bar itself (§5.3). |

Interpretation note, recorded at approval: "pre-split warm-up support bars" (Q3) is read as indicator warm-up bars before each **research/evaluation split's** start date. It is not a special treatment of the NVDA stock split, which remains unadjusted and unhandled as described in §3.

## 16. Change history

- 2026-09-24: specified (SPECIFIED_NOT_IMPLEMENTED). No code, no backtest, no data viewed for H001.
- 2026-09-24: review decisions Q1–Q8 settled (§15): warm-up support bars added (§4); exact D6 positive-pool formula and zero-denominator rule added (§10). Still SPECIFIED_NOT_IMPLEMENTED. No code, no backtest, no data viewed for H001.
