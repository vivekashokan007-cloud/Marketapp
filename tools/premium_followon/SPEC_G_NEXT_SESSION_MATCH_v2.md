# Stage G v2 — next-session quote matching, and the capture that can feed it

**Version** `premium_followon_stage_g_next_session_match_v2_verified_clocks` ·
**declared** 16 November 2026 IST · **matcher** `next_session_match.py` ·
**capture** `app/src/main/java/com/marketradar/app/PremiumFollowOnCapture.kt` ·
**checks** `tests/test_next_session_match.py` (44) and `PremiumFollowOnCaptureTest.kt` (13)

Supersedes v1. v1's readiness claim was wrong and two of its rules let a bad mark through; all
three corrections are below, each with a named regression.

## 0 · The readiness correction

**v1 said two more sessions on the installed build would supply a close mark. They cannot.**
The installed signed b530 (`398e1ab2250c4cdbe97e83c78f0b13795493c9df`) captures

```kotlin
const val WINDOW_OPEN_MIN  = 9 * 60 + 20     // 09:20
const val WINDOW_CLOSE_MIN = 15 * 60 + 5     // 15:05
```

**inclusive of the open and exclusive of the close** (`PremiumCaptureCohort.kt:45-47`), so its
last five-minute slot is **15:00** and the matcher's close window is 15:10–15:30. No number of
further sessions on that window produces the mark.
`PremiumFollowOnCaptureTest.theInstalledPilotWindowCannotProduceACloseMark` asserts this from the
production constants and then shows a real 15:20 attempt through the pilot path being refused
`OUTSIDE_EXPERIMENT_WINDOW` with nothing written.

## 1 · The matcher corrections

| v1 behaviour Codex reproduced | v2 |
| --- | --- |
| a **09:10** quote with **no** `identity_verified` field accepted as the opening mark, delay **−5 min** | a scored mark needs the flag **explicitly true**, no disagreements and all five identity fields present; the receipt must be **in the session's own date and clock**; and the delay is measured on the **receipt instant**, not the slot label. That 09:10 record is now refused `receipt_outside_session_clock` + `identity_unverified` and never becomes a mark. |
| **`staleness_established: true`** with a verified flag but **no** exchange timestamp | the two fields must agree. A flag without a timestamp is `exchange_clock_flag_without_timestamp`; a timestamp after its own receipt is `exchange_ts_after_its_own_receipt`; and `staleness_established` is true only when **both** marks carry a verified flag **and** a timestamp inside the 120 s bound. With neither, the pair is valid and records unknown freshness. |

Also tightened: an exit receipt that precedes the close receipt is `impossible_receipt_order`; a
first usable quote outside the declared opening tolerance is **not** a scored mark but is kept as
**labelled diagnostic evidence** (`LATE_OPEN_DIAGNOSTIC_NOT_A_MARK`, with its delay); and the two
movements are reported apart, as `close_to_first_usable_mid_move` and `post_open_movement`.

Declared refusals: `no_close_mark_in_window`, `close_quote_unusable`, `no_usable_open_quote`,
`first_usable_outside_opening_tolerance`, `contract_absent_in_next_session`,
`contract_identity_changed_roll`, `target_at_or_after_expiry`, `impossible_receipt_order`,
`calendar_not_covered`, `next_session_not_adjacent`. Per-observation reasons: the thirteen in
`UNUSABLE`. Both lists are asserted to be **exactly** what the code can emit.

## 2 · The research capture path

`PremiumFollowOnCapture` is small, separately identified, and reuses the existing acquisition,
scheduling and export code.

**Its own windows, leaving the pilot's alone.** `CaptureWindow` is new on
`PremiumCaptureCohort`; `PILOT_WINDOW` is exactly the two constants above and **every existing
caller takes it by default**, so b530's behaviour and the frozen 60-minute pilot are unchanged.
The follow-on declares `followon_close_1510_1530` (inclusive) and
`followon_reopen_0915_0945` (inclusive).

**The same contracts on both sides, through the normal path.** It calls the same
`PremiumCaptureSession.observeSlot` on the same poll results the pilot capture is given. Two
arguments are added, both defaulted: the `window`, and `requireStrikes`.

1. In the close window the cohort is selected normally and a **carry** is written — index,
   underlying key, expiry, the six `(instrument_key, option_type, strike)` triples, and a digest
   over them.
2. In the next session's reopen window the carry is read **from disk**, validated, and its
   strikes are passed as `requireStrikes`, so the cohort is pinned to **yesterday's contracts**
   instead of being reselected around the new spot. `selection_basis` then reads
   `THREE_STRIKES_REQUIRED_BY_CALLER_VALIDATED_IN_THIS_RESPONSE`.

The test proves the carry is load-bearing rather than incidental: on the committed fixture a free
selection on Monday's response would have pinned **different** strikes, because the spot moved
overnight, and the test asserts that difference before asserting that the reopen pinned the
carried ones.

**No missing dependency in the acquisition path.** `MarketWatchService` polls
09:15–15:40 IST on a five-minute grid (`MARKET_OPEN_MINUTE`, `MARKET_CLOSE_MINUTE`), and the
capture hook already runs inside that poll, wrapped so a capture failure cannot fail the poll. A
chain response therefore already exists at 15:10, 15:20, 15:30 and at 09:15 and 09:20 — only the
capture's own window check stopped it being recorded. Wiring this in costs one call beside the
existing hook, which **this commit deliberately does not make**: it is for the later candidate.

**One dependency was missing, and is now closed.** The first version of this path called
`observeSlot` directly and so produced a tree whose envelopes carried a
`production_authority_ref` with no record to match — the export read
`NO_AUTHORITY_RECORD_PRESENT` and every Stage D row over it would have been refused
`lot_authority_unresolved`. The pilot resolves and writes that record in
`PremiumCaptureCoordinator`, so the follow-on path now **reuses the same resolver**
(`lotClaimFromContractAuthority`) and writes the authority into its own tree. On the committed
fixture it reads: recorded, holds, resolved, not conflicting, reference matched, coverage
complete, **NIFTY lot 65 and BANKNIFTY lot 30**, expiry 2026-10-27, rule
`FAOP70616_NF_weekly_revised`, as-of 2026-10-16.

### How the new records, refusals and bytes stay identifiable

| | |
| --- | --- |
| records | its own store root, `premium_capture_followon`, a **sibling** of the pilot's `premium_capture` — asserted not to be inside it |
| refusals | the follow-on store's own counters and summaries; the pilot's are untouched |
| bytes | the follow-on root's own total, reported in the export as `bytes`. It is bounded by the same per-root cap (`PremiumCaptureStore.MAX_BYTES_GLOBAL`, 4 MiB on this base) and writes on the order of 30 KiB a session at two acquisitions per index |
| export | a separate section under its own key, carrying `purpose: premium_followon_overnight_mark_v1`, both window ids, the pilot window with `independent_of_this_capture: true`, the store root, the byte total and the carries |
| architecture | the file is added to `PremiumCaptureTest`'s entry-point **allowlist** explicitly, which is what that allowlist is for — not hidden from it |

### What it refuses

`CARRY_ABSENT`, `CARRY_UNREADABLE`, `CARRY_DIGEST_MISMATCH`, `CARRY_FOR_ANOTHER_INDEX`,
`CARRY_FROM_THIS_SESSION_NOT_A_PRIOR_ONE`, `CARRY_EXPIRY_NOT_AFTER_SESSION`, `CARRY_INCOMPLETE`,
`NOT_IN_A_FOLLOW_ON_WINDOW`, and from the cohort selector
`REQUIRED_STRIKE_NOT_PAIRED_IN_RESPONSE` — a roll or a delisted strike, refused rather than
substituted with a neighbour.

`CARRY_EXPIRY_NOT_AFTER_SESSION` **is** the exit-before-expiry rule: a mark into expiry day is
not a position this research holds. Holidays and weekends need no special case — the carry is
dated and is used by the next session that actually captures, and the gap it crossed is reported
(`calendar_days`, `intervening_weekend_days`, `intervening_holidays`). The trading calendar is
the project's own `_CONST['NSE_HOLIDAYS']`, read at run time, failing closed outside its covered
years, which end **2026-12-25**.

## 3 · The cross-language proof

The capture's **own** exports are committed as fixtures
(`followon_export_2026-10-16.json`, `followon_export_2026-10-19.json`, produced by
`PremiumFollowOnCapture` from the committed chain fixture and labelled synthetic in their
provenance), and the matcher is run on them in CI:

```
$ next_session_match.py followon_export_2026-10-16.json followon_export_2026-10-19.json --index NF
PAIRED  2026-10-16 -> 2026-10-19  NSE_FO|51018 24450.0 CE  close->first usable +60.00
        elapsed 3960.0 min  delay after open 5 min  staleness unknown
... 6 of 6 contracts, and 6 of 6 for BANKNIFTY, with no refusals
```

Friday 15:20 to Monday 09:20 — 3,960 minutes across a two-day weekend gap, the same instrument
keys on both sides, a 5-minute delay after the open, and staleness honestly unknown because the
fixture carries no verified exchange clock. If the capture path and the matcher ever disagree
about a field, a clock or an identity, those tests fail.

## 4 · Regressions in place

Matcher: the two reproduced defects; the b530 cutoff (a session ending 15:00 has no close mark);
a pre-open quote; a missing identity flag; a missing and a future-dated exchange timestamp; a
cohort reselected at a new strike; a restart that re-presents an acquisition identically; a
roll; a missing close; an impossible receipt order; the opening-tolerance boundary; NIFTY and
BANKNIFTY separately; and a positive end-to-end pair for each index asserting the clocks and
contract provenance the export actually carries.

Capture: the pilot window's inability to reach the close; the follow-on window boundaries; a
poll outside both windows writing nothing at all; the carry written and the reopen pinning
yesterday's strikes; the carry surviving a restart; a roll refused; a carry into expiry refused;
a tampered carry refused by its digest; a same-session carry refused; the separate tree, bytes
and export keys; both indices independently; and the clock and contract provenance Stage G reads.

## 5 · What a Stage G session still needs from the phone

1. A build carrying this capture path **wired into the poll hook** — one call, not in this commit.
2. Two consecutive trading sessions with it enabled.
3. The same cohort available on the second morning (a roll is correctly refused, not paired).
4. The second session before the contracts' expiry.
5. For a staleness claim: `vendor_source_ts` present **and**
   `clocks.quote_event_clock_verified: true`. Without them the pair is still valid and records
   that staleness was never established.

Nothing above changes the pilot window, the frozen 60-minute pilot, the installed b530, the
existing recommendations, ranking, sizing, exits or notifications.
