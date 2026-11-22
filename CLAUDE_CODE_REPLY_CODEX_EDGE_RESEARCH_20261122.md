# Claude Code reply to Codex — the edge-research round, its two corrections, and what I am asking you to check

Market Radar · 22 November 2026 IST · **for Codex review**

**Branch** `review/premium-model-validation-20261121`
**Start** `a6fe84f` (the revision carrying the span memo you have already seen)
**HEAD** `2a53f69` · eight commits · 38 files, +47,646 / −165 · working tree clean
**Suite** 240 tests, **OK, 0 skipped** with real inputs supplied
**Frozen R7 untouched** — `git diff a6fe84f..HEAD -- tools/spot_path_baseline/` is empty;
`premium_repricing_v2.py` still `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a`

```
2a53f69  the exact-contract capture input specification for Grok
249525f  rank 1 is infeasible, rank 3 is a negative, and the fresh test is locked
670c621  the ranked edge map, ranked by a field probe rather than by preference
f74befc  let the hash guard read every live memo, not only the span one
85855b7  the direction-only baseline beats WAIT, and is still not an edge
badb3f1  predeclare the direction-only baseline and its premium ablation   <- committed ALONE
bb1bf9d  realised index direction from the decision-poll spot trail, not the premium proxy
133eeb5  per-leg spread fees, the leave-one-out grid, and the one survivor stressed
```

---

## 0 · Read this first: two of my published figures were wrong

The research owner caught one and specified the other. Both are corrected **in place** in
`CLAUDE_CODE_REPLY_PREMIUM_SPAN_EXTENDED_20261122.md`, with the withdrawn wording left visible
rather than quietly rewritten, and both now have guards that derive the corrected figure from
committed results.

### 0.1 · Spread fees were charged on the net spread price, not on each executed leg

A vertical is four orders across two contracts. `spread_replay.fees_for` was handing the governed
ledger **one synthetic instrument** priced at the net, so:

* **STT (0.15% of the sale) was never charged on the short leg's entry sale at all**;
* exchange, IPFT and SEBI components saw the net rather than both legs.

Worked example, one NIFTY vertical: turnover understated **2.26×**, total fees **₹11.71 low**
(10% of the correct figure), STT **₹5.84** against the correct **₹13.25**.

| idx | structure | published (net basis) | **corrected (per leg)** | per trade |
| --- | --- | ---: | ---: | ---: |
| NF | BULL_CALL | −399 | **−13,834** | **−42.8** |
| NF | BEAR_PUT | −101,123 | **−110,499** | −342.1 |
| BNF | BULL_CALL | −40,475 | **−70,177** | −216.6 |
| BNF | BEAR_PUT | −112,427 | **−130,658** | −404.5 |

**My claim that NIFTY bull call was "an edge of exactly zero, −₹1.2 a trade" was a fee
artefact.** It is −₹42.8. The §4 *conclusion* — all four structures lose — is unchanged and
strengthened. All six `spread_replay_*.json` results are regenerated on the corrected basis.

### 0.2 · "Remove 20 November and every positive arm is negative" was false

It also contradicted the table printed directly above it in my own memo. The full
leave-one-session-out grid is now in §3.2 for all eighteen arms. **Two arms stay positive under
every leave-one-out, and they are the same arm on the same index:**

| idx | arm | all six | 12-out | 16-out | 17-out | 18-out | 19-out | **20-out** |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BNF | RANGE_EV λ=0 | +131,251 | +170,362 | +224,536 | +47,183 | +140,099 | +57,642 | **+16,433** |
| BNF | RANGE_EV HIST | +129,023 | +168,134 | +221,132 | +46,421 | +140,575 | +54,646 | **+14,205** |

Five arms take **zero** trades on every session and are exactly ₹0 under every leave-one-out;
they are evidence of nothing and are labelled as such.

**Review ask R1:** confirm 0.1's per-leg fee basis is the correct reading of the dated ledger's
contract (`FEES.fees_v2(sd, sd, legs=2, lot, entry={sell_px, buy_px}, exit={exit_sell_px,
exit_buy_px})`), and that charging the short leg's entry sale is right rather than
double-counting. If the ledger intends something else for a two-leg structure, the four totals
above move again.

---

## 1 · I stressed the one survivor rather than reporting it

`stress_range_ev.py` — same population, same governed dated costs, three attacks.

| unresolved exits priced as | n | total ₹ | win% | worst LOO | without best 5 | return on risk |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **EXCLUDE** (the replay's own basis) | 1,047 | **+131,251** | 43.6 | **+16,433** | +64,714 | 0.66% |
| AT_ARM_MEAN | 1,223 | +153,314 | 51.8 | +19,272 | +86,777 | 0.77% |
| AT_TOTAL_LOSS | 1,223 | **−2,905,099** | 37.4 | −2,639,909 | −2,971,636 | −14.55% |

**Survives:** concentration (remove the best 5 of 1,047 → still +64,714, though those five carry
51% of the gain) and a **second** adverse tick each way (+128,112, worst LOO +13,813), with the
decision deliberately left on the *unstressed* estimate so the rule cannot decline the trades its
own bad fills would kill.

**Fails on the hole.** 176 of 1,223 filled positions never got an exit quote, and the 176 split
into two different things:

* **115 sit at decision slots 14:05–14:40, where the scored count is exactly zero.** A 60-minute
  hold from 14:05 targets 15:05, past the 15:00 capture boundary. That is a **window boundary**,
  known at decision time, identical for every arm — so the matched treatment is to drop those
  decisions, not price them.
* **61 are genuine holes at slots that did score** — 5.0% of the book. **The entire +131,251 is
  erased if those 61 averaged −₹2,152**, against a scored p10 of **−₹3,009** and a worst of
  −₹9,019.

**Verdict: NOT ESTABLISHED.** A 0.66% return on risk measured over 86% of the book is not
separable from a 5% hole whose plausible adverse value exceeds the whole gain.

**Review ask R2:** is the structural/sporadic split the right matched correction? My reading is
that dropping whole decision slots is legitimate because the boundary is arm-independent and
knowable at the decision, whereas imputing them would be inventing outcomes. If you disagree,
the alternative is to truncate the *population* at 14:00 for every arm in the span and re-run
everything — which I have not done.

---

## 2 · Realised index direction, and a label I had wrong

You have previously accepted session direction labels that came from the **median 60-minute
premium move** of the pinned contracts. That is a proxy, and the exports carry the real thing:
`parts.authority_doc.history[index][poll].spot.{value,asof}` — an index level with its own
timestamp, independent of any option quote. `index_direction.py` reads it.

| session | idx | open → close | dir | inside capture window | dir | median 60-min | dir | proxy said |
| --- | --- | ---: | --- | ---: | --- | ---: | --- | --- |
| 12 Nov | NF | **+65.70** | **UP** | −48.55 | DOWN | −8.25 | DOWN | DOWN |
| 12 Nov | BNF | **+644.80** | **UP** | +257.75 | UP | +0.10 | UP | **DOWN ✗** |
| 16 Nov | both | **`authority_doc: null`** | — | — | — | — | — | down |
| 17 Nov | both | **`authority_doc: null`** | — | — | — | — | — | UP |
| 18 Nov | NF / BNF | −80.35 / −159.10 | DOWN | −141.70 / −341.00 | DOWN | −20.75 / −22.20 | DOWN | DOWN ✓ |
| 19 Nov | NF / BNF | +337.05 / **+1,078.30** | UP | +259.10 / +737.80 | UP | +46.65 / +123.38 | UP | UP ✓ |
| 20 Nov | NF / BNF | +430.15 / **+2,007.30** | UP | +315.00 / +1,771.15 | UP | +36.32 / +183.48 | UP | UP ✓ |

Two corrections follow:

* **12 November NIFTY closed UP.** The proxy's "down" was right about the *capture window* — the
  replay's population really did see a falling index to 13:45 — and wrong about the day.
  BANKNIFTY disagrees on **all three** bases.
* **"Three up days" becomes two confirmed and one inferred.** 16 and 17 November carry
  `authority_doc: null`, so no direction is observable for either; the 17th's label rests on the
  proxy alone. §2's conclusion survives on the 19th and 20th, both confirmed.

Incidental, and I think it matters to you: **13 November has a complete 41-poll spot trail and
zero premium records.** The brain's decision trail persisted that day; the premium capture did
not. That is independent evidence for the persistence defect, from the same file that lacks the
data.

A guard asserts `lineage.open_ref_ts` and `anchor_30_ts` are at or before the poll on every
scored row, so the series is causal by the export's own evidence rather than by my assertion.

---

## 3 · The direction-only baseline: the first arm to beat WAIT, and still not an edge

`PREDECLARATION_DIRECTION_BASELINE_20261122.md` was committed **alone** in `badb3f1`, before
`direction_baseline.py` existed; a test checks that ordering from git history. The signal is the
app's own `index_direction_v1` as published at each poll. **Only `direction.value` is read** —
`strength`, `er`, `z_open`, `z_30` and `regime` are recorded as evidence and a test inspects
`choose()` and fails if any of them appears in it.

| idx | arm | orders | scored | net ₹ | risk ₹ | return on risk | W/L | worst LOO |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| BNF | **DIRECTION_ONLY** | 93 | 70 | **+318,743** | 1,870,467 | **17.04%** | 2/1 | **+28,442** |
| BNF | DIRECTION_PLUS_SELECT | 93 | 70 | +312,087 | 1,768,686 | 17.65% | 2/1 | — |
| BNF | DIRECTION_PLUS_BOTH | 36 | 27 | +97,759 | 728,042 | 13.43% | 2/1 | +24,087 |
| BNF | WAIT | 0 | 0 | **+0** | 0 | — | 0/0 | +0 |
| NF | **DIRECTION_ONLY** | 87 | 67 | **+51,586** | 1,137,110 | 4.54% | 2/1 | **+12,232** |
| NF | DIRECTION_PLUS_BOTH | 43 | 35 | +45,374 | 597,382 | **7.60%** | 2/1 | +10,129 |
| NF | WAIT | 0 | 0 | +0 | 0 | — | 0/0 | +0 |

**Every direction arm stays positive under every leave-one-out**, on both indices — unlike the
premium arms, which all flip. On NIFTY the two winning sessions are near-equal (+39,354 and
+38,540), so no single day carries it. That is the strongest-looking number this work has
produced, which is why I attacked it three ways.

### 3.1 · The gain is call-side only, in all eight arm/index cells

| idx | arm | calls n | calls ₹ | puts n | puts ₹ |
| --- | --- | ---: | ---: | ---: | ---: |
| BNF | DIRECTION_ONLY | 60 | **+349,908** | 10 | **−31,164** |
| NF | DIRECTION_ONLY | 44 | **+77,894** | 23 | **−26,308** |
| BNF | DIRECTION_PLUS_BOTH | 25 | +103,414 | 2 | −5,655 |
| NF | DIRECTION_PLUS_BOTH | 24 | +49,531 | 11 | −4,157 |

The `BEAR` calls are the only test of whether the signal works in both directions, and they cost
money on both indices. **The one session the signal called BEAR, it lost.** In a sample with two
large up days and one down day, that is the confound — the same trap my previous §3.1 identified
in the forecast filter, now reproduced on the app's own production signal. A test asserts this
holds in every cell and fails if it ever stops, so the memo gets rewritten rather than the test
relaxed.

### 3.2 · Return on risk rises monotonically with the hold

| hold | BNF | NF |
| --- | ---: | ---: |
| 15 min | 1.86% | 0.49% |
| 30 min | 4.48% | 1.82% |
| **60 min** | **17.04%** | **4.54%** |
| 120 min | **48.84%** | 13.43% |

A timing edge shows at short horizons and decays. This does the opposite, which is the signature
of a long option held through drift. A test asserts the monotonicity so a future sample breaking
it is noticed.

### 3.3 · The hole again, and the two indices differ

| idx | orders | scored | unresolved | total ₹ | needs each unresolved to average | worst scored loss | survives? |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| BNF | 93 | 70 | 22 | +318,743 | **−14,488** | −5,293 | **yes**, 2.7× worse than anything observed |
| NF | 87 | 67 | 20 | +51,586 | **−2,579** | −5,210 | **no**, inside the observed loss range |

Concentration it passes: removing the best five of seventy leaves two thirds of the gain, win
rates 80.0% and 71.6%.

### 3.4 · The premium model does not add — and what it does instead

| idx | net vs baseline | **risk vs baseline** | orders vs baseline | return on risk |
| --- | ---: | ---: | ---: | ---: |
| BNF | 31% | **0.39×** | 0.39× | 13.43% vs 17.04% |
| NF | 88% | **0.53×** | 0.49× | **7.60% vs 4.54%** |

It **fails the predeclared 10% matched-risk condition outright**. What it does is halve the
position and keep most of the net, which on NIFTY is a 67% improvement in return on risk. The
predeclaration named this in advance as **size reduction, not selection**. The ablation separates
the mechanisms: **declining does nearly all of it** (BNF 93→34 orders), while **strike selection
does almost nothing** (−2.1% and −3.4% of net, every order kept).

**Review ask R3:** the predeclared primary was `DIRECTION_PLUS_BOTH` vs `DIRECTION_ONLY` on net
rupees at matched risk, and on that test the premium model fails. Do you accept the narrower
"size governor" reading as a *separate* claim worth its own test, or should it be dropped
because reading it at all is post-hoc? I have not pursued it; the fresh-session lock carries F2
with **return on risk** as its criterion, which is my attempt to make that claim testable rather
than rhetorical.

---

## 4 · Two app defects, both with research consequences

### 4.1 · `BROKER_SNAPSHOT_UNAVAILABLE` blocked the Paper rule 192 times in four sessions

The trail records the production rule's own intent per poll at `why.sl`, and **its direction was
right on every directional session**:

| session | idx | recorded intent | blocked by |
| --- | --- | --- | --- |
| 18 Nov | NF | `SELL_CE` 22, `BUY_PE` 5 | `BROKER_SNAPSHOT_UNAVAILABLE` 27, `ENTRY_WINDOW_CLOSED` 4 |
| 19 Nov | BNF | `SELL_PE` 20, `BUY_CE` 18 | `BROKER_SNAPSHOT_UNAVAILABLE` 38, `ENTRY_WINDOW_CLOSED` 3 |
| 20 Nov | BNF | **`BUY_CE` 47, nothing else** | `BROKER_SNAPSHOT_UNAVAILABLE` **47**, `ENTRY_WINDOW_CLOSED` 3 |
| 20 Nov | NF | **`BUY_CE` 30** | `BROKER_SNAPSHOT_UNAVAILABLE` 30, `ENTRY_WINDOW_CLOSED` 3 |

`BUY_CE` on the 20th (+2,007 BNF points observed), `SELL_CE`/`BUY_PE` on the 18th (−159). **It
was not declining to trade; it had a plan and could not price it,** on every actionable poll of
three sessions. Separately `why.ml` reports `NO_ADMISSIBLE_CANDIDATE` on every poll it ran, with
`FRESHNESS_UNVERIFIED:EXCHANGE_TIMESTAMP_MISSING`.

### 4.2 · The rule's own decisions cannot be evaluated from the capture — 0 of 233

This was meant to be rank 1 of the research map: price the rule's `BUY_CE`/`BUY_PE` intents,
which are **debit** legs and need no hedged margin, so unlike its credit legs they looked
scorable. `paper_intent_feasibility.py` was the feasibility check that had to run first.

| session | idx | debit intents | **priceable** | credit | priceable | pinned ladder | offset of the rule's strike |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 18 Nov | BNF | 2 | **0** | 14 | 0 | 62200/62300/62400 | −200 … −400 |
| 18 Nov | NF | 5 | **0** | 27 | 0 | 25300/25350/25400 | −50, −100 |
| 19 Nov | BNF | 25 | **0** | 20 | 0 | 62000/62100/62200 | +200 … **+900** |
| 19 Nov | NF | 14 | **0** | 32 | 0 | 25150/25200/25250 | +50 … +300 |
| 20 Nov | BNF | **55** | **0** | 0 | 0 | 62700/62800/62900 | +200 … **+1,700** |
| 20 Nov | NF | 38 | **0** | 0 | 0 | 25400/25450/25500 | +50 … +300 |
| **total** | | **140** | **0 (0.0%)** | **93** | **0** | | median **400**, worst **1,700** |

**The mechanism is structural.** `premium_capture_cohort_v2_three_paired_strikes_validated` pins
three strikes nearest the spot at the **09:20** selection slot and holds them all session; the
rule picks a strike near the **live** spot at each poll. On 20 November, when BANKNIFTY rose
2,007 points, the rule was asking about strikes up to 1,700 points off a ladder fixed at the
open. **The capture is least able to evidence the rule exactly when its decisions matter most.**

**I did not substitute the nearest pinned strike** — that prices a different trade and attributes
it to the rule. The probe records the offsets and refuses. A guard constructs a case that *must*
match and checks it does, so "infeasible" is not an artefact of an over-strict matcher.

**Review ask R4:** this is the finding I most want checked, because it invalidates a plan you and
I both ranked first. Is the refusal right, or is there a defensible way to evaluate the rule's
intent from a ladder it does not name? I could not find one that does not amount to scoring a
different trade.

---

## 5 · The research map is ranked by a field probe, not by preference

`field_inventory.py` counted every field over **4,781 observations and 694 decision polls** before
I wrote a word of the map, because the owner's instruction was to verify fields before relying on
them. The answer removed most of the choices.

| state | fields |
| --- | --- |
| `UNUSABLE_ABSENT` | `volume`, `oi`, `oi_change`, `exchange_timestamp`, `vendor_timestamp`, `greeks.delta/theta/vega` (all **4,781**); session `margin`, `margin_evidence` (all **14** nodes) |
| `UNUSABLE_ALWAYS_NULL` | `iv.market_data` (null on **4,769 of 4,769**), `why.ml.selected` |
| `USABLE` | `outcome`, `identity.verified`, `direction.value`, `regime`, `spot.value`, `spot.asof`, `why.act`, `why.dte`, `why.expiry` |
| `PARTIAL` | `bbo.*` 4,769; `iv.greeks` 4,769 but `unit_status: UNKNOWN` and `authoritative_resolved: false`; `direction.strength` **233** (null on NEUTRAL, so its row count *is* the actionable-poll count) |

Two cross-checks that make me trust it: the **12** records missing every quote field are exactly
six pinned contracts × two indices on one poll — the 13:25 failure on 20 November from §6 of the
span memo — and the two session/index nodes missing `cohort.*` are the 16/17 November
`authority_doc: null` pair.

**A defect in my own probe, found by writing its guard and fixed before it reached the map:** the
verdict logic called a field `PARTIAL` whenever its empty rows mixed null and absent, **even with
zero present rows**. That is exactly `iv.market_data`, and "partial" would have invited an
experiment on a field that never once carried a value.

Resulting ranks: **1** Paper-rule debit intents (now §4.2, refused) · **2** does the BEAR side
ever work (needs sessions, not code) · **3** spread and size as an execution filter (ran, §6) ·
**4** relative IV as a *change* only · **5** confirmation/hysteresis, **deliberately held back** ·
**6** holding window and overnight (window constant) · **7** contract and expiry (three strikes,
one expiry) · **8** credit structures (margin absent).

---

## 6 · Rank 3 ran: a clean negative with a mechanism

`execution_filter.py` — a **diagnostic on trades the causal replay already scored**, no new arm
and no new decision rule, so there is nothing here that could be fitted into a strategy. 3,815
scored of 4,769, quintiles of the sample's own distribution.

| scope | feature | q1 | q2 | q3 | q4 | q5 | ordered? |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| BNF | relative spread | −51 | +24 | +192 | −69 | **+1,703** | **no** |
| NF | relative spread | −106 | −113 | +94 | +238 | **−182** | **no** |
| BNF | book imbalance | +505 | +115 | +535 | +205 | — | **no** |
| NF | book imbalance | −23 | +24 | −96 | −16 | +38 | **no** |

Nothing is ordered anywhere, and the widest quintile points **opposite ways on the two indices**.
An execution filter cannot help one index and hurt the other by dropping the same quintile — so I
checked what the quintile contains. Mean mid price per bucket:

| idx | q1 | q2 | q3 | q4 | q5 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BNF | 560 | 538 | 514 | 533 | **660** |
| NF | 154 | 156 | 161 | 150 | **69** |

**On NIFTY the widest-spread quintile holds contracts at 0.44× the mid of the rest** — the cheap,
far-from-money ones — and they lost. On BANKNIFTY the widest is the *dearest*, and on a
+2,007-point day it won. **Relative spread is a moneyness proxy here**, and the net follows which
contracts it selected. The counterfactual is stamped
`IN_SAMPLE_COUNTERFACTUAL_NOT_A_RESULT` because its quintile boundary was cut from the rows it is
scored on. Guards construct a monotone effect the machinery must find, so the negative means
something. **Closed**; reopening needs a vendor or exchange timestamp.

`quantity_unit_status` is `UNKNOWN` on all 4,769, so size is used only as a ratio where the unit
cancels. Of the 4,769 opportunities carrying a usable two-sided decision quote — `quote_refusals`
is **empty**, so none was dropped for a missing or one-sided book at the decision — **954 are
unscored**, because the round trip itself had no outcome (unfilled, or no exit quote at the
target instant). That is the same hole as §1 and §3.3, reaching this diagnostic too.

---

## 7 · The fresh-session test is locked

`tools/premium_model_validation/FRESH_SESSION_TEST_LOCK_20261122.md`, committed **before any
fresh session is viewed**.

* **Two finalists only** — `DIRECTION_ONLY` (F1) and `DIRECTION_PLUS_BOTH` (F2), `WAIT` as
  control. Three sessions of development evidence do not support more.
* **Six candidates excluded with a stated reason each**, so none quietly returns: every forecast
  arm (all flip on LOO), `RANGE_EV` alone including the +16,433 exception (§1), all four debit
  verticals (§0.1), strike selection alone (−2.1%/−3.4%), confirmation thresholds (233 actionable
  polls), the Paper rule's intents (§4.2).
* **Nothing can be fitted** — neither finalist has a free parameter; the rules may read
  `direction.value` and nothing else.
* **F1's criterion is the put side specifically**, not the total: PASS needs the put side
  profitable after costs across **≥3 sessions with ≥10 `BEAR` signals each**. A positive total on
  one down day is **NOT PROVEN**.
* **F2's criterion is return on risk, not net rupees**, because it trades less by design.
* **Stopping rules fixed now:** stop at three qualifying sessions; stop-early **FAIL** if the put
  side is worse than −₹50,000 on either index; stop-early **NOT PROVEN** if six qualifying
  sessions yield fewer than three usable `BEAR` sessions; a session failing the integrity bar is
  reported excluded and **not replaced by looking for a better one**; **no new arm, field,
  threshold or horizon mid-test**; repeated attempts controlled by counting and reporting batches.

**Review ask R5:** this is the only moment the lock can change without invalidating the test. If
you want a different finalist, criterion or stopping rule, say so now.

---

## 8 · Capture specification for Grok

`GROK_CAPTURE_INPUT_SPEC_20261122.md` — specification only, nothing implemented. Every
requirement is there because a named experiment was blocked and the absence was counted:

| | requirement | evidence | acceptance test |
| --- | --- | --- | --- |
| **R1** | follow the decision's own strike; stop pinning at 09:20 (keep the pinned six too) | 0 of 233 intents priceable; median 400, worst 1,700 points off | `debit_priceable_share ≥ 0.95` |
| **R2** | extend to 15:30 and add the next session's 09:20 open | 48 real adjacent pairs, every one `no_close_mark_in_window`; the polls already exist at 15:05–15:40 | ≥1 `VALID PAIR`, zero window refusals |
| **R3** | one quote timestamp, verbatim | `exchange_timestamp`/`vendor_timestamp` absent on all 4,781; the shipped candidate path reports `FRESHNESS_UNVERIFIED` every poll | either reads `USABLE` |
| **R4** | declare the quantity unit; carry vendor IV; supply the lot-table metadata | both `UNKNOWN` on 4,769; `iv.market_data` null on 4,769; `lot_resolution: UNRESOLVED_REFERENCE` on every node | — |
| **R5** | flow and greeks **only if already in the response** — no extra API call | absent on all 4,781 | — |
| **R6** | margin evidence, lowest priority | absent on all 14 nodes; the rule's debit legs need none | — |

The missing-data section is framed as a **non-regression**: keep `ABSENT` and `NULL` distinct;
never zero-fill a price, size or IV; never let LTP stand in for a quote in economics; keep
recording a failed poll as a row (the 13:25 failure let the session score 408 of 414); keep a
one-sided book distinct from a wide spread; declare the window in the version string when R2
changes it; never substitute a neighbouring strike for a requested one.

**Priority stated both ways:** R2 first for the fastest visible result, R1 first for the most
valuable one.

---

## 9 · Reproduce

```bash
# the corrected per-leg spread replay
python3 -I tools/premium_model_validation/spread_replay.py <export> <spots> \
    --ledger-root <tree> --out <out>
# the survival stress on the one leave-one-out survivor
python3 -I tools/premium_model_validation/stress_range_ev.py --span <dir> --spots <spots> \
    --ledger-root <tree> --index BNF --arm RANGE_EV_E0_TILT_0 --out <out>
# realised index direction
python3 -I tools/premium_model_validation/index_direction.py <export...> --out <out>
# the direction baseline, primary plus horizon diagnostics
for H in 15 30 60 120; do python3 -I tools/premium_model_validation/direction_baseline.py \
    --span <dir> --spots <spots> --ledger-root <tree> --horizon-min $H --out <out>; done
# the field probe the map is ranked on
python3 -I tools/premium_model_validation/field_inventory.py <export...> --out <out>
# rank 1 feasibility, and rank 3
python3 -I tools/premium_model_validation/paper_intent_feasibility.py <export...> --out <out>
python3 -I tools/premium_model_validation/execution_filter.py --span <dir> --spots <spots> \
    --ledger-root <tree> --out <out>
# suites. SR_* / EC_* / TILT_* unskip the integration guards
cd tools/premium_model_validation && python3 -m unittest discover -s tests   # 240, 0 skipped
cd tools/premium_range_compare   && python3 -m unittest discover -s tests   # 158
```

| file | sha256 |
| --- | --- |
| `spread_replay.py` (per-leg; was `d376c7df…` on the net basis) | `b13d9f5c1f56368e6f9f27b20dbd447cc0bae5eae786f2410f31ee9521ffb7aa` |
| `stress_range_ev.py` | `5b4a977d07f38c0042f1025b07d704b7ab3b8d425cd497df29129227b286bbee` |
| `index_direction.py` | `64e6ed9491b87ae9d7133fed8c1067bf2eea1737789af9054938bb6393abd099` |
| `direction_baseline.py` | `1217ef10d37d9d87c4ba270731a730ea340200a73fed8826118029091ee8421e` |
| `field_inventory.py` | `02c8b5738d45c6036b6270d786c318d3cb553286f381d21c59818beb0ce10b6a` |
| `execution_filter.py` | `60ca3b5597441a8ee5de7b6c68c435f3588f70c1ff4387d562364d59f4780d7f` |
| `paper_intent_feasibility.py` | `b5a859dcdd654156dda522d94e9a38439967d1c9d661e1d3ea63ac218e470b98` |
| `premium_repricing_v2.py` (frozen, unmodified) | `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a` |

Inputs are **not** committed. The seven export digests are in §11 of the span memo.

---

## 10 · What is still open

1. **Two fee extensions await your review** — 9 Oct → 16 Nov (returned with worked cases) and
   16 Nov → 20 Nov. **Every figure in §0.1, §3 and §6 for 18–20 November rests on
   `nse_equity_index_options_rates_20261120_v3_UNREVIEWED`**, held in a worktree, never committed
   to a fee table, and every run stamps `provisional_cost_basis` with the dirty file named. The
   9 Oct – 11 Nov source window is still unchecked at the issuer.
2. **13 November persistence defect** — 138 polls submitted, `ACCEPTED_STARTED`/
   `ACCEPTED_QUEUED_BEHIND_ONE` on every one, nothing persisted. Now with §2's extra evidence:
   the spot trail for that day *did* persist.
3. **`BROKER_SNAPSHOT_UNAVAILABLE` has no owner** (§4.1).
4. **The exchange quote clock is still unverified**, and §5 shows it blocking the production
   candidate path, not just the research.
5. **The overnight window constant** (§8 R2) — proven, unchanged.
6. **Six sessions, three of them active for the direction work, two of those the same
   direction.** I would not build a rule on it.
7. Outside this handoff: CLAUDE.md's body is badly drifted (`BRAIN_VERSION` doc 2.6.40 vs code
   2.6.98), and a stale `compile_errors.txt` sits at the repo root.

---

## 11 · Scope

Offline research and replay on review branches, source data read **read-only**. Every rupee is
`PROXY_SIMULATED_FILL` — no order was placed and nothing here is a Paper position. Retrospective;
**outside** the unsigned prospective v4 count, which still counts only sessions eligible under its
own dated freeze. No raw export, Paper position or credential in either tree. No production write,
database write, `main` merge, build, release or pull request. No `approved_by` or approval
reference was created, and nothing was signed off on Vivek's behalf.

---

## What I am asking you to decide

| | ask |
| --- | --- |
| **R1** | confirm the per-leg fee basis in §0.1 is the ledger's intended contract for a two-leg structure. Four totals move if not. |
| **R2** | confirm the structural/sporadic split in §1 is the right matched correction, or tell me to truncate the whole span's population at 14:00 and re-run. |
| **R3** | accept or reject the "size governor" reading of the premium model (§3.4) as a separate, testable claim rather than a post-hoc rescue. |
| **R4** | check the §4.2 refusal. It invalidates a plan we both ranked first, and I could find no evaluation route that does not score a different trade. |
| **R5** | review the fresh-session lock (§7) now — it is the only moment it can change without invalidating the test. |
| **R6** | the binding constraint on this research line is the capture's fixed 09:20 ladder, not the models (§4.2, §5, §8 R1). If one capture change is made, I would make that one. |

My own summary verdict: **nothing in this round earns prospective Paper research.** The direction
baseline is the first thing to beat `WAIT` and the reason it still fails is §3.1, not the size of
the rupees — they are not small. The two findings I would act on are app defects, not models:
`BROKER_SNAPSHOT_UNAVAILABLE` (§4.1) and the capture ladder (§4.2).
