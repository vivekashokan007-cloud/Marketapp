-- Reproducible read-only queries for the 23 Nov 2026 PCR/VIX/IV/Greeks assignment.
-- Supabase project fdynxkfxohbnlvayouje. Every statement is SELECT-only.
-- Pinned build: native 8f9aa234a278528a09d58cd83a6aaa85254e2218, PWA cbb65bda, 2.6.104 / b535.

-- Q1. The five evidence trades as saved.
select id, index_key, strategy_type, status, expiry, width, lots,
       sell_strike, sell_type, sell_ltp, buy_strike, buy_type, buy_ltp,
       entry_premium, exit_premium, entry_date, exit_date,
       entry_spot, exit_spot, entry_vix, entry_atm_iv, entry_pcr,
       max_profit, max_loss, peak_pnl, actual_pnl, net_pnl, friction_cost, exit_reason
from trades_v2 where id in (318,319,320,321) order by id;

-- Q2. FINDING 1. buy_ltp/sell_ltp are ENTRY legs; the executable close comes from
-- exit_snapshot.close_leg_quotes; and the stored close_turnover proves which pair the fee
-- engine used. If turnover_from_executable = close_turnover, the fee engine priced the close at
-- (long bid, short ask) while the P&L used the saved exit_premium.
select id, index_key,
  (case index_key when 'NF' then 65 else 30 end) as lot,
  (buy_ltp::numeric - sell_ltp::numeric)        as entry_legs_rebuild,
  entry_premium::numeric                        as entry_premium,
  (exit_snapshot->'close_leg_quotes'->'buy'->>'bid')::numeric  as long_bid,
  (exit_snapshot->'close_leg_quotes'->'sell'->>'ask')::numeric as short_ask,
  ((exit_snapshot->'close_leg_quotes'->'buy'->>'bid')::numeric
   - (exit_snapshot->'close_leg_quotes'->'sell'->>'ask')::numeric) as executable_close,
  exit_premium::numeric                         as saved_exit_premium,
  (exit_snapshot->'friction_breakdown'->'breakdown'->>'close_turnover')::numeric as close_turnover,
  ((exit_snapshot->'close_leg_quotes'->'buy'->>'bid')::numeric
   + (exit_snapshot->'close_leg_quotes'->'sell'->>'ask')::numeric)
   * (case index_key when 'NF' then 65 else 30 end) as turnover_from_executable,
  ((((exit_snapshot->'close_leg_quotes'->'buy'->>'bid')::numeric
      - (exit_snapshot->'close_leg_quotes'->'sell'->>'ask')::numeric) - entry_premium::numeric)
    * (case index_key when 'NF' then 65 else 30 end)) as executable_gross,
  actual_pnl::numeric as booked_gross, peak_pnl::numeric as peak_gross
from trades_v2 where id in (318,319) order by id;

-- Q3. FINDING 1 (second limb). No retained poll series stands behind either extremum.
select id, jsonb_typeof(close_polls_json) as close_polls_type,
       journey_stats->>'poll_count' as poll_count,
       journey_stats->>'timeline'   as timeline,
       journey_stats->>'peak_pnl'   as peak_pnl,
       journey_stats->>'peak_pnl_validity' as peak_validity,
       close_trace_json->>'source'  as close_source,
       close_trace_json->>'exit_premium' as trace_exit_premium
from trades_v2 where id in (318,319) order by id;

-- Q4. FINDING 3. The 09:25-09:40 warning table, reproduced from the finalisation frame, with the
-- total-OI ratio computed from the OI fields stored in the SAME frame. definition_residual is
-- non-zero and sign-changing, so bnf_pcr is not total_put_oi/total_call_oi.
with v as (
 select to_char(created_at at time zone 'Asia/Kolkata','HH24:MI:SS') as ist,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_pcr')::numeric          as bnf_pcr,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_near_atm_pcr')::numeric as bnf_near_atm_pcr,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_total_put_oi')::numeric as put_oi,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_total_call_oi')::numeric as call_oi,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_atm_iv')::numeric       as bnf_atm_iv,
   (context_json->'c3_finalization_frame'->'values'->>'nf_pcr')::numeric           as nf_pcr,
   (context_json->'c3_finalization_frame'->'values'->>'nf_atm_iv')::numeric        as nf_atm_iv,
   (context_json->'c3_finalization_frame'->'values'->>'vix')::numeric              as vix
 from ml_brain_snapshots
 where created_at >= '2026-11-23T03:45:00Z' and created_at < '2026-11-23T04:45:00Z')
select ist, bnf_pcr, round(put_oi/nullif(call_oi,0),6) as total_oi_pcr, bnf_near_atm_pcr,
       put_oi, call_oi, bnf_atm_iv, nf_pcr, nf_atm_iv, vix,
       round(put_oi/nullif(call_oi,0) - bnf_pcr, 9) as definition_residual
from v order by ist;

-- Q5. Availability check: the snapshot clock against the two entry times, so a value is only a
-- usable warning if its snapshot precedes the entry.
select to_char(created_at at time zone 'Asia/Kolkata','HH24:MI:SS') as snapshot_ist,
       created_at < timestamptz '2026-11-23 04:07:06.819+00' as before_nf_entry_320,
       created_at < timestamptz '2026-11-23 04:07:16.628+00' as before_bnf_entry_321
from ml_brain_snapshots
where created_at >= '2026-11-23T03:50:00Z' and created_at < '2026-11-23T04:15:00Z'
order by created_at;

-- Q6. Expiry coverage for the chain the spreads were priced from (used to check whether a
-- 23 Nov BNF expiry exists at all, given NSE discontinued Bank Nifty weeklies).
select session_date::text, index_key, expiry, count(*) as rows,
       min(strike) as min_strike, max(strike) as max_strike
from ml_option_chain_snapshots
where session_date in ('2026-11-20','2026-11-23')
group by 1,2,3 order by 1,2,3;

-- =====================================================================================
-- QUOTE-QUALITY VERIFICATION (added 23 Nov after Codex's two objections:
--   (a) use executable SIDES, not bid-vs-bid, to claim arbitrage;
--   (b) 31.5 s of pairing lag cannot by itself rule out timing.)
-- These four queries are the diagnostic, in the order they must be read.
-- =====================================================================================

-- Q7. TIMING CONTROL. How far do these contracts move on their own over ~60 s, measured
-- within ONE source (the trusted tick legs)? Any chain-vs-tick difference smaller than this
-- is not evidence of a bad quote.
with t as (
  select tick_ts, (l->>'strike')::numeric as strike, (l->>'option_type') as ot,
         (l->>'bid')::numeric as bid
  from position_ticks, lateral jsonb_array_elements(legs_json) l
  where session_date='2026-11-23' and trade_id in ('320','321') and (l->>'quote_status')='OK'
), d as (
  select strike, ot, tick_ts, bid, lag(bid) over w as prev_bid,
         extract(epoch from (tick_ts - lag(tick_ts) over w)) as gap_s
  from t window w as (partition by strike, ot order by tick_ts)
)
select strike::int as strike, count(*) as n,
       (percentile_cont(0.5) within group (order by abs(100.0*(bid-prev_bid)/nullif(prev_bid,0))))::numeric(10,2) as median_move_pct,
       (percentile_cont(0.9) within group (order by abs(100.0*(bid-prev_bid)/nullif(prev_bid,0))))::numeric(10,2) as p90_move_pct
from d where prev_bid is not null and gap_s between 50 and 75
group by strike order by strike;

-- Q8. EXECUTABLE ARBITRAGE, correct sides. For calls K1 < K2 a locked profit needs
-- bid(K1) - ask(K2) > (K2 - K1). Bid-vs-bid monotonicity is an INDICATIVE inconsistency and
-- is not this test.
with c as (
  select session_date, index_key, poll_ts, strike, bid, ask,
         lead(strike) over w as k2, lead(ask) over w as ask2
  from ml_option_chain_snapshots
  where session_date between '2026-11-18' and '2026-11-23'
    and option_type='CE' and bid is not null and ask is not null
  window w as (partition by session_date, index_key, poll_ts order by strike)
)
select to_char(poll_ts at time zone 'Asia/Kolkata','HH24:MI') as hm,
       count(*) as adjacent_pairs,
       count(*) filter (where bid - ask2 > (k2 - strike) + 0.05) as executable_arb
from c where k2 is not null
group by 1 order by 1;

-- Q9. LEVEL CHECK against a VERIFIED spot (from the export's authority history, not from any
-- trade's own exit_snapshot), restricted to the TRADEABLE near-money band. Deep wings are
-- illiquid by nature and must not be included - that inclusion is what made an earlier run of
-- this look like an all-day problem.
with s(index_key, hm, spot) as (values
 ('BNF','09:15',64934.4),('BNF','09:35',63290.35),('BNF','09:50',63502.3),('BNF','10:10',63440.3),
 ('BNF','11:00',63356.65),('BNF','12:00',63211.65),('BNF','12:40',63272.05),
 ('NF','09:15',25915.4),('NF','09:35',25576.8),('NF','09:50',25664.6),('NF','10:10',25623.1),
 ('NF','11:00',25555.2),('NF','12:00',25509.6),('NF','12:40',25509.15)
), c as (
  select ch.index_key, to_char(ch.poll_ts at time zone 'Asia/Kolkata','HH24:MI') as hm,
         ch.strike, ch.bid, (s.spot - ch.strike) as intrinsic
  from ml_option_chain_snapshots ch
  join s on s.index_key=ch.index_key
        and s.hm = to_char(ch.poll_ts at time zone 'Asia/Kolkata','HH24:MI')
  where ch.session_date='2026-11-23' and ch.option_type='CE' and ch.bid is not null
    and ch.strike < s.spot - 100
    and ch.strike > s.spot - (case ch.index_key when 'BNF' then 1200 else 450 end)
)
select index_key, hm, count(*) as near_itm_calls,
       count(*) filter (where bid < intrinsic - 1) as below_intrinsic,
       max(intrinsic - bid)::numeric(10,1) as worst_shortfall,
       (percentile_cont(0.5) within group (order by (intrinsic - bid)))::numeric(10,1) as median_shortfall
from c group by index_key, hm order by index_key, hm;

-- Q10. MATCHED ROWS for the chain-vs-tick comparison, with identity, side and both clocks
-- exposed so duplicate or mis-keyed joins are visible. One tick per chain poll, nearest in
-- time, within 45 s.
with ticks as (
  select tick_ts, trade_id, (l->>'instrument_key') as ik,
         (l->>'strike')::numeric as strike, (l->>'option_type') as ot,
         (l->>'bid')::numeric as t_bid, (l->>'ask')::numeric as t_ask,
         (l->>'source_ts') as t_source_ts, (l->>'quote_status') as t_status
  from position_ticks, lateral jsonb_array_elements(legs_json) l
  where session_date='2026-11-23' and trade_id in ('320','321')
)
select c.index_key, c.expiry, c.strike, c.option_type,
       to_char(c.poll_ts at time zone 'Asia/Kolkata','HH24:MI:SS') as chain_ist,
       c.bid as chain_bid, c.ask as chain_ask,
       t.ik, t.t_bid, t.t_ask, t.t_source_ts, t.t_status, t.trade_id,
       extract(epoch from (t.tick_ts - c.poll_ts))::numeric(10,1) as pair_lag_s
from ml_option_chain_snapshots c
join lateral (
  select * from ticks tk
  where tk.strike=c.strike and tk.ot=c.option_type
    and abs(extract(epoch from (tk.tick_ts - c.poll_ts))) < 45
  order by abs(extract(epoch from (tk.tick_ts - c.poll_ts))) limit 1
) t on true
where c.session_date='2026-11-23' and c.expiry='2026-11-23' and c.option_type='PE'
  and ((c.index_key='NF' and c.strike in (25600,25450)) or (c.index_key='BNF' and c.strike in (63200,62800)))
order by c.index_key, c.strike, c.poll_ts;
