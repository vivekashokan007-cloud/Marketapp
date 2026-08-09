# PC2 Batch 4 - G0 Delete Proof

## Scope

- Local cached snapshot inputs only.
- No Supabase calls.
- No app behavior change.
- Compared current hard-VIX G0 behavior against a proposed variant where hard VIX thresholds are removed and existing `ivPercentile` state remains authoritative for Force 3.

## Result

- Rows checked: `1601`.
- Missing `ivPercentile` rows: `0`.
- `vix >= IV_HIGH(20)`: `0`.
- `vix >= IV_VERY_HIGH(24)`: `0`.
- `vix <= IV_LOW(15)`: `1597`.
- `ivPercentile < 25`: `1601`.
- `ivPercentile > 65`: `0`.
- Varsity routing deltas: `0`.
- Force3 deltas: `0`.
- Verdict: `BYTE_IDENTICAL_ON_CACHE`.

## Important Boundary

- `IV_HIGH` and `IV_VERY_HIGH` are unreachable in this cache.
- `IV_LOW` is reachable, so deleting it is not automatically safe.
- The proof is byte-identical here because cached rows have `ivPercentile` present, and it already puts the force state into LOW.
- If future rows lack `ivPercentile`, deleting `IV_LOW` would remove a fallback. That should be handled explicitly in live wiring, not hidden inside this proof.

## Recommendation

- Treat G0 delete as locally proven for cached behavior.
- Do not remove runtime hard-VIX constants in isolation yet.
- In Batch 5, switch the G0/force/routing consumer to context-percentile authority with an explicit missing-context fallback and persisted `gate_basis`/`switch_basis` evidence.

## Outputs

- `PC2_BATCH4_G0_DELETE_PROOF.json`
- `PC2_BATCH4_G0_DELETE_PROOF.md`
