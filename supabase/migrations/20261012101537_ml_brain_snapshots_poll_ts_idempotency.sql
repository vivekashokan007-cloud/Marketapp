-- Required before the snapshot outbox client is released. Production inspection on
-- 2026-10-12 found all existing poll_ts values distinct and non-null.
-- A unique poll key makes a retry after an ambiguous HTTP timeout safe.
set lock_timeout = '5s';
set statement_timeout = '60s';
create unique index if not exists ml_brain_snapshots_poll_ts_key
    on public.ml_brain_snapshots (poll_ts);
notify pgrst, 'reload schema';
