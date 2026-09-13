# PAPER + G9 Progress — 2026-09-13

**Status:** Local implementation on isolated branch. **NOT pushed.**  
**Publishing pause:** strict — no push, no merge to main, no APK/PWA publish, no production Supabase migrations, no ml_train enablement, no live orders, no sizing promotion.

Successor to / continuation of `G8_PROGRESS.md` (G8 tip retained as parent commit).  
**Final review:** [`REVIEW_REPORT_G8_G10_20260913.md`](./REVIEW_REPORT_G8_G10_20260913.md)

## Tip SHAs (local only) — FINAL

| Repo | Branch | Tip SHA | Note |
|---|---|---|---|
| Marketapp | `work/g8-g10-integrity-20260913` | `d471b4a4927ce27616b8d274d936fd3ef74ba336` | finish gaps + review docs |
| MarketVivi | `work/g8-g10-integrity-20260913` | `6163c72d296ea3f5b31246109044af9ca5965ad9` | Kelly readout + review docs |
| Marketapp feature (pre-docs) | same | `98a86c25cc2b9d07f785dabc9e55381c92f46dfd` | multi-page export / E3 honesty / champion CLI |
| MarketVivi feature (pre-docs) | same | `79582db1eabf5e9a291b496c624b5bba3c181a0a` | experimental Kelly PWA |
| Prior Marketapp G8 | same branch | `531b77e4ca21769250ad8cdaa5e1f941cb0433a3` | G8 integrity parent |

## Finish pass (2026-09-13 later)

1. **Multi-page deterministic export** — `SupabaseClient.selectAllPages` / `fetchRecentEvaluationOutcomesPaged` / `fetchRecentBrainSnapshotsPaged`; wired into `exportAppTrades` + `exportCanonicalEvaluationInputs` (replaces page-0+incomplete-only).
2. **E3 honesty** — mid-loop remote upsert **not** wired; runtime remains end-of-run `saveEvaluationOutcomes`; `e3_persistence_contract.RUNTIME_MID_LOOP_REMOTE_UPSERT=False`.
3. **Champion/challenger CLI** — `tools/champion_challenger_compare.py` (read-only, no promotion).
4. **Tests** — `python3 -m unittest discover -s app/src/main/python/tests -q` → **742 OK**.
5. **Optional PWA** — `experimentalKellyAdvisoryReadout` labelled experimental; not order qty; `app.js?v=1337`.

## A) Paper lane (sole experimental lane)

### A1/A2 — structurally valid non-primary paper analysis
- New `paper_analysis_eligibility.py` (`paper_analysis_eligibility_v1_20260913`).
- Admits candidates with valid 2/4-leg structure, expiry, lot, CE/PE types, positive entry quotes.
- **Ignores** ranking primary, advisory ML action/OOD/`p_ml`, entryConfidence, capital/direction soft blocks.
- Wired at end of `annotate_candidate_entry_eligibility` — **does not mutate** `entryEligible` / `entryGate` / Real recommendation.
- Compacted through brain `_candidate_view` (present-only to protect Android payload budget) + NativeBridge keys.
- PWA: `paperAnalysisAlternatives` section surfaces up to 5 NF + 5 BNF non-watchlist structurally valid candidates with **PAPER ANALYSIS** button; Real/Sandbox remain `finalEntryAuthorization`-bound.

### A3 — E3 persistence / notification lineage / recovery
- `e3_persistence_contract.py`: batch persistence cursor + streaming verification; refuses false-complete.
- `notification_lineage.py`: stable idempotency keys; device-recovery merge of `position_alert_states`.
- `NotificationAgent.snapshot_state` stamps `notification_lineage_version` and normalizes state shape for restart.
- Existing MarketMLService atomic checkpoint / upsert-on-conflict paths retained (source-contract tested).
- **Runtime write path:** end-of-run `saveEvaluationOutcomes` (documented). Mid-run remote upsert deferred.

### A4 — ML evaluation lineage (G4/G5/G6)
- `evaluation_outcome_lineage.py` stamps/verifies session, snapshot, candidate, model hash, feature schema, policy selector, net-target, exit-policy, run ledger, metrics contract versions.
- Wired into `_eval_single_candidate` after G4 net-target annotation.
- Mixed model/target cohorts refused via `refuse_mixed_lineage_cohort`.

## B) G9 sizing research (no promote)

- New `g9_sizing_kelly.py` (`g9_sizing_kelly_advisory_v1_20260913`).
- Fractional Kelly + Bayesian (Beta prior) Kelly; capped by max-risk %, exposure, concentration, margin, liquidity lot cap, max lots.
- Output labelled `experimental_advisory_only`; `mutates_trade_quantity=False`, `mutates_risk_limits=False`, `p_ml_gate_unchanged=True`.
- Live quantity passthrough remains 1-lot operating assumption.
- PWA experimental Kelly readout mirrors advisory posture only (client heuristic from `p_ml` + R:R).
- Promotion: `<20` sessions or `<60` closed trades → `no_promotion_insufficient_multi_session_evidence`; even above thresholds `recommend_promote=False` (separate review required).

## Tests

```text
python3 -m unittest discover -s app/src/main/python/tests -q
Ran 742 tests in ~3.6s
OK
```

Kotlin/JDK suite blocked locally (no JAVA_HOME).

## Explicit non-actions

- No `git push` / merge / APK / PWA publish
- No production migrations / G3 `--apply` / Auth cutover
- No `ml_train.run` / `online_update` / automatic promotion
- No live broker orders; `OrderExecutionService` stays SANDBOX
- No removal/weakening of live `p_ml` gate
- No silent live quantity / risk-limit change from G9

## Recommendation (from review report)

**Not ready** for next paper phase *promotion* gates. Ready only for continued paper-only observation under publishing pause.
