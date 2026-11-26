-- Source data must be readable by its signed-in owner, not only upload receipts.
-- Existing owner SELECT policy still hides other owners and ownerless research samples.
grant select on public.mr_market_source_captures to authenticated;
