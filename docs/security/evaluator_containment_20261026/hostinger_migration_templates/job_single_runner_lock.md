# Job single-runner lock — Track D

Goal: old Oracle host and a future Hostinger (or test) host must not run the same scheduled/job workload concurrently or produce duplicate writes.

Proposed mechanisms (choose one; none applied now):

1. **systemd flock** on the API unit (`/run/marketradar-oracle.lock`) — prevents two local uvicorn instances.
2. **Application advisory lock** (preferred for cross-host): before starting `/monthly_eval` or any write-path job, acquire a Supabase/Postgres advisory lock or a single-row lease table (`job_leases`) with TTL and holder id = `host_id`.
3. **DNS/cutover exclusivity**: only one host receives production traffic; the other is stopped or bound to a test hostname only.

Fail criteria if both hosts are live against the same write destination without a lease: duplicate `elephant_assessments` / eval rows, conflicting status transitions, or double Gemini spend.

Decommission of old routes/credentials is a **separate explicit decision** after acceptance — not authorised in this batch.
