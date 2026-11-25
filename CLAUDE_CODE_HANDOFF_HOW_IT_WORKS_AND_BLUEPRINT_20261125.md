# Handoff for Codex — what Vivek asked, what the code actually does, and a blueprint

**Date:** 25 November 2026
**Branch:** `review/pcr-vix-iv-greeks-20261123`
**Reference:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda0642a421865261a36dc62aca06e2e1d4`, 2.6.104 / b535 — **frozen**
**Nature of this document:** description of installed behaviour + **proposals requiring review**.
Nothing here is implemented, activated or tuned. No brain behaviour was altered.

---

## 0. The three questions Vivek asked

1. **Is there ample comparison of past data — if an entry could be at 09:20, can our app predict it?**
2. **How is strike selection calculated?**
3. **What about the stop loss and target?**

Below: the answer to each from source and from the captured data, then what is genuinely wrong
(as distinct from what is simply a design boundary), then a ranked blueprint.

---

## 1. Findings

### 1.1 A 09:20 entry is outside what the design can express — and there is no past-data comparison at all

**By construction.** The direction engine measures the session against **its own opening
reference**, defined as:

```python
OPEN_REF_WINDOW                = ((9,20,0), (9,35,0))   # [09:20, 09:35) IST
DIRECTION_START_AFTER_OPEN_MIN = 10.0
Z30_WARMUP_MIN                 = 10.0
ENTRY_START, ENTRY_END         = (9,30), (14,30)        # index_single_leg_v1
```

09:20 is when the yardstick **starts being built**, not when a verdict exists. And `ENTRY_START`
is 09:30, so even a valid verdict could not be acted on earlier.

**Measured across the 9 complete captured sessions × 2 indices (18 session-index units).**
Today, 25 November, is excluded from the counts below: it has 9 polls so far (09:15–09:55, 3
`UNAVAILABLE` + 6 `NEUTRAL`) and is still in progress, which is unremarkable at that hour.

| | result |
| --- | --- |
| First verdict of **any** kind | **09:30, on every unit including today** — 09:15 / 09:20 / 09:25 are always 3 `UNAVAILABLE` polls |
| Earliest ever **directional** call | **09:30** (23 Nov, both indices) |
| Typical first directional call | 10:00 – 11:00 |
| Latest | 13:35 (11 Nov NF) |
| **No directional call at all, whole session** | **4 of 18 complete units** — 12 Nov (both indices), 13 Nov BNF, 16 Nov NF |

**"Ample comparison of past data" — no, and this is the weakest part of the whole system.**

- The direction call uses **no prior-session history whatsoever**. It is a z-score against today's
  own opening reference with fixed thresholds (`Z_OPEN_THRESHOLD = 0.50`,
  `STRONG_THRESHOLD = 1.0`). There is no fitted predictor, no backtest-derived parameter, no
  cross-session comparison in the decision path.
- The component that *would* introduce history — the percentile authority — **never once held** in
  the data examined: `target_threshold_basis` and `stop_threshold_basis` were
  `constant_only` / `constant_safety_floor` on **all 822 position ticks**. The published-threshold
  path has never been exercised in Paper.
- My own research sample is **9 sessions, 2 indices**, and after accounting for overlap and
  pairwise dependence, **effectively two independent session-direction episodes** for the exit
  work and 14 session-index units for the structure work. That is not an evidence base for a
  predictive claim, and I have not made one.

### 1.2 Strike selection — fully mechanical, with liquidity gating, and no optimisation

Not "nearest to spot". It is the **parity strike** (`index_authority_v1.parity_strike`):

> `argmin |CE_mid − PE_mid|` over **±5 listed strikes** around spot, requiring both legs
> `0 < bid ≤ ask` and relative spread `≤ 10%` (`SCALE_MAX_REL_SPREAD`), **ties to the lower
> strike**, and **LTP is never used**.

i.e. the strike where calls and puts price equally — the market's own implied forward — with
illiquid strikes skipped rather than selected.

The structure then follows from K0 with a **fixed width** (`index_construction_v1`):

| direction | structure | legs | width |
| --- | --- | --- | --- |
| BULL | `BULL_CALL` | long CE at **K0**, short CE at **K0 + w** | `WIDTH_FLOORS` NF **150** / BNF **400**, taken from `BASELINE_LADDERS`, rounded **up** to a multiple of the true strike step |
| BEAR | `BEAR_PUT` | long PE at **K0**, short PE at **K0 − w** | same |
| outright | `BUY_CE` / `BUY_PE` | **K0** (`select_strike` returns the parity strike for any BUY) | — |

**No alternative strike or width is scored, ranked or compared.** Spot → parity strike → fixed
width floor. (This is precisely why Test 2b was a clean experiment: the app names one strike, so
the outright and the spread necessarily share the same long leg.)

**One real defect found in the action chooser.** `choose_action` selects buy-vs-sell on the
efficiency ratio:

```python
ER_SWITCH = 0.30            # self-labelled 'er_switch_status': 'UNVALIDATED_PAPER_CHOICE'
if v == 'BULL':  return ('BUY_CE' if er >= ER_SWITCH else 'SELL_PE'), None
else:            return ('BUY_PE' if er >= ER_SWITCH else 'SELL_CE'), None
```

Two things follow, and the second is a correctness problem:

1. `0.30` is **labelled by the app itself as an unvalidated choice**. It is not derived from
   anything.
2. When `er < 0.30` the chosen action is a **single-leg SELL** — which standing policy
   **excludes**. So a valid STRONG direction with low ER produces an action that is then dropped,
   and the user sees no trade **without a reason code saying why**. That is a silent dead end, not
   a decision.

### 1.3 Stop loss and target — two different engines, and one of them has no target at all

**(a) Managed single leg** (`index_single_leg_v1.evaluate_exit`) — the live exit reasons are
exhaustively: `TIME_EXIT`, `TIME_EXIT_OVERDUE`, `STOP_PLANNED`, `INVALIDATION_OPPOSITE_CONFIRMED`.

| control | value |
| --- | --- |
| **Stop** | `BUY_STOP_FRAC = 0.70` — exit when the **bid** falls to ≤ 70% of the entry fill (a 30% premium loss). `SELL_STOP_FRAC = 1.50` exists but single-leg selling is excluded |
| **Profit target** | **none. There is no profit target in this policy at all.** |
| **Time** | `TIME_EXIT = (15,5)`; the Paper pilot additionally carries `exit_deadline = admission + 60 min`, `clock_anchor: admission_time` (`paper_strong_buy_60m_v1`) |
| **Invalidation** | two **confirmed opposite-direction** observations (`contrary_confirmation`, `confirmation_required: 2`). **NEUTRAL does not count as opposite** |
| Session limits | `MAX_ENTRIES_PER_SESSION = 2`, `REENTRY_GAP_MIN = 30` |

So it holds until time or stop. The 23 Nov managed BNF 64,000 PE closed at **+₹3,070.09 net on
`TIME_EXIT`** — the 60-minute deadline, **not** a target.

**(b) Spread positions** (`PositionPolicyV1`, surfaced by `PositionTickService`):

| control | rule |
| --- | --- |
| Target | `current_pnl >= 0.50 × max_profit` (`TP_MULT`) |
| Stop | `current_pnl <= −0.60 × max_loss` (`SL_MULT`) |
| End of day | 15:15 |
| Composition | these compose with brain-published rupee `target_pnl_at` / `stop_pnl_at` when the live percentile authority holds, so publication can only fire **earlier or equal**. **It never held** — constants stood alone on all 822 ticks |
| Execution | **`SHADOW_*` only — alerts, not executions.** All four Paper spreads were closed manually by the operator |

**Measured behaviour of these controls (23 Nov and 20 Nov Paper positions):**

| position | signal | P&L at signal | operator booked | difference |
| --- | --- | ---: | ---: | ---: |
| 318 | `SHADOW_EOD` 20 Nov 15:15:38 | +1,368.00 | **+2,079.00** | operator +711.00 better |
| 319 | `SHADOW_EOD` 20 Nov 15:15:38 | +841.75 | **+3,094.00** | operator +2,252.25 better |
| 321 | **`SHADOW_SL`** 23 Nov 11:14:51 | **−3,147.00** | **+1,560.00** | operator **+4,707.00** better |
| 320 | *never left* `HOLD` (175 ticks) | — | +1,361.75 | policy contributed nothing |

**Every exit signal that fired was early, in all four positions.** And the headline latency
finding: **₹2,639.00 of signalled profit was lost between the target alert firing and the manual
close one to two minutes later** (318: 3,775.50 → 2,079.00; 319: 4,036.50 → 3,094.00), against
₹7,627.33 total booked net on the four trades.

---

## 2. Diagnosis — separating defects from design boundaries

This distinction matters, because the two need completely different responses.

| # | observation | **defect** or **boundary**? |
| --- | --- | --- |
| 1 | No verdict before 09:30 | **Boundary.** The warmup is what makes `z_30` meaningful. Loosening it would make the gate *vacuous* (see §3 D3) |
| 2 | No prior-session history in the direction call | **Boundary of the current design**, but the honest answer to "ample past data" is **no**. Any change here is a **new signal**, not a tweak |
| 3 | Percentile authority never held (822/822 constant) | **Defect or dead code — unknown which.** Needs diagnosis: is it correctly withholding, or silently failing? |
| 4 | `ER_SWITCH = 0.30` self-labelled unvalidated | **Boundary**, honestly flagged by the code |
| 5 | `er < 0.30` → excluded SELL action with no reason code | **Defect.** A silent dead end |
| 6 | No profit target on the managed single leg | **Deliberate boundary.** Not a bug. Whether it *should* exist is a policy question |
| 7 | Shadow stop reachable by a profitable trade (321) | **Open risk**, not yet a defect — but must be settled before the shadow stop executes |
| 8 | ₹2,639 alert-to-execution loss | **Defect in attribution** (the loss is invisible), not in the threshold |
| 9 | `trades_v2.peak_pnl` matches neither extrema field | **Defect.** PWA-side read path |
| 10 | 321's net unverifiable (fill legs not persisted) | **Defect.** E1, native half shipped |
| 11 | BNF PCR reconciles 0/695 | **Discrepancy, cause not established.** Candidate: divergent silent fallbacks |

---

## 3. Blueprint

Ranked by **evidence strength**, not by appeal. Tier 1 is supported by measurements already in
hand and changes no policy. Tier 2 are genuine design questions where **the data cannot choose the
answer** and a predeclared fresh-session test is required first. Tier 3 is blocked on capture.

### Tier 1 — do these first: measured, cheap, no policy change

| id | change | why it is justified now | cost | status |
| --- | --- | --- | --- | --- |
| **F1** | **Capture the contracts the decision names.** M1: request the **held** `instrument_key` through the existing targeted path at each 5-min poll from admission to exit. C1: request the **named candidate** (`why.sl.k`) each poll. C3: persist `expiry`, `lot_size`, `tick_size`, `underlying_key` and the three clocks with each record | **This single item unblocks Tests 3, 4, 2a and the longer-hold comparison.** Today: candidate records show **0 of 352** with identity fields or clocks, and **0 of 80 NF** candidates captured at all. The managed position has **1 scoreable endpoint** out of six | 1–2 extra instruments per poll on **paths that already exist and already work** | **not started — highest leverage** |
| **F2** | PWA consumes the published **close bundle** for gross P&L, fees and the saved outcome | Four independent instances of the fee/P&L split (E1). 321's net ₹1,441.86 is **unverifiable** because the fill's per-leg prices are persisted nowhere | 0 — native half shipped in `d2cc9f2` | native done, **PWA pending** |
| **F3** | PWA consumes `getValidatedPositionExtrema` for `peak_pnl` / `trough_pnl` | `trades_v2.peak_pnl` matches **neither** extrema field on **any** of the four trades; 319's stored 267 is below its own booked 3,094 | 0 — read path shipped for review in `924443f` | **pending review + PWA** |
| **F4** | Make the **alert-to-execution gap attributable**: the target alert carries the quote bundle it fired on, and the close records both | ₹2,639 vanished invisibly into booked P&L. This does not recover it — it makes it **visible** instead of silently absorbed | 0 — bundle already published | **pending PWA** |
| **F5** | Record the **winning fallback tier** for every PCR and OI value | BNF PCR reconciles 0/695 and the cause can currently only be narrowed by elimination | 0 — shipped prospectively in `fb1c480`; historical gap is permanent | **shipped, unreviewed** |
| **F6** | **Name the excluded-SELL outcome.** When `er < ER_SWITCH` selects a `SELL_*` action that policy excludes, emit an explicit reason code instead of a silent no-trade | §1.2 item 2. A user currently cannot tell "no signal" from "signal, action excluded" | trivial; **reason code only, no behaviour change** | **not started** |
| **F7** | **Diagnose why the percentile authority never holds.** Determine whether it is correctly withholding or silently failing, and record which | 822 of 822 ticks ran on constants. An entire designed pathway has never executed in Paper and nobody knows if it works | read-only investigation | **not started** |

### Tier 2 — design questions: measure in shadow, do not ship

Each of these is a **new policy**. The standing rule applies: freeze the rule, test on fresh
sessions, review before activation.

**D1 — should the managed single leg have a profit target at all?**

- *Observation.* There is none today. Giveback from the trusted peak on the four spreads was
  **2,397 / 942 / 312 / 1,215**.
- *What the data can and cannot say.* It can say giveback is real and material. It **cannot choose
  a level** — four trades, two dependent pairs, and all four were trend-aligned debit spreads.
- *Proposal.* **Shadow only.** Log what a peak-capture target (e.g. exit at 0.50× / 0.75× of the
  running trusted peak) *would* have done, alongside the installed rule, for a predeclared number
  of fresh sessions. **No execution. No threshold chosen in advance of the data.**

**D2 — tail control is about capital deployed, not structure**

- *Evidence (Test 2b, arms A/B/C).* The observed extreme tracked **risk deployed**, not the
  structure: arm A risked ₹10,309 → worst −2,535; arm C risked the same ₹10,292 through more lots
  of a narrower structure → worst −21,252.
- *Already rejected.* "Size the spread by the outright's lot count" — costs ₹513.89 per
  observation for a 10% tail improvement while idling 59% of the budget. **Parked.**
- *Proposal.* The untested lever is the **₹25,000 budget itself**
  (`capital × max_risk_pct`), not the structure or the lot rule. That needs **its own predeclared
  experiment**; I have not run it and would not infer it from the existing sample.

**D3 — earlier entries, if genuinely wanted**

- **Do not loosen `Z30_WARMUP_MIN` or widen `OPEN_REF_WINDOW` to get them.** While the 30-minute
  anchor is shared with the open reference, `z_30 = z_open / √(Δ/375)` — a positive multiple of
  the same sign — so the BEAR/BULL gate's second condition is **already vacuous** in that window
  (verified: 23 Nov 09:35 BNF ratio exactly 5.000000, bounded to 09:30–09:50 across 778
  decisions). Loosening the warmup widens a window in which the gate cannot refuse anything. That
  buys **fake confidence**, not earlier signal.
- *The only honest route* is a **differently anchored** direction — e.g. prior-session close plus
  the gap, which the context already carries — and that is a **new signal requiring its own
  validation from scratch**, not an adjustment to this one.
- *Proposal.* Treat as a **separate research track**, shadow-published alongside
  `index_direction_v1` and scored against it before anyone discusses acting on it.

**D4 — the 0.60 shadow stop, before it ever executes**

- *Evidence.* On 321 it fired at **−₹3,147** on a position that closed **+₹1,560** — a ₹4,707
  swing from overriding it. One instance is **not** a case against stops (a stop is supposed to
  cost money when a position recovers), but it establishes that the threshold is **reachable by a
  trade that ends comfortably profitable**.
- *Proposal.* **Settle this before promoting any shadow stop to an executing stop.** It needs a
  distribution, which means fresh sessions, not a re-reading of these four.

### Tier 3 — blocked; specify the capture and wait

| test | blocked on | minimal capture |
| --- | --- | --- |
| **2a** eligible-expiry comparison | only one expiry captured per session+index, ever | **C4** — a second expiry at the decision poll, 2–6 instruments |
| **3** exact-contract IV + VIX | IV exists for 12 pinned contracts, 41 slots, 100% present — but `iv_unit_status: UNKNOWN`, and only 2 are position legs (both short) | **M3** (resolve IV units, 0 extra requests) + **C1/C3** |
| **4** premium attribution / Greeks | **no source carries Greeks.** Production has only `_bs_delta` and an explicitly *simplified* `_bs_theta` — no gamma, no vega, no IV inversion | **C1/C3** + an IV solver must be **built**; every output labelled model-derived |
| **longer hold**, Dec BNF 64,000 PE | zero `position_ticks` rows; the app's own observer marks `near_close`, `next_open`, `next_60` as **`PENDING`** | **M1 + M2.** **No substitution accepted** — not NF prices, not another BNF contract |

---

## 4. What must **not** be done

Each of these was measured and rejected, or is a known trap.

1. **Do not adopt either pullback exit.** Reference +₹7,627.33 against C1 −₹3,664.12 and
   C2 −₹4,260.88; **4 of 4 positions worse**. A PULLBACK is by construction a counter-trend signal
   inside an intact trend, so exiting on it sells the retracement of the move the position holds.
2. **Do not adopt the common-lot-count sizing rule.** Parked, with the arithmetic in `f09478f`.
3. **Do not loosen the direction warmup to obtain earlier entries.** See D3 — it widens a window
   where the gate is already vacuous.
4. **Do not tune `ER_SWITCH`** (or `Z_OPEN_THRESHOLD`, or `TP_MULT`/`SL_MULT`) on this sample.
   9 sessions, 2 indices, two dominant trending days. Any fit would be to those two days.
5. **Do not read `trades_v2.peak_pnl` or the `journey_stats` extrema** for any excursion
   statistic. Use `policy_trace_json.trusted_mfe` / `trusted_mae`, which match an independent
   first-principles recomputation **to the rupee** on all four trades.
6. **Do not backfill or rewrite historical rows** — not the extrema, not the fees, not the PCR
   lineage. The gaps are evidence; a migration is a separate, separately approved change.
7. **Do not treat `running_mfe` / `running_mae` as wrong.** They diverge from `trusted_*`
   **by design** (`EXTREMA_CONTRACT = observed_every_accepted_valuation_no_exclusions`). I had
   this wrong and corrected it.
8. **Do not treat the 09:15 chain as proven corrupt.** It is **SUSPECT, NOT PROVEN** — an opening
   IV move on 0-DTE OTM options is a *possible* explanation and neither it nor corruption is
   established. The app's own validity check flags only **post-close** rows, not the opening ones.
   Excluding 09:15 is a declared conservative assumption whose measured cost is **nil**.

---

## 5. Suggested sequence

1. **F1** (capture) — nothing else in Tier 3 moves without it, and it is the cheapest item on the
   list relative to what it unblocks.
2. **F2, F3, F4** — PWA-side consumption of three bundles already published or prepared. Closes
   E1, E11′ and the latency blind spot.
3. **F6, F7** — two small honesty fixes: name the excluded-SELL outcome, and find out whether the
   percentile authority is withholding or broken.
4. **F5** — already shipped; needs review.
5. Then, and only with fresh-session data from F1: **D1** and **D4** in shadow, **Tier 3** tests.
6. **D2** and **D3** are separate research tracks and should not be bundled with the above.

**My recommendation on priority:** nothing further is worth measuring on 11–23 November. Every
remaining question is **capture-limited, not analysis-limited**. F1 is one or two extra instruments
per poll on paths that already work, and it converts four blocked tests into answerable ones. I
would rather wait for that than produce another study whose conclusion is bounded by its inputs.

---

## 6. Boundaries

Read-only database access throughout. Review branch only; nothing merged. **b535 frozen** — no
version bump, no APK or PWA release, no candidate approval, no `approved_by` written, and **no
threshold, stop, target, strike rule, width or exit rule changed, retuned or activated**. Both
pullback challengers and the lot-count sizing rule remain **parked**. Original trade rows and
open-position records preserved exactly as the app wrote them; wrong journey fields are reported,
not rewritten; nothing backfilled. Manual Paper accessible, Real disabled, single-leg selling
excluded; call/put buying and protected two-leg spreads are the only structures scored. Entries
evaluated throughout supported trading hours with no fixed cutoff. Kotlin is **uncompiled in this
sandbox** — `PaperCloseBundleTest` (8 cases) and `ValidatedExtremaReadPathTest` (15 cases) are
pending build verification, and the signed-release workflow is the compile gate. Python side: the
full suite is **172 of 172 modules green** with an adequate per-module budget. No raw phone
exports, Paper positions or credentials committed; no secret, token, key or credential appears in
any file, fixture, log or in this handoff.
