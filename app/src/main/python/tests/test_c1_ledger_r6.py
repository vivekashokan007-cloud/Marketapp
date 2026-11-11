"""C1 R6 ledger regression: 24 KiB ledger at the supported 48 identities, export-boundary expansion, focused set.

R5 stress (S4/S6/S7, 48 BNF identities) overflowed the 24 KiB ledger at the 15:05 exit record, leaving exit
verdicts REQUESTED.  R6 writes a lossless compact ledger and admits an identity / entry fetch only while the
worst-case final ledger fits; otherwise a deterministic refusal with a reason, counted per side.  Traces are the
SANITIZED 11 Nov fixture and synthetic stress days built from it.  No network, no device data, no Paper positions."""
import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import c1_capture_pilot_v1 as c1
import test_c1_bytecap_r5 as r5t

HERE = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.dirname(HERE)
TR = r5t.TR
S = r5t.S
R4FX = json.load(open(os.path.join(HERE, 'fixtures', 'c1_r4_rows_11nov_sanitized.json')))
SEQ = {'S2': 'B' * 30 + 'S' * 4, 'S3': 'S' * 20 + 'B' * 10, 'S4': ('BBS' * 16)[:48],
       'S6': 'B' * 24 + 'S' * 24, 'S7': 'S' * 24 + 'B' * 24}
# R6 per-side results (BNF; registered, entry retained, resolved) -- deterministic; R5 in brackets
EXPECT = {'S2': {'BUY': (30, 18, 18), 'SELL': (4, 2, 2)},      # R5 BUY 18, SELL 2
          'S3': {'BUY': (10, 3, 3), 'SELL': (20, 14, 14)},     # R5 BUY 3, SELL 14
          'S4': {'BUY': (32, 13, 13), 'SELL': (16, 6, 6)},     # R5 resolved BUY 7, SELL 4 (ledger overflow)
          'S6': {'BUY': (24, 18, 18), 'SELL': (24, 2, 2)},     # R5 resolved BUY 10, SELL 2
          'S7': {'BUY': (24, 3, 3), 'SELL': (24, 14, 14)}}     # R5 resolved BUY 0, SELL 0


def walk_bytes(d):
    return sum(os.stat(os.path.join(r, n)).st_size for r, _, ns in os.walk(d) for n in ns)


class Base(unittest.TestCase):
    def setUp(self):
        importlib.reload(c1)
        self.files = tempfile.mkdtemp()
        self.d = os.path.join(self.files, c1.DIR_NAME)
        self.sl = os.path.join(self.files, 'single_leg_v1')
        self.writes = []
        real = c1._write_ledger

        def wl(d, lg, fallback=None):                         # every ledger write: on-disk bytes + losslessness
            raw = c1._ledger_raw(lg)
            self.assertEqual(c1.expand_ledger(json.loads(raw)), json.loads(c1._canon(lg)))
            r = real(d, lg, fallback)
            self.writes.append((len(raw), c1._ledger_worst(lg), len(lg['ids']),
                                sum(len(f['pids']) for f in lg['fetches'].values() if f['p'] == 'ENTRY'), r[0]))
            return r
        self.p = mock.patch.object(c1, '_write_ledger', wl)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.files, ignore_errors=True)
        importlib.reload(c1)

    def lg(self, ix='BNF'):
        return c1._read_ledger(self.d, S, ix)

    def run_seq(self, name):
        return r5t.replay(r5t.stress(SEQ[name]), self.d)

    def assert_no_ledger_loss(self, ix='BNF'):
        lg = self.lg(ix)
        self.assertEqual(lg['stats']['ledger_write_failed'], 0)
        self.assertNotIn('BUDGET_EXHAUSTED:LEDGER_BYTES', lg['budget']['exhausted'])
        for r in lg['ids'].values():                            # every registered identity has terminal outcomes
            self.assertNotIn(r['o'], (None, 'REQUESTED'))
            self.assertNotIn(r['xo'], (None, 'REQUESTED'))
            if r['s'] == 'SELL' and r['o'] == 'VALID':
                self.assertNotIn(r['mo'], (None, 'REQUESTED'))
        self.assertTrue(all(f['state'] in ('RECORDED', 'RECORDED_CAPPED', 'LOST') for f in lg['fetches'].values()))
        self.assertLessEqual(os.path.getsize(c1._lg_path(self.d, S, ix)), c1._ledger_writable())

    def assert_bound_held(self):
        """Within one admission epoch (same identities + entry-fetch pids) no on-disk write exceeds the worst
        case computed at an earlier write, and no worst case ever exceeds the writable ledger bytes."""
        w = self.writes
        for i, (_, worst, n, e, _) in enumerate(w):
            self.assertLessEqual(worst, c1._ledger_writable())
            for raw, _, n2, e2, _ in w[i:]:
                if (n2, e2) == (n, e):
                    self.assertLessEqual(raw, worst)


class PublicApiLedgerGuards(unittest.TestCase):
    """Public API only (plan/record/read/scorecard): runs unchanged on R5 and fails there for the real reason
    (ledger write failure at the 15:05 record, exit verdicts left REQUESTED)."""
    def setUp(self):
        importlib.reload(c1)
        self.files = tempfile.mkdtemp()
        self.d = os.path.join(self.files, c1.DIR_NAME)

    def tearDown(self):
        shutil.rmtree(self.files, ignore_errors=True)

    def _guard(self, name):
        r5t.replay(r5t.stress(SEQ[name]), self.d)
        lg = c1._read_ledger(self.d, S, 'BNF')
        self.assertEqual(lg['stats']['ledger_write_failed'], 0, 'ledger write failed')
        self.assertNotIn('BUDGET_EXHAUSTED:LEDGER_BYTES', lg['budget']['exhausted'])
        left = sum(1 for r in lg['ids'].values() if r['xo'] in (None, 'REQUESTED'))
        self.assertEqual(left, 0, '%d exit verdicts left REQUESTED' % left)
        cs = r5t.cells(self.d)
        for side, (reg, ent, res) in EXPECT[name].items():
            self.assertEqual(cs[('BNF', side)]['resolved_entries'], res, side)

    def test_public_s4(self):
        self._guard('S4')

    def test_public_s6(self):
        self._guard('S6')

    def test_public_s7(self):
        self._guard('S7')

    def test_public_11nov_fixture_unchanged_from_r5(self):
        r5t.replay(TR, self.d)
        cs = r5t.cells(self.d)
        self.assertEqual([cs[k]['resolved_entries'] for k in (('BNF', 'BUY'), ('BNF', 'SELL'), ('NF', 'SELL'))], [18, 1, 6])


class Ledger48Bound(Base):
    def test_worst_case_48_identities_fits_the_unchanged_24kib_ledger(self):
        sys.path.insert(0, os.path.join(PY_DIR, '..', '..', '..', '..', 'tools'))
        lg = c1._empty_ledger(S, 'BNF')
        now = datetime.fromisoformat(S + 'T15:04:59.999999+05:30')
        for i in range(c1.MAX_IDS_PER_INDEX_SESSION):            # adversarial: all SELL, max field lengths
            k, dec = 'NSE_FO|' + str(100000 + i % 8), 'idx1_%020x' % (0xfffff00000000000000 + i)
            pid = c1.pid_for(S, 'BNF', dec, k)
            ts = S + 'T15:04:59.999999+05:30'
            lg['ids'][pid] = {'pid': pid, 'd': dec, 'k': k, 'a': 'SELL_PE', 's': 'SELL', 'lot': 999, 'an': ts,
                              'pub': ts, 'o': None, 'mo': None, 'xo': None}
        pids = list(lg['ids'])
        for pid in pids[:11]:                                    # 44 requests / 4 per SELL fetch
            r = lg['ids'][pid]
            c1._new_fetch(lg, 'ENTRY', [pid], [r['k']], c1._margin_default(lg['ids'], {'pids': [pid], 'keys': [r['k']]}), now)
            r['o'] = 'REQUESTED'
        ents = list(lg['fetches'].values())
        for j, pid in enumerate(pids[11:]):                      # every other identity coalesced
            ents[j % 11]['pids'].append(pid)
            lg['ids'][pid]['o'] = 'REQUESTED'
        worst = c1._ledger_worst(lg)
        self.assertLessEqual(worst, c1._ledger_writable())       # measured 22,218 of 23,552 (R6)
        self.assertLess(len(c1._ledger_raw(lg)), worst)

    def test_compact_ledger_round_trip_is_exact_and_r5_ledgers_still_read(self):
        self.run_seq('S6')
        lg = self.lg()
        disk = json.load(open(c1._lg_path(self.d, S, 'BNF')))
        self.assertEqual(disk['ledger_layout'], c1.LEDGER_LAYOUT)
        self.assertEqual(c1.expand_ledger(disk), lg)
        r5_shape = json.loads(c1._canon(lg))                     # an R5 (uncompacted) ledger file reads unchanged
        self.assertEqual(c1.expand_ledger(r5_shape), r5_shape)
        self.assertLess(len(c1._canon(disk)), 0.62 * len(c1._canon(lg)))

    def test_ledger_reason_bounded_and_deterministic(self):
        self.assertEqual(c1._vd('X' * 48), 'X' * 48)
        v = c1._vd('MARGIN_FETCH_FAILED:' + 'y' * 80)
        self.assertEqual(len(v), 48)
        self.assertEqual(v, c1._vd('MARGIN_FETCH_FAILED:' + 'y' * 80))


class StressNoLedgerLoss(Base):
    def _check(self, name):
        self.run_seq(name)
        self.assert_no_ledger_loss()
        self.assert_bound_held()
        cs = r5t.cells(self.d)
        for side, (reg, ent, res) in EXPECT[name].items():
            c = cs[('BNF', side)]
            self.assertEqual((c['eligible_proposals'], c['valid_entry_snapshots'], c['resolved_entries']), (reg, ent, res), side)
            self.assertEqual(c['exclusions'].get('EXIT:REQUESTED', 0), 0)
            self.assertEqual(c['identities_not_registered'], 0)
            self.assertEqual(c['hypothetical_exit_valid_verdicts'], reg)      # every exit verdict kept in the ledger
        return cs

    def test_s4_48_alternating(self):
        self._check('S4')
        self.assertEqual(len(self.lg()['ids']), 48)

    def test_s6_24buy_then_24sell(self):
        self._check('S6')

    def test_s7_24sell_then_24buy(self):
        self._check('S7')

    def test_buy_first_pressure_keeps_sell_floor(self):
        cs = self._check('S2')
        self.assertGreaterEqual(cs[('BNF', 'SELL')]['resolved_entries'], 2)

    def test_sell_first_pressure_keeps_buy_floor(self):
        cs = self._check('S3')
        self.assertGreaterEqual(cs[('BNF', 'BUY')]['resolved_entries'], 3)


class Fixture11Nov(Base):
    def test_11nov_fixture_both_exit_batches_and_per_side_results(self):
        r5t.replay(TR, self.d)
        for ix in c1.INDICES:
            self.assert_no_ledger_loss(ix)
        self.assert_bound_held()
        cs = r5t.cells(self.d)
        got = {k: (c['eligible_proposals'], c['valid_entry_snapshots'], c['margin_complete_sells'],
                   c['hypothetical_exit_valid_window_300s'], c['resolved_entries']) for k, c in cs.items() if c['eligible_proposals']}
        self.assertEqual(got, {('BNF', 'BUY'): (18, 18, 0, 18, 18), ('BNF', 'SELL'): (1, 1, 1, 1, 1),
                               ('NF', 'SELL'): (6, 6, 6, 6, 6)})
        ex = [f for f in self.lg()['fetches'].values() if f['p'] == 'TIME_EXIT']
        self.assertEqual(sorted(f['state'] for f in ex), ['RECORDED', 'RECORDED'])
        self.assertTrue(all(f['evidence_stored'] for f in ex))


class PendingAndRetries(Base):
    def _seq(self):
        tr = r5t.stress('BBS')
        plans = [e for e in tr['events'] if e['op'] == 'plan' and e['results']]
        recs = [e for e in tr['events'] if e['op'] == 'record' and e['purpose'] == 'ENTRY']
        return tr, plans, recs

    def test_pending_coalesce_retry_duplicate_and_fenced_late_record(self):
        tr, plans, recs = self._seq()
        c1.plan(self.d, plans[0]['results'], plans[0]['t'], enabled=True, sl_dir=self.sl)
        f0 = next(iter(self.lg()['fetches']))
        # an identical proposal (same contract, action, lot) while the first fetch is pending -> coalesced
        dup = copy.deepcopy(plans[0]['results'][0])
        dup['single_leg_by_index']['BNF']['decision_id'] = dup['by_index']['BNF']['decision_id'] = 'idx1_%020x' % 0xdddd
        c1.plan(self.d, [dup], plans[0]['t'] + 1000, enabled=True, sl_dir=self.sl)
        lg = self.lg()
        self.assertEqual(len(lg['fetches']), 1)
        self.assertEqual(len(lg['fetches'][f0]['pids']), 2)
        self.assertEqual(lg['stats']['coalesced'], 1)
        call = copy.deepcopy(recs[0]['call'])
        call['attempts'] = 2                                     # one transport retry
        self.assertEqual(c1.record(self.d, f0, 'BNF', S, call)['status'], 'RECORDED')
        self.assertEqual(c1.record(self.d, f0, 'BNF', S, call)['status'], 'ALREADY_RECORDED')
        lg = self.lg()
        self.assertEqual((lg['fetches'][f0]['used'], lg['fetches'][f0]['used_basis']), (2, 'COUNTED'))
        # second proposal planned, never recorded: fenced LOST after the TTL, then its late record is refused
        c1.plan(self.d, plans[1]['results'], plans[1]['t'], enabled=True, sl_dir=self.sl)
        f1 = next(f for f in self.lg()['fetches'] if f != f0)
        c1.plan(self.d, [], plans[1]['t'] + (c1.IN_FLIGHT_TTL_S + 5) * 1000, enabled=True, sl_dir=self.sl)
        lg = self.lg()
        self.assertEqual(lg['fetches'][f1]['state'], 'LOST')
        self.assertEqual(lg['fetches'][f1]['used_basis'], 'UPPER_BOUND_UNKNOWN_AFTER_RESTART')
        r = c1.record(self.d, f1, 'BNF', S, copy.deepcopy(recs[1]['call']))
        self.assertEqual(r['reason'], 'FETCH_NOT_IN_FLIGHT:LOST')
        self.assertEqual(self.lg()['stats']['late_records_refused'], 1)
        b = lg['budget']
        self.assertEqual(b['requests_used'] + b['requests_used_upper_bound'],
                         sum(f['used'] for f in lg['fetches'].values()))
        self.assert_bound_held()


class DeterministicLedgerRefusal(Base):
    CAP = 12 * 1024                                              # force the reservation to bind

    def _run(self):
        with mock.patch.object(c1, 'MAX_LEDGER_BYTES_PER_INDEX_SESSION', self.CAP):
            self.run_seq('S6')
            return self.lg(), r5t.cells(self.d), open(c1._lg_path(self.d, S, 'BNF'), 'rb').read()

    def test_refusal_is_deterministic_counted_per_side_and_keeps_verdicts(self):
        lg, cs, raw = self._run()
        st = lg['stats']
        self.assertGreater(st['identities_refused_ledger_bytes'], 0)
        self.assertIn('BUDGET_EXHAUSTED:LEDGER_RESERVE:IDENTITY', lg['budget']['exhausted'])
        self.assertIn('IDENTITY_CAP', lg['completeness_flags'])
        self.assertEqual(st['identities_overflow_buy'] + st['identities_overflow_sell'], st['identities_overflow'])
        self.assertEqual(st['identities_overflow'], 48 - len(lg['ids']))
        for side in ('BUY', 'SELL'):                            # every exclusion accounted on its side
            c = cs[('BNF', side)]
            n = st['identities_overflow_' + side.lower()]
            self.assertEqual(c['identities_not_registered'], n)
            self.assertEqual(c['exclusions'].get('IDENTITY:NOT_REGISTERED', 0), n)
            if n:
                self.assertEqual(c['coverage_kind'], 'PARTIAL')
                self.assertIsNone(c['entry_target_met_80pct'])
        with mock.patch.object(c1, 'MAX_LEDGER_BYTES_PER_INDEX_SESSION', self.CAP):
            self.assert_no_ledger_loss()
            self.assert_bound_held()
        shutil.rmtree(self.files)                               # rerun from scratch: byte-identical ledger
        os.makedirs(self.files)
        self.writes = []
        _, _, raw2 = self._run()
        self.assertEqual(raw, raw2)

    def test_entry_fetch_refusal_reason_when_identity_fits_but_fetch_does_not(self):
        hit = None
        for cap in range(9 * 1024, 14 * 1024, 64):
            shutil.rmtree(self.files, ignore_errors=True)
            os.makedirs(self.files)
            with mock.patch.object(c1, 'MAX_LEDGER_BYTES_PER_INDEX_SESSION', cap):
                self.run_seq('S4')
                lg = self.lg()
            if any(r['o'] == 'CAPPED:LEDGER_BYTES' for r in lg['ids'].values()):
                hit = (cap, lg)
                break
        self.assertIsNotNone(hit)
        cap, lg = hit
        side = next(r['s'] for r in lg['ids'].values() if r['o'] == 'CAPPED:LEDGER_BYTES')
        self.assertIn('BUDGET_EXHAUSTED:LEDGER_RESERVE:' + side, lg['budget']['exhausted'])
        c = r5t.cells(self.d)[('BNF', side)]
        self.assertGreaterEqual(c['exclusions'].get('ENTRY:CAPPED:LEDGER_BYTES', 0), 1)
        self.assertEqual(c['entry_requested'] + sum(v for k, v in c['exclusions'].items()
                                                    if k in ('ENTRY:CAPPED:LEDGER_BYTES', 'ENTRY:CAPPED:BYTES',
                                                             'ENTRY:CAPPED:REQUESTS')), c['eligible_proposals'])


_KILL_CHILD = r'''
import json, os, signal, sys
py, tests, d, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path[:0] = [py, tests]
import c1_capture_pilot_v1 as c1, test_c1_bytecap_r5 as r5t, daily_review_v1 as dr
tr = r5t.stress(('BBS' * 16)[:48])
r5t.replay(dict(tr, events=tr['events'][:n]), d)
real = os.replace
def die(a, b):
    if b.endswith('.ledger.json'):
        os.kill(os.getpid(), signal.SIGKILL)                      # killed between temp flush and os.replace
    return real(a, b)
os.replace = die
r5t.replay(dict(tr, events=tr['events'][n:]), d)
'''


class KillRestartNearCap(Base):
    def test_kill_during_near_cap_ledger_write_then_restart_completes_without_loss(self):
        tr = r5t.stress(SEQ['S4'])
        n = next(i for i, e in enumerate(tr['events']) if e['op'] == 'record' and e['purpose'] == 'TIME_EXIT')
        os.makedirs(self.d, exist_ok=True)
        r = subprocess.run([sys.executable, '-c', _KILL_CHILD, PY_DIR, HERE, self.d, str(n)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, -9, r.stderr[-400:])
        tmp = [x for x in os.listdir(self.d) if x.startswith('.tmp_')]
        self.assertEqual(len(tmp), 1)                            # abandoned temp file: counted, not committed
        before = c1._read_ledger(self.d, S, 'BNF')
        self.assertEqual(c1.expand_ledger(json.load(open(c1._lg_path(self.d, S, 'BNF')))), before)
        self.assertGreater(os.path.getsize(c1._lg_path(self.d, S, 'BNF')), 12 * 1024)   # near-cap write
        self.assertLessEqual(walk_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        importlib.reload(c1)                                     # restart: fresh module state, same files
        self.p.stop()
        self.p = mock.patch.object(c1, '_write_ledger', c1._write_ledger)
        self.p.start()
        r5t.replay(dict(tr, events=tr['events'][n:]), self.d)   # the 15:05 records arrive again after restart
        self.assertEqual([x for x in os.listdir(self.d) if x.startswith('.tmp_')], [])
        self.assert_no_ledger_loss()
        self.assertEqual(len(c1._read_ledger(self.d, S, 'BNF')['ids']), 48)


class ThreeCapsAndAtomicReplacement(Base):
    def test_every_replace_keeps_store_within_global_cap_and_files_within_their_caps(self):
        os.makedirs(self.d, exist_ok=True)
        filler = os.path.join(self.d, '2026-11-02_NF.json')       # unrelated earlier session near the global cap
        with open(filler, 'w') as fh:
            fh.write('"' + 'x' * (c1.MAX_BYTES_GLOBAL - 70 * 1024 - 2) + '"')
        import daily_review_v1 as dr
        real, peaks = os.replace, []

        def rep(a, b):
            if os.path.dirname(b) == self.d:                     # old target + new temp both on disk here
                peaks.append(walk_bytes(self.d))
            return real(a, b)
        with mock.patch.object(dr.os, 'replace', rep):
            r5t.replay(TR, self.d)
        self.assertTrue(peaks)
        self.assertLessEqual(max(peaks), c1.MAX_BYTES_GLOBAL)
        self.assertLessEqual(walk_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        for ix in c1.INDICES:
            self.assertLessEqual(os.path.getsize(c1._lg_path(self.d, S, ix)), c1.MAX_LEDGER_BYTES_PER_INDEX_SESSION)
            if os.path.exists(c1._ev_path(self.d, S, ix)):
                self.assertLessEqual(os.path.getsize(c1._ev_path(self.d, S, ix)), c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION)
            self.assertEqual(self.lg(ix)['stats']['ledger_write_failed'], 0)   # ledgers keep priority
        self.assertFalse(os.path.exists(self.sl))                # C1 never touches single_leg_v1 (Paper untouched)

    def test_stress_files_stay_within_ledger_and_evidence_caps(self):
        self.run_seq('S7')
        self.assertLessEqual(os.path.getsize(c1._lg_path(self.d, S, 'BNF')), c1.MAX_LEDGER_BYTES_PER_INDEX_SESSION)
        self.assertLessEqual(os.path.getsize(c1._ev_path(self.d, S, 'BNF')), c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION)


class ExportBoundary(Base):
    def _export(self):
        part = c1.export_part(self.files, S, level='full')
        return json.loads(json.dumps(part, sort_keys=True, default=str))   # off-device round trip

    def test_export_rows_are_r4_shaped_and_equal_b528_rows(self):
        r5t.replay(TR, self.d)
        part = self._export()
        self.assertEqual(part['row_layout'], 'R4_ROW_SHAPE_EXPANDED_AT_EXPORT')
        for ix in c1.INDICES:
            ev, r4 = part['evidence'][ix], R4FX['evidence'][ix]
            for k in ('row_layout', 'row_defaults', 'row_join'):
                self.assertNotIn(k, ev)
            self.assertEqual(set(ev), set(r4) - {'revision'} | {'revision'})
            mine = {o['obs_id']: o for o in ev['observations']}
            # exit responses are re-chunked onto each build's own exit fetches, so fetch ids and receipt timing
            # differ by construction; key sets must be identical and contract/quote fields equal
            timing = ('obs_id', 'fetch_id', 'request_start_ms', 'receipt_ms', 'receipt_mono_ms', 'latency_ms',
                      'age_at_receipt_s', 'publication_to_capture_lag_s', 'delay_after_cutoff_s', 'prompt_90s')
            drop = lambda r: {k: (v if k not in timing else '<timing>') for k, v in r.items()}
            mine_exit = {o['instrument_key']: drop(o) for o in ev['observations'] if o['purpose'] == 'TIME_EXIT'}
            n = 0
            for o in r4['observations']:                         # every row b528 retained: identical field-for-field
                if o['purpose'] == 'TIME_EXIT':                  # exit fetch ids hash the (different) proposal set
                    self.assertEqual(mine_exit[o['instrument_key']], drop(o))
                else:
                    self.assertEqual(mine[o['obs_id']], o)
                n += 1
            self.assertEqual(n, len(r4['observations']))
            for pid, t in r4['targets'].items():
                self.assertEqual(ev['targets'][pid], t)
            a6 = {a['pid']: drop(a) for a in ev['exit_attribution']}
            for a in r4['exit_attribution']:
                self.assertEqual(a6[a['pid']], drop(a))
            r4keys = {}
            for j in c1.INDICES:
                for o in R4FX['evidence'][j]['observations']:
                    r4keys.setdefault((o['kind'], o['purpose']), set()).update(o)
            for o in ev['observations']:                         # same field set per row kind as b528 writes
                self.assertEqual(set(o), r4keys[(o['kind'], o['purpose'])])
            self.assertEqual(part['ledgers'][ix], c1._read_ledger(self.d, S, ix))   # ledgers exported in R5 shape

    def test_scorecard_identical_after_round_trip_export(self):
        r5t.replay(TR, self.d)
        part = self._export()
        docs = {ix: {'ledger': part['ledgers'][ix], 'evidence': part['evidence'][ix], 'marker': None,
                     'ledger_status': 'PRESENT', 'evidence_status': 'PRESENT'} for ix in c1.INDICES}
        self.assertEqual(json.loads(json.dumps(c1.scorecard({S: docs}, []), sort_keys=True, default=str)),
                         part['scorecard'])

    def test_valid_verdict_without_quote_detail_stays_an_explicit_gap(self):
        real = c1._write_evidence

        def refuse_exit(d, ev, *, exit_phase=False):
            if exit_phase:
                return False, 'BUDGET_EXHAUSTED:EVIDENCE_BYTES'
            return real(d, ev, exit_phase=exit_phase)
        with mock.patch.object(c1, '_write_evidence', refuse_exit):
            r5t.replay(TR, self.d)
        part = self._export()
        c = {(x['index'], x['side']): x for x in part['scorecard']}[('BNF', 'BUY')]
        self.assertEqual(c['hypothetical_exit_valid_verdicts'], 18)
        self.assertEqual(c['evidence_gaps'].get('EXIT_VALID_VERDICT_DETAIL_NOT_RETAINED'), 18)
        self.assertEqual(c['resolved_entries'], 0)
        self.assertEqual(c['coverage_kind'], 'PARTIAL')

    def test_expanded_export_within_bundle_cap(self):
        self.run_seq('S7')
        part = self._export()
        n = len(c1._canon(part).encode())
        self.assertLess(n, 160 * 1024)                           # one index at its caps, expanded
        self.assertLess(2 * n, 4 * 1024 * 1024)                  # session_evidence MAX_BUNDLE_BYTES = 4 MiB


if __name__ == '__main__':
    unittest.main()
