# Proposed cutover procedure — TEMPLATE ONLY (Track D)

**Not authorised to execute.** Requires a later decision naming cost, impact, and exact DNS/TLS change.

## Preconditions
1. Test endpoint validated across multi-session plan (see `multi_session_validation_plan.md`).
2. Containment C1–C3 decision state recorded (independent track; migration does not require Hostinger).
3. Versioned source hash of `oracle_server/evaluator_app.py` and deps pinned; unknown deployed behaviour listed as gap.
4. Job lease / single-runner lock verified on test host.
5. Backup: Hostinger weekly backup enabled; independent application-consistent copy of config + any local state taken.

## Ordered steps (proposed)
1. Provision Hostinger KVM (later decision) in India region if offered; install OS template; create `marketradar` user; deploy versioned tree to `/opt/marketradar`.
2. Configure EnvironmentFile with **test** credential classes; start systemd unit bound to `127.0.0.1:8000`.
3. Configure nginx on a **test hostname** (not production) with TLS; do not change `marketradar-oracle.online` yet.
4. Run multi-session validation against the test hostname.
5. If pass criteria met: schedule cutover window; lower DNS TTL in advance (Hostinger DNS today: NS `*.dns-parking.com`, SOA `dns.hostinger.com`).
6. Stop or idle-job-disable the Oracle process **before** switching A record (or switch A then immediately stop Oracle) so both cannot write.
7. Point A record of `marketradar-oracle.online` to the new Hostinger IP; renew/issue TLS for that host.
8. Health/readback: `GET /health` only; confirm app callers (`MarketWatchService` → `/elephant`, `NativeBridge` → `/evaluation-jobs*` — **gap**: checked-in Oracle lacks evaluation-jobs routes).
9. Observe ≥1 full market session + evening processing before declaring provisional acceptance.
10. Separate decision: retire Oracle VM / revoke old credentials / disable old routes.

## Stop conditions
- TLS handshake failure or cert name mismatch
- Duplicate writes detected
- CPU-credit throttle causing missed evening deadline
- Authentication/denial regressions on Supabase paths
- Any need to re-open unauthenticated evaluator Edge handlers (forbidden as automatic recovery)

## Recovery
- Revert DNS A record to `144.24.117.114` (Oracle) **only if** that host is still intentionally runnable and its process is restarted under the fixed `restart.sh` (no `.env` clobber).
- Do **not** treat redeploy of known-vulnerable unauthenticated Edge Functions as automatic recovery.
- Application-consistent restore from Hostinger backup/snapshot if the new host is corrupt; verify lease table before re-enabling jobs.
