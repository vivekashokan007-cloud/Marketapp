-- Stage A cross-check: the EXECUTED 5 Oct sweep SQL, unchanged in every CTE that defines the
-- 'IB 400 / 12:30 / IV-RV 1.3-1.7 / C2' cell, reduced only where the cell cannot be affected:
--   * slots limited to E1230 and C (other entry slots and O exits feed other cells only);
--   * legs limited to IB m=8 (the 400-point wing);
--   * calendar capped at the 2026-10-05 cutoff (sessions recorded after the original run are excluded);
--   * output = per-entry-day rows for the cell instead of grouping-set summaries, plus eligible entries
--     whose C2 exit produced no row (the inner join that dropped 23 Jul).
-- Read-only; run AFTER market hours. Compare with Stage A (run_legacy_sql_v0) and the published list.
-- Placeholders (substituted by sqlkit.render): :CAL_START = 2026-06-15, :WIN_START = 2026-06-22,
-- :CUTOFF = 2026-10-05 (entry window end and calendar cap).
begin read only;
set local statement_timeout = '60s'; set local work_mem = '256MB'; set local enable_nestloop = off;
with
cal as (select d, row_number() over (order by d) sn
        from (select distinct session_date d from ml_brain_snapshots
              where session_date >= :CAL_START::date and session_date <= :CUTOFF::date) x),
rng as (select min(sn) a, max(sn) b from cal where d between :WIN_START::date and :CUTOFF::date),
win as (select c.d, c.sn from cal c, rng where c.sn between rng.a - 6 and rng.b + 6),
pt as (select distinct poll_ts, (poll_ts at time zone 'Asia/Kolkata')::date d, (poll_ts at time zone 'Asia/Kolkata')::time t
       from ml_option_chain_snapshots
       where poll_ts >= (select (min(d) + time '09:00') at time zone 'Asia/Kolkata' from win)
         and poll_ts <  (select (max(d) + time '16:00') at time zone 'Asia/Kolkata' from win)),
tg(slot, tt) as (values ('E1230','12:30'::time),('C','15:20'::time)),
cp as (select pt.d, tg.slot, tg.tt, min(pt.poll_ts) poll_ts
       from pt join tg on pt.t >= tg.tt and pt.t < tg.tt + interval '25 minutes' group by 1,2,3),
q as materialized (select c.poll_ts, c.index_key ix, c.expiry ex, c.strike k, c.option_type o, c.bid, c.ask
      from ml_option_chain_snapshots c
      where c.poll_ts in (select poll_ts from cp) and c.index_key = 'NF'
        and c.ask > 0 and c.bid >= 0 and c.ask >= c.bid and c.expiry ~ '^\d{4}-\d{2}-\d{2}$'),
atm as (select distinct on (a.poll_ts, a.ix, a.ex) a.poll_ts, a.ix, a.ex, a.k k0,
               a.k + (a.bid+a.ask)/2 - (b.bid+b.ask)/2 spot, (a.bid+a.ask)/2 + (b.bid+b.ask)/2 straddle
        from q a join q b on b.poll_ts=a.poll_ts and b.ix=a.ix and b.ex=a.ex and b.k=a.k and b.o='PE'
        where a.o='CE'
        order by a.poll_ts, a.ix, a.ex, abs((a.bid+a.ask)/2 - (b.bid+b.ask)/2)),
mk as (select cp.d, w.sn, cp.slot, a.ix, a.ex, a.spot, a.straddle,
              (select count(*) from generate_series(cp.d + 1, a.ex::date, interval '1 day') g where extract(isodow from g) < 6)
                + greatest(0.05, extract(epoch from (time '15:30' - cp.tt))/22500.0) td
       from cp join atm a on a.poll_ts=cp.poll_ts join win w on w.d=cp.d),
cls as (select d, sn, ix, ex, spot, (straddle/0.798)/spot/sqrt(td) ivd from mk where slot='C'),
rv as (select c1.ix, w.sn, sqrt(avg(power(ln(c2.spot/c1.spot),2))) rv_daily
       from win w join cls c1 on c1.sn between w.sn-6 and w.sn-2
                  join cls c2 on c2.ix=c1.ix and c2.sn=c1.sn+1
       group by 1,2),
ent2 as materialized (
  select e.d, e.sn, e.slot, a.poll_ts, e.ix, e.ex, a.k0, e.spot, 50 s, 65 lot,
         e.td, (e.straddle/0.798) sig_t, (e.straddle/0.798)/e.spot/sqrt(e.td) ivd, r.rv_daily
  from mk e join cp on cp.d=e.d and cp.slot=e.slot join atm a on a.poll_ts=cp.poll_ts and a.ix=e.ix
  left join rv r on r.ix=e.ix and r.sn=e.sn
  where e.slot = 'E1230' and e.d between :WIN_START::date and :CUTOFF::date),
feat as materialized (
  select e.*,
    case when rv_daily is null or rv_daily=0 then 'vna' when ivd/rv_daily < 1.0 then 'v<1.0' when ivd/rv_daily < 1.3 then 'v1.0'
         when ivd/rv_daily < 1.7 then 'v1.3' else 'v1.7' end vb
  from ent2 e),
legs as (
  select 'IB' st, 0 j, 8 m, 'CE' o, 0 ko, -1 side union all
  select 'IB', 0, 8, 'PE', 0, -1 union all
  select 'IB', 0, 8, 'CE', 8, 1 union all
  select 'IB', 0, 8, 'PE', -8, 1),
st_e as (select e.d, e.slot, e.ix, l.st, l.j, l.m,
                sum(case when l.side=-1 then q.bid else -q.ask end) credit,
                sum(case when l.side=-1 then q.bid else 0 end) sell_px, sum(case when l.side=1 then q.ask else 0 end) buy_px,
                count(*) nl
         from feat e cross join legs l
         join q on q.poll_ts=e.poll_ts and q.ix=e.ix and q.ex=e.ex and q.k=e.k0+l.ko*e.s and q.o=l.o
         where (l.side = -1 and q.bid > 0) or (l.side = 1 and q.ask > 0)
         group by 1,2,3,4,5,6),
st_ok as (select s.*, e.sn, e.ex, e.k0, e.s, e.lot, e.vb
          from st_e s join feat e on e.d=s.d and e.slot=s.slot and e.ix=s.ix
          where s.nl = 4 and s.credit <> 0 and (s.m*e.s - s.credit) > 0),
xp as (select e.d, e.slot, e.ix, e.ex, e.k0, e.s, w.sn - e.sn k, cp.slot xslot, cp.poll_ts xpoll, (w.d = e.ex::date) is_exp
       from feat e
       join win w on w.sn = e.sn + 2 and w.d <= e.ex::date
       join cp on cp.d = w.d and cp.slot = 'C'),
xl as (select x.d, x.slot, x.ix, x.k, x.xslot, x.is_exp, l.st, l.j, l.m,
              sum(case when l.side=-1 then -q.ask else q.bid end) exit_cash,
              sum(case when l.side=-1 then 0 else q.bid end) exit_sell_px,
              sum(case when l.side=-1 then q.ask else 0 end) exit_buy_px, count(*) nl
       from xp x cross join legs l
       join q on q.poll_ts=x.xpoll and q.ix=x.ix and q.ex=x.ex and q.k=x.k0+l.ko*x.s and q.o=l.o
       group by 1,2,3,4,5,6,7,8,9),
out as (select so.d, so.vb, x.is_exp,
               (so.credit + x.exit_cash) * so.lot
                 - ( so.nl*2*20*1.18
                   + 0.0015*(so.sell_px + x.exit_sell_px)*so.lot
                   + 0.0003553*1.18*(so.sell_px + so.buy_px + x.exit_sell_px + x.exit_buy_px)*so.lot
                   + 0.00003*(so.buy_px + x.exit_buy_px)*so.lot ) net
        from st_ok so join xl x on x.d=so.d and x.slot=so.slot and x.ix=so.ix and x.st=so.st and x.j=so.j
                                and x.m=so.m and x.nl=so.nl),
cell_days as (select distinct d from st_ok where vb = 'v1.3'),
res as (
  select c.d,
         (select count(*) from feat f where f.d = c.d) feat_rows,
         (select count(*) from out o where o.d = c.d and o.vb = 'v1.3') out_rows,
         (select round(sum(o.net)::numeric, 2) from out o where o.d = c.d and o.vb = 'v1.3') net,
         (select bool_or(o.is_exp) from out o where o.d = c.d and o.vb = 'v1.3') is_exp,
         (select count(*) from feat f join win w on w.sn = f.sn + 2 and w.d <= f.ex::date where f.d = c.d) c2_days
  from cell_days c)
select string_agg(d || '|' || feat_rows || '|' || out_rows || '|' || coalesce(net::text, '') || '|'
                  || coalesce(is_exp::text, '') || '|' || c2_days, E'\n' order by d) rows,
       extract(epoch from clock_timestamp() - statement_timestamp())::int secs
from res;
commit;
