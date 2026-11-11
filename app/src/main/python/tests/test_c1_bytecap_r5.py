"""C1 R5 byte-cap regression (11 Nov 2026 b528 smoke: BNF hit BUDGET_EXHAUSTED:EVIDENCE_BYTES).

Fixture: offline replay trace of the device session, SANITIZED (synthetic ids/tokens/prices with the same JSON
length, so every write keeps its device byte size).  On b528 (R4 rows) this trace loses the 18th BNF BUY entry
(32 KiB entry ceiling), the only BNF SELL (sticky byte flag) and the second 15:05 exit write (40 KiB); R5 keeps all
of them inside the unchanged limits.  No network, no device evidence, no Paper positions."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import c1_capture_pilot_v1 as c1

HERE = os.path.dirname(os.path.abspath(__file__))
TR = json.load(open(os.path.join(HERE, 'fixtures', 'c1_capture_11nov_bytecap_sanitized.json')))
S = TR['session_date']


def replay(trace, d):
    """Drive plan()/record() in event order.  Entry responses are matched to the planned fetch that carries the
    proposal (by keys, or by pid for responses b528 never requested); a 15:05 response is re-chunked onto
    whichever exit fetch the build under test planned, from the same per-instrument quote rows."""
    pool, done, writes = {}, set(), []
    for e in trace['events']:
        if e['op'] == 'record' and e['purpose'] == 'TIME_EXIT':
            for row in json.loads(e['call']['quote']['body'])['data'].values():
                pool[(e['ix'], row['instrument_token'])] = row
    sl = os.path.join(os.path.dirname(d), 'single_leg_v1')
    for e in trace['events']:
        if e['op'] == 'plan':
            c1.plan(d, e['results'], e['t'], enabled=True, sl_dir=sl)
            continue
        lg = c1._read_ledger(d, S, e['ix'])
        req = {f: v for f, v in lg['fetches'].items() if v['state'] == 'REQUESTED' and v['p'] == e['purpose']}
        call = copy.deepcopy(e['call'])
        if e['purpose'] == 'TIME_EXIT':
            todo = sorted((f for f in req if f not in done), key=lambda f: req[f]['keys'])
            if not todo:
                continue
            fid = todo[0]
            call['requested'] = list(req[fid]['keys'])
            call['quote']['body'] = json.dumps({'status': 'success', 'data': {
                'NSE_FO:R%d' % i: pool[(e['ix'], k)] for i, k in enumerate(req[fid]['keys'])}})
            done.add(fid)
        elif e.get('counterfactual'):
            fids = [f for f, v in req.items() if e['pid'] in v['pids']]
            if not fids:
                continue
            fid = fids[0]
        else:
            fids = sorted((f for f, v in req.items() if v['keys'] == e['keys']), key=lambda f: req[f]['planned_at'])
            if not fids:
                continue
            fid = fids[0]
        writes.append(c1.record(d, fid, e['ix'], S, call)['status'])
    return writes


def cells(d):
    return {(c['index'], c['side']): c for c in c1.scorecard({S: c1.read_session(d, S)}, [])}


def _ms(dt):
    return int(round(dt.timestamp() * 1000))


def stress(seq):
    """Synthetic BNF day at 5-minute cadence from 09:20 ('B' = BUY_CE, 'S' = SELL_PE), 8 rotating contracts,
    built from the fixture's own (sanitized) BNF rows; both 15:05 exit responses cover every contract."""
    ev = TR['events']
    plans = [e for e in ev if e['op'] == 'plan' and e.get('ix') == 'BNF' and e.get('pid')]
    tb = next(e for e in plans if e['results'][0]['single_leg_by_index']['BNF']['action'].startswith('BUY'))
    ts = next(e for e in plans if e['results'][0]['single_leg_by_index']['BNF']['action'].startswith('SELL'))
    rec = [e for e in ev if e['op'] == 'record' and e['ix'] == 'BNF' and e['purpose'] == 'ENTRY']
    rq, rqs = rec[0]['call'], next(e for e in rec if e['call']['margins'])['call']
    ex = next(e for e in ev if e['op'] == 'record' and e['ix'] == 'BNF' and e['purpose'] == 'TIME_EXIT')
    exrow = list(json.loads(ex['call']['quote']['body'])['data'].values())[0]
    keys = ['NSE_FO|%05d' % (91000 + 11 * i) for i in range(8)]
    out, t0 = [], datetime.fromisoformat(S + 'T09:20:00+05:30')
    for i, side in enumerate(seq):
        poll = t0 + timedelta(minutes=5 * i)
        an, pub = poll + timedelta(seconds=8), poll + timedelta(seconds=23)
        res = copy.deepcopy((tb if side == 'B' else ts)['results'][0])
        p, k, dec = res['single_leg_by_index']['BNF'], keys[i % 8], 'idx1_%020x' % (0xabc000 + i)
        p.update(decision_id=dec, decision_poll_ts=poll.isoformat(), instrument_key=k, quote_needed=[k])
        if p.get('margin_needed'):
            p['margin_needed']['instrument_key'] = k
        res['by_index']['BNF'].update(decision_id=dec, analysis_ts=an.isoformat())
        res['published_ms'] = _ms(pub)
        pid = c1.pid_for(S, 'BNF', dec, k)
        out.append({'t': _ms(pub) + 2, 'op': 'plan', 'results': [res]})
        call = copy.deepcopy(rq if side == 'B' else rqs)
        row = list(json.loads(call['quote']['body'])['data'].values())[0]
        rc = _ms(pub) + 320
        row.update(instrument_token=k, timestamp=(pub + timedelta(milliseconds=250)).isoformat(timespec='milliseconds'))
        row['depth']['buy'][0]['quantity'] = row['depth']['sell'][0]['quantity'] = 600.0
        call['quote'].update(body=json.dumps({'status': 'success', 'data': {'NSE_FO:R0': row}}), receipt_ms=rc,
                             request_start_ms=rc - 90)
        call['requested'] = [k]
        for m in call['margins']:
            m.update(target_id=pid, receipt_ms=rc + 100, request=json.dumps({'instruments': [
                {'instrument_key': k, 'quantity': 30, 'product': 'I', 'transaction_type': 'SELL'}]}))
        out.append({'t': rc, 'op': 'record', 'ix': 'BNF', 'purpose': 'ENTRY', 'keys': [k], 'call': call,
                    'counterfactual': True, 'pid': pid})
    b = datetime.fromisoformat(S + 'T15:05:27.171+05:30')
    out.append({'t': _ms(b), 'op': 'plan', 'results': []})
    for off in (461, 603):
        call = copy.deepcopy(ex['call'])
        data = {}
        for i, k in enumerate(keys):
            r = dict(exrow, instrument_token=k, timestamp=(b + timedelta(milliseconds=583)).isoformat(timespec='milliseconds'))
            r['depth'] = {'buy': [{'price': exrow['depth']['buy'][0]['price'], 'quantity': 600.0}],
                          'sell': [{'price': exrow['depth']['sell'][0]['price'], 'quantity': 600.0}]}
            data['NSE_FO:R%d' % i] = r
        call['quote'].update(body=json.dumps({'status': 'success', 'data': data}), receipt_ms=_ms(b) + off,
                             request_start_ms=_ms(b) + off - 94)
        out.append({'t': _ms(b) + off, 'op': 'record', 'ix': 'BNF', 'purpose': 'TIME_EXIT', 'keys': keys[:4], 'call': call})
    return {'session_date': S, 'events': out}


class Base(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.mkdtemp()
        self.d = os.path.join(self.files, c1.DIR_NAME)

    def tearDown(self):
        shutil.rmtree(self.files, ignore_errors=True)


class Bytecap11Nov(Base):
    def test_11nov_session_keeps_every_bnf_entry_margin_and_both_exit_writes(self):
        replay(TR, self.d)
        c = cells(self.d)
        lg = c1._read_ledger(self.d, S, 'BNF')
        self.assertFalse([r for r in lg['budget']['exhausted'] if 'BYTES' in r], lg['budget']['exhausted'])
        # BNF BUY and BNF SELL, separately: every eligible proposal has retained entry (+ margin) + 15:05 exit
        self.assertEqual((c[('BNF', 'BUY')]['eligible_proposals'], c[('BNF', 'BUY')]['valid_entry_snapshots'],
                          c[('BNF', 'BUY')]['hypothetical_exit_valid_window_300s'], c[('BNF', 'BUY')]['resolved_entries']),
                         (18, 18, 18, 18))
        self.assertEqual((c[('BNF', 'SELL')]['eligible_proposals'], c[('BNF', 'SELL')]['valid_entry_snapshots'],
                          c[('BNF', 'SELL')]['margin_complete_sells'],
                          c[('BNF', 'SELL')]['hypothetical_exit_valid_window_300s']), (1, 1, 1, 1))
        self.assertEqual(c[('BNF', 'BUY')]['evidence_gaps'], {})
        self.assertEqual(c[('BNF', 'SELL')]['evidence_gaps'], {})
        # NF unchanged: 6 SELL, all retained
        self.assertEqual((c[('NF', 'SELL')]['valid_entry_snapshots'], c[('NF', 'SELL')]['margin_complete_sells'],
                          c[('NF', 'SELL')]['hypothetical_exit_valid_window_300s']), (6, 6, 6))
        self.assertEqual(c[('NF', 'BUY')]['eligible_proposals'], 0)
        # inside the unchanged limits
        for ix in c1.INDICES:
            self.assertLessEqual(os.path.getsize(c1._ev_path(self.d, S, ix)), c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION)
            self.assertLessEqual(os.path.getsize(c1._lg_path(self.d, S, ix)), c1.MAX_LEDGER_BYTES_PER_INDEX_SESSION)
        self.assertLessEqual(c1.store_bytes(self.d), c1.MAX_BYTES_GLOBAL)
        for k in ('MAX_EVIDENCE_BYTES_PER_INDEX_SESSION', 'EXIT_EVIDENCE_RESERVE_BYTES', 'MAX_BYTES_GLOBAL',
                  'MAX_LEDGER_BYTES_PER_INDEX_SESSION'):
            self.assertEqual(getattr(c1, k), {'MAX_EVIDENCE_BYTES_PER_INDEX_SESSION': 40960, 'EXIT_EVIDENCE_RESERVE_BYTES': 8192,
                                              'MAX_BYTES_GLOBAL': 524288, 'MAX_LEDGER_BYTES_PER_INDEX_SESSION': 24576}[k])

    def test_replay_is_deterministic(self):
        replay(TR, self.d)
        a = {n: open(os.path.join(self.d, n), 'rb').read() for n in sorted(os.listdir(self.d)) if n.endswith('.json')}
        d2 = os.path.join(tempfile.mkdtemp(), c1.DIR_NAME)
        try:
            replay(TR, d2)
            b = {n: open(os.path.join(d2, n), 'rb').read() for n in sorted(os.listdir(d2)) if n.endswith('.json')}
        finally:
            shutil.rmtree(os.path.dirname(d2), ignore_errors=True)
        self.assertEqual(a, b)


class BuyDoesNotCrowdOutSell(Base):
    def test_30_buy_then_4_sell(self):
        replay(stress('B' * 30 + 'S' * 4), self.d)
        c = cells(self.d)
        self.assertGreaterEqual(c[('BNF', 'SELL')]['resolved_entries'], 1)       # SELL keeps its floor
        for side in ('BUY', 'SELL'):                                             # every kept entry resolves
            self.assertEqual(c[('BNF', side)]['resolved_entries'], c[('BNF', side)]['valid_entry_snapshots'], side)

    def test_20_sell_then_10_buy(self):
        replay(stress('S' * 20 + 'B' * 10), self.d)
        c = cells(self.d)
        self.assertGreaterEqual(c[('BNF', 'BUY')]['resolved_entries'], 1)        # BUY keeps its floor
        for side in ('BUY', 'SELL'):
            self.assertEqual(c[('BNF', side)]['resolved_entries'], c[('BNF', side)]['valid_entry_snapshots'], side)


class BothExitFetchesFit(Base):
    def test_40_buy_every_kept_entry_keeps_its_1505_exit(self):
        replay(stress('B' * 40), self.d)
        c = cells(self.d)[('BNF', 'BUY')]
        self.assertGreater(c['valid_entry_snapshots'], 0)
        self.assertEqual(c['resolved_entries'], c['valid_entry_snapshots'])
        lg = c1._read_ledger(self.d, S, 'BNF')
        self.assertTrue(all(f['state'] == 'RECORDED' for f in lg['fetches'].values() if f['p'] == 'TIME_EXIT'))
        self.assertLessEqual(os.path.getsize(c1._ev_path(self.d, S, 'BNF')), c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION)


    def test_exit_overflow_falls_back_to_entry_linked_rows_deterministically(self):
        # under-reserve on purpose so the second 15:05 write overflows 40 KiB: R5 keeps the rows of proposals whose
        # entry evidence is retained (the only ones that can resolve) and counts the others
        with mock.patch.object(c1, 'RESERVE_EXIT_ATTR', 100):
            replay(stress('B' * 40), self.d)
        lg = c1._read_ledger(self.d, S, 'BNF')
        fx = [f for f in lg['fetches'].values() if f['p'] == 'TIME_EXIT']
        self.assertTrue(any(f.get('evidence_scope') == 'ENTRY_LINKED_ONLY' for f in fx), fx)
        self.assertTrue(any(r.endswith(':EXIT_UNLINKED_ROWS') for r in lg['budget']['exhausted']))
        c = cells(self.d)[('BNF', 'BUY')]
        self.assertEqual(c['resolved_entries'], c['valid_entry_snapshots'])
        self.assertLessEqual(os.path.getsize(c1._ev_path(self.d, S, 'BNF')), c1.MAX_EVIDENCE_BYTES_PER_INDEX_SESSION)


class CompactRowsAreLossless(Base):
    def test_expanded_r5_document_equals_the_r4_row_shape(self):
        replay(TR, self.d)
        compact = {ix: json.load(open(c1._ev_path(self.d, S, ix))) for ix in c1.INDICES}
        d2 = os.path.join(tempfile.mkdtemp(), c1.DIR_NAME)
        try:
            with mock.patch.object(c1, '_compact', lambda ev, row, kind: dict(row)), \
                    mock.patch.object(c1, '_compact_target', lambda ev, t: dict(t)), \
                    mock.patch.object(c1, 'MAX_EVIDENCE_BYTES_PER_INDEX_SESSION', 10 ** 9):   # same admissions
                replay(TR, d2)                                    # same build, rows written in the R4 shape
            full = {ix: json.load(open(c1._ev_path(d2, S, ix))) for ix in c1.INDICES}
        finally:
            shutil.rmtree(os.path.dirname(d2), ignore_errors=True)
        for ix in c1.INDICES:
            want = {k: v for k, v in full[ix].items() if k not in ('row_layout', 'row_defaults', 'row_join')}
            self.assertEqual(c1.expand_evidence(compact[ix]), want, ix)
            self.assertLess(len(json.dumps(compact[ix])), len(json.dumps(full[ix])))
        # exact-contract quote / time / depth / lot / margin fields and links are physically present per row
        for ix in c1.INDICES:
            for o in compact[ix]['observations']:
                need = ('obs_id', 'fetch_id', 'target_id', 'instrument_key', 'valid')
                need += (('bid', 'ask', 'bid_qty', 'ask_qty', 'generation_ts_raw', 'request_start_ms', 'receipt_ms',
                          'receipt_mono_ms', 'latency_ms', 'age_at_receipt_s') if o['kind'] == 'QUOTE' else
                         ('required_margin', 'final_margin', 'quantity', 'product', 'request_identity', 'asof'))
                self.assertTrue(all(k in o for k in need), (ix, o['kind'], [k for k in need if k not in o]))
            for t in compact[ix]['targets'].values():
                self.assertTrue(all(k in t for k in ('decision_id', 'instrument_key', 'lot_size', 'lot_source',
                                                     'tick_size', 'analysis_ts', 'published_at', 'registered_at')))

    def test_reads_unchanged_scorecard_identical_on_both_row_shapes(self):
        replay(TR, self.d)
        a = c1.scorecard({S: c1.read_session(self.d, S)}, [])
        d2 = os.path.join(tempfile.mkdtemp(), c1.DIR_NAME)
        try:
            with mock.patch.object(c1, '_compact', lambda ev, row, kind: dict(row)), \
                    mock.patch.object(c1, '_compact_target', lambda ev, t: dict(t)), \
                    mock.patch.object(c1, 'MAX_EVIDENCE_BYTES_PER_INDEX_SESSION', 10 ** 9):   # same admissions
                replay(TR, d2)
            b = c1.scorecard({S: c1.read_session(d2, S)}, [])
        finally:
            shutil.rmtree(os.path.dirname(d2), ignore_errors=True)
        self.assertEqual(a, b)


if __name__ == '__main__':
    unittest.main()
