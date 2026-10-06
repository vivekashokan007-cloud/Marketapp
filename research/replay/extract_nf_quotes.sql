-- NF quote extract v2 for Stage A / Stage B (read-only; run AFTER market hours, one part at a time).
-- Replace :A and :B with the part's first/last IST session dates (YYYY-MM-DD). Run EXPLAIN on the
-- first part before executing any part. Output: one row (n, md5(body), body, line-kind counts).
-- The body is written verbatim to part<NN>.txt after the header lines
--   #format=nf_quotes_v2  #part=NN  #range=A..B  #n=<n>  #md5=<md5>  #body
--
-- Line kinds (nothing is aggregated, de-duplicated or validated here; the engine does that):
--   S|<session>                                      a date with ml_brain_snapshots rows (legacy observed calendar)
--   W|<session>|<E|C|L>|<any|nf>|<poll_ts>|<nf rows>  window poll selection
--        E = [12:30,12:55) IST, C = [15:20,15:45) IST. 'any' = first poll of ANY index (the 5 Oct sweep's
--        rule), 'nf' = first poll with NF rows (corrected rule). L|nf = last NF poll of a day with no NF C poll.
--   A|<poll_ts>|<expiry>|<k0>|<spot>|<straddle>|<tied strikes>|<distinct min pairs>
--        legacy ATM exactly as the sweep computed it: over VALID rows (bid>=0, ask>0, ask>=bid), minimise
--        |mid(CE)-mid(PE)|. All strikes achieving the minimum are listed; k0/spot/straddle use the lowest.
--   R|<poll_ts>|<expiry>|<strike>/<C|P>:<bid>,<ask>[/<C|P>:<bid>,<ask>...];...
--        RAW rows, one token per stored row (a duplicate row appears twice; an empty field is SQL NULL;
--        an option with no stored row has no token). Rows with a non-date expiry are emitted with expiry 'X'.
--   D|<poll_ts>|<NF rows>|<distinct NF (strike,type)>|<distinct valid NF expiries>|<NF rows with non-date expiry>|<inventory>
--        per-poll integrity counts over the WHOLE NF chain at that poll, not just emitted strikes, plus the
--        complete inventory of valid expiries (comma list). ml_ocs_unique is (poll_ts,index_key,strike,option_type)
--        with no expiry, so rows = distinct (strike,type) is expected and >1 valid expiry means an interleaved chain.
-- A 'valid' expiry is a string equal to a real calendar date 2024-01-01..2030-12-31 in YYYY-MM-DD form (lookup,
-- never a cast), so shaped-but-impossible values ('2026-99-99', '2026-02-30') count as non-date ('X').
-- Strikes emitted per (poll, expiry) - only what the IB-400 rule and the engines read:
--   E polls: every tied k0 of every expiry at the poll + {-400, 0, +400} (entry legs; k0 CE/PE also give spot).
--   C polls: every tied k0 at the poll (close spot for RV) plus {-400, 0, +400} around every tied k0 of every
--            E poll in the previous 14 calendar days (exit legs of open entries, including observed-calendar
--            C2 exits stretched by outages and deferred marks up to expiry).
--   The A line carries the full-chain ATM (all tied strikes); D carries whole-chain integrity and inventory.
begin read only;
set local statement_timeout = '45s';
set local work_mem = '64MB';
with
vd as (select to_char(g, 'YYYY-MM-DD') s from generate_series(date '2024-01-01', date '2030-12-31', interval '1 day') g),
days as (select g::date d from generate_series(:A::date - 15, :B::date, interval '1 day') g),
rng as (select d, s.slot, ((d + s.t0) at time zone 'Asia/Kolkata') lo,
               ((d + s.t0 + interval '25 minutes') at time zone 'Asia/Kolkata') hi
        from days, (values ('E', time '12:30'), ('C', time '15:20')) s(slot, t0)),
pw as materialized (
  select r.d, r.slot, min(c.poll_ts) any_poll, min(c.poll_ts) filter (where c.index_key = 'NF') nf_poll
  from rng r join ml_option_chain_snapshots c on c.poll_ts >= r.lo and c.poll_ts < r.hi
  group by 1, 2),
lp as materialized (
  select d.d, (select max(c.poll_ts) from ml_option_chain_snapshots c
               where c.index_key = 'NF'
                 and c.poll_ts >= ((d.d + time '09:00') at time zone 'Asia/Kolkata')
                 and c.poll_ts <  ((d.d + time '16:00') at time zone 'Asia/Kolkata')) last_nf
  from days d
  where d.d between :A::date and :B::date and extract(isodow from d.d) < 6
    and not exists (select 1 from pw where pw.d = d.d and pw.slot = 'C' and pw.nf_poll is not null)),
sel as (select d, slot, any_poll poll_ts from pw where any_poll is not null
        union select d, slot, nf_poll from pw where nf_poll is not null),
q as materialized (
  select s.d, s.slot, c.poll_ts, case when c.expiry in (select vd.s from vd) then c.expiry else 'X' end expiry,
         c.strike, c.option_type, c.bid, c.ask
  from (select distinct d, slot, poll_ts from sel) s
  join ml_option_chain_snapshots c on c.poll_ts = s.poll_ts and c.index_key = 'NF'),
v as (select * from q where expiry <> 'X' and ask > 0 and bid >= 0 and ask >= bid),
pairs as (
  select a.d, a.slot, a.poll_ts, a.expiry, a.strike k, a.bid cb, a.ask ca, b.bid pb, b.ask pa,
         abs((a.bid + a.ask) / 2 - (b.bid + b.ask) / 2) gap
  from v a join v b on b.poll_ts = a.poll_ts and b.expiry = a.expiry and b.strike = a.strike and b.option_type = 'PE'
  where a.option_type = 'CE'),
mins as (select poll_ts, expiry, min(gap) g from pairs group by 1, 2),
atm as materialized (
  select p.d, p.slot, p.poll_ts, p.expiry, min(p.k) k0,
         array_to_string(array_agg(distinct p.k order by p.k), ',') tied,
         count(distinct (p.k, p.cb, p.ca, p.pb, p.pa)) npairs
  from pairs p join mins m on m.poll_ts = p.poll_ts and m.expiry = p.expiry and p.gap = m.g
  group by 1, 2, 3, 4),
atm_v as (
  select a.*, p.k + (p.cb + p.ca) / 2 - (p.pb + p.pa) / 2 spot, (p.cb + p.ca) / 2 + (p.pb + p.pa) / 2 straddle
  from atm a join lateral (
    select * from pairs p where p.poll_ts = a.poll_ts and p.expiry = a.expiry and p.k = a.k0
    order by p.gap, p.cb, p.ca, p.pb, p.pa limit 1) p on true),
k0s as (select distinct poll_ts, d, slot, unnest(string_to_array(tied, ','))::int k0 from atm),
pe as (select distinct poll_ts, expiry from q),
want as (
  select pe.poll_ts, pe.expiry, k.k0 + off k
  from pe join k0s k on k.poll_ts = pe.poll_ts and k.slot = 'E',
       unnest(array[-400,0,400]) off
  union
  select pe.poll_ts, pe.expiry, k.k0 + off
  from pe join k0s k on k.poll_ts = pe.poll_ts and k.slot = 'C',
       unnest(array[0]) off
  union
  select pe.poll_ts, pe.expiry, en.k0 + off
  from pe join k0s cl on cl.poll_ts = pe.poll_ts and cl.slot = 'C'
          join k0s en on en.slot = 'E' and en.d >= cl.d - 14 and en.d < cl.d,
       unnest(array[-400,0,400]) off),
rtok as (
  select q.poll_ts, q.expiry, q.strike,
         string_agg(case q.option_type when 'CE' then 'C' when 'PE' then 'P' else '?' || q.option_type end
                    || ':' || coalesce(round(q.bid::numeric, 2)::text, '') || ',' || coalesce(round(q.ask::numeric, 2)::text, ''),
                    '/' order by q.option_type, q.bid nulls first, q.ask nulls first) tok
  from q join want w on w.poll_ts = q.poll_ts and w.expiry = q.expiry and w.k = q.strike
  group by 1, 2, 3),
emit as (select distinct poll_ts, d from sel where d between :A::date and :B::date),
lines as (
  select 'S|' || session_date line from (select distinct session_date from ml_brain_snapshots
                                         where session_date between :A::date and :B::date) s
  union all
  select 'W|' || pw.d || '|' || pw.slot || '|any|' || to_char(pw.any_poll at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
         || '|' || (select count(*) from ml_option_chain_snapshots c where c.poll_ts = pw.any_poll and c.index_key = 'NF')
  from pw where pw.d between :A::date and :B::date and pw.any_poll is not null
  union all
  select 'W|' || pw.d || '|' || pw.slot || '|nf|' || to_char(pw.nf_poll at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
         || '|' || (select count(*) from ml_option_chain_snapshots c where c.poll_ts = pw.nf_poll and c.index_key = 'NF')
  from pw where pw.d between :A::date and :B::date and pw.nf_poll is not null
  union all
  select 'W|' || lp.d || '|L|nf|' || coalesce(to_char(lp.last_nf at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), '') || '|'
  from lp
  union all
  select 'A|' || to_char(a.poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') || '|' || a.expiry || '|' || a.k0
         || '|' || round(a.spot::numeric, 6) || '|' || round(a.straddle::numeric, 6) || '|' || a.tied || '|' || a.npairs
  from atm_v a where a.poll_ts in (select poll_ts from emit)
  union all
  select 'R|' || to_char(r.poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') || '|' || r.expiry || '|'
         || string_agg(r.strike || '/' || r.tok, ';' order by r.strike)
  from rtok r where r.poll_ts in (select poll_ts from emit)
  group by r.poll_ts, r.expiry
  union all
  select 'D|' || to_char(x.poll_ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') || '|' || count(*) || '|'
         || count(distinct (c.strike, c.option_type)) || '|'
         || count(distinct c.expiry) filter (where c.expiry in (select vd.s from vd)) || '|'
         || count(*) filter (where c.expiry is null or c.expiry not in (select vd.s from vd)) || '|'
         || coalesce(string_agg(distinct c.expiry, ',' order by c.expiry) filter (where c.expiry in (select vd.s from vd)), '')
  from emit x join ml_option_chain_snapshots c on c.poll_ts = x.poll_ts and c.index_key = 'NF'
  group by x.poll_ts),
body as (select string_agg(line, E'\n' order by line) b, count(*) n,
                count(*) filter (where line like 'S|%') ns, count(*) filter (where line like 'W|%') nw,
                count(*) filter (where line like 'A|%') na, count(*) filter (where line like 'R|%') nr,
                count(*) filter (where line like 'D|%') nd
         from lines)
select n, md5(b) md5, ns, nw, na, nr, nd,
       round(extract(epoch from clock_timestamp() - statement_timestamp())::numeric, 3) secs, b from body;
commit;
