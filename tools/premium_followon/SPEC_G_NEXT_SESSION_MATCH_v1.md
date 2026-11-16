# Stage G v1 — next-session quote matching

**Version** `premium_followon_stage_g_next_session_match_v1` · **declared** 16 November 2026 IST ·
**implementation** `next_session_match.py` · **checks** `tests/test_next_session_match.py`
(32 synthetic cases)

No collector change and no exit-lifecycle change is needed for this checkpoint. This is a reading
rule, declared before any pair is scored.

## 1 · Same-contract identity

A contract is the five-tuple

```
(index, instrument_key, expiry, strike, side)
```

and **all five must match** on both observations. The economic description alone is not enough:
after a roll the earlier instrument key usually still resolves, so a rule keyed on strike and
side would join two different contracts and report the difference as an overnight move. A
mismatch is refused as `contract_identity_changed_roll` and **names the field that changed**.

`identity_verified` and `identity_disagreements` come from the capture's own identity block; an
unverified or disagreeing record is unusable, never "probably the same contract".

## 2 · The two clocks, kept apart

| field | what it is |
| --- | --- |
| `receipt_ts` | when the quote was in hand. Derived from the envelope's `acquisition_response_ms`, never from the nominal slot label. |
| `exchange_ts` | the vendor's own event timestamp, when present |
| `exchange_ts_verified` | whether that timestamp was verified (`clocks.quote_event_clock_verified`) |

They are never substituted for one another. **Staleness is checked only against a verified
exchange timestamp** (bound `MAX_QUOTE_STALE_S = 120`). With no verified timestamp the quote is
still usable and the pair records `staleness_established: false` — unknown is reported as
unknown, not as fresh. On the 12 November 2026 export every record carries
`vendor_source_ts: null` and `quote_event_clock_verified: false`, so no staleness claim is
available from existing evidence.

The paired underlying and IV are carried with their own provenance: `underlying` (from
`underlying.spot_raw.raw`), `underlying_status`, `underlying_basis`,
`underlying_sync_with_quote`, `iv`, `iv_status`, and the acquisition id.

## 3 · The marks

| mark | rule |
| --- | --- |
| **close** | the **last usable** observation inside `15:10-15:30 IST`. A session whose capture stops earlier has **no close mark**: `no_close_mark_in_window`, with the last observed slot reported. It is never back-filled from an earlier slot, because relabelling a 13:40 quote as a close would silently change what the number means. |
| **open** | the **first usable** observation of the next trading session, with `delay_after_open_min` measured from 09:15. A first usable quote later than `MAX_OPEN_DELAY_MIN = 30` is still reported, with its delay, and marked `delay_later_than_declared_bound`. |
| **opening-window mark** | exactly close → first usable: `OPENING_WINDOW_CLOSE_TO_FIRST_USABLE`, with `elapsed_min` between the two receipt instants, the delay after open, and the calendar gap. |
| **post-open** | separate marks at `+5, +15, +30` minutes after the first usable quote, each with its own move. **Never folded into the opening mark** — an overnight move and the first half hour of trading are different quantities, and one number mixing them belongs to neither. |

## 4 · The calendar

Read from the app's own `_CONST['NSE_HOLIDAYS']` in `app/src/main/python/brain.py` at run time
rather than copied, with a strict parse that raises instead of silently producing an empty
holiday set. Monday–Friday minus those dates.

**Outside the list's covered years the pairing fails closed** (`calendar_not_covered`). The list
currently holds 15 dates, all in 2026, ending **2026-12-25** — so **no 2027 pair can be formed
until it is extended**. Weekends and holidays are reported as the gap they are
(`calendar_days`, `intervening_weekend_days`, `intervening_holidays`), never silently absorbed.

Two sessions that are not calendar-adjacent are refused as `next_session_not_adjacent`, with the
trading day that was expected.

## 5 · Declared refusals

| reason | when |
| --- | --- |
| `no_close_mark_in_window` | nothing observed in 15:10–15:30 |
| `close_quote_unusable` | everything in the window failed a usability rule (listed per slot) |
| `no_usable_open_quote` | no usable quote in the next session (reasons listed per slot) |
| `contract_absent_in_next_session` | the identity does not appear at all |
| `contract_identity_changed_roll` | it appears with a changed field |
| `target_at_or_after_expiry` | the next session is on or after the contract's expiry |
| `calendar_not_covered` | the holiday list cannot speak about these dates |
| `next_session_not_adjacent` | the two sessions are not consecutive trading days |

And per observation: `book_missing`, `book_one_sided`, `book_non_positive`, `book_crossed`,
`receipt_clock_missing`, `identity_unverified`, `identity_disagreement`,
`stale_against_verified_exchange_ts`. Each is asserted by its own test; the test also asserts
the declared lists contain **no reason the code cannot emit** — a first draft of this spec
carried an `open_quote_unusable` that could never fire, and the test removed it.

Record normalisation inherits the frozen repricing harness's fail-closed rule: two records for
one `(contract, acquisition)` that disagree are **both excluded**, identical repeats collapse to
one, and records without an envelope or outside the pinned cohort are counted
(`conflicting_record_key`, `conflicting_envelope`, `record_without_envelope`,
`record_outside_pinned_cohort`).

## 6 · What the existing evidence can and cannot support

```
python3 -I tools/premium_followon/next_session_match.py \
    phone_2026-11-12.json phone_2026-11-13.json --index NF
```

> sessions 2026-11-12 (premium.slot_envelopes.session_date), 2026-11-13 (parts.authority_doc…)
> **6 of 6 contracts REFUSED `no_close_mark_in_window`**, last observed slot **13:40**

**No opening-window mark can be formed from any evidence held offline.** The only premium
session, 12 November, stops capturing at 13:40 (NIFTY) and 13:45 (BANKNIFTY), and the next
session carries no premium capture at all. The adapter does read that session correctly — six
contracts, no exclusions, usable two-sided books, both clocks separated — so the rule is proven
on real bytes; what is missing is the evidence, not the reader.

### Minimum new phone evidence for one Stage G pair

1. Premium capture on **two consecutive trading sessions** for the same index.
2. The first session must capture **inside 15:10–15:30 IST** — the earliest acceptable close
   mark is a slot at or after 15:10. The b530 capture window must therefore reach the close, not
   stop in the early afternoon.
3. The second session must capture **at or soon after 09:15**, and the first usable quote's delay
   is reported whatever it is.
4. The **same pinned contracts** on both sessions: the cohort must not roll between them, or
   every pair is correctly refused as a roll.
5. The second session must be **before** the contracts' expiry.
6. Two-sided positive books with `identity_verified: true` at both marks.
7. For any staleness claim: `vendor_source_ts` present **and**
   `clocks.quote_event_clock_verified: true`. Without it the pair is still valid and simply
   records that staleness was never established.

Nothing above asks for a collector change; items 2 and 3 are about the capture window the
installed build already has.
