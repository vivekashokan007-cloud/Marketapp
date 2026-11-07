# Empirical direction assignment — status

**Branch:** `review/claude-chain-profile-strike-step-20261107` (Commit A only)
**Base:** `755b127a9143e59a48f33dd09a83f94e14eaebcc` (Marketapp main, v2.6.95/b526)
**Last updated:** 7 November 2026

This file exists so an interrupted session resumes from a record rather than repeating work.
Per the controlling brief: no main merge, no publish, no version bump, no production DB write, no
authority change. Paper accessible, Real disabled.

---

## Verified starting point (fetched, not taken from the brief)

| Item | Value | Matches brief? |
|---|---|---|
| Marketapp main | `755b127a9143e59a48f33dd09a83f94e14eaebcc` | yes |
| MarketVivi main | `3f3f46ad01cee8dd5fcdb04817472d15968b53bc` | yes |
| Published release | v2.6.95, `draft=false`, published 2026-11-06T11:03:40Z | yes |
| D9 repair on main | `14e32bdc996682cfb24d24be6cbc44f437f45bba` (PR #41) | yes |
| Release PRs | Marketapp #42 merged 2026-11-06T10:40:54Z; MarketVivi #30 merged | yes |
| Duplicate release PRs | Marketapp **#43 open, draft, `mergeable_state: dirty`** (branch `release/2.6.95-d9`), now redundant; MarketVivi #31 unchecked | left alone per brief §71 |

Python 3.11.18. No kotlinc / Android SDK in this environment: **Kotlin is compile-gated by CI only.**

---

## Stage status

| Stage | State | Evidence |
|---|---|---|
| 0 — capability, base SHAs, source/consumer map | **done for direction**; coverage inventory now extended by the recovered extracts | §"Source-to-consumer", §"Recovered offline inputs" |
| A — sparse-strike repair | **implemented + tested; R1 and R2 corrections applied; PR #44, CI green at `335c274f`** | this branch |
| A′ — metadata channel removal | **done** | commit `89e70d6` |
| A″ — strike-representation hardening | **done, separable; premise retracted** (see the RETRACTED section) | this commit |
| B–C — observer, schema, causal path/ranks | **pushed; PR #45 (draft); five Codex blockers B1–B5 open** | branch `review/claude-empirical-direction-20261107` @ `bdf201ce` |
| D — E0/E1 estimators | not started; **unblocked on price-only data** by the recovered extracts | §"Recovered offline inputs" |
| E — per-poll adapter, flag, resources | not started; needs D's engine tests **and** admission/ranking/notification/display tracing | only the verdict path is traced so far |
| F — review package, option/multi-day matrix | not started | |

**Next executable step:** fix observer blockers B1–B5 on the B+C branch, in that order, each
reproduced as a failing test before the fix. B1 (future / session-mismatched observations) is first
because B2's causal-cutoff enforcement depends on the same clock contract. No estimator fitting and
no direction-quality claim until those land — Codex's instruction, and the ranks are wrong until then.

Commit A's branch is **not** an ancestor of the direction branch, so the price-only path proceeds
whether or not the repair is approved (`git merge-base --is-ancestor` verified).

---

## Commit A — what was done

**Defect** (`brain.py:chain_profile`, pre-repair line 5189):

```python
step = (all_k[1] - all_k[0]) if len(all_k) > 1 else (100 if spot > 30000 else 50)
```

`all_k[1] - all_k[0]` is the gap between the two **lowest** strikes, which on an NSE monthly ladder is
the sparse far wing. `step` sets the IV sampling distances (`otm_dist` = 0.5σ, `otm_dist2` = 1.0σ), so
a BNF monthly sampled `atm ± 1500` instead of `± 200/400`, read the unpopulated outer wings, and
produced `ivSkew = 0.0`, `ivSlope = 0.0`. This is the same formula the 2026-09-01 candidate-generation
bugfix replaced via `_infer_strike_step`; `chain_profile` was never migrated.

**Reproduced before editing.** `legacy_step(SPARSE_LADDER, 56000) == 1500`, and the strikes at
`atm ± 1500` carry no `iv` key at all in the fixture.

**Repair.** New `infer_strike_interval(all_strikes, atm)` plus
`_normalise_strike_ladder`. `_infer_strike_step` is **unchanged** — candidate generation and the
teaching band call it, and altering it here would mix two changes.

Why not reuse the helper directly, as the brief required me to justify: it returns `min(gaps)` over the
whole ladder. Its docstring claims "modal/minimum" but the code is a pure minimum, so **one anomalous
narrow gap anywhere silently sets every sampling distance**, and a global minimum need not be a local
interval. `infer_strike_interval` instead takes the **modal gap near the money** (±6 strikes around
ATM, where these features actually sample), returns an explicit status, and records
`helper_value` / `helper_agrees` so a disagreement is visible rather than absorbed.

Rule order (v3): near-ATM modal gap → global modal gap when ATM is unknown. There is no metadata
override — see the v3 contract below. A ladder with no unique repeated gap returns `AMBIGUOUS` with a
specific reason rather than inventing a number. It takes **no spot argument** — spot must not infer
ladder geometry.

`chain_profile` keeps an **explicitly isolated legacy fallback** (`100 if spot > 30000 else 50`),
reached only when the interval is unavailable or ambiguous, so existing consumers still get a value
where they previously had one. New additive field `strikeIntervalEvidence` records
`value/status/reason/source/applied/legacy_fallback_used/helper_value/helper_agrees/version`, so new
evidence can exclude the field instead of inheriting the fallback.

**A bug I introduced and caught here:** the normaliser works in float, so the interval came back
`100.0`, making `atm + otm_dist` a float and `str()` produce `"56200.0"` — missing a strikes dict keyed
`"56200"`. Integral intervals are now narrowed to `int`.

### RETRACTED: the float-ATM claim, and what actually happens

An earlier revision of this file, and the Checkpoint 1 handoff built on it, said that `atm` reaches
`chain_profile` as a **float** in production — because `OptionChainBrainFormatter` declares
`var atmStrike = 0.0` and keys strikes `strike.toInt().toString()` — and therefore that **Commit A
might be inert in production**. I raised that as the most important open decision, and Codex
authorised a repair commit on the strength of it.

**That conclusion was wrong.** The Kotlin types are as described, but I stopped at the producer and
never followed the payload across the language boundary. The boundary normalises it:

- `MarketWatchService` passes `ctxObj.toString()` to `brain.analyze`, which does
  `ctx = json.loads(context_json)`. Every number is re-rendered as JSON **text** first.
- Both org.json implementations in use print an integral double as an integer literal.
- The committed golden formatter output,
  `app/src/test/resources/contracts/x1_upstox_formatted_golden.json`, shows `"atm":24650` and
  `"allStrikes":[24250,24300,…]`. After `json.loads` these are Python **ints**, and no strike key
  contains a decimal point.

So the live request is `int + int`, `str()` of it matches the integer-string key, and **the interval
repair does take effect on production input.** Measured four-way matrix on the brief's sparse fixture,
keyed exactly as the producer keys it:

| payload | lookup | `ivSkew` | `ivSlope` | `netDelta` | `avgTheta` | `avgVega` |
|---|---|---|---|---|---|---|
| real (int), old interval | old | 0 | 0.0 | 0.11 | 6.0 | 16.0 |
| real (int), **new interval** | old | **2** | **1.0** | 0.11 | 6.0 | 16.0 |
| real (int), old interval | new | 0 | 0.0 | 0.11 | 6.0 | 16.0 |
| real (int), **new interval** | new | **2** | **1.0** | 0.11 | 6.0 | 16.0 |
| hypothetical Double, old interval | old | 0 | 0.0 | **0.0** | **0.0** | **0.0** |
| hypothetical Double, new interval | old | 0 | 0.0 | **0.0** | **0.0** | **0.0** |
| hypothetical Double, old interval | **new** | 0 | 0.0 | 0.11 | 6.0 | 16.0 |
| hypothetical Double, **new interval** | **new** | **2** | **1.0** | 0.11 | 6.0 | 16.0 |

On the real payload the two lookups agree in both interval columns, and a full-field comparison of
every pre-existing profile field is identical — **the lookup repair changes nothing in production.**
Only on the un-serialised Double shape does it matter, and there both repairs are needed: neither
alone recovers the features.

**Consequence for the representation repair.** It is **hardening, not a prerequisite**, and it is in
its own commit so it can be rejected without touching the interval repair. It still fixes one real
reachable defect: `get_strike` called `int(k)`, which **raised** on a NaN, and `allStrikes` entries
are handed to it unfiltered — so a NaN in the ladder aborted the whole profile with
`ValueError: cannot convert float NaN to integer`. That now degrades to "row not found" and the
profile builds from the strikes that are real.

What the repair does: `_strike_key_candidates` enumerates the spellings of one strike (int,
integer-string, float, float-string) in a documented priority order; a **genuinely fractional** strike
is never truncated onto a neighbouring integer contract; no epsilon tolerance is used, because
integral values below 2⁵³ are exact and the `atm ± n·step` arithmetic is exact for them, so a
tolerance could only match a strike that was not asked for; invalid, non-finite and non-numeric
requests return `None` instead of raising. Truthiness-based selection is deliberately **unchanged**,
so a present-but-falsy row still reads as absent exactly as before.

**Malformed-input scope, narrowed.** The helper normalises its own private ladder, but `chain_profile`
keeps using the original `all_k` for zone iteration and other arithmetic, so string / `None` / `NaN`
entries in `allStrikes` still raise `TypeError` / `ValueError` **end to end**. These are **pre-existing
limits of the legacy profile, not regressions introduced here** — the old code was equally intolerant.
Tested and recorded rather than widened into a rewrite of every profile calculation.

### Interval contract after Codex R1/R2 (v3)

`STRIKE_INTERVAL_VERSION = strike_interval_v3_unique_repeated_mode_ladder_only`.

**`OK` now requires a UNIQUE and REPEATED modal interval.** The v1 contract returned `OK` whenever a
single mode existed, or broke ties by preferring the tighter gap — so Codex's probes
`[56000,56100,56300]` and `[55800,55900,56000,56200,56400]` were both labelled `OK = 100` despite
having no unique repeated interval. A tighter-interval preference is not established geometry, and on
another valid ladder it samples strikes that do not exist and recreates the zero features this repair
removes. Both probes are now tests.

| Evidence | Status | Reason |
|---|---|---|
| unique mode, count ≥ 2 | `OK` | — |
| two or more modes tied at the same count | `AMBIGUOUS` | `TIED_MODES` (+ `tied_modes` listed) |
| no gap repeats, several distinct | `AMBIGUOUS` | `NO_REPEATED_INTERVAL` |
| exactly one gap observed | `AMBIGUOUS` | `SINGLE_GAP_ONLY` |

`value` is populated **only** on `OK`. A best guess is retained separately as `candidate_value`, so it
cannot be mistaken for a verified applied value. Ordering matters and is commented: when nothing
repeats, every distinct gap is a "mode" of count 1, so a tie check placed first mislabels a
no-repetition ladder — a flaw in my own first fix, caught by test.

**The metadata override channel is DELETED (v3).** v1 accepted any finite positive number and
labelled it `OK/METADATA`, so `strikeInterval = 1500` or `7` was applied as trusted evidence and
reinstated the original failure. The comment also called it "dated metadata" while a repository-wide
search finds **no writer** of `strikeInterval` / `strike_interval` in Python, Kotlin or the PWA —
only my own consumer. **The channel was invented.**

The v2 binding contract that replaced it could not be established either, and Codex enumerated why: a
caller passing `index/expiry/session_date=None` skipped every binding check (and `chain_profile` was
such a caller for two of the three), a **future** `as_of` passed the lexical staleness comparison, a
non-empty `source` string authenticated nothing, and "appears as an adjacent gap somewhere in the
ladder" would still accept a sparse 1500 against a dense 100 **local** ladder.

So in v3 there is no parameter, no helper and no constant — the interval comes from the observed
ladder only. The contract a real producer must meet is recorded as a comment, not as live code:
parsed dates with explicit as-of semantics, trusted and complete binding context, **local**
applicability rather than any-gap matching, and contradiction tests. Two tests assert the channel is
gone; the no-producer scan is kept, so adding a producer fails the suite and points at that review.

The **±6-strike window** is a proposed geometric heuristic with a bounded purpose — the interval is
used to sample near the money, so it is inferred there. It is not a market threshold. Boundary tests
cover a distant density change, ATM inside a sparse region, and explicit window sizes.

### Profile-field impact map (measured, not asserted)

| Fixture | legacy step | new step | status | ivSkew | ivSlope |
|---|---|---|---|---|---|
| sparse BNF monthly (brief fixture) | 1500 | **100** | `OK` (modal count 12) | 0.0 → **2.0** | 0.0 → **1.0** |
| normal NF 50-pt | 50 | 50 | `OK` | unchanged | unchanged |
| normal BNF 100-pt | 100 | 100 | `OK` | unchanged | unchanged |
| `[56000,56100,56300]` | 100 | **none** | `AMBIGUOUS` | legacy fallback | legacy fallback |
| tied-mode ladder | 100 | **none** | `AMBIGUOUS` | legacy fallback | legacy fallback |

**Correcting an earlier overstatement:** changes are **not** confined to a sparse ladder. Tied-mode
ladders and no-repetition ladders are also changed paths, asserted by test. (The third path cited
earlier was a refused metadata override; that channel no longer exists in v3, so there are two.)

**Full-field parity on ordinary ladders** is now tested directly, not inferred from step equality:
every pre-existing profile field is compared between the forced-legacy-step run and the new run on
50-pt and 100-pt ladders, and the field set is asserted unchanged apart from the additive
`strikeIntervalEvidence`.

`netDelta`, `volRatio`, `avgTheta`, `avgVega`, `atmIv` do not depend on `step`. The earlier synthetic
audit's 0/0 → 2/1 is **reproduced from the real function**, not hardcoded.

### Active-consumer map for `chain_profile` (completed after Codex R1)

| Consumer | Call site | Uses | Does a changed value reach a decision? |
|---|---|---|---|
| `result["bnfProfile"]` / `["nfProfile"]` | ~17347–17348 | full profile dict | persisted/displayed evidence |
| local `bnf_profile` / `nf_profile` | ~17015–17020 | full profile dict | feeds the consumers below |
| `chain_profile_insights` | ~5364–5370 | `ivSlope` > 3, < −2 | insight text only |
| **`synthesize_verdict`** | def ~5597, called ~17815 | `profile.get('ivSkew')` → `skew > 3: bear += 0.2`; also `'iv_skew'` in the `_trace` block | **traced below — no** |

**Traced, as required.** `synthesize_verdict` writes `result['verdict']`. `_ia_apply_cutover` then
captures `legacy_verdict = result.get('verdict')`, copies only
`('action','strategy','direction','confidence')` into `result['legacy_evidence']['verdict']` under the
label *"legacy_combined_v0 recorded evidence; not used in any decision, ordering or alert"*, and
**afterwards replaces `result['verdict']`** with the authority-derived verdict. Order asserted by test.

So a changed `ivSkew` **can** alter the retained `legacy_evidence.verdict` values and the `_trace`
block, but **cannot** move the canonical displayed verdict, candidate ranking, admission or
notifications. I am claiming neither that the canonical authority changes nor that a profile-only
change has no other effect.

**Historical incidence is unknown**: counting real affected sessions needs raw `allStrikes` arrays per
observation, which this environment does not have.

### Commands

```
PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_chain_profile_strike_interval.py
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_*.py'
```

Results at this head: Commit A suite **67 tests OK** (29 → 54 after R1 → 67 after R2); full Python
suite **1973 tests OK**, 0 failures, 2 skipped. `python3 -m py_compile` clean; `git diff --check`
clean. GitHub review validation was **green at `335c274f`** (run 42359069861, all 28 steps); this head
adds two commits and needs its own CI run.

---

## Source-to-consumer map — current direction authority

At `755b127a…`. `index_authority_v1.compute_direction` reads **only** these; no percentile, skew, PCR,
OI, breadth or ML value reaches the directional verdict:

| Input | Producer / resolution | Timestamp semantics | Missing behaviour | Live consumer |
|---|---|---|---|---|
| Spot series | `poll_spot` → `nfSpot`/`bnfSpot`, else `nf`/`bnf`; finite > 0 | poll time, IST | `SPOT_MISSING` / `SPOT_STALE` | `z_open`, `z_30`, ER |
| Poll timestamps | `poll_time` → `poll_ts`/`pollTs`/`ts`, else `t`/`time` | IST instant; first field that **parses** | row excluded | series membership, anchor age, ER gaps |
| `scale.value` (m) | `compute_scale`, frozen at open reference; D9 may use next expiry | observed once in [09:20,09:35) | non-`OK` ⇒ direction `UNAVAILABLE` | denominator of both z-scores |
| `scale.status` | same | — | `SCALE_*` reason codes | gates the whole verdict |
| `decision_dt`, `receipt_dt`, `analysis_dt` | caller | trusted clocks; latest wins | `DECISION_TIME_INVALID`, `CLOCK_SKEW` | staleness, future-poll exclusion |

Constants: `OPEN_REF_WINDOW [09:20,09:35)`, `Z_OPEN_THRESHOLD 0.50`, `STRONG_THRESHOLD 1.0`,
`Z30_LOOKBACK_MIN 27.5`, `Z30_WARMUP_MIN 10.0`, `Z30_ANCHOR_MAX_AGE_MIN 45.0`, `ER_MAX_GAP_MIN 12.0`,
`RANGE_ER_MAX 0.30`, `SCALE_M_BOUNDS (0.002, 0.05)`, `SQRT_2_OVER_PI 0.79788`.

Writer: `brain.compute_index_authority` (sole writer of `by_index`). Persistence:
`index_authority_store` (`<filesDir>/index_authority_v1/<session>.json`), scales write-once except the
D9 recoverable-failure → OK upgrade. Decision identity: `decision_identity` hashes `direction`
wholesale, so **any** added field changes every `decision_id`, which carries admission lineage
(`entry_decision_id`) and history record keys — the reason D9 provenance is stamped only off the
front-expiry path.

**Not yet traced, and needed before Commit E:** notification ownership, candidate ranking and admission
read paths, and the PWA display consumers.

## Data-coverage inventory — initial, and the honest gaps

| Need | Available here | Gap |
|---|---|---|
| Per-poll spot series, both indices | device-time `authority_doc.history` for 2–4 Nov (78 records/index/session) | 5 Nov export never reached this session; only its 09:30 slot via the brief |
| Multi-session history for empirical ranks | app retains a **100-row rolling** `poll_history` only | no per-session archive locally; earlier sessions needed for cohorts are **not present** |
| Raw `allStrikes` per observation | none | blocks historical incidence counting for Commit A |
| Option quotes with bid/ask + quote time | none offline | blocks the options/feasibility arms |
| Greeks, OI previous-observation identity, breadth | none offline | those bundles are **unavailable**, not "tested and negative" |
| Exchange calendar | `NSE_HOLIDAYS_2026` in `MarketWatchService.kt`; `2026-11-10` confirmed a holiday | Python-side calendar authority to be confirmed at the checked-out SHA |

**Consequence for Commit D:** with only a 100-row rolling window locally, E1's earlier-session cohorts
cannot be built from this environment's data. E0/E1 will be implemented and unit-tested against
synthetic and any supplied real extracts, and marked **blocked on data** with the exact missing
fields/dates rather than fitted on manufactured history.

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

Sessions unusable or degraded, which must be excluded or flagged rather than averaged over:

| session | slots | gaps | note |
|---|---|---|---|
| 2026-09-28 | 7 of 77 | 60 | near-total outage; ends 14:50 |
| 2026-10-07 | 46 | 32 | |
| 2026-10-08 | 56 | 21 | ends 15:35 |
| 2026-09-30 | 65 | 13 | |
| 2026-10-01 | 67 | 11 | |
| 2026-10-19 | 69 | 9 | |

**5 November, explicitly.** 64 slots, ids 0–63, **zero internal gaps**, reconstructed 09:15 → 14:30.
Slots 64–77 (14:35 onward) are **absent** — the extract stops mid-session, so no 5 Nov label with a
horizon ending after 14:30 can be matured from it. `phone_dirs_20261105.txt` carries exactly 64
timestamped entries over the same 09:15–14:30 range, so the two align 1:1; its codes are
`UNUN`×3, `NENE`×29, `NEBU`×19, `BUBU`×12, `BUNE`×1 (first two letters NF, last two BNF;
UN/NE/BU). That series is a **phone-reported direction label**, not an independent price source.

**Staleness.** 168 repeated-value runs start at slot ≥ 70 — a systematic post-close tail (slots
72–74 and 75–77 repeat in most sessions), consistent with a fixed 78-slot pad past the 15:30 close.
Only **5** repeated pairs occur mid-session across all 45 sessions (2026-09-17 NF 12:30–12:35,
2026-09-21 NF 09:55–10:00 and 10:55–11:00, 2026-09-29 NF 11:40–11:45, 2026-11-03 BNF 13:00–13:05).
Negligible in volume, but flagged rather than consumed as fresh observations.

**What the opening chain windows are NOT.** 90 rows, **09:20 only**, 10 legs each, fields limited to
`strike, ceBid, ceAsk, peBid, peAsk`. No IV, no Greeks, no OI, no volume, and **no intraday series**.
NF calendar DTE spans 0–6, BNF 0–28; 12 rows are own-expiry-day (`date == expiry`), 10 NF and 2 BNF.
Any option-leg feature after 09:20 is **unsupported** by this evidence, and an option-based arm cannot
be built from it.

**Consequence for D.** A **price-only** E0/E1 arm is feasible on 45 sessions / 3,267 observations with
explicit as-of and label-maturity limits. An option-priced arm is **not**. I will not claim the
supplied replay script reproduces phone parity without adapting and validating its clock and
interface assumptions, and no estimator is fitted until the observer's B1–B5 causal defects are fixed.

---
## Not claimed

- No direction-quality, forecast or profitability claim anywhere in Commit A.
- Historical incidence of the sparse-strike defect: **unknown**.
- No phone evidence: no device in this session; CI and a release do not prove installation.
- Kotlin not compiled locally in this environment.
