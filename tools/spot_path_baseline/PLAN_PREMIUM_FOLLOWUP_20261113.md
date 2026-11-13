# Frozen plan — premium repricing research check

**Dated 13 November 2026 IST. Revision R2, frozen before any fresh session is scored.**

R2 supersedes R1, which superseded the plan first written earlier today. Changes are recorded
here rather than quietly edited away.

**New in R2**, after review found two further defects:

- The parity forward is computed **per (underlying, expiry) group**, requiring ≥ 2 distinct
  strikes inside the group. R1 pooled per-strike estimates across expiries, so one pair from
  each of two expiries passed the count check against a meaningless dispersion.
- **Spot must be established as available before use.** R1 used the same-minute reading
  regardless of when it arrived; the spot authority's `receipt_ts` runs 1.69–32.60 s behind the
  quote cutoff, so every spot-dependent row of 12 November carried look-ahead. Spot-based models
  now run on the most recent reading **received by the cutoff** — about 5 minutes stale — with
  its age reported, and `E0_FWD` scores with **no spot join at all**.
- Forecast time to expiry comes from the **planned target** (entry receipt + 60 min). The actual
  exit receipt only tests the scoring quote against the tolerance.
- The CRPS/MAE outcome rule is **explicit and computed by the harness** (§6).

**Carried from R1:**

- The **spot–forward basis is a diagnostic, not a gate.** The earlier plan made "BANKNIFTY's
  basis stays negative" a prediction whose failure would halt the read. That was wrong: a
  positive BANKNIFTY basis is a market observation, not a data fault, and alone it leaves an
  otherwise valid session eligible.
- **No inferential statistic is computed from the six contracts.** The earlier plan claimed a
  sign test at `p = 2⁻⁶ = 0.016`. Those six contracts are three strikes × two sides on one
  underlying over one session: they share the same spot path, so they are not independent and
  that p-value was not available. Per-contract agreement is reported as **descriptive** only.
- The harness is **contract 2.1**, which fixes the parity inputs, the forecast cutoff and the
  post-anchor price bounds.

The 12 November run is **development evidence**. It found and fixed defects in its own
harness, so it cannot also confirm the conclusions it produced.

## 1 · What is frozen

Everything in `RULES_FROZEN.md` Addenda 3, 4 and 5: the ACT/365 calendar clock with entry at the
**envelope receipt instant**, the forecast clock at the **planned target** (entry receipt +
60 min), a 15:30 IST per-contract expiry timestamp, and the scoring quote admitted only when the
actual exit receipt falls within **± 5 s** of that target; the parity forward
`F = K + (C_mid − P_mid)` with `DF = 1`, computed **per (underlying, expiry) group** inside one
envelope, requiring **≥ 2 distinct strikes in the group** before the ≤ 25-point in-group
dispersion gate may pass, and usable only by contracts in that group; spot admitted only when
its own `receipt_ts ≤ the forecast cutoff`, most recent such reading, age reported; the anchoring
form
`observed_entry_mid + (model_exit − model_entry)` with price bounds re-applied **after**
anchoring, including in the anchored realised-spot diagnostic; the ranked set and the three
oracle diagnostics; the denominator set; and every proxy label.

| item | identity |
|---|---|
| harness | `tools/spot_path_baseline/premium_repricing_v2.py`, `CONTRACT_VERSION = 2.2` |
| focused tests | `tools/spot_path_baseline/test_repricing_fixes.py`, 39 checks, all passing |
| rules | `RULES_FROZEN.md`, Addenda 3, 4 and 5 |
| spot history | `spots_20261021_20261104.jsonl`, sha256 `f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186` |
| development session | `mr_session_evidence_2026-11-12.json`, sha256 `5f399accefabd0dd3f1f1f3bb829bb71fd6ae56a309dfc92db5ac18e6d3444cb` |

No constant, model, gate, metric or denominator in that set may change for the research check.
A change to any of them restarts this plan under a new date and revision.

## 2 · Primary comparison

**`E0_FWD` against `PERSIST`**, by mean CRPS, per index, on **matched rows** — identical
contract, forecast cutoff and exit observation.

`E0_FWD` is primary because it uses no fitted offset and no future observation, so it cannot
be argued to have absorbed a level error through a free parameter. `E0_IV_ANCH` is reported
as **secondary**. All three oracles stay outside the forecast ranking.

A known split in the development evidence, carried forward deliberately: `E0_FWD` improved
CRPS against persistence on both indices while its **point-forecast MAE was worse** (NIFTY
24.41 vs 24.35, BANKNIFTY 52.98 vs 47.91). CRPS scores the whole predictive distribution and
MAE only its median, so a wider but better-placed distribution can win one and lose the
other. **Both are reported every time**, and a CRPS win accompanied by an MAE loss is
reported as exactly that, never as an unqualified improvement.

## 3 · What is recorded as diagnostic

Reported every session, never used to admit or refuse one:

- the **sign and size** of the spot–forward basis, per index, with its range;
- entry basis error under both underlyings, in points and in implied-vol points;
- cross-strike dispersion of the parity forward and pairs per minute;
- the realised receipt-to-receipt horizon distribution;
- all three oracle diagnostics.

## 4 · Eligibility and exclusions

A session is eligible when it yields matched rows under the frozen gates. A **minute** is
refused only for a stated data reason: fewer than 2 valid parity pairs, cross-strike
dispersion above 25 points, a realised horizon outside 3600 ± 5 s, an unresolved
record-to-envelope join, a missing receipt clock, a one-sided or non-positive book, an
unverified or disagreeing identity, a missing entry spot or IV, a target at or after expiry,
or fewer than 5 prior sessions in the E0 pool. Every refusal is counted by reason and
reported. A missing realised exit spot excludes the **oracle alone**.

Nothing is refused on the basis sign, on the magnitude of a model's error, or on any
outcome-dependent quantity.

## 5 · Sessions and stopping rule

- **Three fresh sessions** for an initial research check, each not yet examined for premium
  research, each captured under the existing pilot rules.
- Each session is scored **independently and reported separately**, with **matched `E0_FWD` and
  persistence CRPS and MAE**, interval coverage, and exclusions **by session and index**, before
  any pooling. The harness prints the per-session verdict from the §6 rule itself.
- The **available spot age** is reported per session, since spot-based models run on a reading
  that is stale by construction, and `E0_FWD`'s independence from it is the reason it is primary.
- Pooling, if any, is by **session-level mean**, never by row and never by contract: rows
  within a session share one spot path.
- Run order is fixed by date. No session is dropped after its result is seen; exclusion
  requires a pre-stated data reason from §4, recorded with its counts.

## 6 · What each outcome is allowed to mean

| outcome | permitted conclusion |
|---|---|
| `E0_FWD` beats `PERSIST` on CRPS in all three sessions for an index | the 60-minute E0 premium challenger is supported **for research** on that index. Still no activation claim and no change to v7 ranking, gates, sizing, exits, teacher fee or notifications. |
| it wins in two of three | **inconclusive**; report both metrics and widen the session count before any further claim |
| **ΔCRPS < 0 and ΔMAE ≥ 0 in any session** | that session is **SPLIT — INCONCLUSIVE on its own**, and is never reported as an unqualified improvement. A line of three SPLIT sessions does **not** aggregate into support: it is reported as a consistent CRPS gain carrying an MAE cost, with both numbers, and the decision stays open. Support requires **ΔCRPS < 0 and ΔMAE < 0 together** in all three sessions. |
| ΔCRPS ≥ 0 and ΔMAE < 0 in any session | also **SPLIT — INCONCLUSIVE on its own** |
| it fails in two or more sessions for an index | the 12 November result does not generalise for that index; record it and stop that line |
| entry basis error under the forward exceeds ~0.5 implied-vol points, or the parity forward stops agreeing across strikes | **stop and re-diagnose the data contract before reading any forecast number.** A forecast score computed on a broken underlying is what produced the defect this plan exists to correct. This is a data-quality halt, not a market finding. |

## 7 · Explicitly out of scope

- **Any IV-change model.** Deferred on review. The corrected evidence says the unchanged-IV
  assumption was not the binding error once the underlying is right, so nothing here
  pre-registers an IV-dynamics model.
- Any change to live behaviour: v7 ranking, entry gates, sizing, exits, teacher fee,
  notification policy, the b529 phone smoke, or the frozen t203 pilot.
- Execution or activation claims. Ask-to-bid P&L stays a **proxy** while quote freshness,
  quantity units and dated lot authority are unresolved.
- Full-session, next-session and multi-day horizons, which remain on the agreed forward path
  under their own rules.

## 8 · Evidence needed to run the confirmation

For each of the three fresh sessions, one `mr_session_evidence_<date>.json` export carrying,
per index: `premium.indices[ix].records` with `bbo.bid_raw` / `ask_raw` and
`iv.iv_greeks_raw`; `premium.indices[ix].slot_envelopes` with `acquisition_id`, `slot` and
`clocks.acquisition_response_ms`; `pinned_contracts` with `instrument_key`, `option_type`,
`strike` and `expiry`; and `parts.authority_doc.history[ix][].spot` with `value` and `asof`.
Slots must span at least 60 minutes beyond the first so a t+60 target exists, and at least two
strikes must carry **both** a call and a put in the same expiry at the same minute, or the
parity forward is refused and `E0_FWD` cannot be scored.

Plus a spot-history file covering **at least 5 prior sessions** at the same minute grid as the
session's slots, in the frozen `{d, t, nf, bnf}` JSONL form.

## 9 · Return format

Commit, patch against its stated base, checksum manifest, replay commands, rule and input
hashes, per-session tables by index with CRPS, MAE, coverage and exclusion counts, the
diagnostics of §3, and a proposed decision per index. Any deviation from this plan is reported
as a deviation, with its reason, not presented as a planned analysis.
