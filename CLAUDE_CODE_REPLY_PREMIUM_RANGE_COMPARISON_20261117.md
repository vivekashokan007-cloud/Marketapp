# Claude Code reply — neutral premium range comparison, runner and protocol draft

Market Radar · 17 November 2026 IST · reply to
`CLAUDE_CODE_HANDOFF_PREMIUM_RANGE_COMPARISON_20261116`

**Branch** `review/premium-neutral-range-comparison-20261116`
**Base** `124d62ee027941e43decc737e53d202c32b1d214` (verified R7 replay commit)
**Commit** `1419ac2d61001b1415e119373dece3c2c446e7f0`, pushed
**Patch** `git format-patch -1 1419ac2` — 7 files, 5,452 insertions, 0 deletions

The runner, the trial protocol draft, the machine-readable scores and the input manifest are on
the branch. The frozen R7 tree is **byte-identical**: `git diff 124d62e -- tools/spot_path_baseline/`
is empty, and `premium_repricing_v2.py` still hashes to
`95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a`.

| file | what it is |
| --- | --- |
| `tools/premium_range_compare/range_compare.py` | the runner, `premium_neutral_range_comparison_v1` |
| `tools/premium_range_compare/PROTOCOL_RANGE_TRIAL_DRAFT_v1.md` | the trial definition, written to be frozen before the first scored fresh export |
| `tools/premium_range_compare/tests/test_range_compare.py` | 52 checks, no network |
| `tools/premium_range_compare/results/range_comparison_2026-11-12.{json,txt}` | machine-readable scores and the printed run |
| `tools/premium_range_compare/INPUT_MANIFEST_20261116.json` | every input by SHA-256, including the absent one |
| `tools/premium_range_compare/README.md` | the exact invocations and the result in one table |

---

## 1 · Exact invocation

```bash
# the committed 12 November comparison, both indices, all four horizons, plus the grid sweep
python3 -I tools/premium_range_compare/range_compare.py \
    <export_2026-11-12.json> <spots_20261021_20261104.jsonl> \
    --grid-sensitivity NF --out tools/premium_range_compare/results

# the 16 November evening export, the moment it is available (nothing else is missing)
python3 -I tools/premium_range_compare/range_compare.py \
    <export_2026-11-16.json> <spots_20261021_20261104.jsonl> --out <dir>

# the unchanged frozen replay, for the reference numbers
python3 -I tools/spot_path_baseline/premium_repricing_v2.py \
    <export.json> <spots.jsonl> --out <dir>

# the tests; 7 of the 52 need the real inputs
cd tools/premium_range_compare && python3 -m unittest discover -s tests
RANGE_EXPORT=<export_2026-11-12.json> RANGE_SPOTS=<spots.jsonl> \
    python3 -m unittest discover -s tests
```

Options: `--horizons 15,30,60,120`, `--slot-policy exact|floor5|explicit`, `--reverse-input`,
`--grid-sensitivity <INDEX>`, `--json-only`, and several exports for a multi-session run.

**Inputs are not committed.** Raw phone exports stay out of the repository; the manifest ties a
result to its input by SHA-256 without the input being in the tree.

---

## 2 · The comparison as built

| variant | centre | dispersion from | recentred | role |
| --- | --- | --- | --- | --- |
| `FLAT` | entry mid | nothing | n/a | the floor a range must beat |
| `IV_ONLY` | entry mid | the contract's entry IV, **fitted from its own midpoint** under the parity forward, on a fixed 201-point quantile grid | yes | candidate |
| `HIST` | entry mid | prior sessions' log returns over the same clock interval, **median removed** | yes | candidate |
| `R7_E0_FWD` | — | prior sessions' raw returns at the **quoted** IV | no | reference against the unchanged replay |
| `IV_ONLY_RAW`, `HIST_RAW` | — | as above | no | diagnostics: what the recentring actually did |

Both candidates are repriced with the **same** fitted entry IV, the same ACT/365 clock and the
same bounds, so the only difference between them is the scenario distribution. The median
historical return is removed from `HIST` so that an empirical drift cannot do the centring's job
and be reported as added value.

**`R7_E0_FWD` is a reference, not a competitor**: it is not recentred and it uses the quoted IV,
so its CRPS is not comparable to the two recentred ranges. It exists so the runner can be checked
against the frozen replay, which it reproduces to six decimal places (§5).

---

## 3 · Results — 12 November, development evidence

Common scored rows, quantile grid 201, `--slot-policy exact`. Gate = the proposed 70–90% nominal
80% coverage.

| index | horizon | rows | `IV_ONLY` CRPS | cov80 | gate | `HIST` CRPS | cov80 | gate | `HIST` vs `IV_ONLY` |
| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | :---: | ---: |
| NIFTY | 15 | 300 | **8.219** | 64.7% | ✗ | 9.279 | 71.3% | ✓ | −12.90% |
| NIFTY | 30 | 282 | **11.034** | 67.4% | ✗ | 13.098 | 76.2% | ✓ | −18.70% |
| NIFTY | **60** | 246 | **16.840** | 54.9% | ✗ | 18.096 | 81.7% | ✓ | −7.46% |
| NIFTY | 120 | 174 | 31.857 | 41.4% | ✗ | **30.914** | 75.9% | ✓ | +2.96% |
| BANKNIFTY | 15 | 305 | **21.862** | 70.2% | ✓ | 23.007 | 78.4% | ✓ | −5.24% |
| BANKNIFTY | 30 | 287 | **27.823** | 71.1% | ✓ | 31.511 | 79.1% | ✓ | −13.25% |
| BANKNIFTY | **60** | 251 | **34.456** | 78.5% | ✓ | 38.046 | 87.6% | ✓ | −10.42% |
| BANKNIFTY | 120 | 179 | **36.890** | 92.2% | ✗ | 46.736 | 92.7% | ✗ | −26.69% |

Mean interval widths at 60 minutes: NIFTY `IV_ONLY` 56.9 against `HIST` 92.8; BANKNIFTY 151.9
against 213.4. `FLAT` CRPS = MAE at 60 minutes: NIFTY 24.351, BANKNIFTY 47.911.

**The central comparison ties on MAE, as it must.** NIFTY 60 min: `FLAT`, `IV_ONLY` and `HIST`
all 24.351. BANKNIFTY: all 47.911. The tie holds to nine decimal places at every horizon on both
indices and is asserted on the real rows, not argued for — it is the check that makes the CRPS
column a comparison of *shape* rather than of location. `R7_E0_FWD`'s MAE differs (24.413 /
52.982) precisely because it is not recentred.

### 3.1 · The primary question is answered split, and the split is structural

`IV_ONLY` has the lower CRPS in **7 of 8** cells. `HIST` passes the calibration gate in **7 of 8**
while `IV_ONLY` passes in **3 of 8**. The two proposed criteria disagree almost everywhere, in the
same direction:

* NIFTY `IV_ONLY` is **systematically under-dispersed, and worse the longer the horizon** —
  64.7%, 67.4%, 54.9%, 41.4%. A nominal 80% interval delivering 41% at two hours is not slightly
  imprecise; it is the wrong width.
* BANKNIFTY at 120 minutes is the opposite failure — **both** models over-cover at 92%, so the
  gate rejects the historical range too. The gate is not a proxy for "prefer history".
* The one cell where `HIST` also wins CRPS (NIFTY 120) is the cell where `IV_ONLY`'s
  under-dispersion is most extreme. One cell is one cell, and under this protocol it cannot be
  promoted to the primary horizon after the fact.

So this is not a 60-minute sampling accident, and the protocol has to say which criterion governs
**before** the trial starts. §7.1 of the draft states three defensible resolutions and
recommends **calibration as a gate with CRPS ranking inside it** — which on this evidence would
adopt `HIST` at 60 minutes on NIFTY and leave BANKNIFTY to CRPS inside a gate both models pass.
That means the rule must tolerate the two indices answering differently, which is why they are
kept separate throughout and why no pooled verdict is defined.

A widened IV-only range is the third candidate this evidence most directly suggests. It is
**deliberately out of scope**: adding a model after seeing these numbers is exactly the selection
the protocol exists to prevent, so it belongs in a separately declared comparison.

---

## 4 · Development checks from the handoff's table

| check | result |
| --- | --- |
| Unchanged R7, **12** November at 60 min | NIFTY 246 matched rows, flat MAE 24.35, original 24.41 → **SPLIT**. BANKNIFTY 251 rows, 47.91 / 52.98 → **SPLIT**. Both SPLIT under the existing rules. |
| Unchanged R7, **16** November at 60 min | **NOT RUN** — export absent (§6) |
| Common-centre 60-min reproduction, **16** November | **NOT RUN** — export absent (§6) |
| **12 November NIFTY at 120 minutes** | **Reproduced exactly.** `IV_ONLY_RAW` CRPS **31.69** against the original **30.84**; IV-only nominal 80% coverage **41.4%**. All three to the precision stated. |
| Historical timestamps | The pinned 855-row file matches `f6f29ed8…` byte-for-byte; 11 sessions, 21 Oct – 4 Nov. It carries **no scheduled-slot provenance**, so every join is labelled inferred (§7.1). |
| Edge cases | Missing target, insufficient history, invalid forward, unsolvable IV and mismatched contract all have named refusals; a lost row stays in coverage; results and decision state are order-independent. All asserted (§5). |

### 4.1 · The counterexample survives recentring — it was never a location problem

This is the substantive finding from keeping the counterexample. Recentring moves NIFTY 120-minute
CRPS by **0.17** (31.687 → 31.857) and coverage by **under one point**. The reason is structural:
because the entry IV is *fitted from the contract's own midpoint*, the model reproduces the entry
price by construction and the only drift over the hold is time decay. So the recentring is close
to a no-op, and the 41% coverage is **a width problem**, not a mislocation. At the same horizon
`HIST` covers 75.9% with a width of 134.3 against IV-only's 81.3.

If the earlier "before recentering" figures came from a distribution with a real location error,
that error came from somewhere other than the centring step — most likely the quoted IV or the
underlying reference rather than the midpoint fit.

---

## 5 · Verification

**Against the frozen replay.** `premium_repricing_v2.py`'s admission block is not importable on
its own, so the runner mirrors it and a test asserts the mirror reproduces the frozen replay
exactly at 60 minutes:

* identical `(contract, minute, entry acquisition, exit acquisition)` row sets — 246 NIFTY, 251
  BANKNIFTY;
* identical `entry_candidates` (318 / 323), `rows_scored` (246 / 251), `usable_records`
  (318 / 323) and `rej_no_exit_quote_within_tolerance` (72 / 72);
* `R7_E0_FWD` reproducing the frozen `E0_FWD` CRPS to six decimals — **17.567** (NIFTY) and
  **39.489** (BANKNIFTY).

If the mirror ever drifts, that test fails rather than a score nobody can explain.

**Test output.** 52 checks. 45 run anywhere; 7 need the real inputs and skip loudly without them.

```
Ran 52 tests in 2.46s
OK                                       # with RANGE_EXPORT / RANGE_SPOTS set
OK (skipped=7)                           # without them
```

What they pin, beyond the parity check: `norm_ppf` against known quantiles to 1e-9 and as the
exact inverse of the pricer's own normal CDF; IV fit round-trip across σ ∈ [0.05, 0.80], both
sides, three strikes; quote-bound failures named and **never clamped to a bracket endpoint**;
recentring invariance (shift the input by any constant, recentre, scores identical), idempotence,
and the clip accounting; the MAE tie; the historical median-removal leaving shape untouched under
a pure drift; invalid scenario forwards counted not priced; the four slot-policy behaviours
including `explicit` failing closed; every named refusal reachable; and order independence of both
the scores and the decision state.

**One guarantee the tests turned up, now documented in the code.** Clipping at zero touches only
values below the median once the shift has placed the median on a positive centre, and a median is
unmoved by anything strictly on one side of it. So **whenever the centre lies inside the
contract's own no-arbitrage bounds — which a two-sided midpoint does unless the book is crossed —
the centring is exact however much tail is clipped.** Exactness can only be lost if the centre
itself is outside the bounds, which is reported (`centred_exactly: false`) rather than absorbed.
An earlier draft of the test asserted the opposite and was wrong.

---

## 6 · Blocked: one file

The 16 November evening export, SHA-256
`903225d9826dfbcac5011a099da1706f901854d40bc2e6d02d7ebdd43f9625e1`, is **not on this machine**.
The ChatGPT library links in the handoff are not reachable from here. That blocks exactly two
checks:

* unchanged R7, 16 November at 60 minutes — 342 matched rows per index; NIFTY flat MAE 20.78 and
  original 25.39; BANKNIFTY 58.56 and 66.49;
* the common-centre 60-minute reproduction — NIFTY IV-only 14.37 against historical 15.45;
  BANKNIFTY 41.31 against 43.82.

**That one file is the only missing input.** The 12 November export (`5f399acc…`) and the pinned
spot history (`f6f29ed8…`) are both present and hash-verified. The command in §1 runs both checks
the moment the export arrives, and I have not estimated, interpolated or asserted any of those
six numbers in the meantime.

Direction check while it is outstanding: on 12 November at 60 minutes NIFTY shows `IV_ONLY`
better than `HIST` by 7.46%, and the 16 November expectation (14.37 against 15.45) is IV-only
better by 7.0%. Same direction, similar magnitude — consistent with the two constructions being
in the same family, which is a plausibility note and not a verification.

---

## 7 · Two discrepancies with the handoff

### 7.1 · The historical file carries no scheduled-slot field

The handoff states the review package has **explicit scheduled-slot fields on 233 of 855**
historical rows. The pinned file whose SHA-256 matches the handoff's own
(`f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186`, 855 rows, 11 dates) carries
**no scheduled-slot field on any row**. Its complete field set is `d`, `t`, `nf`, `bnf`,
`app_brain_version`, `rec_nf_dir`, `rec_bnf_dir`. The only partially-present fields are the
recorded directions, non-null on **234** rows — all from 2026-11-02 to 11-04, sampled value
`UNAVAILABLE`. A recorded direction is not a scheduled slot, and 234 is not 233.

So either the claim refers to a different file in the review package, or it is a misreading of
those 234 rows. Please point me at the file if it is the former. Until it is resolved, **every
historical join in these results reads an inferred slot and is labelled so**; the `explicit`
policy exists, is tested, and refuses this source by name rather than inferring quietly:

```
ValueError: slot policy "explicit" requires a scheduled-slot field on every row; this source
carries one on 0 of 855 rows (fields seen: none). An inferred slot must not be presented as a
scheduled one -- use "exact" or "floor5" and the manifest will label it inferred.
```

What *is* measurable on the file: **41 of 855** observation minutes are off the five-minute grid
(09:36, 10:01, …, 15:31), spread over 9 of the 11 sessions. Flooring them to a five-minute slot
collides with an existing on-grid observation **0 times**, so on this sample `floor5` loses no
observation — which is why it is usable as the labelled sensitivity. It still yields an inferred
slot. The default remains `exact`, which is what the frozen replay reads.

The join's own accounting is reported per run rather than assumed: NIFTY 60 minutes used 2,370
historical pairs, with 186 missing entry endpoints and 150 missing exit endpoints across the 246
rows × 11 sessions. Nothing is interpolated.

### 7.2 · The quoted and fitted IVs agree — no model-to-quote mismatch in these rows

The handoff says "the current evidence shows a model-to-quote mismatch". On 12 November, with the
parity forward and the ACT/365 clock, the quoted IV and the IV fitted from the midpoint agree
closely:

| index | rows | median (quoted − fitted), vol points | min | max | fit failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| NIFTY | 246 | **+0.010** | −0.645 | +0.900 | 0 |
| BANKNIFTY | 251 | **−0.006** | −0.467 | +0.559 | 0 |

The convention stays an **open question** — I have not established what the broker uses. What is
settled is that it is not visible here. The likeliest source of earlier mismatch evidence is the
underlying reference rather than the convention: R7's own 2.0 correction found index spot with
`r = 0` produced entry basis errors of 8.27 (NIFTY) and 58.67 (BANKNIFTY) points, **antisymmetric
in calls and puts** — a wrong underlying, not a wrong volatility — and replacing spot with the
parity forward removed it. If the mismatch evidence predates that correction, it is explained.

---

## 8 · One design decision the evidence forced

**The quantile grid had to be pinned, and the reason is measured, not stylistic.** Mean CRPS is
insensitive to the grid; nominal 80% coverage is not. NIFTY, 120 minutes, `IV_ONLY_RAW`:

| grid N | 21 | 31 | 41 | 51 | 101 | 201 | 401 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| cov80 | 35.6% | 37.4% | 38.5% | 39.1% | **41.4%** | 42.0% | 42.0% |
| CRPS | 31.732 | 31.700 | 31.696 | 31.690 | 31.688 | 31.687 | 31.686 |

A six-point coverage swing is wider than the distance between a passing and a failing calibration
gate, while CRPS moves 0.15% across the whole range. **A protocol that gates on coverage must pin
the grid, or the gate decides on an implementation detail.** Pinned at **201**: the smallest grid
where coverage is converged (unchanged to 401) and CRPS is converged to four significant figures.
Mean `width80` still creeps up slowly with N — the 10th and 90th percentiles of a discrete grid
approach the continuous quantiles from inside — so width is reported with the grid pinned and
never compared across grids. `HIST` is grid-independent by construction and the tests assert it.

This also **reconciles the handoff's own figure**: 41.4% is exactly the 101-point grid, and the
companion CRPS 31.69 is the un-recentred IV-only range at any grid from 41 upward. Both reproduce;
the reference implementation evidently used ~101 points. `--grid-sensitivity <INDEX>` reproduces
the table above from the runner.

---

## 9 · Coverage ledger and the trial definition

Coverage is built from **intended opportunities**, defined before any later price is read: one per
(pinned contract, nominal capture minute) entry candidate, from the capture schedule and the
pinned universe. NIFTY 60 min: 318 intended → 246 matched exits (72 refused
`rej_no_exit_quote_within_tolerance`) → 246 with a usable forward, a fitted IV and usable history
→ 246 common scored. BANKNIFTY: 323 → 251 → 251. **A row with no exit has no computable CRPS and
still counts in the denominator**, so a session that stopped capturing cannot read like a session
that scored. Every variant's own row count is printed beside the common set.

`clock_event_time_verified` is **0** on every row of both indices: no verified exchange quote
event clock exists in this export, so every number here rests on the app's acquisition receipt,
which bounds when a price could be known and never how old it was.

The draft protocol (`PROTOCOL_RANGE_TRIAL_DRAFT_v1.md`) carries your proposed settings as
**proposals**: ≥5% mean CRPS improvement at 60 minutes, nominal 80% coverage between 70% and 90%,
decision points at 10/15/20 sessions. It adds what they need to be operable:

* **minimum usable coverage** — a session enters the sample only if `common_scored ≥ 0.60 ×
  intended`, per index at the primary horizon. 12 November is 77% (NIFTY) and 78% (BANKNIFTY). A
  session below it is reported and excluded whole, with its ratio and leading refusal reason; an
  incomplete session is not a failed model;
* **session means as the unit** — rows within a session are not independent (neighbouring strikes
  and overlapping windows see the same few underlying moves), so rows average within a session
  per index and sessions average across. Your decision points are counted in sessions, so that
  has to be the unit the rule reads;
* **a repeated-looks rule** — three looks at a 5% threshold is three chances to cross it by
  noise. A look may stop for adoption only if the threshold is met **and** the gate passes **and**
  the paired sign count agrees (`HIST` better in ≥7 of 10, ≥11 of 15, ≥14 of 20 — the one-sided
  sign test at roughly 5%); may stop for futility if the point estimate is on the wrong side of
  zero at 15 sessions; otherwise continues. Not crossing is not evidence of equivalence;
* **coverage by time to expiry and by horizon**, not only pooled — `days_to_expiry` is carried on
  every row for that breakdown;
* **the rebuilt prior window stays fixed** for the first controlled comparison. A rolling-history
  variant is listed separately and needs its timestamped inputs and update rule specified first.

Quote calibration and coverage are reported **separately** from after-cost Paper economics; no
number in this reply is an after-cost result. The fee, fill and exit evidence a Paper decision
needs lives in `tools/premium_followon/` with its own ledger and lifecycle tests.

---

## 10 · Scope

Offline research tooling and fixtures only. No production ranking, gate, sizing, exit, teacher
fee or notification change; no database write; no push to `main`; no build or release. The frozen
60-minute pilot keeps its existing model, metrics, data rules and 16 November SPLIT result for
both indices. The overnight-capture and Paper-ledger work remains on its own branch
(`review/claude-premium-followon-prep-20261116`, `c73b28c`). No raw phone export, Paper position
or credential is in the tree or in this document.

**Next checkpoint, in your hands:** the 16 November export, so §6's two checks can run; and a
decision on §3.1 — which criterion governs when CRPS and the calibration gate disagree — so the
protocol can be frozen before the first scored fresh session.
