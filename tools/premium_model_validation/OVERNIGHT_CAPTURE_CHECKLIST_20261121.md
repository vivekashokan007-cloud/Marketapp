# Overnight capture — the blocker is the capture window, not the missing session

**21 November 2026 IST · for Codex review · specification only, nothing implemented**

Codex: *"The current inventory has zero eligible next-session pairs. Specify the minimum capture
for same-contract quotes on consecutive trading sessions, with a dated pre-expiry exit, quote
timing and missing-data handling. Reuse the matcher and show the first qualifying pair when
available."*

**The headline is not what the inventory suggested.** Zero pairs is not a data-volume problem
that more sessions would fix. The pilot capture window ends before the matcher's close window
begins, so **no overnight pair can be formed from the current capture at all** — not now, and not
after any number of adjacent sessions are captured.

---

## 1 · The blocker, proven rather than inferred

| | |
| --- | --- |
| pilot capture window | `premium_capture_cohort_pilot_window_0920_1505_exclusive_v1` — 09:20 inclusive → **15:05 exclusive**, so the **last slot is 15:00** |
| matcher's close window | `NEAR_CLOSE_FROM = '15:10'` → `CLOSE_CLOCK = '15:30'` |
| consequence | the capture can never produce a close mark |

Confirmed against the export rather than assumed: the 16 November capture holds **69 distinct
slots, first 09:20, last 15:00**, for both indices.

**The proof isolates the blocker from adjacency.** I relabelled the 16 November export as
17 November, so the two sessions are consecutive trading days carrying identical contracts —
adjacency satisfied by construction. All twelve pairs (6 pinned contracts × 2 indices) still
refuse, every one for the same reason:

```
  REFUSED  2026-11-16 -> 2026-11-17   no_close_mark_in_window   15:00     (x6, NF)
  REFUSED  2026-11-16 -> 2026-11-17   no_close_mark_in_window   15:00     (x6, BNF)
  refusals by reason:  no_close_mark_in_window  6  (per index)
```

So the real inventory refusals — `next_session_not_adjacent` for 12→16 November, and
`no_observations_for_index_in_next_session` for the `NO_PREMIUM_DATA` 13 November export — were
hiding a second, harder blocker behind them.

**The open side is already fine.** The matcher wants a usable mark within 30 minutes of 09:15,
i.e. by 09:45; the capture's first slot is 09:20.

## 2 · The minimum capture, derived from the matcher's own constants

Not invented here. Every requirement below is a constant or a refusal code in
`tools/premium_followon/next_session_match.py` (`premium_followon_stage_g_next_session_match_v3_explicit_section`),
so a capture that meets this list is one the existing matcher will pair without modification.

### 2.1 · The one change that unblocks everything

| | required | today | gap |
| --- | --- | --- | --- |
| last captured slot on the **close** session | **≥ 15:10**, and ideally several slots across 15:10–15:30 so one unusable book does not lose the pair | 15:00 | **extend the pilot window's end from 15:05 exclusive to 15:35 exclusive** |

That adds six slots (15:05, 15:10, 15:15, 15:20, 15:25, 15:30), taking the grid from 69 to 75.
It is a **capture-window change in the phone build**, which this assignment puts out of scope, so
it is specified and not implemented. Note it also moves the authoritative planned-schedule
denominator, so `planned_schedule.py`'s `CAPTURE_WINDOW_RULES_VERSION` must be bumped in the same
change and the 60-minute eligible-slot count recomputed — the coverage figures in the range trial
are built on the current 69-slot grid and must not silently inherit a 75-slot one.

### 2.2 · Both sessions

| requirement | matcher's refusal if absent |
| --- | --- |
| the two dates are **consecutive trading sessions** per the app's own `NSE_HOLIDAYS`, read from `brain.py` | `next_session_not_adjacent`, `calendar_not_covered` |
| the **same contract identity** on both — instrument key, expiry, strike, side. A roll is not a pair | `contract_identity_changed_roll`, `contract_absent_in_next_session` |
| a usable **close mark** in 15:10–15:30 on the close session | `no_close_mark_in_window`, `close_quote_unusable` |
| a usable **open mark** within 30 min of 09:15 on the next session | `no_usable_open_quote`, `first_usable_outside_opening_tolerance` |
| receipt clocks that do not run backwards across the pair | `impossible_receipt_order` |

### 2.3 · A dated pre-expiry exit

The overnight exit must fall **strictly before expiry**, or the matcher refuses with
`target_at_or_after_expiry`. Two consequences worth stating before any capture is scheduled:

* a close session that is the **last session before expiry** gives an exit **on expiry day**.
  That is a different instrument state — pin risk, collapsing time value, a settlement
  convention — and Codex's instruction is explicit that overnight and longer holds get their own
  labels and evaluation. It must not be pooled with ordinary overnight pairs;
* NIFTY's pinned 16 November expiry was **2026-11-17**, the very next session, so even a
  working close mark that day would have produced only an expiry-day pair. **A useful overnight
  cohort needs the close session at least two sessions before expiry**, which for weeklies means
  capturing early in the contract's week.

### 2.4 · Quote usability, per observation

A mark is usable only if none of the thirteen declared `UNUSABLE` reasons applies. Grouped:

* **the book** — present, two-sided, positive, not crossed (`book_missing`, `book_one_sided`,
  `book_non_positive`, `book_crossed`);
* **the clock** — a receipt timestamp exists, its date is the session, and it falls inside
  09:15–15:30 (`receipt_clock_missing`, `receipt_date_not_in_session`,
  `receipt_outside_session_clock`);
* **the identity** — verified, with no disagreement and nothing missing (`identity_unverified`,
  `identity_disagreement`, `identity_incomplete`);
* **the exchange timestamp** — if the record flags one, it must exist, must not post-date its own
  receipt, and must be within `MAX_QUOTE_STALE_S = 120` s
  (`exchange_clock_flag_without_timestamp`, `exchange_ts_after_its_own_receipt`,
  `stale_against_verified_exchange_ts`).

### 2.5 · Quote timing, and what cannot be established today

**Staleness is only checkable against a verified exchange timestamp, and these exports have
none** — every instant is the app's own acquisition receipt. So `stale_against_verified_exchange_ts`
cannot fire, and an overnight pair scored on this capture carries a **receipt-proxy** label on
both marks rather than a demonstrated quote age.

This matters more overnight than intraday. A close mark captured at 15:12 whose underlying quote
is actually from 15:02 misstates the overnight gap by ten minutes of movement, and nothing in the
current capture would reveal it. **If overnight economics are to carry weight, the exchange quote
timestamp needs to be captured and verified**, not just the receipt. That is a capture change,
listed here as a requirement rather than a caveat.

### 2.6 · Missing data

The matcher already distinguishes every failure above by name and reports counts per reason, so a
capture that partly fails is diagnosable rather than simply empty. Two rules carried over from
the economic replay, which should hold for the overnight scoring too:

* **a missing exit is an outcome, not a decision.** An overnight position whose next-session open
  mark is unusable must keep its recorded decision and report the missing mark as its own status —
  never be dropped from the population, and never be counted as a WAIT;
* **nothing is estimated to fill a gap.** No interpolated open, no previous-close substitution, no
  assumed lot. The fee table's dated refusal is the model to follow.

## 3 · The inventory today

```
  REFUSED  2026-11-12 -> 2026-11-13   no_observations_for_index_in_next_session
  REFUSED  2026-11-13 -> 2026-11-16   no_observations_for_index_in_close_session
  REFUSED  2026-11-12 -> 2026-11-16   next_session_not_adjacent (2026-11-13)
```

**Zero eligible pairs**, for three stacked reasons: 13 November carries no premium observations
(`4f544bee…`, `NO_PREMIUM_DATA`) and is the only session between the two scored dates, so no
adjacency exists; and even where adjacency exists, §1 shows the close mark cannot be captured.

## 4 · What to do, in order

1. **Extend the capture window** to 15:35 exclusive, bumping `CAPTURE_WINDOW_RULES_VERSION` and
   recomputing the planned-schedule denominator in the same change. Without this, nothing else
   matters.
2. **Capture two consecutive sessions**, both with premium records, with the close session at
   least two sessions before the pinned expiry.
3. **Capture and verify the exchange quote timestamp**, so overnight staleness becomes checkable
   rather than proxied.
4. Then run the matcher unchanged and show the first qualifying pair:

```bash
python3 -I tools/premium_followon/next_session_match.py <export_day_n> <export_day_n+1> \
    --index NF
```

Until step 1 lands, that command will print `no_close_mark_in_window` for every contract, and
reporting it as "no eligible pairs yet" would be misleading: it is not waiting for data, it is
waiting for a capture change.

## 5 · Scope

Specification only. No capture-window change, no phone build, no production write, no change to
the matcher, and no overnight figures are claimed — there are none to claim. The proof in §1 used
a synthetic relabelled export in the scratch directory, which is not committed.
