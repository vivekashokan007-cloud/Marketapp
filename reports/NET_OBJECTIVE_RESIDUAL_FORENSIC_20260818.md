# Pre-Fix Net Objective Baseline Forensic

Date: 2026-08-18

Scope: June 1, 2026 through August 17, 2026. The goal was to use historical pre-fix evidence as a baseline, fix the confirmed gross-vs-net objective mismatch in local code, and identify what still needs to be re-tested under the new net objective.

## What Was Fixed

The live brain was ranking and gating candidates using gross economics while the teacher judged outcomes after friction. This meant small-profit candidates could look good before costs and then fail in the teacher outcome.

Implemented fix:

- Candidate builders now compute teacher-style friction and net economics.
- Ranking uses `netPremiumEdge`, `netProbProfit`, and net max profit/loss when available.
- Entry eligibility uses net edge after teacher friction.
- Net-aware live candidates fail closed if net economics are missing, instead of falling back to gross values.
- Gross fields remain preserved for teacher/evidence comparison.

## Verification

Focused tests:

- `test_net_economics_authority.py`: 6 passed.
- `test_build3_a8_nf_ab.py`: 14 passed.

Full Python brain suite:

- `python3 -m unittest discover -s Marketapp/app/src/main/python/tests -p 'test_*.py'`
- Result: 310 tests passed.

## Pre-Fix Forensic Study Ran

Report folder:

- `reports/brain_forensic_residual_netfix_20260818/selection`
- `reports/brain_forensic_residual_netfix_20260818/replay`
- `reports/brain_forensic_residual_netfix_20260818/study`

Strict sample:

- 56,158 strict outcome rows.
- 596 eligible decision menus.
- 4,148 snapshot rows.
- 6,041 strict rejected sample rows.

## Main Finding

The gross/net mismatch was real. The historical baseline also shows other failure modes, but these are pre-fix findings and must be re-baselined after the net objective is active.

The pre-fix failures are mostly selection-quality failures:

1. Strategy family mismatch

The brain often chose the wrong family, such as Iron Butterfly when Bear Call later proved better, or Bear Call when Bull Put later proved better.

Evidence:

- `STRATEGY_FAMILY_MISS`: 156 menus.
- 2026-08-13: best family mostly `BEAR_CALL`, primary mostly `IRON_BUTTERFLY`.
- 2026-08-17: best family `BEAR_CALL`, primary mostly `IRON_BUTTERFLY`.

2. Same-family width/strike miss

Sometimes the broad family was correct, but the selected strikes/width/center were poor.

Evidence:

- `SAME_FAMILY_RANK_MISS`: 179 menus.
- 2026-08-10 had heavy same-family miss while primary and best were often both Iron Butterfly.

3. Selectors disagreed and the selected PC2 primary often underperformed

Even when a better generated candidate existed in the same live menu, the actual PC2 paper-primary selector often preferred the weaker one. Deterministic rank-1 was a different selector and cannot be treated as the direct cause of the chosen primary.

Evidence:

- Deterministic rank 1 improved over surface primary: mean R 0.0884 vs 0.0760, showing selector disagreement.
- Oracle best accepted was far higher: mean R 0.2676.

4. Premium/probability/ML signals can mislead selection

Raw premium edge is better than current primary on average, but still far below oracle. ML probability and high probability-of-profit can favor bad candidates when the economics are tiny or costs dominate.

Evidence:

- `PREMIUM_EDGE_FAVORED_PRIMARY`: 342 menus.
- `ML_PROBABILITY_FAVORED_PRIMARY`: 121 menus.
- `PROBABILITY_MODEL_FAVORED_PRIMARY`: 72 menus.

5. PC2/teacher attribution is still partly blind

The current stored evidence does not fully persist the exact PC2 rank tuple for every generated candidate, so many misses cannot be explained exactly.

Evidence:

- `PC2_EXACT_RANK_MISSING_FOR_PAIR`: 501 menus.
- `BEST_NOT_IN_SNAPSHOT_TOP_JSON`: 352 menus.

6. Supply is not the main issue, but still exists

Many better candidates were already generated but not selected. Some days also show best candidates missing from generated joins or top snapshots.

Evidence:

- `BEST_NOT_IN_GENERATED_JOIN`: 93 menus.
- `BEST_NOT_IN_SNAPSHOT_TOP_JSON`: 352 menus.

## Recent Day Pattern

The sharpest recent failures:

- 2026-08-10: primary mean R -0.0884 vs random menu mean R 0.0022.
- 2026-08-13: primary mean R -0.1537 vs random menu mean R -0.0528.
- 2026-08-12: primary mean R -0.1317 vs random menu mean R -0.0995.

This means the brain was sometimes worse than a simple random selection from its own generated menu.

## Next Research Direction

The next layer should not be another broad gate. It should isolate authority by stage:

1. Family selector research

Test whether market context should choose family first: Bear Call, Bull Put, Iron Butterfly, Iron Condor. Use breadth, MW, NF/BNF divergence, candle context, walls, and VIX context.

2. Width/strike optimizer research

Within a selected family, test width, center, and distance from spot separately. The August failures show width/center is a separate problem from family choice.

3. Net objective backtest

Replay historical generated menus using net premium edge and teacher-style friction as the objective, not gross max profit or raw probability.

4. Telemetry patch

Persist the exact PC2 rank tuple, family score, width score, net economics, and rejection reason for every generated candidate so future failures are explainable without guessing.

5. ML recalibration

Treat ML as advisory until it is calibrated by regime and net outcome. Do not let raw ML confidence override bad net economics or out-of-distribution checks.

## Bottom Line

The first fix aligns local live selection with teacher net economics. This report is the pre-fix baseline. Next, replay historical menus under the net objective, then decide how much family selection and width/strike optimization still remain.
