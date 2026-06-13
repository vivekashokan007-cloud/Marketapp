# Reconciliation Scope Audit For b248-b252

This audit maps the distinct work shipped in `b248` through `b252` to the governing directive/study set, and flags where implementation moved ahead of an explicit directive.

## Governing references

- `UNIFIED_ARCHITECTURE_STUDY_V3_FOR_OPENCLAW`
- `DIRECTIVE_OPENCLAW_SIGMA_FIX_AND_Q7_GUARDS_20260610`
- `LEARNING_CONTROL_PLANE_ANSWERS_20260611`
- `DIRECTIVE_OPENCLAW_RECONCILIATION_b252_20260611`

## Release-by-release map

| Release | Distinct change | Files | Authorization |
| --- | --- | --- | --- |
| `b248 / v2.4.17` | Compact generated-candidate persistence groundwork (`ml_generated_candidates` write path, bounded cap) | `MarketWatchService.kt`, `SupabaseClient.kt` | `LEARNING_CONTROL_PLANE_ANSWERS_20260611` — authorized next step after payload slimming |
| `b249 / v2.4.18` | Bounded trace/rejection payload persistence (`attempt_stats`, bounded rejection sample, rejection stats) | `brain.py`, `SupabaseClient.kt` | `UNIFIED_ARCHITECTURE_STUDY_V3_FOR_OPENCLAW` — bounded observability prerequisite before Round 2 |
| `b250 / v2.4.19` | Native ML lane summary fetch path | `SupabaseClient.kt` | `UNAUTHORIZED — roadmap execution ahead of directive`; operational reporting repair, not yet described in Claude directives |
| `b251 / v2.4.20` | WebView bridge export for lane summary | `MainActivity.kt` | `UNAUTHORIZED — follow-on repair for b250 bridge omission`; required to make shipped reporting path callable |
| `b252 / v2.4.21` | Round 0 qualitative Elephant observe-only prompt v1, coherence handoff, parity framework scaffolding | `oracle_server/evaluator_app.py`, `brain.py`, `MarketWatchService.kt`, Python test files | Partially authorized by `UNIFIED_ARCHITECTURE_STUDY_V3_FOR_OPENCLAW` (Round 0) but superseded in detail by `DIRECTIVE_OPENCLAW_RECONCILIATION_b252_20260611` |

## File-level reconciliation focus

### `oracle_server/evaluator_app.py`

- `b252` introduced Round 0 qualitative observe-only schema and normalized flag persistence.
- **Reconciliation result:** the original `qualitative_prompt_v1` schema moved ahead of the later reconciliation directive and required correction to `qualitative_prompt_v2`.

### `app/src/main/python/brain.py`

- `b249` added bounded observability controls around trace and rejection persistence.
- `b252` added `quality_tag` and `coherence_signal` into the Elephant fact pack.
- **Reconciliation result:** the fact-pack work was directionally authorized, but the schema tag and deterministic coherence penalty were incomplete until the current reconciliation patch.

### `app/src/main/java/com/marketradar/app/MarketWatchService.kt`

- `b248` added compact generated-candidate persistence groundwork.
- `b252` added qualitative handoff fields for Elephant.
- **Reconciliation result:** compact candidate persistence was authorized by the learning-control-plane answers; qualitative handoff is authorized by Round 0, but quality-tag defaults required v2 alignment.

### `app/src/main/java/com/marketradar/app/SupabaseClient.kt`

- `b248` and `b250` added persistence/reporting helpers for generated candidates and lane summaries.
- **Reconciliation result:** these were operational fixes and data-contract support. They were not explicitly spelled out in Claude’s directive sequence, so they should be treated as pragmatic runtime repairs rather than architecture-authority work.

### `app/src/main/java/com/marketradar/app/MainActivity.kt`

- `b251` exported `getMLEvaluationLaneSummary()` to the WebView bridge.
- **Reconciliation result:** pure bridge repair, not architecture work. Necessary after `b250`, but not independently authorized by a Claude directive.

## Current compliance status after this reconciliation patch

1. **Round 0 schema**
   - `qualitative_prompt_v2` now matches the reconciliation directive.
2. **Candidate notes**
   - `support` stance removed.
   - Notes are explicitly display/logging only.
3. **A6 coherence wiring**
   - `signal_coherence()` now subtracts confidence only on caution.
   - Positive/aligned coherence is explicit no-op.
4. **Oracle-only writer invariant**
   - No app-side `elephant_assessments` write path present.

## Remaining architecture work outside this audit

- Live proof of `ml_generated_candidates` writes on a candidate-producing poll
- Rich chain parity fixture capture during market hours
- Round 1 deterministic coherence rollout verification under fixture gate
- Any future Round 2 gate reform only after parity fixture freeze
