# M4.1 SELECTOR-OBJECTIVE TEST — live-data replay

**Date:** 2026-08-24 · **Method:** read-only Supabase join, no code changed.
**Data:** `ml_generated_candidates` (ex-ante economics) ⋈ `ml_brain_snapshots` (menu, via `date_bin('5 min', poll_ts)`) ⋈
`ml_recommendation_outcomes` (realized `r_multiple`, `price_integrity='OK'`). Selectable menu only: `direction_safe`, not `capital_blocked`, ≥2 candidates.
**Objective = pick the menu candidate that maximizes X ex-ante; score = its realized r_multiple; average over menus.**
Session range 2026-06-01 → present. R normalized by risk_at_entry (0.6×max_loss), per the teacher (M9.3).

## Result — mean realized R by selection objective

| Objective | ALL (808 menus) | net-era stamped (178) | pre-stamp (630) |
|---|---:|---:|---:|
| **Stored primary (what the app chose)** | 0.0518 | **−0.0591** | 0.0832 |
| Menu mean (random pick) | −0.0201 | −0.0232 | −0.0192 |
| Max absolute edge | 0.0800 | −0.0042 | 0.1038 |
| Max edge-per-risk `(edge/max_loss)` | **−0.1745** | **−0.2761** | −0.1457 |
| Max ev_per_1k (R-native) | 0.0800 | −0.0042 | 0.1038 |
| **Oracle (best realized R in menu)** | 0.2419 | 0.1572 | 0.2658 |

Hit-rate (ALL): stored 33.0% positive · ev_per_1k 51.0% · oracle 86.3%.

## Findings

**1. The live paper-primary's own key — edge-per-risk — is empirically the WORST objective tested (−0.17 all / −0.28 net-era).**
This is the exact key `_pc2_paper_primary_sort_key` leads with (M4.1/M4.2). Ranking by edge-per-risk actively DESTROYS value: it over-promotes
tiny-credit structures whose ratio is inflated by a small denominator. Turning it OFF is the single highest-confidence change available.

**2. On CURRENT (net-era stamped) builds the stored selector is NEGATIVE (−0.0591) and WORSE THAN RANDOM (−0.0232).**
This is not a historical artifact — it is the recent-code behavior, and it independently reproduces the 08-18 report's "sometimes worse than
random" (08-10/12/13). The selector is presently subtracting value versus blindly picking from its own menu.

**3. The objective mismatch is real but BOUNDED — it is ~15–30% of the gap, not the whole residual.**
Swapping stored→best-simple-objective recovers only ~+0.03R all-era (0.052→0.080) / ~+0.05R net-era (−0.059→−0.004). The R-native
`ev_per_1k` TIES absolute edge — it does not beat it — and neither approaches oracle. On net-era builds the best simple objective is still
≈0 while oracle is +0.157. → **~0.16R of residual is NOT the economic ranking key. It is family/strike/structure selection**
(matches 08-18: STRATEGY_FAMILY_MISS 156, SAME_FAMILY_RANK_MISS 179). Fixing the objective alone will not close the residual.

## Verdict & recommended fix order (evidence-backed)

1. **Stop leading the paper selector with edge-per-risk** (remove `-edge_per_risk`/composite-70%-per-risk as primary authority). Highest
   confidence, biggest single downside removed (−0.28R → ~0). Data-supported, low-risk.
2. Replace with absolute net edge OR ev_per_1k (statistically tied here; ev_per_1k has better hit-rate 51% vs 33% — prefer it for downside).
   Expected recovery ~+0.05R net-era — gets from clearly-losing to break-even, NOT to oracle.
3. The remaining ~0.16R is FAMILY/STRIKE selection. Next experiment must isolate family choice (was the right family even surfaced/ranked?),
   which is the real value lever — and which loops back to F0.6/M9.1 (if the right family is a multi-day IC that's blocked, no objective can pick it).

CAVEATS: net-era subset (178 menus) is single low-VIX regime and slightly build-mixed (2.5.94–2.5.98). `premium_edge` persisted may be
gross for some rows. Directionally robust (edge-per-risk worst in BOTH eras; stored≈random or worse in both).
