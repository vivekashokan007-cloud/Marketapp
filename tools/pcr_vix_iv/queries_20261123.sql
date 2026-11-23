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

-- =====================================================================================
-- TEST 2b inputs (matched outright buy vs protected debit spread).  Both read-only.
-- =====================================================================================

-- Q11. The signal set: production's own published direction and constructed legs per poll.
-- candidate_ids carries what index_construction_v1.leg_plan built, so the spread's LONG leg
-- is K0 and is exactly what index_single_leg_v1.select_strike returns for a BUY.
with s as (
  select (created_at at time zone 'Asia/Kolkata')::date sd,
         context_json->'snapshot_index_authority_primary'->'by_index' as bi,
         (context_json->>'bnfSpot')::numeric bnf_spot, (context_json->>'nfSpot')::numeric nf_spot
  from ml_brain_snapshots where created_at >= '2026-11-10'
), e as (
  select sd, ix, v->>'direction' dir, v->>'action' act, (v->>'decision_poll_ts')::timestamptz dpt,
         case when ix='BNF' then bnf_spot else nf_spot end spot,
         (select string_agg(x,'|' order by x) from jsonb_array_elements_text(v->'candidate_ids') x) cids
  from s, lateral jsonb_each(bi) as t(ix, v)
)
select jsonb_agg(jsonb_build_array(sd::text, ix, dir, act,
         to_char(dpt at time zone 'Asia/Kolkata','HH24:MI'), spot, cids) order by sd, ix, dpt) j
from e where dir in ('BEAR','BULL');

-- Q11b. Direction census including WAIT, so the denominator is honest (955 WAIT of 1,386).
with s as (
  select (created_at at time zone 'Asia/Kolkata')::date sd,
         context_json->'snapshot_index_authority_primary'->'by_index' as bi
  from ml_brain_snapshots where created_at >= '2026-11-10')
select sd::text, ix, v->>'direction' dir, count(*) n
from s, lateral jsonb_each(bi) as t(ix, v) group by 1,2,3 order by 1,2,3;

-- Q12. Quote series for the exact strikes the signals name.  The strike list is generated from
-- Q11's candidate_ids, so no wider read is performed.  (Full WHERE clause in
-- tools/pcr_vix_iv/test2b_data/, reproduced by the engine's own extract step.)
-- select session_date||'|'||index_key||'|'||option_type||'|'||strike key,
--        jsonb_agg(jsonb_build_array(to_char(poll_ts at time zone 'Asia/Kolkata','HH24:MI'), bid, ask)
--                  order by poll_ts)
-- from ml_option_chain_snapshots where <per (session,index,option_type) strike sets> group by 1;

-- Q13. E2 sign test: the 09:15 chain against the 09:15/09:20 index level.  A whole call curve
-- doubling on a +0.22% index move, or a whole put curve falling by two thirds while the index
-- FALLS, is not a timing artefact.
select (created_at at time zone 'Asia/Kolkata')::date::text sd,
       to_char(created_at at time zone 'Asia/Kolkata','HH24:MI:SS') t,
       (context_json->>'bnfSpot')::numeric bnf, (context_json->>'nfSpot')::numeric nf
from ml_brain_snapshots
where created_at >= '2026-11-10'
  and to_char(created_at at time zone 'Asia/Kolkata','HH24:MI') in ('09:15','09:20','09:25')
order by 1,2;

-- =====================================================================================
-- TEST 5 — PCR / OI provenance.  Read-only.  Definitions are in
-- OptionChainBrainFormatter.kt:160-161 (pcr = totalPutOI/totalCallOI over ONE expiry;
-- nearAtmPCR over abs(strike-atm) <= max(spot*0.015, 1.0)).
-- =====================================================================================

-- Q14. Does the reported PCR reconcile with the OI totals stored in the SAME frame?
-- NF: 695/695 exact.  BNF: 0/695.  bnf_near_atm_pcr is null at every poll.
with v as (
 select (created_at at time zone 'Asia/Kolkata')::date sd,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_pcr')::numeric bp,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_total_put_oi')::numeric bpo,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_total_call_oi')::numeric bco,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_near_atm_pcr')::numeric bnap,
   (context_json->'c3_finalization_frame'->'values'->>'nf_pcr')::numeric np,
   (context_json->'c3_finalization_frame'->'values'->>'nf_total_put_oi')::numeric npo,
   (context_json->'c3_finalization_frame'->'values'->>'nf_total_call_oi')::numeric nco
 from ml_brain_snapshots where created_at >= '2026-11-10')
select sd::text, count(*) n,
 count(*) filter (where abs(np - npo/nullif(nco,0))<0.0005) nf_ok,
 count(*) filter (where abs(bp - bpo/nullif(bco,0))<0.0005) bnf_ok,
 count(bnap) bnf_nearatm_present,
 round(avg(abs(bp - bpo/nullif(bco,0))),4) bnf_mean_resid,
 round(min(bco)/1e6,2) bnf_call_oi_min_m, round(max(bco)/1e6,2) bnf_call_oi_max_m,
 round(min(nco)/1e6,2) nf_call_oi_min_m,  round(max(nco)/1e6,2) nf_call_oi_max_m
from v group by 1 order by 1;

-- Q15. Rule out staleness and an index mix-up as the explanation for the BNF residual.
-- 0 matches at either one-poll lag, and 0 against NF's ratio.
with v as (
 select created_at,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_pcr')::numeric pcr,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_total_put_oi')::numeric po,
   (context_json->'c3_finalization_frame'->'values'->>'bnf_total_call_oi')::numeric co
 from ml_brain_snapshots where created_at >= '2026-11-23' and created_at < '2026-11-24'
), d as (
 select pcr, round(po/nullif(co,0),6) r0,
   round(lag(po) over w/nullif(lag(co) over w,0),6) r_m1,
   round(lead(po) over w/nullif(lead(co) over w,0),6) r_p1
 from v window w as (order by created_at))
select count(*) n,
  count(*) filter (where abs(pcr-r0)  < 0.0005) match_same,
  count(*) filter (where abs(pcr-r_m1)< 0.0005) match_prev,
  count(*) filter (where abs(pcr-r_p1)< 0.0005) match_next
from d where pcr is not null and r0 is not null;

-- Q16. Rule out a moving strike universe: 226 BNF / 158 NF strikes, one expiry, EVERY poll.
with p as (
 select index_key, poll_ts, count(distinct strike) ks, count(distinct expiry) exps
 from ml_option_chain_snapshots where session_date='2026-11-23' group by 1,2)
select index_key, count(*) polls, min(ks) min_strikes, max(ks) max_strikes,
       count(distinct ks) distinct_strike_counts, max(exps) max_expiries_per_poll
from p group by 1;

-- Q17. OI freshness: the only table that ever carried OI totals, walls and max pain stopped on
-- 2026-06-29 (the same date the premium_history writer was removed).  ml_option_chain_snapshots
-- has no oi column at all, so no OI figure anywhere carries an exchange clock.
select max(date)::text newest, count(*) rows,
       count(*) filter (where date >= '2026-11-01') rows_since_november
from chain_snapshots;

-- =====================================================================================
-- TEST 6 part 1 — tick extrema from FIRST PRINCIPLES, plus the structural bound.
-- The mark is rebuilt from the raw per-leg bid/ask, never read from current_pnl or
-- running_mfe, so this validates the app's numbers instead of restating them.
-- =====================================================================================

-- Q18. Reconstruction fidelity: the rebuilt mark must equal executable_mark, and
-- current_pnl must equal (mark - entry_premium) * units.  Observed: 808 of 808 eligible
-- ticks exact, max gap 0.0000.
with t as (
 select pt.trade_id, pt.current_pnl::numeric pnl, pt.executable_mark::numeric mk,
   pt.quantity_units::numeric u, tv.entry_premium::numeric ep,
   (select sum(case when upper(l->>'side') in ('LONG','BUY') then (l->>'bid')::numeric
                    else -(l->>'ask')::numeric end)
    from jsonb_array_elements(pt.legs_json) l) leg_mark,
   (select count(*) from jsonb_array_elements(pt.legs_json) l
    where (l->>'bid') is null or (l->>'ask') is null) null_legs
 from position_ticks pt join trades_v2 tv on tv.id::text = pt.trade_id
 where pt.trade_id in ('318','319','320','321'))
select trade_id, count(*) ticks, sum(null_legs) legs_with_null_quote,
 count(*) filter (where abs(pnl - (mk - ep)*u) < 0.01) pnl_matches_mark,
 count(*) filter (where leg_mark is not null and abs(mk - leg_mark) < 0.001) mark_matches_legs,
 round(max(abs(mk - leg_mark)),4) max_gap
from t group by 1 order by 1;

-- Q19. Whole-life extrema with the STRUCTURAL bound (a long debit spread's mark must lie in
-- [0, width]).  Exactly one of 808 ticks violates it: trade 319 at 09:15:01, mark 194.25 on a
-- 150-wide spread, because the short leg's quote was unrefreshed while the long leg's was
-- current.  Preferred over excluding the opening window, which would have discarded two legal
-- marks to remove one illegal one.
with t as (
 select pt.trade_id, pt.tick_ts, pt.quantity_units::numeric u, tv.entry_premium::numeric ep,
   abs(tv.sell_strike::numeric - tv.buy_strike::numeric) width,
   (select sum(case when upper(l->>'side') in ('LONG','BUY') then (l->>'bid')::numeric
                    else -(l->>'ask')::numeric end)
    from jsonb_array_elements(pt.legs_json) l) mk,
   (select count(*) from jsonb_array_elements(pt.legs_json) l
    where (l->>'bid') is null or (l->>'ask') is null) nulls
 from position_ticks pt join trades_v2 tv on tv.id::text = pt.trade_id
 where pt.trade_id in ('318','319','320','321')
), r as (
 select trade_id, width, mk, (mk - ep)*u pnl,
        (mk >= -0.0001 and mk <= width + 0.0001) in_bounds
 from t where nulls = 0 and mk is not null)
select trade_id, max(width) width, count(*) ticks,
 count(*) filter (where not in_bounds) out_of_bounds,
 round(max(mk) filter (where not in_bounds),2) worst_impossible_mark,
 round(min(pnl) filter (where in_bounds),2) trough_bounded,
 round(max(pnl) filter (where in_bounds),2) peak_bounded
from r group by 1 order by 1;

-- Q20. The saved journey fields, for comparison with Q19.  Wrong on all four: the trough is
-- pinned to 0 on both overnight trades, and trade 319's saved peak of 267 is BELOW its own
-- booked actual_pnl of 3094, which is impossible for a peak.
select id, abs(sell_strike::numeric - buy_strike::numeric) width,
       peak_pnl::numeric saved_peak, journey_stats->>'trough_pnl' saved_trough,
       actual_pnl::numeric booked_gross, net_pnl::numeric booked_net
from trades_v2 where id in (318,319,320,321) order by id;

-- Q21. CORRECTION to the first version of Q19's write-up: the structural bound is NOT a
-- proposal, it already exists and already fires.  Three extrema are stored per position and
-- only trusted_* is right.
--   318: running 4476.00  trusted 4476.00  trades_v2 3776   anomalies 0
--   319: running 8586.50  trusted 4036.50  trades_v2  267   anomalies 1  <-- diverges
--   320: running 1673.75  trusted 1673.75  trades_v2 1645   anomalies 0
--   321: running 2775.00  trusted 2775.00  trades_v2 2457   anomalies 0
-- trusted_* matches an independent first-principles recomputation (Q19) to the rupee on all
-- four.  Guard contract:
--   a1_mark_trust_v2_quote_validity_plus_structural_bounds_plus_b3_1_book_width_gross_basis
with last as (
 select distinct on (trade_id) trade_id,
   running_mfe::numeric r_mfe, running_mae::numeric r_mae,
   (policy_trace_json->>'trusted_mfe')::numeric t_mfe,
   (policy_trace_json->>'trusted_mae')::numeric t_mae
 from position_ticks where trade_id in ('318','319','320','321')
 order by trade_id, tick_ts desc
), anom as (
 select trade_id, count(*) total,
   count(*) filter (where policy_trace_json->>'bound_anomaly' = 'true') bound_anomaly_ticks,
   count(*) filter (where policy_trace_json->'mark_trust'->>'state' = 'UNTRUSTED') untrusted_ticks
 from position_ticks where trade_id in ('318','319','320','321') group by 1)
select l.trade_id, a.total ticks, a.bound_anomaly_ticks, a.untrusted_ticks,
 l.r_mfe running_mfe, l.t_mfe trusted_mfe, l.r_mae running_mae, l.t_mae trusted_mae,
 tv.peak_pnl::numeric saved_peak, tv.journey_stats->>'trough_pnl' saved_trough
from last l join anom a using (trade_id) join trades_v2 tv on tv.id::text = l.trade_id
order by l.trade_id;

-- Q22. The guard firing, in full, on the one anomalous tick.
select to_char(tick_ts at time zone 'Asia/Kolkata','MM-DD HH24:MI:SS') t,
       executable_mark::numeric mark,
       policy_trace_json->>'bound_anomaly' bound_anomaly,
       policy_trace_json->'mark_trust'->>'state' mark_trust_state,
       policy_trace_json->'mark_trust'->>'structure_width' structure_width,
       policy_trace_json->'mark_trust'->>'price_gate_applied' price_gate_applied,
       (policy_trace_json->>'trusted_mfe')::numeric trusted_mfe
from position_ticks where trade_id='319' and session_date='2026-11-23' order by tick_ts;

-- Q23. Test 4 feasibility: the tick legs carry NO Greeks.  16 keys, none of delta/gamma/
-- theta/vega/iv -- they carry quotes and rich provenance (source_ts, source_age_ms,
-- source_validity, last_trade_ts, key_match, price_basis) but no risk measures.
select k, count(*) n from (
  select jsonb_object_keys(l) k
  from position_ticks pt, lateral jsonb_array_elements(pt.legs_json) l
  where pt.trade_id in ('318','319','320','321')
) s group by 1 order by 1;

-- =====================================================================================
-- TEST 6 part 2 — the exit policy's own signals against what the operator booked.
-- All four Paper positions were closed manually, and the tick service recorded at every
-- tick what the policy would have said, so this is a direct comparison on real fills.
-- =====================================================================================

-- Q24. Which signals fired per position, and the threshold basis.  Observed: thresholds are
-- constant_only / constant_safety_floor on ALL 822 ticks, so the live percentile-authority
-- publication path has never been exercised in Paper (E14).
select trade_id, count(*) ticks,
 string_agg(distinct policy_action, ',' order by policy_action) action_set,
 max((policy_trace_json->>'tp_threshold')::numeric) tp_threshold,
 min((policy_trace_json->>'sl_threshold')::numeric) sl_threshold,
 string_agg(distinct policy_trace_json->>'target_threshold_basis', ',') tp_basis,
 string_agg(distinct policy_trace_json->>'stop_threshold_basis', ',') sl_basis
from position_ticks where trade_id in ('318','319','320','321') group by 1 order by 1;

-- Q25. First firing of each non-HOLD action, with the P&L at that tick against the booked
-- outcome.  Results:
--   318 EOD 11-20 15:15:38 +1368.00   TP 11-23 09:15:01 +3775.50   booked +2079.00
--   319 EOD 11-20 15:15:38  +841.75   TP 11-23 09:16:05 +4036.50   booked +3094.00
--   320 none (HOLD on all 175 ticks)                               booked +1361.75
--   321 SL  11-23 11:14:51 -3147.00                                booked +1560.00
-- Every signal that fired was early; overriding 321's stop was worth +4707.00, and 2639.00
-- was lost between the target alert and the manual close one to two minutes later.
with f as (
 select distinct on (trade_id, policy_action) trade_id, policy_action,
   to_char(tick_ts at time zone 'Asia/Kolkata','MM-DD HH24:MI:SS') t,
   current_pnl::numeric pnl, executable_mark::numeric mark, policy_reason,
   (policy_trace_json->>'trusted_mfe')::numeric trusted_mfe_then,
   policy_trace_json->'mark_trust'->>'state' mark_trust_state
 from position_ticks
 where trade_id in ('318','319','320','321') and policy_action <> 'HOLD'
 order by trade_id, policy_action, tick_ts)
select f.*, tv.actual_pnl::numeric booked_gross, tv.net_pnl::numeric booked_net
from f join trades_v2 tv on tv.id::text = f.trade_id order by f.trade_id, f.t;

-- =====================================================================================
-- Q10 FINAL — the committed matched-row CSV: tools/pcr_vix_iv/test2b_data/q10_matched_rows.csv
-- 330 rows, full contract identity, both clocks, source age, quote status, source validity,
-- price basis, key match, trade id, pairing lag and the duplicate-candidate count.
-- Audit: key_match EXACT on all 330; 0 contracts with >1 instrument_key; 0 contracts matching
-- >1 trade; candidates within 45 s are 1 (156 polls) or 2 (174), max 2; absolute pairing lag
-- min 0.1 s, median 15.4 s, max 31.5 s. source_age_ms spans -168..+825 ms, median -47, and is
-- NEGATIVE on 204 of 330 rows, so the source clock reads ahead of the device clock and
-- source_age_ms is not a usable staleness measure as it stands.
-- The 8 source_outside_regular_session rows are all POST-CLOSE (15:35 and 15:40 on 20 Nov);
-- the 09:15 rows are flagged OK, so the app's own validity check does NOT support treating
-- 09:15 as suspect.
-- =====================================================================================
with ticks as (
  select pt.tick_ts, pt.trade_id, pt.session_date, tv.index_key, tv.expiry::text expiry,
         (l->>'instrument_key') ik, (l->>'strike')::numeric strike, (l->>'option_type') ot,
         (l->>'side') side, (l->>'bid')::numeric t_bid, (l->>'ask')::numeric t_ask,
         (l->>'source_ts') t_src, (l->>'source_age_ms') t_age, (l->>'quote_status') t_st,
         (l->>'source_validity') t_val, (l->>'price_basis') t_basis, (l->>'key_match') t_key
  from position_ticks pt join trades_v2 tv on tv.id::text = pt.trade_id,
       lateral jsonb_array_elements(pt.legs_json) l
  where pt.trade_id in ('318','319','320','321')
)
select c.session_date, c.index_key, c.expiry, c.strike, c.option_type,
       to_char(c.poll_ts at time zone 'Asia/Kolkata','HH24:MI:SS') chain_poll_ist, c.bid, c.ask,
       t.ik, t.side, t.t_bid, t.t_ask, t.t_src, t.t_age, t.t_st, t.t_val, t.t_basis, t.t_key,
       t.trade_id, to_char(t.tick_ts at time zone 'Asia/Kolkata','HH24:MI:SS') tick_ist,
       round(extract(epoch from (t.tick_ts - c.poll_ts))::numeric,1) pair_lag_s, t.cands
from ml_option_chain_snapshots c
join lateral (
  select tk.*, (select count(*) from ticks t2 where t2.session_date=c.session_date
                  and t2.index_key=c.index_key and t2.expiry=c.expiry and t2.strike=c.strike
                  and t2.ot=c.option_type
                  and abs(extract(epoch from (t2.tick_ts - c.poll_ts))) < 45) cands
  from ticks tk
  where tk.session_date=c.session_date and tk.index_key=c.index_key and tk.expiry=c.expiry
    and tk.strike=c.strike and tk.ot=c.option_type
    and abs(extract(epoch from (tk.tick_ts - c.poll_ts))) < 45
  order by abs(extract(epoch from (tk.tick_ts - c.poll_ts))) limit 1) t on true
where c.session_date in ('2026-11-20','2026-11-23')
  and exists (select 1 from ticks t3 where t3.session_date=c.session_date
                and t3.index_key=c.index_key and t3.expiry=c.expiry
                and t3.strike=c.strike and t3.ot=c.option_type)
order by 1,2,4,5,6;

-- Q26. The triggers and the first eligible trusted quote behind the exit comparison.
-- Declared tolerance: first tick at or after the triggering publication minute, within 10 min,
-- mark_trust TRUSTED and bound_anomaly false. All eight resolved in 0.05-0.80 min.
with trig(pos, sd, label, hm) as (values
  ('318','2026-11-20','C1_first_NEUTRAL','13:30'), ('318','2026-11-20','C2_two_consecutive','13:35'),
  ('319','2026-11-20','C1_first_NEUTRAL','13:20'), ('319','2026-11-20','C2_two_consecutive','13:35'),
  ('320','2026-11-23','C1_first_PULLBACK','10:05'), ('320','2026-11-23','C2_two_consecutive','11:30'),
  ('321','2026-11-23','C1_first_PULLBACK','10:05'), ('321','2026-11-23','C2_two_consecutive','11:20'))
select t.pos, t.label, t.hm trigger_hm,
       to_char(k.tick_ts at time zone 'Asia/Kolkata','HH24:MI:SS') quote_ts,
       round(extract(epoch from (k.tick_ts - (t.sd||' '||t.hm||':00')::timestamp at time zone 'Asia/Kolkata'))/60.0, 2) delay_min,
       k.executable_mark::numeric mark, k.current_pnl::numeric gross_pnl,
       k.policy_trace_json->'mark_trust'->>'state' trust,
       k.policy_trace_json->>'bound_anomaly' anomaly
from trig t
left join lateral (
  select * from position_ticks p
  where p.trade_id = t.pos and p.session_date = t.sd::date
    and p.tick_ts >= (t.sd||' '||t.hm||':00')::timestamp at time zone 'Asia/Kolkata'
    and p.tick_ts <  (t.sd||' '||t.hm||':00')::timestamp at time zone 'Asia/Kolkata' + interval '10 min'
    and p.policy_trace_json->'mark_trust'->>'state' = 'TRUSTED'
    and coalesce(p.policy_trace_json->>'bound_anomaly','false') = 'false'
  order by p.tick_ts limit 1) k on true
order by t.pos, t.label;
