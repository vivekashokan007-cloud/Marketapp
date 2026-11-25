"""expiry_fallback_v1 (REVIEW DRAFT r1, fixtures updated for r2): expiry-day Paper-BUY fallback.  SYNTHETIC fixtures and deterministic
clocks only (no network, no phone observation).  Covers: quote after cutoff (Codex finding 1), BNF monthly
through the fallback path (Codex finding 2), stale/missing evidence, wrong index/instrument, holiday-shifted
expiry, unchanged sell and ordinary-day behaviour, low capital / occupied exposure, and the integration path
(analysis -> acquisition apply -> proposal -> admission incl. stale/replaced approval -> restart -> exact exit)."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta

import brain
import expiry_fallback_v1 as efb
import index_authority_v1 as ia
import index_single_leg_v1 as sl
import paper_broker_snapshot_v1 as pbs
import single_leg_store_v1 as sls
import index_authority_fixtures as F
import test_index_single_leg_v1_delivery_b as TB
from test_single_leg_store_e2e_x1_identity import OK

UNDER = TB.UNDER
SPEC = {'NF': {'step': 50, 'lot': 65, 'spot': 24000.0}, 'BNF': {'step': 100, 'lot': 30, 'spot': 52000.0}}


def ms(t):
    return int(t.timestamp() * 1000)


def stamped_chain(ix, expiry, spot=None):
    s = SPEC[ix]
    c = F.chain(spot or s['spot'], s['step'], lot=s['lot'], rel_spread=0.02)
    for k, sides in c['strikes'].items():
        for ot, leg in sides.items():
            key = 'NSE_FO|%s%s%s%s' % (ix, expiry.replace('-', ''), k, ot)
            leg.update(instrument_key=key, instrumentKey=key, contract_expiry=expiry, underlying_key=UNDER[ix])
    c.update(expiry=expiry, requested_expiry=expiry, requested_underlying_key=UNDER[ix])
    return c


def snap(chain, key, gen, receipt, qty=500):
    resp = TB.v3_response(chain, gen, qty, keys={key})
    out, rej = pbs.parse_v3_response([key], resp, request_start_ms=ms(receipt) - 400, receipt_ms=ms(receipt),
                                     receipt_mono_ms=1)
    assert not rej, rej
    return out[key]


class Sc:
    """One synthetic expiry-day scenario.  T = trusted analysis clock; cutoff defaults to T + 5 s."""

    def __init__(self, ix='NF', session='2026-11-23', trade='2026-11-23', fallback='2026-12-01',
                 listed=('2026-11-23', '2026-12-01', '2026-12-08'), er=0.5, value='BULL', hm=(10, 0)):
        self.ix, self.session, self.trade, self.fallback, self.listed = ix, session, trade, fallback, list(listed)
        self.T = ia.parse_ts('%sT%02d:%02d:20+05:30' % (session, hm[0], hm[1]))
        self.cutoff = ms(self.T) + 5000
        self.spot = SPEC[ix]['spot']
        self.dec = {'direction': {'value': value, 'er': er, 'inputs': {'spot': {'value': self.spot}}},
                    'decision_id': 'idx1_fb_' + ix, 'decision_poll_ts': (self.T - timedelta(seconds=20)).isoformat(),
                    'analysis_ts': self.T.isoformat(), 'session_date': session}
        self.chain = stamped_chain(ix, fallback) if fallback else None
        # R2: Upstox listing rows carry `weekly`; BNF monthly identity needs weekly == false for the monthly expiry
        self.weekly = {e: (e != fallback) for e in self.listed}

    def base(self, enabled=True, paper_mode=True, **kw):
        tchain = stamped_chain(self.ix, self.trade)
        args = dict(session_date=self.session, decision_dt=self.T, expiry=self.trade, captured_lot=SPEC[self.ix]['lot'],
                    history=[], open_trades=[], evidence=None)
        args.update(kw)
        p = sl.plan_entry(self.ix, self.dec, tchain, self.spot, **args)
        return efb.mark_required(self.ix, self.dec, p, session_date=self.session, trade_expiry=self.trade,
                                 enabled=enabled, paper_mode=paper_mode)

    def k0_key(self, chain=None):
        k, _ = sl.select_strike(sl.choose_action(self.dec['direction'])[0], chain or self.chain, self.spot)
        ot = sl.choose_action(self.dec['direction'])[0][-2:]
        return (chain or self.chain)['strikes'][str(k)][ot]['instrument_key']

    def records(self, listing_s=2.0, chain_s=3.0, contracts_s=2.5, snap_gen_s=3.5, snap_rc_s=4.0, chain=None,
                snapshot=True, listing_ix=None, chain_ix=None, contracts_underlying=None):
        T = self.T
        c = chain or self.chain
        recs = [{'kind': 'listing', 'index': listing_ix or self.ix, 'expiries': self.listed, 'weekly': self.weekly,
                 'request_start_ms': ms(T) + int(listing_s * 1000) - 300, 'receipt_ms': ms(T) + int(listing_s * 1000)}]
        if c is not None:
            recs.append({'kind': 'chain', 'index': chain_ix or self.ix, 'expiry': self.fallback, 'chain': c,
                         'request_start_ms': ms(T) + int(chain_s * 1000) - 400, 'receipt_ms': ms(T) + int(chain_s * 1000)})
            recs.append({'kind': 'contracts', 'index': self.ix, 'expiry': self.fallback,
                         'response': TB.contracts_response(c, self.ix, expiry=self.fallback, lot=SPEC[self.ix]['lot'],
                                                           underlying=contracts_underlying),
                         'receipt_ms': ms(T) + int(contracts_s * 1000)})
            if snapshot:
                key = self.k0_key(c)
                s = snap(c, key, T + timedelta(seconds=snap_gen_s), T + timedelta(seconds=snap_rc_s))
                recs.append({'kind': 'snapshot', 'index': self.ix, 'expiry': self.fallback, 'instrument_key': key,
                             'receipt_ms': s['receipt_ms'], 'snapshot': s})
        return recs

    def run(self, recs=None, cutoff=None, base=None, **kw):
        args = dict(session_date=self.session, trade_expiry=self.trade, cutoff_ms=cutoff or self.cutoff,
                    reader=efb.EvidenceReader(self.records() if recs is None else recs), history=[], open_trades=[],
                    capital=OK['capital'], max_risk_pct=OK['max_risk_pct'])
        args.update(kw)
        return efb.plan_fallback(self.ix, self.dec, base or self.base(), self.spot, **args)


def wait_reason(p):
    return (p.get('expiry_fallback') or {}).get('reason')


class Applicability(unittest.TestCase):
    def test_ordinary_day_plan_is_returned_unchanged(self):
        sc = Sc(trade='2026-12-01')                     # trading expiry after the session: not an expiry day
        tchain = stamped_chain('NF', '2026-12-01')
        p = sl.plan_entry('NF', sc.dec, tchain, sc.spot, session_date=sc.session, decision_dt=sc.T, expiry='2026-12-01',
                          captured_lot=65, history=[], open_trades=[], evidence=None)
        before = copy.deepcopy(p)
        out = efb.mark_required('NF', sc.dec, p, session_date=sc.session, trade_expiry='2026-12-01', enabled=True,
                                paper_mode=True)
        self.assertIs(out, p)
        self.assertEqual(out, before)
        self.assertNotIn('expiry_fallback', out)

    def test_sell_on_expiry_day_is_unchanged(self):
        sc = Sc(er=0.1)                                  # BULL with ER < 0.30 -> SELL_PE (policy)
        tchain = stamped_chain('NF', sc.trade)
        p = sl.plan_entry('NF', sc.dec, tchain, sc.spot, session_date=sc.session, decision_dt=sc.T, expiry=sc.trade,
                          captured_lot=65, history=[], open_trades=[], evidence=None)
        self.assertEqual(p['action'], 'SELL_PE')
        before = copy.deepcopy(p)
        out = efb.mark_required('NF', sc.dec, p, session_date=sc.session, trade_expiry=sc.trade, enabled=True,
                                paper_mode=True)
        self.assertIs(out, p)
        self.assertEqual(out, before)
        self.assertIn('EXPIRY_DAY_NO_NEXT_CHAIN', out['reasons'])       # the existing refusal stands

    def test_buy_on_expiry_day_asks_for_evidence_and_keeps_base_reasons(self):
        sc = Sc()
        p = sc.base()
        fb = p['expiry_fallback']
        self.assertEqual(fb['status'], efb.STATUS_REQUIRED)
        self.assertIn('EXPIRY_DAY_NO_NEXT_CHAIN', p['reasons'])
        self.assertEqual(p['reasons'][-1], 'EXPIRY_FALLBACK_EVIDENCE_PENDING')
        self.assertEqual(p['status'], 'BLOCKED')
        self.assertFalse(p['admit'])
        self.assertIn('EXPIRY_DAY_NO_NEXT_CHAIN', fb['base_reasons'])

    def test_expiry_independent_gates_and_optin_wait_without_acquisition(self):
        late = Sc(hm=(14, 40))
        self.assertEqual(wait_reason(late.base()), 'EXPIRY_INDEPENDENT_GATE')
        self.assertIn('ENTRY_WINDOW_CLOSED', late.base()['expiry_fallback']['gates'])
        sc = Sc()
        self.assertEqual(wait_reason(sc.base(enabled=False)), 'SINGLE_LEG_PAPER_DISABLED')
        self.assertEqual(wait_reason(sc.base(paper_mode=False)), 'NOT_PAPER_MODE')
        self.assertEqual(wait_reason(sc.base(history_supplied=False)), 'EXPIRY_INDEPENDENT_GATE')
        for p in (late.base(), sc.base(enabled=False)):
            self.assertEqual(p['status'], 'BLOCKED')
            self.assertTrue(p['reasons'][-1].startswith('EXPIRY_FALLBACK_WAIT:'))


class Resolution(unittest.TestCase):
    def test_nf_next_distinct_listing_and_holiday_shifted_expiry(self):
        # 17 Nov 2026 (Tue) is NF's own expiry; Tue 24 Nov is an NSE holiday -> next listed NF expiry Mon 23 Nov
        sc = Sc(session='2026-11-17', trade='2026-11-17', fallback='2026-11-23',
                listed=('2026-11-17', '2026-11-17', '2026-11-23', '2026-12-01'))
        p = sc.run()
        self.assertEqual(p['expiry_fallback']['status'], efb.STATUS_APPLIED, p.get('reasons'))
        self.assertEqual(p['contract']['expiry'], '2026-11-23')
        self.assertEqual(p['status'], 'ADMISSIBLE', p['reasons'])
        # NF on the holiday-shifted Monday expiry itself -> next distinct listing 1 Dec
        sc2 = Sc()
        self.assertEqual(sc2.run()['contract']['expiry'], '2026-12-01')

    def test_bnf_monthly_rule(self):
        self.assertEqual(efb.bnf_monthly_expiry(2026, 11).isoformat(), '2026-11-23')   # Tue 24 Nov holiday
        self.assertEqual(efb.bnf_monthly_expiry(2026, 12).isoformat(), '2026-12-29')
        self.assertEqual(efb.next_bnf_monthly_after('2026-11-23').isoformat(), '2026-12-29')

    def test_bnf_monthly_selected_through_fallback_and_weekly_skipped(self):
        sc = Sc('BNF', session='2026-11-23', trade='2026-11-23', fallback='2026-12-29',
                listed=('2026-11-23', '2026-12-01', '2026-12-29'))
        p = sc.run()
        fb = p['expiry_fallback']
        self.assertEqual(fb['status'], efb.STATUS_APPLIED, p.get('reasons'))
        self.assertEqual(p['contract']['expiry'], '2026-12-29')
        self.assertEqual(fb['resolution']['skipped'], ['2026-12-01'])
        self.assertEqual(p['contract']['lot_size'], 30)

    def test_codex_finding_2_bnf_non_monthly_never_selected_when_monthly_absent(self):
        """Codex R0 finding 2: with the monthly omitted from the listing, a non-monthly BNF expiry must not be
        selected -> WAIT BNF_MONTHLY_NOT_LISTED (no caller-supplied monthly input exists on this path)."""
        sc = Sc('BNF', session='2026-11-23', trade='2026-11-23', fallback='2026-12-01',
                listed=('2026-11-23', '2026-12-01', '2026-12-08'))
        p = sc.run()
        self.assertEqual(wait_reason(p), 'BNF_MONTHLY_NOT_LISTED')
        self.assertEqual(p['status'], 'BLOCKED')
        self.assertIn('EXPIRY_DAY_NO_NEXT_CHAIN', p['reasons'])
        self.assertNotEqual(p.get('contract', {}).get('expiry'), '2026-12-01')

    def test_bnf_calendar_uncovered_year_waits(self):
        r = efb.resolve_expiry('BNF', '2026-12-29', {'kind': 'listing', 'index': 'BNF',
                               'expiries': ['2026-12-29', '2027-01-26'],
                               'receipt_ms': ms(ia.parse_ts('2026-12-29T10:00:00+05:30'))},
                               ms(ia.parse_ts('2026-12-29T10:00:05+05:30')))
        self.assertEqual(r['reason'], 'BNF_MONTHLY_CALENDAR_UNAVAILABLE')

    def test_listing_unverifiable(self):
        sc = Sc()
        self.assertEqual(wait_reason(sc.run(recs=[])), 'LISTING_UNAVAILABLE')
        self.assertEqual(wait_reason(sc.run(recs=sc.records(listing_ix='BNF'))), 'LISTING_UNAVAILABLE')
        prev = [dict(sc.records()[0], receipt_ms=ms(sc.T) - 86400000)] + sc.records()[1:]
        self.assertEqual(wait_reason(sc.run(recs=prev)), 'LISTING_NOT_THIS_SESSION')
        none_later = Sc(listed=('2026-11-23',))
        self.assertEqual(wait_reason(none_later.run()), 'NO_LATER_LISTED_EXPIRY')


class CutoffClocks(unittest.TestCase):
    def test_codex_finding_1_receipt_500ms_after_cutoff_does_not_pass(self):
        """Codex R0 finding 1: a broker snapshot RECEIVED 500 ms after the selection cutoff must not support the
        verdict (it was not available).  The planner's own status is preserved: ELIGIBLE_PENDING_SNAPSHOT."""
        sc = Sc()
        cutoff = sc.cutoff
        recs = sc.records(snap_gen_s=4.5, snap_rc_s=5.5)       # generated before, RECEIVED 500 ms after cutoff
        p = sc.run(recs=recs, cutoff=cutoff)
        self.assertFalse(p['admit'])
        self.assertEqual(p['status'], 'ELIGIBLE_PENDING_SNAPSHOT')
        self.assertIn('BROKER_SNAPSHOT_UNAVAILABLE', p['reasons'])
        later = p['expiry_fallback']['later_evidence_rejected']
        self.assertEqual([x['kind'] for x in later], ['snapshot'])
        # the same snapshot offered through the store is rejected by the cutoff rule as well
        s = recs[-1]['snapshot']
        p2 = sc.run(recs=recs[:-1], cutoff=cutoff, store_snapshots={s['instrument_key']: s})
        self.assertEqual(p2['status'], 'ELIGIBLE_PENDING_SNAPSHOT')
        self.assertIn('SNAPSHOT_RECEIVED_AFTER_CUTOFF', p2['expiry_fallback']['snapshot_rejections'])
        # received exactly at the cutoff: available
        p3 = sc.run(recs=sc.records(snap_gen_s=4.5, snap_rc_s=5.0), cutoff=cutoff)
        self.assertEqual(p3['status'], 'ADMISSIBLE', p3['reasons'])

    def test_later_listing_chain_contracts_rejected(self):
        sc = Sc()
        self.assertEqual(wait_reason(sc.run(recs=sc.records(listing_s=5.5))), 'LISTING_UNAVAILABLE')
        self.assertEqual(wait_reason(sc.run(recs=sc.records(chain_s=5.5))), 'FALLBACK_CHAIN_UNAVAILABLE')
        self.assertEqual(wait_reason(sc.run(recs=sc.records(contracts_s=5.5))), 'FALLBACK_CONTRACTS_UNAVAILABLE')
        r = efb.resolve_expiry('NF', sc.session, sc.records(listing_s=5.5)[0], sc.cutoff)
        self.assertEqual(r['reason'], 'LISTING_RECEIVED_AFTER_CUTOFF')
        c, why = efb.check_chain('NF', sc.fallback, sc.records(chain_s=5.5)[1], sc.cutoff)
        self.assertEqual(why, 'FALLBACK_CHAIN_RECEIVED_AFTER_CUTOFF')

    def test_broker_generation_after_cutoff_rejected(self):
        sc = Sc()
        recs = sc.records(snap_gen_s=11.0, snap_rc_s=4.0)        # generation 6 s past cutoff (> 5 s skew)
        s = recs[-1]['snapshot']
        got, why = efb.snapshot_at_cutoff(s, s['instrument_key'], sc.cutoff)
        self.assertIsNone(got)
        self.assertEqual(why, 'SNAPSHOT_GENERATED_AFTER_CUTOFF')


class StaleMissingWrong(unittest.TestCase):
    def test_stale_and_missing_evidence(self):
        sc = Sc()
        stale_chain = sc.run(recs=sc.records(chain_s=3.0), cutoff=ms(sc.T) + 3000 + efb.CHAIN_MAX_AGE_S * 1000 + 1)
        self.assertEqual(wait_reason(stale_chain), 'FALLBACK_CHAIN_STALE_AT_CUTOFF')
        self.assertEqual(wait_reason(sc.run(recs=sc.records()[:1])), 'FALLBACK_CHAIN_UNAVAILABLE')
        no_meta = [r for r in sc.records() if r['kind'] != 'contracts']
        self.assertEqual(wait_reason(sc.run(recs=no_meta)), 'FALLBACK_CONTRACTS_UNAVAILABLE')
        no_snap = sc.run(recs=sc.records(snapshot=False))
        self.assertEqual(no_snap['status'], 'ELIGIBLE_PENDING_SNAPSHOT')            # planner status preserved
        self.assertEqual(no_snap['reasons'], ['BROKER_SNAPSHOT_UNAVAILABLE'])
        old = sc.records(snap_gen_s=-95.0, snap_rc_s=-94.5)                          # broker snapshot 100 s old
        p = sc.run(recs=old)
        self.assertEqual(p['status'], 'ELIGIBLE_PENDING_SNAPSHOT')
        self.assertEqual(p['reasons'], ['SNAPSHOT_STALE'])
        self.assertEqual(wait_reason(sc.run(capital=None)), 'CAPITAL_STATE_UNRECORDED')

    def test_wrong_index_or_instrument(self):
        sc = Sc()
        self.assertEqual(wait_reason(sc.run(recs=sc.records(chain_ix='BNF'))), 'FALLBACK_CHAIN_UNAVAILABLE')
        bad = stamped_chain('NF', sc.fallback)
        for sides in bad['strikes'].values():
            sides['CE']['underlying_key'] = UNDER['BNF']
        self.assertEqual(wait_reason(sc.run(recs=sc.records(chain=bad, snapshot=False))),
                         'FALLBACK_CHAIN_LEG_INDEX_MISMATCH')
        wrong_exp = stamped_chain('NF', '2026-12-08')
        wrong_exp['requested_expiry'] = sc.fallback
        wrong_exp['expiry'] = sc.fallback
        self.assertEqual(wait_reason(sc.run(recs=sc.records(chain=wrong_exp, snapshot=False))),
                         'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH')
        # contract metadata of another underlying is rejected by managed_evidence_v1 (never borrowed) -> WAIT
        p = sc.run(recs=sc.records(contracts_underlying=UNDER['BNF']))
        self.assertEqual(wait_reason(p), 'FALLBACK_CONTRACTS_NONE_FOR_EXPIRY')
        # a snapshot for a different instrument is never used for the proposal
        recs = sc.records()
        other = [k for k in (v['PE']['instrument_key'] for v in sc.chain['strikes'].values())][0]
        s = snap(sc.chain, other, sc.T + timedelta(seconds=3), sc.T + timedelta(seconds=4))
        recs[-1] = {'kind': 'snapshot', 'index': 'NF', 'expiry': sc.fallback, 'instrument_key': other,
                    'receipt_ms': s['receipt_ms'], 'snapshot': s}
        p = sc.run(recs=recs)
        self.assertEqual(p['status'], 'ELIGIBLE_PENDING_SNAPSHOT')
        self.assertNotEqual(p['instrument_key'], other)


class ExistingGates(unittest.TestCase):
    def test_low_capital_blocks_via_planner(self):
        sc = Sc()
        ok = sc.run()
        self.assertEqual(ok['status'], 'ADMISSIBLE', ok['reasons'])
        low = ok['capital_required'] * 100.0 / OK['max_risk_pct'] * 0.5
        p = sc.run(capital=low)
        self.assertEqual(p['status'], 'BLOCKED')
        self.assertIn('AFFORDABILITY_FAILED', p['reasons'])
        self.assertEqual(p['expiry_fallback']['status'], efb.STATUS_APPLIED)

    def test_occupied_exposure(self):
        sc = Sc()
        occ = [{'index': 'NF', 'policy_id': 'index_single_leg_v1', 'status': 'OPEN'}]
        base = sc.base(single_leg_positions=occ)
        self.assertEqual(wait_reason(base), 'EXPIRY_INDEPENDENT_GATE')
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', base['expiry_fallback']['gates'])
        p = sc.run(single_leg_positions=occ)          # even if evidence arrives, the planner refuses
        self.assertEqual(p['status'], 'BLOCKED')
        self.assertIn('MANAGED_POLICY_CAP_ONE_PER_INDEX', p['reasons'])

    def test_exit_before_contract_expiry(self):
        self.assertTrue(efb.exit_before_expiry('2026-11-23', '2026-12-01'))
        self.assertFalse(efb.exit_before_expiry('2026-11-23', '2026-11-23', time_exit=(15, 45)))
        self.assertFalse(efb.exit_before_expiry('2026-11-23', None))


class Retention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, 'single_leg_v1')
        os.makedirs(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bounded_retention_round_trips_through_the_reader(self):
        sc = Sc()
        recs = sc.records()
        p = sc.run(recs=recs)
        out = efb.retain(self.d, sc.session, 'NF', recs, {'decision_id': sc.dec['decision_id'], 'status': 'APPLIED'},
                         {'requests': {'listing': 1, 'contracts': 1, 'chain': 1},
                          'response_bytes': {'listing': 1000, 'contracts': 2000, 'chain': 3000}}, sc.spot)
        self.assertLessEqual(out['retained_bytes'], efb.MAX_RETAINED_BYTES)
        doc = efb.read_retained(self.d)
        ch = [r for r in doc['indices']['NF']['records'] if r['kind'] == 'chain'][0]
        self.assertLessEqual(len(ch['chain']['strikes']), 2 * efb.RETAIN_STRIKES_EACH_SIDE)
        # the retained (bounded) evidence replays to the same verdict through the same reader
        p2 = sc.run(reader=efb.EvidenceReader.from_doc(doc))
        self.assertEqual((p2['status'], p2['instrument_key'], p2['contract']['expiry']),
                         (p['status'], p['instrument_key'], p['contract']['expiry']))
        s = efb.summary(self.d)
        self.assertEqual(s['NF']['stats'][sc.session]['requests'], {'listing': 1, 'contracts': 1, 'chain': 1})
        self.assertNotIn('BNF', s)
        chain_rec = [r for r in recs if r['kind'] == 'chain'][0]
        for i in range(efb.MAX_RECORDS_PER_INDEX + 5):
            efb.retain(self.d, sc.session, 'NF', [dict(chain_rec, receipt_ms=chain_rec['receipt_ms'] + i + 1)], {}, {},
                       sc.spot)
        kept = efb.read_retained(self.d)['indices']['NF']['records']
        self.assertLessEqual(len(kept), efb.MAX_RECORDS_PER_INDEX)
        # trimming drops the oldest chains first: the listing and contracts stay, so later chains still replay
        self.assertTrue({'listing', 'contracts', 'chain'} <= {r['kind'] for r in kept})
        late = sc.run(reader=efb.EvidenceReader.from_doc(efb.read_retained(self.d)),
                      cutoff=chain_rec['receipt_ms'] + efb.MAX_RECORDS_PER_INDEX + 10)
        self.assertEqual(late['expiry_fallback']['status'], efb.STATUS_APPLIED, late.get('reasons'))


class Integration(unittest.TestCase):
    """Analysis (brain.analyze on BNF's own expiry day, synthetic polls) -> native-shaped evidence -> apply ->
    published proposal -> quote targets -> admission (stale / replaced approval rejected) -> restart -> exit on
    the exact fallback contract.  SYNTHETIC; the native acquisition itself is covered by the JVM test."""
    SESSION = '2026-11-23'               # Tue 24 Nov 2026 holiday shifts BNF monthly expiry to Monday
    NEXT = '2026-12-29'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.sd = os.path.join(cls.tmp, 'index_authority_v1')
        cls.d = os.path.join(cls.tmp, 'single_leg_v1')
        os.makedirs(cls.d)
        t = F.times('09:15', '10:00')
        polls = F.polls(F.linear(24000, 25, len(t)), F.linear(52000, 60, len(t)), t)
        out = None
        for i in range(len(polls)):
            c = F.ctx(polls[i]['nfSpot'], polls[i]['bnfSpot'], indexAuthorityStateDir=cls.sd, session_date=cls.SESSION,
                      today_ist=cls.SESSION, bnfExpiry=cls.SESSION, singleLegPaperEnabled=True, singleLegStateDir=cls.d,
                      bnfScaleChain=F.chain(polls[i]['bnfSpot'], 100, lot=30), bnfScaleExpiry=cls.NEXT)
            out = F.run(brain, polls[:i + 1], c)
        cls.ctx = c
        cls.result = out

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def evidence(self, at, spot, chain_s=3.0):
        ch = stamped_chain('BNF', self.NEXT, spot)
        ev = {'BNF': {'listing': {'index': 'BNF', 'session': self.SESSION,
                                 'expiries': [self.SESSION, self.NEXT],
                                 'weekly': {self.SESSION: False, self.NEXT: False},
                                 'request_start_ms': ms(at) + 1500, 'receipt_ms': ms(at) + 2000},
                     'contracts': {'index': 'BNF', 'expiry': self.NEXT, 'fetched_at_ms': ms(at) + 2500,
                                   'response': TB.contracts_response(ch, 'BNF', expiry=self.NEXT, lot=30)},
                     'chain': {'index': 'BNF', 'expiry': self.NEXT, 'chain': ch,
                               'request_start_ms': ms(at) + int(chain_s * 1000) - 400,
                               'receipt_ms': ms(at) + int(chain_s * 1000)},
                     'stats': {'requests': {'listing': 1, 'contracts': 1, 'chain': 1},
                               'response_bytes': {'listing': 10, 'contracts': 20, 'chain': 30}}}}
        return ev, ch

    def test_end_to_end(self):
        r = copy.deepcopy(self.result)
        base = r['single_leg_by_index']['BNF']
        self.assertEqual(base['action'], 'BUY_CE')
        self.assertEqual(base['expiry_fallback']['status'], efb.STATUS_REQUIRED)
        bnf = r['single_leg_by_index'].get('NF') or {}
        self.assertNotIn('expiry_fallback', bnf)                          # retired NF has no active proposal
        dec = r['by_index']['BNF']
        at = ia.parse_ts(dec['analysis_ts'])
        spot = dec['direction']['inputs']['spot']['value']
        ev, ch = self.evidence(at, spot)
        cutoff = ms(at) + 5000
        out = json.loads(brain.single_leg_expiry_fallback_apply(self.d, json.dumps(r), json.dumps(self.ctx),
                                                                json.dumps(ev), cutoff, '[]'))
        self.assertNotIn('error', out, out)
        p = out['single_leg_by_index']['BNF']
        self.assertEqual(p['status'], 'ELIGIBLE_PENDING_SNAPSHOT', p['reasons'])
        self.assertEqual(p['contract']['expiry'], self.NEXT)
        key = p['instrument_key']
        self.assertTrue(key.startswith('NSE_FO|BNF20261229'))
        self.assertEqual(p['quote_needed'], [key])
        self.assertEqual(out['retention']['BNF']['index_bytes'] > 0, True)
        # publication: the patched result carries the fallback proposal; quote targets name the exact key
        r['single_leg_by_index']['BNF'] = p
        tg = json.loads(brain.single_leg_quote_targets(self.d, json.dumps(r)))
        self.assertIn(key, tg['keys'])
        # stale approval: a quote older than 90 s at the tap is rejected
        t_tap = at + timedelta(seconds=60)
        old = snap(ch, key, at - timedelta(seconds=40), at - timedelta(seconds=39))
        rej = sls.admit(self.d, r, 'BNF', clock=lambda: t_tap, snapshot=old, **OK)
        self.assertEqual(rej['status'], 'REJECTED')
        self.assertIn('SNAPSHOT_STALE_AT_ADMISSION', rej['reasons'])
        # replaced approval: the published decision changed (state probe) -> rejected before commit
        fresh = snap(ch, key, t_tap - timedelta(seconds=2), t_tap - timedelta(seconds=1))
        probe = lambda: json.dumps({'decisions': {'BNF': {'decision_id': 'idx1_newer', 'instrument_key': key}},
                                    'enabled': True, 'paper_mode': True, 'selector': 'BOTH',
                                    'capital': OK['capital'], 'max_risk_pct': OK['max_risk_pct']})
        rep = sls.admit(self.d, r, 'BNF', clock=lambda: t_tap, snapshot=fresh, state_probe=probe, **OK)
        self.assertEqual(rep['reason'], 'DECISION_CHANGED_BEFORE_COMMIT')
        # a different instrument (trading-expiry strike) published -> also rejected
        probe2 = lambda: json.dumps({'decisions': {'BNF': {'decision_id': dec['decision_id'], 'instrument_key': 'X|other'}},
                                     'enabled': True, 'paper_mode': True, 'selector': 'BOTH',
                                     'capital': OK['capital'], 'max_risk_pct': OK['max_risk_pct']})
        self.assertEqual(sls.admit(self.d, r, 'BNF', clock=lambda: t_tap, snapshot=fresh, state_probe=probe2,
                                   **OK)['reason'], 'DECISION_CHANGED_BEFORE_COMMIT')
        # too old a decision (> ADMIT_MAX_DECISION_AGE_S) -> rejected
        t_old = at + timedelta(seconds=sls.ADMIT_MAX_DECISION_AGE_S + 5)
        self.assertEqual(sls.admit(self.d, r, 'BNF', clock=lambda: t_old,
                                   snapshot=snap(ch, key, t_old - timedelta(seconds=2), t_old - timedelta(seconds=1)),
                                   **OK)['reason'], 'DECISION_NOT_CURRENT')
        # exact admission
        okp = sls.admit(self.d, r, 'BNF', clock=lambda: t_tap, snapshot=fresh, **OK)
        self.assertEqual(okp['status'], 'ADMITTED', okp)
        pos = okp['position']
        self.assertEqual((pos['index'], pos['expiry'], pos['instrument_key'], pos['strike'], pos['option_type']),
                         ('BNF', self.NEXT, key, p['strike'], 'CE'))
        self.assertEqual(pos['expiry_fallback']['fallback_expiry'], self.NEXT)
        self.assertEqual(pos['expiry_fallback']['trade_expiry'], self.SESSION)
        # restart: a fresh read of the durable store (no in-memory state) still names the exact contract
        reopened = sls.open_positions(self.d)
        self.assertEqual([(x['instrument_key'], x['expiry']) for x in reopened], [(key, self.NEXT)])
        # time exit at 15:05 on the exact instrument; a quote for the trading-expiry strike never closes it
        t_exit = ia.parse_ts(self.SESSION + 'T15:05:30+05:30')
        trading_key = 'NSE_FO|BNF%s%s' % (self.SESSION.replace('-', ''), str(p['strike']) + 'CE')
        tq = snap(stamped_chain('BNF', self.SESSION, spot), trading_key, t_exit - timedelta(seconds=5),
                  t_exit - timedelta(seconds=4))
        m1 = sls.monitor(self.d, {trading_key: tq}, {}, clock=lambda: t_exit)
        self.assertEqual(m1['events'][0]['event'], 'EXIT_PENDING')
        q = snap(ch, key, t_exit - timedelta(seconds=5), t_exit - timedelta(seconds=4))
        m2 = sls.monitor(self.d, {key: q}, {}, clock=lambda: t_exit)
        self.assertEqual(m2['events'][0]['event'], 'EXIT', m2)
        closed = sls.read(self.d)['closed'][-1]
        self.assertEqual(closed.get('instrument_key', key), key)
        # R2 (Codex R1 reply s3.6): the exit record names the contract expiry unambiguously; trade_expiry stays the
        # ORIGINAL trading expiry (== why.exp)
        self.assertEqual(closed['contract_expiry'], self.NEXT)
        self.assertEqual(closed['expiry_fallback']['trade_expiry'], self.SESSION)
        self.assertEqual(closed['expiry_fallback']['trade_expiry_meaning'], 'ORIGINAL_TRADING_EXPIRY')
        self.assertEqual(closed['expiry_fallback']['contract_expiry'], self.NEXT)
        self.assertEqual(p['expiry_fallback']['contract_expiry'], self.NEXT)
        self.assertEqual(p['expiry_fallback']['trade_expiry_meaning'], 'ORIGINAL_TRADING_EXPIRY')
        self.assertLess(t_exit, ia.parse_ts(self.NEXT + 'T15:30:00+05:30'))

    def test_decision_changed_between_analysis_and_apply_is_skipped(self):
        r = copy.deepcopy(self.result)
        r['by_index']['BNF']['decision_id'] = 'idx1_replaced'
        at = ia.parse_ts(r['by_index']['BNF']['analysis_ts'])
        ev, _ = self.evidence(at, r['by_index']['BNF']['direction']['inputs']['spot']['value'])
        out = json.loads(brain.single_leg_expiry_fallback_apply(self.d, json.dumps(r), json.dumps(self.ctx),
                                                                json.dumps(ev), ms(at) + 5000, '[]'))
        self.assertEqual(out['skipped'], {'BNF': 'DECISION_CHANGED'})
        self.assertEqual(out['single_leg_by_index'], {})


if __name__ == '__main__':
    unittest.main()
