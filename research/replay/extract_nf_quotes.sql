-- Rule-scoped NF quote extract for Stage A / Stage B (read-only, run AFTER market hours).
-- Replace :A and :B with the part's first/last IST session dates (YYYY-MM-DD).
-- Output: one row (part text body, md5 of the body). The body is written verbatim to
-- part<NN>.txt after the header lines '#part=', '#range=', '#md5=' and '#body'.
--
-- Slots (IST): E = first poll in [12:30,12:55); C = first poll in [15:20,15:45);
--              L = last poll of the day, only when the day has no C poll.
-- Strikes per line:
--   E: ATM-150..ATM+150 (ATM detection) plus ATM+-300 and ATM+-400 (IB 300/400 legs).
--   C/L: own ATM-150..+150 (close spot for RV) plus {K0, K0+-300, K0+-400} of every
--        E poll in the previous 7 calendar days (exit legs of open entries).
-- ATM rule (same as the engine): valid quote = bid>=0, ask>0, ask>=bid; minimise
-- |mid(CE)-mid(PE)|, ties to the lower strike. Every expiry in the poll is kept on its
-- own line; the engine selects the nearest expiry explicitly.
begin read only;
set local statement_timeout = '45s';
with
pd as (
  select distinct poll_ts, (poll_ts at time zone 'Asia/Kolkata')::date d, (poll_ts at time zone 'Asia/Kolkata')::time t
  from ml_option_chain_snapshots
  where index_key = 'NF'
    and poll_ts >= ((:A::date - 8) + time '09:00') at time zone 'Asia/Kolkata'
    and poll_ts <  (:B::date + time '16:00') at time zone 'Asia/Kolkata'),
e as (select d, min(poll_ts) poll_ts, 'E'::text slot from pd where t >= '12:30' and t < '12:55' group by d),
c as (select d, min(poll_ts) poll_ts, 'C'::text slot from pd where t >= '15:20' and t < '15:45' group by d),
l as (select d, max(poll_ts) poll_ts, 'L'::text slot from pd where d not in (select d from c) group by d),
sel as (select * from e union all select * from c union all select * from l),
q as (select s.d, s.slot, x.poll_ts, x.expiry, x.strike, x.option_type, x.bid, x.ask
      from sel s join ml_option_chain_snapshots x on x.poll_ts = s.poll_ts and x.index_key = 'NF'
      where x.expiry ~ '^\d{4}-\d{2}-\d{2}$'),
atm as (
  select distinct on (a.poll_ts, a.expiry) a.d, a.slot, a.poll_ts, a.expiry, a.strike k0
  from q a join q b on b.poll_ts = a.poll_ts and b.expiry = a.expiry and b.strike = a.strike and b.option_type = 'PE'
  where a.option_type = 'CE' and a.bid >= 0 and a.ask > 0 and a.ask >= a.bid and b.bid >= 0 and b.ask > 0 and b.ask >= b.bid
  order by a.poll_ts, a.expiry, abs((a.bid + a.ask) / 2 - (b.bid + b.ask) / 2), a.strike),
want as (
  select a.poll_ts, a.expiry, a.k0 + off k from atm a,
    unnest(array[-150,-100,-50,0,50,100,150]) off
  union
  select a.poll_ts, a.expiry, a.k0 + off from atm a, unnest(array[-400,-300,300,400]) off where a.slot = 'E'
  union
  select cl.poll_ts, cl.expiry, en.k0 + off
  from atm cl join atm en on en.slot = 'E' and en.expiry = cl.expiry and en.d >= cl.d - 7 and en.d < cl.d,
    unnest(array[-400,-300,0,300,400]) off
  where cl.slot in ('C','L')),
px as (
  select w.poll_ts, w.expiry, w.k,
    coalesce(round(max(q.bid) filter (where q.option_type='CE')::numeric, 2)::text, '') cb,
    coalesce(round(max(q.ask) filter (where q.option_type='CE')::numeric, 2)::text, '') ca,
    coalesce(round(max(q.bid) filter (where q.option_type='PE')::numeric, 2)::text, '') pb,
    coalesce(round(max(q.ask) filter (where q.option_type='PE')::numeric, 2)::text, '') pa
  from want w left join q on q.poll_ts = w.poll_ts and q.expiry = w.expiry and q.strike = w.k
  group by 1,2,3),
lines as (
  select 'P|' || to_char(s.poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') || '|' || s.slot || '|NF|' || p.expiry || '|'
         || string_agg(p.k || ':' || p.cb || ',' || p.ca || ',' || p.pb || ',' || p.pa, ';' order by p.k) line
  from sel s join px p on p.poll_ts = s.poll_ts
  where s.d between :A::date and :B::date
  group by s.poll_ts, s.slot, p.expiry
  union all
  select distinct 'S|' || session_date from ml_brain_snapshots where session_date between :A::date and :B::date),
body as (select string_agg(line, E'\n' order by line) b, count(*) n from lines)
select n, md5(b) md5, b from body;
commit;
