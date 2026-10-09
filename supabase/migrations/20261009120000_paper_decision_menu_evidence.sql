-- Market Radar evidence batch 1 (Item B): bounded Paper decision-menu evidence.
-- PREPARED FOR REVIEW ONLY. Tested on a disposable local Postgres 17. NOT applied to production.
-- Additive: two new tables, one trigger function, one finalize function. No existing table, policy,
-- grant, retention job or containment setting is changed.
--
-- Why new tables (HANDOFF_R2 decision 5):
--   * ml_generated_candidates has no legs/strikes/CE-PE/BUY-SELL, no lot authority, no quote refs or
--     bid/ask, no gate reasons, no menu id, no capture policy, and generated_count is the persisted cohort
--     (<=50 per poll by GENERATED_CANDIDATE_PERSIST_CAP), not the population. Its key is
--     (snapshot_poll_ts, candidate_id), so a once-per-session menu identity and completeness cannot be
--     expressed without adding ~15 columns that would be NULL on ~3,000 rows/day of operational writes.
--   * ml_brain_snapshots.context_json carries top-200 ranked views per poll, is budget-compacted
--     (forward_capture_budget_exceeded_batch_a on every snapshot since 2026-09-28) and has no row-level
--     uniqueness, so durable completeness of a ~700-row menu cannot be reconciled there.
--   Reusing either would duplicate the operational writer's semantics or the per-poll bulk.
--
-- Semantics:
--   * One manifest per (session_date, capture_policy_version); menu_id = 'pdm_' || sha256(date|policy)[:24].
--   * Manifest and candidate rows are insert-only for app roles (no UPDATE/DELETE policy or grant).
--     Clients insert with ON CONFLICT DO NOTHING, so a transport retry of the frozen menu is idempotent.
--   * completeness_state is declared ONLY by paper_decision_menu_finalize(), which reconciles expected
--     vs durable rows and the candidate-id digest. A successful upload alone never marks completeness.
--   * MISSED_SESSION manifests carry attempts and expect zero rows; an EMPTY_MENU capture is distinct.

begin;

create table if not exists public.ml_paper_decision_menus (
    menu_id                text primary key,
    session_date           date not null,
    capture_policy_version text not null,
    menu_schema_version    text,
    capture_status         text not null,
    population_status      text,
    generated_count        integer,
    captured_count         integer,
    duplicate_count        integer,
    expected_candidate_rows integer not null,
    candidate_ids_sha256   text,
    primary_candidate_id   text,
    control_status         text,
    control_peer_candidate_id text,
    control_inclusion_probability double precision,
    slot_key               text,
    slot_scheduled_ist     timestamptz,
    dispatch_at            timestamptz,
    brain_completed_at     timestamptz,
    chain_poll_ts          timestamptz,
    execution_mode         text,
    app_version            text,
    brain_version          text,
    attempts               jsonb not null default '[]'::jsonb,
    manifest               jsonb not null,
    payload_sha256         text not null,
    completeness_state     text not null default 'PENDING_ROWS',
    durable_candidate_rows integer,
    durable_ids_sha256     text,
    reconciled_at          timestamptz,
    finalize_calls         integer not null default 0,
    created_at             timestamptz not null default now(),
    constraint pdm_menu_id_format check (menu_id ~ '^pdm_[0-9a-f]{24}$'),
    constraint pdm_session_policy_unique unique (session_date, capture_policy_version),
    constraint pdm_capture_status check (capture_status in ('CAPTURED', 'MISSED_SESSION')),
    constraint pdm_population_status check (population_status is null or population_status in ('MENU_BUILT', 'EMPTY_MENU')),
    constraint pdm_status_pairing check (
        (capture_status = 'CAPTURED' and population_status is not null)
        or (capture_status = 'MISSED_SESSION' and population_status is null and expected_candidate_rows = 0)),
    constraint pdm_counts_nonneg check (coalesce(generated_count, 0) >= 0 and coalesce(captured_count, 0) >= 0
        and coalesce(duplicate_count, 0) >= 0 and expected_candidate_rows >= 0),
    constraint pdm_counts_reconcile check (capture_status <> 'CAPTURED'
        or (generated_count = captured_count + duplicate_count and expected_candidate_rows = captured_count)),
    constraint pdm_empty_menu check (population_status is distinct from 'EMPTY_MENU' or generated_count = 0),
    constraint pdm_sha_format check (payload_sha256 ~ '^[0-9a-f]{64}$'),
    constraint pdm_completeness_state check (completeness_state in
        ('PENDING_ROWS', 'PARTIAL', 'COMPLETE', 'NO_ROWS_EXPECTED', 'MISMATCH')),
    constraint pdm_manifest_object check (jsonb_typeof(manifest) = 'object'),
    constraint pdm_attempts_array check (jsonb_typeof(attempts) = 'array'),
    constraint pdm_manifest_size check (octet_length(manifest::text) <= 262144)
);

create table if not exists public.ml_paper_decision_menu_candidates (
    menu_id          text not null references public.ml_paper_decision_menus (menu_id) on delete restrict,
    candidate_id     text not null,
    generated_rank   integer not null,
    watchlist_rank   integer,
    surfaced         boolean not null,
    selected_primary boolean not null,
    control_role     text,
    index_key        text,
    expiry           date,
    strategy_family  text,
    lane             text,
    lot_size         integer,
    control_eligible boolean,
    legs             jsonb not null,
    margin_evidence  jsonb,
    row_json         jsonb not null,
    created_at       timestamptz not null default now(),
    primary key (menu_id, candidate_id),
    constraint pdmc_rank_unique unique (menu_id, generated_rank),
    constraint pdmc_rank_positive check (generated_rank > 0 and (watchlist_rank is null or watchlist_rank > 0)),
    constraint pdmc_role check (control_role is null or control_role in ('PRIMARY', 'REGISTERED_UNIFORM_PEER')),
    constraint pdmc_primary_role check (selected_primary = (control_role is not distinct from 'PRIMARY')),
    constraint pdmc_legs_array check (jsonb_typeof(legs) = 'array'),
    constraint pdmc_row_object check (jsonb_typeof(row_json) = 'object'),
    constraint pdmc_margin_only_roles check (margin_evidence is null or control_role is not null),
    constraint pdmc_row_size check (octet_length(row_json::text) + octet_length(legs::text) <= 32768)
);

create index if not exists pdm_session_date_idx on public.ml_paper_decision_menus (session_date);
-- One primary and at most one registered peer per menu.
create unique index if not exists pdmc_one_role_per_menu
    on public.ml_paper_decision_menu_candidates (menu_id, control_role) where control_role is not null;

-- Frozen-content guard: candidate rows are immutable; manifests may change only in the reconciliation
-- columns, and only inside paper_decision_menu_finalize() (transaction-local flag). DELETE is refused.
create or replace function public.pdm_guard_frozen() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'paper decision menu evidence is append-only (% on %)', tg_op, tg_table_name;
    end if;
    if tg_table_name = 'ml_paper_decision_menu_candidates' then
        raise exception 'paper decision menu candidate rows are immutable';
    end if;
    if coalesce(current_setting('pdm.finalizing', true), '') <> 'on' then
        raise exception 'paper decision menu manifests are insert-only; use paper_decision_menu_finalize()';
    end if;
    if (to_jsonb(new) - array['completeness_state', 'durable_candidate_rows', 'durable_ids_sha256',
                              'reconciled_at', 'finalize_calls'])
       is distinct from
       (to_jsonb(old) - array['completeness_state', 'durable_candidate_rows', 'durable_ids_sha256',
                              'reconciled_at', 'finalize_calls']) then
        raise exception 'frozen paper decision menu content cannot change (menu %)', old.menu_id;
    end if;
    return new;
end $$;

drop trigger if exists pdm_guard_frozen_menus on public.ml_paper_decision_menus;
create trigger pdm_guard_frozen_menus before update or delete on public.ml_paper_decision_menus
    for each row execute function public.pdm_guard_frozen();
drop trigger if exists pdm_guard_frozen_candidates on public.ml_paper_decision_menu_candidates;
create trigger pdm_guard_frozen_candidates before update or delete on public.ml_paper_decision_menu_candidates
    for each row execute function public.pdm_guard_frozen();

-- Candidate rows may only be added for a CAPTURED manifest that is not yet reconciled.
create or replace function public.pdm_guard_candidate_insert() returns trigger
language plpgsql security definer set search_path = public, pg_temp as $$
declare m public.ml_paper_decision_menus%rowtype;
begin
    select * into m from public.ml_paper_decision_menus where menu_id = new.menu_id;
    if not found then
        raise exception 'manifest % must be inserted before its candidate rows', new.menu_id;
    end if;
    if m.capture_status <> 'CAPTURED' then
        raise exception 'menu % is %; it cannot carry candidate rows', new.menu_id, m.capture_status;
    end if;
    if m.completeness_state in ('COMPLETE', 'NO_ROWS_EXPECTED', 'MISMATCH') then
        -- A transport retry of an already-durable row after reconciliation is a silent no-op;
        -- any new row after reconciliation is refused.
        if exists (select 1 from public.ml_paper_decision_menu_candidates c
                    where c.menu_id = new.menu_id and c.candidate_id = new.candidate_id) then
            return null;
        end if;
        raise exception 'menu % is already reconciled (%)', new.menu_id, m.completeness_state;
    end if;
    return new;
end $$;

drop trigger if exists pdm_guard_candidate_insert on public.ml_paper_decision_menu_candidates;
create trigger pdm_guard_candidate_insert before insert on public.ml_paper_decision_menu_candidates
    for each row execute function public.pdm_guard_candidate_insert();

-- Reconciliation. Idempotent: may be called after every upload attempt. Declares
--   NO_ROWS_EXPECTED  expected 0 and durable 0 (MISSED_SESSION or EMPTY_MENU)
--   COMPLETE          durable == expected and the durable id digest (ordered by generated_rank) matches
--   PARTIAL           durable < expected (retry may still complete it)
--   MISMATCH          durable > expected, digest differs, or the client payload hash differs (terminal)
create or replace function public.paper_decision_menu_finalize(p_menu_id text, p_payload_sha256 text)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    m public.ml_paper_decision_menus%rowtype;
    n integer;
    digest text;
    st text;
begin
    select * into m from public.ml_paper_decision_menus where menu_id = p_menu_id for update;
    if not found then
        return jsonb_build_object('menu_id', p_menu_id, 'completeness_state', 'MANIFEST_MISSING');
    end if;
    if m.completeness_state in ('COMPLETE', 'NO_ROWS_EXPECTED', 'MISMATCH') then
        return jsonb_build_object('menu_id', m.menu_id, 'completeness_state', m.completeness_state,
                                  'durable_candidate_rows', m.durable_candidate_rows,
                                  'expected_candidate_rows', m.expected_candidate_rows, 'already_reconciled', true);
    end if;
    select count(*), encode(sha256(convert_to(coalesce(string_agg(candidate_id, E'\n' order by generated_rank), ''), 'UTF8')), 'hex')
      into n, digest
      from public.ml_paper_decision_menu_candidates where menu_id = p_menu_id;
    st := case
        when p_payload_sha256 is distinct from m.payload_sha256 then 'MISMATCH'
        when n > m.expected_candidate_rows then 'MISMATCH'
        when n < m.expected_candidate_rows then 'PARTIAL'
        when m.expected_candidate_rows = 0 then 'NO_ROWS_EXPECTED'
        when m.candidate_ids_sha256 is distinct from digest then 'MISMATCH'
        else 'COMPLETE' end;
    perform set_config('pdm.finalizing', 'on', true);
    update public.ml_paper_decision_menus
       set completeness_state = st, durable_candidate_rows = n, durable_ids_sha256 = digest,
           reconciled_at = now(), finalize_calls = finalize_calls + 1
     where menu_id = p_menu_id;
    perform set_config('pdm.finalizing', 'off', true);
    return jsonb_build_object('menu_id', m.menu_id, 'completeness_state', st, 'durable_candidate_rows', n,
                              'expected_candidate_rows', m.expected_candidate_rows, 'already_reconciled', false);
end $$;

-- RLS: app roles may read and insert; no update/delete/truncate. Reconciliation only via the function.
alter table public.ml_paper_decision_menus enable row level security;
alter table public.ml_paper_decision_menu_candidates enable row level security;

drop policy if exists pdm_select on public.ml_paper_decision_menus;
create policy pdm_select on public.ml_paper_decision_menus for select to anon, authenticated using (true);
drop policy if exists pdm_insert on public.ml_paper_decision_menus;
create policy pdm_insert on public.ml_paper_decision_menus for insert to anon, authenticated
    with check (completeness_state = 'PENDING_ROWS' and durable_candidate_rows is null
                and reconciled_at is null and finalize_calls = 0);
drop policy if exists pdmc_select on public.ml_paper_decision_menu_candidates;
create policy pdmc_select on public.ml_paper_decision_menu_candidates for select to anon, authenticated using (true);
drop policy if exists pdmc_insert on public.ml_paper_decision_menu_candidates;
create policy pdmc_insert on public.ml_paper_decision_menu_candidates for insert to anon, authenticated with check (true);

revoke all on public.ml_paper_decision_menus from anon, authenticated;
revoke all on public.ml_paper_decision_menu_candidates from anon, authenticated;
grant select, insert on public.ml_paper_decision_menus to anon, authenticated;
grant select, insert on public.ml_paper_decision_menu_candidates to anon, authenticated;

revoke all on function public.paper_decision_menu_finalize(text, text) from public;
grant execute on function public.paper_decision_menu_finalize(text, text) to anon, authenticated;
revoke all on function public.pdm_guard_frozen() from public;
revoke all on function public.pdm_guard_candidate_insert() from public;

comment on table public.ml_paper_decision_menus is
    'Evidence batch 1: one frozen Paper decision-menu manifest per session (12:30-12:55 IST first completed slot). Research evidence only; not read by ranking/gates/sizing/exits.';
comment on table public.ml_paper_decision_menu_candidates is
    'Evidence batch 1: every generated candidate of the frozen Paper decision menu (legs, lot authority, quote refs, gate reasons). Margin evidence only on PRIMARY and REGISTERED_UNIFORM_PEER rows.';

commit;
