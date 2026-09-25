# Research Protocol V2 — staged pre-portfolio admission

| Field | Value |
|---|---|
| Version | `research_protocol_v2` |
| Status | **Review decisions Q1–Q7 settled 2026-09-25** (§21). Binding for H005+ once this document is committed. |
| Created | 2026-09-25 |
| Written at commit | `d6a8ad0` ("feat: confirm zero-cost random control behavior") |
| Machine-readable form | `config/research_protocol_v2.json` |
| Predecessor | `config/research_protocol_v1.json`, **unchanged** |
| Applies to | H005 and later only. MA_BASELINE_V1 and H001–H004 stay under V1 and are **not** re-evaluated or relabelled. |

---

## 1. Why V2 exists

Under V1, a hypothesis could reach a full portfolio backtest before anyone had shown that its raw entry signal contained information. Five strategies later, the evidence says that workflow spent most of its effort on the wrong question.

**Evidence from V1 (DEVELOPMENT, plus the contaminated KNOWN period for feeds):**

**Results**
- MA_BASELINE_V1 was negative in every split.
- H001, H003 and H004 were rejected at Development at about −0.06R and PF about 0.79. H002 was also rejected.

**Autopsies**
- H001 and H003 have nearly identical outcome distributions: every CLES is 0.49–0.53.
- About 47% of trades are immediate failures.

**Research Sanity Audit V1**
- **Cost:** H001 and H003 are positive at 0 bps (+0.063 and +0.051R) and negative at the canonical 5 bps. Break-even is about 2.3–2.6 bps per side.
- **No signal edge over random:** real raw signals are not better than matched random bars; the median 60m return sits at the 1st–2nd percentile of the random distribution.
- **Random entries under the same management:**
  - At 5 bps, isolated random trades lose about as much as the real signals.
  - At 0 bps, isolated random trades are also positive: H001 real +0.021R vs random median +0.021R (52.5th percentile); H003 +0.048R vs +0.039R (67.5th percentile).
  - So the small gross edge is consistent with **long-market drift plus shared management**, not signal alpha.
- **No compression by the RiskManager:** H001 and H003 raw opportunity distributions were already nearly identical before it (max |CLES − 0.5| = 0.024).
- **Feed sensitivity:** IEX-vs-SIP raw-signal Jaccard on KNOWN was 0.69 (H001) and 0.62 (H003), although OHLC differences were only a few bps. The 5Min threshold signals are feed-sensitive.
- **Friction:** round-trip friction is about 0.10% of price. That is about 22–29% of a typical winning move.

**Lesson:** prove signal-level information *before* spending a portfolio backtest.

## 2. Workflow

```
IDEA → SPECIFICATION FREEZE (Stage 0) → RAW SIGNAL SCREEN (Stage 1) → MATCHED RANDOM CONTROL (Stage 2)
→ ECONOMIC SCALE CHECK (Stage 3) → FEED ROBUSTNESS CHECK (Stage 4) → PRE-PORTFOLIO ADMISSION (P1–P6)
→ FULL DEVELOPMENT PORTFOLIO BACKTEST → D1–D6 → FROZEN IMPLEMENTATION → VALIDATION (once) → FORWARD
```

- A hypothesis that fails at any stage **stops there**.
- It is never rescued under the same ID: any meaningful rule change creates a new ID (§14).

## 3. Hypothesis IDs and Stage 0 — specification freeze

- **IDs:** sequential H005, H006, ….
- **Before any hypothesis signal is calculated**, the spec freezes:
  - universe;
  - feed declarations (§5);
  - timeframe and its justification (§6);
  - indicators, entry rules, exit assumptions, signal timing, warm-up and missing-data handling;
  - execution assumptions;
  - forward horizons;
  - the **primary** raw metric and its **supporting** metrics, with their expected directions;
  - the random-control design (§8), sample-size minimum (P1), economic-scale criteria (P4) and feed-robustness criteria (P5);
  - D1–D6, V1–V4, and the contamination disclosure (§13).
- **Commit and record:** the spec is committed and the **frozen-spec commit hash is recorded** before any hypothesis data is viewed.
- **Changes after viewing:** once any stage has been viewed, changing its rules requires a new hypothesis ID.

## 4. Stage 1 — raw signal screen

- **No portfolio:** no positions, leverage competition, daily or loss-streak halts, capital allocation or symbol blocking.
- **Signals:** every frozen raw signal in DEVELOPMENT is evaluated **independently**, from the next executable bar per the spec.
- **Horizons:** fixed forward horizons are **preregistered per hypothesis**. There is no universal default; for example, a 15Min spec might choose 30m / 60m / 120m / session end.
- **Cost:**
  - The primary directional screen is measured at **0 bps**. That is not a claim about realism; it isolates information before friction.
  - Secondary economics use the canonical cost.
- **Required metrics:**
  - raw signal count and valid forward observations;
  - median and mean forward return R;
  - MFE and MAE;
  - +0.5R, +1R and −1R reach rates;
  - weak-forward-excursion rate. Default: MFE at the primary horizon < +0.25R, unless the spec preregisters another definition;
  - symbol, time-of-day and ATR/volatility distributions.
- **Tooling:** the methodology is the validated V1-audit shadow method: `reconstruct_risk_ps` and `shadow_fixed_horizon`, with excursions on completed-bar closes.

## 5. Data-feed policy (Q1)

**Required declarations.** Every hypothesis preregisters:
- `PRIMARY_RESEARCH_FEED`;
- `INTENDED_LIVE_SIGNAL_FEED`;
- `SECONDARY_ROBUSTNESS_FEED`, if one is available;
- `ADJUSTMENT`, `TIMEFRAME` and `DATE_RANGE`.

Every artifact states these too.

**Default rule: `PRIMARY_RESEARCH_FEED` must match `INTENDED_LIVE_SIGNAL_FEED`.**
- **Feed mismatch:** research may use a different feed from the intended live feed only if the mismatch is **preregistered and justified** and the spec names the **operationally authoritative feed**. P5 feed robustness is then **mandatory**. An unjustified mismatch fails P5.
- **Threshold-sensitive rules:** P5 also applies when research and live feeds match, whenever a secondary feed is available (§10).

**SIP's role**
- SIP is the preferred **high-quality comparison feed** when legally available.
- It is the **primary** research feed only when the hypothesis intends SIP as its operational signal feed. SIP is not automatically authoritative.
- The V1 audit confirmed historical SIP access on the existing account; nothing is purchased or bypassed.

**Separation**
- Feeds are **never mixed** in one strategy run.
- Caches stay separate (`data/historical/` holds IEX; SIP lives in its own directory), and so do manifests and checksums.

**History:** H001–H004 remain valid historical experiments **on IEX**. They are not rewritten.

## 6. Timeframe principle

- 5Min is **not** banned. Every hypothesis must justify why its expected move is large relative to modeled friction.
- New signal families are strongly preferred at larger move scales (for example 15Min or 30Min) unless the spec preregisters a reason for 5Min.
- A higher timeframe is not assumed to be better.

## 7. Stage 2 — matched random control (first-class benchmark)

- **Requirement:** every hypothesis is compared with matched random entries.
- **Default matching** (the V1-audit design):
  - same symbol;
  - same calendar month;
  - same 30-minute time-of-day bucket, by decision time;
  - same point-in-time ATR% quintile. This is the rank of ATR14%/close against the previous history of the same symbol, with no future information, adapted to the hypothesis timeframe in its spec.
- **Default fallback hierarchy:**
  - L0: exact match;
  - L1: adjacent time bucket;
  - L2: adjacent ATR quintile;
  - L3: adjacent bucket and adjacent quintile;
  - L4: same symbol, month and bucket, any quintile;
  - otherwise: unmatched.
  - Fallback usage is reported. Matching is never broadened beyond same symbol and month unless the spec says why before outcomes.
- **Other defaults:** **200** deterministic replicates; seeds `default_rng(BASE_SEED + 1000·r + j)` with BASE_SEED preregistered; draws **with replacement** (disclosed); **all** raw-signal bars of the hypothesis excluded from the pools.
- **Extensions:** the spec may add matching dimensions if justified beforehand. It must **not overmatch** on variables derived from the strategy rule itself when that would erase the tested signal.
- **Freeze:** dimensions, buckets, fallback, replicate count, seeds, replacement and exclusions are frozen before outcomes, and matching never changes after real outcomes are viewed.

## 8. Pre-portfolio admission gates (P1–P6)

**Recording**
- Each gate is recorded as **PASS**, **FAIL** or **NOT_APPLICABLE**. NOT_APPLICABLE is allowed only for P5, as defined in §10.
- **Any mandatory gate that was not measured is FAIL.**
- Any FAIL means **no full portfolio backtest**.

| Gate | Final rule |
|---|---|
| **P1 sample size** | At least **300 VALID raw-signal forward observations** in DEVELOPMENT. This counts valid observations, not merely emitted signals. A lower minimum is allowed only if preregistered with a structural-rarity reason; it is never lowered after results. |
| **P2 primary metric vs random** | Exactly **one** preregistered primary metric (e.g. mean or median forward R at a preregistered horizon, or P(+1R before −1R)), at **0 bps**. Its **oriented empirical percentile** against the matched-random replicate distribution must be **≥ 95** (see *Percentile convention* below). |
| **P3 supporting metrics** | At least **two** preregistered supporting metrics are **strictly better than the matched-random replicate median in their own preregistered favorable direction**. Examples: higher MFE, lower MAE, higher +0.5R or +1R rate, lower weak-excursion rate, lower −1R rate. The comparison is direction-aware, and a tie with the median does not count. There is no percentile requirement unless one is preregistered. |
| **P4 economic scale** | Fails only if **A and B** both hold (§9). |
| **P5 feed robustness** | §10. |
| **P6 integrity** | Data-integrity audit PASS, no-look-ahead checks PASS and deterministic reproduction PASS. |

**Percentile convention** (frozen; P2 and P4 use the same convention)

```
pct = (count(random < real) + 0.5 * count(random == real)) / n * 100
```
- `n` is the number of non-null replicate values. Comparisons are exact float comparisons, with no interpolation.
- For a lower-is-better metric the **oriented** percentile is `100 − pct`.
- A missing real value, or zero replicates, gives no percentile, and the gate is FAIL.
- This is the implementation already tested in `research_sanity_audit.percentile_of` and `preportfolio_screen.directional_percentile`. It must not be changed after outcomes.

**Other rules**
- **Multiple-metric discipline:** only the preregistered primary metric controls admission. All metrics are still reported, and metrics are never switched after the fact.

**Status on failure (Q4)**

The rule is **stage-based, not a count of failed gates**. The pre-portfolio stages are:
- **RAW SIGNAL SCREEN:** P1, P2, P3;
- **ECONOMIC SCALE:** P4;
- **FEED ROBUSTNESS:** P5;
- **INTEGRITY:** P6.

| Failed gates | Status |
|---|---|
| Any combination limited to P1/P2/P3 (e.g. P1; P2 + P3; P1 + P2 + P3) | `REJECTED_AT_RAW_SIGNAL_SCREEN` |
| P4 only | `REJECTED_AT_ECONOMIC_SCALE` |
| P5 only | `REJECTED_AT_FEED_ROBUSTNESS` |
| Failures in more than one distinct stage (e.g. raw screen + P4; raw screen + P5; P4 + P5) | `REJECTED_PRE_PORTFOLIO` |
| Any P6 failure, alone or combined with other failures | `REJECTED_PRE_PORTFOLIO` |
| None | `PRE_PORTFOLIO_PASS` |

Every rejection also records the umbrella field `pre_portfolio = REJECTED_PRE_PORTFOLIO` and the PASS/FAIL/NOT_APPLICABLE outcome of each of P1–P6.

## 9. Stage 3 — economic scale (P4, Q2) and execution-sensitivity precheck (Q3)

**Report**
- median initial R in price %;
- assumed one-way slippage in bps;
- estimated round-trip friction %;
- round-trip cost / R;
- median favorable move % and median winning move %;
- gross signal expectancy at 0 bps, and signal expectancy at canonical cost.

**P4 definitions**
- **Successful signal:** its `direction_normalized_forward_return_at_primary_horizon` is **> 0 at 0 bps**. A long signal uses the price-direction return; a short signal uses its negation.
- **Favorable move:** the **direction-normalized MFE %** at the preregistered primary horizon. Its **median over successful signals** is used.
- **A:** `roundtrip canonical friction % ≥ 0.25 × median successful favorable move %`. If there is no measurable favorable move, A counts as true.
- **B — the advantage over random disappears at canonical cost:**
  - recompute **both** the real signals **and the exact frozen matched-random controls** under canonical cost, with the matching design unchanged;
  - B holds when the real **primary** metric no longer satisfies the **same P2 requirement** (oriented percentile ≥ 95, same convention, same preregistered direction).
- **Descriptive only:** if the primary metric is a signed return or expectancy, the report also states whether it is ≤ 0 at canonical cost. This does not replace B.
- **Result:** P4 **FAILS iff A and B**. A spec may preregister a stricter criterion, but never a looser one.
- Missing friction is FAIL. A missing canonical-cost primary value makes B true.

**Isolated execution-sensitivity precheck (Q3)**
- **Mandatory to run and report**, but **non-gating**.
- Uses the validated isolated one-trade simulator at the preregistered costs, by default **0 / 2.5 / 5 / 7.5 / 10 bps** per side.
- Reports isolated expectancy R, PF, win rate and an approximate break-even cost. The break-even is descriptive interpolation, not an achievable fill.
- It can be evidence against a hypothesis but creates no additional P gate. **P4 remains the economic admission gate.**

## 10. Stage 4 — feed robustness (P5)

**When P5 applies** (derived only from the frozen declarations in §5)
- It is **mandatory** when `PRIMARY_RESEARCH_FEED ≠ INTENDED_LIVE_SIGNAL_FEED`.
- It also applies to **threshold-sensitive** rules (exact OHLC thresholds, crossovers, breakouts, close location, other price-sensitive boundaries) whenever a secondary feed is available, even if the research and live feeds match.
- It is **NOT_APPLICABLE only** when the feeds match **and** the rule is either not threshold-sensitive or has no secondary feed available.
- NOT_APPLICABLE can never be used to leave an applicable gate unmeasured. An applicable but unmeasured P5 is FAIL, and the tooling rejects a NOT_APPLICABLE that is inconsistent with the declarations.

**How the test runs**
- Raw signals are generated **independently** on each feed over the **contaminated KNOWN period only**, never Validation.
- Warm-up happens inside KNOWN.
- The report covers signal counts, exact matches, feed-only signals, Jaccard, ±1-bar near matches and common-bar OHLC differences.

| Jaccard | Band |
|---|---|
| ≥ 0.90 | HIGH_STABILITY |
| 0.80 – < 0.90 | ACCEPTABLE_MODERATE |
| 0.75 – < 0.80 | CAUTION |
| < 0.75 | MATERIAL_FEED_SENSITIVITY |

**P5 outcome**
- Jaccard ≥ 0.75 → PASS.
- Jaccard < 0.75 → FAIL, unless the spec **preregistered** all three of:
  1. why that sensitivity is expected;
  2. which feed is authoritative for research and live operation;
  3. why the signal remains operationally meaningful.
- An unjustified research/live feed mismatch (§5) → FAIL. There are no post-hoc exceptions.

## 11. Full Development portfolio backtest and gates (inherited unchanged)

- **When:** only after P1–P6 PASS.
- **What runs:** the frozen strategy runs through the existing portfolio engine with the RiskManager, sizing, leverage, max positions, halts, shared capital, heat, trade management and canonical slippage.

**Gates**

| # | Rule |
|---|---|
| D1 | expectancy R > 0 |
| D2 | PF ≥ 1.10 |
| D3 | completed trades ≥ 150 |
| D4 | abs(max DD %) ≤ 25 |
| D5 | total R > 0 |
| D6 | P_s = realized P&L per tradable symbol; Π = Σ max(P_s, 0). PASS only if Π > 0 **and** max share ≤ 0.50 (exact). Π = 0 FAILS. |

## 12. Validation (inherited unchanged) and Forward (Q6)

**Validation**
- Opened **once**, and only after all of:
  1. frozen specification;
  2. P1–P6 all mandatory gates PASS;
  3. D1–D6 all PASS;
  4. frozen implementation;
  5. a recorded frozen implementation commit.
- **No Validation-period data from SIP or any other feed** is downloaded or read before all five conditions hold. This follows the H004 precedent.
- Gates:
  - **V1:** expectancy R > 0;
  - **V2:** PF > 1.0;
  - **V3:** total R > 0;
  - **V4:** max DD ≤ 30%.
- No tuning after viewing, and a failure closes the hypothesis.

**Forward**
- Only after pre-portfolio, Development and Validation all PASS.
- Forward data is never used for design.

## 13. Development contamination

- Development is hypothesis-generation territory and is conceptually contaminated by MA_BASELINE_V1, H001–H004, the autopsies and the V1 sanity audit.
- Every new hypothesis must disclose:
  - which prior Development findings motivated it;
  - which thresholds came from prior Development observations;
  - which rules were chosen independently.
- Validation remains the first untouched strategy-level evaluation. Broad public macro knowledge of the Validation period is disclosed per hypothesis.

## 14. No salvage under the same ID

- Fails the raw screen → do not adjust thresholds under the same ID.
- Fails economic scale → do not change the timeframe under the same ID.
- Fails feed robustness → do not soften the trigger under the same ID.
- Any meaningful rule change gets the next ID.

## 15. Benchmark layers reported for every future strategy

- **A. Market/drift baseline** (optional passive-long diagnostic, §16).
- **B. Matched random entries** (Stage 2).
- **C. Real raw strategy signals** (Stage 1).
- **D. Executable portfolio strategy** (only if admitted).

Keeping these layers separate stops market drift being read as signal alpha.

## 16. Optional passive-long / scheduled-entry baselines (Q5)

- There is **no global scheduled-entry baseline** in V2. Matched random (Stage 2) remains the **mandatory** timing baseline.
- Passive-long, scheduled-entry and buy-and-hold baselines are **optional diagnostics**, never strategies to optimize.
- A hypothesis that uses one must preregister, before outcomes are viewed:
  - the exact entry schedule and symbols;
  - the holding/management convention;
  - the costs;
  - the sample period.

## 17. Cost policy and real-fill calibration

- Canonical cost is **5 bps per side**, next-bar open, $0 commission.
- It is **never reduced because a strategy fails**, and a strategy cannot choose its own execution model after seeing results.
- Real paper fills keep accumulating separately, and are periodically compared with the simulated next-bar open and the modeled slippage.
- Any change to canonical slippage needs a **separate calibration decision** based on independent real-fill evidence, never an individual strategy result.

## 18. Standardized statuses (V2 hypotheses only)

**Pre-portfolio stages**
- `SPECIFIED_NOT_IMPLEMENTED`
- `RAW_SCREEN_RUNNING`
- `REJECTED_AT_RAW_SIGNAL_SCREEN`
- `REJECTED_AT_ECONOMIC_SCALE`
- `REJECTED_AT_FEED_ROBUSTNESS`
- `REJECTED_PRE_PORTFOLIO`
- `PRE_PORTFOLIO_PASS`

**Portfolio, Validation and Forward**
- `REJECTED_AT_DEVELOPMENT`
- `DEVELOPMENT_PASS_VALIDATION_NOT_RUN`
- `REJECTED_AT_VALIDATION`
- `VALIDATION_PASS_FORWARD_NOT_STARTED`
- `FORWARD_TESTING`

Historical H001–H004 statuses are not rewritten.

## 19. Required artifacts per future hypothesis

- `research/strategy_v2_hypothesis_XXX.md`, a registry entry and research-log entries.
- Raw-signal output, matched-random output and an economic-scale report.
- A feed-robustness report (if applicable).
- A pre-portfolio admission report (P1–P6).
- Full Development output **only if admitted**, and Validation output **only if eligible**.

## 20. Tooling

`src/preportfolio_screen.py` is research-only and touches no shared or live code. It provides:
- the V2 protocol loader;
- the DEVELOPMENT/KNOWN window guards, which refuse Validation and Forward with no bypass;
- the P1–P6 evaluators, which return PASS/FAIL/NOT_APPLICABLE;
- `direction_normalized` and `median_successful_favorable_move_pct` (Q2);
- `p5_applicable` (Q1);
- the admission decision with the Q4 status rules and per-gate outcomes;
- the frozen `PERCENTILE_METHOD` convention;
- a guard that refuses any full portfolio run unless admission is `PRE_PORTFOLIO_PASS` with every gate PASS or legitimately NOT_APPLICABLE.

It reuses the frozen V1-audit helpers: matched-random pools and draws, percentile, shadow metrics, the isolated simulator and break-even interpolation.

**Registry (Q7).** When H005 is created, its registry entry is tagged `research_protocol_v2` and the V2 statuses are added to the registry schema's allowed values. H001–H004 are preserved exactly as historical V1 research. The registry is not changed now.

## 21. Review decisions (settled 2026-09-25)

| # | Decision |
|---|---|
| Q1 | **Approved with modification.** The research feed matches the intended live signal feed by default. A mismatch must be preregistered and justified, must name the operationally authoritative feed, and makes P5 mandatory. Threshold-sensitive rules still need P5 when a secondary feed is available. SIP is the preferred comparison feed and is primary only if it is the intended operational feed. H001–H004 are unchanged (§5, §10). |
| Q2 | **Approved with modification.** Success means direction-normalized forward return at the primary horizon > 0 at 0 bps. The favorable move is the direction-normalized MFE % at the primary horizon. B means the primary metric fails the same P2 percentile rule against the same frozen random controls, both recomputed at canonical cost. "≤ 0 at canonical cost" is descriptive only (§9). |
| Q3 | **Approved.** The isolated execution precheck is mandatory to run and report, and non-gating. P4 remains the economic gate (§9). |
| Q4 | **Approved; clarified 2026-09-25.** Stage-based, not gate-count-based. Any failures limited to P1/P2/P3 get `REJECTED_AT_RAW_SIGNAL_SCREEN`; P4 only gets `REJECTED_AT_ECONOMIC_SCALE`; P5 only gets `REJECTED_AT_FEED_ROBUSTNESS`. Failures spanning more than one distinct stage, or any P6 failure, get `REJECTED_PRE_PORTFOLIO`. The umbrella `pre_portfolio` field and per-gate PASS/FAIL/NOT_APPLICABLE are always recorded (§8). |
| Q5 | **No global scheduled-entry baseline.** Such baselines are optional and fully preregistered per hypothesis. Matched random stays mandatory (§16). |
| Q6 | **Approved.** No Validation-period data from any feed before the five conditions in §12. |
| Q7 | **Approved.** Tag H005 with `research_protocol_v2` and extend the registry statuses when H005 is created. H001–H004 are untouched (§20). |

**Additional clarifications (frozen)**
1. P1 counts **valid** forward observations.
2. The P2 percentile and tie convention is the documented, tested implementation (§8).
3. P3 comparisons are direction-aware.
4. P5 is NOT_APPLICABLE only when genuinely not applicable under V2.
5. Any unmeasured mandatory gate is FAIL.

## 22. Change history

- 2026-09-25: proposed at `d6a8ad0`. No H005 was created, no strategy outcomes were run, and V1 and H001–H004 were not modified. Validation and Forward untouched.
- 2026-09-25: review decisions Q1–Q7 and clarifications 1–5 settled (§21).
  - Updated: feed policy, gate outcomes, P4 definitions, P5 applicability, status rules, non-gating precheck, optional baselines, the Validation feed rule and the registry rule.
  - Tooling and tests were updated to match.
- 2026-09-25: the Q4 status rule was clarified as stage-based (wording only; the implemented and tested behavior is unchanged).
