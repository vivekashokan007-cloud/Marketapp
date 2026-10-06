"""Run the real research SQL against a throwaway local Postgres loaded with synthetic fixtures.

Skipped unless MR_SCRATCH_PG is set to a libpq DSN for a LOCAL scratch server (unix socket or localhost)
whose database name contains 'scratch' or 'test'. It refuses anything else, so it can never touch
production. It proves, on data with known answers:

* the extract SQL (sqlkit.render, the exact text used after hours) reproduces the fixture dataset well
  enough that Stage B gives identical ledgers from the SQL extract and from the fixture's own rendering;
* run_legacy_sql_v0 agrees per day with the executed sweep SQL (stage_a_sql_crosscheck.sql), including
  a missing close, a BNF-only first poll and an interleaved two-expiry poll;
* the fill-reconciliation and payload queries execute and classify as specified.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import replay_engine as re_  # noqa: E402
import run_stage_a_b as runner  # noqa: E402
import sqlkit  # noqa: E402
import test_replay_engine as tre  # noqa: E402

DSN = os.environ.get('MR_SCRATCH_PG')
EVIDENCE = os.path.normpath(os.path.join(HERE, '..', '..', 'evidence'))
EQ_VERSIONS = os.path.join(EVIDENCE, 'eq_rules_v2_versions.sql')
EQ_COUNTS = os.path.join(EVIDENCE, 'eq_rules_v2_counts.sql')
LOT_SQL = os.path.join(EVIDENCE, 'lot_captured_vs_authority.sql')

SCHEMA = """
drop table if exists ml_option_chain_snapshots, ml_brain_snapshots, trades_v2;
create table ml_option_chain_snapshots (id bigserial primary key, poll_ts timestamptz, session_date date,
  index_key text, expiry text, strike integer, option_type text, ltp double precision,
  bid double precision, ask double precision);
create unique index ml_ocs_unique on ml_option_chain_snapshots (poll_ts, index_key, strike, option_type);
create table ml_brain_snapshots (id bigserial primary key, poll_ts timestamptz, session_date date,
  recommendation_id text, primary_candidate_json jsonb, top_candidates_json jsonb, context_json jsonb);
create index idx_ml_brain_snapshots_session_date on ml_brain_snapshots (session_date);
create table trades_v2 (id bigserial primary key, index_key text, expiry date, entry_date timestamptz,
  paper boolean, execution_mode text, strategy_type text, entry_vix numeric, entry_snapshot jsonb,
  friction_breakdown_json jsonb, lots integer,
  sell_strike numeric, sell_type text, sell_ltp numeric, buy_strike numeric, buy_type text, buy_ltp numeric,
  sell_strike2 numeric, sell_type2 text, sell_ltp2 numeric, buy_strike2 numeric, buy_type2 text, buy_ltp2 numeric);
"""


def _safe_dsn(dsn):
    import psycopg2.extensions as ext
    params = ext.parse_dsn(dsn)
    host = params.get('host', '')
    db = params.get('dbname', '')
    if not (host.startswith('/') or host in ('localhost', '127.0.0.1')):
        raise unittest.SkipTest(f'refusing non-local host {host!r}')
    if 'scratch' not in db and 'test' not in db:
        raise unittest.SkipTest(f'refusing database {db!r}: name must contain scratch or test')
    return dsn


@unittest.skipUnless(DSN, 'MR_SCRATCH_PG not set (local scratch Postgres only)')
class ScratchPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.conn = psycopg2.connect(_safe_dsn(DSN))
        cls.conn.autocommit = True
        cls.sessions = tre._sessions(date(2026, 8, 20), date(2026, 9, 25))
        cls.spots = tre._wiggle(cls.sessions)
        # A premium scale that puts several sessions inside the legacy v1.3 band.
        le = tre.LegacyEmulationTests()
        le.sessions, le.spots = cls.sessions, cls.spots
        cls.fx, _ = le._force_in_band()
        fx = cls.fx
        # Edge cases the SQL must handle exactly like the engines.
        fx.drop_poll(date(2026, 9, 7), 'C')                                   # missing close
        fx.any_override[(date(2026, 9, 9), 'E')] = tre._ts(date(2026, 9, 9), 12, 30)   # BNF-only first poll
        fx.overwrite_with_expiry(date(2026, 9, 16), 'E', '2026-09-29', [(23300, 'CE'), (23300, 'PE')],
                                 spot=cls.spots[date(2026, 9, 16)])          # interleaved two-expiry poll
        cls.load(fx)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    @classmethod
    def load(cls, fx):
        cur = cls.conn.cursor()
        cur.execute(SCHEMA)
        for d in fx.observed:
            cur.execute('insert into ml_brain_snapshots (poll_ts, session_date, context_json, top_candidates_json) '
                        "values (%s, %s, %s, '[]')",
                        (datetime.combine(d, time(9, 20), re_.IST), d,
                         json.dumps({'executionMode': 'paper', 'snapshot_generated_candidates': [{}, {}],
                                     'snapshot_brain_version': '2.6.66' if d >= date(2026, 9, 21) else '2.6.65',
                                     'morningBias': {'bias': 'NEUTRAL', 'signals': [
                                         {'name': 'FII Short%', 'value': '88% (prev: N/A)', 'dir': 'BEAR'}]}})))
        for (d, slot), p in fx.polls.items():
            for expiry, rows in list(p['chains'].items()) + list(fx.hidden.get((d, slot), {}).items()):
                for (k, opt), qs in rows.items():
                    for bid, ask in qs:
                        cur.execute('insert into ml_option_chain_snapshots (poll_ts, session_date, index_key, expiry, '
                                    'strike, option_type, bid, ask) values (%s,%s,%s,%s,%s,%s,%s,%s)',
                                    (p['ts'], d, 'NF', '' if expiry == 'X' else expiry, k, opt, bid, ask))
        for (d, slot), ts in fx.any_override.items():
            cur.execute("insert into ml_option_chain_snapshots (poll_ts, session_date, index_key, expiry, strike, "
                        "option_type, bid, ask) values (%s, %s, 'BNF', '2026-09-29', 50000, 'CE', 10, 11)", (ts, d))

    def _query(self, sql):
        cur = self.conn.cursor()
        cur.execute(sql)
        return cur.fetchall() if cur.description else None

    def _extract(self, out_dir, manifest):
        frozen = []
        for part, a, b in manifest:
            rows = self._query(sqlkit.select_body(sqlkit.EXTRACT_SQL, A=a, B=b))
            n, md5, ns, nw, na, nr, nd, secs, body = rows[0]
            frozen.append(sqlkit.write_part(out_dir, part, a, b, n, md5, body or ''))
        return frozen

    def test_guarded_forms_execute_read_only(self):
        cur = self.conn.cursor()
        cur.execute(sqlkit.render(sqlkit.EXTRACT_SQL, A='2026-09-01', B='2026-09-15'))
        cur.execute(sqlkit.render_single_statement(sqlkit.EXTRACT_SQL, A='2026-09-01', B='2026-09-15'))
        self.assertEqual(cur.fetchone()[0] > 0, True)
        cur.execute('show default_transaction_read_only')
        self.assertEqual(cur.fetchone()[0], 'on')
        cur.execute('reset all')

    def test_extract_then_stage_b_matches_fixture_rendering(self):
        tmp = tempfile.mkdtemp()
        try:
            manifest = [('01', '2026-08-20', '2026-09-07'), ('02', '2026-09-08', '2026-09-25')]
            frozen = self._extract(tmp, manifest)
            self.assertEqual([f['part'] for f in frozen], ['01', '02'])
            saved = (runner.CALENDAR_START, runner.CUTOFF)
            runner.CALENDAR_START, runner.CUTOFF = date(2026, 8, 20), date(2026, 9, 25)
            try:
                ds_sql, report = runner.load_parts(tmp, manifest)
            finally:
                runner.CALENDAR_START, runner.CUTOFF = saved
            ds_fx = self.fx.dataset()
            b_sql = re_.run_corrected(ds_sql, tre.OPEN_BAND, date(2026, 9, 1), date(2026, 9, 18), date(2026, 9, 25),
                                      lot_resolver=tre._lot65)
            b_fx = re_.run_corrected(ds_fx, tre.OPEN_BAND, date(2026, 9, 1), date(2026, 9, 18), date(2026, 9, 25),
                                     lot_resolver=tre._lot65)
            keys = ('status', 'reason', 'outcome_status', 'k0', 'expiry', 'ivrv', 'net_rs', 'planned_exit_date')
            proj = lambda out: {r['session']: {k: r.get(k) for k in keys} for r in out['ledger']}  # noqa: E731
            self.assertEqual(proj(b_sql), proj(b_fx))
            self.assertEqual(b_sql['summary']['sequential'], b_fx['summary']['sequential'])
            rows = {r['session']: r for r in b_sql['ledger']}
            self.assertEqual(rows['2026-09-16']['status'], 'entry_poll_quarantined')
            self.assertEqual(rows['2026-09-16']['reason'], 'poll_mixed_expiries')
            self.assertEqual(rows['2026-09-03']['outcome_status'], 'missing_close')
        finally:
            shutil.rmtree(tmp)

    def test_stage_a_emulation_matches_the_executed_sweep_sql(self):
        tmp = tempfile.mkdtemp()
        try:
            manifest = [('01', '2026-08-20', '2026-09-07'), ('02', '2026-09-08', '2026-09-25')]
            self._extract(tmp, manifest)
            saved = (runner.CALENDAR_START, runner.CUTOFF)
            runner.CALENDAR_START, runner.CUTOFF = date(2026, 8, 20), date(2026, 9, 25)
            try:
                ds, _ = runner.load_parts(tmp, manifest)
            finally:
                runner.CALENDAR_START, runner.CUTOFF = saved
            a = re_.run_legacy_sql_v0(ds, tre.LEGACY_RULE, date(2026, 8, 27), date(2026, 9, 25), date(2026, 9, 25),
                                      calendar_start=date(2026, 8, 20))
            rows = self._query(sqlkit.select_body(sqlkit.CROSSCHECK_SQL, CAL_START='2026-08-20',
                                                  WIN_START='2026-08-27', CUTOFF='2026-09-25'))
            text = rows[0][0] or ''
            sql_days = {}
            for line in text.split('\n'):
                if not line:
                    continue
                d, feat_rows, out_rows, net, is_exp, c2 = line.split('|')
                sql_days[d] = {'out_rows': int(out_rows), 'net': float(net) if net else None, 'c2_days': int(c2)}
            led = {r['session']: r for r in a['ledger']}
            eng_cell = {s: r for s, r in led.items()
                        if r['status'] in ('eligible', 'horizon_beyond_expiry', 'horizon_beyond_calendar')}
            self.assertTrue(sql_days, 'fixture must produce cell days')
            self.assertEqual(sorted(sql_days), sorted(eng_cell))
            for d, srow in sql_days.items():
                r = eng_cell[d]
                if srow['out_rows']:
                    self.assertEqual(r['outcome_status'], 'resolved', d)
                    self.assertAlmostEqual(r['net_rs'], srow['net'], places=2, msg=d)
                    self.assertEqual(r['out_rows'], srow['out_rows'], d)
                else:
                    self.assertNotEqual(r.get('outcome_status'), 'resolved', d)
            # The 7 Sep close is missing: any cell day exiting on 7 Sep has no SQL out row and is missing_close.
            for d, r in led.items():
                if r.get('planned_exit_date') == '2026-09-07':
                    self.assertEqual(r['outcome_status'], 'missing_close')
                    self.assertEqual(sql_days[d]['out_rows'], 0)
        finally:
            shutil.rmtree(tmp)

    def test_fill_reconciliation_runs_and_classifies(self):
        cur = self.conn.cursor()
        d = date(2026, 9, 3)
        p = self.fx.polls[(d, 'E')]
        rows = p['chains']['2026-09-08']
        k0 = 23000
        while (k0, 'CE') not in rows:
            k0 += 50
        bid_ce, ask_ce = rows[(k0, 'CE')][0]
        bid_pe, ask_pe = rows[(k0 - 400, 'PE')][0]
        entry = p['ts'] + timedelta(seconds=20)
        cur.execute('delete from trades_v2')
        # Trade 1: sells at the bid, buys at the ask (executable). Trade 2: sells at the ask (optimistic),
        # unknown index spelling, CALL/PUT spelling, wrong expiry.
        cur.execute("insert into trades_v2 (index_key, expiry, entry_date, paper, execution_mode, strategy_type, "
                    "sell_strike, sell_type, sell_ltp, buy_strike, buy_type, buy_ltp) values "
                    "('NIFTY', '2026-09-08', %s, true, 'paper', 'BULL_PUT', %s, 'CALL', %s, %s, 'PUT', %s)",
                    (entry, k0, bid_ce, k0 - 400, ask_pe))
        cur.execute("insert into trades_v2 (index_key, expiry, entry_date, paper, execution_mode, strategy_type, "
                    "sell_strike, sell_type, sell_ltp) values ('NF', '2026-09-08', %s, true, null, 'BEAR_CALL', %s, 'CE', %s)",
                    (entry, k0, ask_ce))
        cur.execute("insert into trades_v2 (index_key, expiry, entry_date, paper, execution_mode, strategy_type, "
                    "sell_strike, sell_type, sell_ltp) values ('FINNIFTY', '2026-09-08', %s, true, 'paper', 'X', 1, 'CE', 1)",
                    (entry,))
        cur.execute("insert into trades_v2 (index_key, expiry, entry_date, paper, execution_mode, strategy_type, "
                    "sell_strike, sell_type, sell_ltp) values ('NF', '2026-09-15', %s, true, 'paper', 'Y', %s, 'CE', 1)",
                    (entry, k0))
        res = self._query(sqlkit.select_body(sqlkit.FILL_SQL, FROM='2026-09-01', TO='2026-09-30'))[0]
        names = [c.name for c in cur.description] if cur.description else None
        trades, legs, matched, unmatched, by_side, by_mode = res[0], res[1], res[2], res[3], res[4], res[5]
        self.assertEqual((trades, legs, matched, unmatched), (4, 5, 3, 2))
        self.assertEqual(by_side['sell:at_executable'], 1)
        self.assertEqual(by_side['buy:at_executable'], 1)
        self.assertEqual(by_side['sell:at_opposite_side_optimistic'], 1)
        self.assertEqual(by_side['sell:unmatched_unknown_index'], 1)
        self.assertEqual(by_side['sell:unmatched_expiry_mismatch'], 1)
        self.assertIn('null|paper=true:at_opposite_side_optimistic', by_mode)
        del names

    def test_fill_reconciliation_survives_impossible_chain_expiry(self):
        cur = self.conn.cursor()
        d = date(2026, 9, 3)
        ts = datetime.combine(d, time(13, 0), re_.IST)
        cur.execute("insert into ml_option_chain_snapshots (poll_ts, session_date, index_key, expiry, strike, option_type, "
                    "bid, ask) values (%s, %s, 'NF', '2026-99-99', 23000, 'CE', 10, 11)", (ts, d))
        cur.execute('delete from trades_v2')
        cur.execute("insert into trades_v2 (index_key, expiry, entry_date, paper, execution_mode, strategy_type, "
                    "sell_strike, sell_type, sell_ltp) values ('NF', '2026-09-08', %s, true, 'paper', 'Z', 23000, 'CE', 10)",
                    (ts + timedelta(seconds=5),))
        try:
            res = self._query(sqlkit.select_body(sqlkit.FILL_SQL, FROM='2026-09-01', TO='2026-09-30'))[0]
            self.assertEqual(res[4], {'sell:unmatched_chain_expiry_not_a_real_date': 1})
        finally:
            cur.execute("delete from ml_option_chain_snapshots where expiry = '2026-99-99'")

    def test_extract_maps_impossible_expiry_to_x_and_engine_quarantines(self):
        cur = self.conn.cursor()
        d = date(2026, 9, 10)
        ts = self.fx.polls[(d, 'E')]['ts']
        cur.execute("insert into ml_option_chain_snapshots (poll_ts, session_date, index_key, expiry, strike, option_type, "
                    "bid, ask) values (%s, %s, 'NF', '2026-02-30', 99950, 'CE', 1, 2)", (ts, d))
        try:
            rows = self._query(sqlkit.select_body(sqlkit.EXTRACT_SQL, A='2026-09-10', B='2026-09-10'))
            body = rows[0][8]
            ds = re_.parse_extract(body)
            p = ds.polls[ts.astimezone(re_.timezone.utc)]
            self.assertEqual(p.integrity[3], 1)                       # counted as a non-date row
            self.assertEqual(re_.poll_integrity_problem(p), 'poll_non_date_expiry_rows')
        finally:
            cur.execute("delete from ml_option_chain_snapshots where expiry = '2026-02-30'")

    def test_eq_reader_and_lot_crosscheck_execute(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'evidence'))
        import eq_lot_report  # noqa: E402
        cur = self.conn.cursor()
        cur.execute('delete from trades_v2')
        ts = datetime(2026, 9, 3, 13, 0, tzinfo=re_.IST)
        cur.execute("insert into trades_v2 (index_key, expiry, entry_date, paper, execution_mode, entry_vix, lots, "
                    "entry_snapshot, friction_breakdown_json) values "
                    "('NF', '2026-09-08', %s, true, 'paper', 11.0, 1, %s, %s), "
                    "('NF', '2026-09-08', %s, false, 'paper', 11.0, 1, %s, %s), "
                    "('NF', '2026-09-22', %s, true, 'paper', 12.0, 1, %s, %s)",
                    (ts, json.dumps({'vix_direction': round(11.0 - 13.61, 2), 'fii_deriv_net': 0,
                                     'contract_lot_size': 65}), json.dumps({'lot_size': 65}),
                     ts, json.dumps({'vix_direction': 0.5, 'fii_deriv_net': 1200, 'contract_lot_size': 75}), '{}',
                     datetime(2026, 9, 22, 13, 0, tzinfo=re_.IST), json.dumps({'vix_direction': 0.2}), '{}'))
        versions = self._query(sqlkit.select_body(EQ_VERSIONS, FROM='2026-08-20'))[0]
        cols = ('first_snapshot_ge_2_6_66', 'last_session_checked', 'snapshots_checked', 'per_session')
        versions = dict(zip(cols, versions))
        versions['last_session_checked'] = str(versions['last_session_checked'])
        b = eq_lot_report.boundary_from_versions(versions)
        self.assertEqual((b['status'], b['boundary_session']), ('observed', '2026-09-21'))
        counts = self._query(sqlkit.select_body(EQ_COUNTS, BOUNDARY=b['boundary_session']))[0]
        eq1, eq2, eq3, eq6, eq8 = counts
        self.assertEqual((eq1['in_window_trades'], eq1['in_window_signature_entry_vix_minus_13_61']), (2, 1))
        self.assertEqual(eq1['outside_window_trades'], 1)
        self.assertEqual((eq2['in_window_zero_treated_missing'], eq2['in_window_nonzero']), (1, 1))
        self.assertEqual(eq3['excluded_prev_na_level_only'], eq3['sessions'])
        self.assertEqual(eq8['paper_false_with_mode_paper'], 1)
        lots = self._query(sqlkit.select_body(LOT_SQL, FROM='2026-08-01'))[0][0]
        rep = eq_lot_report.lot_report(lots)
        self.assertEqual((rep['trades_agree'], rep['trades_disagree'], rep['trades_captured_missing']), (1, 1, 1))

    def test_payload_query_runs(self):
        res = self._query(sqlkit.select_body(sqlkit.PAYLOAD_SQL, D='2026-09-03'))[0]
        self.assertEqual(res[0], 1)        # one snapshot that day in the fixture


if __name__ == '__main__':
    unittest.main()
