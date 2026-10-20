# b511 E1 — evaluation completeness diagnosis (2026-10-18)

Scope: diagnose first; change code only for a defect reproduced red-then-green.
Production access: read-only (`BEGIN READ ONLY`, statement timeouts, bounded aggregates),
project fdynxkfxohbnlvayouje, 18 Oct 2026 08:46–09:20 IST. No insert, backfill or ACL change.
Evidence files: `rollout/b511_evidence/prod_e1_*.json` (pack).

## 1. Facts (production, read-only)

| Session | Primary BNF/NF | Secondary | Rejected | Outcome write (IST) | Evaluated by (published IST) |
|---|---|---|---|---|---|
| 08 Oct | 54 | 486 (≈9/snap, from `top_candidates_json`) | — | 08 Oct 16:39 | 2.6.68 |
| 09 Oct | 72 | 0 | 0 | 09 Oct 17:27 | 2.6.68 |
| 12 Oct | 70 / 0 | 0 | 0 | 12 Oct 17:09 | ≤2.6.69 |
| 13 Oct | 34 / 0 | 0 | 0 | 13 Oct 16:47 | ≤2.6.72 (2.6.72 published 15:52) |
| 14 Oct | 15 / 60 | 0 | 0 | 14 Oct 17:22 | ≤2.6.73 (2.6.74 published 19:19) |
| 15 Oct | 16 / 58 | 0 | 0 | 15 Oct 17:12 | ≤2.6.75 (2.6.76 published 17:32) |
| 16 Oct | 16 / 59 | 11,416 / 7,259 | 570 | 17 Oct 11:25–11:34 | 2.6.78 (b509) |

* Every primary-only day has one identical `created_at` (one statement) and an
  `ml_daily_accuracy` row written 13 s later: the old run completed "successfully".
* Remote `ml_brain_snapshots` for 12–16 Oct all carry full menus: 200 ranked
  (`snapshot_ranked_candidates_full`) and full rejected menus per primary-bearing snapshot.
* `ml_option_chain_snapshots` is complete for 9, 12–16 Oct (76–78 polls/day, 09:15–15:40 IST,
  ≥96.6 % two-sided quotes; the NF same-day expiry on 13 Oct thins after 13:00 as expected).
* No trigger, cron schema or SQL function touches the outcome tables; no client code deletes them.
* `ml_evaluation_runs` and `ml_evaluation_metrics` hold no rows for these dates.

## 2. Root cause A — secondaries/rejected absent 9–15 Oct

Reproduced offline by running each released brain on the production snapshot 7103 (13 Oct
12:35 IST) and its real chain rows:

* 2.6.67, 2.6.68, 2.6.69, 2.6.71, 2.6.72, 2.6.73, 2.6.75: primary only; every ranked candidate
  raises `TypeError: '<=' not supported between instances of 'NoneType' and 'int'` at
  `_legacy_h2_structure_valuation` (`lot_size <= 0`).
* 2.6.76 and 2.6.78: primary and secondaries valued, no errors.

Mechanism: Android cannot read the lot-table asset from Python (Chaquopy), so ≤2.6.75 fell back
to the embedded table with no rules and `_candidate_lot_size` returned `None` for candidates
without `lotSize`. b499 (2.6.68, 7 Oct) began persisting the 200-ranked/rejected cohorts as
evaluable projections without `lotSize`; the primary keeps it. The TypeError was caught as a
non-fatal per-candidate error, so the menu vanished while the run, labels and accuracy
aggregate looked complete. 8 Oct snapshots were captured by 2.6.67 (`top_candidates_json`
carries `lotSize`), hence its 9 secondaries per snapshot.

Already fixed in production code: b507 (2.6.76, `ca6e2ad`) packaged
`_contract_lot_table_data.py` (digest-checked). The first session evaluated on a fixed build
(16 Oct, by b509) has full secondaries and rejected rows. b508/b509 did not cause or fix A;
they made the large (≈150 MB, 19,320-row) outcome population persist and verify.

b511 hardening (red-then-green, `test_b511_e1_lot_unavailable_accounting.py`, fixture derived
from snapshot 7103): `lot_size is None` now takes the existing
`MISSING_ENTRY_PREMIUM_OR_INVALID_LOT` branch, so a future lot-resolution gap becomes an
accounted teacher drop (`entry_execution_basis_unavailable`) instead of a swallowed exception.
Behaviour with an available table is unchanged (test asserts identical valuation).

## 3. Root cause B — 13 Oct morning primaries missing (35 identities)

* 72 snapshots (ids 7066–7137). Matched outcomes are exactly the newest 35 rows
  (#38 7103 12:35:43 IST … #72 15:40); every older primary-bearing snapshot (09:25–12:30) is
  unmatched. All are BNF 2026-10-27, lane BNF_intraday, WAIT, non-labelable.
* Chain inputs were complete all morning. Replaying 2.6.72 on unmatched snapshots 7101/7102
  values the primary normally, so they never reached the evaluator.
* 2.6.72 evaluation input: remote fetch `EVALUATION_SNAPSHOT_PAGE_SIZE = 1`, `maxPages = 80`;
  any failed page makes the read incomplete (the all-dates second source cannot complete), and
  the run silently uses `EvaluationLocalCache` — a bounded recent cache (row cap 60 → 36,
  byte cap 64 → 48 MiB, oldest dropped first). A newest-first trim to 35 rows matches the
  observed suffix exactly. Device logs are not available, so the remote-page failure is
  inferred, not observed.
* Defect still present on main (7037f44): the frozen manifest, `expected_identity_ids` and the
  b509 readback assessment are derived from the input file itself; `SnapshotReconciler` only
  resolved IDs of rows present. A truncated fallback therefore verifies as complete.

b511 fix (red-then-green, `EvaluationInputCoverageB511Test`, 72-remote/35-input shape):
`SnapshotReconciler.requireCoversRemote` compares the resolved input with the complete remote
identity list (`fetchEvaluationSnapshotIdentities`, fail-closed paging) before Python runs.
A strict subset throws `EVAL_INPUT_INCOMPLETE: missing=N …`, deletes the prepared-input marker
so the retry refetches Supabase, and leaves the run in the existing retryable failure state
(no outcome upload, no labels saved, no C3). Complete inputs are unaffected.
Residual: `maxPages = 80` with page size 1 is unchanged (no session has exceeded 78 polls); a
longer session now fails closed as `EVAL_INPUT_INCOMPLETE` instead of truncating silently.

### 3a. R1 (Codex review): empty prepared input

The first b511 gate ran only when `snapshotCount > 0`. If the full-payload fetch fails and the bounded
local fallback is empty, Python accepts `[]` and the zero-snapshot branch recorded DONE ("no brain
snapshots found") whenever `last_poll_date` was another date. That is a false DONE for a session
Supabase still holds.

Fix: the coverage window is extracted as `EvaluationInputCoverageGate`, and the service calls it
unconditionally right after preparation:
* `verify` always runs the remote identity lookup.
  - Remote identities the input lacks → `EVAL_INPUT_INCOMPLETE`.
  - Lookup failed or capped → `EVAL_INPUT_UNVERIFIED`.
  - Both invalidate the prepared marker, keep the inputs and the selected date, and land in the
    existing retryable failure.
* `decideZeroSnapshots` refuses DONE unless the lookup proved remote empty.

Red → green: `EvaluationEmptyInputCoverageR1Test`.
* Red: 6 of 9 failed against the faithful extraction of the old semantics. Empty input reached
  DONE with no lookup, including after a failed or capped lookup.
* Green: 9 of 9.

Limit: the Android Service itself is not instantiable in JVM tests (no Robolectric). The tests drive
the service's real gate and reconciler through a host with real files and prefs, in the service's
order. A source-contract test pins the service wiring and ordering.

## 4. Identity coverage and statuses (current main + b511)

| Status | Meaning | Where |
|---|---|---|
| complete | every expected primary/secondary/rejected identity proven by server readback | `EvaluationIdentityCoverage` (b509 bounded readback, unchanged) |
| missing | produced identities absent server-side (`PARTIAL_IDENTITY_COVERAGE`), or input subset incl. empty input (`EVAL_INPUT_INCOMPLETE`, new) | coverage assessment / `EvaluationInputCoverageGate` |
| input-unverified | remote identity lookup failed/capped before evaluation (`EVAL_INPUT_UNVERIFIED`, new); retryable, never DONE | `EvaluationInputCoverageGate` |
| verification-unavailable | readback error or timeout (`IDENTITY_READBACK_FAILED`) — never treated as complete | `readEvaluationOutcomeIdentityKeys` |
| deferred | cooperative time budget (`EVALUATION_TIME_BUDGET_EXCEEDED`) → continuation alarm | `MarketMLService` |

16 Oct coverage (read-only SQL, 17–18 Oct): 18,750 evaluation rows + 570 rejected; all 52
labelable frozen chosen identities and all 75 produced primaries matched both required tables.
Rejected-candidate evidence exists only from 16 Oct (root cause A suppressed it earlier).

## 5. late_pending

`SnapshotStageHealth.late_pending` is a cumulative per-session event counter incremented by
`CaptureEngine.onLatePending` when a caller's wait expires while the original computation stays
owned; it is not a queue depth. Measurement design (no code in b511):
unresolved_late = late_pending − late_built − late_exception − terminal_loss(late keys);
outbox depth = durable outbox files present; missing acks = queued − upload_ack per session.
Report all three per session; never infer depth from late_pending alone.

## 6. Historical recovery — feasibility only (nothing inserted)

Inventory: 9, 12–15 Oct remote snapshots carry frozen identities and full menu provenance
(`evidence_source`, `ranked_population_size`, sampling fields); chain rows are complete; label
contract teacher_v1 / tc_2026_07_A is unchanged; affected builds are 2.6.68–2.6.75.
Procedure (requires Vivek's approval before any write):
1. Offline dry run on the box with current brain from read-only exports of snapshots + chain.
2. Recompute stored primaries and require byte-identical teacher fields per
   (snapshot_id, candidate_id, role); any difference stops the date.
3. Candidate rows = recomputed − stored identities (secondary, rejected, 13 Oct primaries).
4. Insert with `Prefer: resolution=ignore-duplicates` (the client default is merge-duplicates,
   which would overwrite), in bounded batches, then exact identity readback.
5. Rerun daily aggregates only after readback proves completeness.

## 7. Future field gate

Three consecutive sessions on the b511 build, planned 19, 21 and 22 Oct 2026 (20 Oct is not a
session in the current calendar). Pass per session: complete status, zero `EVAL_INPUT_INCOMPLETE`,
secondary and rejected rows present, readback exact. No pass is claimed here.
