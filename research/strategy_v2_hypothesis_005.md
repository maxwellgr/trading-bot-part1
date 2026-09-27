# STRATEGY_V2_HYPOTHESIS_005 — 30Min relative-strength trend continuation (long-only)

| Field | Value |
|---|---|
| Hypothesis ID | `STRATEGY_V2_HYPOTHESIS_005` |
| Name | 30Min relative-strength trend continuation |
| Status | **SPECIFIED_NOT_IMPLEMENTED** |
| Protocol | **Research Protocol V2** (`config/research_protocol_v2.json`, `research/research_protocol_v2.md`) |
| Frozen protocol commit | `565f7aa4b7e72a55780ecfe59ada805f166e4290` (`565f7aa`, "feat: add research protocol v2") |
| Date created | 2026-09-27 |
| Spec written at commit | `565f7aa4b7e72a55780ecfe59ada805f166e4290` |
| Frozen spec commit | *(recorded after this document is committed)* |
| Direction | long-only |
| Design review | closed 2026-09-27; zero open design choices |

No H005 code exists, no H005 signal has been generated, and no Development outcome has been viewed. Nothing here may change after any H005 stage has been viewed; any meaningful change requires a new hypothesis ID (Protocol V2 §14).

---

## 1. Hypothesis

At the close of a completed 30Min regular-session bar, a stock is hypothesized to show **superior typical 60-minute forward continuation** compared with matched random 30Min bars when it meets all three conditions:
- **A.** it is positive versus its own session open;
- **B.** it is outperforming SPY over the identical session window;
- **C.** it is at a new session closing high.

**Primary test statistic:** the median 60-minute forward R at 0 bps, computed on `matched_real_population` (§10). Admission requires the frozen Protocol V2 P2 threshold: an oriented empirical percentile ≥ 95 against 200 matched-random replicates.

## 2. Universe

- **Tradable:** NVDA, AMD, PLTR, HOOD, MARA, INTC, MU, META.
- **SPY:** context only. It is never traded and never counted in D6.

## 3. Data and time

**Development and history**
- Development runs 2024-01-02 → 2025-12-31. History support begins 2023-12-01.
- **No data after 2025-12-31** may enter the Development signal or economics research, and loaders are bounded at that date.

**Bars**
- 30Min regular-session buckets `[t, t+30min)`, anchored at 09:30 ET.
- They are built with the frozen RTH resampling convention at 30 minutes: only 1Min bars whose start lies in the regular session; first / max / min / last / sum aggregation.
- A bucket exists if it contains at least one 1Min bar. Nothing is fabricated or interpolated.

**Scheduled closes**
- 16:00 on regular sessions; 13:00 on the frozen `EARLY_CLOSES` sessions.
- The scheduled close is **never inferred from observed data**.
- Full sessions have 13 buckets (09:30 … 15:30); early-close sessions have 7 (09:30 … 12:30).

## 4. Feeds

| Declaration | Value |
|---|---|
| `PRIMARY_RESEARCH_FEED` | IEX |
| `INTENDED_LIVE_SIGNAL_FEED` | IEX (matches `src/broker_alpaca.py`, `feed=iex`) |
| `SECONDARY_ROBUSTNESS_FEED` | SIP |
| `ADJUSTMENT` | raw |
| `TIMEFRAME` | 30Min RTH |
| `DATE_RANGE` | Development 2024-01-02 → 2025-12-31 (history from 2023-12-01); feed robustness on KNOWN 2026-06-01 → 2026-09-23 only |

- Feeds are never mixed inside one run.
- SPY is always taken from the **same feed** as the stocks it is compared with.

## 5. Signal window

- **Decision time:** D = end of the completed signal bar T.
- **Eligible window:** `11:00 ET ≤ D ≤ scheduled_close(S) − 60 min`. That is 11:00–15:00 on full sessions and 11:00–12:00 on early-close sessions.
- **Earliest decision (11:00):** three completed session bars exist, so "new session closing high" is meaningfully established.
- **Emission:**
  - at most **one** emitted signal per symbol per session;
  - only the first qualifying evaluable bar emits;
  - there is no re-arm, and nothing carries across sessions.

## 6. Exact signal

**Notation** for symbol X and session S:
- `O_X(S)` = open of X's 09:30 bucket;
- `C_X(T)` = close of completed bucket T;
- SPY uses its bucket with the **identical start time**: `O_SPY(S)` and `C_SPY(T)`.

**Evaluable bar.** A bar T of X is evaluable iff all hold:
- D is in the §5 window;
- X's 09:30 bucket exists;
- SPY's 09:30 bucket exists;
- SPY's bucket T exists;
- X's bucket T exists.

**Conditions**
```
A:  C_X(T) > O_X(S)
B:  (C_X(T) / O_X(S) − 1) − (C_SPY(T) / O_SPY(S) − 1) > 0
C:  C_X(T) > max{ C_X(T') : T' an earlier existing completed bucket of X in session S }

qualifies(T) = evaluable(T) ∧ A ∧ B ∧ C
signal(T)    = qualifies(T) ∧ no earlier qualifying bar of X in session S
```

- **Comparisons:** strict, in exact float, with no rounding tolerance and no magnitude threshold.
- **C's reference set:** C includes the earlier session buckets **before 11:00**.
- **Suppressed qualifiers:** later qualifying bars in the same symbol and session are not emitted signals.
- **No R dependence:** emission does **not** depend on ATR or R.

## 7. Missing data

- If SPY's 09:30 bucket is missing, there are **no H005 signals for any stock** that session.
- If X's 09:30 bucket is missing, there is **no H005 signal for X** that session.
- SPY's bucket T and X's bucket T must both exist.
- There is no alternate anchor, no fabricated bucket and no fallback session open.
- Known IEX gaps, for example: 2025-03-10 has no bars; 2024-12-23 is truncated at about 10:22, so it has no evaluable bar.

## 8. Entry and exit (60-minute primary horizon)

**Bars**
- **E1** is the 30Min bucket starting exactly at D. **E2** is the 30Min bucket starting at D + 30 min.
- Both must exist. If either is missing, the emitted signal is an **invalid observation**; there is no later-bar substitution.
- Because D ≤ scheduled close − 60 min, E2 always ends at or before the scheduled close. The latest exits are 16:00 on full sessions and 13:00 on early-close sessions.

**Prices at per-side cost `c`** (bps / 10⁴)
```
P_in    = open(E1)  × (1 + c)
P_out   = close(E2) × (1 − c)
fwdR_60 = (P_out − P_in) / R
```

**The costed E2 close is a SYNTHETIC FIXED-HORIZON RESEARCH EXIT ASSUMPTION.**
- It is **not** a claimed executable live close fill.
- It is not replaced by the next open. The next open would change the frozen 60-minute research horizon and could introduce overnight exposure for late-session signals.

**Close path (0 bps)**
```
x1 = (close(E1) − open(E1)) / R
x2 = (close(E2) − open(E1)) / R
MFE_60 = max(x1, x2)
MAE_60 = −min(x1, x2)
```
Closes only, with no intrabar excursion and no floor at zero.

**Secondary horizons** (diagnostic only; never affect P2, P3 or admission; never cross sessions)
- **30 min:** the close of E1.
- **120 min:** four buckets from D. Valid only if all four exist within S; otherwise that horizon is invalid for that observation.
- **Session end:** the last existing bucket of S.

## 9. R (research normalization)

Uses completed 30Min bars of X only.
```
TR_i     = max(high_i − low_i, |high_i − close_{i−1}|, |low_i − close_{i−1}|)
ATR14(T) = mean(TR_{T−13} … TR_T)          (bars T−14 … T required)
R        = 2 × ATR14(T)
```
- R contains **no execution cost** and **no modeled-entry adjustment**. It is **identical at every cost level**.
- Every input bar is complete at D, so R uses no information after D.
- An undefined, zero or non-finite R makes the observation invalid.
- Later portfolio execution (§20) uses the exact RiskManager implementation. This definition is only for pre-portfolio research normalization.

## 10. Populations

| Population | Definition | Used for |
|---|---|---|
| `emitted_signal_population` | every emitted H005 signal (§6) | counts, suppression of later qualifiers |
| `valid_real_population` | emitted signals with E1, E2 and a valid R | **P1**; P4-A; cost precheck; descriptive full-valid metrics |
| `matched_real_population` | valid real observations with a non-empty eligible random pool under L0–L4 (§15) | **the only population for P2, P3 and P4-B** |
| `unmatched_valid_population` | valid real observations with no pool at any level | counted and visible, descriptive metrics allowed; **never** enters P2, P3 or P4-B |

- Every random replicate contains **exactly one draw for every observation in `matched_real_population`**.
- There is no matched-count admission gate, and P1 remains based on valid observations.
- No threshold on unmatched coverage may be added after results.

## 11. P1 — sample size

P1 requires ≥ 300 valid 60-minute observations (`valid_real_population`) in Development.

## 12. P2 — primary metric vs matched random

- **Primary statistic:** the median fwdR_60 at 0 bps on `matched_real_population`. Higher is better.
- **Comparison:** against the 200 frozen matched-random replicate medians.
- **Requirement:** an oriented empirical percentile ≥ 95, using the exact frozen Protocol V2 percentile method:
  ```
  pct = (count(random < real) + 0.5 · count(random == real)) / n · 100
  ```
  - `n` is the number of non-null replicate values;
  - comparisons are exact float, with no interpolation;
  - a missing value gives no percentile, and the gate is FAIL.

## 13. P3 — supporting metrics (exactly three)

| # | Metric (0 bps, 60 min, `matched_real_population`) | Aggregate | Favorable direction |
|---|---|---|---|
| P3-1 | fwdR_60 | mean | higher |
| P3-2 | close-path MFE_60 in R | median | higher |
| P3-3 | close-path MAE_60 in R | median | lower |

- **Pass rule:** at least 2 of the 3 must be **strictly better** than the median of their matched-random replicate distribution, in the preregistered direction. A tie does not count.
- **Close-path definition:** as in §8.

## 14. P4 — economic scale

- **Canonical cost:** 5 bps per side, i.e. 0.10% round trip.
- **P4 fails only when BOTH A and B hold.**

**P4-A**
```
0.10%  ≥  0.25 × median over successful VALID real observations of  (MFE_60 × R / open(E1) × 100)
successful ⇔ fwdR_60 at 0 bps > 0
```
P4-A is not a random comparison. It is computed on all valid real observations.

**P4-B**
- Recompute the real statistic and the matched-random controls at 5 bps using:
  - the **same** `matched_real_population`;
  - the **same** 200 frozen random draws;
  - the **same** R;
  - cost applied **only** to the fills (§8).
- B holds when the real primary metric (median fwdR_60) no longer reaches the frozen 95th oriented percentile.
- Also reported, descriptively only: whether the real 5 bps median is ≤ 0.

## 15. Matched random control

**Candidate pool.** A bar of symbol X in Development is a candidate iff it:
- is evaluable under the H005 timing and data rules (§5–§7);
- has a valid R (§9);
- has E1 and E2 (§8);
- is **not** an emitted H005 signal bar.

**Suppressed qualifiers remain eligible candidates.** Report:
- the suppressed qualifier count;
- their fraction of the eligible pool;
- how often they are drawn across replicates.

**Matching variables**
- same symbol;
- same NY calendar month of T;
- same decision time D;
- same point-in-time 30Min ATR% quintile.

**Never matched on:** A, B, C, session return, relative strength or new-high state.

**ATR% and quintile**
```
ATR%(T)  = ATR14(T) / C_X(T) × 100
rank(T)  = (#{prior values < ATR%(T)} + 0.5 · #{prior values == ATR%(T)}) / 260
           over the 260 immediately preceding valid 30Min ATR% values of X
quintile = min(4, ⌊5 · rank⌋)          ∈ {0, 1, 2, 3, 4}
```
- The rank uses the frozen Protocol V2 percentile/tie convention.
- **Short history:** if fewer than 260 valid prior ATR% values exist, quintile = −1 and matching goes **directly to L4**. This happens in early January 2024. Earlier stock history is **not** downloaded to avoid it, and the stock cache and manifest are not modified.
- **Report:** the count and percentage of real observations with quintile −1.

**Fallback hierarchy** (fixed order; the first non-empty level is used)

| Level | Match |
|---|---|
| L0 | exact month + decision slot + quintile |
| L1 | adjacent decision slot (D ± 30 min within the eligible window), same quintile |
| L2 | same slot, adjacent quintile |
| L3 | adjacent slot + adjacent quintile |
| L4 | month + slot, any quintile |
| — | otherwise **unmatched** |

**Replicates and draws**
- 200 replicates; `BASE_SEED = 20260925`.
- For replicate r ∈ {0 … 199} and sorted-symbol index j, use `numpy.random.default_rng(20260925 + 1000·r + j)`.
  - j is X's index in the sorted list of symbols that have at least one valid real observation. This is the frozen audit-tooling convention.
- Exactly one uniform draw per `matched_real_population` observation, **with replacement**.

## 16. Isolated cost precheck (non-gating)

- **Costs per side:** 0, 2.5, 5, 7.5 and 10 bps.
- **Population:** all valid real observations.
- **Trades:** entry and exit per §8 at each cost, with R unchanged. No stop, trailing, scale-out, giveback or portfolio restriction.
- **Report at each cost:**
  - median fwdR_60 and mean fwdR_60;
  - PF = Σ positive fwdR_60 / |Σ negative fwdR_60|;
  - win rate = share with fwdR_60 > 0.
- **Approximate break-even:** linear interpolation across the frozen costs where the **mean** crosses zero. The **median** crossing is also reported. Both are descriptive, not achievable fills.
- P4 remains the only economic admission gate.

## 17. P5 — feed robustness (mandatory)

- **Why mandatory:** A, B and C are boundary comparisons (threshold-sensitive), and a secondary feed (SIP) is available.
- **Period:** KNOWN only, 2026-06-01 → 2026-09-23.
- **Construction:** signals are built independently from:
  - IEX stocks + IEX SPY;
  - SIP stocks + SIP SPY.
- Feeds are never mixed.
- Signal emission uses only same-session data, so no warm-up and no Validation-period data are needed.
- **No KNOWN outcomes or P&L are computed.**
- **Report:**
  - signal counts;
  - exact (symbol, T) matches and feed-only signals;
  - Jaccard;
  - ±30-minute near matches;
  - common-bucket OHLC differences in bps;
  - missing-bucket sessions.
- **Pass:** Jaccard ≥ 0.75. **No exception is preregistered.**

## 18. P6 — integrity

P6 must include:
- a data-integrity audit;
- manifest and checksum validation: `research/historical_manifest_v1.json` for stocks and `research/context_manifest_spy_v1.json` for Development SPY;
- an integrity audit of the SPY KNOWN (IEX and SIP) files once acquired;
- no-look-ahead tests. **A signal at T must be invariant to any data after D.** R and the ATR quintile use bars up to and including T only;
- deterministic reproduction of all outputs.

**Admission rules (Protocol V2)**
- Each gate is recorded PASS / FAIL / NOT_APPLICABLE; NOT_APPLICABLE is only possible for P5, and does not apply to H005.
- Unmeasured gates are FAIL.
- Status is stage-based:
  - P1–P3 only → `REJECTED_AT_RAW_SIGNAL_SCREEN`;
  - P4 only → `REJECTED_AT_ECONOMIC_SCALE`;
  - P5 only → `REJECTED_AT_FEED_ROBUSTNESS`;
  - more than one stage, or any P6 failure → `REJECTED_PRE_PORTFOLIO`;
  - all pass → `PRE_PORTFOLIO_PASS`.
- `pre_portfolio` and the per-gate outcomes are always recorded.

## 19. Required transparency outputs

- **Counts:** `emitted_signal_count`, `valid_real_count`, `matched_real_count`, `unmatched_real_count`, `matched_fraction`.
- **Unmatched breakdown:** by symbol, month and decision time.
- **Matching:** fallback-level usage; real observations with quintile −1 (count and %).
- **Validity:** invalid-observation counts, by reason (missing E1, missing E2, invalid R).
- **Distributions:** symbol, decision time and ATR.
- **Suppressed qualifiers:** count, pool fraction, draw frequency.
- **Non-gating metrics** (also reported):
  - +0.5R / +1R / −1R reach rates;
  - weak forward excursion (MFE_60 < +0.25R);
  - 5 bps versions;
  - full-valid-sample descriptive metrics;
  - the secondary horizons, which remain diagnostic only.

## 20. Full Development portfolio (only after admission)

**ONLY if P1–P6 all pass**, run the existing frozen shared portfolio engine and management stack, **unchanged**:
- RiskManager and existing sizing;
- max positions, leverage and portfolio heat;
- daily and loss-streak halts;
- existing stop logic, trailing, scale-out and giveback;
- canonical 5 bps per side and the existing next-bar execution conventions.

**Rules**
- Management may not be tuned on pre-portfolio outcomes. Any meaningful change to the signal or management assumptions requires a **new hypothesis ID**.
- Development gates are D1–D6 from Research Protocol V2:
  - **D1:** expectancy R > 0;
  - **D2:** PF ≥ 1.10;
  - **D3:** ≥ 150 completed trades;
  - **D4:** |max DD| ≤ 25%;
  - **D5:** total R > 0;
  - **D6:** positive-pool max symbol share ≤ 0.50, and an empty pool fails.
- Validation gates are V1–V4 from Research Protocol V2:
  - **V1:** expectancy R > 0;
  - **V2:** PF > 1.0;
  - **V3:** total R > 0;
  - **V4:** max DD ≤ 30%.

## 21. Validation

No Validation-period feed of any kind may be accessed or downloaded until **all** of the following hold:
1. the spec is frozen;
2. P1–P6 pass;
3. Development D1–D6 pass;
4. the implementation is frozen;
5. the frozen implementation commit is recorded.

Then Validation runs **once**. There is no tuning after viewing, and a failure closes H005.

## 22. Contamination disclosure

1. H005's direction was motivated by **already-viewed** H001–H004 Development results and by Research Sanity Audit V1:
   - friction dominance;
   - matched random performing as well as the real signals;
   - H004's use of SPY context;
   - 5Min feed sensitivity.
2. H001/H003 **time-of-day results were previously viewed**. The 11:00 earliest decision is justified structurally (three completed 30Min bars) but is therefore **not fully independent** of viewed Development data.
3. **No magnitude threshold was selected from outcomes.** A, B and C are sign or strict-comparison tests. The remaining constants are structural (bar size, horizon, window), inherited (ATR14, 2×ATR, costs) or Protocol V2 defaults.
4. **Validation (2026-01-02 → 2026-05-29) is the first untouched strategy-level evaluation.** Public macro knowledge of that period exists and is disclosed.
5. KNOWN (2026-06-01 → 2026-09-23) is permanently contaminated and is used only for P5, with no outcomes.

## 23. Constants (frozen)

| Constant | Value |
|---|---|
| Bar | 30Min RTH, anchored 09:30 ET; scheduled close 16:00 / 13:00 (`EARLY_CLOSES`) |
| Earliest decision | 11:00 ET |
| Latest decision | scheduled close − 60 min |
| Signals per symbol per session | 1 (first qualifying), no re-arm |
| A / B / C | strict; zero / comparison; no magnitude |
| ATR window | 14 (30Min bars) |
| R | 2 × ATR14(T) |
| Primary horizon | 60 min (E1, E2) |
| Secondary horizons | 30 min, 120 min, session end (diagnostic) |
| Canonical cost | 5 bps per side (0.10% round trip) |
| Precheck costs | 0, 2.5, 5, 7.5, 10 bps per side |
| P1 | ≥ 300 valid observations |
| P2 | ≥ 95 oriented percentile (frozen method) |
| P3 | ≥ 2 of 3 |
| P4 friction share | 0.25 |
| P5 Jaccard | ≥ 0.75, no exception |
| Replicates | 200 |
| BASE_SEED | 20260925 |
| ATR rank window | 260 prior valid 30Min ATR% values |
| Weak forward excursion | MFE_60 < +0.25R (report only) |

## 24. Implementation boundary (for the later step)

- **New code:** research-only modules for H005 raw signals, the matched random control and admission reporting. They use `src/preportfolio_screen.py` for the gates and the frozen audit helpers where they apply.
- **Real statistics:** computed on `matched_real_population`, not with the audit helper's all-valid convention.
- **Unchanged:** the shared engine, RiskManager, H001–H004, Research Protocol V1, live code and `src/smoke_test.py`.

## 25. Change history

- 2026-09-27: specification created (SPECIFIED_NOT_IMPLEMENTED) under Research Protocol V2 (`565f7aa`).
  - The design review was closed with zero open choices.
  - No H005 code, no signals, no Development outcomes, no data downloads, and no Validation or Forward.
