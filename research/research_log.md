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

## 2026-09-25 — Shared trade-management autopsy, H001 vs H003 (DEVELOPMENT, diagnostic only)

- **Question:** Do H001 and H003 lose through the same management mechanics, and how much stop damage comes from close-based detection and next-bar execution?
- **Scope and checks:**
  - DEVELOPMENT only; bars loaded to 2025-12-31.
  - Both reruns were byte-identical to their stored runs (trades.csv, trades.json, daily_results.csv, equity_curve.csv; summary.json equal).
  - Read-only exit observer; no shared code changed.
- **Results (DEVELOPMENT EVIDENCE):**
  - **Signature (H001 / H003):**
    - expectancy −0.059 / −0.061R; PF 0.795 / 0.791; win 41.2 / 41.4%;
    - median MFE +0.235 / +0.228R; reach +0.25R 48.6 / 48.7%;
    - immediate failures 46.9 / 46.8%; stop share 21.1 / 19.5%, stop average −1.05 / −1.06R;
    - giveback share 75 / 77%, capturing 0.30 / 0.29 of MFE;
    - median hold 25 / 25 min; cost 0.100 / 0.103R;
    - geometry identical (stop 2.00 ATR, take profit 3.00 ATR, approximate RR 1.38, ATR% ≈ 0.49–0.50).
  - **Stops:** 98.6 / 95.7% never reached +0.25R; median MFE −0.18 / −0.16R.
    - 66 / 70% had a trailing stop tighter than the initial stop (median stop level −0.86 / −0.82R from the fill).
    - Median path from stop level to realized: the close was 0.11R through the stop at detection, then the next-open fill cost 0.04R more, for about −1.02R realized.
    - Detection-to-fill latency cost: median −0.044 / −0.042R; −$6,262 / −$4,728 in total, about 4.5% of stop P&L.
    - Loss beyond −1R: −$20,500 / −$20,049.
  - **Post-stop (future information; not a rule):** within 60 minutes, 79 / 78% closed below the actual exit, 20 / 23% recovered above entry, and 5 / 4% reached +1R.
  - **Lifecycle:** immediate failures (MFE < +0.25R and a loss) totalled −$169,721 / −$140,022. All other buckets combined were positive: +$131,708 / +$107,315.
  - **Differences:** H003 reached +1R less often (13.4 vs 16.1%), reached +0.25R later (15 vs 10 min), and took fewer take profits (3.1 vs 3.9%). Overnight: 41 trades at +0.855R (H001) vs 27 at +0.299R (H003). Every CLES comparing the two distributions was 0.49–0.53.
- **Conclusion (descriptive):** Both entry families produce nearly identical excursion distributions. The shared RiskManager and management layer normalizes geometry (2 ATR stop, 3 ATR target, implicit ATR% floor) and yields the same payoff profile: about 47% immediate failures that lose about 0.55R each on average, against modest giveback captures. Detection-to-fill latency is a small share of stop losses. No rule was derived; no H004 was created.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_004 specified (H003 + SPY broad-market regime gate)

- **Event:** specification only, written at `5bba9da`. Status SPECIFIED_NOT_IMPLEMENTED. Spec: `research/strategy_v2_hypothesis_004.md`; registry entry added.
- **Hypothesis:** some H003 breakouts fail because the broad market is not in an upward regime. Requiring a positive SPY regime before an H003 BUY may reduce immediate failures, with the stock signal and management unchanged.
- **Single change versus H003:** an entry-only gate. The H003 raw BUY proceeds to the existing path only if the latest **completed** regular-session 15Min SPY bar (end ≤ decision time) has `close > EMA50` and `EMA50 − EMA50_{R−3} > 0`, both strict. Otherwise it is `MARKET_REGIME_BLOCKED`, or `MARKET_REGIME_UNAVAILABLE` if the expected bucket is missing (no older-bucket fallback). Blocked signals never reach the RiskManager. The setup is consumed on raw emission regardless of the gate. There is no new exit.
- **SPY role:** context only; never traded, sized, in P&L or in D6. Alpaca IEX raw, 200 15Min support bars, no SIP switch.
- **Gates:** D1–D6 and V1–V4 are unchanged from H001–H003.
- **Contamination:** the motivation comes from already-viewed development research (shared management autopsy). The parent's development results are known, and general macro knowledge of 2024–2026 exists. Development is hypothesis-development evidence; validation is the first untouched test.
- **Open review questions:** Q1–Q7 (EMA convention, bucket availability, SPY download range, manifest location, shadow definitions, path-decomposition groups, reproduction mode).
- **Not done:** no implementation, no SPY download or read, no H004 run. Validation, known and forward untouched.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_004 review decisions (specification only)

- **Settled Q1–Q7:**
  - **Q1:** the SPY EMA inherits H003's own-window convention exactly, including split-boundary behavior.
  - **Q2:** a bucket exists with at least one valid 1Min bar. Only the expected bucket is checked. `R−3` counts existing bars back. `spy_slope_windows_spanning_missing_bucket` is a diagnostic only.
  - **Q3:** SPY is downloaded for support + development only. Validation-range SPY stays unopened until a development pass, a frozen implementation and a recorded commit.
  - **Q4:** new `research/context_manifest_spy_v1.json`; the stock manifest is untouched.
  - **Q5:** `weak_forward_excursion` = 60m shadow MFE < +0.25R. Blocked and passed groups are each reported for all valid observations and for the stateless RR + liquidity subset.
  - **Q6:** path groups A / B / B′ / C, with A + B + B′ = H003 trades.
  - **Q7:** a gate-disabled mode exists only for tests and the pre-run H003 reproduction check, with no bypass flag and an abort on mismatch.
- **Wording correction:** H003 is not stateless within a session. The correct reason the gate cannot alter H003's raw signal sequence is now stated. Documentation only; no behavior change.
- **SPY audit hard-fail rule, fixed before any SPY data is seen:** the audit fails on (1) structural/integrity failure, (2) fewer than 200 valid pre-development 15Min support bars, or (3) a required development session with zero usable SPY 1Min bars. Partial missing buckets never fail the audit; they are handled by `MARKET_REGIME_UNAVAILABLE`. There is no percentage threshold.
- **Open:** Q8, the definition of a "required session", raised because of the known 2025-03-10 IEX gap.
- **Status:** SPECIFIED_NOT_IMPLEMENTED. No implementation, no SPY download or read, no H004 run. Validation, known and forward untouched.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_004 Q8 settled (specification only)

- **Q8, option (a):** a required H004 development session is "any Development calendar date for which at least one of the eight tradable symbols has at least one valid regular-session bar in the existing stock cache."
- **The list:**
  - derived only from the stock cache, before any SPY download or inspection;
  - independent of H004 outcomes and of whether H003 signals that day;
  - frozen for the development audit;
  - never extended with zero-coverage dates from an external exchange calendar.
- **Consequences:**
  - The 2025-03-10 all-stock IEX gap is excluded if the cache confirms zero bars for all eight symbols that day.
  - A required session with zero SPY bars hard-fails the audit.
  - Partial SPY gaps only trigger `MARKET_REGIME_UNAVAILABLE`.
- **Manifest fields added to the spec:** `required_session_definition`, `required_session_count`, `required_session_dates` (or a hash of the list), `excluded_zero_stock_coverage_dates`, `required_sessions_with_zero_spy_data`, `required_sessions_with_partial_spy_gaps`.
- **Status:** Q1–Q8 are all settled; SPECIFIED_NOT_IMPLEMENTED. No implementation, no SPY download or read, no H004 run. Validation, known and forward untouched.
- **Strategy behavior changed:** No.

## 2026-09-25 — H004 SPY context data prepared and audited (data only; no H004 implementation or run)

- **Frozen spec:** `31526c2`. Module `src/spy_context_data.py`, run in the order sessions → download → audit. Manifest: `research/context_manifest_spy_v1.json`. The stock manifest is untouched.
- **Required sessions (Q8), derived from the stock cache only, before any SPY access:**
  - 501 dates, 2024-01-02 → 2025-12-31, sha256 `208bb7bf873f952cc6183c79c80dcce465910b4678f87598ac99c400d73cbea3`;
  - two derivations were identical;
  - the stock cache matches `historical_manifest_v1.json`;
  - excluded (zero stock coverage): 2025-03-10 only.
- **SPY download:** Alpaca v2, IEX, raw, 1Min, 2023-12-01 → 2025-12-31 only, no SIP.
  - 200,276 rows, 2023-12-01T14:30Z → 2025-12-31T20:59Z, sha256 `7af01690…9930ed`.
  - Integrity clean: 0 duplicates, 0 out of order, 0 bad OHLC, 0 non-positive prices, 0 negative volume, 0 NaN or non-finite values.
- **15Min bars:** 13,452 bars, all anchored and monotonic. Early closes end with the 12:45 bucket. 1–15 minutes per bucket (observed 5–15).
- **Support:** 200/200 bars (2023-12-19 11:30 → 2023-12-29 15:45 ET).
- **Coverage:**
  - zero-SPY required sessions: none;
  - partial gaps: 2024-12-23 only, with 22 missing buckets (IEX truncated at about 10:22, as for stocks);
  - buckets: 12,954 expected, 12,932 existing, 22 missing;
  - `spy_slope_windows_spanning_missing_bucket` (bucket level): 3.
- **H004 data readiness:** PASS. No H004 trading outcome was computed. Validation, known and forward SPY data were not accessed.
- **Strategy behavior changed:** No.

## 2026-09-25 — STRATEGY_V2_HYPOTHESIS_004 implemented and evaluated on DEVELOPMENT (H003 + SPY regime gate)

- **Implementation:** frozen spec `31526c2`.
  - `src/strategy_v2_h004.py` (gate wrapper around frozen H003) and `src/research_h004.py` (runner).
  - `src/spy_context_data.py` refactored so the slope-gap diagnostic also gives per-bar flags; its outputs are unchanged.
  - No shared engine, RiskManager, H003 or live code changed.
- **Pre-run checks:**
  - The internal gate-disabled mode reproduced stored H003 development byte for byte (trades.csv, trades.json, daily_results.csv, equity_curve.csv; summary.json equal).
  - SPY manifest PASS and checksum `7af01690…` verified.
  - Time-alignment audit: 4,497 rows, 0 violations.
- **Funnel:**
  - 4,497 raw H003 BUY signals (identical to H003) = 1,166 `MARKET_REGIME_BLOCKED` + 0 `UNAVAILABLE` + 3,331 passed.
  - Of the passed signals: 89 had the symbol already open, 601 hit the profit halt, 295 hit the loss-streak halt, and 2,346 reached the RiskManager.
  - At the RiskManager: 926 accepts; rejects were 913 RR, 400 liquidity, 98 leverage and 9 max positions.
- **Results (DEVELOPMENT EVIDENCE):**
  - 926 trades, 41.9% win rate, −$23,305 (−23.3%).
  - Expectancy −$25.17 / −0.0598R; PF 0.781; total R −55.34; max DD −26.48%; max loss streak 10.
  - Exits: giveback 721 (+$36,056), stop 178 (−$75,584), take profit 27 (+$16,222); 142 scale-out legs (+$36,046).
- **Gates:** D1 FAIL, D2 FAIL, D3 PASS, D4 FAIL (−26.48%), D5 FAIL, D6 FAIL (MARA is 100% of a $3,474 pool).
- **Immediate failures:** H003 46.8% vs H004 47.3%.
- **Path decomposition:**
  - A = 878 trades (−$19,951 in H003 / −$20,606 in H004).
  - B (regime-removed) = 421 trades (−$11,513).
  - B′ (passed the gate, then removed by path divergence) = 21 trades (−$1,243).
  - C (new in H004) = 48 trades (−$2,699).
  - Bridge: −$32,707 + $11,513 + $1,243 − $655 − $2,699 = −$23,305. It reconciles.
- **Shadow (descriptive; blocked vs passed, all valid signals):**
  - 60m median MFE 0.465 vs 0.479R; reached +1R 26.3 vs 27.9%; reached −1R 32.2 vs 32.0%.
  - weak_forward_excursion: 37.9 vs 35.9%. For the RR + liquidity subset: 38.8 vs 37.1%.
  - The stateless check agreed with the RiskManager on 2,337 of 2,337 signals.
- **Regime:**
  - 74.1% positive, 25.9% negative, 0% unavailable.
  - Median SPY close vs EMA50 +0.27%; median 3-bar EMA50 slope +0.032%.
  - Signal-level slope-gap windows: 1 (INTC 2024-12-24).
  - 2024-12-23: 0 raw signals, 0 unavailable.
- **Post-run reporting correction:** `exit_rates` used labels that don't match the engine's exit codes. It was recomputed from the stored trades.json with no re-run and no metric changed.
- **Outcome:** progression to validation **FAIL**, so H004 is REJECTED_AT_DEVELOPMENT. Validation, known and forward were never run, and validation-range SPY was never downloaded. Any follow-up is H005.
- **Strategy behavior changed:** No live behavior changed.

## 2026-09-25 — Research Sanity Audit V1 (diagnostic only; no H005)

- **Scope:**
  - H001/H003 strategy diagnostics on DEVELOPMENT only.
  - IEX-vs-SIP comparison on KNOWN (2026-06-01 → 2026-09-23) only.
  - Validation and forward untouched.
  - Module `src/research_sanity_audit.py`; outputs in `data/research_v1/research_sanity_audit_v1/`.
- **Reproduction:**
  - H001 and H003 development reproduced byte-identically.
  - The portfolio-free raw-signal generator matched the engine exactly (10,491 / 4,497 signals).
- **Execution (full portfolio reruns, 0 / 2.5 / 5 / 7.5 / 10 / 15 bps):**
  - H001 expectancy +0.063 / +0.002 / −0.059 / −0.117 / −0.164 / −0.258R.
  - H003 expectancy +0.051 / −0.004 / −0.061 / −0.109 / −0.158 / −0.268R.
  - Descriptive break-even (linear interpolation): H001 ≈ 2.6 bps per side, H003 ≈ 2.3 bps per side. Not an achievable-fill claim.
- **Raw signals vs 200 matched-random controls (0 bps; same symbol / month / 30-minute bucket / point-in-time ATR% quintile):**
  - Real signals are **not** superior.
  - Median 60m return R sits at the 1.0 / 1.5 percentile of the random replicates for H001 / H003.
  - Weak-forward-excursion rate sits at the 100th percentile, i.e. worse than random.
  - The isolated same-management trade test at 5 bps gives real −0.089 / −0.060R vs a random median of −0.088 / −0.070R.
- **Risk stages:**
  - H001 vs H003 are already nearly indistinguishable at the raw stage: max |CLES − 0.5| = 0.024; 0.017 at stage 2 and 0.015 at stage 3.
  - No risk-filter compression by the predeclared criterion.
  - The stateless RR / liquidity filters mainly shift volatility and cost geometry (CLES for ATR% ≈ 0.90) with no directional improvement (CLES for 60m return ≈ 0.50).
- **Feed (KNOWN):**
  - SIP was available on the existing account, and every file passed integrity checks.
  - 5Min bar overlap was ~100%. Median 5Min close difference was 0.5–3.9 bps (P95 2.6–16.4 bps).
  - Raw-signal Jaccard IEX vs SIP: H001 0.69, H003 0.62 → DATA_FEED_SENSITIVE (predeclared band).
- **Labels (predeclared matrix):** H001 and H003 both get SIGNAL_EDGE_TOO_SMALL_FOR_COST (positive at 0 bps, negative at 5 bps) and DATA_FEED_SENSITIVE. No RISK_FILTER_COMPRESSION.
- **Caveat:** the 0 bps gross edge is not attributable to signal selection, because matched random entries show equal or better forward returns.
- **Strategy behavior changed:** No.
