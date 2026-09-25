# STRATEGY_V2_HYPOTHESIS_002 — H001 + Failure-to-Progress exit (long-only, 5Min)

| Field | Value |
|---|---|
| Hypothesis ID | `STRATEGY_V2_HYPOTHESIS_002` |
| Status | **SPECIFIED_NOT_IMPLEMENTED** — specification **FROZEN** (approved 2026-09-25); not to be implemented until this frozen spec is committed |
| Frozen spec commit | `6b0ac231388c3a7bce9135782aab5af4d2576fd3` (`6b0ac23`, "docs: freeze h002 strategy specification") |
| Date created | 2026-09-25 |
| Spec written at commit | `0bd6d95d4521c2ce8a2e65a873fb1facc9db3b61` (`0bd6d95`, "docs: add frozen h001 strategy specification") |
| Base | `STRATEGY_V2_HYPOTHESIS_001` (frozen; REJECTED_AT_DEVELOPMENT), spec `research/strategy_v2_hypothesis_001.md` |
| Protocol | `config/research_protocol_v1.json` |
| Registry | `research/strategy_registry_v1.json` |

Nothing here has been implemented or run. No H002 result exists on any split. This document is the pre-registration: rules, constants, gates and the comparison plan are fixed **before** any H002 result is seen. **The specification is frozen:** any change to rules, constants, gates, definitions or reporting creates a new hypothesis ID.

---

## 1. Hypothesis

H002 is **identical to frozen H001** in data, timeframe, entries, risk, sizing, execution and all existing trade management, except for **one** added trade-management rule: the **Failure-to-Progress (FTP) exit**.

**Claim tested:** most H001 losses are immediate failures. Closing positions that have made essentially no progress by the third post-fill close may avoid a large part of the stop-hit losses without sacrificing enough winners to cancel the benefit.

## 2. Evidence that motivated H002 (H001 DEVELOPMENT autopsy, already viewed)

Source: `data/research_v1/h001_autopsy/` (DEVELOPMENT 2024-01-02 → 2025-12-31 only).

- **Losing trades (987):**
  - 79.7% never reached +0.25R, and 94.5% never reached +0.50R;
  - 0.2% reached +1R before losing;
  - median MFE +0.07R, median MAE +0.41R adverse;
  - median time to MAE 15 minutes (3 bars).
- **Stop-hit trades:** 354 trades, −$136,674, average −1.05R. 98.6% never reached +0.25R.
- **Entry features:** CLES ≈ 0.44–0.57 everywhere, so no entry filter separated winners from losers. **H002 therefore does not change entry logic.**

## 3. Contamination disclosure (binding)

1. **H002 DEVELOPMENT IS NOT AN INDEPENDENT TEST.**
   - The 3-bar and +0.25R constants were derived from the H001 analysis on the **same DEVELOPMENT split**.
   - H002's entries are identical to H001's, so H002's development trades are essentially the trades used to choose the constants.
   - H002 DEVELOPMENT is therefore a **screening / resubstitution check only**. It is in-sample for the FTP constants and expected to be optimistic.
   - Passing the development gate is necessary but is **not** evidence that the rule generalizes.
   - **The first untouched test of H002 is VALIDATION.** It may be viewed **once**, and only after H002 is completely frozen (spec, code and commit hash recorded in the registry).
2. **One frozen test.** The constants may not be tuned after H002 development is viewed. No alternatives are tested (2 or 4 bars, 0.15R, 0.30R, or any combination). Any change requires a new ID.
3. **Known period excluded.** The known/contaminated period (2026-06-01 → 2026-09-23) played no part in H002 and must not be used for selection.

## 4. Everything inherited unchanged from H001

These are unchanged and referenced to the H001 spec:
- **§3** data: 5Min bars resampled from the audited 1Min IEX raw cache, regular session only, with the known feed gaps and the unadjusted NVDA split;
- **§4** indicators: 200-bar own-window EMA/ATR, 150-bar warm-up, and 200 warm-up support bars before each split;
- **§5** entry rules: trend, pullback, trigger, the 6th-bar rule, re-arm/reset, consumption on emission, no strategy exit signal, long-only;
- **§6** risk and trade management: the exact MA_BASELINE_V1 production values;
- **§7** execution: next-bar open + 5 bps, $0 commission, stops and targets on closes, no decision on the last bar of a session. The single, backtest-only exception is FTP's own 16:00 evaluation (§5.3, R1); H001 management still never runs at 16:00.

Implementation uses the same `TrendPullbackH001` strategy object, so **H001 entry signals are identical by construction**. Tests verify this (§11).

## 5. The Failure-to-Progress (FTP) rule

### 5.1 Definitions (per filled long position)

- **Entry fill bar `E`:** the 5Min bar at whose **open** the entry order filled. With a delayed fill, `E` is the bar where the fill actually happened.
- **Entry fill price `F`:** the actual simulated fill price, `open_E × (1 + 5 bps)`. This is the ledger's `entry_fill_price`.
- **Initial risk per share `R_ps`:** `risk_per_share_modeled = |modeled entry − initial stop|`. This is the same R denominator used for `realized_r` and `mfe_r` in the backtester, and it never changes after entry.
- **Post-fill closes:** the closes of the chronologically available regular-session 5Min bars of that symbol, starting **with bar `E` itself**:
  - `E`'s close is the **1st** post-fill close;
  - the next existing bar of that symbol is the **2nd**;
  - the one after that is the **3rd**.

  Missing bars (IEX gaps), nights, weekends and holidays are skipped. No bar is manufactured.
- **Close excursion:** `x_k = (close_k − F) / R_ps` for each post-fill close `k`.
- **MFE so far:** `max(x_1, x_2, x_3)`. It uses **closes only**: never intrabar highs, never pre-fill bars, never future bars.

### 5.2 Trigger

At the FTP checkpoint (§5.3), the FTP exit triggers if and only if **both** hold:

1. `max(x_1, x_2, x_3) < +0.25`, strictly. An MFE of exactly +0.25R does **not** trigger.
2. `current_close ≤ F`. Equality **triggers**.

When triggered, an exit order is submitted for **all remaining shares** with `exit_reason = failure_to_progress`. It fills at the **next available 5Min bar open of that symbol** with the simulator's normal sell slippage, `open × (1 − 5 bps)`, and $0 commission.

A trade that has not reached +0.25R but is still above `F` at the checkpoint is **not** exited.

### 5.3 Checkpoint timing (frozen)

**Normal case:**
- The entry fills at `E` open.
- The closes of `E`, `E+1` and `E+2` are observed.
- The **FTP test is made at the decision on the 3rd post-fill close** (`E+2`), and `current_close` is `E+2`'s close.
- If triggered, the exit fills at `E+3` open (the next existing bar of that symbol).

**Other rules:**
- **No early test:** no FTP test happens before three post-fill closes exist.
- **One-time check:** the test is made exactly once per trade. If it doesn't trigger, FTP never applies to that trade again.
- **Overnight and near-close:**
  - If fewer than three post-fill bars remain in the session, counting **continues into the next regular session**. No after-hours bars are created and no end-of-day exit is forced, which keeps H001's existing overnight behavior.
  - **Checkpoint on the session's last bar (review decision R1, final).** This applies when the 3rd post-fill close belongs to a bar whose close instant is **not** a permitted engine decision time. In practice that is the last regular bar, 15:55–16:00, whose close becomes known at 16:00.
    - FTP is **not skipped** and **not deferred to a later close**.
    - The FTP condition is evaluated **at that instant (16:00)**, using **exactly the first three post-fill closes** for MFE and **the third close as `current_close`**. No later close is ever incorporated.
    - If it triggers, the exit is **pending** and fills at the **next available regular-session bar open** of that symbol. Overnight this is normally the next session's 09:30 open. After a weekend, holiday or feed gap, it is the next existing bar, and nothing is manufactured.
    - H001's own management does not run at 16:00, because the engine never decides then (unchanged). So at this checkpoint FTP is the only logic evaluated, and no H001 exit can be submitted at the same instant.
    - This uses a **backtest-only** pending-exit path (§12). Production and live behavior are unchanged.
  - **Early-close days:** the engine treats the 13:00 close of an early-close session as a permitted decision (unchanged), so a checkpoint there follows the normal case.
  - **Pending exit already in flight:** if an exit order for the symbol is already pending at the checkpoint, the one-time FTP check is consumed without effect. This is defensive; normally any earlier order has already filled at the checkpoint bar's open.

### 5.4 Precedence with existing exits (frozen)

H001's position-management sequence runs **unchanged and first** at every decision, in this exact order:
1. trailing-stop update
2. break-even move
3. scale-out
4. giveback close
5. stop hit / take profit

Steps 3–5 each end management for that bar once they submit an order. Then:

6. **FTP**, evaluated **only if** all of the following hold:
   - this is the trade's FTP checkpoint (§5.3);
   - the position still has shares;
   - **no exit order was submitted by steps 3–5 at this same decision.**

Consequences:
- Protective and risk exits (stop), then profit-management exits (scale-out, giveback, take profit), always take priority.
- FTP **never overwrites** the exit reason of an exit H001 would have made at that decision. If an existing exit fires at the checkpoint, the one-time FTP check is consumed without effect.
- If the position was already closed before the checkpoint (for example a stop filled at `E+2` open), FTP does nothing.
- At a 16:00 checkpoint (§5.3, R1), steps 1–5 do not run at that instant (no engine decision), so step 6 is evaluated on its own. The next H001 management decision is the close of the next session's first bar. A triggered FTP exit has already filled at that bar's open, so the position is closed by then.

### 5.5 Reference prices: FTP versus existing management (documented, unchanged)

- H001's existing management computes `r_now`, break-even, scale-out and giveback from the **modeled entry** (`signal close × (1 + 5 bps)`, rounded), exactly as in production and in H001.
- FTP uses the **actual fill price `F`**, as specified.
- Both use the same `R_ps`. Neither changes the other.

### 5.6 Scale-out interaction

- If a scale-out happened before the checkpoint, FTP keeps tracking the **original trade** with the original `F`, the original `R_ps`, and MFE measured from the original entry. It applies only to the **remaining** shares.
- In practice H001 scales out at `r_now ≥ +1R`, measured on a close **from the modeled entry**. At that close the FTP excursion is `x ≥ 1 − (F − modeled entry)/R_ps`.
  - Normally `F` is the next bar's open in the same session, very close to the modeled entry, so `x` is far above +0.25R and **a trade that has scaled out cannot satisfy the FTP trigger**.
  - The only exception would be a fill more than **0.75 × R_ps** (about 1.5 ATR) above the modeled entry, e.g. a gap caused by a delayed fill. That is possible in principle but not expected.
  - FTP still applies its rule unchanged in that case. The implementation reports the number of such trades (expected 0), and a test enforces the normal case (§11).

### 5.7 Accounting

- The FTP exit is a normal sell fill.
- Its P&L feeds realized P&L, `record_close`, the loss streak, the daily loss limit, daily P&L and the daily profit halt, and all normal portfolio and risk state, exactly like any other completed exit (review decision R5).
- It is reported as its own exit reason, `failure_to_progress`.
- **Day boundary: inherited engine accounting convention (confirmed 2026-09-25; NOT changed for H002).** A fill at the first bar of a new session is processed **before** that session's first decision, which is when the daily reset happens (`RiskManager.start_of_day()`, daily P&L and profit-halt state to zero). For an overnight FTP exit filled at the next session's 09:30 open (or the next available bar):
  - the fill is **fully included** in equity, the trade ledger and total P&L;
  - `daily_results.csv` reports its realized P&L on the **fill date**;
  - its loss or win is applied to the running loss streak and daily P&L **before** the new session's reset, so it does **not** carry into the new day's loss streak, daily loss limit (whose starting equity is taken after the fill) or profit halt.

  This is the engine's existing rule for **any** fill before a day's first decision, and it is kept exactly as-is. Changing it would introduce a second experimental variable beyond the FTP rule. It is documented as an inherited convention and limitation (§13), tested (§11), and measured (§10).

**Path dependence:** different exits change later equity, sizing, halts and which later entries exist. That divergence is legitimate. It is **reported** through the matched and unmatched trade accounting of §10, not corrected for.

## 6. Constants (frozen; never tuned)

| Constant | Value | Origin |
|---|---|---|
| Post-fill closes before the check | 3 (fill bar included) | H001 losers: median 15 min (3 bars) to MAE |
| MFE threshold | < +0.25R (strict) | 79.7% of H001 losers and 98.6% of H001 stop hits never reached +0.25R |
| Current-close condition | `≤ F` (equality triggers) | only trades that are also not above entry |
| All H001 constants | unchanged | H001 spec §5–§7 |

## 7. Research splits and hygiene

| Split | Dates | Use for H002 |
|---|---|---|
| DEVELOPMENT | 2024-01-02 → 2025-12-31 | the first and only run allowed before the gate; **screening/resubstitution check only**, in-sample for the FTP constants (§3) |
| VALIDATION | 2026-01-02 → 2026-05-29 | **first untouched test**; stays unopened unless D1–D6 all pass **and** H002 is completely FROZEN (spec + code + commit recorded); viewed once |
| KNOWN/CONTAMINATED | 2026-06-01 → 2026-09-23 | never used for H002 selection; never run for H002 |
| FORWARD | 2026-09-24 → | chronological paper only, after H002 passes validation |

Rules:
- Any change to logic or constants after H002 development is viewed creates a new ID (`STRATEGY_V2_HYPOTHESIS_003`, …).
- A signal-changing bug fix after viewing also creates a new ID.
- Once validation is viewed, H002 is frozen permanently, whatever the outcome.

## 8. Pre-registered DEVELOPMENT gate (5 bps; identical to H001)

| # | Criterion |
|---|---|
| D1 | expectancy R > 0 (mean `realized_r` of completed trades) |
| D2 | profit factor ≥ 1.10 |
| D3 | completed trades ≥ 150 |
| D4 | `abs(max drawdown %) ≤ 25` |
| D5 | total R > 0 |
| D6 | `P_s` = sum of `realized_pnl` of completed trades for symbol `s`; `Π = Σ max(P_s, 0)`. PASS only if `Π > 0` **and** `max_s max(P_s,0)/Π ≤ 0.50` (exact; 0.50 passes). If `Π = 0`, D6 is **undefined and FAILS**. `P_s`, `Π` and all shares are always reported. |

Progression to validation: PASS only if D1–D6 all pass. Any failure means **REJECTED_AT_DEVELOPMENT**, and validation is never run.

## 9. Pre-registered VALIDATION gate (run once, only after §8 passes and H002 is FROZEN)

- V1: expectancy R > 0
- V2: PF > 1.0
- V3: total R > 0
- V4: max DD ≤ 30%

Directional comparison with development (reported, not gated): trades per day, expectancy R, PF, max DD, D6 share. Failure means **REJECTED_AT_VALIDATION**, and H002 is closed permanently.

## 10. Required DEVELOPMENT comparison: H002 vs H001 (reported, never used to tune)

**Aggregate metrics, side by side:**
- trades, win rate, expectancy R, PF, total R, P&L, max DD, max loss streak;
- count and P&L for each exit reason: `stop_hit`, `giveback_close`, `take_profit_hit`, `failure_to_progress`.

**Matched-trade accounting.** Trades are matched by `(symbol, entry_signal_timestamp)`, the same signal and entry.
- Report three groups: matched; only in H001; only in H002. Unmatched trades come from path dependence.
- **Losses avoided:** H001 `stop_hit` trades that H002 closed by `failure_to_progress`. Report the count, the sum and mean of `realized_r(H002) − realized_r(H001)`, and the P&L difference.
- **Winners sacrificed:** eventual H001 winners (`result == "win"`) that H002 closed by `failure_to_progress`. Report the count, R and P&L given up, and their H001 exit reasons.
- **Other FTP exits:** all H001 exit reasons of trades H002 closed by FTP (e.g. giveback losers and breakevens), with the R difference.
- **Exact decomposition:** `P&L(H002) − P&L(H001)` = matched-trade change + P&L of H002-only trades − P&L of H001-only trades. It must sum exactly.

**Overnight FTP reporting (required).** An FTP exit leg is **overnight** when the New York date of its fill differs from the New York date of the FTP decision that submitted it. This covers:
- the 16:00 checkpoint (R1);
- a checkpoint at an early close (13:00);
- a checkpoint whose next bar is missing that day, e.g. the 2024-12-23 feed gap.

Report:
- `overnight_ftp_fills`: the number of overnight FTP exit legs;
- `overnight_ftp_realized_pnl`: the sum of those legs' `realized_pnl`;
- `overnight_ftp_losses`: the number of those legs with `realized_pnl < −0.005`, using the engine's breakeven epsilon;
- `overnight_ftp_wins`: the number of those legs with `realized_pnl > +0.005`;
- also, for completeness: `overnight_ftp_breakevens`, and the split of overnight fills by checkpoint path (16:00, early close, gap).

The H001 reference is the stored run in `data/research_v1/h001_development`. H002 must reproduce H001's entry signals.

## 11. Test plan (all must pass before the DEVELOPMENT run)

**FTP rule, with hand-built 5Min bars:**
1. Exactly 3 post-fill closes: the fill bar's close counts as the 1st, and the test happens at the `E+2` decision.
2. No **FTP** exit after only 1 or 2 post-fill closes, even with deeply negative closes. The existing stop can still fire.
3. MFE < +0.25R and close < F: triggers.
4. MFE < +0.25R and close == F: triggers.
5. MFE == +0.25R exactly: no trigger.
6. MFE > +0.25R: no trigger.
7. MFE < +0.25R but close > F: no trigger. The check is not repeated later (one-time).
8. Execution: the exit fills at the next existing bar's open with sell slippage `open × (1 − 5 bps)` and $0 commission, with reason `failure_to_progress`.
9. No intrabar highs: a high above +0.25R with closes below it still triggers. Pre-fill (signal-bar) prices are ignored.
10. No look-ahead: modifying any bar after the checkpoint doesn't change the FTP decision.

**Timing and sessions:**

11. Near-close entry: counting continues into the next session, with no after-hours bars and no forced end-of-day exit.
12. Weekends, holidays and missing bars are skipped chronologically.
13. 16:00 checkpoint (R1): when the 3rd post-fill close is the 15:55 bar, the test is evaluated at 16:00 with exactly the first three closes and the third close as `current_close`. Specifically:
    - later closes can't change the decision (modifying the next session's bars doesn't change it);
    - a trigger fills at the next session's first available bar open (09:30, or later after a weekend, holiday or gap) with sell slippage;
    - a non-trigger is never re-tested;
    - an early-close 13:00 checkpoint follows the normal path.
13b. **Day boundary (inherited convention; explicit tests).** For a losing overnight FTP exit filled at the next session's 09:30 open, check each of the following:
    1. the fill is in the ledger, and final equity and total P&L include it;
    2. `daily_results` attributes its realized P&L to the fill date;
    3. after the new session's first decision, `RiskManager.consecutive_losses` is 0. The overnight loss does not carry, although it was applied before the reset;
    4. the new day's `day_start_equity` already reflects the loss, so the loss is not counted against the new day's daily loss limit;
    5. the new day's daily P&L (profit-halt state) starts at 0 and excludes the overnight fill;
    6. the same behavior holds for an H001 fill before the first decision (e.g. a delayed fill), proving it is the engine's existing rule and not FTP-specific;
    7. the four overnight metrics (§10) count the fill correctly: wins, losses and breakevens by the leg's `realized_pnl` sign, and the checkpoint path.
13c. A pending exit already in flight at the checkpoint consumes the FTP check without effect.

**Interaction with existing management:**

14. Scale-out:
    - a trade that has scaled out (with a normal, same-session fill) cannot trigger FTP;
    - FTP applies only to remaining shares, and uses the original `F`, the original `R_ps` and the MFE since the original entry;
    - the count of trades where the fill was more than 0.75·R_ps above the modeled entry is reported.
15. Precedence: if a stop, take profit, giveback or scale-out fires at the checkpoint decision, that exit and its reason stand, and FTP is consumed without effect. If the position closed before the checkpoint, FTP does nothing.
16. Uses the fill, not the modeled entry: `F`, not the modeled entry, is the FTP reference.
17. Accounting: FTP exits count toward the loss streak, daily P&L and halts like any exit.

**Invariance and determinism:**

18. Deterministic: identical outputs on repeated runs.
19. H001 unchanged:
    - With FTP disabled, the H002 engine reproduces H001 exactly (trades, fills, equity).
    - With FTP enabled, the **entry signals are identical on every bar**: the strategy is a pure function of the bars, and the strategy object is the same.
    - Only *which* signals become trades may diverge, through portfolio state (positions, halts, sizing). That divergence is reported (§10).
20. MA_BASELINE_V1 and the shared engine unchanged: FTP lives in a research-only engine subclass, and shared-engine defaults are untouched.

**Hygiene:**

21. Development only: the runner accepts only the development split.
22. Validation refused unless the registry status is FROZEN **and** the development gate passed **and** validation has not been viewed.
23. Known and forward refused always.

## 12. Implementation boundary (for the later implementation step)

- **Where FTP lives:** a research-only subclass of the portfolio backtester.
  - It appends step 6 after the unchanged `_manage` sequence at permitted decisions, and detects whether steps 3–5 submitted an order on the same decision.
  - For a checkpoint whose close instant is not a permitted decision (16:00), it evaluates FTP at that bar's close, in the per-bar mark/excursion step that already runs for every bar, and submits a **pending** sell to the simulated broker. That sell fills at the symbol's next available bar open through the normal fill path, with no new fill logic.
  - This pending-exit path is **backtest-only**; there is no live counterpart.
- **Checkpoint tracking:** each trade's existing close-based excursion tracker supplies the post-fill closes and the maximum close.
- **Unchanged:** H001's strategy and runner, the shared engine, the RiskManager, `run_paper`, BrokerAlpaca, live stops and order tracking, and `src/smoke_test.py`. No live behavior changes.

## 13. Known limitations

1. **In-sample development:** the development result is in-sample for the FTP constants (§3).
2. **Close-based FTP:** FTP is evaluated on 5Min closes and fills at the next open, which adds one bar of latency. It also pays slippage twice on round trips it cuts short.
3. **Mixed references:** FTP uses the fill price while existing management uses the modeled entry (§5.5), so a trade can be "at entry" for one and not for the other.
4. **Path dependence:** halts and sizing change, so matched comparisons are partial (§10).
5. **Inherited H001 limitations:** IEX only, feed gaps, the unadjusted NVDA split, a liquidity filter that is looser on 5Min bars, the fixed-dollar profit halt, overnight gap risk, and a small, correlated universe.
6. **Overnight effect unknown:** the H001 autopsy found the 41 overnight holds to be the most positive group. FTP may cut some of them before they are carried overnight. This is **not** handled specially, and its effect is simply reported.
7. **Inherited day-boundary accounting convention (§5.7):**
   - An overnight FTP exit that fills at the next session's open is fully included in equity, the ledger, total P&L and `daily_results` (fill date).
   - Because it precedes that session's reset, it does not carry into the new day's loss streak, daily loss limit or profit halt.
   - This is the engine's existing rule for any fill before a day's first decision. It is kept unchanged on purpose, to avoid a second experimental variable, and its size is measured by the overnight FTP metrics (§10).
8. **No live counterpart:** the 16:00 FTP evaluation and pending exit are backtest-only. Any future live use would need its own mechanism and a new review.

## 14. Review decisions (settled 2026-09-25, before implementation; part of the frozen spec)

| # | Question | Decision |
|---|---|---|
| R1 | 3rd post-fill close on the session's last bar (close known at 16:00; no engine decision) | **Evaluate FTP at 16:00** with exactly the first three post-fill closes and the third close as `current_close`. No later close is used. If triggered, the exit is **pending** and fills at the next available regular-session bar open, normally the next session's 09:30. It is neither skipped nor deferred to a later close. Backtest-only pending path; live unchanged (§5.3, §12). |
| R2 | Precedence | **Approved as proposed:** H001's `_manage` sequence runs unchanged; FTP is step 6, only if no exit order was submitted at that decision (§5.4). |
| R3 | FTP reference price | **Approved:** actual fill `F`. Existing H001 management keeps its modeled-entry semantics unchanged (§5.5). |
| R4 | R denominator | **Approved:** original initial risk per share (`risk_per_share_modeled`), identical to the `realized_r` / `mfe_r` denominator. |
| R5 | FTP P&L in state | **Approved:** it feeds the loss streak, daily P&L and halts, and all portfolio and risk state, like any completed exit (including the engine's existing day-boundary rule, §5.7). Path divergence is legitimate and reported (§10). |
| R6 | Day-boundary accounting of overnight FTP exits | **Confirmed: keep the engine behavior exactly.** The fill is in equity, the ledger, total P&L and `daily_results` (fill date), but it does not carry into the new day's loss streak, loss limit or profit halt after the reset. It is not changed for H002 (that would be a second variable). It is documented as an inherited convention (§5.7, §13.7), tested (§11.13b), and measured (§10 overnight metrics). |

## 15. Change history

- 2026-09-25: specified (SPECIFIED_NOT_IMPLEMENTED). No code, no backtest, no H002 data viewed. Validation, known and forward untouched.
- 2026-09-25: review decisions R1–R5 settled (§14).
  - R1 revised: FTP is evaluated at 16:00 on the 3rd close, with a pending exit at the next available open.
  - Added the explicit disclosure that development is a screening/resubstitution check (§3).
  - Added the day-boundary accounting note (§5.7).
  - Still SPECIFIED_NOT_IMPLEMENTED; no code, no data viewed.
- 2026-09-25: R6 confirmed (day-boundary convention kept; explicit tests §11.13b; overnight FTP metrics §10). **Specification approved and FROZEN.**
  - Implementation starts only after this frozen document is committed; that commit hash is recorded in the header and the registry.
  - Status remains SPECIFIED_NOT_IMPLEMENTED. No code, no data viewed.
