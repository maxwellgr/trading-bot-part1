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
