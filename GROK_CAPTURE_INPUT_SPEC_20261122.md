# Input specification — exact-contract capture for overnight and rule-evaluable research

Market Radar · 22 November 2026 IST · for Grok · **specification only, nothing implemented here**
Supersedes nothing; extends `tools/premium_model_validation/OVERNIGHT_CAPTURE_CHECKLIST_20261121.md`
with what the 12–20 November replay has since proved necessary.

Every requirement below is here because a specific experiment was **blocked by its absence and
the absence was counted**, not because it would be nice to have. The count is given each time so
you can judge the priority yourself.

---

## 0 · Two sentences of context

The current capture (`premium_capture_cohort_v2_three_paired_strikes_validated`, pilot window
`..._0920_1505_exclusive_v1`) records six instruments per index — three strikes nearest the
**09:20** spot, call and put — on a 5-minute grid from 09:20 to 15:00. It is good data: 4,781
observations across seven sessions, hash-verified, with a clean integrity block.

It has two structural limits that no amount of further collection fixes, and they are **R1** and
**R2** below.

---

## R1 — Follow the decision's own strike. Do not pin at the open. **(blocking, highest priority)**

**Why:** the production Paper rule chooses a strike near the *live* spot at each poll. The cohort
pins three strikes at 09:20 and holds them. **Of 233 recorded rule intents across four sessions,
0 could be priced — 0.0%.** Median gap between the rule's chosen strike and the pinned ladder:
**400 points**. Worst: **1,700 points** (20 November BANKNIFTY, a +2,007-point day). Evidence:
`tools/premium_model_validation/paper_intent_feasibility.py`.

The gap grows with the index move, so the capture is least able to evidence the rule exactly when
the rule's decisions matter most.

**Requirement.** At each poll, capture the contracts the decision layer actually named that poll,
in addition to any fixed ladder you keep. Concretely, either:

* **R1a (preferred)** — the capture reads `why.sl.k` / `why.sl.ot` and `why.ml`'s selected legs
  for that poll and records those exact instruments; or
* **R1b** — a rolling ladder of **±5 strikes around the live spot** at each poll, call and put,
  re-centred every poll rather than once at 09:20.

**Keep the 09:20 pinned six as well.** Every result in this research is scored on them and
continuity matters; R1 adds instruments, it does not replace them.

**Acceptance test:** re-run `paper_intent_feasibility.py` on one new session.
`pooled.debit_priceable_share` must be **≥ 0.95**, and `verdict` must read
`PARTIALLY_FEASIBLE` or better. At 0.0% today, anything above zero is progress, but the
experiment needs near-complete coverage to be worth running.

---

## R2 — Extend the window to 15:30, and add the next session's open. **(blocking for overnight)**

**Why:** the matcher needs a close mark in **15:10–15:30**; the capture's last slot is **15:00**.
Proven on **48 real adjacent pairs** (16→17, 17→18, 18→19, 19→20, both indices, six contracts
each) — every one refused for `no_close_mark_in_window`, and no other reason appeared. This is
not a volume problem: no number of extra sessions produces a pair.

**The polls already exist.** The 13 November logs show `PREMIUM_CAPTURE: SUBMITTED` at 15:00,
15:05, 15:10, 15:15, 15:20, 15:25, 15:30, 15:35 and 15:40 — covering the whole close window —
while every manifest declares `expected_slots: 69` ending at 15:00. **The cohort window constant
discards data that was collected.** This is an admission change first and a collection change
second.

**Requirement.**

| | |
| --- | --- |
| close window | capture through **15:30 inclusive**. Declare the new window in the cohort version string and in `expected_slots` so a reader can tell the two eras apart |
| open mark | the **next** session's **09:20** observation for the *same* instrument keys, so a pair has both ends |
| pre-expiry | never pair across the contract's expiry. Carry `expiry` on every record (it is already there) and refuse the pair rather than extrapolating |
| partial close | if a close mark lands at 15:25 but not 15:30, keep it and stamp its slot. A near-close mark with its own timestamp is usable; a silently substituted one is not |

**Acceptance test:** `tools/premium_followon/next_session_match.py` on two adjacent new sessions
must return **at least one VALID PAIR**, with zero `no_close_mark_in_window` refusals.

---

## R3 — A quote timestamp. Any quote timestamp. **(blocking for all freshness work)**

**Why:** `exchange_timestamp` and `vendor_timestamp` are **absent on all 4,781 records**.
Consequences, all current:

* the production candidate path reports `NO_ADMISSIBLE_CANDIDATE` with
  `FRESHNESS_UNVERIFIED:EXCHANGE_TIMESTAMP_MISSING` on **every poll it ran** — it is blocking the
  shipped brain, not just the research;
* `clock_provenance` is
  `DEVICE_LOCAL_ACQUISITION_RECEIPT_EXCHANGE_EVENT_SEMANTICS_UNVERIFIED`, so every rupee in this
  research carries a receipt-proxy caveat;
* staleness cannot be separated from spread, which is why the execution-filter experiment
  (rank 3) is closed as a negative rather than as a result.

**Requirement.** One timestamp per quote, as the vendor sent it, unparsed:

```
exchange_timestamp   the exchange event time, if the vendor supplies it      (preferred)
vendor_timestamp     the vendor's own stamp on the response                  (acceptable)
received_at          the device receipt time                                  (already present)
```

Record whichever of the first two exist, **verbatim and unconverted** — no normalisation to local
time, no rounding to the slot. The existing receipt stamp stays. If the vendor supplies neither,
record `timestamp_source: VENDOR_SUPPLIES_NONE` explicitly so the gap is a stated fact rather
than an inference from a missing key.

**Acceptance test:** `field_inventory.py` on one new session must report
`record:exchange_timestamp` or `record:vendor_timestamp` as `USABLE`, not `UNUSABLE_ABSENT`.

---

## R4 — Declare the quantity unit, and carry the vendor's own IV

**Why, quantities:** `quantity_unit_status` is **`UNKNOWN` on all 4,769** quoted records. Book
size is therefore usable only as a ratio, where the unit cancels. Every depth, liquidity or
capacity question is unanswerable in absolute terms.

**Why, IV:** `iv.market_data` is **present and null on all 4,769** records. The only IV available
is `iv.greeks`, with `iv_source: GREEKS_PRESENT_ONLY`, `iv_unit_status: UNKNOWN` and
`iv_authoritative_resolved: false` on every row. So no absolute or cross-contract IV claim is
permissible — only within-contract relative movement.

**Requirement.**

* Resolve `quantity_unit_status` to `LOTS` or `CONTRACTS` (whichever the vendor means) and stamp
  it. If it cannot be resolved, say which of the two it is *not*.
* Record the vendor's `iv` field when the response carries it, and stamp `iv_unit_status` as
  `PERCENT` or `DECIMAL`. One verification against a single known contract is enough to resolve
  it permanently.
* Same for `references.contract_lot_table`: it currently resolves to `UNRESOLVED_REFERENCE` on
  **every** session/index node, so even lot size is unauthenticated. Supply the table metadata
  the reference names.

---

## R5 — Flow and greeks, if they are free

**Why:** `volume`, `oi`, `oi_change`, `greeks.delta`, `greeks.theta` and `greeks.vega` are
**absent on all 4,781 records**. The whole chain-and-flow research family and every
delta/theta/vega question are dead on this data — not weakly supported, dead.

**Requirement.** If the vendor response already contains them, record them. **Do not add a
separate API call for them** — they rank below R1–R4 and are not worth a new request per poll or
extra latency on the capture path.

---

## R6 — Margin evidence, for credit structures only

**Why:** session `margin` and `margin_evidence` are **absent on all 14** session/index nodes.
Credit verticals and the Paper rule's `SELL_CE`/`SELL_PE` legs have been refused for weeks on
this ground, and the refusal is now counted rather than recalled.

**Requirement.** Per candidate structure: required hedged margin, as the broker states it, with
its own timestamp and a source label. Lowest priority of the six — the rule's **debit** legs need
none of this, which is why R1 matters more.

---

## Missing-data handling — the part that matters most, and the cheapest to get wrong

These rules already hold in the current capture and **must not regress**. Every one of them has
caught a real defect in this research.

1. **`ABSENT` and `NULL` are different and must stay different.** "The key is not in the schema"
   and "the vendor sent nothing" lead to different fixes. The current `{raw, status}` envelope
   does this correctly — keep it. My own field probe had a bug that conflated the two and would
   have invited an experiment on a field that never carried a value.
2. **Never zero-fill a price, a size or an IV.** A missing bid is not a bid of zero. The
   `PRESENT_VALID` / `ABSENT` status on each raw field is the mechanism; use it everywhere.
3. **Never let LTP stand in for a quote in economics.** Display-only use is fine and is already
   labelled `ltp_used_as_quote_fallback`. Executable economics must fail closed with
   `QUOTE_INCOMPLETE` instead.
4. **A failed poll must produce records, not a gap.** The 13:25 poll on 20 November returned
   `MISSING_REQUEST_FAILED` for all six contracts in both indices and recorded `bbo: null` — and
   that was *correct behaviour* that let the session be scored at 408 of 414. (It also crashed
   two readers that dereferenced it unguarded, which is the readers' fault, not the capture's.)
   Keep recording the failure as a row.
5. **A one-sided book is not a wide spread.** Keep `book_status` (`TWO_SIDED` and its
   alternatives) distinct from a quote with a missing side.
6. **Declare the window in the version string.** When R2 changes the window, the cohort version
   and `expected_slots` must change with it, so a reader can tell 69-slot data from 75-slot data
   without inferring it.
7. **Do not substitute a neighbouring strike for a requested one.** If the decision named 62,400
   and the capture holds 62,700, record the miss. A substituted strike prices a different trade
   and attributes it to the rule — which is exactly why R1 is a refusal today and not a
   workaround.

---

## Priority, if only some of this is possible

1. **R1** — follow the decision's strike. Unblocks evaluating the shipped rule, which is
   currently at 0 of 233.
2. **R2** — the 15:30 window. Cheapest of all: the polls already happen and a constant discards
   them. Unblocks every overnight question.
3. **R3** — a quote timestamp. Unblocks the production candidate path, which is reporting
   `FRESHNESS_UNVERIFIED` on every poll today.
4. **R4** — units and vendor IV. Turns one dead research family into a live one.
5. **R5**, **R6** — only if free, or only when credit structures are actually wanted.

**R2 before R1 if you want the fastest visible result; R1 before R2 if you want the most
valuable one.**

---

### Scope

Specification only. Nothing in this document is implemented, no capture code was changed, and no
production write, build, release, `main` merge or pull request is proposed. Every count quoted is
reproducible from the committed probes named above, on exports that stay out of the repository.
