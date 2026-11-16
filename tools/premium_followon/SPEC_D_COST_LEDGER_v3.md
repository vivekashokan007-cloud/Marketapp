# Stage D v3 — the outright-buy cost ledger

**Version** `premium_followon_stage_d_cost_ledger_v3_bound_authority_and_decision_clock` ·
**declared** 16 November 2026 IST · **implementation** `cost_ledger.py` ·
**checks** `tests/test_cost_ledger.py` (57) plus the bundled `--self-check` (14)

Supersedes v2. v2 corrected v1's four wrong answers (§0–§7, unchanged below) and left two of its
own: a fill could be scored with no decision clock, and a mutated lot authority was accepted.
**§8 is the v3 correction.**

## 0 · The four corrections

| v1 | v2 |
| --- | --- |
| a 60-minute target scored with a **09:25 entry and a 14:40 exit**, and a **multi-hour gap accepted as the next entry poll** — v1 counted polls | **every delay is measured on receipt timestamps** against bounds declared before scoring, and each row records the **target, the actual and the difference** |
| an actual Paper net of **−₹8** reported under the **actual** label beside a calculated **+₹2,921.77** | a calculated row is **always** `PROXY_SIMULATED_FILL`; a linked actual outcome sits in its own field and its own summary, and cannot relabel the row or enter the proxy means |
| **0 of 318 NIFTY and 0 of 323 BANKNIFTY** rows scorable for an unresolved lot | v1 read the per-slot producer **claim**, which is `PRODUCER_CLAIM_UNRESOLVED` **by design**. The lot comes from the export's **production lot authority**, which the 12 November export carries resolved for both indices |
| only the first refusal reported, so a lot gap hid a fee gap | **every** blocking reason is collected and counted |

## 1 · Timing, declared before scoring

| | |
| --- | --- |
| fill | the poll after the decision: `ENTRY_DELAY_TARGET_S = 300`, tolerance `± 120 s` on the **receipt** gap |
| hold | **asymmetric**: never early (`HOLD_EARLY_TOLERANCE_S = 0`), at most one poll late (`HOLD_LATE_TOLERANCE_S = 330`) |
| recorded | `timing.entry_delay` and `timing.hold`, each with `target_s`, `actual_s`, `difference_s` and its tolerances |
| refusals | `entry_delay_outside_declared_bound`, `exit_time_outside_declared_bound` |

The bounds are measured, not guessed. On the 12 November receipts the entry gap is
**299.06–301.23 s** across 312 and 317 decisions, and the hold for a 60-minute target is bimodal
at **3600.01–3600.79 s** and **3900.16–3901.14 s** — "the target, or one five-minute poll after
it", because the exit is the first observation at or after the target. A symmetric ±180 s bound
refused 114 of 240 NIFTY rows for being late **by construction**, which is a bound fault rather
than a data fault; the asymmetric bound admits both modes and still refuses the 14:40 exit at
18,900 s and the three-hour fill gap at 10,800 s.

## 2 · Executable prices

Ask in, bid out, never the mid. `EXTRA_SLIPPAGE_TICKS = 1` tick of `TICK_INR = 0.05` adversely on
each side, reported **with and without** so a reader can remove the convention instead of arguing
about it. Each price must be **finite and positive**, must sit on the declared tick grid, and the
exit price must stay **above zero after slippage** — refusals
`entry_price_not_executable`, `exit_price_not_executable`, `price_not_on_declared_tick_grid`,
`exit_price_negative_after_slippage`. Every row carries `tick_evidence` (the tick, both
on-grid checks, and the exit bid after slippage). Real export prices are on the grid: 263.1 and
263.7 both divide exactly by 0.05, and a 0.05 bid is refused because one tick would take it to
zero.

## 3 · The lot, from the production authority

The per-slot `lot` block in every envelope reads `PRODUCER_CLAIM_UNRESOLVED` /
`UNRESOLVED_NO_METADATA_SUPPLIED` **because that is what it is for**:
`PremiumObservationContract.lotEvidence` records a claim and its shape checks, and states in the
envelope that authority requires a resolver-validated table row. Authority is decided where the
metadata is — at export, in `references.production_lot_authority`, resolved from the
application's own `ContractLotTable` **by index, expiry, cycle and observation date**.

On the 12 November export that record is present and resolved for both indices:

| index | lot | expiry | matched rule | table version | as-of | coverage |
| --- | ---: | --- | --- | --- | --- | --- |
| NF | **65** | 2026-11-17 | `FAOP70616_NF_weekly_revised` | `contract_lot_table_v3_20260914` | 2026-11-12 | 53 / 53 envelopes |
| BNF | **30** | 2026-11-23 | `FAOP70616_BNF_monthly_revised` | the same | 2026-11-12 | 54 / 54 |

`lot_conflict: false` on both, so the producer's claim agrees with the authority. Note the NIFTY
lot is **65**, not the 75 an older constant would have supplied — which is why a lot taken from a
constant would silently rescale every rupee in the ledger.

Every one of these must hold or there is no lot: `recorded`, `authority_holds`, `resolved`,
`authoritative`, `reference_matches_record`, `lot_conflict == false`, `coverage.complete`, the
**envelope's own** `production_authority_ref` listed in `referenced_by_envelopes`, and the
authority's **expiry equal to this contract's expiry** — the table resolves per expiry and cycle
and states it is not flattened to one lot per underlying, so borrowing another expiry's lot would
be exactly that flattening. Each condition has a test.

## 4 · Labels and aggregation

A calculated fill is `PROXY_SIMULATED_FILL`, always. An actual Paper outcome is carried in
`linked_actual_paper_outcome` and summarised **separately** in
`aggregate()['actual_paper_outcomes']`, from the actual records' own numbers, with the note that
no calculated number enters it and no actual number enters the proxy means. With no actual
outcome present there is no actual summary at all. The midpoint forecast is carried in
`forecast_view` and a test asserts that adding one cannot change `net_inr`.

Rows inside one `(index, session)` are correlated — one path, overlapping strikes, polls minutes
apart — so aggregation is by cluster with equal weight per cluster, never by row, and coverage is
printed beside every mean with **all** unscorable reasons counted.

## 5 · The corrected measurement on the 12 November export

```bash
python3 -I tools/premium_followon/cost_ledger.py mr_session_evidence_2026-11-12.json \
    --index NF --hold-min 60        # and --index BNF
```

| index | BUY rows | scorable | fee window **alone** | no hold window | no fill at all |
| --- | ---: | ---: | ---: | ---: | ---: |
| NF | 318 | **0** | **240** | 72 | 6 |
| BNF | 323 | **0** | **244** | 73 | 6 |

The partition is exact: 240 and 244 rows carry **one** reason, `fee_rates_unknown_for_trade_date`;
72 and 73 are the last 60 minutes of a capture that stops at 13:40/13:45; 6 per index are each
contract's final poll, which has no fill and therefore no envelope to read an authority reference
from. The lot gap is closed — `lot_authority_unresolved` fell from **240 to 6**.

**Coverage stays zero**, and for one honest reason. The fee table is verified only through
2026-10-09: `2026-11-12 → UnknownHistoricalRate`, `2026-11-16 → UnknownHistoricalRate`. See
`FEE_SOURCE_AUDIT_20261116.md`: four of the six components are confirmed unchanged against the
exchange's own current page, two remain outstanding, and the one-line conditional change is
written out there. **No rate was assumed and the frozen table is unchanged.**

## 6 · What a scorable Stage D row still needs

1. A trade date inside a verified fee-rate window (§5 and the audit).
2. At least `hold_min` of capture after the fill poll, so the hold closes inside the session —
   which the follow-on capture path's close window also helps with.
3. Two-sided positive executable prices with verified identity at both the fill and the exit.
4. A production lot authority in the tree being scored. The follow-on capture writes one; see
   `SPEC_G_NEXT_SESSION_MATCH_v2.md` §2, where the first version of that path did not and the
   gap was found and closed.

## 7 · Out of scope

No live behaviour, no production write, no activation claim. Ask-to-bid P&L stays a **proxy**
while quote freshness and quantity units are unresolved, exactly as the frozen plan states. The
frozen fee model and the frozen repricing tooling are imported, not modified.

## 8 · v3: the authority is bound to the row, and a fill needs a decision clock

### 8.1 Two defects, both reproduced before they were fixed

**A row scored with no decision clock.** v2 computed the entry-delay check only
`if dec_s is not None`, so removing the decision receipt **skipped** it, and the row scored
**₹2,921.77** as a simulated trade off a decision that carried no instant at all.

**A mutated authority was accepted.** v2 checked that the authority record called itself
resolved. An authority mutated to another index (`BANKNIFTY`, lot 30), an unrelated as-of date,
an **empty** `referenced_by_envelopes` list and an arbitrary reference was accepted, and a
NIFTY row scored at **lot 30**. The membership test read `if listed and ref not in listed`, so
an empty list passed it, and nothing compared the record's index, session, as-of date or digest
to the row.

### 8.2 A valid decision clock is required before any simulated P&L

Named reasons, each tested on its own, each blocking:

| reason | fires when |
| --- | --- |
| `decision_clock_missing` | no decision observation, or its `receipt_ts` does not parse |
| `decision_clock_not_in_this_session` | the receipt falls on another date, or outside the session's trading clock |
| `decision_not_before_entry` | the decision is at or after its own fill |

A blocked row carries no `net_inr` and no `gross_inr`, and the reason is reported **beside**
every other blocking reason, not instead of them.

### 8.3 The dated lot authority, bound to the exact row

`lot_from_production_authority(block, expiry, envelope_ref, index, session)`. Every one of these
must hold, and each has its own test:

| binding | refusal |
| --- | --- |
| recorded, `authority_holds`, `resolved`, `authoritative`, `reference_matches_record` | `*_not_true` |
| no lot conflict; not flattened to one lot per underlying; coverage complete | `lot_conflict`, `authority_flattened_to_one_lot_per_underlying`, `coverage_incomplete` |
| `referenced_by_envelopes` is **NON-EMPTY** | `referenced_by_envelopes_is_empty` |
| the envelope's reference is present and a member of that list | `envelope_reference_absent`, `envelope_reference_not_listed` |
| the reference **recomputes** from the record's own bytes | `authority_digest_does_not_match_envelope_reference` |
| `index_key` and `requested_index` are THIS index | `authority_index_key_is_X_not_Y` |
| `requested_session_date` is THIS session | `authority_requested_session_is_X_not_Y` |
| `lot_as_of` is applicable to that session | `authority_as_of_X_not_applicable_to_Y` |
| `expiry` and `requested_expiry` are THIS contract's expiry | `authority_expiry_is_X_not_Y` |
| the lot is a positive integer | `contract_lot_size_not_an_integer`, `..._not_positive` |

An empty list failing the membership check and a listed-but-arbitrary reference failing the
digest check are separate tests, because either one alone would have let Codex's mutation
through. `test_a_tampered_lot_breaks_its_own_digest` edits the record after the reference is
computed and shows the reference no longer recomputes.

The binding is usable because the reference recomputes from the COMMITTED production record:
`test_the_reference_recomputes_from_the_committed_production_record` checks the fixture's own
`production_lot_authority` against `authority_reference()`.

### 8.4 The stream is named here too

`decisions_from_export(..., section=...)` and `score_export(..., section=...)`, with
`--section` at the CLI. The hazard is the one Stage G hit: a phone-shaped export carries two
streams, and reading whichever one sits under `premium` scores the wrong contracts without
saying so. A missing section returns `[]` rows and `{'premium_followon_section_absent': 1}` —
a stated reason, not an empty ledger.

### 8.5 Measured on the phone-shaped export

`phone_shaped_export_2026-10-19.json`, `--section premium_followon`:

| index | BUY rows | lot resolved | blocked by | scorable |
| --- | ---: | ---: | --- | ---: |
| NF | 12 | 6 at **65** (`FAOP70616_NF_weekly_revised`) | `fee_rates_unknown_for_trade_date` only | 0 |
| BNF | 12 | 6 at **30** (`FAOP70616_BNF_monthly_revised`) | `fee_rates_unknown_for_trade_date` only | 0 |

The other 6 rows per index are each contract's final poll, which has no fill
(`fill_poll_absent`). The lots are the DATED table's, resolved per index, expiry and cycle —
not the producer's per-slot claim, which is `PRODUCER_CLAIM_UNRESOLVED` by design.

**After-cost coverage is exactly zero while fee evidence is unresolved**, by construction.
`FEE_SOURCE_AUDIT_20261116.md` now records all six components verified against primary or
issuing sources, and `patches/0001-fee-table-verified-through-20261116.patch` is the dated
table change, **not applied**: the frozen table earlier research ran against is preserved
byte-for-byte, and the patch states what it still cannot prove.
