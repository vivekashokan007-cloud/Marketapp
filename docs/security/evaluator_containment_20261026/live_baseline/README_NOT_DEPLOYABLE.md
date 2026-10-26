# Live baseline capture — REVIEW-ONLY AUDIT EVIDENCE, NOT A DEPLOYABLE OR RECOVERY TARGET

Captured read-only on 26 Oct 2026, 06:30–06:31 IST (01:00–01:01 UTC) from Supabase project fdynxkfxohbnlvayouje
through the management tools (list/get Edge Function, read-only catalog SELECTs). Nothing was invoked or changed.

* `function_source/<slug>/functions/...` — the exact files returned for each deployed function (entry + helpers).
  These are the currently deployed, UNAUTHENTICATED service-role handlers. Redeploying them re-opens C1.
  They are not the original compiled bundle (ezbr_sha256 is the platform bundle digest); the helpers import
  `https://esm.sh/@supabase/supabase-js@2` (mutable remote import), so a byte-identical rebuild is not guaranteed.
* `deployed_function_source_snapshot_20261026.json` — full returned objects (id, slug, version, status,
  verify_jwt, import_map, entrypoint_path, ezbr_sha256, created/updated_at, files). No secret values.
* `function_drift_20261026.json` — per-file SHA-256 and comparison with the 25 Oct Codex snapshot (no drift).
* `catalog/` — read-only catalog SELECT results for the evaluator / ai_* objects (relations, ACLs, policies,
  columns, constraints, views, triggers, functions, roles, default ACLs, statistics counters, row counts).

Never redeploy repo main's old handlers as a rollback either: main's evaluator-proposals-review writes
ai_branch_proposals (the brain-consumed table) whereas live v2 writes approved_branch_proposals.
