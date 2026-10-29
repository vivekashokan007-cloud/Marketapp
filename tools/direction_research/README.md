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
