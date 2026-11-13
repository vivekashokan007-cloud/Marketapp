# Frozen follow-up plan — premium repricing confirmation

**Dated 13 November 2026 IST. Frozen before any fresh session is evaluated.**

The 12 November run is **development evidence**. It found and fixed three defects in the
scoring harness, so it cannot also serve as confirmation of the conclusions it produced.
This plan fixes what will be measured, on what, and what each outcome will be allowed to
mean, so that the fresh-session test cannot be re-specified after its result is seen.

## 1 · What is frozen

Everything in `RULES_FROZEN.md` Addendum 3: the ACT/365 calendar clock with a 15:30 IST
per-contract expiry timestamp and the 60-calendar-minute runtime assertion; the parity
forward `F = K + (C_mid − P_mid)` with `DF = 1` and the 25-point cross-strike dispersion
gate; the anchoring form `observed_entry_mid + (model_exit − model_entry)`; the price
bounds; the five ranked models and three oracle diagnostics; the denominator set; the
paired per-contract sign test; and every proxy label.

Frozen artifacts:

| item | identity |
|---|---|
| harness | `tools/spot_path_baseline/premium_repricing_v2.py` at the commit recorded in the reply |
| rules | `tools/spot_path_baseline/RULES_FROZEN.md`, Addendum 3 |
| spot history | `spots_20261021_20261104.jsonl`, sha256 `f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186` |
| development session | `mr_session_evidence_2026-11-12.json`, sha256 `5f399accefabd0dd3f1f1f3bb829bb71fd6ae56a309dfc92db5ac18e6d3444cb` |

No constant, model, gate, metric or denominator in that set may be changed for the
confirmation run. A change to any of them restarts this plan under a new date.

## 2 · Primary question

**Does E0 spot information price the 60-minute premium better than assuming the premium
does not move, once the clock and the underlying reference are correct?**

The pre-registered primary comparison is **`E0_FWD` against `PERSIST`**, by mean CRPS, per
index, decided by the paired per-contract sign test.

`E0_FWD` is primary rather than the anchored variants for one reason: it uses no fitted
offset and no future observation, so it cannot be argued to have absorbed the level error
through a free parameter. The anchored variants are reported beside it as secondary.

## 3 · Pre-registered predictions

Stated now, before fresh data, so they can fail.

| # | prediction | fails if |
|---|---|---|
| P1 | BANKNIFTY's forward basis stays negative in every admitted minute | any admitted minute shows `F ≥ spot` |
| P2 | NIFTY's forward basis stays small, inside ±0.1 % | the mean basis leaves that band |
| P3 | Entry basis error priced off the parity forward stays under 0.5 implied-vol points for both indices | either index exceeds it |
| P4 | `E0_FWD` beats `PERSIST` on mean CRPS for both indices | either index fails to beat it |
| P5 | `E0_FWD` beats `PERSIST` on at least 5 of 6 contracts per index | fewer on either index |
| P6 | Spot-based unanchored `E0_IV` is worse than `E0_FWD` for BANKNIFTY | it is not |
| P7 | `E0RV_IV_ANCH` does not beat `E0_IV_ANCH` on the common sample for both indices | it beats it on both |

P1–P3 test whether the 12 November market structure and data contract hold at all. P4–P6
test the forecast conclusion. P7 carries forward the premium-side RV finding.

## 4 · Sessions and stopping rule

- **Minimum three fresh sessions**, each a session not yet examined for premium research,
  each with its own export captured under the existing pilot rules.
- Every session is scored **independently and reported separately** before any pooling.
- Pooling, if any, is by **session-level mean**, never by row, because rows inside one
  session are heavily correlated across minutes and strikes.
- The run order is fixed by date. No session is dropped after its result is seen; a session
  may be excluded only for a **pre-stated data reason** — missing authority spot, no admitted
  two-sided book, or a cross-strike dispersion refusal — and the reason is recorded.
- No result is reported as confirmation until all three sessions have been scored.

## 5 · What each outcome is allowed to mean

| outcome | permitted conclusion |
|---|---|
| P4 and P5 hold on all three sessions, per index | the 60-minute E0 premium challenger is confirmed **for research**, for that index. Still no activation claim, no ranking change, no change to v7, gates, sizing, exits or teacher fee. |
| P4 holds but P5 fails, or results split across sessions | **inconclusive**; report and widen the session count before any further claim |
| P4 fails on either index | the 12 November result does not generalise for that index; record it and stop that line |
| P1, P2 or P3 fails | the data contract or market structure changed. **Stop and re-diagnose before reading any forecast result** — a forecast score computed on a broken underlying is what produced the defect this plan exists to correct. |

## 6 · Explicitly out of scope for this checkpoint

- Any IV-change model. The 12 November evidence now says the unchanged-IV assumption was
  **not** the binding error once the underlying is correct, so an IV-dynamics model is not
  justified by it and is not pre-registered here.
- Any change to live behaviour: v7 ranking, entry gates, sizing, exits, teacher fee,
  notification policy, the b529 phone smoke, or the frozen t203 pilot.
- Execution or activation claims of any kind. The P&L figures stay proxies while quote event
  age, quantity units and the dated lot authority are unresolved.
- Full-session, next-session and multi-day horizons, which remain on the agreed forward path
  under their own rules.

## 7 · Return format for the confirmation run

Commit, patch against its stated base, checksum manifest, replay commands, rule and input
hashes, per-session tables by index, the P1–P7 outcome list stated as pass or fail, exclusion
counts with reasons, and a proposed decision per index. Any deviation from this plan is
reported as a deviation, with its reason, and not presented as a planned analysis.
