# tools/direction_research — direction study preparation (DRAFT, synthetic only)

Codex task 2026-10-29 section 4A plus the Codex v2 addendum. **Not a frozen protocol, not a scorer.** Nothing
here reads production data, Supabase, outcomes or the network, and no app code imports it
(`test_tooling_is_not_imported_by_app_code`). Confirmatory scoring and PASS output are refused
(`dr_core.confirmatory_verdict`) until an approved freeze record exists; none exists and none is invented.

| File | Purpose | Deps |
|---|---|---|
| `protocol/DIRECTION_PROTOCOL_DRAFT_V0_20261029.json` | machine-readable DRAFT protocol/schema; unresolved fields explicit; `confirmation_enabled: false` | — |
| `dr_core.py` | validator/normaliser, identity, status axes, duplicate handling, reference/end selection (no earlier-price fallback), causal B-MAJ/B-TREND, paired session table, blinded monitor, cohort feasibility, stdlib MBB, freeze gate | stdlib |
| `dr_synth.py` | synthetic record builders (synthetic weekday calendars, no holidays) | stdlib |
| `make_fixtures.py` | writes/checks `fixtures/` byte-identically | stdlib |
| `tests/test_dr_counterexamples.py` | counterexamples; each shows the intended wrong implementation (`tests/wrong_impls.py`) fails | stdlib |
| `dr_oc_sim.py` | OC utility: scenario registry, per-scenario seeds, checkpoint/resume, MC uncertainty | numpy, scipy |
| `dr_e2e_sim.py` | end-to-end synthetic price/forecast paths through the real `dr_core` pipeline | numpy |
| `dr_oc_report.py` | renders OC JSONL into tables | stdlib |
| `oc_results/` | committed OC outputs (JSONL, rendered tables) with environment and config hashes | — |
| `reference_claude_20261029/` | Claude's v2 simulation sources and CSVs, byte-identical to the handoff (attributed reference inputs, CRLF CSVs preserved) | — |

Commands (from the repository root):

    python3 -m unittest discover -s tools/direction_research/tests -p 'test_*.py' -v
    python3 tools/direction_research/make_fixtures.py --check
    # OC (needs numpy 2.1.3 / scipy 1.14.1 for byte-reproduction of committed results):
    python3 tools/direction_research/dr_oc_sim.py --out oc_results_calib.jsonl --seed-set calib --workers 8
    python3 tools/direction_research/dr_oc_sim.py --out oc_results_valid.jsonl --seed-set valid --workers 8
    python3 tools/direction_research/dr_e2e_sim.py --out e2e.jsonl --reps 400 --n-eval 150 --skill 0.0 --workers 8

Gap handling in the bootstrap concatenates evaluable sessions in order. That is a modelling choice, not
calendar-time equivalence; scheduled positions and missing runs are retained in every audit output.

## Codex review corrections (2026-10-29, R1–R3)

- **R1 – observation chronology.** `Study._select` keeps first-selected-invalid rejection (never skips forward) and now
  also rejects: unknown/undeclared `clock_id`/`source_id` (or not in an optional allow-list) → `*_CLOCK_UNKNOWN`;
  declared clock uncertainty above `max_clock_uncertainty_s` (5 s) → `*_CLOCK_UNCERTAIN`; a feed time demonstrably
  later than its own fetch completion or `as_of` (`feed − u > fetch`) → `*_FUTURE_FEED`. Bounded skew: a feed time may
  exceed fetch time by at most the observation's declared uncertainty `u`; feed and fetch are never compared across
  different declared clocks. The same rules apply to S0, S1 and the historical end observations that feed B-MAJ history
  and the B-TREND anchor (which additionally needs feed < C).
- **R2 – S_dec provenance.** `Study.input_spot_status` links the input spot to the forecast's own watermark poll
  (`poll_id` must match if present; fetch completion = poll time within `input_poll_tolerance_s` + `u`), requires it to be
  known by the forecast's availability time, the cutoff C and `as_of`, with valid price, trusted clock, non-future feed
  and age ≤ 60 s. Failure gives an explicit `B_TREND_INPUT_*` B-axis exclusion (never repaired or substituted); the slot
  leaves the rule and both baselines together (identical paired denominators). NF and BNF are validated independently.
- **R3 – coverage.** Forecast visibility is restricted to `as_of` before stream/identity/duplicate decisions (records with
  unknown availability are visible only to an unbounded audit). With `Study(manifest=[...])` expected streams/slots come
  only from an explicit deployment manifest (entries visible once `established_at ≤ as_of`; identity used for a slot only
  if established by its cutoff, else `DEPLOYMENT_IDENTITY_UNKNOWN`; record/manifest disagreement →
  `IDENTITY_MANIFEST_CONFLICT`; streams absent from the manifest are listed in `unmanifested_streams`, not scored).
  Missing slots are counted even when a stream/day has no records. Without a manifest a stream starts at its first
  visible record and missing slots carry only an EARLIER observed identity — never a later one.
- Tests: `tests/test_dr_review_r1_r3.py` (Codex probes as assertions + counterexamples, prefix-stability sweep).

### Dependency check of committed results after R1–R3

- `oc_results/oc_results_{calib,valid}.jsonl` and `OC_TABLES_20261029.md`: `dr_oc_sim.py` simulates d-series directly
  and does not import `dr_core`; no dependency on the corrected code, not rerun.
- `oc_results/e2e_results.jsonl`: `dr_e2e_sim.py` runs through `dr_core.Study`, so it depends on the fix. It now passes an
  explicit manifest. All four configurations were regenerated with identical args/seeds
  (`oc_results/e2e_results_r2_20261029.jsonl`); every rate, count, mean d and schedule percentile is identical to the
  committed run, and `e2e_rep_equivalence.py <ec11f65 checkout> <this checkout> 20` found 0 of 60 replications with
  different d series, schedules, ties or row counts.
- `fixtures/fx_golden_audit_base_v0.json`: regenerated; rows differ only by the added `input_spot_status` field and the
  `identity_basis` diagnostic (session table and blinded monitor byte-equal in content). `fx_counterexamples_v0.json`
  unchanged.
- Interpretation: the e2e skill-0.10 pass rate 0.827 (95% MC ≈ 0.780–0.865) is a deliberately skilled synthetic
  forecaster on the NF T1 path only; it is not a brain-accuracy result nor a validation of T2, BNF or regime-spanning
  inference. Two-index inflation figures are `P1_any_index` (family-wise: any index passes), not "both pass". Nominal
  alpha 0.0125 is supported only for the reported synthetic scenarios and is not an adopted protocol threshold.
