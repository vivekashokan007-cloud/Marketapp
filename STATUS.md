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
| A — sparse-strike repair | **implemented + tested**, awaiting Codex | PR #44, 29 tests |
| B — versioned input/output/state contract | **implemented + tested** | this branch, 48 tests |
| C — observed path and empirical ranks | **not started** | envelope reports `NOT_IMPLEMENTED` |
| D — E0/E1 estimators | not started | E1 blocked on data, see below |
| E — per-poll adapter, flag, resources | not started | |
| F — review package, option/multi-day matrix | not started | |

**Next executable step:** Commit C — fill `observed_path` (log-return frames at 15/30/60 min and
session-to-date, causal reference extrema, excursion/retracement per §8.2) and `empirical` (midrank per
§8.3), with mirrored path tests. The envelope, state machine and reason codes are already in place.

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

## Not claimed

- No direction-quality, forecast or profitability claim anywhere in A or B.
- Commit A's historical incidence: **unknown** (needs raw `allStrikes` per observation).
- No phone evidence: no device in this session. CI and a release do not prove installation.
- Kotlin not compiled locally.
