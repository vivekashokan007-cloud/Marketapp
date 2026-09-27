-- ============================================================================
-- B1.1 — position_ticks.client_event_id  (PREPARED ONLY — NOT APPLIED)
-- ============================================================================
-- Status: NOT APPLIED to production. Applying it requires Vivek's explicit
-- approval (Codex R3 reply §3/§14). Do not run from CI or from the app.
--
-- Purpose: a stable client event identity so an exact retry of an already
-- persisted tick (e.g. server committed, HTTP acknowledgement lost) resolves
-- as "already persisted" instead of inserting a duplicate row.
--
-- client_event_id = lowercase hex SHA-256 of the canonical immutable tick
-- payload (contract: position_tick_client_event_id_v1, see
-- PositionTickIdentity.kt). Server-generated fields (id, created_at) and the
-- identity field itself are excluded from the hash.
--
-- Additive and nullable. No backfill. Existing 34,837 rows keep NULL.
--
-- Upload protocol (B1.1 drain): plain INSERT (no upsert). An exact retry of an
-- already-persisted chunk fails atomically with 409 / 23505 naming
-- position_ticks_client_event_id_uidx; the app then re-sends that chunk row by
-- row and treats a per-row 23505 on this index as "already persisted" (the id is
-- a hash of the full payload). Upsert (`on_conflict` + resolution=ignore-
-- duplicates) is NOT used: with the current RLS (INSERT-only policy, no SELECT
-- policy) PostgREST's upsert fails with 42501, verified locally.
--
-- Deviation from the requested *partial* index (Codex R3 §3), for review: this
-- is a plain UNIQUE index, NULLS DISTINCT (the PostgreSQL default, stated
-- explicitly). It constrains non-null identities only and places no constraint
-- on NULL legacy rows — the same semantics as
-- "... (client_event_id) WHERE client_event_id IS NOT NULL". The difference: a
-- partial index cannot be inferred by ON CONFLICT (client_event_id) without
-- repeating the predicate, which PostgREST never emits (42P10, verified
-- locally), so a future upsert/RPC path would be blocked. Switching to the
-- partial form is a one-line change and works identically with the plain-INSERT
-- protocol above (also verified locally).
--
-- Existing duplicates: production has 16 exact-duplicate (trade_id, tick_ts)
-- groups (32 rows). With no backfill they stay NULL and cannot clash. If a
-- backfill is ever approved, it must assign the id to at most one row per
-- exact group (leave the others NULL) or the index build/insert will fail.
--
-- Lock/size note: CREATE UNIQUE INDEX on ~35k rows of NULLs is small and quick.
-- CONCURRENTLY is not used because migrations run inside a transaction.
-- ============================================================================

alter table public.position_ticks
  add column if not exists client_event_id text;

alter table public.position_ticks
  drop constraint if exists position_ticks_client_event_id_format;
alter table public.position_ticks
  add constraint position_ticks_client_event_id_format
  check (client_event_id is null or client_event_id ~ '^[0-9a-f]{64}$');

create unique index if not exists position_ticks_client_event_id_uidx
  on public.position_ticks (client_event_id) nulls distinct;

comment on column public.position_ticks.client_event_id is
  'position_tick_client_event_id_v1: sha256 hex of canonical immutable tick payload (excludes id, created_at, client_event_id). NULL for rows written before B1.1.';

notify pgrst, 'reload schema';
