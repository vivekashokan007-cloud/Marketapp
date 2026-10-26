# Environment / credential classes (names only) — Track D

**Authority:** documentation only. Do not copy production secret values into fixtures or this packet.

| Class | Example env names | Where used | Notes |
| --- | --- | --- | --- |
| Publishable / legacy anon | `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Oracle → Supabase REST (e.g. `elephant_assessments`) | Class confirmed in repo; **runtime class on live VM unverified** (no VM access this batch) |
| Gemini / upstream LLM | `GEMINI_API_KEY` | `/elephant`, `/monthly_eval` waits | Recurring API cost; distinct from VPS compute |
| TLS material | Let's Encrypt paths under `/etc/letsencrypt/live/<host>/` | nginx / uvicorn historically | Renewal is operational; not authorised now |
| Service-role / secret | `SUPABASE_SERVICE_ROLE_KEY` | Edge Functions only (not Oracle thin API) | Must **not** be placed on a public VPS for the thin API scope |
| Host OS | SSH keys, sudoers | Hostinger VPS | Least privilege; separate from app secrets |

Rules for a future test host:
- Use a **test-scoped** anon key / project if available; never paste production `.env` into CI fixtures.
- Old and new hosts must not share a job lock or write destination that permits duplicate job writes.
- EnvironmentFile must be sourced, never rewritten by restart scripts (historical Oracle defect).
