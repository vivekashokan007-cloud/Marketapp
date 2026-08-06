-- Market Radar: allow app-side generated-candidate upserts.
-- The Android app writes with PostgREST on_conflict, so RLS must permit
-- INSERT plus UPDATE/SELECT for rows that already exist.

alter table if exists public.ml_generated_candidates enable row level security;

drop policy if exists ml_generated_candidates_select_anon on public.ml_generated_candidates;
create policy ml_generated_candidates_select_anon
on public.ml_generated_candidates
for select
to anon, authenticated
using (true);

drop policy if exists ml_generated_candidates_insert_anon on public.ml_generated_candidates;
create policy ml_generated_candidates_insert_anon
on public.ml_generated_candidates
for insert
to anon, authenticated
with check (true);

drop policy if exists ml_generated_candidates_update_anon on public.ml_generated_candidates;
create policy ml_generated_candidates_update_anon
on public.ml_generated_candidates
for update
to anon, authenticated
using (true)
with check (true);
