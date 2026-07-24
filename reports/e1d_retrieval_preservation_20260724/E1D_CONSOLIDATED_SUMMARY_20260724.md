# E1-D Retrieval Preservation Consolidated Summary - 2026-07-24

## Scope

- Offline research only.
- No Android/phone code changed.
- No live ranking authority.
- No Supabase access during model runs.
- Dataset source: `reports/e1b_context_scaling_20260723/e1b_all_roles_dataset.csv`.
- Test rows: primary-only.
- Context rows: strictly prior-day rows.
- Fixed context size: `256`.
- Model: `TabICL`.

## Retrieval Feature Safety

Retrieval distance excluded close-known/leaky fields:

- `bearish_close`
- `bullish_close`
- `day_direction`
- `day_range`
- `day_range_sigma`
- `downtrend`
- `inside_day`
- `outside_day`
- `uptrend`

Retrieval distance also excluded labels/outcomes/IDs and `role`/`training_role`; `role` remained available to TabICL as a model feature.

Numeric retrieval features:

- `buy_strike`
- `consec_days`
- `cost`
- `dte`
- `entry_credit`
- `gap_sigma`
- `max_loss`
- `max_profit`
- `move_sigma`
- `sell_strike`
- `sigma_away`
- `spot`
- `vix`
- `weekday`
- `width`

Categorical retrieval features:

- `buy_strike2`
- `day_group`
- `day_vix`
- `index`
- `is_credit`
- `mode`
- `sell_strike2`
- `strategy`
- `vix_regime`

## Completed Results

| strategy | rows | pooled AUC | mean within-day AUC | log-loss | Brier | ECE-10 | median latency | max latency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| recent_256 | 574 | 0.4596 | 0.5675 | 1.2465 | 0.3908 | 0.3740 | 11.3290s | 13.7980s |
| random_256 deterministic | 574 | 0.6911 | 0.7201 | 0.7031 | 0.2371 | 0.1227 | 2.5682s | 56.3216s |
| stratified_256 | 574 | 0.5272 | 0.5888 | 0.8568 | 0.3009 | 0.2487 | 1.9669s | 34.6689s |
| knn_256 3-day sample | 72 | 0.7081 | 0.7352 | 0.8376 | 0.2139 | 0.2366 | 1.7018s | 34.7568s |
| knn_256 full | - | - | - | - | - | - | runtime-blocked | timeout 1800s |

## Comparisons To Earlier Reference Points

Earlier E1/E1B references:

- E1 A3 primary-only full TabICL pooled AUC: `0.5991`
- E1 A3 primary-only full TabICL within-day AUC: `0.6829`
- E1B A2 primary 50% TabICL pooled AUC: `0.6487`
- E1B A2 primary 50% TabICL within-day AUC: `0.6888`
- Frozen deployed pooled AUC: `0.4267`
- Frozen deployed within-day AUC: `0.5369`

E1-D read:

- `random_256` beats E1 A3, E1B A2, and frozen deployed on pooled AUC and within-day AUC.
- `random_256` also has better calibration than E1 A3 TabICL:
  - E1 A3 TabICL log-loss `0.7821`, ECE `0.2122`
  - random_256 log-loss `0.7031`, ECE `0.1227`
- `recent_256` does not preserve accuracy.
- `stratified_256` does not preserve accuracy.
- `knn_256` 3-day sample is promising on AUC, but full k-NN is not currently runnable with the naive per-candidate implementation.

## Interpretation

E1-D supports Claude's warning that retrieval is not a harmless engineering optimization. Retrieval strategy changes accuracy materially.

What is evidenced:

- Fixed-size context can preserve or improve TabICL performance.
- Deterministic random 256-row context is currently the strongest completed full-run arm.
- Recent-only context is poor.
- Naive stratification by index/strategy is poor.
- k-NN has promising partial signal but cannot be accepted because the full run timed out.

What is not yet evidenced:

- k-NN RAG architecture is not validated by the full required test.
- ONNX/mobile deployment remains untested.
- Quantization calibration drift remains untested.
- Determinism on-device remains untested.

## Runtime Findings

- `recent_256` and `random_256` are day-batchable.
- `stratified_256` became tractable after batching by day/index/strategy.
- `knn_256` is query-specific and required one TabICL inference per candidate.
- Full `knn_256` was attempted with `timeout 1800` and exited with code `124`.
- Therefore naive per-candidate k-NN TabICL is currently too slow for full offline evaluation in this environment.

## Recommended Next Step

Do not proceed to Android/ONNX yet.

Next offline step should be one of:

1. Run leakage/sanity checks on `random_256` because it is unexpectedly strong.
2. Add batchable retrieval variants that are closer to RAG but not per-candidate expensive:
   - same day/index/strategy random 256
   - regime-bucket random 256
   - strategy/index weighted random 256
3. Optimize k-NN evaluation by caching contexts or running fewer representative candidate groups before claiming k-NN viability.

No integration is justified from this result alone.
