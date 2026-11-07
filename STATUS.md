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
| 0 — capability, base SHAs, source/consumer map | **done for direction; coverage inventory partial** | §"Source-to-consumer" below |
| A — sparse-strike repair | **implemented + tested; awaiting Codex review** | this branch |
| B–C — observer, schema, causal path/ranks | not started | to be a separate branch `review/claude-empirical-direction-20261107` off verified main |
| D — E0/E1 estimators | not started | |
| E — per-poll adapter, flag, resources | not started | |
| F — review package, option/multi-day matrix | not started | |

**Next executable step:** open the Commit A PR, then start B–C on a *separate* branch based on
`755b127a…` (verified main), not on this unapproved repair. A price-only path must remain able to
proceed without Commit A.

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

**Repair.** New `infer_strike_interval(all_strikes, atm, metadata_step)` plus
`_normalise_strike_ladder`. `_infer_strike_step` is **unchanged** — candidate generation and the
teaching band call it, and altering it here would mix two changes.

Why not reuse the helper directly, as the brief required me to justify: it returns `min(gaps)` over the
whole ladder. Its docstring claims "modal/minimum" but the code is a pure minimum, so **one anomalous
narrow gap anywhere silently sets every sampling distance**, and a global minimum need not be a local
interval. `infer_strike_interval` instead takes the **modal gap near the money** (±6 strikes around
ATM, where these features actually sample), returns an explicit status, and records
`helper_value` / `helper_agrees` so a disagreement is visible rather than absorbed.

Rule order: valid dated `strikeInterval` metadata → near-ATM modal gap → global modal gap.
Ties resolve to the tighter interval. A ladder with no repeating gap and ≥3 distinct gaps returns
`AMBIGUOUS` / `NO_MODAL_INTERVAL` rather than inventing a number. It takes **no spot argument** —
spot must not infer ladder geometry.

`chain_profile` keeps an **explicitly isolated legacy fallback** (`100 if spot > 30000 else 50`),
reached only when the interval is unavailable or ambiguous, so existing consumers still get a value
where they previously had one. New additive field `strikeIntervalEvidence` records
`value/status/reason/source/applied/legacy_fallback_used/helper_value/helper_agrees/version`, so new
evidence can exclude the field instead of inheriting the fallback.

**A bug I introduced and caught here:** the normaliser works in float, so the interval came back
`100.0`, making `atm + otm_dist` a float and `str()` produce `"56200.0"` — missing a strikes dict keyed
`"56200"` and silently reproducing the very zero-IV symptom being repaired. Integral intervals are now
narrowed to `int`, with four tests pinning it, including an end-to-end `get_strike` resolution check.

### Profile-field impact map (measured, not asserted)

| Fixture | legacy step | new step | source | ivSkew | ivSlope |
|---|---|---|---|---|---|
| sparse BNF monthly (brief fixture) | 1500 | **100** | `NEAR_ATM_MODAL` | 0.0 → **2.0** | 0.0 → **1.0** |
| normal NF 50-pt | 50 | 50 | `NEAR_ATM_MODAL` | unchanged | unchanged |
| normal BNF 100-pt | 100 | 100 | `NEAR_ATM_MODAL` | unchanged | unchanged |

`netDelta`, `volRatio`, `avgTheta`, `avgVega`, `atmIv` do not depend on `step` and are unchanged.
The earlier synthetic audit's 0/0 → 2/1 is **reproduced from the real function**, not hardcoded.

### Active-consumer map for `chain_profile`

| Consumer | Call site | Uses |
|---|---|---|
| `result["bnfProfile"]` / `result["nfProfile"]` | `brain.py` ~17347–17348 | full profile dict |
| local `bnf_profile` / `nf_profile` | `brain.py` ~17015–17020 | full profile dict |
| `chain_profile_insights` | `brain.py` ~5364–5370 | `ivSlope` thresholds (>3 "fear skew steep", <-2 "call skew unusual") |

Only `ivSkew` and `ivSlope` change, and only on a sparse ladder. **Historical incidence is unknown**:
counting real affected sessions needs raw `allStrikes` arrays per observation, which this environment
does not have.

### Commands

```
PYTHONPATH=app/src/main/python python3 app/src/main/python/tests/test_chain_profile_strike_interval.py
PYTHONPATH=app/src/main/python python3 -m unittest discover -s app/src/main/python/tests -p 'test_*.py'
```

Results: Commit A suite **29 tests OK**; full Python suite **1935 tests OK**, 0 failures, 2 skipped.
`python3 -m py_compile` clean; `git diff --check` clean.

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

## Not claimed

- No direction-quality, forecast or profitability claim anywhere in Commit A.
- Historical incidence of the sparse-strike defect: **unknown**.
- No phone evidence: no device in this session; CI and a release do not prove installation.
- Kotlin not compiled locally in this environment.
