# Multi-session validation plan — prepare now, run only when authorised

Vivek rejects a ten-minute valuation. This plan covers **several complete market sessions**, evening processing, and overnight/restart behaviour. Infrastructure acceptance only — does **not** establish trading profitability.

## Duration (proposed before testing)
- Minimum: **5 complete market sessions** spanning ≥3 calendar days, including at least **2 overnight restarts** and **1 mid-job process restart**.
- Include the evening processing window the app already schedules (do not invent a shorter substitute).
- Record wall-clock start/end in Asia/Calcutta.

## What to measure
| Metric | Method | Budget / deadline (provisional) |
| --- | --- | --- |
| API latency p50/p95/p99 for `/health`, `/elephant` | synthetic clients + logs | p95 `/health` < 500 ms local-DC; `/elephant` dominated by Gemini wait — record upstream wait separately |
| Error rate / timeouts | nginx + app logs | < 1% 5xx excluding upstream 429; no silent drops |
| CPU credit behaviour | Hostinger dashboard + `mpstat` | Must not sit empty-balance limited during evening job; if limited, fail |
| Peak RAM | `ps` / cgroup MemoryMax | Headroom ≥ 25% under MemoryMax 1500M (tune after first session) |
| Job duration / retries / queue | app logs | Evening job completes before next session open; retries bounded |
| Disk / log growth | `du` daily | Logs bounded/rotated; no unbounded growth |
| Gemini/API cost | provider usage | Record only; not a VPS substitute |
| Duplicate/lost writes | Supabase row audit for test keys | Zero duplicates; zero lost expected rows |
| Restart mid-job | kill -TERM during job | Idempotent resume or clean fail + single retry; no double write |
| Network loss / upstream 429/5xx | fault injection on test host | Safe backoff; no credential leak in logs |
| Auth denial | call with bad/missing token if contract requires | Structured deny; no stack traces with secrets |

## Pass criteria
1. All 5 sessions complete evening processing within the stated deadline.
2. Zero duplicate writes; zero lost expected synthetic rows.
3. No empty-balance CPU throttle during the evening job window (or documented upgrade decision).
4. Overnight restart brings service healthy before next session (`/health` OK; systemd active).
5. Ordinary `/health` responsiveness retained while workers run.
6. Logs contain no secret values (scanned).
7. Application-consistent backup restore drill succeeds on a disposable clone (not production DNS).

## Fail criteria
- Missed evening deadline twice
- Any duplicate write to shared destinations
- Sustained empty-balance throttle during required work
- Need to re-open vulnerable unauthenticated handlers to "recover"
- OOM / MemoryMax kill during normal evening load
- DNS/TLS mis-issuance affecting the production hostname during test (test must use separate hostname)

## Distinction
- **Locally run this batch:** containment scratch suites only (A1–A5). No Hostinger VM existed to benchmark.
- **Future Hostinger tests:** this matrix, only after purchase/provision is explicitly authorised.
