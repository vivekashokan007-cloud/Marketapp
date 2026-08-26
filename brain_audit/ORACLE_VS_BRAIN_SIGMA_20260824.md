# ORACLE vs BRAIN — what the best picks look like, and what the brain picks instead
**2026-08-24 · read-only analysis · no code changed**
Basis: realistic trading unit (1–2 trades/day), teacher outcomes `price_integrity='OK'`, direction-safe & capital-safe only.

## 1. The discriminator is SIGMA DISTANCE (how far OTM the short strike sits)

| Family | ORACLE picks (best-2/day) | BRAIN primary picks |
|---|---|---|
| BEAR_CALL BNF | **0.57σ** · R **+0.192** (13) | **2.11σ** · R −0.017 (24) |
| BEAR_CALL NF  | **0.11σ** · R **+0.178** (4)  | **2.62σ** · R +0.027 (6) |
| BULL_PUT BNF  | **0.89σ** · R **+0.105** (5)  | **1.83σ** · R +0.009 (5) |

Consistent across every directional family and both indices: the oracle sells NEAR the money, the brain sells FAR OTM (2–4× further).

## 2. Outcome by sigma bucket (BEAR_CALL + BULL_PUT, all evaluated candidates)

| Bucket | n | mean R | % profitable |
|---|---:|---:|---:|
| **<0.5σ** | 1,417 | **+0.0288** | **57.3%** |
| 0.5–0.8σ (documented "sweet spot") | 957 | −0.0072 | 46.8% |
| 0.8–1.15σ (current MAX_SIGMA_OTM) | 2,471 | −0.0305 | 32.1% |
| 1.15–1.5σ | 1,498 | −0.0202 | 42.5% |
| 2.0–3.0σ (**where the brain picks**) | 842 | −0.0128 | 30.6% |
| **>3.0σ** | **16,945** | −0.0239 | **0.7%** |

Two facts: (a) the only profitable bucket is `<0.5σ` — which `MIN_SIGMA_OTM=0.5` penalises; (b) `>1.15σ` is **19,559 of 23,244 candidates (84% of the menu) at a 5.7% win rate** — the menu is dominated by structural losers.

## 3. Artifact test — per-day (this is NOT a single day-cell)

| day | n(<0.5σ) | near-money R | near win% | far (>1.15σ) R |
|---|---:|---:|---:|---:|
| 08-11 | 250 | −0.0497 | 9.2% | −0.0266 |
| 08-12 | 35 | −0.1070 | 20.0% | −0.0544 |
| 08-13 | 40 | +0.0044 | 52.5% | −0.0242 |
| 08-14 | 180 | +0.0167 | 48.3% | −0.0213 |
| 08-17 | 37 | +0.0711 | 73.0% | −0.0180 |
| 08-18 | 270 | +0.0488 | 94.8% | +0.0117 |
| 08-19 | 120 | +0.0134 | 65.8% | −0.0221 |
| 08-20 | 131 | +0.0603 | 53.4% | −0.0205 |
| 08-21 | 156 | +0.0468 | 57.7% | −0.0207 |
| 08-24 | 198 | +0.1067 | 76.8% | −0.0230 |

**Near-money positive on 8/10 days. Far-OTM negative on 9/10 days.** Persistent, not one cell.

## 4. THE CRITICAL CAVEAT — the downside tail is UNMEASURED

Across every bucket: `% stopped = 0.0`, `% near max-loss = 0.00`, worst R = −0.41.
No evaluated candidate ever approached max loss, because the teacher is **same-day** (M9.1/M9.2) — positions
never run overnight and never gap through a short strike. **The data physically cannot see the risk of selling
closer to the money.** Near-money credit spreads are precisely the trade that wins small and often in a calm
tape and then loses big on a gap. All 10 days are VIX ~11.

Therefore: the evidence that far-OTM is bad is SAFE to act on (removing losers adds no risk).
The evidence that near-money is good is NOT safe to act on yet (it would add unquantified tail risk).

## 5. Recommendation — asymmetric, deliberately

**DO (safe, evidence-robust):** stop the brain promoting far-OTM. `MAX_SIGMA_OTM=1.15` exists but is a SOFT
opportunity gate, so far-OTM candidates leak into ranking and become primary at 2.1–2.6σ. Make sigma distance
an explicit ranking penalty above ~1.15σ. This removes candidates that lose on 9/10 days and 94.3% of the time.
Expected effect: moves brain picks out of the −0.02R zone without adding tail risk.

**DO NOT (yet):** lower `MIN_SIGMA_OTM` below 0.5 to chase the near-money bucket. The tail is unmeasured, the
sample is 10 days in one VIX regime, and this is exactly the trade that blows up on a gap. This belongs in
Phase B (multi-day, next-open marking, max-loss realisation frequency) before any change.

**ALSO:** the >3σ flood (16,945 rows, 0.7% profitable) is the supply-quality issue the 08-14 journal entry
already flagged. Suppressing it at generation would cut menu noise ~70% with no loss of profitable candidates.

## 6. Family allocation (secondary finding)
Brain's best family by outcome is IRON_BUTTERFLY NF (R +0.333, 14 picks) — yet it over-allocates to
BEAR_CALL BNF (R −0.017, 24 picks) and BEAR_PUT NF (R −0.031, 20 picks): 44 of 91 primaries (48%) go to its
two worst families. Worth a separate allocation review.
