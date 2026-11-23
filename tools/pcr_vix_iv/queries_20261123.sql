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
