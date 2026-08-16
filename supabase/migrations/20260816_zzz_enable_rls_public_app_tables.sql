-- Enable RLS on the remaining Data API tables used by the single-user app.
-- The phone currently uses the anon role, so policies are operation-scoped;
-- delete access and writes to legacy/export-only tables remain denied.

alter table public.bhav_options enable row level security;
alter table public.radar_inputs enable row level security;
alter table public.trades enable row level security;
alter table public.straddle_ratios enable row level security;
alter table public.daily_data enable row level security;
alter table public.ml_brain_snapshots enable row level security;
alter table public.chain_slices enable row level security;
alter table public.ml_rejected_candidate_outcomes enable row level security;
alter table public.ml_context_percentile_history enable row level security;

revoke all on table
    public.bhav_options,
    public.radar_inputs,
    public.trades,
    public.straddle_ratios,
    public.daily_data,
    public.ml_brain_snapshots,
    public.chain_slices,
    public.ml_rejected_candidate_outcomes,
    public.ml_context_percentile_history
from anon, authenticated;

grant select on table
    public.bhav_options,
    public.radar_inputs,
    public.trades,
    public.straddle_ratios,
    public.daily_data,
    public.ml_brain_snapshots,
    public.chain_slices,
    public.ml_rejected_candidate_outcomes,
    public.ml_context_percentile_history
to anon, authenticated;

grant insert on table
    public.ml_brain_snapshots,
    public.chain_slices,
    public.ml_rejected_candidate_outcomes,
    public.ml_context_percentile_history
to anon, authenticated;

grant update on table
    public.ml_rejected_candidate_outcomes,
    public.ml_context_percentile_history
to anon, authenticated;

create policy bhav_options_app_read
    on public.bhav_options for select to anon, authenticated using (true);
create policy radar_inputs_app_read
    on public.radar_inputs for select to anon, authenticated using (true);
create policy trades_legacy_app_read
    on public.trades for select to anon, authenticated using (true);
create policy straddle_ratios_app_read
    on public.straddle_ratios for select to anon, authenticated using (true);
create policy daily_data_app_read
    on public.daily_data for select to anon, authenticated using (true);

create policy ml_brain_snapshots_app_read
    on public.ml_brain_snapshots for select to anon, authenticated using (true);
create policy ml_brain_snapshots_app_insert
    on public.ml_brain_snapshots for insert to anon, authenticated with check (true);

create policy chain_slices_app_read
    on public.chain_slices for select to anon, authenticated using (true);
create policy chain_slices_app_insert
    on public.chain_slices for insert to anon, authenticated with check (true);

create policy ml_rejected_candidate_outcomes_app_read
    on public.ml_rejected_candidate_outcomes for select to anon, authenticated using (true);
create policy ml_rejected_candidate_outcomes_app_insert
    on public.ml_rejected_candidate_outcomes for insert to anon, authenticated with check (true);
create policy ml_rejected_candidate_outcomes_app_update
    on public.ml_rejected_candidate_outcomes for update to anon, authenticated
    using (true) with check (true);

create policy ml_context_percentile_history_app_read
    on public.ml_context_percentile_history for select to anon, authenticated using (true);
create policy ml_context_percentile_history_app_insert
    on public.ml_context_percentile_history for insert to anon, authenticated with check (true);
create policy ml_context_percentile_history_app_update
    on public.ml_context_percentile_history for update to anon, authenticated
    using (true) with check (true);
