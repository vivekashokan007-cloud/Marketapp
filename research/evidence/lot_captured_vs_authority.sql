-- Captured contract lot vs project authority, input query (read-only; after market hours).
-- Returns one row per (index, IST entry date, expiry, captured values) for trades from :FROM; the Python
-- driver lot_crosscheck.py resolves each row with contract_lot_table.resolve_contract_lot and reports
-- agreement, disagreement and gaps. Captured values are reported as stored (text), never coerced here.
begin read only;
set local statement_timeout = '60s';
select json_agg(row_to_json(x) order by x.d, x.index_key, x.expiry) rows
from (
  select index_key, (entry_date at time zone 'Asia/Kolkata')::date d, expiry,
         entry_snapshot->>'contract_lot_size' captured_contract_lot,
         entry_snapshot->>'lot_size' captured_lot_size,
         entry_snapshot->>'number_of_lots' captured_number_of_lots,
         friction_breakdown_json->>'lot_size' friction_lot_size,
         lots, count(*) n
  from trades_v2
  where entry_date >= (:FROM::date + time '00:00') at time zone 'Asia/Kolkata'
  group by 1, 2, 3, 4, 5, 6, 7, 8) x;
commit;
