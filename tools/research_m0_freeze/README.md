# CI-3 offline freeze verifier (CDX-S2-BUILD-R1 R4)

Offline research tooling only; not shipped in the APK.

- `freeze_inputs/SPECIAL_SESSION_PROTOCOL_V1_20261023.md`: **immutable** protocol text, `special_session_protocol_v1`,
  full-file SHA-256 `cf65c2ada8f91da1f077836935eb6fbb15e78e39b8689bedbbae2679a4b74aa5`. Never edit; any edit is a new version.
- `freeze_inputs/SPECIAL_SESSION_DECISION_RECORD_APPROVED_20261023.json`: the actual approved record (Vivek, FREEZE,
  2026-10-23T17:49+05:30, Codex ref CDX-M0-PLAN-V3), SHA-256 `70274ccdab7aa1bdedaf50955a1f94552089238d95c46ad405725779fcefdec9`.
  Not re-created, re-dated or re-approved.
- `freeze_inputs/verify_freeze_record_20261023.py` (Codex-supplied, SHA-256 `e9a80bc2…2448`): exit 0 FREEZE, 2 blank,
  3 invalid/unreadable (impossible dates, malformed JSON, wrong types), 4 DO_NOT_FREEZE.
- `test_freeze_verifier.py` (Codex-supplied, `e9a323b1…293b`): 8 regressions.

`ResearchExport.PROTOCOL_SHA256` in the app is bound to the protocol file by `M0Stage2R1Test`.
