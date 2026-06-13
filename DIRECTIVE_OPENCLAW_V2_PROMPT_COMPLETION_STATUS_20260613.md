# V2 Prompt Completion Status — 2026-06-13

## Completed locally

- Rewrote the Oracle `build_elephant_prompt()` instruction body so the model is explicitly asked:
  - whether bearish positioning is **genuine directional fear** or **protective hedging**
  - whether the picture tells **one consistent story** or is **materially conflicted**
- Kept the v2 schema unchanged:
  - `distribution_signal = genuine|hedging|ambiguous|unclear`
  - `coherence_read = aligned|conflicted|unclear`
  - `QUALITATIVE_SCHEMA_VERSION = qualitative_prompt_v2`
- Confirmed the prompt explicitly says there is **no `support` stance**.
- Left the normalizer enums unchanged.
- `brain.py` quality tag remains `qualitative_prompt_v2`.

## Local verification passed

- Prompt contains: `genuine`, `hedging`, `protective`, `consistent story`
- Prompt does not offer `support` as a stance option
- `python3 -m py_compile oracle_server/evaluator_app.py`
- `git diff --check`
- `test_round0_elephant_schema.py`

## Still pending

- **Live proof of `ml_generated_candidates` writes** on a real candidate-producing poll.
- **Oracle redeploy and manual `/elephant` probe** returning schema-valid `normalized_flags` under the completed v2 prompt.

## Delivery status

- Directive §3 is implemented locally.
- Directive §4 remains operationally pending and cannot be honestly closed without live poll evidence.
