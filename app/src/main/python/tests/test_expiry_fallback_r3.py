"""expiry_fallback_v1 R3 regressions (Codex R2 reply 21 Nov 2026).  SYNTHETIC fixtures and deterministic clocks only
(no network, no phone observation).  Every class here must FAIL on R2 ea0a0a53 and PASS on R3:
  s1  retention trimming must never discard the evidence of a chain-leg refusal (bounded refusal witness, checked
      causally by the shared reader), both mismatch types, both indices, both window edges, restart, later
      trimming; exact contract / planner status / reasons for every outcome advertised replayable; valid-chain and
      missing-evidence controls;
  s2  export coverage is recomputed from the records actually delivered at every degradation level;
  s3  publication reconciliation: computed vs published, deadline during / between indices, after return but
      before native accepts, late completion after timeout / cancel, restart + following successful poll;
  caps 2.5 MiB / 400 records per index with a coordinated export read cap."""
import copy
import inspect
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

import brain
import expiry_fallback_v1 as efb
import index_authority_v1 as ia
import session_evidence_v1 as se
import single_leg_store_v1 as sls
import test_expiry_fallback_v1 as T1
import test_expiry_fallback_r2 as T2
from test_expiry_fallback_v1 import SPEC, UNDER, Sc, ms, snap, stamped_chain
from test_expiry_fallback_r2 import CASES, DAY, Base, Poll, at_ms, contracts_rec, listing_rec, load, verdict
from test_single_leg_store_e2e_x1_identity import OK

OTHER_UNDER = {'NF': UNDER['BNF'], 'BNF': UNDER['NF']}
BAD = {'contract_expiry': lambda ix, c: '2027-02-23', 'underlying_key': lambda ix, c: OTHER_UNDER[ix]}
WANT = {'contract_expiry': 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH', 'underlying_key': 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH'}


def contract_identity(p):
    c = p.get('contract') or {}
    return (c.get('index'), c.get('expiry'), c.get('instrument_key'), c.get('strike'), c.get('option_type'),
            p.get('instrument_key'), p.get('status'), tuple(p.get('reasons') or []),
            (p.get('expiry_fallback') or {}).get('status'), (p.get('expiry_fallback') or {}).get('reason'),
            (p.get('expiry_fallback') or {}).get('planner_status'))


def outside_key(pl, edge):
    """A listed strike outside the retained window, at the low ('min') or high ('max') edge of the chain."""
    keys = efb._window_keys(pl.sc.chain, pl.sc.spot)
    listed = [k for k in pl.sc.chain['strikes'] if efb._strike_num(k) not in keys]
    k = (min if edge == 'min' else max)(listed, key=float)
    assert efb._strike_num(k) not in keys
    return k


# ---------------------------------------------------------------------------------------------------------------
# s1: refusal witness
# ---------------------------------------------------------------------------------------------------------------
class S1RefusalWitness(Base):
    def _bad_poll(self, ix, c, field, edge, side, hm=(10, 0), lst=None, meta=None, tag=''):
        lst = lst or listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
        meta = meta or contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
        pl = Poll(ix, c, hm, c['wide'], lst, meta, tag=tag)
        k = outside_key(pl, edge)
        pl.recs[1]['chain']['strikes'][k][side][field] = BAD[field](ix, c)
        return pl, k

    def test_outside_window_leg_refusal_replays_as_refused_both_types_indices_edges(self):
        """Codex R2 s1: an offending leg OUTSIDE the retained window (both edges, both sides, both mismatch types,
        NF and BNF).  Live is BLOCKED with the leg reason; the saved file must replay to the same refusal (R2:
        ADMISSIBLE / REPLAYABLE)."""
        n = 0
        for ix, c in CASES.items():
            for field in BAD:
                for edge, side in (('min', 'PE'), ('max', 'CE')):
                    with self.subTest(index=ix, field=field, edge=edge):
                        shutil.rmtree(self.d); os.makedirs(self.d)
                        pl, k = self._bad_poll(ix, c, field, edge, side)
                        live = self.live_and_retain(ix, [pl])[0]
                        self.assertEqual((live['status'], live['expiry_fallback']['reason']), ('BLOCKED', WANT[field]))
                        replay = pl.plan(efb.EvidenceReader.from_file(self.path()))
                        self.assertEqual(contract_identity(replay), contract_identity(live))
                        doc = load(self.path())
                        o = doc['indices'][ix]['outcomes'][0]
                        self.assertEqual(o['replay_coverage']['status'], 'REPLAYABLE', o['replay_coverage'])
                        ch = next(r for r in doc['indices'][ix]['records'] if r['kind'] == 'chain')['chain']
                        self.assertEqual(ch['leg_check']['verdict'], WANT[field])
                        self.assertEqual(ch['leg_check']['witness'], {'strike': k, 'side': side})
                        self.assertEqual(list(ch['strikes'][k]), [side])          # bounded: one witness leg
                        self.assertTrue(ch['retained_window']['refusal_witness'])
                        n += 1
        self.assertEqual(n, 8)

    def test_witness_is_the_leg_check_chain_stops_at_when_several_offend(self):
        """Both an expiry-mismatch and an index-mismatch leg outside the window: live reports the one met first in
        the chain's own order; the witness keeps exactly that one and the replay reports the same reason."""
        for ix, c in CASES.items():
            for first in ('min', 'max'):
                with self.subTest(index=ix, first=first):
                    shutil.rmtree(self.d); os.makedirs(self.d)
                    pl, k1 = self._bad_poll(ix, c, 'contract_expiry', 'min', 'CE')
                    k2 = outside_key(pl, 'max')
                    pl.recs[1]['chain']['strikes'][k2]['PE']['underlying_key'] = OTHER_UNDER[ix]
                    if first == 'max':        # reorder so the high strike is iterated first
                        st = pl.recs[1]['chain']['strikes']
                        pl.recs[1]['chain']['strikes'] = dict([(k2, st[k2])] + [(k, v) for k, v in st.items() if k != k2])
                    live = self.live_and_retain(ix, [pl])[0]
                    want = 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH' if first == 'min' else 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH'
                    self.assertEqual(live['expiry_fallback']['reason'], want)
                    self.assertEqual(efb.check_chain(ix, c['fallback'], pl.recs[1], pl.sc.cutoff)[1], want)
                    replay = pl.plan(efb.EvidenceReader.from_file(self.path()))
                    self.assertEqual(contract_identity(replay), contract_identity(live))

    def test_restart_rebound_and_later_trimming(self):
        """Restart: a retained (already bounded) chain re-bounded around a different spot keeps its witness and the
        full-chain leg_check.  Later trimming: once the record cap drops the refused poll's chain record the
        outcome is INCOMPLETE (CHAIN_RECORD_DROPPED), never REPLAYABLE with a different verdict."""
        for ix, c in CASES.items():
            with self.subTest(index=ix), mock.patch.object(efb, 'MAX_RECORDS_PER_INDEX', 8):
                shutil.rmtree(self.d); os.makedirs(self.d)
                lst = listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
                meta = contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
                bad, k = self._bad_poll(ix, c, 'underlying_key', 'min', 'CE', hm=(10, 0), lst=lst, meta=meta)
                live_bad = self.live_and_retain(ix, [bad])[0]
                saved = next(r for r in load(self.path())['indices'][ix]['records'] if r['kind'] == 'chain')
                again = efb.bound_record(saved, c['wide'] + 40 * SPEC[ix]['step'])        # restart, new spot
                self.assertEqual(again['chain']['leg_check'], saved['chain']['leg_check'])
                self.assertIn(k, again['chain']['strikes'])
                self.assertEqual(efb.check_chain(ix, c['fallback'], again, bad.sc.cutoff)[1],
                                 live_bad['expiry_fallback']['reason'])
                # restart: a later poll reads the saved file (no memory) and appends
                good = [Poll(ix, c, (11 + i // 4, (i % 4) * 15), c['wide'], lst, meta) for i in range(6)]
                live_good = self.live_and_retain(ix, good)
                doc = load(self.path())
                reader = efb.EvidenceReader.from_doc(doc)
                by_id = {pl.sc.dec['decision_id']: (pl, p) for pl, p in zip([bad] + good, [live_bad] + live_good)}
                seen = {}
                for o in doc['indices'][ix]['outcomes']:
                    pl, p = by_id[o['decision_id']]
                    seen[o['decision_id']] = o['replay_coverage']
                    if o['replay_coverage']['status'] == 'REPLAYABLE':
                        self.assertEqual(contract_identity(pl.plan(reader)), contract_identity(p), o['decision_id'])
                cov = seen[bad.sc.dec['decision_id']]
                self.assertEqual(cov['status'], 'INCOMPLETE')
                self.assertIn('CHAIN_RECORD_DROPPED', cov['reasons'])

    def test_missing_witness_or_pre_r3_record_is_incomplete(self):
        """A retained chain whose witness leg is missing (e.g. an R2-format record, or one trimmed by anything
        other than bound_record) is INCOMPLETE, never REPLAYABLE."""
        c = CASES['NF']
        pl, k = self._bad_poll('NF', c, 'contract_expiry', 'max', 'CE')
        p = self.live_and_retain('NF', [pl])[0]
        doc = load(self.path())
        ixd = doc['indices']['NF']
        ch = next(r for r in ixd['records'] if r['kind'] == 'chain')
        del ch['chain']['strikes'][k]
        efb._recompute('NF', ixd)
        self.assertEqual(ixd['outcomes'][0]['replay_coverage'],
                         {'status': 'INCOMPLETE', 'reasons': ['CHAIN_REFUSAL_WITNESS_MISSING']})
        del ch['chain']['leg_check']                              # R2-format record
        efb._recompute('NF', ixd)
        self.assertEqual(ixd['outcomes'][0]['replay_coverage']['reasons'], ['CHAIN_LEG_CHECK_UNRECORDED'])
        self.assertEqual(p['expiry_fallback']['reason'], 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH')

    def test_every_replayable_outcome_replays_exactly_with_controls(self):
        """Mixed sessions under a record cap: valid chains, both refusal types outside the window, a poll without a
        snapshot and a poll without contract metadata (missing-evidence controls).  For EVERY outcome advertised
        REPLAYABLE the shared retained reader reaches the exact contract, planner status and reasons; the controls
        keep their own results when replayed alone."""
        for ix, c in CASES.items():
            with self.subTest(index=ix), mock.patch.object(efb, 'MAX_RECORDS_PER_INDEX', 14):
                shutil.rmtree(self.d); os.makedirs(self.d)
                lst = listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
                meta = contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
                step = SPEC[ix]['step']
                polls = []
                for i in range(12):
                    hm = (10 + i // 4, (i % 4) * 15)
                    kind = i % 4
                    if kind == 1:
                        pl, _ = self._bad_poll(ix, c, 'contract_expiry', 'min', 'PE', hm=hm, lst=lst, meta=meta)
                    elif kind == 2:
                        pl, _ = self._bad_poll(ix, c, 'underlying_key', 'max', 'CE', hm=hm, lst=lst, meta=meta)
                    elif kind == 3 and i < 6:
                        pl = Poll(ix, c, hm, c['wide'] + step * 2, lst, meta, snaps=[])          # no snapshot
                    elif kind == 3:
                        pl = Poll(ix, c, hm, c['wide'] - step * 2, lst, None)                    # no metadata
                    else:
                        pl = Poll(ix, c, hm, c['wide'] + step * ((i % 5) - 2) * 3, lst, meta)
                    polls.append(pl)
                live = self.live_and_retain(ix, polls)
                reasons = {p['expiry_fallback'].get('reason') for p in live}
                self.assertTrue({None, WANT['contract_expiry'], WANT['underlying_key'],
                                 'FALLBACK_CONTRACTS_UNAVAILABLE'} <= reasons, reasons)
                self.assertTrue(any(p['status'] == 'ELIGIBLE_PENDING_SNAPSHOT' for p in live))
                doc = load(self.path())
                reader = efb.EvidenceReader.from_doc(doc)
                by_id = {pl.sc.dec['decision_id']: (pl, p) for pl, p in zip(polls, live)}
                rep = 0
                for o in doc['indices'][ix]['outcomes']:
                    pl, p = by_id[o['decision_id']]
                    if o['replay_coverage']['status'] == 'REPLAYABLE':
                        rep += 1
                        self.assertEqual(contract_identity(pl.plan(reader)), contract_identity(p), o['decision_id'])
                    else:
                        self.assertTrue(o['replay_coverage']['reasons'])
                self.assertGreater(rep, 0)
                # controls alone: each missing-evidence poll replays to its own live result from its own file
                for pl, p in zip(polls, live):
                    if p['expiry_fallback'].get('reason') == 'FALLBACK_CONTRACTS_UNAVAILABLE' or \
                            p['status'] == 'ELIGIBLE_PENDING_SNAPSHOT':
                        shutil.rmtree(self.d); os.makedirs(self.d)
                        self.live_and_retain(ix, [pl])
                        o = load(self.path())['indices'][ix]['outcomes'][0]
                        self.assertEqual(o['replay_coverage']['status'], 'REPLAYABLE')
                        self.assertEqual(contract_identity(pl.plan(efb.EvidenceReader.from_file(self.path()))),
                                         contract_identity(p))

    def test_absent_read_answered_from_another_polls_record_is_incomplete(self):
        """Missing-evidence control: a poll that had NO contract metadata live, retained next to a poll that had it
        (received before the first poll's cutoff).  The shared reader would now find metadata, so the outcome must
        say INCOMPLETE (READER_PICK_DIFFERS:contracts) rather than REPLAYABLE."""
        c = CASES['NF']
        lst = listing_rec('NF', c, at_ms(c['session'], (9, 30), 2))
        meta = contracts_rec('NF', c, at_ms(c['session'], (9, 30), 3))
        a = Poll('NF', c, (10, 0), c['wide'], lst, None)
        b = Poll('NF', c, (10, 15), c['wide'], lst, meta)
        la, lb = self.live_and_retain('NF', [a, b])
        self.assertEqual(la['expiry_fallback']['reason'], 'FALLBACK_CONTRACTS_UNAVAILABLE')
        cov = {o['decision_id']: o['replay_coverage'] for o in load(self.path())['indices']['NF']['outcomes']}
        self.assertEqual(cov[a.sc.dec['decision_id']],
                         {'status': 'INCOMPLETE', 'reasons': ['READER_PICK_DIFFERS:contracts']})
        self.assertEqual(cov[b.sc.dec['decision_id']]['status'], 'REPLAYABLE')

    def test_live_validation_unchanged(self):
        """check_chain is the live rule and is not modified: first_leg_offence (witness helper) agrees with it on
        every single-leg mutation of a stamped chain, NF and BNF."""
        for ix, c in CASES.items():
            base = stamped_chain(ix, c['fallback'], c['wide'])
            rec = {'kind': 'chain', 'index': ix, 'expiry': c['fallback'], 'chain': base,
                   'request_start_ms': 1_000, 'receipt_ms': 1_500}
            self.assertEqual(efb.check_chain(ix, c['fallback'], rec, 2_000)[1], None)
            for k in list(base['strikes'])[::7]:
                for side in ('CE', 'PE'):
                    for field in BAD:
                        m = copy.deepcopy(rec)
                        m['chain']['strikes'][k][side][field] = BAD[field](ix, c)
                        self.assertEqual(efb.check_chain(ix, c['fallback'], m, 2_000)[1],
                                         efb.first_leg_offence(ix, c['fallback'], m['chain'])[0])


# ---------------------------------------------------------------------------------------------------------------
# s2: delivered coverage at every export level
# ---------------------------------------------------------------------------------------------------------------
class S2DeliveredCoverage(Base):
    def setUp(self):
        super().setUp()
        self.fd = self.tmp
        os.makedirs(os.path.join(self.fd, 'index_authority_v1'))
        with open(os.path.join(self.fd, 'index_authority_v1', DAY + '.json'), 'w') as f:
            json.dump({'session_date': DAY, 'history': {'NF': [], 'BNF': []}}, f)

    build = T2.S2ExportPart.build
    fill = T2.S2ExportPart.fill

    @staticmethod
    def pilot(_d, _day, level):
        return {'status': 'OK', 'level': level, 'payload': 'x' * (8000 if level == 'full' else 100)}

    def trace(self):
        os.makedirs(os.path.join(self.fd, se.TRACE_DIR), exist_ok=True)
        with open(os.path.join(self.fd, se.TRACE_DIR, DAY + '.json'), 'w') as f:
            json.dump({'schema': 'mr.single_leg_trace.v1', 'session_date': DAY, 'truncated': False,
                       'records': [{'kind': 'TAP', 't_ms': 1, 'index': 'NF', 'origin': 'tap%d' % i} for i in range(30)]}, f)

    def check_bytes(self, raw, source_cov):
        """Recompute both indices' coverage from the EXPORTED BYTES only and compare with what the export says."""
        b = json.loads(raw)
        part = b['parts'].get('expiry_fallback')
        entry = b['manifest']['parts'].get('expiry_fallback')
        if part is None:
            if entry is not None:
                self.assertEqual((entry['delivered_outcomes'], entry['delivered_replayable']), (0, 0))
            self.assertNotIn(b'"replay_coverage"', raw)
            return entry and entry['status']
        recomputed = {ix: [efb.coverage_of(ix, o, {efb._rkey(r): r for r in d.get('records') or []})
                           for o in d.get('outcomes') or []] for ix, d in part['indices'].items()}
        self.assertEqual(sorted(part['indices']), ['BNF', 'NF'])
        for ix, d in part['indices'].items():
            for o, cov in zip(d.get('outcomes') or [], recomputed[ix]):
                # what the export claims for this outcome must be what its own bytes support
                self.assertEqual(o.get('delivered_replay_coverage', o.get('replay_coverage')), cov)
                self.assertNotIn('replay_coverage', o)                               # no ambiguous name
            self.assertEqual(d['source_coverage_session'], source_cov[ix])           # device metric, own name
            self.assertEqual(efb.delivered_coverage_from_export(part)[ix], recomputed[ix])
            if part['export_level'] == 'counts_only':
                self.assertNotIn('outcomes', d)
                self.assertEqual(d['delivered_coverage_session'],
                                 {'REPLAYABLE': 0, 'INCOMPLETE': 0, 'NOT_DELIVERED': d['source_counts']['outcomes']})
            else:
                self.assertEqual(d['delivered_coverage_session'],
                                 {s: sum(1 for x in recomputed[ix] if x['status'] == s) for s in ('REPLAYABLE', 'INCOMPLETE')})
            if part['export_level'] in ('no_chain', 'outcomes_only'):
                self.assertEqual(d['delivered_coverage_session']['REPLAYABLE'], 0)
                self.assertTrue(all('CHAIN_RECORD_DROPPED' in x['reasons'] for x in recomputed[ix]))
        return part['export_level']

    def test_every_level_recomputes_coverage_from_exported_bytes_and_protects_c1_trace(self):
        self.trace()
        with mock.patch('c1_capture_pilot_v1.export_part', side_effect=self.pilot):
            ref, ref_raw = self.build()
            self.fill(6)
            full_b, full_raw = self.build()
            src = efb.export_part(self.d, DAY, se.EXPIRY_FALLBACK_READ_CAP)[1]
            source_cov = {ix: d['coverage_session'] for ix, d in src['indices'].items()}
            self.assertEqual(source_cov, {'NF': {'REPLAYABLE': 6, 'INCOMPLETE': 0}, 'BNF': {'REPLAYABLE': 6, 'INCOMPLETE': 0}})
            seen = {self.check_bytes(full_raw, source_cov)}
            lo, hi = len(ref_raw), len(full_raw)
            for i in range(1, 48):
                cap = hi - (hi - lo) * i // 48
                with mock.patch.object(se, 'MAX_BUNDLE_BYTES', cap):
                    b, raw = self.build()
                self.assertLessEqual(len(raw), cap)
                for k in ('c1_capture', 'single_leg_trace'):
                    self.assertEqual(b['parts'][k], ref['parts'][k], k)
                    self.assertEqual(b['manifest']['parts'][k], ref['manifest']['parts'][k], k)
                seen.add(self.check_bytes(raw, source_cov))
            with mock.patch.object(se, 'MAX_BUNDLE_BYTES', lo + 5):
                b, raw = self.build()
            seen.add(self.check_bytes(raw, source_cov) or 'ABSENT')
            self.assertTrue({'full', 'no_chain', 'outcomes_only', 'counts_only', 'OMITTED_FOR_CAP', 'ABSENT'} <= seen, seen)

    def test_codex_reproduction_no_chain_is_not_replayable(self):
        self.fill()
        _, full_raw = self.build()
        with mock.patch.object(se, 'MAX_BUNDLE_BYTES', len(full_raw) - 10):
            exported, raw = self.build()
        e = exported['manifest']['parts']['expiry_fallback']
        self.assertEqual((e['status'], e['fallback_level']), ('TRUNCATED', 'no_chain'))
        ixp = exported['parts']['expiry_fallback']['indices']['NF']
        row = ixp['outcomes'][0]
        recomputed = efb.coverage_of('NF', row, {efb._rkey(r): r for r in ixp['records']})
        self.assertEqual(recomputed, {'status': 'INCOMPLETE', 'reasons': ['CHAIN_RECORD_DROPPED']})
        self.assertEqual(row.get('delivered_replay_coverage', row.get('replay_coverage')), recomputed)
        self.assertEqual(ixp['delivered_counts']['chain_records'], 0)
        self.assertEqual(row['delivered_replay_coverage']['status'], 'INCOMPLETE')
        self.assertIn('CHAIN_RECORD_DROPPED', row['delivered_replay_coverage']['reasons'])
        self.assertEqual(row['source_replay_coverage']['status'], 'REPLAYABLE')
        self.assertEqual(ixp['delivered_coverage_session'], {'REPLAYABLE': 0, 'INCOMPLETE': 4})
        self.assertEqual(ixp['source_coverage_session'], {'REPLAYABLE': 4, 'INCOMPLETE': 0})
        self.assertEqual(e['drops']['NF']['delivered_coverage_session'], {'REPLAYABLE': 0, 'INCOMPLETE': 4})

    def test_views_never_mutate_the_source(self):
        self.fill()
        src = efb.export_part(self.d, DAY, se.EXPIRY_FALLBACK_READ_CAP)[1]
        before = json.dumps(src, sort_keys=True)
        views = [efb.export_view(src, lvl) for lvl in efb.EXPORT_LEVELS]
        self.assertEqual(json.dumps(src, sort_keys=True), before)
        views[0]['indices']['NF']['outcomes'][0]['deps']['strike'] = -1             # copies are independent
        self.assertEqual(json.dumps(src, sort_keys=True), before)

    def test_unreadable_and_oversized_deliver_nothing_replayable(self):
        with mock.patch('c1_capture_pilot_v1.export_part', side_effect=self.pilot):
            ref, _ = self.build()
            os.makedirs(os.path.join(self.d, efb.RETAIN_DIR))
            for body in ('{bad', ' ' * (se.EXPIRY_FALLBACK_READ_CAP + 1)):
                with open(self.path(), 'w') as f:
                    f.write(body)
                b, raw = self.build()
                e = b['manifest']['parts']['expiry_fallback']
                self.assertEqual(e['status'], 'UNREADABLE')
                self.assertEqual((e['delivered_outcomes'], e['delivered_replayable']), (0, 0))
                self.assertNotIn('expiry_fallback', b['parts'])
                self.assertEqual(b['manifest']['parts']['c1_capture'], ref['manifest']['parts']['c1_capture'])


# ---------------------------------------------------------------------------------------------------------------
# s3: publication reconciliation
# ---------------------------------------------------------------------------------------------------------------
class S3Publication(unittest.TestCase):
    SESSION, NEXT = T1.Integration.SESSION, T1.Integration.NEXT

    @classmethod
    def setUpClass(cls):
        T1.Integration.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    evidence = T1.Integration.evidence
    newdir = T2.S3S4Integration.newdir
    probe = T2.S3S4Integration.probe

    def setUp(self):
        self.r = copy.deepcopy(self.result)
        self.dec = self.r['by_index']['NF']
        self.at = ia.parse_ts(self.dec['analysis_ts'])
        self.spot = self.dec['direction']['inputs']['spot']['value']

    def apply(self, r, ev, d, deadline_ms=None, attempt=None):
        args = [d, json.dumps(r), json.dumps(self.ctx), json.dumps(ev), ms(self.at) + 5000, '[]', deadline_ms]
        # (signature-adaptive so the same test runs against R2, which has no attempt binding: there it fails on
        #  BEHAVIOUR, not on the call)
        if attempt is not None and 'attempt_json' in inspect.signature(brain.single_leg_expiry_fallback_apply).parameters:
            args.append(json.dumps(attempt))
        return json.loads(brain.single_leg_expiry_fallback_apply(*args))

    def two_index(self):
        """NF plus a BNF copy that also asks for evidence (its own outcome is a named WAIT: the BNF monthly is
        listed as weekly in this fixture) so both indices retain an outcome in one attempt."""
        r = copy.deepcopy(self.result)
        bdec = copy.deepcopy(r['by_index']['NF'])
        bdec['decision_id'] = r['by_index']['NF']['decision_id'] + '_bnf'
        bplan = copy.deepcopy(r['single_leg_by_index']['NF'])
        bplan['decision_id'] = bdec['decision_id']
        bplan['expiry_fallback'] = dict(bplan['expiry_fallback'], decision_id=bdec['decision_id'], index='BNF')
        r['by_index']['BNF'] = bdec
        r['single_leg_by_index']['BNF'] = bplan
        ev, ch = self.evidence(self.at, self.spot)
        ev['BNF'] = copy.deepcopy(ev['NF'])
        ev['BNF']['listing']['index'] = 'BNF'
        return r, ev, ch

    def outcomes(self, d):
        doc = load(os.path.join(d, efb.RETAIN_DIR, efb.RETAIN_DOC))
        return doc, {ix: v['outcomes'] for ix, v in doc['indices'].items()}

    def controlled(self, mode, two):
        """time.time is a mutable clock; deadline = clock + 4 s; the production retain is wrapped to advance the
        clock by 5 s before (mode 'during') or after (mode 'between') the FIRST index's retention write."""
        clock = {'t': float(int(time.time() * 1000))}
        orig = efb.retain
        calls = []

        def wrapped(*a, **k):
            calls.append(a[2])
            if len(calls) == 1 and mode == 'during':
                clock['t'] += 5000
            out = orig(*a, **k)
            if len(calls) == 1 and mode == 'between':
                clock['t'] += 5000
            return out
        r, ev, _ = self.two_index() if two else (self.r, self.evidence(self.at, self.spot)[0], None)
        d = self.newdir()
        with mock.patch('time.time', lambda: clock['t'] / 1000.0), mock.patch.object(efb, 'retain', wrapped):
            out = self.apply(r, ev, d, deadline_ms=int(clock['t']) + 4000,
                             attempt={'attempt_id': 'efb-11-t', 'generation': 11})
        return out, d, calls

    def test_deadline_expires_during_first_index_retention(self):
        """Codex R2 s3 reproduction (clock + 5 s inside the real retention call, 4 s deadline): R2 returned the
        plan and saved ELIGIBLE_PENDING_SNAPSHOT as if published.  R3: no plan is returned and the saved outcome
        keeps its COMPUTED verdict with publication NOT_RETURNED_DEADLINE (never counted as published)."""
        for two in (False, True):
            with self.subTest(two_indices=two):
                out, d, calls = self.controlled('during', two)
                self.assertEqual(out.get('error'), 'APPLY_DEADLINE_EXCEEDED', out)
                self.assertNotIn('single_leg_by_index', out)
                self.assertEqual(calls, ['NF'])                       # nothing written after the deadline
                doc, oc = self.outcomes(d)
                o = oc['NF'][-1]
                self.assertEqual(o['planner_status'], 'ELIGIBLE_PENDING_SNAPSHOT')        # computed, kept
                self.assertEqual(o['publication']['state'], efb.PUB_NOT_RETURNED)
                self.assertEqual((o['publication']['attempt_id'], o['publication']['generation']), ('efb-11-t', 11))
                self.assertNotIn('BNF', oc)
                cnt = efb.publication_counts(doc['indices']['NF'])
                self.assertNotIn(efb.PUB_PUBLISHED, cnt)
                self.assertEqual(cnt[efb.PUB_NOT_RETURNED], {'outcomes': 1, 'computed_eligible': 1})
                # native's own decision for that attempt arrives afterwards: refused, state unchanged
                rec = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-11-t', 11, 'PUBLISHED'))
                self.assertEqual((rec['accepted'], rec['state']), (False, efb.PUB_NOT_RETURNED))

    def test_deadline_expires_between_indices(self):
        out, d, calls = self.controlled('between', True)
        self.assertEqual(out.get('error'), 'APPLY_DEADLINE_EXCEEDED', out)
        self.assertEqual(out['attempt']['retained_indices'], ['NF'])
        self.assertEqual(calls, ['NF'])                               # BNF never written after the deadline
        _doc, oc = self.outcomes(d)
        self.assertEqual(oc['NF'][-1]['publication']['state'], efb.PUB_NOT_RETURNED)
        self.assertNotIn('BNF', oc)

    def test_both_indices_bound_to_one_attempt_when_in_time(self):
        r, ev, _ = self.two_index()
        d = self.newdir()
        out = self.apply(r, ev, d, deadline_ms=int(time.time() * 1000) + 60_000,
                         attempt={'attempt_id': 'efb-12-t', 'generation': 12})
        self.assertNotIn('error', out)
        self.assertEqual(out['attempt'], {'attempt_id': 'efb-12-t', 'generation': 12, 'publication_state': 'UNCONFIRMED'})
        _doc, oc = self.outcomes(d)
        for ix in ('NF', 'BNF'):
            self.assertEqual(oc[ix][-1]['publication']['state'], efb.PUB_UNCONFIRMED)
            self.assertEqual(out['single_leg_by_index'][ix]['expiry_fallback']['attempt']['attempt_id'], 'efb-12-t')
        self.assertEqual(json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-12-t', 12, 'PUBLISHED'))['state'],
                         efb.PUB_PUBLISHED)
        _doc, oc = self.outcomes(d)
        self.assertEqual({ix: oc[ix][-1]['publication']['state'] for ix in oc}, {'NF': 'PUBLISHED', 'BNF': 'PUBLISHED'})

    def test_returned_but_not_accepted_by_native_is_never_published_or_admitted(self):
        """Python returned in time; native found its deadline passed before accepting (or PyTimeout fired) and
        published the explicit WAIT instead.  The retained outcome becomes TIMED_OUT; a later PUBLISHED ack is
        refused; a tap on the computed (never published) proposal is rejected; no position is created."""
        ev, ch = self.evidence(self.at, self.spot)
        d = self.newdir()
        base = copy.deepcopy(self.r['single_leg_by_index']['NF'])
        out = self.apply(self.r, ev, d, attempt={'attempt_id': 'efb-21-t', 'generation': 21})
        p = out['single_leg_by_index']['NF']
        self.assertEqual(p['status'], 'ELIGIBLE_PENDING_SNAPSHOT')
        rec = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-21-t', 21, 'TIMED_OUT'))
        self.assertEqual((rec['accepted'], rec['state']), (True, 'TIMED_OUT'))
        late = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-21-t', 21, 'PUBLISHED'))
        self.assertEqual((late['accepted'], late['state']), (False, 'TIMED_OUT'))
        doc, oc = self.outcomes(d)
        self.assertEqual(oc['NF'][-1]['publication']['state'], 'TIMED_OUT')
        self.assertEqual(oc['NF'][-1]['planner_status'], 'ELIGIBLE_PENDING_SNAPSHOT')
        self.assertEqual(doc['publication']['late_acks_refused'], 1)
        w = efb.withdraw(base, 'APPLY_TIMEOUT')                                   # what native published
        computed = copy.deepcopy(self.r); computed['single_leg_by_index']['NF'] = p
        t = self.at + timedelta(seconds=40)
        fresh = snap(ch, p['instrument_key'], t - timedelta(seconds=2), t - timedelta(seconds=1))
        tap = sls.admit(d, computed, 'NF', clock=lambda: t, snapshot=fresh, state_probe=self.probe(w), **OK)
        self.assertEqual(tap['reason'], 'DECISION_CHANGED_BEFORE_COMMIT')
        published = copy.deepcopy(self.r); published['single_leg_by_index']['NF'] = w
        self.assertEqual(sls.admit(d, published, 'NF', clock=lambda: t, snapshot=fresh, **OK)['status'], 'REJECTED')
        self.assertEqual(sls.read(d)['positions'], [])

    def test_late_completion_after_timeout_or_cancel_inherits_the_decision(self):
        """Native decides first (PyTimeout fired / a newer attempt cancelled it); the Python apply finishes later.
        Its retention write inherits TIMED_OUT / CANCELLED (never UNCONFIRMED or PUBLISHED), it returns no plan,
        and an older generation written after a newer one is SUPERSEDED."""
        for st in ('TIMED_OUT', 'CANCELLED'):
            with self.subTest(state=st):
                ev, _ = self.evidence(self.at, self.spot)
                d = self.newdir()
                pre = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-31-t', 31, st))
                self.assertEqual((pre['accepted'], pre['state']), (True, st))
                out = self.apply(self.r, ev, d, attempt={'attempt_id': 'efb-31-t', 'generation': 31})
                self.assertEqual(out['error'], 'ATTEMPT_ALREADY_DECIDED:' + st)
                self.assertNotIn('single_leg_by_index', out)
                _doc, oc = self.outcomes(d)
                self.assertEqual(oc['NF'][-1]['publication']['state'], st)
                self.assertEqual(json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-31-t', 31, 'PUBLISHED'))['state'], st)
                self.assertEqual(sls.read(d)['positions'], [])
        # old work after newer work
        ev, _ = self.evidence(self.at, self.spot)
        d = self.newdir()
        newer = self.apply(self.r, ev, d, attempt={'attempt_id': 'efb-41-t', 'generation': 41})
        self.assertNotIn('error', newer)
        older = self.apply(self.r, ev, d, attempt={'attempt_id': 'efb-40-t', 'generation': 40})
        self.assertEqual(older['error'], 'ATTEMPT_ALREADY_DECIDED:SUPERSEDED')
        rec = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-40-t', 40, 'PUBLISHED'))
        self.assertEqual((rec['accepted'], rec['state']), (False, 'SUPERSEDED'))
        self.assertEqual(json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-41-t', 41, 'PUBLISHED'))['state'],
                         'PUBLISHED')
        _doc, oc = self.outcomes(d)
        self.assertEqual([(o['publication']['attempt_id'], o['publication']['state']) for o in oc['NF']],
                         [('efb-41-t', 'PUBLISHED'), ('efb-40-t', 'SUPERSEDED')])

    def test_restart_keeps_unconfirmed_honest_and_next_poll_publishes_and_admits(self):
        """Attempt g51 is retained and returned, then the process dies before native acknowledges it: it stays
        UNCONFIRMED (never PUBLISHED).  After restart (state only on disk) the next qualifying poll (g52, a new
        decision) is published and admitted; the late g51 ack is refused; only g52's decision opens a position."""
        ev, ch = self.evidence(self.at, self.spot)
        d = self.newdir()
        first = self.apply(self.r, ev, d, attempt={'attempt_id': 'efb-51-t', 'generation': 51})
        self.assertNotIn('error', first)
        shown = copy.deepcopy(self.r); shown['single_leg_by_index']['NF'] = first['single_leg_by_index']['NF']
        # restart: drop every in-memory object; only the files remain
        nxt = copy.deepcopy(self.result)
        new_id = self.dec['decision_id'] + '_r3next'
        t2 = self.at + timedelta(seconds=60)
        nxt['by_index']['NF'].update(decision_id=new_id, analysis_ts=t2.isoformat())
        nb = nxt['single_leg_by_index']['NF']
        nb['decision_id'] = new_id
        nb['expiry_fallback']['decision_id'] = new_id
        ev2, ch2 = self.evidence(t2, self.spot)
        args = [d, json.dumps(nxt), json.dumps(self.ctx), json.dumps(ev2), ms(t2) + 5000, '[]', None,
                json.dumps({'attempt_id': 'efb-52-t', 'generation': 52})]
        second = json.loads(brain.single_leg_expiry_fallback_apply(*args))
        self.assertNotIn('error', second)
        self.assertEqual(json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-52-t', 52, 'PUBLISHED'))['state'],
                         'PUBLISHED')
        stale_ack = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-51-t', 51, 'PUBLISHED'))
        self.assertEqual((stale_ack['accepted'], stale_ack['state']), (False, 'SUPERSEDED'))
        doc, oc = self.outcomes(d)
        states = [(o['publication']['attempt_id'], o['publication']['state']) for o in oc['NF']]
        self.assertEqual(states, [('efb-51-t', 'SUPERSEDED'), ('efb-52-t', 'PUBLISHED')])
        cnt = efb.publication_counts(doc['indices']['NF'])
        self.assertEqual(cnt['PUBLISHED'], {'outcomes': 1, 'computed_eligible': 1})
        p2 = second['single_leg_by_index']['NF']
        nxt['single_leg_by_index']['NF'] = p2
        t3 = t2 + timedelta(seconds=30)
        f3 = snap(ch2, p2['instrument_key'], t3 - timedelta(seconds=2), t3 - timedelta(seconds=1))
        self.assertEqual(sls.admit(d, shown, 'NF', clock=lambda: t3, snapshot=f3, state_probe=self.probe(p2),
                                   **OK)['status'], 'REJECTED')
        okp = sls.admit(d, nxt, 'NF', clock=lambda: t3, snapshot=f3, state_probe=self.probe(p2), **OK)
        self.assertEqual(okp['status'], 'ADMITTED', okp)
        self.assertEqual([x.get('entry_decision_id') for x in sls.read(d)['positions']], [new_id])

    def test_unconfirmed_without_any_later_attempt_stays_unconfirmed(self):
        ev, _ = self.evidence(self.at, self.spot)
        d = self.newdir()
        self.apply(self.r, ev, d, attempt={'attempt_id': 'efb-61-t', 'generation': 61})
        doc, oc = self.outcomes(d)
        self.assertEqual(oc['NF'][-1]['publication']['state'], 'UNCONFIRMED')
        self.assertEqual(efb.summary(d)['NF']['publication'], {'UNCONFIRMED': {'outcomes': 1, 'computed_eligible': 1}})

    def test_late_write_and_reconcile_are_serialised(self):
        """A late Python retention write (slow, inside the lock) and native's TIMED_OUT reconcile race on two
        threads: whichever order, the attempt and its outcome end TIMED_OUT."""
        ev, _ = self.evidence(self.at, self.spot)
        for delay_reconcile in (0.0, 0.4):
            with self.subTest(delay=delay_reconcile):
                d = self.newdir()
                orig = efb._retain_locked

                def slow(*a, **k):
                    time.sleep(0.3)
                    return orig(*a, **k)
                res = {}
                with mock.patch.object(efb, '_retain_locked', slow):
                    th = threading.Thread(target=lambda: res.update(out=self.apply(
                        self.r, ev, d, attempt={'attempt_id': 'efb-71-t', 'generation': 71})))
                    th.start()
                    time.sleep(0.05 + delay_reconcile)
                    rec = json.loads(brain.single_leg_expiry_fallback_reconcile(d, 'efb-71-t', 71, 'TIMED_OUT'))
                    th.join(10)
                self.assertEqual(rec['state'], 'TIMED_OUT')
                doc, oc = self.outcomes(d)
                self.assertEqual(oc['NF'][-1]['publication']['state'], 'TIMED_OUT')
                self.assertEqual(doc['publication']['attempts']['efb-71-t']['state'], 'TIMED_OUT')


# ---------------------------------------------------------------------------------------------------------------
# bounded storage: 2.5 MiB / 400 records with a coordinated export read cap
# ---------------------------------------------------------------------------------------------------------------
class CapsCoordinated(Base):
    def test_constants(self):
        self.assertEqual(efb.MAX_RETAINED_BYTES, 2560 * 1024)
        self.assertEqual(efb.MAX_RECORDS_PER_INDEX, 400)
        self.assertGreaterEqual(se.EXPIRY_FALLBACK_READ_CAP - efb.MAX_RETAINED_BYTES, 256 * 1024)
        self.assertLess(se.EXPIRY_FALLBACK_READ_CAP, se.MAX_BUNDLE_BYTES)

    def test_a_file_at_the_writer_cap_is_readable_by_the_exporter(self):
        doc = {'schema': efb.EVIDENCE_SCHEMA, 'indices': {'NF': {'records': [], 'outcomes': [], 'stats': {}}}}
        raw = json.dumps(doc, separators=(',', ':'))
        pad = efb.MAX_RETAINED_BYTES - len(raw) - len(',"pad":""')
        doc['pad'] = 'x' * pad
        os.makedirs(os.path.join(self.d, efb.RETAIN_DIR))
        with open(self.path(), 'w') as f:
            f.write(json.dumps(doc, separators=(',', ':')))
        self.assertEqual(os.path.getsize(self.path()), efb.MAX_RETAINED_BYTES)
        st, part = efb.export_part(self.d, DAY, se.EXPIRY_FALLBACK_READ_CAP)
        self.assertEqual(st, 'PRESENT')
        with open(self.path(), 'a') as f:
            f.write(' ' * (se.EXPIRY_FALLBACK_READ_CAP - efb.MAX_RETAINED_BYTES + 1))
        self.assertEqual(efb.export_part(self.d, DAY, se.EXPIRY_FALLBACK_READ_CAP)[0], 'OVER_CAP')

    def test_record_cap_400_is_enforced_and_byte_cap_holds_even_with_outcomes_only(self):
        c = CASES['NF']
        lst = listing_rec('NF', c, at_ms(c['session'], (9, 30), 2))
        meta = contracts_rec('NF', c, at_ms(c['session'], (9, 30), 3))
        with mock.patch.object(efb, 'MAX_RECORDS_PER_INDEX', 9):
            polls = [Poll('NF', c, (10 + i // 4, (i % 4) * 15), c['wide'], lst, meta) for i in range(6)]
            self.live_and_retain('NF', polls)
            self.assertLessEqual(len(load(self.path())['indices']['NF']['records']), 9)
        shutil.rmtree(self.d); os.makedirs(self.d)
        with mock.patch.object(efb, 'MAX_RETAINED_BYTES', 4000):
            for i in range(30):
                efb.retain(self.d, DAY, 'NF', [], {'decision_id': 'x%d' % i, 'cutoff_ms': i, 'deps': {},
                                                   'planner_reasons': ['R' * 40]}, {})
            self.assertLessEqual(os.path.getsize(self.path()), 4000)
            drops = load(self.path())['indices']['NF']['drops'][DAY]
            self.assertGreater(drops.get('BYTE_CAP:outcome', 0), 0)


if __name__ == '__main__':
    unittest.main()
