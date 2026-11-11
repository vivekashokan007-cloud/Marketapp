# Empirical direction assignment — status

**Branch:** `review/claude-empirical-direction-20261107` (Commits B+)
**Base:** `755b127a9143e59a48f33dd09a83f94e14eaebcc` (Marketapp main, v2.6.95/b526)
**Last updated:** 7 November 2026

Deliberately based on **verified main, not on Commit A**. Verified: `git merge-base --is-ancestor`
reports Commit A's head `e2c8f7d7` is NOT an ancestor of this branch, so the price-only path can
proceed whether or not the sparse-strike repair is approved.

Per the controlling brief: no main merge, no publish, no version bump, no production DB write, no
authority change. Paper accessible, Real disabled.

---

## Companion branch (Commit A, separate review)

`review/claude-chain-profile-strike-step-20261107` @ `e2c8f7d7499c40c4d78c6a721683e1c7231c7d2d`,
draft **PR #44**. Sparse-strike `chain_profile` repair. Its own STATUS content is in that PR body and
in Checkpoint 1.

## Verified starting point (fetched, not taken from the brief)

| Item | Value | Matches brief? |
|---|---|---|
| Marketapp main | `755b127a9143e59a48f33dd09a83f94e14eaebcc` | yes |
| MarketVivi main | `3f3f46ad01cee8dd5fcdb04817472d15968b53bc` | yes |
| Published release | v2.6.95, `draft=false`, 2026-11-06T11:03:40Z | yes |
| D9 repair on main | `14e32bdc996682cfb24d24be6cbc44f437f45bba` (PR #41) | yes |
| Release PRs | Marketapp #42 merged by Vivek 2026-11-06T10:40:54Z; MarketVivi #30 merged | yes |
| Duplicate release PRs | Marketapp **#43 open, draft, `dirty`**, redundant; MarketVivi #31 unchecked | left alone per §71 |

Python 3.11.18. No kotlinc / Android SDK here: **Kotlin is compile-gated by CI only.**

---

## Stage status

| Stage | State | Evidence |
|---|---|---|
| 0 — capability, base SHAs, source/consumer map, coverage inventory | **done** | below |
| A — sparse-strike repair | R1+R2 corrections applied; CI green at `335c274f`; two further commits pushed | PR #44, 67 tests |
| B — versioned input/output/state contract | implemented + tested | this branch, 48 tests |
| C — observed path and empirical ranks | implemented + tested | this branch, 48 further tests |
| **B1–B5 — Codex correctness blockers** | **all five reproduced, then fixed** | `test_empirical_direction_blockers_b1_b5.py`, 39 tests |
| D — E0/E1 estimators | not started | a **price-only** arm is unblocked by the recovered extracts; see §"Recovered offline inputs" below |
| E — per-poll adapter, flag, resources | not started | |
| F — review package, option/multi-day matrix | not started | |

**Next executable step:** Commit D — E0 (unconditional forward-return distribution per
index/slot/horizon) and E1 (equal-weight nearest neighbour over the fixed five-rank bundle), with
horizon labels at +15/+30/+60 min and session close, chronological walk-forward accounting, and the
neighbour-count arms {10,20,40} selected on inner validation. **E1 is expected to report blocked on
data** in this environment: see the coverage inventory. Commit E (per-poll adapter behind a
default-off flag) must come after D's engine tests.

---

## Codex blockers B1–B5 — reproduced, then fixed

Codex reviewed head `bdf201ce` and reproduced five correctness defects using the module's real
functions. **Every one was real.** Each was reproduced here first, with the figures Codex reported,
before anything was changed; the reproduction script and its before/after output are kept outside the
repository. Regression coverage is `tests/test_empirical_direction_blockers_b1_b5.py` — **39 tests,
34 of which fail against pre-fix `bdf201ce`**. The five that pass pre-fix are characterisation or
companion-invariant tests, not fix-pins, and are labelled as such.

| | Defect, as reproduced | Fix |
|---|---|---|
| **B1** | Only `age_s > max_source_age_s` was tested, so a **negative** age passed. NF 100 accepted at 09:20, then NF 110 stamped **10:00** with decision time **09:25** was accepted: `available=True`, observations 1→2, `last_accepted_source_ts` = 10:00. A wrong-session poll was refused only incidentally, as stale. | A clock contract ahead of any state change: `FUTURE_SOURCE_TIME`, `FUTURE_RECEIVE_TIME`, `SOURCE_AFTER_RECEIVE`, `SESSION_MISMATCH`, plus explicit `INVALID_DECISION_CLOCK` / `INVALID_RECEIVE_CLOCK` for naive or non-datetime clocks instead of raising in the arithmetic. A documented `clock_skew_tolerance_s` (proposed 2.0 s) covers machine disagreement only; sensitivity tested at the boundary. |
| **B2.1** | A snapshot built **as-of 7 Nov** holding a 6 Nov row ranked a **5 Nov** value, rank 100.0. The cutoff was enforced only at construction. | `snapshot_is_usable_for` enforces it at **consumption** too: `SNAPSHOT_SESSION_MISMATCH` / `SNAPSHOT_NOT_STRICTLY_EARLIER`, checked per record, not just on the declared as-of. |
| **B2.2** | Mutating the caller's record changed `snapshot['records']` while `history_hash` stayed `2afb6751a0d7dae2`. A tuple of borrowed dicts is not immutable. | Records are deep-copied through a canonical round-trip, so contents cannot move beneath the hash. |
| **B2.3** | 78 rows from one session with `max_history_sessions=2` kept **2 rows** — the cap counted rows, discarding 76 slots and most cohorts, and over-weighting later dates through retention. | The cap now retains the most recent **distinct sessions whole**, with a separate `max_history_rows` bound. `rows`, `distinct_sessions`, `session_dates`, `excluded_capped_sessions` and `excluded_capped_rows` are all reported. Ordering is fully canonical, so the hash is invariant under input reordering — tested on a capped duplicate population too. |
| **B3** | With observations only at 09:20 and 10:00, the requested `m15` frame had **40.0** actual minutes and `within_tolerance=False`, yet was ranked against a genuine 15.0-minute historical row: **rank 100.0**. Historical eligibility checked only that a feature key existed, and null values were filtered *after* the hash-based duplicate choice, so a null row could evict a valid one. | An out-of-tolerance query frame is kept as labelled **descriptive** evidence (`INCOMPARABLE_FRAME`, `descriptive_only`, actual vs requested minutes) and excluded from the nominal cohort. Historical rows are screened **before** deduplication on availability, tolerance status, actual horizon against the nominal window, feature version and finiteness, with each rejection counted by reason. Unknown compatibility is explicit, never inferred from the label `m15`. The eligible-session denominator and the tolerance used are published in the cohort key. |
| **B4** | Accept 09:20 @ 100.0, resubmit 09:20 @ 102.0 with decision clock 10:20 → `NEW_PREFERRED` then `STALE_SOURCE`, and the returned state held **zero** observations where it had held one: the old observation was popped **before** freshness validation. The tie-break also compared `digest(new_full_poll)` against `digest({'spot': old_value})` — two different shapes. | Validation runs first; the candidate update is computed in isolation and committed only on acceptance, so a rejected event leaves state **byte-identical**. Both sides of a conflict now hash the same canonical triple via `_conflict_key`, and both keys are published. A same-time replacement supersedes the observation only and cannot move the frozen open reference. Arrival permutations tested. |
| **B5.1** | `observations=[['oops']]` passed the shape check and raised `ValueError: not enough values to unpack (expected 3, got 1)`. A non-mapping state raised `AttributeError: 'list' object has no attribute 'get'` while **inferring** the index and session, before any compatibility check. | The complete stored structure is validated — every row a 3-element sequence with a parseable timestamp, finite positive spot, string-or-None field, chronologically non-decreasing — plus the reference, extrema, prior-session and contract blocks. An incompatible state is a recorded `COLD_START` + `STATE_STRUCTURE_INVALID`, never a raise. The index/session inference no longer dereferences a non-mapping state. |
| **B5.2** | With cap 3 and causal prices 100 → 120 → 110 → 108 → 105, `extrema.high` became **110** although the real high since the reference was **120**: extrema were recomputed from the rolling buffer, so trimming erased accepted history. | `state['path_extrema']` keeps compact **persistent fixed-reference** extrema and post-extreme summaries, monotone and independent of the buffer, so a dropped observation cannot lower a high. `observations_trimmed` is counted and surfaced as `coverage_complete: false` + `TRUNCATED_COVERAGE`, so a truncated path is never advertised as session-complete. Bounded-state and restart-across-the-cap tests included. |

**Root cause of B3 worth recording.** The hand-built history fixture carried only `log_return`, so
eligibility was never asked to screen anything. The fixture now carries what `observed_path`
actually emits, and `TestHistoryFixtureMatchesRealFrames` asserts it against real observer output so
it cannot drift back — the same discipline the Kotlin fixture overclaim earned earlier.

**Until Codex re-reviews these, no rank from this module may be used for model selection or any
direction-quality claim.**

---

## Codex revision round R1–R5 — reproduced, then fixed

Codex reviewed `de641b0` and reproduced five residual defects that survived the B1–B5 round.
**Every one was real.** Each was reproduced here first, with the figures Codex reported, before
anything changed. Regression coverage is `tests/test_empirical_direction_revision_r1_r5.py` —
**37 tests, 32 of which fail against pre-fix `de641b0`** (19 failures, 13 errors). The five that pass
pre-fix are carried-over B-round behaviour or no-regression guards, not R-fix pins.

| | Defect, as reproduced | Fix |
|---|---|---|
| **R1** | 09:20=100 reference, then 09:25=120 then 09:25=110 → `NEW_PREFERRED`, final observation 110, retained high **120**; reverse order → `EXISTING_PREFERRED`, final observation 110, retained high **110**. Identical final observation, different path — the superseded 120 stayed folded into `path_extrema`. | The accumulator now covers only the **committed prefix**: every accepted observation except the newest, which is the only one a same-time event can still replace. A point is folded when a **strictly later** observation arrives, and the reported extrema fold the replaceable tail on top without persisting it. Tested for path-state equality across permutations, at peaks and troughs, across trimming, and across a serialise/reload restart. `processed_ids` still differs between orders — correctly, because different events were *accepted* — and a test shows both orders converge when the superseded value is re-presented. |
| **R2a** | `session_to_date.path_efficiency` = **1.048799393197891** while still reported available and within tolerance: the net return used the frozen 100 reference, the total variation only the retained 110→108→105. | Not clipped. Coverage is computed per frame and the metric is **withheld** with `path_efficiency_reason = INCOMPLETE_FRAME_COVERAGE` when the frame's anchor is no longer in the buffer. Every frame publishes `coverage_complete` and `observations_trimmed`. A truncated **historical** frame is also excluded from a cohort (`rejections.incomplete_coverage`). |
| **R2b** | `references.first_observed_reference` moved to **110.0 at 09:30** although the first accepted observation was 100.0 at 09:20: it read `observations[0]`, the start of the rolling buffer. | `state['first_observed']` is captured once at the first accept and never revised — except when the first observation is itself superseded by a same-time replacement, which has its own test. The buffer's start is reported separately as `buffer_start_reference` with `is_session_first` and the trim count. |
| **R3** | From a valid one-poll state: `observations_trimmed='oops'` → ValueError; `path_extrema` without `low_ts` → KeyError; `observations=7` on the **disabled** path → TypeError. | Every field actually read is validated: all required `path_extrema` keys, post-extreme value/timestamp pairs as pairs, the ordering relationships (high ≥ low, both bracketing the reference), non-negative integer counters, and `first_observed`. Malformed state is an explicit `COLD_START` + `STATE_STRUCTURE_INVALID`. The disabled branch guards `len()` and reports `STATE_STRUCTURE_INVALID` instead of raising. A round-trip test asserts the guard does not reject what this module itself writes. |
| **R4a** | A historical m15 frame carrying only `elapsed_min=15` and `log_return=.01` was accepted as `[.01]` with **zero rejections** — flags were checked only when exactly `False`, and a missing frame version was explicitly permitted. So unknown compatibility was implicitly accepted, the opposite of what I claimed. | A record must declare `record_version` and `feature_version`; availability and tolerance must be **positively** evidenced (`is True`, not "not False"); a missing horizon is `unknown_compatibility`, counted separately from a known-and-wrong `horizon_mismatch`. Because `observed_path` stamps its version at the **top level** and not per frame, requiring a frame-level version would demand a field no producer emits — so the new `history_record()` adapter carries it at the record level and the screen checks it there, with a **real output → record → snapshot → rank round trip** test. |
| **R4b** | `anchor_tolerance_min=0` reported and applied **2.5**, because `value or default` replaces a valid zero; the query side used the real 0. | `_cfg_num` falls back only on a **missing or unusable** value, so a configured zero survives. Both sides now read the same setting; tested for zero, missing, malformed and negative. |
| **R5** | `snapshot['records'][0]['frames']['m15']['log_return']=99` mutated the snapshot in place with `history_hash` unchanged, and consumption read 99.0. A tuple of copied mutable dictionaries is not immutable. | Records are stored as **canonical JSON strings**, which cannot be mutated in place, and `snapshot_rows()` **re-verifies the hash** before parsing, so wholesale substitution of the tuple or of any accounting field is refused at consumption with `SNAPSHOT_INTEGRITY_FAILED`. Both boundaries are documented and tested; reorder invariance and cap accounting are re-asserted unchanged. |

**B1 follow-up.** The 2.0 s skew allowance is recorded as a **proposed data-quality setting**, not
evidence of machine synchronisation. A tolerated-ahead observation publishes
`source_ahead_of_decision_s`, and a test shows `clock_skew_tolerance_s=0` — what price-only
historical replay should use — refuses any lookahead at all. Receive time stays
`RECEIVE_TIME_UNKNOWN` when it is unknown; a tolerated stamp is never described as verified
availability at the decision time.

---

## Session-frame comparability + feature-specific coverage (Codex §5 follow-up)

Two defects Codex reproduced from real observer output at `2da6079`, both confirmed here first.

**S1 — `session_to_date` pooled incompatible anchor horizons.** It carries `nominal=None`, so the
rolling-window duration check never applied to it. Reproduced exactly: a 4 Nov history with
reference 09:20 (session elapsed **20 min**) ranked a 5 Nov query with reference 09:25 (elapsed
**15 min**) at the same 09:40 slot — `available=True`, **rank 50.0**.

Comparability for a non-nominal frame is now established from the **actual elapsed duration** and
the **reference kind**, by the same rule on both sides:
`SESSION_FRAME_COMPARISON_VERSION = session_frame_comparability_v1_anchor_kind_and_actual_elapsed`.
A rolling frame keeps `comparability_basis: NOMINAL_WINDOW`; a session frame gets `ACTUAL_ELAPSED`
with the query's own elapsed as the target. An incompatible pairing is withheld as
`INCOMPARABLE_SESSION_FRAME` with the measurement retained as labelled evidence; with no elapsed
reading at all the rank is withheld as `SESSION_FRAME_BASIS_UNKNOWN` rather than pooled. A
mismatched `anchor_kind` is never borrowed. No forecast or directional threshold was invented to do
this.

**S2 — coverage policy was one rule per frame, applied asymmetrically.** A historical row with
`coverage_complete=False` was discarded outright while the query kept its own endpoint-return rank.
That discards valid endpoint returns: an **endpoint** return needs only its two endpoints and is
valid with interior observations missing; a **path-shape** measure is not, because its denominator
sums the observations actually present.

The requirement now belongs to the **feature**
(`FEATURE_EVIDENCE_VERSION = feature_evidence_v1_endpoint_vs_path_coverage`) and the same rule is
applied to query and history:

| feature | interior coverage required |
|---|---|
| `log_return`, `log_return_per_min` | no — endpoints suffice |
| `path_efficiency` | yes |
| anything unlisted | yes (fails closed) |

Unknown coverage is not treated as complete. The cohort key publishes
`comparability_basis`, `target_elapsed_min`, `anchor_kind`, `requires_interior_coverage` and both
contract versions, so a rank is reproducible without guessing the rule.

**One of my own tests encoded the wrong policy.** `test_a_truncated_historical_frame_is_excluded_from_a_cohort`
asserted that a truncated historical frame is excluded from a `log_return` cohort — the very
behaviour Codex corrected. It is replaced by `test_coverage_is_required_per_feature_not_per_frame`,
which asserts both halves. Three of my first attempts at these tests also passed for the wrong
reason: the fixture frame carried no `path_efficiency` key, so rows were rejected earlier as
`frame_absent` and the coverage gate never ran.

Tests: `test_empirical_direction_revision_r1_r5.py` grew to **46**, of which **9 fail against
pre-fix `2da6079`** (5 failures, 4 errors) — the three coverage-policy tests and six session-frame
tests. The tenth, `test_the_two_real_output_elapsed_durations_are_what_codex_reported`, passes
pre-fix because it characterises the condition rather than pinning the fix. All observer suites
**184 OK**.

---

## Commit B — what was built

`app/src/main/python/index_empirical_direction_v1.py`. Pure: no network, no wall clock, no database,
no notification, no global mutable state. **Not imported by `brain.py` or any Kotlin** — verified by
grep — so it cannot affect a shipped decision. Commit E wires it behind a default-off flag.

Interfaces as specified: `assess_poll`, `build_history_snapshot`, `replay_session`.

Output envelope groups: identity, status, references, observed_path, empirical, forecast, provenance,
operational. The three unimplemented groups report `available: false` with `NOT_IMPLEMENTED` rather
than returning zeros, so absence cannot be mistaken for a measurement of nothing.

### Deliberate reuse, with the reason stated

Timestamp and spot resolution come from `index_authority_v1` (`parse_ts`, `poll_time`, `poll_spot`,
open-reference window constants) rather than being reimplemented. The observer must see exactly the
inputs the shipped authority sees, or any disagreement would be an artefact of two parsing dialects
instead of a real difference. That is not hypothetical: an earlier repair in this repository existed
precisely because a re-implemented timestamp identity diverged from its consumer.

### State machine

Keyed by schema/algo/config-hash/index/session. Behaviours, each tested:

| Condition | Result |
|---|---|
| no previous state | normal fresh start, **not** flagged as a cold restart |
| present but corrupt or incompatible | **explicit** `COLD_START` + specific reason; no reconstructed morning |
| session change | `SESSION_ROLLOVER`: intraday reset, prior-session reference retained |
| contract change | `CONTRACT_ROLLOVER` recorded |
| duplicate poll | idempotent; adds no weight |
| out-of-order arrival | `OUT_OF_ORDER_EXCLUDED`; earlier records byte-identical |
| same source time | documented `SOURCE_PRIORITY` (keyed spot outranks generic), then stable payload hash; conflict recorded |
| source older than `SPOT_MAX_AGE_S` | `STALE_SOURCE`, not accepted |
| caller's state | copied, never mutated |

**A bug found and fixed while testing:** `replay_session` originally replaced a non-dict state with a
fresh one *before* `assess_poll` saw it, so a corrupt state produced a clean-looking record and the
cold start vanished. `assess_poll` now owns that decision and records it.

### Previous close is labelled, not invented

A retained prior-session reference is published with `is_last_poll_proxy: true` — it is the last
observation of the previous session, not a verified exchange close. Absent, it is
`MISSING_PREVIOUS_CLOSE`.

### Bounds

`MAX_SESSION_OBSERVATIONS` 120, `MAX_PROCESSED_IDS` 256, `MAX_HISTORY_SESSIONS` 400,
`MAX_OUTPUT_BYTES` 16384. Measured envelope size on a 5-poll session: **1421 bytes**. No NaN or
Infinity can reach the output; a walker test asserts every float is finite.

### Commands

```
PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_empirical_direction_contract.py
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_*.py'
```

Commit B suite **48 tests OK**; full Python suite **1954 tests OK**, 0 failures, 2 skipped.

---

## Commit C — observed path and empirical ranks

`observed_path` (§8.1, §8.2) and `empirical` (§8.3) are now real. `forecast` still reports
`NOT_IMPLEMENTED`. `ALGO_VERSION` moved to `index_empirical_direction_v1_path_ranks`.

### Frames

Log returns only, `log(S_t/S_a)`, for strictly positive finite prices. Windows 15/30/60 minutes plus
session-to-date, plus a separate previous-close frame. These are **diagnostic horizons, not proven
windows and not thresholds**.

The anchor is the **latest observation at or before** the requested anchor time. A later observation is
never chosen merely because it is closer in absolute terms — a frame may not look into its own future,
and a test pins exactly that (09:44 wins over 09:46 for a 09:45 request).

`ANCHOR_TOLERANCE_MIN` is a **proposed** 2.5 minutes, in configuration rather than buried in code. A
frame outside it is still returned but relabelled `within_tolerance: false` with its **actual**
`elapsed_min`, and the poll records `provenance.frame_tolerance_exceeded`. So a 10-minute observation
can never silently stand in for a 30-minute frame. Sensitivity is tested at 0.5 and 10.0 minutes
against an anchor 22 minutes back.

Each frame reports raw log return, return per minute, path efficiency (null with a reason on a flat or
single-observation span), observation count, max gap, and its own window extremes — which are kept
**separate** from the reference-anchored extrema.

### Retained excursion and retracement

Per §8.2, in log space, with the reference included in its own path. Ratios are **not clipped to
[0,1]**: crossing back through the reference exceeds 1 and that is meaningful. A zero denominator
yields null with `NO_PRIOR_EXCURSION`, never 0.

The brief's R2 blind spot reproduces: A 100 → H 102 → now 100.3 gives an up-retracement of
**0.848732 (84.87%)**, matching "about 84.9% in log terms". The exact **multiplicative** mirror
(100 → 100/1.02 → 100/1.003) gives a down-retracement equal to it to 10 decimal places — a true mirror
rather than an approximate one.

Both sides are retained, extrema carry timestamps and chronological order (`HIGH_FIRST` /
`LOW_FIRST` / `SAME_OBSERVATION`), and a post-peak trough appears **only after it is observed**, stamped
at its own observation time and never backdated to the peak. A high alone does not declare an uptrend:
a test asserts no directional label (`BULL`, `BEAR`, `TREND_UP`, `STRONG`, `PULLBACK`, …) appears
anywhere in the record.

### Empirical midrank

`rank(x) = 100 * (count(< x) + 0.5 * count(== x)) / N`, exactly as specified, on stored canonical
values with no prior rounding. `N = 0` yields null with `INSUFFICIENT_HISTORY`; `N = 1` yields a
descriptive rank flagged `low_support`. Signed and magnitude ranks are computed separately.

**One eligible observation per earlier session per query frame.** 78 correlated polls from one session
collapse to 1, with `duplicates_collapsed: 77` reported; the collapse is deterministic under input
reordering. The cohort key (index, feature, window, slot) is published, with counts for
`rejected_other_index` and `rejected_slot_or_frame`, plus the snapshot's `history_hash` so a rank can
be reproduced independently. A wrong-index cohort is `UNSUPPORTED_COHORT`, never borrowed.

### Measured

Envelope 4092 bytes on a five-poll session with excursion and frames populated; cap is 16384. Commit C
suite **48 tests**; Commit B suite still **48 tests** (one assertion updated: it had pinned all three
groups as `NOT_IMPLEMENTED`, which Commit C makes stale for two of them). Full Python suite
**2002 tests OK**, 0 failures, 2 skipped.

Two of my own test fixtures were wrong and the module was right: a gapped path made a 15-minute anchor
legitimately fall back past an early spike, and a 40-minute-old anchor sat outside any plausible
tolerance. Both were rebuilt rather than loosened.

## Source-to-consumer map — current direction authority

At `755b127a…`. `compute_direction` reads **only** these; no percentile, skew, PCR, OI, breadth or ML
value reaches the directional verdict:

| Input | Producer / resolution | Timestamp semantics | Missing behaviour | Live consumer |
|---|---|---|---|---|
| Spot series | `poll_spot` → `nfSpot`/`bnfSpot`, else `nf`/`bnf`; finite > 0 | poll time, IST | `SPOT_MISSING` / `SPOT_STALE` | `z_open`, `z_30`, ER |
| Poll timestamps | `poll_time` → `poll_ts`/`pollTs`/`ts`, else `t`/`time` | IST; first field that **parses** | row excluded | series membership, anchor age, ER gaps |
| `scale.value` (m) | `compute_scale`, frozen at open reference; D9 may use next expiry | observed once in [09:20,09:35) | non-`OK` ⇒ `UNAVAILABLE` | denominator of both z-scores |
| `scale.status` | same | — | `SCALE_*` codes | gates the whole verdict |
| `decision_dt`, `receipt_dt`, `analysis_dt` | caller | trusted clocks, latest wins | `DECISION_TIME_INVALID`, `CLOCK_SKEW` | staleness, future-poll exclusion |

Constants: `OPEN_REF_WINDOW [09:20,09:35)`, `Z_OPEN_THRESHOLD 0.50`, `STRONG_THRESHOLD 1.0`,
`Z30_LOOKBACK_MIN 27.5`, `Z30_WARMUP_MIN 10.0`, `Z30_ANCHOR_MAX_AGE_MIN 45.0`, `ER_MAX_GAP_MIN 12.0`,
`RANGE_ER_MAX 0.30`, `SCALE_M_BOUNDS (0.002, 0.05)`, `SQRT_2_OVER_PI 0.79788`.

Writer: `brain.compute_index_authority`, sole writer of `by_index`. Persistence:
`index_authority_store` at `<filesDir>/index_authority_v1/<session>.json`; scales write-once except the
D9 recoverable-failure → OK upgrade. **`decision_identity` hashes `direction` wholesale**, so any added
field changes every `decision_id`, which carries admission lineage (`entry_decision_id`) and history
record keys. That is the hard constraint on Commit E: the observer payload must be a separate object,
never a new key inside `direction`.

**Not yet traced** (needed before Commit E): notification ownership, candidate-ranking and admission
read paths, PWA display consumers.

## Data-coverage inventory — and the gaps

| Need | Available here | Gap |
|---|---|---|
| Per-poll spot series | device `authority_doc.history` for 2–4 Nov (78 records/index/session) | 5 Nov export never reached this session; only its 09:30 slot, via the brief |
| Multi-session history for ranks | app retains a **100-row rolling** `poll_history` only | **earlier-session cohorts cannot be built from this environment** |
| Raw `allStrikes` per observation | none | blocks Commit A incidence counting |
| Option quotes with bid/ask + quote time | none offline | blocks the options/feasibility arms |
| Greeks, previous-OI identity, breadth | none offline | those bundles are **unavailable**, not "tested and negative" |
| Exchange calendar | `NSE_HOLIDAYS_2026` in `MarketWatchService.kt`; `2026-11-10` confirmed a holiday | Python-side calendar authority to confirm at the checked-out SHA |

**Consequence for Commit D:** E1's nearest-neighbour cohorts are **blocked on data, not on
implementation**. E0/E1 will be built and unit-tested against synthetic and any supplied real extracts,
and E1 marked blocked with the exact missing fields and dates. Nothing will be fitted on manufactured
history.

---

## Recovered offline inputs — manifest and coverage audit

Supplied as embedded appendices in the Codex Checkpoint 1 review and recreated under a **private
offline input directory outside this repository**. The data files are deliberately **not committed**
and nothing was uploaded to production storage; only this manifest is recorded here.

All seven files verified **byte-exact** against their stated byte count and SHA-256:

| file | bytes | SHA-256 (first 16) |
|---|---|---|
| `old/spots_enc_20260901_20261030.txt` | 34573 | `68d5146326f91ce9…` |
| `old/chainA_window_20260901_20261030.txt` | 28413 | `e841f0756aeaed7d…` |
| `new/spots_20261102_20261105.txt` | 7658 | `e36639a1e2965d0f…` |
| `new/chainA_20261102_20261105.txt` | 2775 | `61765783c2245721…` |
| `new/phone_dirs_20261105.txt` | 704 | `9446a700b0766b13…` |
| `scripts/replay_all_e5e0fe9.py` | 3671 | `9193932a2657de15…` |
| `scripts/recon_20261105.py` | 4235 | `6d1074cb0fbcb4d3…` |

Checksums establish **these copies**, not original raw-source completeness — Codex's own caveat, and
it stands. The supplied scripts were **read, not run**; the decoder and auditor used here were written
separately and are recorded as adapted code.

**Decode, independently validated.** The old file is `date slot:nfDelta:bnfDelta …`, 1-based slots,
first listed slot absolute and all later ones cumulative, every value ×20. Validation: for **all 90**
`chainA` rows the stated 09:20 spot matches the decoded value at **slot 1** exactly (1e-6), across
both indices and both eras. Independent row count for 01 Sep–30 Oct is **2,969**, matching the count
Codex stated without my having been told how to decode it.

**Clock mapping is reconstructed, not a source fact:** slot 1 = 09:20 and slot *n* = 09:15 + 5*n* min,
derived from that match. Recorded as derived wherever it is used.

| | sessions | observed slot rows | internal gaps |
|---|---|---|---|
| 01 Sep – 30 Oct (old) | 41 | 2,969 | 193 |
| 02 Nov – 05 Nov (new) | 4 | 298 | 0 |
| **total** | **45** | **3,267** | **193** |

### Label inventory — corrected twice, and what it does and does not establish

**First retraction (mine, caught by Codex).** An earlier revision named 2026-09-28, 2026-10-07 and
2026-10-08 as the sessions to exclude. Those three are exactly what an **unstated** "intraday
coverage below 80%" threshold removes, and no such rule had been stated — an ad hoc
favourable-sample filter. Re-deriving it also exposed a slot-convention bug of mine: the old files
omit slot 0 and the new ones include it, so a fixed 72-slot denominator gave coverage above 1.0 for
later sessions.

**Second retraction (also mine, also caught by Codex).** Having replaced the threshold with a
per-label rule, I then claimed 2026-09-28 "can support no label" and kept it as the one session-level
exclusion. **That is false.** Its observed slots are `1, 5, 10, 20, 26, 46, 67`, and slots
**20 → 26 are a valid +30-minute endpoint pair** (10:55 → 11:25). It has no +15 and no +60 pair, but
it does have that one. **There is therefore no session-level exclusion at all** under the endpoint
rule: 09-28 is retained for the horizon it supports, and its missing +15/+60 labels are reported as
missing.

**The rule.** A label at horizon *h* from entry slot *s* is admitted only when **both** endpoints
*s* and *s + h/5* are observed in that session, inside the declared nominal research window slots
1–75. No forward fill, no fabricated price, no neighbouring-slot substitution.

| horizon | admitted pairs | nominal grid | sessions contributing | missing entry | missing exit | missing both | extract-end censored |
|---|---|---|---|---|---|---|---|
| +15 min | 2,931 | 3,240 | **44** | 100 | 104 | 93 | 12 |
| +30 min | 2,804 | 3,105 | **45** | 98 | 102 | 89 | 12 |
| +60 min | 2,549 | 2,835 | **44** | 96 | 100 | 78 | 12 |

The `nominal grid` column is the **full nominal slot grid**, including unobserved and truncated
opportunities. It is not an achievable target count. The session column counts sessions contributing
to **that horizon**, not all dates in the input — 09-28 is the only session absent from +15 and +60.

**What this does NOT establish**, per Codex, and worth stating because an endpoint rule is easy to
over-read: it establishes a fixed-horizon **underlying return** only. It says nothing about
intervening path coverage, so excursions, realised variance, barriers and managed exits each need
their own feature and label eligibility before any of them is computed.

**Index separation.** The spot extracts carry NF and BNF on **one shared slot grid**, so a slot pair
is counted once per session. These are not per-index session counts; NF and BNF values and all
downstream results stay separate, and the two are never added as independent sessions.

**Repeated values — corrected label.** I previously called these "staleness". Equal consecutive
prices do **not** establish a stale feed, and nothing in this extract carries an observation time or
feed status to distinguish the two. They are **repeated values with unknown freshness**, kept
separate from independently evidenced stale timestamps, of which there are none here. Legitimate
unchanged prices are not excluded by assumption. 168 repeated runs start at slot ≥ 70; **5** pairs
occur mid-session across all 45 sessions (2026-09-17 NF 12:30–12:35, 2026-09-21 NF 09:55–10:00 and
10:55–11:00, 2026-09-29 NF 11:40–11:45, 2026-11-03 BNF 13:00–13:05).

**No verified close.** The reconstructed slot clock and the padded tail do not establish the official
session close, nor the original observation times. Slots 1–75 are a **declared nominal research
window**, not proof of an exchange timestamp, and a final observed poll is not a verified close. No
session-close target is produced from this convention alone. Source and derived clocks stay
distinguished, including across the differing old and new slot conventions.

**5 November, explicitly.** 64 slots, ids 0–63, **zero internal gaps**, reconstructed 09:15 → 14:30.
Slots 64–77 (14:35 onward) are **absent** — the extract stops mid-session, so no 5 Nov label with a
horizon ending after 14:30 can be matured from it. `phone_dirs_20261105.txt` carries exactly 64
timestamped entries over the same 09:15–14:30 range, so the two align 1:1; its codes are
`UNUN`×3, `NENE`×29, `NEBU`×19, `BUBU`×12, `BUNE`×1 (first two letters NF, last two BNF;
UN/NE/BU). That series is a **phone-reported direction label**, not an independent price source.

**Repeated values — corrected label.** I previously called these "staleness". Codex is right that
equal consecutive prices do **not** establish a stale feed: an index can legitimately print the same
value twice, and nothing in this extract carries an observation time or a feed status to distinguish
the two. They are recorded as **repeated values with unknown freshness**, kept separate from
independently evidenced stale timestamps, of which there are none here. Legitimate unchanged prices
are not excluded by assumption.

168 repeated runs start at slot ≥ 70, a systematic tail (72–74 and 75–77 repeat in most sessions)
consistent with a fixed pad past the reconstructed close. Only **5** repeated pairs occur
mid-session across all 45 sessions (2026-09-17 NF 12:30–12:35, 2026-09-21 NF 09:55–10:00 and
10:55–11:00, 2026-09-29 NF 11:40–11:45, 2026-11-03 BNF 13:00–13:05).

**No close label may be matured from the tail.** The repeated tail values and the reconstructed slot
clock do not establish the official session close, nor the original observation times. A final
observed poll is not a verified close, and a frozen or padded tail is not a close print. Source and
derived clocks stay distinguished throughout, including across the differing old and new slot
conventions.

**What the opening chain windows are NOT.** 90 rows, **09:20 only**, 10 legs each, fields limited to
`strike, ceBid, ceAsk, peBid, peAsk`. No IV, no Greeks, no OI, no volume, and **no intraday series**.
NF calendar DTE spans 0–6, BNF 0–28; 12 rows are own-expiry-day (`date == expiry`), 10 NF and 2 BNF.
Any option-leg feature after 09:20 is **unsupported** by this evidence, and an option-based arm cannot
be built from it.

**Consequence for D.** A **price-only** E0/E1 arm is feasible on **all 45** recovered sessions with
no session-level exclusion (per-horizon contributing counts 44 / 45 / 44 above) / 3,267 observations,
with
explicit as-of and label-maturity limits. An option-priced arm is **not**. I will not claim the
supplied replay script reproduces phone parity without adapting and validating its clock and
interface assumptions, and no estimator is fitted until the observer's B1–B5 causal defects are fixed.

---
## Not claimed

- No direction-quality, forecast or profitability claim anywhere in A or B.
- Commit A's historical incidence: **unknown** (needs raw `allStrikes` per observation).
- No phone evidence: no device in this session. CI and a release do not prove installation.
- Kotlin not compiled locally.
