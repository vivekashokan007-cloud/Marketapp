# The locked fresh-session test

Market Radar · 22 November 2026 IST · research owner's edge-research handoff, section 3

**Committed before any fresh session is viewed.** Section 3 asked for exactly this: "Before
viewing fresh outcomes, lock a small finalist set, decision rules, training cutoffs, selection
criteria, opportunity definitions and stopping rules." Everything below is fixed as of this
commit. A test checks that this file predates any result it governs, and a change to it after a
fresh session is scored is a new document with a new date, not an edit to this one.

12–20 November is **development evidence**. Nothing in it may be counted towards the verdicts
below.

---

## 1 · The finalist set — two arms, and that is deliberate

Only two, because three sessions of development evidence do not support more, and because every
extra finalist is another chance to be right by accident.

| finalist | what it is | why it is in |
| --- | --- | --- |
| **F1 · `DIRECTION_ONLY`** | the app's `index_direction_v1` at each poll; `BULL` buys the nearest-the-money pinned call, `BEAR` the nearest-the-money pinned put, `NEUTRAL`/`UNAVAILABLE` waits; filled one poll later, exited at +60 min | the only arm that has beaten `WAIT` and survived every leave-one-out. Its development result is **confounded** — call-side only — and the fresh test exists to resolve that one question |
| **F2 · `DIRECTION_PLUS_BOTH`** | F1, with the predeclared `RANGE_EV` rule choosing the strike among the three pinned and declining when expected net ≤ 0 | the only candidate that improved return on risk (NIFTY 4.54% → 7.60%) while halving capital. It is in as a **size governor**, which is the narrower claim the development evidence actually supports |

**`WAIT` is the control** and is reported with both, at exactly zero rupees and zero risk.

**Explicitly NOT finalists**, and each for a stated reason, so none of them quietly returns:

* every `FORECAST_RULE_*` arm — all flipped sign on leave-one-out;
* `RANGE_EV` alone, including the BANKNIFTY exception at +16,433 — 5% of its book has no exit
  quote and −₹2,152 on those 61 positions erases the whole gain;
* all four debit verticals — all lose on per-leg dated costs;
* strike selection on its own — −2.1% and −3.4% of net, every order kept;
* confirmation and hysteresis on `direction.strength` — 233 actionable polls is not enough to
  tune a threshold on, and doing it now would fit two up days;
* the Paper rule's own intents — **not one of 233 can be priced** from this capture
  (`paper_intent_feasibility.py`); this is a capture change, not an experiment.

## 2 · The decision rules, fixed

No parameter below may move. There is nothing to fit, and that is the point.

| element | fixed value |
| --- | --- |
| signal | `parts.authority_doc.history[index][poll].direction.value`, as published at that poll |
| fields the rules may read | `direction.value` **only**. Not `strength`, `er`, `z_open`, `z_30` or `regime` |
| contract | nearest pinned strike to `cohort.selection_spot` (F1); `argmax` expected net over the three pinned strikes on the signalled side, `candidate_id` as tie-break (F2) |
| decline | F2 only: expected net ≤ 0 under the whole forecast distribution, `E0_TILT_0` |
| fill | one poll (5 min) after the decision, at the ask plus one tick of ₹0.05 |
| exit | decision instant + **60 minutes**, at the bid less one tick. 60 minutes is the primary and the only horizon the verdict may be read from |
| costs | the governed dated ledger, per executed leg. No arm re-implements a fee |
| capital | the same `CapitalLedger`; a filled position with no exit quote stays UNRESOLVED to session end |
| risk budget | none, matching the development runs. If one is imposed it is imposed identically on F1, F2 and `WAIT` |

## 3 · Training cutoff, opportunity definition, and what gets excluded

* **Cutoff.** Any session dated **after the commit that carries this file** is a fresh session.
  There is no model to retrain — neither finalist fits a parameter — so the cutoff is purely
  about which outcomes have been seen. I have seen 12–20 November. I have seen nothing later.
* **Opportunity.** Every decision slot where the signal was published and the index has a pinned
  cohort. An arm that declines records a `WAIT`, never a missing row. Both indices separately,
  calls and puts separately.
* **Excluded before scoring, by rule and not by choice:** a session whose `authority_doc` is
  null has no signal and is excluded from every arm alike, including `WAIT`; a session with no
  premium records cannot be scored; a decision slot whose +60-minute target falls past the last
  exit the capture holds is dropped from every arm alike, because no arm could close it.
* **Chronological only.** Sessions are scored in date order and reported in date order. No
  pooling that lets a later session inform an earlier one; no reordering to improve a curve.

## 4 · What the fresh test must report, per session and index

Not a single pooled number. Every one of these, or the test has not been run:

concentration (total without the best 1, 3 and 5 trades) · loss size (worst, p10, expected
shortfall) · drawdown (worst cumulative run within the session) · coverage (scored / filled /
unresolved, with the unresolved break-even) · turnover (orders, rupees committed, peak concurrent
positions) · missed trades (every `WAIT` with its reason code) · **the call/put side split** ·
sensitivity to a second adverse tick each way, with the decision left unstressed · sensitivity to
the fee basis being the unreviewed revision.

**Overlapping trades are controlled by the capital ledger**, as in development: a 5-minute
cadence and a 60-minute hold leaves up to twelve positions open at once, and peak concurrency
and committed rupees are reported so a bigger book is not read as a better rule.

**Repeated attempts are controlled by counting them.** Both finalists and the control are run
once per fresh batch, and the number of batches run to date is stated in every report. If I run
the test five times I say so.

## 5 · The success criteria, declared now

### F1 — does the direction signal work in both directions?

**This is the one question the fresh test exists to answer.**

* **PASS** requires **the put side to be profitable after costs**, on the fresh sessions, with at
  least **three sessions in which the signal went `BEAR` at least ten times**. Not the total —
  the put side specifically.
* **FAIL** is the put side losing again. The development result then stands as what it looks
  like: long-call exposure to a rising sample.
* **NOT PROVEN** is fewer than three qualifying `BEAR` sessions, however the rupees fall. A
  positive total on one down day is not a pass, and I will say so rather than bank it.

### F2 — is the premium model a size governor worth having?

* **PASS** requires **better return on risk than F1 on the fresh sessions** *and* losing no more
  than half the session/index cells. Net rupees are explicitly **not** the criterion for F2,
  because it trades less by design and the development evidence already showed it loses on net
  while winning on return on risk.
* **FAIL** is worse return on risk than F1.

### Both

* **Either finalist losing to `WAIT` is a FAIL for that finalist**, whatever the other does.
* **A tie, a deterioration or insufficient evidence is a valid result.** It has been the result
  three times in this work already.

## 6 · Stopping rules

Fixed now, so that neither persistence nor impatience is a decision made later:

1. **Stop on three fresh sessions** that each meet the per-session integrity bar below, and
   report. Do not extend the sample because the answer is not the one wanted.
2. **Integrity bar for a session to count:** `authority_doc` present, premium records present,
   ≥ 90% slot coverage, and the capture's own `integrity` block clean. A session that misses it
   is reported as excluded with the reason and is **not** replaced by looking for a better one.
3. **Stop early and report FAIL** if the put side's cumulative net is worse than −₹50,000 across
   the fresh sessions on either index. There is no case for continuing to pay for the answer.
4. **Stop early and report NOT PROVEN** if six fresh sessions pass the integrity bar without
   producing three qualifying `BEAR` sessions. The question then needs a different market, not
   more of this one.
5. **No new arm, field, threshold or horizon enters the fresh test.** Anything new is a
   development idea, goes in the research map, and waits for its own lock. If I think of
   something better halfway through, it still waits.
6. **The fee basis must be reviewed before any fresh verdict is called final.** 17–20 November
   already rests on `nse_equity_index_options_rates_20261120_v3_UNREVIEWED`; a fresh verdict on
   an unreviewed table is provisional and will be labelled so.

## 7 · Scope

Offline replay on a review branch, source data read read-only, every rupee
`PROXY_SIMULATED_FILL` — no order is placed and nothing here is a Paper position. This test is
**prospective** and, unlike the 12–20 November span, its sessions count towards nothing until
they are scored under these rules. It remains separate from the v4 range trial, which counts only
sessions eligible under its own dated freeze. Codex reviews the evidence before any decision to
change recommendations.
