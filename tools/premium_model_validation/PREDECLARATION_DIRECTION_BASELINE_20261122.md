# Predeclaration — the direction-only baseline and its premium-model ablation

Market Radar · 22 November 2026 IST · research owner's edge-research handoff, section 1

**This file is committed ALONE, before any of the code that scores it.** A test checks that
ordering against git history rather than taking my word for it. Nothing below was chosen after
seeing a result; where I had a choice I say which way I resolved it and why, so that a later
change is visible as a change.

The handoff asks for a frozen direction-only baseline "using a reproducible signal available at
the time, a simple exact-contract selection rule and a defined exit", compared against "the same
signal plus the premium model choosing a contract or declining a trade", with opportunity,
capital, risk, timing and cost assumptions matched and the 60-minute primary retained.

---

## 1 · The signal

**`parts.authority_doc.history[index][poll].direction.value`** — the app's own
`index_direction_v1`, as it was published on that poll, taking the values `BULL`, `BEAR`,
`NEUTRAL` or `UNAVAILABLE`.

This is the signal and not a reconstruction of one. It is chosen for three reasons:

1. **It is the app's production signal**, so a result about it is a result about Market Radar
   rather than about a model I invented for this exercise.
2. **It is reproducible from the export alone**, with no refit and no parameter of mine.
3. **It is causal by construction, and the export proves it rather than asserting it.** Each
   entry carries `decision_poll_ts`, `computed_at` and `receipt_ts` at the poll, and
   `lineage.open_ref_ts` and `lineage.anchor_30_ts` name the *earlier* instants its `z_open` and
   `z_30` are measured against. A test asserts both references are at or before the poll.

**No tuning.** `direction.strength`, `direction.er`, `z_open`, `z_30` and `regime` all exist and
are all tempting. The baseline uses **only `direction.value`**. The continuous fields are
recorded on every row as evidence and are **not** read by any arm declared here; proposing a
threshold on them is a separate experiment that needs its own predeclaration.

## 2 · The contract rule

The capture pins **three strikes per index**, each with a call and a put
(`cohort.pinned_contracts`), so the whole contract universe is six instruments per index. The
rule is therefore as simple as it can be:

* `BULL` → **buy the call** at the strike nearest the cohort's `selection_spot`.
* `BEAR` → **buy the put** at the strike nearest the cohort's `selection_spot`.
* `NEUTRAL` or `UNAVAILABLE` → **WAIT**. No position, no risk, no rupees.

Nearest-the-money is fixed in advance because it is the only choice that needs no judgement.
Choosing a different strike is exactly what the premium model is given the chance to do in §4,
so it must not also be the baseline's choice.

## 3 · The exit, the fill and the costs

Unchanged from the causal replay, so the two are comparable:

* **Decision** at the poll. The signal read is the one published at that poll.
* **Fill one poll later** (5 minutes) at the ask, plus one declared tick.
* **Exit at the decision instant plus 60 minutes**, at the bid, less one declared tick. This is
  the decision-anchored horizon: the instant scored is the instant the forecast targeted.
* **60 minutes is the primary**, for continuity with every previous round.
* **Costs** from the governed dated ledger only. No arm re-implements a fee.
* **Capital** through the same `CapitalLedger`: reserved at the decision, released unfilled at
  the fill poll, and a position with no exit quote stays **UNRESOLVED to session end** rather
  than being quietly released.

## 4 · The arms

Five, on one population, so any difference is attributable to one named choice:

| arm | side chosen by | strike chosen by | may decline? |
| --- | --- | --- | --- |
| `WAIT` | — | — | always |
| `DIRECTION_ONLY` | the signal | nearest the money, fixed | no |
| `DIRECTION_PLUS_DECLINE` | the signal | nearest the money, fixed | **yes** — premium model |
| `DIRECTION_PLUS_SELECT` | the signal | **the premium model**, over the three pinned strikes | no |
| `DIRECTION_PLUS_BOTH` | the signal | the premium model | yes |

**Where the premium model acts, it acts through the already-predeclared `RANGE_EV` rule** —
expected net over the whole forecast distribution, after estimated costs, zero free parameters,
as committed in `PREDECLARATION_RANGE_AWARE_RULE_20261121.md`. To decline is `expected net ≤ 0`.
To select is `argmax` of the same expected net across the three strikes on the signalled side,
with `candidate_id` as the deterministic tie-break. It reads the decision view only, and the
decision view cannot see the exit.

**The primary comparison is `DIRECTION_PLUS_BOTH` against `DIRECTION_ONLY`**, by total net rupees
over the matched population, with `WAIT` as the thing both must beat. The two single ablations
exist to attribute any difference, not to be ranked.

## 5 · What counts as a win, and what counts as nothing

Declared now, so it cannot be relaxed later:

* The premium model **adds** something only if it beats `DIRECTION_ONLY` on total net rupees at
  **matched risk** — total rupees committed within 10% — on the pooled population **and** does
  not lose on a majority of session/index cells.
* Beating `DIRECTION_ONLY` **only by trading less** is not an edge; it is a smaller position. So
  every arm reports rupees committed, orders, mean net per trade and return on risk, and a
  rupee improvement that comes with a proportional fall in both trades and risk is reported as
  **size reduction, not selection**.
* **A tie, a deterioration, or too little evidence is a valid result**, as it has been in every
  round of this work.
* `DIRECTION_ONLY` losing to `WAIT` is itself a finding and is reported as one.

## 6 · The population, and what it excludes before anything is scored

* **Four of the six usable sessions carry the signal: 12, 18, 19 and 20 November.** `16 and 17
  November carry `authority_doc: null`, so they have no signal at all and are excluded from
  every arm alike, including `WAIT`. This is stated now because it is known now, and because it
  halves an already small sample.
* **13 November has the signal and no premium records**, so it cannot be scored and is excluded.
* Every opportunity the replay offers is offered to every arm. An arm that declines records a
  `WAIT`, never a missing row.
* Both indices are reported separately. Calls and puts are reported separately.

So the baseline rests on **four sessions and two indices — eight session/index cells.** That is
small, I expect it to stay small, and I will not describe anything it produces as established.

## 7 · The current Paper rule

The handoff asks to include it "where it can be faithfully reconstructed". It **cannot** be
reconstructed as rupees, and the honest version of that answer is to report what the export
actually records instead of approximating it:

* The trail carries the Paper rule's own intent per poll at `why.sl` — the action (`a`, e.g.
  `SELL_PE`), the strike (`k`), the option type (`ot`) and its state (`st`) — and at `why.ml`
  for the PC2 candidate path.
* It is a **credit** single leg, not a debit buy, so it needs hedged-margin evidence that no
  export carries. **It stays refused for net rupees**, like the credit verticals before it.
* What is reportable, and will be reported, is **coverage and the blocking reason codes**: how
  often the rule had a plan, what it wanted, and what stopped it.

Anything beyond that would be me inventing a Paper execution, which is the one thing this
research must never do.

## 8 · Scope

Offline replay on a review branch, source data read-only, every rupee
`PROXY_SIMULATED_FILL` — no order is placed and none of this is a Paper position. 17–20 November
rests on the **unreviewed** fee revision and every run stamps that. This is retrospective
research and stays **outside** the unsigned prospective v4 count. No raw export, Paper position
or credential enters either tree.
