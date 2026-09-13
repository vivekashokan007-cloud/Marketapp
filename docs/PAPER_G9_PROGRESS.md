# PAPER + G9 Progress — 2026-09-13

**Status:** Local implementation on isolated branch. **NOT pushed.**  
**Publishing pause:** strict — no push, no merge to main, no APK/PWA publish, no production Supabase migrations, no ml_train enablement, no live orders, no sizing promotion.

Successor to / continuation of `G8_PROGRESS.md` (G8 tip retained as parent commit).

## Tip SHAs (local only)

| Repo | Branch | Tip SHA | Note |
|---|---|---|---|
| Marketapp | `work/g8-g10-integrity-20260913` | `8cfc326896bac245b495a73b0ffeeaf15f2bc718` | Paper analysis + lineage + G9 advisory |
| MarketVivi | `work/g8-g10-integrity-20260913` | `97999706c6a771d4fc98968143419b3efc2fc77e` | Paper analysis alternatives UI; cache `app.js?v=1336` |
| Prior Marketapp G8 | same branch | `531b77e4ca21769250ad8cdaa5e1f941cb0433a3` | parent |

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
- **Still incomplete for final review:** live Kotlin per-batch remote persist during evaluation loop (cursor is contract-ready; end-of-run `saveEvaluationOutcomes` remains the write path); on-device restart/notification delivery proof; Gradle/Android suite blocked locally (no JDK).

### A4 — ML evaluation lineage (G4/G5/G6)
- `evaluation_outcome_lineage.py` stamps/verifies session, snapshot, candidate, model hash, feature schema, policy selector, net-target, exit-policy, run ledger, metrics contract versions.
- Wired into `_eval_single_candidate` after G4 net-target annotation.
- Mixed model/target cohorts refused via `refuse_mixed_lineage_cohort`.

## B) G9 sizing research (no promote)

- New `g9_sizing_kelly.py` (`g9_sizing_kelly_advisory_v1_20260913`).
- Fractional Kelly + Bayesian (Beta prior) Kelly; capped by max-risk %, exposure, concentration, margin, liquidity lot cap, max lots.
- Output labelled `experimental_advisory_only`; `mutates_trade_quantity=False`, `mutates_risk_limits=False`, `p_ml_gate_unchanged=True`.
- Live quantity passthrough remains 1-lot operating assumption.
- Promotion: `<20` sessions or `<60` closed trades → `no_promotion_insufficient_multi_session_evidence`; even above thresholds `recommend_promote=False` (separate review required).
- Claude’s original +38,633 / 627-test patch still **not supplied** — this is an independent advisory scaffold, not that patch.

## Tests

```text
python3 -m unittest discover -s app/src/main/python/tests -q
Ran 731 tests in ~3.5–3.9s
OK
```

New focused coverage: paper analysis, outcome lineage, notification/E3 contracts, G9 Kelly, PWA/NativeBridge source contracts (~20 tests).

## Changed files (high level)

**Marketapp**
- `app/src/main/python/paper_analysis_eligibility.py` *(new)*
- `app/src/main/python/evaluation_outcome_lineage.py` *(new)*
- `app/src/main/python/notification_lineage.py` *(new)*
- `app/src/main/python/e3_persistence_contract.py` *(new)*
- `app/src/main/python/g9_sizing_kelly.py` *(new)*
- `app/src/main/python/brain.py`
- `app/src/main/java/.../NativeBridge.kt`
- `app/src/main/python/tests/test_paper_analysis_eligibility.py` *(new)*
- `app/src/main/python/tests/test_evaluation_outcome_lineage.py` *(new)*
- `app/src/main/python/tests/test_notification_lineage_e3.py` *(new)*
- `app/src/main/python/tests/test_g9_sizing_kelly.py` *(new)*
- `app/src/main/python/tests/test_paper_g9_source_contracts.py` *(new)*

**MarketVivi**
- `app.js` — paper analysis alternatives lane
- `index.html` — `app.js?v=1336`

**Docs (mr-g8plus workspace)**
- `PAPER_G9_PROGRESS.md` *(this file)*
- `G8_PROGRESS.md` — pointer retained

## Explicit non-actions

- No `git push` / merge / APK / PWA publish
- No production migrations / G3 `--apply` / Auth cutover
- No `ml_train.run` / `online_update` / automatic promotion
- No live broker orders; `OrderExecutionService` stays SANDBOX
- No removal/weakening of live `p_ml` gate
- No silent live quantity / risk-limit change from G9

## Still incomplete for final review report

1. Wire E3 batch cursor into Kotlin evaluation loop for mid-run remote upserts (contract exists; runtime still end-of-run save).
2. Device/CI: NotificationAgent recovery after process kill; PositionTickService lineage delivery evidence.
3. JDK/Gradle Android unit tests.
4. Multi-session Real-only sizing evidence inventory (G9 remains no-promote).
5. Optional: surface advisory Kelly lots in PWA as labelled experimental readout (not wired to order qty).
6. G8 remaining: multi-page export fetch; champion/challenger offline CLI.
