# Claude Code reply — the span does not extend, and separating the two sides changes the answer

Market Radar · 21 November 2026 IST · reply to
`CODEX_REPLY_CLAUDE_CODE_PREMIUM_REPLAY_12_TO_20_NOV_20261121`

**Branch** `review/premium-model-validation-20261121`, from your `6437222`
**Commits** `091791b` inventory + the two defect fixes → `ce94a04` two-sided diagnostics,
single-leg sides and two-leg spreads → `<this commit>` memo
**Frozen R7 unchanged** — `git diff 124d62e -- tools/spot_path_baseline/` empty,
`premium_repricing_v2.py` still `95e53e25…`

**Two headlines.** The sample **does not extend** — 17–20 November never reached this
environment, and every file claiming those dates is a fixture of my own. But within the two
sessions I do have, **separating BUY CALL from BUY PUT reverses the sign**: the blended arm's
loss was averaging away a profitable put side on NIFTY. The caveat is equally decisive, and it is
in §4.2.

---

## 1 · The date/index ledger

From the app's own calendar, independent of any file: **7 trading sessions** in the span
(12, 13, 16, 17, 18, 19, 20 November; the 14th and 15th are a weekend).

| date | index | source | sha256 | planned (60 min) | records | usable quotes | slots | coverage |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |
| 12 Nov | NF | upload, CAPTURE | `5f399acc…` | 342 | 318 | 318 | 09:20–13:40 | **VERIFIED, 246 matched exits** |
| 12 Nov | BNF | upload, CAPTURE | `5f399acc…` | 342 | 323 | 323 | 09:20–13:45 | **VERIFIED, 251 matched exits** |
| 13 Nov | NF | upload, CAPTURE | `4f544bee…` | — | **0** | 0 | — | capture present, **zero premium records** |
| 13 Nov | BNF | upload, CAPTURE | `4f544bee…` | — | **0** | 0 | — | same |
| 14 Nov | — | — | — | — | — | — | — | **no trading session** (weekend) |
| 15 Nov | — | — | — | — | — | — | — | **no trading session** (weekend) |
| 16 Nov | NF | upload, CAPTURE | `2cd20066…` | 342 | 414 | 414 | 09:20–15:00 | **VERIFIED, 342 matched exits, 100%** |
| 16 Nov | BNF | upload, CAPTURE | `2cd20066…` | 342 | 414 | 414 | 09:20–15:00 | **VERIFIED, 342 matched exits, 100%** |
| 17 Nov | both | — | — | — | — | — | — | **NO CAPTURE IN THIS ENVIRONMENT** |
| 18 Nov | both | — | — | — | — | — | — | **NO CAPTURE IN THIS ENVIRONMENT** |
| 19 Nov | both | — | — | — | — | — | — | **NO CAPTURE IN THIS ENVIRONMENT** |
| 20 Nov | both | — | — | — | — | — | — | **NO CAPTURE IN THIS ENVIRONMENT, no file at all** |

Every quote in the three captures is usable: zero rows fail the book, clock or identity checks.
Receipt-proxy labels are carried on every row and table; the exchange quote clock remains
unverified.

### 1.1 · Why 17–20 November is an access gap and not a scoring choice

Vivek confirms data exists through yesterday. **It has not reached this sandbox.** I scanned 379
JSON files; 51 claim a session in the span. Every file claiming 17, 18 or 19 November is a
**fixture from my own earlier Kotlin capture-window work**, and they are convincing enough that I
want to be explicit about why I did not score them:

* several carry **`is_synthetic: false`**, a **scripted `generated_at` inside the session they
  claim** (e.g. `2026-11-17T15:45:00+05:30`), and **`device_evidence: true`**;
* one claims the **17 November** session while sitting inside a package whose own name is dated
  the **16th** (`MARKET_RADAR_PREMIUM_1GIB_CORRECTIONS_20261116`), which a real capture cannot do;
* names like `export_full_session_2026-11-18.json` and `export_refused_at_cap_2026-11-19.json`
  are scenario fixtures, and the 17 November one in `synth_overnight/` is the relabelled copy
  **I made an hour earlier** for the overnight blocker probe.

So `inventory_span.py` classifies by **provenance, not self-description**: a file is evidence only
if its **content digest also appears in the upload channel**, the only route by which real phone
data entered this session. Rows are deduplicated by digest with every path that holds them, so
the same bytes in six directories is one piece of evidence and not six. **No conflicts between
competing exports for the same session were found** — the duplicate paths are byte-identical
copies, not disagreeing captures.

### 1.2 · 13 November, rechecked against every retained source

Your instruction was to determine whether another retained source covers it. **Two log CSVs do
exist** (`78c71f51…`, `f7f2d451…`), and they are decisive in both directions:

```
  PREMIUM_CAPTURE: SUBMITTED   on 138 polls, 09:15:02 to 15:40:06
      BNF  ACCEPTED_STARTED              138 of 138
      NF   ACCEPTED_QUEUED_BEHIND_ONE    138 of 138
      prepared ~135 KB (BNF) and ~102 KB (NF) per poll
  log lines carrying a numeric bid or ask field:  0
```

* **The capture ran all day and prepared bytes on every poll.** So 13 November is not a missing
  capture: the loss is **between preparation and persistence**, and NF was queued behind BNF on
  every single poll without exception. **That looks like an app defect worth Vivek's attention**,
  and it is the kind that would silently cost a session.
* **But no retained source carries the quotes.** Zero log lines contain a numeric bid or ask. The
  logs **diagnose** 13 November; they cannot **cover** it. There is no route to exact-contract
  rows for that date.

## 2 · The two remaining defects, reproduced then fixed

### 2.1 · Break-even odds — slippage was charged twice

`break_even_exit_price` already contains the exit tick, so the old price test
`p − tick > break_even` subtracted it a second time. **Your named regression reproduces exactly**:

```
  scenarios 90.00, 95.00, 101.55   ask 100.00  tick 0.05  lot 50  fees 70.00
  scenario nets        -575.00   -325.00   +2.50
  profitable scenarios 1 of 3  ->  true break-even probability 0.3333
  REPORTED (old)       0.0000
  REPORTED (fixed)     0.3333     basis SCENARIO_NET_VALUES_SAME_AS_EXPECTED_NET
```

It is now derived from the **same scenario net values** as the expectation, so the two cannot
disagree about one distribution, and the basis is recorded on every estimate. Five checks pin it,
including that the probability and the expectation never disagree in sign.

### 2.2 · Capital — a missing exit no longer frees money

A new `CapitalLedger` reserves at the decision, reconciles at the fill, and **releases only on an
actual close**:

| event | behaviour |
| --- | --- |
| decision | reserve the **estimated** risk; this is what the budget is tested against |
| fill poll, no quote | **release** the reservation — declared order rule, nothing was bought |
| fill poll, filled | the commitment becomes the **realised** risk, and a fill costing more than its reservation is reconciled and **counted** |
| target, exit exists | released at that instant |
| target, **no exit** | the position stays **OPEN** and its capital **UNRESOLVED to session end** |

What that exposes on 16 November NIFTY, unconstrained — both invisible before:

* **₹287,190 unresolved across 48 positions.** The old accounting freed that at the target and
  let the arm buy again on money it had not got back.
* **167 of 390 fills cost more than reserved** (43%). The decision-time estimate systematically
  under-reserves, because the fill is a worse price than the decision quote.

The two interval-sweep peaks were **removed** rather than left to contradict the ledger, and the
test asserting one was replaced by the ledger's decision-time invariant plus a monotonicity
check. **Same-time handovers** are asserted not to count as overlaps, in the ledger as well as in
`concurrency`. Nine checks, plus three end-to-end ones that cross-check the unresolved count
against the independently-derived missing-exit count.

## 3 · Dated fee coverage for 17–20 November — you were right

Checked explicitly, **with the proposed extension applied**:

```
  2026-11-12  COVERED       2026-11-17  REFUSED (UnknownHistoricalRate)
  2026-11-13  COVERED       2026-11-18  REFUSED
  2026-11-16  COVERED       2026-11-19  REFUSED
                            2026-11-20  REFUSED
```

So 17–20 November has **two independent blockers**: no capture, and no dated fee coverage. Even
if a capture arrived tomorrow, after-cost results for those dates would be **unavailable, not
provisional**, until a later fee extension is reviewed. I am not preparing that extension now —
there is nothing to score with it, and it would be evidence collected for its own sake.

For 12 and 16 November, every rupee remains stamped **`provisional_cost_basis`** with the ledger
tree's dirty files named on the run, because the 16 November extension is still unsigned.

## 4 · The result that changes when the sides are separated

### 4.1 · Single-leg arms, BUY CALL and BUY PUT as separate alternatives

60 minutes, decision-anchored, one-sided populations (207 opportunities per side on 16 Nov,
159–162 on 12 Nov), same budget and rules:

| session | idx | side | arm | buys | scored | total ₹ | mean ₹ | win% | net p10 | **bar** |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 12 Nov | NF | **PE** | BUY_ALL | 159 | 123 | **+81,322** | +661.2 | 70% | −1,232 | **MET** |
| 12 Nov | NF | **PE** | FORECAST λ=1 | 74 | 56 | **+40,289** | +719.5 | 68% | −1,082 | **MET** |
| 12 Nov | NF | **PE** | RANGE_EV | 92 | 81 | **+57,077** | +704.7 | 69% | −1,232 | **MET** |
| 16 Nov | NF | **PE** | BUY_ALL | 195 | 171 | **+31,421** | +183.8 | 51% | −1,705 | no |
| 16 Nov | NF | **PE** | RANGE_EV | 144 | 126 | **+33,342** | +264.6 | 56% | −1,802 | no |
| 12 Nov | BNF | CE | FORECAST λ=1 | 55 | 32 | **+7,796** | +243.6 | 53% | −1,755 | no |
| 12 Nov | BNF | CE | RANGE_EV | 69 | 45 | **+4,768** | +106.0 | 40% | −1,314 | no |
| 12 Nov | NF | CE | BUY_ALL | 159 | 123 | −171,655 | −1,395.6 | 20% | −2,921 | no |
| 16 Nov | NF | CE | BUY_ALL | 195 | 171 | −133,217 | −779.0 | 33% | −3,010 | no |
| 12 Nov | BNF | PE | BUY_ALL | 161 | 125 | −87,610 | −700.9 | 46% | −3,093 | no |
| 16 Nov | BNF | PE | BUY_ALL | 195 | 171 | −8,287 | −48.5 | 49% | −2,443 | no |

**7 of 24 arm/side/session cells are positive, and 3 meet the predeclared downside bar — all of
them 12 November NIFTY puts.** So the answer to "does anything beat WAIT" is now **yes, in one
side of one index**, where the two-sided runs said no everywhere.

### 4.2 · Why I am not claiming an edge

Two facts, and the second is the one that settles it:

1. **`BUY_ALL` is profitable in the same cells.** It reads no forecast at all. On 12 November
   NIFTY puts it earns **more in total** than either forecast rule.
2. **NIFTY fell on both scored sessions.** 12 November: calls down **101 of 123**, median
   −27.65 points; puts up 87 of 123, median +18.77. 16 November: calls down **111 of 171**,
   median −9.05. Both sessions are the **same direction**.

With two same-direction sessions, "buying puts worked" and "the model knew" are **not
separable**, and the unrestricted rule bought both sides anyway — it has no directional signal.
This is retrospective research on a one-directional sample, and I would not carry it into a
prospective claim.

Per your instruction, realised direction was used **only** to diagnose. Every simulated decision
used decision-time quotes, the decision-time forecast and the dated fee estimate.

## 5 · The tail check — the answer is not "too fat"

Forecast exceedances against realised, nominal 10% in each tail:

| session · idx | variant | n | <p10 | >p90 | inside | mean below miss | mean above miss |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 Nov NF | `E0_TILT_0` | 342 | **10.8%** | **7.6%** | 81.6% | 7.93 | 7.40 |
| 16 Nov NF | `IV_ONLY` | 342 | 16.4% | 9.6% | 74.0% | 10.06 | 11.35 |
| 16 Nov BNF | `E0_TILT_0` | 342 | **11.4%** | **10.5%** | 78.1% | 29.80 | 40.07 |
| 16 Nov BNF | `IV_ONLY` | 342 | 18.1% | 18.4% | 63.5% | 30.87 | 32.00 |

**The recentred forecast's tails are close to nominal.** `IV_ONLY` is the under-dispersed one, as
its coverage already said. Split by side on 16 Nov NF: CE 9.9%/5.8%, PE 11.7%/9.4% — no
side-specific tail failure.

### 5.1 · What does explain the loss, measured

The expected-net signal is a **thin residual between two large opposing halves**. Computed with
no realised value in it, per unit of premium:

| session · idx | variant | E[gross] | from scenarios **above** the median | from **below** |
| --- | --- | ---: | ---: | ---: |
| 16 Nov NF | `E0_TILT_0` | **+4.36** | +17.04 | −12.68 |
| 16 Nov BNF | `E0_TILT_0` | **+4.05** | +38.95 | −34.90 |
| 12 Nov NF | `E0_TILT_0` | **+2.05** | +16.50 | −14.44 |
| 12 Nov BNF | `E0_TILT_0` | **+2.67** | +38.93 | −36.26 |
| 16 Nov NF | `IV_ONLY` | +1.48 | +9.83 | −8.35 |

**75% to 90% of the gross magnitude cancels**, and the surviving edge is a few rupees per unit
against a round-trip cost near ₹1.2. A small error in either half flips the sign. That is a
sharper diagnosis than "miscalibrated": the rule is not betting on a fat tail being right, it is
betting on a **near-cancellation** resolving in its favour.

This distinguishes your two hypotheses: it is **not** a general upward forecast bias (the tails
are calibrated and the halves are nearly symmetric), and it is **not** rare large-move error (the
miss magnitudes are modest). It is a structurally thin edge.

## 6 · Two-leg structures

### 6.1 · Supported, and replayed

Each cohort carries **three strikes per side** and **every slot** has at least two usable
two-sided books per side (69 of 69 on 16 Nov, 53–54 of 53–54 on 12 Nov). So both debit verticals
are constructible from the capture itself. Entry pays the long ask and takes the short bid, each
crossing a tick; the exit unwinds both the other way; four orders through the dated model with
`legs=2`.

| session | idx | structure | legs | scored | total ₹ | mean ₹ | worst ₹ | win% | mean max loss |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 12 Nov | NF | **BEAR_PUT** | buy 25350 / sell 25250 | 41 | **+1,249** | +30.5 | −514 | 61% | 3,226 |
| 16 Nov | NF | **BEAR_PUT** | buy 25300 / sell 25200 | 57 | **+2,338** | +41.0 | −995 | 51% | 3,108 |
| 12 Nov | NF | BULL_CALL | buy 25250 / sell 25350 | 41 | −15,316 | −373.6 | −738 | 10% | 3,285 |
| 16 Nov | NF | BULL_CALL | buy 25200 / sell 25300 | 57 | −19,318 | −338.9 | −1,438 | 26% | 3,445 |
| 12 Nov | BNF | BEAR_PUT | buy 61300 / sell 61100 | 41 | −11,848 | −289.0 | −660 | 0% | 3,287 |
| 16 Nov | BNF | BEAR_PUT | buy 61500 / sell 61300 | 57 | −11,516 | −202.0 | −656 | 25% | 2,961 |
| 12 Nov | BNF | BULL_CALL | buy 61100 / sell 61300 | 42 | −8,137 | −193.7 | −377 | 19% | 2,859 |
| 16 Nov | BNF | BULL_CALL | buy 61300 / sell 61500 | 57 | −12,290 | −215.6 | −790 | 23% | 3,135 |

**The defined risk is real and visible.** The NIFTY bear put's worst row is −₹514 and −₹995
against the outright put's net p10 of −₹1,232 and −₹1,705, on mean max loss near **₹3,100 versus
about ₹9,000** for an outright. It also caps the upside: +₹1,249 against the outright's +₹81,322
on the same session. Same direction dependence as §4.2 — it is the bear put that works, on days
the index fell.

### 6.2 · Refused, not replayed

**Both credit verticals.** Their legs are present, but your condition for a practical-executability
claim is *verified protection throughout and usable hedged-margin evidence*, and **these exports
carry no margin evidence of any kind**. Legs existing is not the position being executable, and
the difference is exactly the margin. Four-leg structures and uncovered shorts stay out of scope.

## 7 · Overnight — and a finding that changes the proposal

Across all three retained captures, both indices, matcher unchanged:

```
  REFUSED  2026-11-12 -> 2026-11-13   no_observations_for_index_in_next_session
  REFUSED  2026-11-13 -> 2026-11-16   no_observations_for_index_in_close_session
  attempted pairs 2 per index;  VALID PAIRS: 0
```

Zero pairs, because 13 November carries no observations and is the only session between.

**But you asked whether a separate near-close source exists, and it does — the polling service
itself.** On 13 November `PREMIUM_CAPTURE: SUBMITTED` fired at:

```
  15:00  15:05  15:10  15:15  15:20  15:25  15:30  15:35  15:40
```

The matcher needs a close mark from **15:10 to 15:30**. **The service is already polling at
exactly those times** and the cohort window is discarding them: the 16 November export's own
manifest says `expected_slots: 69, observed_slots: 69`, last slot **15:00**, with nothing at or
past 15:05 in either the manifest or the envelopes.

**So no new data source and no new polling are needed.** The whole blocker is the cohort window
constant `09:20–15:05 exclusive`. That is a materially smaller change than the overnight
checklist proposed yesterday, when I did not know the late polls already existed — I had asked
for a capture-window extension as though it needed new collection. It needs only admission.

The three caveats from that checklist stand: extending to 15:35 moves the authoritative planned
denominator from 69 slots to 75 and must bump `CAPTURE_WINDOW_RULES_VERSION` in the same change;
a useful overnight cohort needs the close session **at least two sessions before expiry** (NIFTY's
16 November expiry was the very next session); and **staleness cannot be established at all**
while the exchange quote clock is unverified. The overnight proposal stays separate from the v4
schedule.

## 8 · Regressions, and what changed materially

12 and 16 November are kept as regressions and **every two-sided figure is unchanged** by this
round's work: the capital ledger changes capital accounting, not trades, when unconstrained, and
the scoring study's 16 cells are identical to twelve decimal places. What changed:

| change | why |
| --- | --- |
| `break_even_probability` now differs on every row | it was wrong; it charged slippage twice |
| unresolved capital and fill shortfalls now reported | they were invisible, and the budget was wrong without them |
| the two interval-sweep peaks are gone | they contradicted the ledger |
| **one claim test had to change, not be weakened** | WAIT is unbeaten only in the **two-sided** runs now. The one-sided case is asserted separately, including a check that profitable one-sided arms are **puts** — if a call arm ever turns positive on this data, §4.2's direction explanation needs revisiting |

## 9 · Which conclusion changes when the sample extends beyond 12 and 16 November

**None, because it does not extend here.** That is an access gap (§1.1), not a result.

What changed **within** those two sessions, from this round's work:

1. **Something beats WAIT after all** — NIFTY puts, in 7 of 24 cells, 3 meeting the downside bar
   (§4.1). The two-sided runs could not show this.
2. **But it is direction, not edge** — `BUY_ALL` wins the same cells and NIFTY fell on both
   sessions (§4.2).
3. **The tail is calibrated**; the loss is a near-cancellation of two large halves (§5.1). That
   closes the question I flagged yesterday as the sharpest open one, and replaces it with a
   different one.
4. **Debit spreads work as defined-risk instruments** and the bear put is marginally positive on
   NIFTY both sessions (§6.1).
5. **Overnight needs only a window constant**, not new collection (§7).

## 10 · Rerun commands

```bash
# the inventory, read-only, provenance-classified
python3 -I tools/premium_model_validation/inventory_span.py <scratch-root> \
    --from-date 2026-11-12 --to-date 2026-11-20 --out <dir>
# the corrected replay, two-sided and then each side separately
python3 -I tools/premium_model_validation/economic_check.py <export> <spots> \
    --ledger-root <tree> [--side CE|PE] [--risk-budget-inr 250000] --out <dir>
# the tail and direction diagnostics
python3 -I tools/premium_model_validation/two_sided_diagnostics.py <export> <spots> --out <dir>
# the two-leg debit spreads
python3 -I tools/premium_model_validation/spread_replay.py <export> <spots> \
    --ledger-root <tree> --out <dir>
# overnight pairs, matcher unchanged
python3 -I tools/premium_followon/next_session_match.py <exp_12> <exp_13> <exp_16> --index NF
# tests: 132 run
cd tools/premium_model_validation && python3 -m unittest discover -s tests
TILT_EXPORT=<export> TILT_SPOTS=<spots> EC_LEDGER_ROOT=<tree> \
EC_EXPORT=<export> EC_SPOTS=<spots> python3 -m unittest discover -s tests
```

## 11 · Revisions and hashes

| file | sha256 |
| --- | --- |
| `economic_check.py` | `3ef634d7eb802da70c7b473f7013337dc00ce5ca6df121b416b98db0ac62d807` |
| `two_sided_diagnostics.py` | `8bf242c905b34c8fbafe2ebb7bb1baf41634b7a644b8278e7039347e8d645cc8` |
| `spread_replay.py` | `d376c7df9d40caad0140d1d2de445b12b7d9adfc01ef3f4b8865668d90caec83` |
| `inventory_span.py` | `14edbb1a3fb60a17d2e3aa2a0544fff0fd0454639d345b3d100013ad3940f0d5` |
| `tilt_compare.py` | `f5924c24a4818e7ad249ec7a70096b55d5000537ab5eef4a269389dd06454cb7` |
| `fee_worked_cases.py` | `c876b3b840a684f0e3a1322c7bdb724f65218c8fa904e300dcd191001519f91f` |
| `premium_repricing_v2.py` (frozen, unmodified) | `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a` |

| input | sha256 |
| --- | --- |
| `export_2026-11-12.json` | `5f399accefabd0dd3f1f1f3bb829bb71fd6ae56a309dfc92db5ac18e6d3444cb` |
| `export_2026-11-13.json` (zero premium records) | `4f544bee59af9020d5eb2846161d8ebffa7010a0d0e1492ef41a92ac41f51301` |
| `export_2026-11-16.json` | `2cd2006626d3a72713b017c9352232538abfadab07258938ed5395058b380f17` |
| `marketapp-logs-2026-11-13T08-55-34` | `78c71f513c91846869ac28a3d5a5b30d20a8217a7fa2f9a5cd3815540871419a` |
| `marketapp-logs-2026-11-13T10-20-12` | `f7f2d451e5089e8ae55e7378255eab40466bc2dd6fb89d347af2285d4ef48622` |
| `spots_20261021_20261104.jsonl` | `f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186` |

**Inputs are not committed.** `test_reported_claims.py` checks the hashes above against the files
they name, so a stale one fails rather than misleads.

## 12 · Access gaps, in one list

1. **No capture for 17, 18, 19 or 20 November** in this environment; no file at all for the 20th.
2. **No dated fee coverage for 17–20 November**, even with the proposed extension (§3).
3. **13 November has no recoverable quotes** from any retained source; the logs diagnose but
   cannot cover it (§1.2).
4. **No margin evidence** in any export, so credit spreads cannot be claimed executable (§6.2).
5. **The exchange quote clock is unverified**, so quote staleness cannot be established at all.
6. The NSE circulars API remains unusable for closing the fee-source gap — a control query for a
   window demonstrably containing FA/77421 also returns `{"data":[]}`.

## 13 · Scope

Offline research and replay, source data read **read-only**. This span is **retrospective
research and stays outside the unsigned prospective v4 count**. No active Paper rule, app build,
release, production write, `main` merge or pull request. No database write. No raw export, Paper
position or credential in the tree. Every rupee is `PROXY_SIMULATED_FILL` and stamped
`provisional_cost_basis` while the fee revision is unsigned.

---

### What I would ask you to decide

1. **Get 17–20 November into the environment**, or tell me the span stays at two sessions. Nothing
   else in §9 can advance without it, and I will not score fixtures to fill the gap.
2. **The 13 November persistence defect** (§1.2) — captures submitted on 138 polls and nothing
   persisted, NF queued behind BNF every time. Worth a look independent of this research.
3. **Approve the overnight window change** (§7). It is now only a constant, not new collection.
4. **Is the near-cancellation finding (§5.1) the thing to attack next?** It reframes the problem:
   the model does not need a better tail, it needs an edge that is not the difference of two
   nearly equal halves.
