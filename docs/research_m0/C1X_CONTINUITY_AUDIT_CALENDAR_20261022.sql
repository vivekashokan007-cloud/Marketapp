-- C1X continuity audit, CALENDAR-DERIVED (corrects C1X_CONTINUITY_AUDIT_EXPANDED_20261022.sql per Codex A2).
-- READ ONLY. Bounded: at most 5 entry sessions per run, every chain lookup is a poll_ts range on the leading
-- column of ml_ocs_unique (poll_ts, index_key, strike, option_type); 20 s statement timeout; ends with ROLLBACK.
-- NOT run on production by the M0 draft. Validated only against synthetic scratch data
-- (tools/research_m0_db_suite/audit_suite.py).
--
-- psql variables (all required):
--   :'start'  first entry session (YYYY-MM-DD)     :'end'  last entry session (<= start + 6 calendar days)
--   :'asof'   audit time (timestamptz) - expected observations after it are PENDING (right-censored), not missing
--   :'bnf_expiries'  comma list of AUTHORIZED BNF expiries for the window ('' = BNF expiry authority unknown ->
--                    every BNF entry is EXPIRY_AUTHORITY_UNKNOWN; nothing is borrowed from NF or from the data)
-- Definitions:
--   expected sessions = weekdays minus the 2026 NSE F&O holiday list (other years: the run aborts, fail closed);
--   entry clocks 09:30,10:30,11:30,12:30,13:30,14:30; O(T) = first poll with poll_ts in [T, T+90 s) for that
--   index and the ONE authorized expiry (NF: Tuesday / previous session rule; BNF: supplied list);
--   rows for any other expiry never participate. Conflicting duplicate quote rows (same key, different bid/ask/
--   ltp) make the poll CONFLICTING before any selection; identical duplicates are counted separately.
--   K0 = strike with both legs EXEC_OK (bid>0, ask>0, ask>=bid) minimising |C-P| mid, ties lower strike, from O(T)
--   only. Step = the smallest positive listed strike gap at O(T) (dated listed step; never a 50/100 default).
--   P2 = K0 +/- 2 steps. P-S wing = smallest listed symmetric w >= straddle mid with K0+w CE and K0-w PE both
--   EXEC_OK at O(T). Entry recheck E = first poll strictly after O(T) receipt (historical proxy for D, labelled
--   AVAIL_PROXY_OT_RECEIPT) and before O(T)+6m30s: the SAME contracts are re-checked, never reselected.
--   Marks: every calendar session from the entry session to the expiry day at 15:05 (first poll in
--   [15:05, 15:05+90 s)). Uniquely valid = exactly one row AND EXEC_OK. Missing roles (no K0/wing) stay in the
--   denominators as explicit states.
begin read only;
set local statement_timeout = '20s';
set local work_mem = '32MB';
set local search_path = pg_catalog;

with
params as (
  select :'start'::date as d0, :'end'::date as d1, :'asof'::timestamptz as asof,
         coalesce(nullif(:'bnf_expiries', ''), null) as bnf_list
),
guard as (
  select case
    when d1 < d0 or d1 > d0 + 6 then (select 1/0)              -- window too large: abort (bounded plan)
    when extract(year from d0) <> 2026 or extract(year from d1) <> 2026 then (select 1/0)  -- 2026 authority only
    else 1 end as ok from params
),
holidays(d) as (values (date '2026-01-26'),(date '2026-03-03'),(date '2026-03-26'),(date '2026-03-31'),
  (date '2026-04-03'),(date '2026-04-14'),(date '2026-05-01'),(date '2026-05-28'),(date '2026-06-26'),
  (date '2026-09-14'),(date '2026-10-02'),(date '2026-10-20'),(date '2026-11-10'),(date '2026-11-24'),(date '2026-12-25')),
cal as (
  select g::date as d from params, guard, generate_series(d0, least(d1 + 40, date '2026-12-31'), interval '1 day') g
  where extract(isodow from g) < 6 and g::date not in (select d from holidays)
),
entry_sessions as (select c.d from cal c, params p where c.d between p.d0 and p.d1),
-- NF authorized expiry: Tuesday of the week (or next week if passed), previous session when Tuesday is a holiday
nf_exp as (
  select e.d as session_date,
    (select max(c.d) from cal c where c.d <= t.tue and c.d >= e.d) as expiry
  from entry_sessions e
  cross join lateral (select e.d + ((9 - extract(isodow from e.d)::int) % 7) as tue0) w
  cross join lateral (select case when (select max(c.d) from cal c where c.d <= w.tue0 and c.d >= e.d) is null
                                  then w.tue0 + 7 else w.tue0 end as tue) t
),
bnf_exp as (
  select e.d as session_date,
    (select min(x::date) from params p, unnest(string_to_array(p.bnf_list, ',')) x where x::date >= e.d) as expiry
  from entry_sessions e
),
auth as (
  select 'NF'::text as index_key, session_date, expiry, 'NF_TUE_PREV_SESSION_RULE'::text as authority from nf_exp
  union all
  select 'BNF', session_date, expiry, case when expiry is null then 'EXPIRY_AUTHORITY_UNKNOWN' else 'SUPPLIED_LIST' end from bnf_exp
),
clocks(lbl) as (values ('09:30'::time),('10:30'::time),('11:30'::time),('12:30'::time),('13:30'::time),('14:30'::time)),
entries as (
  select a.index_key, a.session_date, a.expiry, a.authority, c.lbl,
         (a.session_date + c.lbl) at time zone 'Asia/Kolkata' as t
  from auth a cross join clocks c
),
-- O(T): window predicate inside the lateral so a missing window stays a row with ot_ts NULL
ot as (
  select e.*, o.poll_ts as ot_ts
  from entries e
  left join lateral (
    select s.poll_ts from public.ml_option_chain_snapshots s
    where e.expiry is not null and s.poll_ts >= e.t and s.poll_ts < e.t + interval '90 seconds'
      and s.index_key = e.index_key and s.expiry = to_char(e.expiry, 'YYYY-MM-DD')
    order by s.poll_ts limit 1) o on true
),
ot_rows as (
  select o.index_key, o.session_date, o.lbl, s.strike,
         case upper(s.option_type) when 'CALL' then 'CE' when 'PUT' then 'PE' else upper(s.option_type) end as ot,
         count(*) as n, count(distinct (s.bid, s.ask, s.ltp)) as nd,
         min(s.bid) as bid, min(s.ask) as ask
  from ot o join public.ml_option_chain_snapshots s
    on s.poll_ts = o.ot_ts and s.index_key = o.index_key and s.expiry = to_char(o.expiry, 'YYYY-MM-DD')
  where o.ot_ts is not null
  group by 1,2,3,4,5
),
ot_conf as (select index_key, session_date, lbl, bool_or(nd > 1) as conflicting, sum(n - 1) filter (where nd = 1 and n > 1) as identical_dups
            from ot_rows group by 1,2,3),
ot_ok as (
  select r.*, (r.bid > 0 and r.ask > 0 and r.ask >= r.bid) is true as exec_ok, (r.bid + r.ask) / 2.0 as mid
  from ot_rows r join ot_conf c using (index_key, session_date, lbl) where not c.conflicting and r.nd = 1
),
k0 as (
  select distinct on (c.index_key, c.session_date, c.lbl) c.index_key, c.session_date, c.lbl, c.strike as k0, c.mid + p.mid as smid
  from ot_ok c join ot_ok p on p.index_key = c.index_key and p.session_date = c.session_date and p.lbl = c.lbl
    and p.strike = c.strike and p.ot = 'PE' and p.exec_ok
  where c.ot = 'CE' and c.exec_ok
  order by c.index_key, c.session_date, c.lbl, abs(c.mid - p.mid), c.strike
),
step as (
  select x.index_key, x.session_date, x.lbl, min(x.gap) as step from (
    select index_key, session_date, lbl, strike - lag(strike) over (partition by index_key, session_date, lbl order by strike) as gap
    from (select distinct index_key, session_date, lbl, strike from ot_ok) s) x
  where x.gap > 0 group by 1,2,3
),
wing as (
  select distinct on (k.index_key, k.session_date, k.lbl) k.index_key, k.session_date, k.lbl, u.strike - k.k0 as w
  from k0 k
  join ot_ok u on u.index_key = k.index_key and u.session_date = k.session_date and u.lbl = k.lbl
     and u.ot = 'CE' and u.exec_ok and u.strike - k.k0 >= k.smid
  join ot_ok d on d.index_key = k.index_key and d.session_date = k.session_date and d.lbl = k.lbl
     and d.ot = 'PE' and d.exec_ok and d.strike = 2 * k.k0 - u.strike
  order by k.index_key, k.session_date, k.lbl, u.strike - k.k0
),
entry_state as (
  select o.index_key, o.session_date, o.expiry, o.authority, o.lbl, o.t, o.ot_ts, k.k0, st.step, w.w,
    case when o.authority = 'EXPIRY_AUTHORITY_UNKNOWN' then 'EXPIRY_AUTHORITY_UNKNOWN'
         when o.expiry > date '2026-12-31' then 'CALENDAR_UNAVAILABLE'
         when o.t > (select asof from params) then 'PENDING'
         when o.ot_ts is null then 'MISSING_OT'
         when c.conflicting then 'CONFLICTING_DUPLICATES'
         when k.k0 is null then 'NO_K0'
         else 'SELECTED' end as state
  from ot o
  left join ot_conf c using (index_key, session_date, lbl)
  left join k0 k using (index_key, session_date, lbl)
  left join step st using (index_key, session_date, lbl)
  left join wing w using (index_key, session_date, lbl)
),
legs as (
  select e.index_key, e.session_date, e.expiry, e.lbl, e.ot_ts, r.role, r.strike, r.otype, e.state as entry_state
  from entry_state e cross join lateral (values
    ('K0_CE', e.k0, 'CE'), ('K0_PE', e.k0, 'PE'),
    ('P2_CE', e.k0 + 2 * e.step, 'CE'), ('P2_PE', e.k0 - 2 * e.step, 'PE'),
    ('PS_WING_CE', e.k0 + e.w, 'CE'), ('PS_WING_PE', e.k0 - e.w, 'PE')) r(role, strike, otype)
),
-- E: first poll strictly after O(T) receipt, before O(T)+6m30s; same contracts rechecked
eobs as (
  select l.*, eo.poll_ts as e_ts
  from legs l left join lateral (
    select s.poll_ts from public.ml_option_chain_snapshots s
    where l.ot_ts is not null and s.poll_ts > l.ot_ts and s.poll_ts < l.ot_ts + interval '390 seconds'
      and s.index_key = l.index_key and s.expiry = to_char(l.expiry, 'YYYY-MM-DD')
    order by s.poll_ts limit 1) eo on true
),
-- expected marks from the CALENDAR, not from stored polls
mark_exp as (
  select l.index_key, l.session_date, l.expiry, l.lbl, l.role, l.strike, l.otype, l.entry_state, c.d as mark_date,
         (c.d + time '15:05') at time zone 'Asia/Kolkata' as mt
  from legs l join cal c on c.d >= l.session_date and c.d <= l.expiry
),
mark_poll as (
  select distinct m.index_key, m.expiry, m.mark_date, m.mt from mark_exp m
),
mark_obs as (
  select mp.*, o.poll_ts as m_ts from mark_poll mp left join lateral (
    select s.poll_ts from public.ml_option_chain_snapshots s
    where s.poll_ts >= mp.mt and s.poll_ts < mp.mt + interval '90 seconds'
      and s.index_key = mp.index_key and s.expiry = to_char(mp.expiry, 'YYYY-MM-DD')
    order by s.poll_ts limit 1) o on true
),
leg_check as (
  select m.*, mo.m_ts,
    (select count(*) from public.ml_option_chain_snapshots s where mo.m_ts is not null and m.strike is not null
       and s.poll_ts = mo.m_ts and s.index_key = m.index_key and s.expiry = to_char(m.expiry, 'YYYY-MM-DD')
       and s.strike = m.strike and upper(s.option_type) in (m.otype, case m.otype when 'CE' then 'CALL' else 'PUT' end)) as nrows,
    (select count(distinct (s.bid, s.ask, s.ltp)) from public.ml_option_chain_snapshots s where mo.m_ts is not null and m.strike is not null
       and s.poll_ts = mo.m_ts and s.index_key = m.index_key and s.expiry = to_char(m.expiry, 'YYYY-MM-DD')
       and s.strike = m.strike and upper(s.option_type) in (m.otype, case m.otype when 'CE' then 'CALL' else 'PUT' end)) as ndistinct,
    (select bool_and((s.bid > 0 and s.ask > 0 and s.ask >= s.bid) is true) from public.ml_option_chain_snapshots s where mo.m_ts is not null and m.strike is not null
       and s.poll_ts = mo.m_ts and s.index_key = m.index_key and s.expiry = to_char(m.expiry, 'YYYY-MM-DD')
       and s.strike = m.strike and upper(s.option_type) in (m.otype, case m.otype when 'CE' then 'CALL' else 'PUT' end)) as all_ok
  from mark_exp m join mark_obs mo using (index_key, expiry, mark_date, mt)
),
mark_state as (
  select index_key, role, (mark_date = expiry) as expiry_day,
    case when entry_state <> 'SELECTED' then 'ENTRY_' || entry_state
         when strike is null then 'ROLE_UNAVAILABLE'
         when mt > (select asof from params) then 'PENDING'
         when m_ts is null then 'MISSING_MARK_POLL'
         when nrows = 0 then 'MISSING_ROW'
         when ndistinct > 1 then 'CONFLICTING_DUPLICATE'
         when nrows > 1 then 'IDENTICAL_DUPLICATE'
         when all_ok then 'UNIQUELY_VALID'
         else 'INVALID_QUOTE' end as state
  from leg_check
),
entry_recheck as (
  select index_key, role,
    case when entry_state <> 'SELECTED' then 'ENTRY_' || entry_state
         when strike is null then 'ROLE_UNAVAILABLE'
         when e_ts is null then 'NO_POST_DECISION_POLL'
         else (select case when count(*) = 0 then 'MISSING_ROW' when count(distinct (s.bid, s.ask, s.ltp)) > 1 then 'CONFLICTING_DUPLICATE'
                           when count(*) > 1 then 'IDENTICAL_DUPLICATE'
                           when bool_and((s.bid > 0 and s.ask > 0 and s.ask >= s.bid) is true) then 'UNIQUELY_VALID' else 'INVALID_QUOTE' end
               from public.ml_option_chain_snapshots s
               where s.poll_ts = e_ts and s.index_key = eobs.index_key and s.expiry = to_char(eobs.expiry, 'YYYY-MM-DD')
                 and s.strike = eobs.strike and upper(s.option_type) in (eobs.otype, case eobs.otype when 'CE' then 'CALL' else 'PUT' end))
    end as state
  from eobs
)
select 'entry' as part, index_key, null::text as role, null::boolean as expiry_day, state, count(*) as n
  from entry_state group by 1,2,3,4,5
union all
select 'entry_recheck_AVAIL_PROXY_OT_RECEIPT', index_key, role, null::boolean, state, count(*) from entry_recheck group by 1,2,3,4,5
union all
select 'mark_1505', index_key, role, expiry_day, state, count(*) from mark_state group by 1,2,3,4,5
order by 1,2,3,4,5;

rollback;
