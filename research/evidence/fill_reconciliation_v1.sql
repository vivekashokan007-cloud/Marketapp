-- Offline paper-fill consistency v1 (read-only; after market hours). Research evidence only:
-- it compares recorded paper ENTRY leg prices with the contemporaneous stored bid/ask. It does not
-- and cannot establish broker fillability (no order book depth, no latency, no fills).
-- Matching: latest ml_option_chain_snapshots poll for the trade's index at or before entry_date and
-- no more than 10 minutes earlier; leg identity = index + strike + option type, and the stored chain
-- expiry must equal trades_v2.expiry. Tolerance for 'at' classes: 0.025 (half the 0.05 tick).
-- Sell legs: executable = bid. Buy legs: executable = ask. Index and option-type spellings are normalised
-- (NIFTY/BANKNIFTY, CALL/PUT); anything else is counted as unmatched, never guessed.
begin read only;
set local statement_timeout = '60s';
with t as (
  select id,
         case when upper(trim(index_key)) in ('NF', 'NIFTY', 'NIFTY 50', 'NIFTY50') then 'NF'
              when upper(trim(index_key)) in ('BNF', 'BANKNIFTY', 'NIFTY BANK', 'BANK NIFTY') then 'BNF'
              else 'UNKNOWN:' || coalesce(index_key, 'null') end index_key,
         expiry, entry_date, paper, execution_mode, strategy_type,
         sell_strike, sell_type, sell_ltp, buy_strike, buy_type, buy_ltp,
         sell_strike2, sell_type2, sell_ltp2, buy_strike2, buy_type2, buy_ltp2
  from trades_v2
  where entry_date >= timestamptz '2026-07-02 00:00+05:30' and entry_date < timestamptz '2026-10-06 00:00+05:30'),
legs as (
  select id, index_key, expiry, entry_date, 'sell' side, sell_strike::int k, case upper(trim(sell_type)) when 'CALL' then 'CE' when 'PUT' then 'PE' else upper(trim(sell_type)) end o, sell_ltp::float8 px from t where sell_strike is not null
  union all select id, index_key, expiry, entry_date, 'buy', buy_strike::int, case upper(trim(buy_type)) when 'CALL' then 'CE' when 'PUT' then 'PE' else upper(trim(buy_type)) end, buy_ltp::float8 from t where buy_strike is not null
  union all select id, index_key, expiry, entry_date, 'sell', sell_strike2::int, case upper(trim(sell_type2)) when 'CALL' then 'CE' when 'PUT' then 'PE' else upper(trim(sell_type2)) end, sell_ltp2::float8 from t where sell_strike2 is not null
  union all select id, index_key, expiry, entry_date, 'buy', buy_strike2::int, case upper(trim(buy_type2)) when 'CALL' then 'CE' when 'PUT' then 'PE' else upper(trim(buy_type2)) end, buy_ltp2::float8 from t where buy_strike2 is not null),
pp as (
  select t.id, (select max(c.poll_ts) from ml_option_chain_snapshots c
                where c.index_key = t.index_key and c.poll_ts <= t.entry_date
                  and c.poll_ts > t.entry_date - interval '10 minutes') poll_ts
  from t),
m as (
  select l.*, pp.poll_ts, c.expiry chain_expiry, c.bid, c.ask
  from legs l join pp on pp.id = l.id
  left join ml_option_chain_snapshots c
    on c.poll_ts = pp.poll_ts and c.index_key = l.index_key and c.strike = l.k and c.option_type = l.o),
cls as (
  -- CASE branches are evaluated in order, so the date cast below only ever sees a validated string.
  -- (An OR of the regex test and the cast is NOT guaranteed to short-circuit and can raise on blank expiry.)
  select *, case
    when index_key like 'UNKNOWN:%' then 'unmatched_unknown_index'
    when o not in ('CE', 'PE') then 'unmatched_unknown_option_type'
    when expiry is null then 'unmatched_trade_expiry_missing'
    when poll_ts is null then 'unmatched_no_poll_within_10min'
    when chain_expiry is null then 'unmatched_strike_absent'
    when chain_expiry !~ '^\d{4}-\d{2}-\d{2}$' then 'unmatched_chain_expiry_not_a_date'
    when chain_expiry::date <> expiry then 'unmatched_expiry_mismatch'
    when px is null then 'unmatched_no_recorded_price'
    when bid is null or ask is null or ask <= 0 or bid < 0 or ask < bid then 'unmatched_invalid_quote'
    when side = 'sell' and abs(px - bid) <= 0.025 then 'at_executable'
    when side = 'buy'  and abs(px - ask) <= 0.025 then 'at_executable'
    when side = 'sell' and px < bid then 'worse_than_executable'
    when side = 'buy'  and px > ask then 'worse_than_executable'
    when px > bid and px < ask then 'inside_spread_optimistic'
    when side = 'sell' and abs(px - ask) <= 0.025 then 'at_opposite_side_optimistic'
    when side = 'buy'  and abs(px - bid) <= 0.025 then 'at_opposite_side_optimistic'
    else 'beyond_opposite_side' end cls,
    case when bid is not null and ask is not null and px is not null and ask >= bid and bid >= 0 and ask > 0
         then case when side = 'sell' then px - bid else ask - px end end optimism_pts
  from m)
select (select count(*) from t) trades, (select count(*) from legs) legs,
       (select json_object_agg(k, n) from (select side || ':' || cls k, count(*) n from cls group by 1 order by 1) x) by_side_class,
       (select json_object_agg(k, n) from (select coalesce(execution_mode, 'null') || ':' || cls k, count(*) n from cls group by 1 order by 1) x) by_mode_class,
       (select round(avg(optimism_pts)::numeric, 3) from cls where cls not like 'unmatched%') mean_optimism_pts,
       (select round(percentile_cont(0.5) within group (order by optimism_pts)::numeric, 3) from cls where cls not like 'unmatched%') median_optimism_pts,
       (select round(extract(epoch from avg(l.entry_date - l.poll_ts))::numeric, 1) from cls l where poll_ts is not null) mean_poll_lag_s;
commit;
