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
