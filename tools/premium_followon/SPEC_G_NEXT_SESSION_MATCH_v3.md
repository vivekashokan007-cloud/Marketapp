# Stage G v3 — next-session quote matching, wired end to end

**Version** `premium_followon_stage_g_next_session_match_v3_explicit_section` ·
**declared** 16 November 2026 IST · **matcher** `next_session_match.py` ·
**capture** `app/src/main/java/com/marketradar/app/PremiumFollowOnCapture.kt` ·
**poll hook** `MarketWatchService.kt` · **export** `NativeBridge.kt` + `PremiumExportSection.kt` ·
**checks** `tests/test_next_session_match.py` (52), `PremiumFollowOnCaptureTest.kt` (19) and
`PremiumFollowOnEndToEndTest.kt` (4)

Supersedes v2, which was correct about clocks and wrong about where it read them from: pointed
at a phone-shaped export it read the PILOT stream, found no overnight pairing in it, and
reported **zero pairs for both indices with no refusal reason**. §6 below is the correction and
the end-to-end wiring that proves it; v2's own corrections (§0–§5) stand unchanged.

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

## 6 · v3: the stream is named, and the path is wired end to end

### 6.1 The defect, reproduced before it was fixed

On an export carrying both streams — the frozen pilot capture under `premium`, the follow-on
research capture under `premium_followon` — v2 read `premium` unconditionally:

```
$ next_session_match.py <close>.json <next>.json --index NF     # v2
attempted pairs 0
(no refusals, no reasons, no sessions)
```

Zero pairs and no reason is the worst answer the matcher can give, because it is
indistinguishable from "the market did not move".

### 6.2 The rule

`section_of(doc, name)` selects the capture stream by NAME. `SECTION_PILOT = 'premium'`,
`SECTION_FOLLOWON = 'premium_followon'`; the follow-on wrapper (`{purpose, section: {...}}`,
which is what `PremiumFollowOnCapture.exportSection` writes) and a bare section are both
accepted. A section that is absent, malformed or unknown is a STATED outcome
(`section_absent_or_malformed`, `unknown_section_<name>`), never an empty result. `--section`
selects it at the CLI; Stage D takes the same argument, for the same reason.

An index present in one session and not the other is named too
(`no_observations_for_index_in_close_session` / `..._in_next_session`), with the excluded-record
counts attached.

### 6.3 The wiring, all three hops

| hop | where | what it does |
| --- | --- | --- |
| poll → capture | `MarketWatchService`, immediately after the pilot's `PremiumCaptureCoordinator.onChainsAcquired` block | its own `premiumCaptureSafely` wrapper, gated by `PremiumFollowOnCapture.isResearchEnabled(filesDir)`, feeding `PremiumCapturePollAdapter.acquisitions(...)` output to `PremiumFollowOnCapture.observe(..., expiryForAuthority = acq.expiry)`. Makes no request of its own, reuses the chains already in hand, and runs before the quotes abort that would discard good chains. |
| capture → export | `NativeBridge`, after the pilot section is merged | `PremiumExportSection.mergeWithinCap(..., "premium_followon")`. Attached only if the COMPLETE section fits the existing combined cap; if it does not it is **omitted and reported** (`omitted_over_cap`), because a research stream must never shrink, truncate or refuse the pilot export. |
| export → matcher | `next_session_match.py --section premium_followon` | §6.2 |

`expiryForAuthority` is what makes the capture resolve and write the production lot authority
into its own tree, through the same `PremiumCaptureCoordinator.lotClaimFromContractAuthority`
the pilot uses. The first version of the end-to-end test supplied that argument by hand and
forgot it, producing a tree whose envelopes pointed at an authority reference with no record
behind it — Stage D then refused all 12 rows `lot_authority_unresolved`. The test now routes
through the adapter, so it cannot diverge from the poll on that field again.

### 6.4 Identity at the reopen, unchanged and now asserted through the export

The carry pins the exact five-field contract identity. At the reopen the cohort must BE the
carry's contracts; a changed instrument key under an unchanged strike, a rolled expiry, a
missing strike and a stale carry are each refused rather than substituted. A cohort already
pinned for the session is compared against the carry before anything is written
(`SAVED_COHORT_DOES_NOT_MATCH_CARRY`).

New in v3: a cohort-level refusal writes no record at all, so the export used to publish only
`NO_PREMIUM_DATA_FOR_THIS_INDEX` and the REASON lived in a returned object until the process
ended. `window_outcomes` now carries it per index, as durable counters inside the store's
existing reserve (`followon_reopen_refused_<status>`, `followon_close_observed`, …), so a
refusal is stated in the evidence rather than inferred from an absence.

### 6.5 The end-to-end check, and what it measures

`PremiumFollowOnEndToEndTest` builds the phone shape through the real poll mapping and the real
export assembly, for both indices, across a restart, and writes
`app/src/test/resources/premium_capture/phone_shaped_export_2026-10-16.json` and
`...-10-19.json`. The pilot stream carries its OWN cohort with valid quotes; the follow-on
carries yesterday's carried contracts. Run against the normal matcher entry point:

```
--index NF  --section premium_followon   attempted pairs 6   PAIRED 6   refusals {}
--index BNF --section premium_followon   attempted pairs 6   PAIRED 6   refusals {}
--index NF  --section premium            attempted pairs 6   PAIRED 0   no_close_mark_in_window 6
--index BNF --section premium            attempted pairs 6   PAIRED 0   no_close_mark_in_window 6
missing `premium_followon`                                   PAIRED 0   section_absent_or_malformed
```

The pilot refusal is structural, not incidental: the pilot window ends at 15:05 **exclusive**,
so that stream can hold no close mark whatever its cohort — asserted from the production
constants in `theTwoStreamsAreAssembledApartForBothIndicesAcrossARestart`.

**A synthetic pairing is evidence that the integration works in the test.** Phone capture
remains an on-device check: nothing here claims a device produced these bytes, and the fixtures
carry `_provenance.synthetic: true` for the Stage C inventory to read.

### 6.6 Regressions added in v3

| check | where |
| --- | --- |
| the follow-on hook is in the poll, inside the Throwable guard, behind its own control, and makes no request | `PremiumFollowOnCaptureTest.theFollowOnHookIsWiredIntoThePollAndCannotFailIt` |
| the normal export assembly carries the two streams under separate keys, with the pilot untouched | `...theNormalExportAssemblyCarriesBothStreamsApart` |
| a switched instrument key is refused, and the reason is published | `PremiumFollowOnEndToEndTest.aSwitchedInstrumentKeyIsRefusedInTheAssembledExport` |
| an expiry roll is refused at the reopen | `...anExpiryRollIsRefusedInTheAssembledExport` |
| with the research control off, the pilot export is byte-identical to what it was | `...aMissingFollowOnSectionLeavesThePilotExportExactlyAsItWas` |
| the named section pairs 6/6 per index; the pilot section refuses with a reason | `test_next_session_match.PhoneShapedExport` (8 cases) |
