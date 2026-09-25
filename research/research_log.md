# Research log (append-only)

Rules for this file:

- Append new entries at the bottom. Never edit or delete past entries. If an entry is wrong, add a correction entry that references it.
- Keep conclusions descriptive and limited to what was actually tested, on the period that was actually used.
- Always state which evidence class a result belongs to (see `config/research_protocol_v1.json`): DEVELOPMENT, VALIDATION, KNOWN/CONTAMINATED or FORWARD.
- Always state whether strategy behavior changed.

Entries before 2026-09-24 13:00 ET were reconstructed on 2026-09-24 from commits and saved reports. Dates are commit dates (America/New_York).

---

## 2026-09-24 10:35 — Confirmed-fill accounting (commit 4116b7b)

- **Question:** Is live/paper P&L booked from what the broker confirmed as filled, rather than from submitted orders?
- **Work:** `run_paper`, `order_tracking`, `broker_alpaca`, `session_summary`, `analyze_session` and `structured_logger` were changed to account P&L from confirmed order fills. Order-lifecycle tests were added.
- **Result:** P&L accounting follows confirmed fills (see `tests/test_order_lifecycle.py`).
- **Conclusion:** Accounting fix only. No performance claim.
- **Strategy behavior changed:** Signal rules and MA parameters did not change. Live order/P&L accounting code did change, and inputs derived from P&L now come from confirmed fills.

## 2026-09-24 10:58 — Historical portfolio backtester v1 / MA 3/7 baseline (commit 46173ea)

- **Question:** How does the current production strategy (MA 3/7 with production risk settings) behave on local historical bars?
- **Period:** 2026-06-01 → 2026-09-23. **Evidence class: KNOWN/CONTAMINATED.** This period was new when used, but has since been examined extensively.
- **Setup:** 8 symbols (NVDA, AMD, PLTR, HOOD, MARA, INTC, MU, META), 1Min IEX bars, fills at next-bar open + 5 bps, $0 commission.
- **Result:** 544 trades, win rate 34.9%, P&L −$24,033.32, expectancy −$44.18 / −0.107R, profit factor 0.65, max drawdown −24.50%.
- **Conclusion:** On this sample the MA 3/7 benchmark lost money under the modeled execution.
- **Strategy behavior changed:** No. The backtester reuses production strategy and risk code.

## 2026-09-24 12:39 — MFE/MAE diagnostics (commit d1abcab)

- **Question:** Do losing trades first move in favor and then give it back (an exit problem), or do they never work (an entry problem)?
- **Period:** 2026-06-01 → 2026-09-23 (KNOWN/CONTAMINATED).
- **Result:**
  - 97.5% of the 354 losing trades never reached +0.5R, and 0% reached +1R before losing.
  - signal_exit trades had a median MFE of −0.16R. 95.9% of stop_hit trades never reached +0.25R.
  - Profitable trades captured a median of ~68% of available MFE.
  - Baseline P&L stayed bit-identical after the diagnostics were added.
- **Conclusion:** In this sample, losses come mostly from entries that never moved favorably. Exits did not appear to be the main source of loss.
- **Strategy behavior changed:** No.

## 2026-09-24 12:45 — Entry Quality Diagnostics v1 (commit 8a83ea8)

- **Question:** Do any context features known at entry time (MA geometry, extension, signal bar, momentum, volatility, volume, session, gap) separate entries that never worked from entries that reached +1R?
- **Period:** 2026-06-01 → 2026-09-23 (KNOWN/CONTAMINATED).
- **Result:**
  - Separation between never-worked and +1R entries was weak for every feature: CLES ≈ 0.44–0.57, max |Spearman| ≈ 0.18.
  - Crossover age is always 0 by construction.
  - 58% of BUYs happened with a falling slow MA, and that group was only mildly worse.
  - 82% of entries fell in the first 30 minutes, confounded by loss-streak and daily-profit halts.
  - The lowest-ATR% quintile held about half of the loss.
- **Conclusion:** No simple entry feature strongly separated outcomes in this sample. No filter was derived.
- **Strategy behavior changed:** No. The baseline stayed bit-identical.

## 2026-09-24 13:00 — Execution Sensitivity v1 (commit 63667b8)

- **Question:** How sensitive is the MA 3/7 benchmark to execution slippage (0, 2.5, 5, 7.5, 10 and 15 bps)?
- **Period:** 2026-06-01 → 2026-09-23 (KNOWN/CONTAMINATED).
- **Result:**
  - At 0 bps: P&L −$3,914, PF 0.93, expectancy −0.015R.
  - At 5 bps: −$24,033, an exact baseline reproduction.
  - At 15 bps: −$52,004.
  - Empirical slope ≈ −$3.2k per +1 bps.
  - No break-even slippage within the tested range. The results are path-dependent: halts and trade sets differ between scenarios.
- **Conclusion:** On this sample the MA 3/7 benchmark stayed slightly negative at 0 bps and degraded rapidly as slippage rose.
- **Strategy behavior changed:** No.

## 2026-09-24 — Research Protocol v1 + extended historical dataset

- **Question:** None about performance. This entry is process and data work.
- **Work:**
  - Defined `config/research_protocol_v1.json`, with splits for warmup, development, validation, known/contaminated and forward.
  - Extended the IEX 1Min cache back to 2023-12-01 with `--keep-existing`, so cached bars are never rewritten.
  - Added a read-only data audit and a checksum manifest.
  - Recorded MA_BASELINE_V1 in `research/strategy_registry_v1.json`.
- **Decision:** 2026-06-01 → 2026-09-23 is permanently classified as KNOWN/CONTAMINATED for model selection.
- **Strategy behavior changed:** No.

## 2026-09-24 — MA_BASELINE_V1 benchmark across protocol splits

- **Question:** How does the unchanged MA 3/7 benchmark behave on each protocol split, evaluated separately?
- **Setup:** Production strategy and risk code, next-bar open + 5 bps, $0 commission, 8 symbols, 1Min IEX raw bars. Output: `data/research_v1/benchmark_periods.json`.
- **DEVELOPMENT EVIDENCE (2024-01-02 → 2025-12-31):** 2,592 trades, win 34.8%, P&L −$72,363.30 (−72.36%), expectancy −$27.92 / −0.105R, PF 0.64, total R −272.5, max DD −72.45%, max loss streak 25.
- **VALIDATION EVIDENCE (2026-01-02 → 2026-05-29):** 661 trades, win 33.0%, P&L −$31,038.29 (−31.04%), expectancy −$46.96 / −0.119R, PF 0.60, total R −78.3, max DD −32.29%, max loss streak 14. Viewed once for MA_BASELINE_V1, which is frozen, so this look does not contaminate it. Any new version must be frozen before its own look.
- **KNOWN/CONTAMINATED EVIDENCE (2026-06-01 → 2026-09-23):** reused validated run. A rerun on the extended cache was byte-identical. 544 trades, expectancy −0.107R, PF 0.65.
- **FORWARD EVIDENCE:** none yet.
- **Conclusion (descriptive):** In each split evaluated separately, the MA 3/7 benchmark had negative expectancy of about −0.11 to −0.12R per trade at 5 bps, with PF between 0.60 and 0.65. Dollar figures depend on the path: sizing scales with shrinking equity, so R metrics are the comparable ones across splits.
- **Data caveats:**
  - IEX feed gaps on 2025-03-10 (no bars) and 2024-12-23 (bars stop at 10:22 ET) fall in development.
  - The NVDA 10:1 split on 2024-06-10 is stored unadjusted. It did not enter the 24h strategy window (the split was on a Monday).
- **Strategy behavior changed:** No.

## 2026-09-24 — STRATEGY_V2_HYPOTHESIS_001 specified (pre-registration)

- **Question (to be tested later):** Does a 5Min regular-session trend + pullback continuation entry (long-only) give fewer, higher-quality entries than MA_BASELINE_V1, with costs a smaller fraction of R?
- **Work:**
  - Wrote `research/strategy_v2_hypothesis_001.md`: exact rules, fixed constants, reset behavior, reused risk/management, execution model, and pre-registered development and validation gates.
  - Added a registry entry with status SPECIFIED_NOT_IMPLEMENTED at spec commit cfe33dd.
- **Pending:** 8 review questions (Q1–Q8) must be settled before implementation.
- **Result:** None. Nothing was implemented, backtested or viewed for H001. Development, validation and known-period data were not touched.
- **Disclosure:** the idea was motivated by known/contaminated-period diagnostics, which therefore provide no evidence for it.
- **Strategy behavior changed:** No.

## 2026-09-24 — STRATEGY_V2_HYPOTHESIS_001 review decisions settled

- **Decisions:** Q1–Q8 approved and recorded in §15 of `research/strategy_v2_hypothesis_001.md` and in the registry.
- **Additions:**
  - 200 warm-up support bars before each evaluation split's start. They are indicator history only, with no signals, trades or metrics. This is read as research-split warm-up, not NVDA split handling.
  - Exact D6 positive-pool formula; an empty pool means FAIL.
- **Status:** still SPECIFIED_NOT_IMPLEMENTED. No code, no backtest, no data viewed for H001.
- **Strategy behavior changed:** No.

## 2026-09-24 — STRATEGY_V2_HYPOTHESIS_001 implemented; DEVELOPMENT run

- **Implementation:**
  - `src/strategy_v2_h001.py` (resampler, strategy, gates, hygiene guard) and `src/research_h001.py` (runner).
  - Backtest-only engine hooks: an injected strategy and `window_hours_limit`, both defaulting to prior behavior.
  - 41 tests; full suite 397 passing.
  - MA_BASELINE_V1 development rerun after the hooks: bit-identical.
  - The known period was not run, per instruction.
- **Implementation note (no rule change):** each bar's EMA/ATR is computed on its own 200-bar window, i.e. what was computed when that bar was the signal bar. The §5.5 session replay therefore reproduces the emitted signals exactly.
- **DEVELOPMENT EVIDENCE (2024-01-02 → 2025-12-31), 5 bps:**
  - 1,679 trades, win 41.2%, P&L −$38,012.72, expectancy −0.0586R, PF 0.795, total R −98.46, max DD −44.10%.
  - 200 support bars per symbol.
  - The unadjusted NVDA split suppressed NVDA signals on 2024-06-10 and 06-11; the first signal came on 2024-06-12.
- **Pre-registered gate:**
  - D1 FAIL, D2 FAIL, D3 PASS, D4 FAIL, D5 FAIL.
  - D6 FAIL: positive pool $3,576.48; MARA 97.2%, MU 2.8%; all other symbols negative.
- **Outcome:** progression to validation **FAIL**, so H001 is REJECTED_AT_DEVELOPMENT (spec §12). Validation was never run and never viewed. H001 is closed; any follow-up needs a new ID and a new pre-registration.
- **Strategy behavior changed:** No live behavior changed.

## 2026-09-25 — H001 DEVELOPMENT AUTOPSY (diagnostic only)

- **Question:** Why does H001 lose on DEVELOPMENT, and what descriptive evidence exists for a future hypothesis?
- **Scope:** DEVELOPMENT only, 2024-01-02 → 2025-12-31.
  - Bars were loaded only up to 2025-12-31.
  - The rerun reproduced the stored H001 `trades.json` byte-for-byte.
  - Validation, known and forward were not accessed.
- **Output:** `data/research_v1/h001_autopsy/`.
- **Results (DEVELOPMENT EVIDENCE):**
  - **Funnel:** 10,491 signals generated → 4,965 reached risk → 1,679 accepted and completed.
    - Not reaching risk: 3,123 daily-profit-halt, 1,626 loss-streak, 777 already in position.
    - Rejected: 1,984 RR, 1,069 liquidity, 210 leverage, 23 max positions.
    - RR rejections act as an implicit ATR% floor: rejected max 0.287% vs accepted min 0.288%.
  - **Losers (987):** 79.7% never reached +0.25R and 94.5% never reached +0.5R; 0.2% reached +1R. Median MFE +0.07R, median MAE +0.41R, median 15 min to MAE.
  - **Exit reasons:**
    - stop_hit: 354 trades, −$136,674; 98.6% never reached +0.25R.
    - giveback_close: 1,259 trades, +$65,327 overall; 514 with MFE < 0.25R lost −$32,326, while 204 with MFE ≥ 1R made +$85,928.
    - take_profit_hit: 66 trades, +$33,333 (22.6% of gross profit).
  - **Pullback depth:** penetrated −0.057R (n=1,057), close −0.042R (n=312), loose −0.082R (n=310). Small differences; all negative.
  - **Trend age:** 0–12 bars −0.113R; 13+ bars −0.010 to −0.060R.
  - **Trigger:** close below midpoint −0.174R (n=125); strongest body tercile −0.032R.
  - **ATR% quintiles:** not monotonic (Q2 worst −0.129R; Q3 +0.005R). Modeled slippage cost ranges from 0.155R (Q1) to 0.051R (Q5).
  - **Time of day:** only 14:00–16:00 was positive (+0.088R, n=137), confounded by overnight holds. The 41 overnight trades made +0.855R / +$11,305; the 1,638 intraday trades made −0.082R / −$49,318.
  - **Separation:** weak everywhere; CLES about 0.44–0.57 for all entry features. No evidence of chasing (entry-distance medians nearly identical).
  - **Versus MA_BASELINE_V1 on development:**

    | Metric | MA_BASELINE_V1 | H001 |
    |---|---|---|
    | Expectancy | −0.105R | −0.059R |
    | Profit factor | 0.64 | 0.80 |
    | Max drawdown | −72.5% | −44.1% |
    | Max loss streak | 25 | 12 |

    H001 is still negative.
- **Conclusion (descriptive):** H001's losses are dominated by immediate failures that hit their stops. The entry features studied do not separate these failures from winners. The accepted-trade sample is strongly shaped by the RiskManager's RR floor and by the daily halts.
- **Strategy behavior changed:** No. H001 is unchanged and closed; no H002 was created.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_002 specified (pre-registration)

- **Hypothesis:** Frozen H001 plus one trade-management rule, the Failure-to-Progress exit.
  - At the decision on the 3rd post-fill 5Min close (the fill bar counts as the 1st), exit all remaining shares if both hold:
    - close-based MFE < +0.25R (strict);
    - that close ≤ the actual entry fill price (equality triggers).
  - The exit fills at the next available bar open with sell slippage.
- **Unchanged:** entries, risk, sizing, execution and all existing exits. FTP runs only after the unchanged H001 management sequence, and only if no exit order was submitted at that decision.
- **Constants:** 3 bars and +0.25R, derived from the viewed H001 DEVELOPMENT autopsy. H002's development result is therefore in-sample for them; validation would be the first independent test. No alternatives will be tested under this ID.
- **Gates:** identical to H001 (D1–D6, including the exact D6 positive-pool formula; V1–V4).
- **Comparison plan:** a required matched-trade comparison against H001 on development, covering losses avoided vs winners sacrificed.
- **Pending:** 5 review decisions (R1–R5).
- **Spec document:** `research/strategy_v2_hypothesis_002.md`, written at commit 0bd6d95.
- **Status:** SPECIFIED_NOT_IMPLEMENTED. No code; no data viewed for H002; validation, known and forward untouched.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_002 review decisions settled

- **R1 (revised):** When the 3rd post-fill close is the 15:55 bar, FTP is evaluated at 16:00 using exactly the first three closes, with the third close as current_close. If it triggers, the exit is pending and fills at the next available regular-session open. FTP is neither skipped nor deferred; the pending path is backtest-only.
- **R2–R5:** approved.
  - R2: the H001 sequence runs first; FTP only if no exit was submitted.
  - R3: FTP measures from the actual fill.
  - R4: the original initial risk per share.
  - R5: FTP P&L feeds all state like any exit; divergence is reported.
- **Disclosure added:** H002 DEVELOPMENT is a screening/resubstitution check only, not an independent test. VALIDATION is the first untouched test, viewable once after a complete freeze.
- **Status:** SPECIFIED_NOT_IMPLEMENTED. No code, no data viewed.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_002 specification FROZEN

- **R6 confirmed:** the engine's day-boundary accounting is kept exactly.
  - An overnight FTP fill at the next session's open is in equity, the ledger, total P&L and `daily_results` (fill date).
  - It does not carry into the new day's loss streak, loss limit or profit halt after the reset.
  - It was not changed for H002, because that would add a second experimental variable. It is documented as an inherited convention.
- **Additions:**
  - explicit day-boundary tests (spec §11.13b);
  - required H002 development metrics: `overnight_ftp_fills`, `overnight_ftp_realized_pnl`, `overnight_ftp_losses`, `overnight_ftp_wins` (plus breakevens and checkpoint path). Overnight means the NY fill date differs from the NY FTP decision date.
- **Status:** specification approved and FROZEN; still SPECIFIED_NOT_IMPLEMENTED. Implementation starts only after the frozen spec is committed, and that commit hash will be recorded in the spec header and the registry.
- **Data:** no code; no data viewed.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_002 implemented; DEVELOPMENT run

- **Implementation:** `src/strategy_v2_h002.py` (FTPEngine, a research-only subclass; the shared engine is unchanged), `src/research_h002.py` and 36 tests; full suite 474 passing. Frozen spec commit 6b0ac23.
- **Invariance:** with FTP off, the development run is byte-identical to stored H001 (trades.json, trades.csv, equity_curve.csv, daily_results.csv) and summary.json is equal.
- **DEVELOPMENT (screening/resubstitution only), 5 bps:**
  - 1,808 trades, win 32.7%, P&L −$45,210.39, expectancy −0.0689R, PF 0.726, total R −124.66, max DD −49.61%, max loss streak 17.
  - Exits: failure_to_progress 613 (−$88,247), giveback 1,046 (+$52,592), stop 88 (−$38,040), take-profit 61 (+$28,484).
  - FTP checkpoints: 1,504 evaluated; the condition was met on 755. 613 triggered and 289 were blocked by an existing exit on the same decision (checkpoints can repeat both counts).
- **Matched against H001 (1,486 matched; 193 only in H001; 322 only in H002):**
  - Losses avoided: 244 H001 stop-hits closed by FTP, +115.0R / +$44,541 (mean +0.47R each).
  - Winners sacrificed: 120 H001 winners closed by FTP, −100.7R / −$35,315 (112 giveback, 6 take-profit, 2 stop).
  - Also: 134 H001 giveback losers closed by FTP, −19.1R / −$6,027.
- **P&L bridge (exact):** −$7,197.67 = matched change +$911.54 + H002-only −$13,520.43 − H001-only (−$5,411.21).
- **Overnight FTP:** 2 fills (both via the 16:00 path), −$721.09, 2 losses, 0 wins.
- **Gates:**
  - D1 FAIL, D2 FAIL, D3 PASS, D4 FAIL, D5 FAIL.
  - D6 FAIL: MARA is the only positive symbol, 100% of a $1,380.59 pool.
- **Outcome:** progression to validation **FAIL**, so H002 is REJECTED_AT_DEVELOPMENT. Validation, known and forward were never run. No variants will be tested under this ID.
- **Strategy behavior changed:** No live behavior changed.

## 2026-09-25 — H001 DEVELOPMENT opportunity / signal-selection autopsy (diagnostic only)

- **Question:** Does the portfolio's routing (RiskManager rejects, halts, symbol-already-open, same-timestamp competition) select better, worse or roughly random H001 opportunities?
- **Scope and checks:** DEVELOPMENT only; bars loaded to 2025-12-31. The H001 rerun was byte-identical to the stored run.
- **Method:**
  - Shadow outcomes are counterfactual only: entry at the next bar's open + 5 bps, R_ps reconstructed exactly (1,679 of 1,679 match), close-based 15/30/60-minute and end-of-session horizons.
  - Isolated shadow trades use the unchanged engine in an empty single-symbol portfolio. They were validated on all 1,679 accepted trades (entry, exit reason and R all identical).
- **Results (DEVELOPMENT EVIDENCE):**
  - **Routes:** accepted 1,679; daily-profit halt 3,123; RR 1,984; loss-streak 1,626; liquidity 1,069; symbol already open 777; leverage 210; max positions 23.
  - **Leverage rejects:** isolated expectancy −0.061R (PF 0.82), essentially the same as accepted (−0.059R, PF 0.81).
  - **Max-position rejects:** +0.053R (n=23, too small to interpret).
  - **Halts:** 68–69% of halt-blocked signals would have been rejected by the RR or liquidity check anyway. These signals are mostly afternoon and low-ATR (median ATR% 0.26 vs 0.50 accepted).
    - Of the rest, isolated expectancy is −0.094R (profit halt, n=987) and −0.184R (loss streak, n=500), versus −0.059R accepted.
    - Halt signals are conditional on prior outcomes, so they are not independent samples.
  - **RR and liquidity rejects:** low ATR%, afternoon. Their fixed-horizon MFE and MAE are both larger in R units (small R); end-of-session R is −0.04 (RR) and −0.07 (liquidity); exit costs are excluded.
  - **Symbol-already-open:** the eventual host trade won in 362 cases and lost in 415; repeat-signal rate per held bar 0.066 (winners) vs 0.070 (losers). There is no sign that these signals confirm good positions.
  - **Same-timestamp competition:** 787 groups, 72 of them with both an acceptance and a capacity rejection. In those, accepted trades averaged +0.203R versus a +0.043R isolated shadow for the rejected ones.
  - **Ex-post regret** (future information; not a rule): by 60-minute MFE, the best rejected candidate beat the accepted one in 52.8% of groups; median realized-R regret −0.11R.
  - **Processing order:** acceptance falls with rank in the group (39.7% at rank 1 to about 10% at ranks 4–5) without better outcomes at early ranks. Capacity rejects are only 233 of 4,965 risk decisions.
  - **H002 path divergence:** of the 322 H002-only trades, 171 (−$9,630, −28.4R) were H001 symbol-already-open signals, i.e. re-entries into a symbol after FTP closed the first position. 97 (−$3,176) were H001 profit-halt-blocked.
- **Conclusion (descriptive):**
  - Within capacity-constrained competition, allocation looks roughly random to slightly favorable.
  - Leverage-rejected signals look like accepted ones.
  - Halt-blocked signals that passed the RR and liquidity checks were on average weaker than accepted signals, confounded by time of day.
  - H002's damaging path divergence came mainly from same-symbol re-entries.
  - No rule was derived; no H003 was created.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_003 specified (proposed pre-registration)

- **Hypothesis:** new signal family: a 5Min regular-session trend-filtered consolidation breakout, long-only.
  - **Trend:** close > EMA50 and EMA50 3-bar slope > 0.
  - **Consolidation:** the six same-session bars before T have range ≤ 2.00 × ATR14(T−1).
  - **Breakout:** close_T > consolidation high (strict), a bullish bar, and close location ≥ 0.75.
  - **Setup state:** consumed on emission; re-arm needs a fresh six-bar window after the previous signal; resets each session.
- **Exits and risk:** no strategy exit; generic trade management and production risk values unchanged.
- **Not reused:** H001's pullback rule and H002's FTP exit.
- **Gates:** identical to H001/H002 (D1–D6, including the exact D6 formula; V1–V4).
- **Contamination disclosure:**
  - Development is hypothesis-development evidence only.
  - The 0.75 close-location threshold coincides with an H001 autopsy bucket boundary already viewed.
  - Validation is the first untouched test.
- **Pending:** 5 review questions (Q1–Q5).
- **Spec document:** `research/strategy_v2_hypothesis_003.md`, written at commit ed4967a.
- **Status:** SPECIFIED_NOT_IMPLEMENTED. No code; no H003 data produced or read; validation, known and forward untouched.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_003 review decisions settled

- **Q1:** the earliest signal bar starts at 10:00 ET; it is decided at its 10:05 close, and the entry fills at the 10:05 bar open.
- **Q2:** the window is the six existing same-session bars; non-contiguous windows are allowed and nothing is fabricated. The non-contiguous signal and trade counts are diagnostics only.
- **Q3:** H001 indicator semantics inherited (own-window EMA200, 278-bar context, 150-bar guard); exactly 200 support bars are still required.
- **Q4:** an invalid ATR(T−1) means no signal, with no fallback.
- **Q5:** ATR(T−1) is used for report normalization.
- **Disclosure kept:** the ≥ 0.75 close-location boundary was already viewed in H001 development; H003 development is hypothesis-development evidence, and validation is the first untouched test.
- **Status:** SPECIFIED_NOT_IMPLEMENTED. No other rule or constant changed. No code; no data.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_003 implemented; DEVELOPMENT run

- **Implementation:** `src/strategy_v2_h003.py` and `src/research_h003.py` (research-only; existing strategy injection; no shared code changed), plus 33 tests. Full suite 529 passing. Frozen spec commit 6eb6078.
- **DEVELOPMENT (hypothesis-development evidence only), 5 bps:**
  - **Funnel:** 4,497 BUY signals → 1,320 accepted, 1,894 rejected (RR 1,165; liquidity 581; leverage 130; max positions 18). Pre-risk blocks: loss streak 373, profit halt 782, symbol already open 128.
  - **Results:** 1,320 trades, win 41.4%, P&L −$32,706.95 (−32.71%), expectancy −$24.78 / −0.0614R, PF 0.791, total R −81.03, max DD −35.34%, max loss streak 17.
  - **Exits:** giveback 1,022 (+$56,726), stop 257 (−$113,354), take-profit 41 (+$23,921); 198 scale-out legs (+$56,405).
  - **Signal structure (medians, all signals / trades):** range/ATR 1.664 / 1.625; breakout-bar range/ATR 0.991 / 0.891; breakout distance/ATR 0.375 / 0.313.
  - **Other:** non-contiguous windows on 23 signals and 0 trades; 6 NVDA split-week signals; 2,037 same-session re-arms.
- **Gates:**
  - D1 FAIL, D2 FAIL, D3 PASS, D4 FAIL, D5 FAIL.
  - D6 FAIL: MARA is the only positive symbol, 100% of a $2,374.43 pool.
- **Versus DEVELOPMENT benchmarks:**

  | Strategy | Expectancy | PF | Max DD |
  |---|---|---|---|
  | MA_BASELINE_V1 | −0.105R | 0.64 | −72.5% |
  | H001 | −0.059R | 0.80 | −44.1% |
  | H003 | −0.061R | 0.79 | −35.3% |

- **Outcome:** progression to validation **FAIL**, so H003 is REJECTED_AT_DEVELOPMENT. Validation, known and forward were never run. Any follow-up is H004.
- **Strategy behavior changed:** No live behavior changed.
