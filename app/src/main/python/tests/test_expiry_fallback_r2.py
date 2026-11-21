"""expiry_fallback_v1 R2 regressions (Codex R1 reply 21 Nov 2026).  SYNTHETIC fixtures, deterministic clocks, no
network.  Section 1 tests are written against APIs that already existed in R1 (retain / EvidenceReader.from_file /
plan_fallback) so they FAIL on R1 (7d961ff8) by assertion and pass on R2; the R1 failure output is recorded in the
R2 report.  Later sections cover s2 (export part), s3 (BNF identity, capital source, refresh withdrawal, labels,
stable WAIT codes) and s4 (deadline)."""
import copy
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

import brain
import expiry_fallback_v1 as efb
import ev_v12_calendar as cal
import index_authority_v1 as ia
import session_evidence_v1 as se
import single_leg_store_v1 as sls
import test_expiry_fallback_v1 as T1
import test_index_single_leg_v1_delivery_b as TB
from test_expiry_fallback_v1 import SPEC, Sc, ms, snap, stamped_chain
from test_single_leg_store_e2e_x1_identity import OK

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..', '..'))

CASES = {
    # Codex s1 reproduction shape: NF 2026-11-23 own expiry day, fallback 2026-12-01, one cached metadata response
    'NF': dict(session='2026-11-23', trade='2026-11-23', fallback='2026-12-01',
               listed=('2026-11-23', '2026-12-01', '2026-12-08'), spots=(24000.0, 24800.0, 23300.0), wide=24000.0),
    # BNF own (holiday-shifted) monthly expiry 2026-11-23; next monthly 2026-12-29
    'BNF': dict(session='2026-11-23', trade='2026-11-23', fallback='2026-12-29',
                listed=('2026-11-23', '2026-12-29', '2027-01-26'), spots=(52000.0, 54000.0, 50200.0), wide=52000.0),
}
HOURS = ((10, 0), (13, 0), (14, 0))


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def at_ms(session, hm, s=0):
    return ms(ia.parse_ts('%sT%02d:%02d:%02d+05:30' % (session, hm[0], hm[1], s)))


def listing_rec(ix, c, receipt_ms):
    return {'kind': 'listing', 'index': ix, 'expiries': list(c['listed']),
            'weekly': {e: (e != c['fallback']) for e in c['listed']},
            'request_start_ms': receipt_ms - 300, 'receipt_ms': receipt_ms}


def contracts_rec(ix, c, receipt_ms, chain=None):
    ch = chain or stamped_chain(ix, c['fallback'], c['wide'])
    return {'kind': 'contracts', 'index': ix, 'expiry': c['fallback'], 'receipt_ms': receipt_ms,
            'response': TB.contracts_response(ch, ix, expiry=c['fallback'], lot=SPEC[ix]['lot'])}


class Poll:
    """One synthetic poll: decision at hm with `spot`; fresh chain; exact snapshot(s); shared listing/metadata."""

    def __init__(self, ix, c, hm, spot, listing, contracts, value='BULL', snaps=None, tag=''):
        self.sc = Sc(ix, c['session'], c['trade'], c['fallback'], c['listed'], value=value, hm=hm)
        self.sc.spot = spot
        self.sc.dec['direction']['inputs']['spot']['value'] = spot
        self.sc.dec['decision_id'] = 'idx1_%s_%02d%02d%s' % (ix, hm[0], hm[1], tag)
        self.sc.chain = stamped_chain(ix, c['fallback'], spot)
        self.base = self.sc.base()
        T = self.sc.T
        self.recs = [listing, {'kind': 'chain', 'index': ix, 'expiry': c['fallback'], 'chain': self.sc.chain,
                               'request_start_ms': ms(T) + 2600, 'receipt_ms': ms(T) + 3000}]
        if contracts is not None:
            self.recs.append(contracts)
        if snaps is None:
            key = self.sc.k0_key()
            s = snap(self.sc.chain, key, T + timedelta(seconds=3.5), T + timedelta(seconds=4))
            snaps = [{'kind': 'snapshot', 'index': ix, 'expiry': c['fallback'], 'instrument_key': key,
                      'receipt_ms': s['receipt_ms'], 'snapshot': s}]
        self.recs += snaps

    def plan(self, reader):
        return self.sc.run(reader=reader, base=copy.deepcopy(self.base))


def verdict(p):
    fb = p.get('expiry_fallback') or {}
    return (p.get('status'), p.get('instrument_key'), (p.get('contract') or {}).get('expiry'),
            tuple(p.get('reasons') or []), fb.get('status'), fb.get('reason'))


def outcome_for(poll, p):
    if hasattr(efb, 'outcome_record'):
        return efb.outcome_record(poll.sc.dec['decision_id'], p, poll.sc.cutoff, poll.sc.spot, poll.sc.dec)
    efb_ = p.get('expiry_fallback') or {}
    return {'decision_id': poll.sc.dec['decision_id'], 'status': efb_.get('status'), 'reason': efb_.get('reason'),
            'fallback_expiry': efb_.get('fallback_expiry'), 'instrument_key': p.get('instrument_key')}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, 'single_leg_v1')
        os.makedirs(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self):
        return os.path.join(self.d, efb.RETAIN_DIR, efb.RETAIN_DOC)

    def live_and_retain(self, ix, polls):
        live = []
        for pl in polls:
            p = pl.plan(efb.EvidenceReader(pl.recs))
            live.append(p)
            efb.retain(self.d, pl.sc.session, ix, pl.recs, outcome_for(pl, p),
                       {'requests': {'chain': 1}, 'response_bytes': {'chain': 1000}}, pl.sc.spot)
        return live


# ---------------------------------------------------------------------------------------------------------------
# s1: metadata retention (must FAIL on R1 7d961ff8, PASS on R2)
# ---------------------------------------------------------------------------------------------------------------
class S1CachedMetadataReuse(Base):
    def test_selected_strike_moves_beyond_first_window_both_directions_nf_and_bnf_restart(self):
        """One cached contracts response (original receipt 09:30) reused by polls whose selected strike moves up
        and then down beyond the first poll's retention window; the saved file (restart) replays every poll to the
        live contract, status and reasons."""
        for ix, c in CASES.items():
            with self.subTest(index=ix):
                shutil.rmtree(self.d); os.makedirs(self.d)
                lst = listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
                meta = contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
                polls = [Poll(ix, c, hm, sp, lst, meta) for hm, sp in zip(HOURS, c['spots'])]
                strikes = [pl.sc.k0_key() for pl in polls]
                self.assertEqual(len(set(strikes)), 3)                   # the selected contract really moves
                live = self.live_and_retain(ix, polls)
                for p in live:
                    self.assertEqual(p['expiry_fallback']['status'], efb.STATUS_APPLIED, p['reasons'])
                    self.assertNotIn('CONTRACT_METADATA_UNAVAILABLE', p['reasons'])
                reader = efb.EvidenceReader.from_file(self.path())          # restart: only the saved file
                for pl, p in zip(polls, live):
                    self.assertEqual(verdict(pl.plan(reader)), verdict(p), pl.sc.dec['decision_id'])
                doc = load(self.path())
                metas = [r for r in doc['indices'][ix]['records'] if r['kind'] == 'contracts']
                self.assertEqual([m['receipt_ms'] for m in metas], [meta['receipt_ms']])   # one original response
                cov = [o['replay_coverage']['status'] for o in doc['indices'][ix]['outcomes']]
                self.assertEqual(cov, ['REPLAYABLE'] * 3)

    def test_codex_reproduction_nf_10_00_and_13_00(self):
        """Codex s1: metadata received 09:30 covering 23,400-25,650; polls 10:00 (spot 24,000) and 13:00
        (spot 24,800).  Live 13:00 is ELIGIBLE/ADMISSIBLE; the retained replay must not be BLOCKED for metadata."""
        c = CASES['NF']
        wide = stamped_chain('NF', c['fallback'], 24525.0)
        wide['strikes'] = {k: v for k, v in wide['strikes'].items() if 23400 <= float(k) <= 25650}
        lst = listing_rec('NF', c, at_ms(c['session'], (9, 30), 2))
        meta = contracts_rec('NF', c, at_ms(c['session'], (9, 30), 3), chain=wide)
        polls = [Poll('NF', c, (10, 0), 24000.0, lst, meta), Poll('NF', c, (13, 0), 24800.0, lst, meta)]
        live = self.live_and_retain('NF', polls)
        self.assertIn(live[1]['status'], ('ELIGIBLE_PENDING_SNAPSHOT', 'ADMISSIBLE'))
        replay = polls[1].plan(efb.EvidenceReader.from_file(self.path()))
        self.assertNotIn('CONTRACT_METADATA_UNAVAILABLE', replay['reasons'])
        self.assertEqual(verdict(replay), verdict(live[1]))


class S1InstrumentIdentity(Base):
    def test_distinct_snapshot_keys_sharing_one_receipt_are_both_retained(self):
        """One v3 batch (single receipt) returns the CE and the PE at K0.  Poll 1 (BULL) uses (and retains) the CE,
        poll 2 (BEAR, 30 s later) uses the PE from the same batch.  R1's dedup key (kind, expiry, receipt_ms) has no
        instrument identity, so the PE was discarded as a "duplicate" and poll 2 could not be replayed."""
        for ix, c in CASES.items():
            with self.subTest(index=ix):
                shutil.rmtree(self.d); os.makedirs(self.d)
                lst = listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
                meta = contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
                p1 = Poll(ix, c, (10, 0), c['spots'][0], lst, meta, snaps=[])
                T = p1.sc.T
                k = p1.sc.k0_key()
                kp = k[:-2] + 'PE'
                batch = []
                for key in (k, kp):
                    s = snap(p1.sc.chain, key, T + timedelta(seconds=3.5), T + timedelta(seconds=4))
                    batch.append({'kind': 'snapshot', 'index': ix, 'expiry': c['fallback'], 'instrument_key': key,
                                  'receipt_ms': s['receipt_ms'], 'snapshot': s})
                self.assertEqual(batch[0]['receipt_ms'], batch[1]['receipt_ms'])
                # production path (brain apply): each poll retains only the snapshot its verdict USED
                p1.recs += batch[:1]
                p2 = Poll(ix, c, (10, 0), c['spots'][0], lst, meta, value='BEAR', snaps=batch[1:], tag='b')
                p2.sc.T = T + timedelta(seconds=30)
                p2.sc.cutoff = ms(p2.sc.T) + 5000
                p2.recs[1] = dict(p2.recs[1], request_start_ms=ms(p2.sc.T) + 2600, receipt_ms=ms(p2.sc.T) + 3000)
                live = self.live_and_retain(ix, [p1, p2])
                self.assertEqual(live[1]['instrument_key'], kp)
                reader = efb.EvidenceReader.from_file(self.path())
                self.assertEqual(verdict(p2.plan(reader)), verdict(live[1]))
                keys = sorted(r['instrument_key'] for r in load(self.path())['indices'][ix]['records']
                              if r['kind'] == 'snapshot')
                self.assertEqual(keys, sorted([k, kp]))


class S1LaterMetadata(Base):
    def test_genuinely_later_metadata_cannot_support_an_earlier_cutoff(self):
        """Response A (09:30) lacks the strikes near 24,800; response B (13:00, a NEW fetch) has them.  The 10:00
        decision at spot 24,800 must replay exactly as live (metadata unavailable) even though B is retained;
        A and B are never merged (different original receipts)."""
        c = CASES['NF']
        narrow = stamped_chain('NF', c['fallback'], 24000.0)
        narrow['strikes'] = {k: v for k, v in narrow['strikes'].items() if float(k) <= 24300}
        lst = listing_rec('NF', c, at_ms(c['session'], (9, 30), 2))
        a = contracts_rec('NF', c, at_ms(c['session'], (9, 30), 3), chain=narrow)
        b = contracts_rec('NF', c, at_ms(c['session'], (13, 0), 1))
        early = Poll('NF', c, (10, 0), 24800.0, lst, a)
        late = Poll('NF', c, (13, 0), 24800.0, lst, b)
        live = self.live_and_retain('NF', [early, late])
        self.assertIn('CONTRACT_METADATA_UNAVAILABLE', live[0]['reasons'])
        self.assertNotIn('CONTRACT_METADATA_UNAVAILABLE', live[1]['reasons'])
        reader = efb.EvidenceReader.from_file(self.path())
        self.assertEqual(verdict(early.plan(reader)), verdict(live[0]))
        self.assertEqual(verdict(late.plan(reader)), verdict(live[1]))
        metas = sorted(r['receipt_ms'] for r in load(self.path())['indices']['NF']['records']
                       if r['kind'] == 'contracts')
        self.assertEqual(metas, sorted([a['receipt_ms'], b['receipt_ms']]))


class S1TrimCoverage(Base):
    def _run(self, ix, n):
        c = CASES[ix]
        lst = listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
        meta = contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
        step = SPEC[ix]['step']
        polls = [Poll(ix, c, (10 + i // 4, (i % 4) * 15), c['wide'] + step * ((i % 5) - 2) * 3, lst, meta)
                 for i in range(n)]
        return polls, self.live_and_retain(ix, polls)

    def _check(self, ix, polls, live):
        doc = load(self.path())
        reader = efb.EvidenceReader.from_doc(doc)
        by_id = {pl.sc.dec['decision_id']: (pl, p) for pl, p in zip(polls, live)}
        seen = {'REPLAYABLE': 0, 'INCOMPLETE': 0}
        for o in doc['indices'][ix]['outcomes']:
            cov = o['replay_coverage']
            seen[cov['status']] += 1
            pl, p = by_id[o['decision_id']]
            if cov['status'] == 'REPLAYABLE':            # advertised replayable -> exact replay
                self.assertEqual(verdict(pl.plan(reader)), verdict(p), o['decision_id'])
            else:
                self.assertTrue(cov['reasons'])
        return doc, seen

    def test_record_cap_keeps_dependencies_or_reports_incomplete(self):
        for ix in ('NF', 'BNF'):
            with self.subTest(index=ix), mock.patch.object(efb, 'MAX_RECORDS_PER_INDEX', 12):
                shutil.rmtree(self.d); os.makedirs(self.d)
                polls, live = self._run(ix, 10)
                doc, seen = self._check(ix, polls, live)
                self.assertLessEqual(len(doc['indices'][ix]['records']), 12)
                self.assertGreater(seen['REPLAYABLE'], 0)
                self.assertGreater(seen['INCOMPLETE'], 0)
                drops = doc['indices'][ix]['drops'][CASES[ix]['session']]
                self.assertTrue(any(k.startswith('RECORD_CAP:') for k in drops), drops)
                # the shared listing and metadata (referenced by every outcome) are never the first to go
                kinds = {r['kind'] for r in doc['indices'][ix]['records']}
                self.assertTrue({'listing', 'contracts'} <= kinds)

    def test_byte_cap_keeps_dependencies_or_reports_incomplete(self):
        with mock.patch.object(efb, 'MAX_RETAINED_BYTES', 30_000):
            polls, live = self._run('NF', 10)
            doc, seen = self._check('NF', polls, live)
            self.assertLessEqual(os.path.getsize(self.path()), 30_000)
            self.assertGreater(seen['REPLAYABLE'], 0)
            self.assertGreater(seen['INCOMPLETE'], 0)
            drops = doc['indices']['NF']['drops'][CASES['NF']['session']]
            self.assertTrue(any(k.startswith('BYTE_CAP:') for k in drops), drops)


class S1ReducedLegFields(Base):
    def test_retained_leg_identity_reaches_the_same_check_chain_verdict(self):
        """R2 keeps per-leg contract_expiry / underlying_key only when they differ from the request; a chain whose
        leg names another expiry or index still replays to the same WAIT, and a clean chain to the same plan."""
        c = CASES['NF']
        lst = listing_rec('NF', c, at_ms(c['session'], (9, 30), 2))
        meta = contracts_rec('NF', c, at_ms(c['session'], (9, 30), 3))
        ok = Poll('NF', c, (10, 0), 24000.0, lst, meta)
        bad_exp = Poll('NF', c, (11, 0), 24000.0, lst, meta)
        k = ok.sc.k0_key()[len('NSE_FO|NF20261201'):-2]
        bad_exp.recs[1]['chain']['strikes'][k]['CE']['contract_expiry'] = '2026-12-08'
        bad_ix = Poll('NF', c, (12, 0), 24000.0, lst, meta)
        bad_ix.recs[1]['chain']['strikes'][k]['PE']['underlying_key'] = 'NSE_INDEX|Nifty Bank'
        live = self.live_and_retain('NF', [ok, bad_exp, bad_ix])
        self.assertEqual([p['expiry_fallback'].get('reason') for p in live],
                         [None, 'FALLBACK_CHAIN_LEG_EXPIRY_MISMATCH', 'FALLBACK_CHAIN_LEG_INDEX_MISMATCH'])
        reader = efb.EvidenceReader.from_file(self.path())
        for pl, p in zip((ok, bad_exp, bad_ix), live):
            self.assertEqual(verdict(pl.plan(reader)), verdict(p))
        legs = [leg for r in load(self.path())['indices']['NF']['records'] if r['kind'] == 'chain'
                for sides in r['chain']['strikes'].values() for leg in sides.values()]
        self.assertEqual(sum('contract_expiry' in leg for leg in legs), 1)
        self.assertEqual(sum('underlying_key' in leg for leg in legs), 1)
        self.assertTrue(all(set(leg) <= set(efb.RETAIN_LEG_FIELDS) for leg in legs))

# ---------------------------------------------------------------------------------------------------------------
# s3: BNF monthly identity, labels, stable codes, capital source, withdrawal
# ---------------------------------------------------------------------------------------------------------------
NSE_FAOP_71777_2026 = ('2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31', '2026-04-03', '2026-04-14',
                       '2026-05-01', '2026-05-28', '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20',
                       '2026-11-10', '2026-11-24', '2026-12-25')   # transcribed from the circular PDF, 21 Nov 2026


class S3BnfIdentity(unittest.TestCase):
    def lst(self, weekly, session='2026-11-23', listed=('2026-11-23', '2026-12-29')):
        rc = at_ms(session, (9, 30))
        return {'kind': 'listing', 'index': 'BNF', 'expiries': list(listed), 'weekly': weekly,
                'request_start_ms': rc - 300, 'receipt_ms': rc}, rc + 1000

    def test_monthly_identity_requires_weekly_false(self):
        for weekly, want in (({'2026-12-29': False}, None),
                             ({'2026-12-29': True}, 'BNF_MONTHLY_LISTED_AS_WEEKLY'),
                             ({'2026-12-29': 'MIXED'}, 'BNF_MONTHLY_IDENTITY_UNVERIFIED'),
                             ({}, 'BNF_MONTHLY_IDENTITY_UNVERIFIED'), (None, 'BNF_MONTHLY_IDENTITY_UNVERIFIED')):
            li, cut = self.lst(weekly)
            r = efb.resolve_expiry('BNF', '2026-11-23', li, cut)
            self.assertEqual(r['reason'], want, weekly)
            self.assertEqual(r['expiry'], '2026-12-29' if want is None else None)
            self.assertEqual(r['source']['rule_circular'][:15], 'NSE/FAOP/68747 ')

    def test_rule_not_effective_before_2025_09_01_and_uncovered_year(self):
        li, cut = self.lst({'2025-08-28': False}, session='2025-08-26', listed=('2025-08-26', '2025-08-28'))
        self.assertEqual(efb.resolve_expiry('BNF', '2025-08-26', li, cut)['reason'], 'BNF_MONTHLY_RULE_NOT_EFFECTIVE')
        li, cut = self.lst({'2027-01-26': False}, session='2026-12-29', listed=('2026-12-29', '2027-01-26'))
        self.assertEqual(efb.resolve_expiry('BNF', '2026-12-29', li, cut)['reason'], 'BNF_MONTHLY_CALENDAR_UNAVAILABLE')

    def test_holiday_coverage_matches_official_circular(self):
        self.assertEqual(tuple(cal.NSE_HOLIDAYS_2026), NSE_FAOP_71777_2026)
        self.assertEqual(cal.COVERED_YEARS, (2026,))
        self.assertEqual(efb.bnf_monthly_expiry(2026, 11).isoformat(), '2026-11-23')     # 24 Nov holiday
        self.assertEqual(efb.bnf_monthly_expiry(2026, 3).isoformat(), '2026-03-30')      # 31 Mar holiday


class S3LabelsAndCodes(unittest.TestCase):
    def test_chain_age_is_labelled_development_assumption(self):
        p = Sc().run()
        self.assertEqual(p['expiry_fallback']['chain_max_age_status'], 'DEVELOPMENT_ASSUMPTION_UNVALIDATED')
        self.assertEqual(p['expiry_fallback']['chain_max_age_s'], 60)
        self.assertEqual(p['expiry_fallback']['trade_expiry_meaning'], 'ORIGINAL_TRADING_EXPIRY')
        self.assertEqual(p['expiry_fallback']['contract_expiry'], p['contract']['expiry'])
        self.assertNotEqual(p['expiry_fallback']['trade_expiry'], p['contract']['expiry'])

    def test_every_emitted_wait_code_is_stable(self):
        with open(efb.__file__) as f:
            src = f.read()
        emitted = set(re.findall(r"wait_plan\([^,]+,\s*prov,\s*'([A-Z_]+)'", src))
        emitted |= set(re.findall(r"out\['reason'\] = '([A-Z_]+)'", src))
        emitted |= set(re.findall(r"return None, '([A-Z_]+)'", src[src.index('def check_chain'):src.index('def _captured_lot')]))
        with open(os.path.join(REPO, 'app/src/main/java/com/marketradar/app/MarketWatchService.kt')) as f:
            kt = f.read()
        native = set(re.findall(r'markWait\(resultObj,\s*"([A-Z_]+)"\)', kt))
        native |= set(re.findall(r'"(APPLY_DEADLINE_EXCEEDED)"', kt))
        # R3: the apply/publication decision moved to ExpiryFallbackPublication.applyAndPublish
        with open(os.path.join(REPO, 'app/src/main/java/com/marketradar/app/ExpiryFallbackPublication.kt')) as f:
            pk = f.read()
        for line in re.findall(r'markWait\(result,[^\n]*', pk):
            native |= set(re.findall(r'"([A-Z_]+)"', line))
        self.assertGreaterEqual(len(emitted), 30)
        self.assertEqual(native, {'NOT_ACQUIRED', 'NO_TOKEN', 'APPLY_TIMEOUT', 'APPLY_ERROR',
                                  'APPLY_DEADLINE_EXCEEDED', 'REFRESH_NOT_ACQUIRED', 'APPLY_SUPERSEDED'})
        self.assertEqual((emitted | native) - set(efb.WAIT_CODES) - {'NOT_APPLICABLE'}, set())
        self.assertEqual(len(efb.WAIT_CODES), len(set(efb.WAIT_CODES)))

    def test_withdraw_mirrors_native_markwait_and_never_touches_applied(self):
        sc = Sc()
        base = sc.base()
        w = efb.withdraw(base, 'REFRESH_NOT_ACQUIRED')
        self.assertEqual(w['expiry_fallback']['status'], 'WAIT')
        self.assertEqual(w['reasons'][-1], 'EXPIRY_FALLBACK_WAIT:REFRESH_NOT_ACQUIRED')
        self.assertEqual(w['reasons'][:-1], base['expiry_fallback']['base_reasons'])
        self.assertEqual((w['status'], w['admit']), ('BLOCKED', False))
        applied = sc.run()
        self.assertIs(efb.withdraw(applied, 'REFRESH_NOT_ACQUIRED'), applied)
        self.assertIs(efb.withdraw(w, 'NO_TOKEN'), w)


class S3S4Integration(unittest.TestCase):
    """brain-level: capital source, configured-capital admission, refresh withdrawal + stale tap, next poll, deadline."""
    SESSION, NEXT = T1.Integration.SESSION, T1.Integration.NEXT

    @classmethod
    def setUpClass(cls):
        T1.Integration.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    evidence = T1.Integration.evidence

    def apply(self, r, ctx, ev, cutoff, d=None, deadline_ms=None):
        return json.loads(brain.single_leg_expiry_fallback_apply(d or self.d, json.dumps(r), json.dumps(ctx),
                                                                 json.dumps(ev), cutoff, '[]', deadline_ms))

    def probe(self, plan, capital=OK['capital']):
        return lambda: json.dumps({'decisions': {'NF': {'decision_id': plan.get('decision_id'),
                                                        'instrument_key': plan.get('instrument_key')}},
                                   'enabled': True, 'paper_mode': True, 'selector': 'BOTH',
                                   'capital': capital, 'max_risk_pct': OK['max_risk_pct']})

    def newdir(self):
        d = os.path.join(tempfile.mkdtemp(dir=self.tmp), 'single_leg_v1')
        os.makedirs(d)
        return d

    def setUp(self):
        self.r = copy.deepcopy(self.result)
        self.dec = self.r['by_index']['NF']
        self.at = ia.parse_ts(self.dec['analysis_ts'])
        self.spot = self.dec['direction']['inputs']['spot']['value']

    def test_planning_capital_source_exposed(self):
        ev, _ = self.evidence(self.at, self.spot)
        for ctx, want in ((dict(self.ctx, capitalSource='CONFIGURED'), 'CONFIGURED'),
                          (dict(self.ctx, capitalSource='APP_DEFAULT'), 'APP_DEFAULT'),
                          ({k: v for k, v in self.ctx.items() if k != 'capital'}, 'ANALYSIS_DEFAULT')):
            d = self.newdir()
            out = self.apply(self.r, ctx, ev, ms(self.at) + 5000, d=d)
            self.assertEqual(out['single_leg_by_index']['NF']['expiry_fallback']['planning_capital_source'], want)
            o = load(os.path.join(d, efb.RETAIN_DIR, efb.RETAIN_DOC))['indices']['NF']['outcomes'][-1]
            self.assertEqual(o['planning_capital_source'], want)

    def test_admission_without_configured_capital_is_rejected(self):
        ev, ch = self.evidence(self.at, self.spot)
        d = self.newdir()
        p = self.apply(self.r, dict(self.ctx, capitalSource='APP_DEFAULT'), ev, ms(self.at) + 5000, d=d)['single_leg_by_index']['NF']
        self.r['single_leg_by_index']['NF'] = p
        t = self.at + timedelta(seconds=60)
        fresh = snap(ch, p['instrument_key'], t - timedelta(seconds=2), t - timedelta(seconds=1))
        rej = sls.admit(d, self.r, 'NF', clock=lambda: t, snapshot=fresh, state_probe=self.probe(p, capital=None),
                        **OK)
        self.assertEqual((rej['status'], rej['reason']), ('REJECTED', 'CAPITAL_CONFIG_UNAVAILABLE'))

    def test_refresh_withdraws_stale_tap_rejected_next_poll_restores_without_resurrection(self):
        ev, ch = self.evidence(self.at, self.spot)
        d = self.newdir()
        base = copy.deepcopy(self.r['single_leg_by_index']['NF'])
        p = self.apply(self.r, self.ctx, ev, ms(self.at) + 5000, d=d)['single_leg_by_index']['NF']
        shown = copy.deepcopy(self.r); shown['single_leg_by_index']['NF'] = p          # what the user saw
        # refresh: no acquisition; the recomputed base plan (EVIDENCE_REQUIRED) is withdrawn with an explicit WAIT
        w = efb.withdraw(base, 'REFRESH_NOT_ACQUIRED')
        self.assertEqual(w['expiry_fallback']['reason'], 'REFRESH_NOT_ACQUIRED')
        self.assertNotEqual(w.get('instrument_key'), p['instrument_key'])
        t = self.at + timedelta(seconds=40)
        fresh = snap(ch, p['instrument_key'], t - timedelta(seconds=2), t - timedelta(seconds=1))
        stale = sls.admit(d, shown, 'NF', clock=lambda: t, snapshot=fresh, state_probe=self.probe(w), **OK)
        self.assertEqual(stale['reason'], 'DECISION_CHANGED_BEFORE_COMMIT')
        cur = copy.deepcopy(self.r); cur['single_leg_by_index']['NF'] = w
        self.assertEqual(sls.admit(d, cur, 'NF', clock=lambda: t, snapshot=fresh, **OK)['status'], 'REJECTED')
        # next qualifying poll: a NEW decision, fresh evidence, a new proposal; the old decision is never revived
        nxt = copy.deepcopy(self.r)
        new_id = self.dec['decision_id'] + '_next'
        t2 = self.at + timedelta(seconds=60)
        nxt['by_index']['NF'].update(decision_id=new_id, analysis_ts=t2.isoformat())
        nb = nxt['single_leg_by_index']['NF']
        nb['decision_id'] = new_id
        nb['expiry_fallback']['decision_id'] = new_id
        ev2, ch2 = self.evidence(t2, self.spot)
        p2 = self.apply(nxt, self.ctx, ev2, ms(t2) + 5000, d=d)['single_leg_by_index']['NF']
        self.assertEqual((p2['decision_id'], p2['expiry_fallback']['status']), (new_id, efb.STATUS_APPLIED))
        nxt['single_leg_by_index']['NF'] = p2
        t3 = t2 + timedelta(seconds=30)
        f3 = snap(ch2, p2['instrument_key'], t3 - timedelta(seconds=2), t3 - timedelta(seconds=1))
        old_tap = sls.admit(d, shown, 'NF', clock=lambda: t3, snapshot=f3, state_probe=self.probe(p2), **OK)
        self.assertEqual(old_tap['status'], 'REJECTED')
        okp = sls.admit(d, nxt, 'NF', clock=lambda: t3, snapshot=f3, state_probe=self.probe(p2), **OK)
        self.assertEqual(okp['status'], 'ADMITTED', okp)
        ids = [x.get('entry_decision_id') for x in sls.read(d)['positions']]
        self.assertEqual(ids, [new_id])

    def test_acquisition_deadline_exhausted_is_an_explicit_wait(self):
        ev, _ = self.evidence(self.at, self.spot)
        del ev['NF']['chain']
        ev['NF']['stats']['acquisition'] = {'status': 'DEADLINE_EXHAUSTED', 'elapsed_ms': 8001, 'budget_ms': 8000,
                                            'requests': 2, 'bytes': 30}
        d = self.newdir()
        out = self.apply(self.r, self.ctx, ev, ms(self.at) + 5000, d=d)
        fb = out['single_leg_by_index']['NF']['expiry_fallback']
        self.assertEqual((fb['status'], fb['reason']), ('WAIT', 'ACQUISITION_DEADLINE_EXHAUSTED'))
        self.assertEqual(fb['acquisition']['elapsed_ms'], 8001)
        st = efb.summary(d)['NF']['stats'][self.SESSION]
        self.assertEqual((st['deadline_exhausted'], st['elapsed_ms_max']), (1, 8001))

    def test_apply_after_deadline_publishes_nothing_and_retains_nothing(self):
        ev, _ = self.evidence(self.at, self.spot)
        d = self.newdir()
        out = self.apply(self.r, self.ctx, ev, ms(self.at) + 5000, d=d, deadline_ms=int(time.time() * 1000) - 1)
        self.assertEqual(out['error'], 'APPLY_DEADLINE_EXCEEDED')
        self.assertNotIn('single_leg_by_index', out)
        self.assertFalse(os.path.exists(os.path.join(d, efb.RETAIN_DIR, efb.RETAIN_DOC)))


# ---------------------------------------------------------------------------------------------------------------
# s2: optional bounded export part
# ---------------------------------------------------------------------------------------------------------------
DAY = '2026-11-23'


class S2ExportPart(Base):
    def setUp(self):
        super().setUp()
        self.fd = self.tmp
        os.makedirs(os.path.join(self.fd, 'index_authority_v1'))
        with open(os.path.join(self.fd, 'index_authority_v1', DAY + '.json'), 'w') as f:
            json.dump({'session_date': DAY, 'history': {'NF': [], 'BNF': []}}, f)

    def build(self):
        return se.build(self.fd, DAY, None, {'pwa': 'T', 'app': 'T'}, require_exact=True)

    def fill(self, n=4):
        for ix in ('NF', 'BNF'):
            c = CASES[ix]
            lst = listing_rec(ix, c, at_ms(c['session'], (9, 30), 2))
            meta = contracts_rec(ix, c, at_ms(c['session'], (9, 30), 3))
            self.live_and_retain(ix, [Poll(ix, c, (10 + i, 0), c['wide'], lst, meta) for i in range(n)])

    def test_absent_directory_exports_exactly_as_before(self):
        b, raw = self.build()
        self.assertNotIn('expiry_fallback', b['manifest']['parts'])
        os.makedirs(os.path.join(self.d, 'other'), exist_ok=True)
        self.assertEqual(len(self.build()[1]), len(raw))

    def test_present_part_per_index_with_coverage_and_drops(self):
        self.fill()
        b, raw = self.build()
        e = b['manifest']['parts']['expiry_fallback']
        self.assertEqual(e['status'], 'PRESENT')
        part = b['parts']['expiry_fallback']
        self.assertEqual(sorted(part['indices']), ['BNF', 'NF'])
        for ix in ('NF', 'BNF'):
            # R3 (Codex R2 reply s2): the device metric and the delivered metric carry distinct names
            self.assertEqual(part['indices'][ix]['source_coverage_session'], {'REPLAYABLE': 4, 'INCOMPLETE': 0})
            self.assertEqual(part['indices'][ix]['delivered_coverage_session'], {'REPLAYABLE': 4, 'INCOMPLETE': 0})
            self.assertIn('window_exclusions', e['drops'][ix])
        self.assertEqual(part['labels']['why_exp'], 'ORIGINAL_TRADING_EXPIRY')
        self.assertNotIn(b'"capital":', raw)

    def test_unreadable_and_oversized_are_explicit_and_harm_nothing(self):
        def pilot(_d, _day, level):
            return {'status': 'OK', 'level': level, 'payload': 'x' * (8000 if level == 'full' else 100)}
        with mock.patch('c1_capture_pilot_v1.export_part', side_effect=pilot):
            ref, _ = self.build()
            os.makedirs(os.path.join(self.d, efb.RETAIN_DIR))
            with open(self.path(), 'w') as f:
                f.write('{bad')
            b, _ = self.build()
            self.assertEqual(b['manifest']['parts']['expiry_fallback']['status'], 'UNREADABLE')
            self.assertEqual(b['parts']['c1_capture'], ref['parts']['c1_capture'])
            with open(self.path(), 'w') as f:
                f.write(' ' * (se.EXPIRY_FALLBACK_READ_CAP + 1))
            b, _ = self.build()
            e = b['manifest']['parts']['expiry_fallback']
            self.assertEqual((e['status'], e['reason']), ('UNREADABLE', 'OVER_CAP'))
            self.assertEqual(b['manifest']['parts']['c1_capture'], ref['manifest']['parts']['c1_capture'])

    def test_cap_degradation_never_harms_c1_or_trace(self):
        def pilot(_d, _day, level):
            return {'status': 'OK', 'level': level, 'payload': 'x' * (8000 if level == 'full' else 100)}
        os.makedirs(os.path.join(self.fd, se.TRACE_DIR))
        with open(os.path.join(self.fd, se.TRACE_DIR, DAY + '.json'), 'w') as f:
            json.dump({'schema': 'mr.single_leg_trace.v1', 'session_date': DAY, 'truncated': False,
                       'records': [{'kind': 'TAP', 't_ms': 1, 'index': 'NF', 'origin': 'tap%d' % i} for i in range(30)]}, f)
        with mock.patch('c1_capture_pilot_v1.export_part', side_effect=pilot):
            ref, ref_raw = self.build()
            self.fill(6)
            full_b, full_raw = self.build()
            self.assertEqual(full_b['manifest']['parts']['expiry_fallback']['status'], 'PRESENT')
            levels = []
            for cap in (len(full_raw) - 10, len(ref_raw) + 20000, len(ref_raw) + 3000, len(ref_raw) + 200,
                        len(ref_raw) + 5):
                with mock.patch.object(se, 'MAX_BUNDLE_BYTES', cap):
                    b, raw = self.build()
                self.assertLessEqual(len(raw), cap)
                for k in ('c1_capture', 'single_leg_trace'):
                    self.assertEqual(b['parts'][k], ref['parts'][k], k)
                    self.assertEqual(b['manifest']['parts'][k], ref['manifest']['parts'][k], k)
                e = b['manifest']['parts'].get('expiry_fallback')
                levels.append(None if e is None else (e['status'], e.get('fallback_level')))
                if e and e.get('drops'):
                    self.assertIn('records_retained', e['drops']['NF'])
            self.assertEqual(levels[0][0], 'TRUNCATED')
            self.assertIn(('OMITTED_FOR_CAP', None), levels)
            self.assertIsNone(levels[-1])


if __name__ == '__main__':
    unittest.main()
