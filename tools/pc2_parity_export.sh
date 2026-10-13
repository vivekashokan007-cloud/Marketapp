#!/usr/bin/env bash
# DB-1 parity export: READ ONLY. Produces the inputs of Pc2ParityAudit for one
# explicit IST day, [D 00:00+05:30, D+1 00:00+05:30).
#
#   PC2_DB_URL='postgresql://...' tools/pc2_parity_export.sh 2026-10-01 out_dir
#   PC2_AUDIT_DIR=out_dir ./gradlew :app:testDebugUnitTest --rerun \
#       --tests 'com.marketradar.app.Pc2ParityAuditRunTest'
#
# Round 4 (Codex A1, A3):
#  - All five queries run in ONE psql session inside a single
#    REPEATABLE READ READ ONLY transaction, so the files describe one
#    consistent database state even while the phone is still writing.
#  - inventory.jsonl is the independent poll inventory: the distinct poll
#    times in ml_option_chain_snapshots, which the phone persists BEFORE the
#    brain runs.
#  - Stored poll times are parsed with public.pc2_poll_utc, which returns NULL
#    instead of raising, so a malformed row cannot abort the export. Every
#    malformed row in the WHOLE batch table is counted into the manifest,
#    and the audit fails on any.
#  - b503 R6: each snapshot carries poll_summary_json.capture_provenance; a late capture whose origin
#    session is this day is exported even when it completed after midnight.
#  - b503 C3: generated.jsonl holds the retained generated-candidate sample rows for the cohort audit
#    (CaptureCohortAudit / CaptureReadiness: PC2_AUDIT_DIR=out_dir ... --tests 'com.marketradar.app.CaptureReadinessRunTest').
#  - manifest.json is written last, atomically, with each file's row count and
#    SHA-256. Without it, or if anything disagrees with it, the audit cannot
#    certify. Any manifest left from an earlier run is removed first.
set -euo pipefail

day="${1:?usage: pc2_parity_export.sh YYYY-MM-DD [out_dir]}"
out="${2:-pc2_parity_${day}}"
[[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "date must be YYYY-MM-DD" >&2; exit 2; }
: "${PC2_DB_URL:?set PC2_DB_URL to a connection string}"
mkdir -p "$out"
rm -f "$out/manifest.json" "$out/manifest.json.tmp" "$out/pc2_parity_report.json" \
      "$out/snapshots.jsonl" "$out/batches.jsonl" "$out/legacy.jsonl" "$out/inventory.jsonl" \
      "$out/malformed.json" "$out/generated.jsonl" "$out/capture_cohort_report.json" "$out/capture_readiness_report.json"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

export PGOPTIONS='-c default_transaction_read_only=on'
psql "$PC2_DB_URL" -X -q -At -v ON_ERROR_STOP=1 -v day="$day" -v out="$out" <<'SQL'
begin transaction isolation level repeatable read read only;
select (:'day' || ' 00:00:00+05:30')::timestamptz as lo,
       (:'day' || ' 00:00:00+05:30')::timestamptz + interval '1 day' as hi,
       ((:'day' || ' 00:00:00+05:30')::timestamptz at time zone 'UTC') as lo_utc,
       ((:'day' || ' 00:00:00+05:30')::timestamptz + interval '1 day') at time zone 'UTC' as hi_utc
\gset

\o :out/snapshots.jsonl
select pg_catalog.json_build_object(
         'poll_ts_utc', to_char(poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
         'session_date', session_date::text,
         'compact_ref', context_json -> 'snapshot_pc2_authority_compact_ref',
         'decisions', context_json -> 'snapshot_pc2_authority_decisions',
         'policy', context_json -> 'snapshot_pc2_authority_policy',
         'brain_version', context_json -> 'snapshot_brain_version',
         'capture_provenance', poll_summary_json -> 'capture_provenance',
         'recommendation_id', recommendation_id,
         'generated_cohort_manifest', poll_summary_json -> 'generated_cohort_manifest')::text
  from public.ml_brain_snapshots
 where (poll_ts >= :'lo'::timestamptz and poll_ts < :'hi'::timestamptz)
    -- b503 R6: a late capture of this day that completed after midnight belongs to this day's audit.
    or (poll_summary_json -> 'capture_provenance' ->> 'session_date') = :'day'
 order by poll_ts;

\o :out/batches.jsonl
select pg_catalog.row_to_json(b)::text
  from (select batch_id, session_date_text, poll_ts_text, brain_version, policy_hash,
               policy_version, authority_diagnostics_version, grouping_schema_version,
               digest_algorithm, decision_count, distinct_decision_count,
               decision_digest, grouped_digest, grouped_canonical, complete
          from public.ml_pc2_decision_batches
         where session_date_text = :'day'
            or (public.pc2_poll_utc(poll_ts_text) >= :'lo_utc'::timestamp
                and public.pc2_poll_utc(poll_ts_text) < :'hi_utc'::timestamp)
         order by poll_ts_text, batch_id) b;

\o :out/legacy.jsonl
select pg_catalog.json_build_object(
         'poll_ts_utc', to_char(poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
         'decision_index', decision_index,
         'decision_json', decision_json)::text
  from public.ml_pc2_authority_decisions
 where poll_ts >= :'lo'::timestamptz and poll_ts < :'hi'::timestamptz
 order by poll_ts, decision_index;

set extra_float_digits = 1;
\o :out/generated.jsonl
-- b503 C3 (round 4, generated_row_witness_v2): the retained generated-candidate sample rows of the same origin
-- session (lineage = the frozen fact-pack poll time in snapshot_poll_ts, NOT the snapshot completion time), with
-- EVERY column except the server-owned id/created_at, so the audit recomputes the full typed witness from the
-- actual stored values. float8 columns are exported as their float8out text (extra_float_digits = 1: shortest
-- text that reads back to the identical double), never through numeric, which would lose -0 and widen NaN.
select ((pg_catalog.to_jsonb(g) - 'id' - 'created_at' - array['net_premium','max_profit','max_loss','premium_edge','ev_per_1k','est_cost','sigma_otm','iv_richness','credit_width_ratio','brain_score','p_ml','ml_edge','signal_independence_score']::text[]) || pg_catalog.jsonb_build_object(
         'snapshot_poll_ts_utc', to_char(g.snapshot_poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
         'net_premium', g.net_premium::text,
         'max_profit', g.max_profit::text,
         'max_loss', g.max_loss::text,
         'premium_edge', g.premium_edge::text,
         'ev_per_1k', g.ev_per_1k::text,
         'est_cost', g.est_cost::text,
         'sigma_otm', g.sigma_otm::text,
         'iv_richness', g.iv_richness::text,
         'credit_width_ratio', g.credit_width_ratio::text,
         'brain_score', g.brain_score::text,
         'p_ml', g.p_ml::text,
         'ml_edge', g.ml_edge::text,
         'signal_independence_score', g.signal_independence_score::text))::text
  from public.ml_generated_candidates g
 where session_date = :'day'::date
    or (snapshot_poll_ts >= :'lo'::timestamptz and snapshot_poll_ts < :'hi'::timestamptz)
 order by snapshot_poll_ts, index_key, candidate_id, rank;

\o :out/inventory.jsonl
select pg_catalog.json_build_object(
         'poll_ts_utc', to_char(poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))::text
  from (select distinct poll_ts from public.ml_option_chain_snapshots
         where poll_ts >= :'lo'::timestamptz and poll_ts < :'hi'::timestamptz) c
 order by 1;

\o :out/malformed.json
select pg_catalog.json_build_object(
         'count', count(*),
         'sample', coalesce(pg_catalog.json_agg(batch_id) filter (where rn <= 50), '[]'::json))::text
  from (select batch_id, row_number() over (order by batch_id) as rn
          from public.ml_pc2_decision_batches
         where public.pc2_poll_ts_valid(poll_ts_text, session_date_text) is not true) m;
\o
commit;
SQL

finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$out" "$day" "$started" "$finished" <<'PY'
import hashlib, json, os, sys
out, day, started, finished = sys.argv[1:5]
files = {}
for name in ("snapshots.jsonl", "batches.jsonl", "legacy.jsonl", "inventory.jsonl", "generated.jsonl"):
    raw = open(os.path.join(out, name), "rb").read()
    rows = sum(1 for line in raw.decode("utf-8").split("\n") if line.strip())
    files[name] = {"rows": rows, "sha256": hashlib.sha256(raw).hexdigest()}
malformed = json.load(open(os.path.join(out, "malformed.json")))
manifest = {
    "complete": True,
    "ist_day": day,
    "window": f"[{day} 00:00+05:30, +1 day)",
    "consistency": "single REPEATABLE READ READ ONLY transaction",
    "export_started_utc": started,
    "export_finished_utc": finished,
    "inventory_source": "ml_option_chain_snapshots (distinct poll_ts)",
    "malformed_batch_rows": malformed["count"],
    "malformed_batch_sample": malformed["sample"],
    "files": files,
}
tmp = os.path.join(out, "manifest.json.tmp")
with open(tmp, "w") as f:
    json.dump(manifest, f, indent=2)
    f.flush(); os.fsync(f.fileno())
os.replace(tmp, os.path.join(out, "manifest.json"))
print(f"exported {files['snapshots.jsonl']['rows']} snapshots, {files['batches.jsonl']['rows']} batches, "
      f"{files['legacy.jsonl']['rows']} legacy rows, {files['inventory.jsonl']['rows']} inventory polls; "
      f"{malformed['count']} malformed batch rows table-wide -> {out}")
PY
