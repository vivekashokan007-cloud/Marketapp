"""b510 Batch C: prospective X1 v2 Paper buying collector — Codex buying tests 1-16.

Synthetic timestamped quote events with exact expected economics; no broker calls.
Chains obey put-call parity (CE mid - PE mid = F - K), so forward = F at every strike.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', '..', 'tools'))
sys.path.insert(0, HERE)

import brain  # noqa: E402
import paper_buying_research as pbr  # noqa: E402
import x1_report  # noqa: E402
from ev_v12_fees import fee_identity, one_side_fee  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
FRI, MON, WED = '2026-10-16', '2026-10-19', '2026-10-21'   # 2026-10-20 is an NSE holiday
EXP = '2026-10-27'


def ms(day, hms):
    h, m, s = (hms.split(':') + ['0'])[:3]
    sec = float(s)
    return int(round(datetime(*map(int, day.split('-')), int(h), int(m), int(sec), tzinfo=IST).timestamp() * 1000
                     + (sec - int(sec)) * 1000))


UNDERLYING = {'NF': 'NSE_INDEX|Nifty 50', 'BNF': 'NSE_INDEX|Nifty Bank'}


def chain(ix, F, expiry=EXP, n=6, tv=100.0, spread=1.0, sym_ix=None, lot=None, drop=None, mutate=None,
          with_symbol=False):
    """Formatter-output shape for a documented /v2/option/chain response (b510 R1): per-leg
    underlying_key/contract_expiry from the chain row, requested_* from the native request, and
    NO trading_symbol unless with_symbol (or sym_ix) asks for one."""
    step = 50 if ix == 'NF' else 100
    base = round(F / step) * step
    pref = 'NIFTY' if (sym_ix or ix) == 'NF' else 'BANKNIFTY'
    strikes = {}
    for i in range(-n, n + 1):
        K = base + i * step
        row = {}
        for side, mid in (('CE', max(F - K, 0) + tv), ('PE', max(K - F, 0) + tv)):
            row[side] = {'bid': mid - spread / 2, 'ask': mid + spread / 2, 'ltp': mid,
                         'instrument_key': 'NSE_FO|%s%d%s%s' % (ix, K, side, expiry),
                         'underlying_key': UNDERLYING[ix], 'contract_expiry': expiry}
            if with_symbol or sym_ix:
                row[side]['trading_symbol'] = '%s %d %s 27 OCT 26' % (pref, K, side)
        strikes[str(int(K))] = row
    c = {'expiry': expiry, 'requested_expiry': expiry, 'requested_underlying_key': UNDERLYING[ix],
         'strikes': strikes}
    if lot is not None:
        c['lotSize'] = lot
    if mutate:
        mutate(c)
    return c


class Harness:
    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, pbr.STATE_DIR_NAME)
        self.path = os.path.join(self.dir, pbr.JOURNAL_FILE)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, day, hms, nf=None, bnf=None, q_lag_s=20, qnf=None, qbnf=None, mode='paper', **kw):
        d = ms(day, hms)
        c = {'executionMode': mode, 'x1CaptureSource': 'LIVE_PHONE_POLL', 'x1StateDir': self.dir,
             'x1PollMs': d, 'x1QuoteFetchedMs': {}}
        if nf is not None:
            c['nfChain'] = nf if isinstance(nf, dict) else chain('NF', nf)
            c['x1QuoteFetchedMs']['NF'] = qnf if qnf is not None else d - q_lag_s * 1000
        if bnf is not None:
            c['bnfChain'] = bnf if isinstance(bnf, dict) else chain('BNF', bnf)
            c['x1QuoteFetchedMs']['BNF'] = qbnf if qbnf is not None else d - q_lag_s * 1000
        if mode is None:
            c.pop('executionMode')
        c.update(kw)
        return c

    def run(self, ctx, now=None):
        with patch.object(pbr, '_now_ms', return_value=now if now is not None else ctx.get('x1PollMs')):
            return pbr.run(ctx, brain_version='2.6.79')

    def state(self):
        with open(self.path) as fh:
            return json.load(fh)

    def raw(self):
        with open(self.path, 'rb') as fh:
            return fh.read()

    def rows(self, **match):
        return [r for r in self.state()['records'] if all(r.get(k) == v for k, v in match.items())]

    def row(self, **match):
        rows = self.rows(**match)
        assert len(rows) == 1, (match, rows)
        return rows[0]

    def reference(self, day=FRI, nf=24500, bnf=55000, hms='15:20:30'):
        return self.run(self.ctx(day, hms, nf, bnf))

    def standard_day(self):
        """Fri reference; Mon +60 NF (CE) / -120 BNF (PE) gap, control, exits."""
        self.reference()
        self.run(self.ctx(MON, '09:20:40', 24560, 54880))
        self.run(self.ctx(MON, '09:25:40', 24600, 54850))
        self.run(self.ctx(MON, '09:51:00', 24650, 54800, q_lag_s=10))    # quote 09:50:50 >= 09:20:40 + 30m
        return self.run(self.ctx(MON, '09:56:00', 24700, 54750, q_lag_s=10))  # quote 09:55:50 >= 09:25:40 + 30m


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()


# 1 ------------------------------------------------------------------------------------------------
class T01ModeGate(Base):
    def test_real_sandbox_missing_conflicting_mode_no_journal(self):
        for mode in ('live', 'sandbox', None, 'unknown', ''):
            self.assertIsNone(self.h.run(self.h.ctx(FRI, '15:20:30', 24500, 55000, mode=mode)), mode)
        self.assertIsNone(self.h.run(self.h.ctx(FRI, '15:20:30', 24500, 55000, execution_mode='live')))
        self.assertIsNone(self.h.run(self.h.ctx(FRI, '15:20:30', 24500, 55000, x1CaptureSource='UI')))
        self.assertFalse(os.path.exists(self.h.dir))

    def test_live_analyze_with_forged_x1_keys_changes_nothing(self):
        polls = json.dumps([{'t': '09:15', 'nf': 24500, 'bnf': 55000, 'vix': 14}] * 2)
        ctx = self.h.ctx(MON, '09:20:40', 24560, 54880, mode='live')
        plain = {k: v for k, v in ctx.items() if k not in brain.X1_CONTEXT_KEYS}
        with patch.object(pbr, '_now_ms', return_value=ctx['x1PollMs']):
            a = json.loads(brain.analyze(polls, '[]', '{}', '[]', '[]', '{}', json.dumps(ctx)))
        b = json.loads(brain.analyze(polls, '[]', '{}', '[]', '[]', '{}', json.dumps(plain)))
        self.assertNotIn('paper_buying_research', a)
        self.assertFalse(os.path.exists(self.h.dir))
        for k in ('verdict', 'watchlist', 'generated_candidates'):
            self.assertEqual(json.dumps(a.get(k), sort_keys=True), json.dumps(b.get(k), sort_keys=True))


# 2 ------------------------------------------------------------------------------------------------
class T02WarmupObservation(Base):
    def test_paper_poll_observes_main_arm_during_warmup_and_entry_stays_wait(self):
        self.h.reference()
        polls = json.dumps([{'t': '09:15', 'nf': 24500, 'bnf': 55000, 'vix': 14},
                            {'t': '09:20', 'nf': 24560, 'bnf': 54880, 'vix': 14}])
        ctx = self.h.ctx(MON, '09:20:40', 24560, 54880)
        with patch.object(pbr, '_now_ms', return_value=ctx['x1PollMs']):
            out = json.loads(brain.analyze(polls, '[]', '{}', '[]', '[]', '{}', json.dumps(ctx)))
        self.assertEqual(out['verdict']['action'], 'WAIT')
        self.assertEqual(out['generated_candidates'], [])
        self.assertEqual(out['watchlist'], [])
        x1 = out['paper_buying_research']
        self.assertEqual(x1['status'], 'COLLECTING')
        self.assertFalse(x1['recommendation_authority'])
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')
        self.assertNotIn(self.h.tmp, json.dumps(out))            # no local path leaks


# 3 ------------------------------------------------------------------------------------------------
class T03NoMutationPaths(Base):
    def test_replay_refresh_source_tag_and_stale_context_cannot_mutate(self):
        self.h.reference()
        before = self.h.raw()
        polls = json.dumps([{'t': '09:15', 'nf': 24500, 'bnf': 55000, 'vix': 14}] * 2)
        ctx = self.h.ctx(MON, '09:20:40', 24560, 54880)
        with patch.object(pbr, '_now_ms', return_value=ctx['x1PollMs']):
            brain.replay({'poll_json': polls, 'trades_json': '[]', 'baseline_json': '{}',
                          'open_trades_json': '[]', 'candidates_json': '[]', 'strike_oi_json': '{}',
                          'context_json': json.dumps(ctx)})
        self.assertEqual(self.h.raw(), before)
        refresh = {k: v for k, v in ctx.items() if k not in brain.X1_CONTEXT_KEYS}   # native strips on refresh
        self.assertIsNone(self.h.run(refresh))
        self.assertIsNone(self.h.run(dict(ctx, _trace_source_override='replay')))
        stale = self.h.run(ctx, now=ctx['x1PollMs'] + 3 * 3600_000)            # persisted ctx replayed later
        self.assertEqual(stale['status'], 'STALE_CONTEXT')
        self.assertEqual(self.h.raw(), before)
        bad_dir = dict(ctx, x1StateDir=os.path.join(self.h.tmp, 'elsewhere'))
        self.assertEqual(self.h.run(bad_dir)['status'], 'INPUT_INVALID')
        self.assertEqual(self.h.raw(), before)


# 4 ------------------------------------------------------------------------------------------------
class T04Windows(Base):
    def test_entry_window_inclusive_end_and_one_ms_late(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:21:30', 24560, 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')
        h2 = Harness()
        try:
            h2.reference()
            h2.run(h2.ctx(MON, '09:21:30.001', 24560, 54880))
            r = h2.row(index='NF', arm='primary')
            self.assertEqual((r['status'], r['reason']), ('SKIP', 'ENTRY_WINDOW_MISSED'))
        finally:
            h2.close()

    def test_quote_before_window_then_missed_with_reason_no_ten_minute_fallback(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:05', 24560, 54880, q_lag_s=6))      # quotes at 09:19:59
        self.assertEqual(self.h.rows(arm='primary'), [])
        self.h.run(self.h.ctx(MON, '09:29:00', 24560, 54880))                  # historical +10 min: not allowed
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['reason'], r['window_reason']),
                         ('SKIP', 'ENTRY_WINDOW_MISSED', 'QUOTE_OUTSIDE_WINDOW'))
        self.assertEqual(self.h.row(index='NF', arm='delayed')['reason'], 'NO_PRIMARY_ENTRY')

    def test_control_window_boundaries(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.h.run(self.h.ctx(MON, '09:26:30', 24600, None))                   # NF ok at end; BNF no quote
        self.assertEqual(self.h.row(index='NF', arm='delayed')['status'], 'OPEN')
        self.assertEqual(self.h.rows(index='BNF', arm='delayed'), [])
        self.h.run(self.h.ctx(MON, '09:26:31', 24600, 54850))
        d = self.h.row(index='BNF', arm='delayed')
        self.assertEqual((d['status'], d['reason'], d['window_reason']),
                         ('SKIP', 'CONTROL_WINDOW_MISSED', 'QUOTE_TIME_UNAVAILABLE'))

    def test_reference_window_boundaries(self):
        self.h.run(self.h.ctx(FRI, '15:19:59', 24400, 54900, q_lag_s=1))       # before window: ignored
        self.assertEqual(self.h.state()['references'], {})
        self.h.run(self.h.ctx(FRI, '15:21:30', 24500, 55000, q_lag_s=10))
        self.assertEqual(self.h.state()['references']['NF']['forward'], 24500)
        self.h.run(self.h.ctx(FRI, '15:21:00', 1, 1))                          # out of order: rejected
        h2 = Harness()
        try:
            h2.run(h2.ctx(FRI, '15:21:31', 24500, 55000, q_lag_s=10))
            self.assertEqual(h2.state()['references'], {})
        finally:
            h2.close()


# 5 ------------------------------------------------------------------------------------------------
class T05GapSideAtm(Base):
    def test_positive_gap_buys_ce_negative_buys_pe(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        nf, bnf = self.h.row(index='NF', arm='primary'), self.h.row(index='BNF', arm='primary')
        self.assertEqual((nf['side'], nf['strike'], nf['entry_ask']), ('CE', 24550.0, 110.5))
        self.assertAlmostEqual(nf['gap_straddle_ratio'], 60 / 210, places=9)
        self.assertEqual((bnf['side'], bnf['strike'], bnf['entry_ask']), ('PE', 54900.0, 120.5))
        self.assertAlmostEqual(bnf['gap'], -120.0, places=6)

    def test_threshold_equality_skips(self):
        self.h.reference(nf=24470, bnf=55000)            # F_prev 24470; F_now 24500 on a strike -> 30/200
        self.h.run(self.h.ctx(MON, '09:20:40', 24500, 55000))
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['reason']), ('SKIP', 'GAP_BELOW_THRESHOLD'))
        self.assertEqual(r['gap_straddle_ratio'], 0.15)

    def test_deterministic_atm_tie_break_lower_strike(self):
        f, _ = pbr._frame({'decision_ts_ms': ms(MON, '10:00:20'), 'frames': {
            'NF': {'chain': chain('NF', 24525), 'quote_ts_ms': ms(MON, '10:00:00')}}}, 'NF',
            datetime.fromtimestamp(ms(MON, '10:00:20') / 1000, IST))
        atm = pbr._atm(f)
        self.assertEqual(atm['strike'], 24500.0)
        self.assertEqual(atm['forward'], 24525.0)
        # Order of chain keys does not matter.
        rev = chain('NF', 24525)
        rev['strikes'] = dict(reversed(list(rev['strikes'].items())))
        self.assertEqual(pbr._atm(dict(f, chain=rev))['strike'], 24500.0)


# 6 ------------------------------------------------------------------------------------------------
class T06PreviousSession(Base):
    def test_weekend_and_holiday_previous_session(self):
        self.h.reference(day=MON)                              # Mon ref -> Wed (Tue 20 Oct holiday)
        self.h.run(self.h.ctx(WED, '09:20:40', 24560, 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['reference']['session_date'], MON)
        h2 = Harness()
        try:
            h2.reference(day=FRI)                              # Fri ref -> Mon across the weekend
            h2.run(h2.ctx(MON, '09:20:40', 24560, 54880))
            self.assertEqual(h2.row(index='NF', arm='primary')['status'], 'OPEN')
        finally:
            h2.close()

    def test_missing_or_older_reference_skips(self):
        self.h.reference(day='2026-10-15')                     # Thu ref is NOT the previous session of Mon
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'PREVIOUS_SESSION_REFERENCE_UNAVAILABLE')

    def test_unsupported_calendar_year_fails_closed(self):
        self.h.reference()
        before = self.h.raw()
        out = self.h.run(self.h.ctx('2027-01-04', '09:20:40', 24560, 54880))
        self.assertEqual(out['status'], 'CALENDAR_UNSUPPORTED')
        self.assertEqual(self.h.raw(), before)
        self.assertEqual(self.h.run(self.h.ctx('2026-10-18', '09:20:40', 1, 1))['status'], 'NOT_A_SESSION')


# 7 ------------------------------------------------------------------------------------------------
class T07ExpiryIndexLot(Base):
    def test_expiry_roll_skips(self):
        self.h.run(self.h.ctx(FRI, '15:20:30', chain('NF', 24500, expiry='2026-10-20'), 55000))
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['reason'], r['reference_expiry'], r['expiry']), ('EXPIRY_ROLL', '2026-10-20', EXP))

    def test_wrong_index_chain_rejected(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', chain('NF', 24560, sym_ix='BNF'), 54880))
        self.h.run(self.h.ctx(MON, '09:25:40', 24600, 54850))
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['reason'], r['window_reason']), ('ENTRY_WINDOW_MISSED', 'CHAIN_INDEX_MISMATCH'))

    def test_mismatched_captured_lot_rejected(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', chain('NF', 24560, lot=75), chain('BNF', 54880, lot=30)))
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'LOT_AUTHORITY_UNAVAILABLE')
        self.assertEqual(self.h.row(index='BNF', arm='primary')['contract_lot_size'], 30)

    def test_unknown_lot_authority_rejected(self):
        self.h.reference()
        with patch.object(pbr, 'resolve_contract_lot', return_value={'resolved': False}):
            self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'LOT_AUTHORITY_UNAVAILABLE')


# 8 ------------------------------------------------------------------------------------------------
class T08QuoteValidity(Base):
    def frame_reason(self, c, q, decision='10:00:20', day=MON):
        d = ms(day, decision)
        _, reason = pbr._frame({'decision_ts_ms': d, 'frames': {'NF': {'chain': c, 'quote_ts_ms': q}}}, 'NF',
                               datetime.fromtimestamp(d / 1000, IST))
        return reason

    def test_receipt_time_and_session_rejections(self):
        c = chain('NF', 24500)
        self.assertEqual(self.frame_reason(c, None), 'QUOTE_TIME_UNAVAILABLE')
        self.assertEqual(self.frame_reason(c, ms(MON, '10:00:21')), 'QUOTE_FROM_FUTURE')
        self.assertEqual(self.frame_reason(c, ms(MON, '09:58:49')), 'QUOTE_STALE')
        self.assertIsNone(self.frame_reason(c, ms(MON, '09:58:50')))                    # exactly 90 s
        self.assertEqual(self.frame_reason(c, ms(MON, '09:15:30'), '09:16:10'), 'QUOTE_OUTSIDE_SESSION')
        self.assertEqual(self.frame_reason(c, ms(MON, '15:30:00'), '15:30:20'), 'QUOTE_OUTSIDE_SESSION')
        self.assertEqual(self.frame_reason({'expiry': EXP}, ms(MON, '10:00:00')), 'CHAIN_UNAVAILABLE')
        self.assertEqual(self.frame_reason(chain('NF', 24500, expiry='2026-10-16'), ms(MON, '10:00:00')),
                         'EXPIRY_PASSED')
        self.assertEqual(self.frame_reason(chain('NF', 24500, expiry='soon'), ms(MON, '10:00:00')),
                         'EXPIRY_UNAVAILABLE')

    def test_invalid_legs_never_priced(self):
        frame = {'index': 'NF', 'expiry': EXP, 'duplicate_keys': frozenset({'NSE_FO|dup'})}
        ok = {'bid': 10.0, 'ask': 11.0, 'instrument_key': 'NSE_FO|1', 'underlying_key': 'NSE_INDEX|Nifty 50',
              'contract_expiry': EXP}
        self.assertIsNotNone(pbr._valid_leg(frame, ok))                       # documented feed: no symbol
        self.assertIsNotNone(pbr._valid_leg(frame, dict(ok, trading_symbol='NIFTY 1 CE')))
        for bad in ({'bid': 12.0}, {'bid': 0.0}, {'bid': float('nan')}, {'ask': None}, {'bid': True},
                    {'instrument_key': ''}, {'instrument_key': None}, {'instrument_key': '12345'},
                    {'instrument_key': 'NSE_FO|dup'}, {'trading_symbol': 'BANKNIFTY 1 CE'},
                    {'underlying_key': 'NSE_INDEX|Nifty Bank'}, {'underlying_key': None},
                    {'contract_expiry': '2026-11-03'}, {'contract_expiry': None}):
            self.assertIsNone(pbr._valid_leg(frame, dict(ok, **bad)), bad)
        # LTP alone is never used.
        self.assertIsNone(pbr._valid_leg(frame, {'ltp': 10.0, 'instrument_key': 'NSE_FO|k',
                                                 'underlying_key': 'NSE_INDEX|Nifty 50', 'contract_expiry': EXP}))

    def test_missing_instrument_key_on_atm_leg_falls_to_next_valid_pair(self):
        def kill(c):
            for side in ('CE', 'PE'):
                c['strikes']['24550'][side]['instrument_key'] = ''
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', chain('NF', 24560, mutate=kill), 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['strike'], 24600.0)   # 24550 not tradable


# 9 ------------------------------------------------------------------------------------------------
class T09DelayedControl(Base):
    def test_no_main_means_skip_and_no_reselection(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24505, 54880))          # NF gap below threshold
        self.h.run(self.h.ctx(MON, '09:25:40', 24300, 55300))          # big new move: still no re-selection
        self.assertEqual(self.h.row(index='NF', arm='delayed')['reason'], 'NO_PRIMARY_ENTRY')
        d, p = self.h.row(index='BNF', arm='delayed'), self.h.row(index='BNF', arm='primary')
        for k in ('side', 'strike', 'expiry', 'instrument_key', 'contract_lot_size'):
            self.assertEqual(d[k], p[k])
        self.assertEqual((d['side'], d['strike']), ('PE', 54900.0))  # the market now suggests CE elsewhere
        self.assertEqual(d['entry_ask'], 0 + 100 + 0.5)                # PE 54900 at F=55300

    def test_missing_control_stays_visible(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.h.run(self.h.ctx(MON, '09:30:40', 24600, 54850))
        for ix in ('NF', 'BNF'):
            self.assertEqual(self.h.row(index=ix, arm='delayed')['reason'], 'CONTROL_WINDOW_MISSED')
        rep = x1_report.report(self.h.state())
        self.assertEqual(rep['indices']['NF']['delayed_control']['status_counts'],
                         {'SKIP:CONTROL_WINDOW_MISSED': 1})


# 10 -----------------------------------------------------------------------------------------------
class T10ExitWindow(Base):
    def test_early_ignored_first_valid_accepted(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))          # entry quote 09:20:20
        self.h.run(self.h.ctx(MON, '09:50:25', 24650, 54800, q_lag_s=10))   # quote 09:50:15 < +30m
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')
        self.h.run(self.h.ctx(MON, '09:55:40', 24700, 54750, q_lag_s=10))   # first valid (+35m10s)
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['exit_bid'], r['exit_quote_ts_ms']), ('RESOLVED', 249.5, ms(MON, '09:55:30')))
        frozen = copy.deepcopy(r)
        self.h.run(self.h.ctx(MON, '10:00:40', 25000, 54000, q_lag_s=10))
        self.assertEqual(self.h.row(index='NF', arm='primary'), frozen)     # immutable once resolved

    def test_only_later_quote_is_unresolved_never_zero(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.h.run(self.h.ctx(MON, '10:05:40', 24700, 54750, q_lag_s=10))   # +45m only
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['reason']), ('UNRESOLVED', 'EXIT_QUOTE_DEADLINE'))
        self.assertNotIn('net', r)

    def test_open_row_from_previous_session_is_unresolved(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.h.run(self.h.ctx(WED, '09:16:40', 24560, 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['reason'], 'SESSION_ENDED')


class T10bHoldAnchoredAtEntryDecision(Base):
    """b510 R4: hold measured from the entry DECISION; exit quote >= decision+30m, exit decision <= +40m."""

    def enter(self, decision='09:21:00', lag=60):
        self.h.reference()
        self.h.run(self.h.ctx(MON, decision, 24560, 54880, q_lag_s=lag))     # quote 09:20:00, decision 09:21:00
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['entry_quote_ts_ms'], r['entry_decision_ts_ms']), (ms(MON, '09:20:00'), ms(MON, decision)))
        return r

    def test_codex_example_0921_to_0950_stays_open(self):
        self.enter()
        self.h.run(self.h.ctx(MON, '09:50:00', 24650, 54800, q_lag_s=0))       # 29 min after the decision
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')

    def test_plus_30_boundary_resolves(self):
        self.enter()
        self.h.run(self.h.ctx(MON, '09:51:20', 24650, 54800, q_lag_s=20))      # quote exactly 09:51:00
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['hold_ms_from_entry_decision']), ('RESOLVED', 30 * 60_000))

    def test_quote_one_ms_before_plus_30_is_rejected(self):
        self.enter()
        self.h.run(self.h.ctx(MON, '09:51:20', 24650, 54800, qnf=ms(MON, '09:51:00') - 1))
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')

    def test_plus_40_boundary_resolves_with_realistic_lag(self):
        self.enter()
        self.h.run(self.h.ctx(MON, '10:01:00', 24650, 54800, q_lag_s=75))      # decision exactly +40m, quote 75 s old
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['exit_quote_ts_ms']), ('RESOLVED', ms(MON, '09:59:45')))

    def test_past_deadline_is_unresolved_even_with_an_in_window_quote(self):
        self.enter()
        self.h.run(self.h.ctx(MON, '10:01:00.001', 24650, 54800, qnf=ms(MON, '10:00:00'), qbnf=ms(MON, '10:00:00')))
        r = self.h.row(index='NF', arm='primary')
        self.assertEqual((r['status'], r['reason']), ('UNRESOLVED', 'EXIT_QUOTE_DEADLINE'))
        self.assertNotIn('net', r)

    def test_delayed_arm_uses_its_own_decision(self):
        self.enter()
        self.h.run(self.h.ctx(MON, '09:26:00', 24600, 54850, q_lag_s=50))      # control quote 09:25:10, decision 09:26
        d = self.h.row(index='NF', arm='delayed')
        self.assertEqual(d['status'], 'OPEN')
        self.h.run(self.h.ctx(MON, '09:55:30', 24700, 54750, q_lag_s=0))       # +29m30s from 09:26 decision
        self.assertEqual(self.h.row(index='NF', arm='delayed')['status'], 'OPEN')
        self.h.run(self.h.ctx(MON, '09:56:20', 24700, 54750, q_lag_s=20))      # quote 09:56:00 = +30m
        self.assertEqual(self.h.row(index='NF', arm='delayed')['status'], 'RESOLVED')
        self.h.close(); self.h = Harness()
        self.enter()
        self.h.run(self.h.ctx(MON, '09:26:00', 24600, 54850, q_lag_s=50))
        self.h.run(self.h.ctx(MON, '10:06:00.001', 24700, 54750, qnf=ms(MON, '10:05:00'), qbnf=ms(MON, '10:05:00')))
        self.assertEqual(self.h.row(index='NF', arm='delayed')['reason'], 'EXIT_QUOTE_DEADLINE')


# 11 -----------------------------------------------------------------------------------------------
class T11Economics(Base):
    def test_exact_lot_fees_and_net(self):
        self.h.standard_day()
        cases = (('NF', 'primary', 65, 110.5, 199.5), ('BNF', 'primary', 30, 120.5, 199.5),
                 ('NF', 'delayed', 65, 150.5, 249.5), ('BNF', 'delayed', 30, 150.5, 249.5))
        for ix, arm, lot, ask, bid in cases:
            r = self.h.row(index=ix, arm=arm)
            self.assertEqual(r['status'], 'RESOLVED', (ix, arm))
            self.assertEqual(r['contract_lot_size'], lot)
            self.assertEqual((r['entry_ask'], r['exit_bid']), (ask, bid))
            fe = one_side_fee(0.0, ask, 1, lot, MON)
            fx = one_side_fee(bid, 0.0, 1, lot, MON)
            self.assertAlmostEqual(r['entry_fee'], fe, places=6)
            self.assertAlmostEqual(r['exit_fee'], fx, places=6)
            self.assertAlmostEqual(r['gross'], (bid - ask) * lot, places=6)
            self.assertAlmostEqual(r['net'], (bid - ask) * lot - fe - fx, places=5)
            self.assertAlmostEqual(r['net'], r['gross'] - r['fees'], places=5)
            self.assertEqual(r['fee_identity'], fee_identity(MON))
            self.assertEqual(r['lot_table_version'], 'contract_lot_table_v3_20260914')
        self.assertAlmostEqual(self.h.row(index='NF', arm='primary')['gross'], 5785.0, places=6)


# 12 -----------------------------------------------------------------------------------------------
class T12Robustness(Base):
    def test_duplicate_out_of_order_restart(self):
        self.h.reference()
        c = self.h.ctx(MON, '09:20:40', 24560, 54880)
        self.h.run(c)
        snap = self.h.raw()
        self.assertEqual(self.h.run(c)['status'], 'DUPLICATE_OR_OUT_OF_ORDER')        # same poll again
        self.assertEqual(self.h.run(self.h.ctx(MON, '09:20:30', 24900, 54000))['status'],
                         'DUPLICATE_OR_OUT_OF_ORDER')
        self.assertEqual(self.h.raw(), snap)
        # "restart": fresh module state, same files; a later in-window poll never re-enters.
        self.h.run(self.h.ctx(MON, '09:21:10', 24900, 54000))
        self.assertEqual(len(self.h.rows(index='NF', arm='primary')), 1)
        self.assertEqual(self.h.row(index='NF', arm='primary')['entry_ask'], 110.5)

    def test_crash_during_write_keeps_previous_journal(self):
        self.h.reference()
        before = self.h.raw()
        with patch.object(pbr.os, 'replace', side_effect=OSError('disk')):
            out = self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.assertEqual(out['status'], 'WRITE_FAILED')
        self.assertEqual(self.h.raw(), before)
        self.assertEqual([f for f in os.listdir(self.h.dir) if f.endswith('.tmp')], [])
        # the next poll recovers inside the window from the intact journal
        self.h.run(self.h.ctx(MON, '09:21:10', 24560, 54880))
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'OPEN')

    def test_corrupt_unknown_digest_and_contract_mismatch_stop_without_reset(self):
        self.h.reference()
        good = self.h.raw()
        variants = {
            'JOURNAL_CORRUPT': good[: len(good) // 2],
            'JOURNAL_UNKNOWN_VERSION': json.dumps(dict(json.loads(good), schema='x1_journal_v9')).encode(),
            'PROTOCOL_DIGEST_MISMATCH': json.dumps(dict(json.loads(good), protocol_digest='0' * 64)).encode(),
            'BLOCK_CONTRACT_MISMATCH': json.dumps(dict(json.loads(good), block=dict(
                json.loads(good)['block'], fee_contract_version='other'))).encode(),
        }
        t = 0
        for status, data in variants.items():
            with open(self.h.path, 'wb') as fh:
                fh.write(data)
            t += 1
            out = self.h.run(self.h.ctx(MON, '09:20:%02d' % (40 + t), 24560, 54880))
            self.assertEqual(out['status'], status)
            with open(self.h.path, 'rb') as fh:
                self.assertEqual(fh.read(), data)                   # never silently reset
            with open(os.path.join(self.h.dir, pbr.STATUS_FILE)) as fh:
                self.assertEqual(json.load(fh)['status'], status)

    def test_lock_contention_is_truthful(self):
        import fcntl
        self.h.reference()
        before = self.h.raw()
        with open(os.path.join(self.h.dir, pbr.LOCK_FILE), 'a+') as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            with patch.object(pbr.time, 'monotonic', side_effect=[0.0, 10.0]):
                out = self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        self.assertEqual(out['status'], 'LOCK_UNAVAILABLE')
        self.assertEqual(self.h.raw(), before)


# 13 -----------------------------------------------------------------------------------------------
def _resolved_rows(ix, n, start='2026-06-01'):
    out = []
    d = datetime.fromisoformat(start)
    i = 0
    while len(out) < n:
        if d.weekday() < 5:
            s = d.date().isoformat()
            out.append({'id': '%s:%s:primary' % (s, ix), 'session_date': s, 'index': ix, 'arm': 'primary',
                        'status': 'RESOLVED', 'net': 100.0 + (i % 7) * 10 - 30, 'side': 'CE', 'expiry': EXP})
            i += 1
        d += timedelta(days=1)
    return out


class T13Endpoint(Base):
    def test_no_61st_primary_and_last_delayed_finishes(self):
        self.h.reference()
        st = self.h.state()
        st['records'] = _resolved_rows('NF', 59)
        with open(self.h.path, 'w') as fh:
            json.dump(st, fh)
        self.h.standard_day()                                   # 60th NF primary resolves; control finishes
        self.assertEqual(len(self.h.rows(index='NF', arm='primary', status='RESOLVED')), 60)
        self.assertEqual(self.h.row(index='NF', arm='delayed')['status'], 'RESOLVED')
        self.h.reference(day=MON, hms='15:20:40')
        out = self.h.run(self.h.ctx(WED, '09:20:40', 24560, 54880))
        self.assertEqual(len(self.h.rows(index='NF', arm='primary')), 60)           # no 61st
        self.assertEqual(self.h.rows(index='NF', session_date=WED), [])
        self.assertEqual(self.h.row(index='BNF', arm='primary', session_date=WED)['status'], 'OPEN')
        self.assertTrue(out['counts']['NF']['endpoint_reached'])
        self.assertEqual(out['status'], 'COLLECTING')


# 14 -----------------------------------------------------------------------------------------------
class T14Storage(Base):
    def test_full_ledger_stops_truthfully_but_finishes_open_rows(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        st = self.h.state()
        filler = [{'id': 'f%d' % i, 'session_date': '2026-06-01', 'index': 'X', 'arm': 'primary',
                   'status': 'SKIP'} for i in range(pbr.MAX_RECORDS - len(st['records']))]
        st['records'] += filler
        with open(self.h.path, 'w') as fh:
            json.dump(st, fh)
        out = self.h.run(self.h.ctx(MON, '09:25:40', 24600, 54850))
        self.assertEqual(out['status'], 'JOURNAL_FULL_REVIEW_REQUIRED')
        self.assertEqual(len(self.h.state()['records']), pbr.MAX_RECORDS)          # nothing pruned
        out = self.h.run(self.h.ctx(MON, '09:51:00', 24650, 54800, q_lag_s=10))
        self.assertEqual(out['status'], 'JOURNAL_FULL_REVIEW_REQUIRED')            # sticky
        self.assertEqual(self.h.row(index='NF', arm='primary')['status'], 'RESOLVED')

    def test_size_limit_finishes_open_rows_adds_none_and_is_sticky(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        before = self.h.state()
        cap = len(self.h.raw()) + 400            # room to finalize, not to add a row
        with patch.object(pbr, 'MAX_STATE_BYTES', cap):
            out = self.h.run(self.h.ctx(MON, '09:25:40', 24600, 54850))
            self.assertEqual(out['status'], 'JOURNAL_SIZE_LIMIT')
            self.assertEqual(len(self.h.state()['records']), len(before['records']))
            out = self.h.run(self.h.ctx(MON, '09:50:40', 24650, 54800, q_lag_s=10))
        self.assertEqual(out['status'], 'JOURNAL_SIZE_LIMIT')
        self.assertEqual(self.h.state()['stopped']['reason'], 'JOURNAL_SIZE_LIMIT')

    def test_oversize_file_is_refused_unchanged(self):
        self.h.reference()
        before = self.h.raw()
        with patch.object(pbr, 'MAX_STATE_BYTES', len(before) - 1):
            self.assertEqual(self.h.run(self.h.ctx(MON, '09:21:00', 24560, 54880))['status'], 'JOURNAL_SIZE_LIMIT')
        self.assertEqual(self.h.raw(), before)

    def test_storage_estimate(self):
        """Documented estimate: one fully observed session (4 resolved rows) and the 60/index block."""
        self.h.standard_day()
        rows = self.h.state()['records']
        day = sum(len(json.dumps(r, separators=(',', ':'))) for r in rows)
        self.assertEqual(len(rows), 4)
        # >= 60 sessions needed for 60 resolved per index; 150 fully-entered sessions still fit 1 MB.
        self.assertLess(day * 150 + 4096, pbr.MAX_STATE_BYTES)
        skip = len(json.dumps({'id': '2026-10-19:BNF:delayed', 'session_date': MON, 'index': 'BNF',
                               'arm': 'delayed', 'status': 'SKIP', 'decision_ts_ms': 1792381840000,
                               'reason': 'NO_PRIMARY_ENTRY', 'primary_id': '2026-10-19:BNF:primary',
                               'event_seq': 9}, separators=(',', ':')))
        self.assertLess(skip, 300)


# 15 -----------------------------------------------------------------------------------------------
class T15SnapshotAndAuthority(Base):
    def test_snapshot_carries_bounded_status_only_and_no_trade_hooks(self):
        self.h.reference()
        polls = [{'t': '09:15', 'nf': 24500, 'bnf': 55000, 'vix': 14},
                 {'t': '09:20', 'nf': 24560, 'bnf': 54880, 'vix': 14}]
        ctx = self.h.ctx(MON, '09:20:40', 24560, 54880)
        with patch.object(pbr, '_now_ms', return_value=ctx['x1PollMs']):
            out = json.loads(brain.analyze(json.dumps(polls), '[]', '{}', '[]', '[]', '{}', json.dumps(ctx)))
        snap = brain.take_poll_snapshot(out, ctx, polls, persistence_mode='android_compact_v1')
        mf = json.loads(snap['market_forces_json'])['paper_buying_research']
        self.assertNotIn('records', mf)
        self.assertLessEqual(len(mf['transitions']), 8)
        self.assertEqual({t['index'] for t in mf['transitions']}, {'NF', 'BNF'})
        self.assertFalse(mf['recommendation_authority'])
        self.assertLess(len(json.dumps(mf)), 4096)
        full = json.dumps(snap)
        self.assertNotIn(self.h.tmp, full)
        for key in brain.X1_CONTEXT_KEYS:
            self.assertNotIn(key, json.loads(snap['context_json']))
        full_mode = brain.take_poll_snapshot(out, ctx, polls)
        self.assertNotIn(self.h.tmp, json.dumps(full_mode))
        # no candidate / watchlist / notification entry from research
        self.assertEqual(out['generated_candidates'], [])
        self.assertFalse(any('X1' in json.dumps(c) for c in out.get('watchlist') or []))

    def test_next_poll_snapshot_does_not_repeat_transitions(self):
        self.h.reference()
        self.h.run(self.h.ctx(MON, '09:20:40', 24560, 54880))
        out = self.h.run(self.h.ctx(MON, '09:21:10', 24560, 54880))
        self.assertEqual(out['transitions'], [])
        self.assertEqual(out['event_seq'], self.h.state()['event_seq'])

    def test_paper_trading_outputs_identical_with_and_without_collector(self):
        from test_b510_direction_semantics import args_ctx, run
        self.h.reference()
        args, ctx = args_ctx('fixture_a_bull_credit_range', 'paper')
        ctx_x1 = dict(ctx, **{k: v for k, v in self.h.ctx(MON, '09:20:40', 24560, 54880).items()
                              if k in brain.X1_CONTEXT_KEYS})
        with patch.object(pbr, '_now_ms', return_value=ctx_x1['x1PollMs']):
            a = run(args, ctx_x1)
        b = run(args, ctx)
        self.assertIn('paper_buying_research', a)
        for k in ('verdict', 'watchlist', 'generated_candidates', 'ranked_candidates_full', 'decisionSource'):
            self.assertEqual(json.dumps(a.get(k), sort_keys=True, default=str),
                             json.dumps(b.get(k), sort_keys=True, default=str), k)


# 16 -----------------------------------------------------------------------------------------------
class T16Report(Base):
    def journal(self, nf_n, bnf_n):
        from x1_report_fixtures import journal
        return journal({'NF': {'resolved': nf_n, 'controls': nf_n, 'unresolved': 1,
                               'net': lambda i: 70.0 + (i % 7) * 10},
                        'BNF': {'resolved': bnf_n}})

    def test_reproducible_week_clusters_cohorts_and_verdicts(self):
        st = self.journal(60, 12)
        a, b = x1_report.report(st), x1_report.report(copy.deepcopy(st))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        nf, bnf = a['indices']['NF'], a['indices']['BNF']
        self.assertEqual(nf['primary']['resolved'], 60)
        self.assertEqual(nf['primary']['unresolved'], 1)
        self.assertEqual(nf['primary']['unique_weeks'], 12)
        ci = nf['primary']['bootstrap_95ci_mean_net']
        self.assertEqual((ci['resamples'], ci['seed'], ci['clusters']), (10000, 20261017, 12))
        self.assertLessEqual(ci['lower'], nf['primary']['mean_net'])
        self.assertGreaterEqual(ci['upper'], nf['primary']['mean_net'])
        self.assertEqual(nf['verdict'], 'CRITERIA_MET_REVIEW_REQUIRED')
        self.assertFalse(nf['trading_authority'])
        self.assertEqual(nf['delayed_control']['paired_coverage_of_resolved_primary'], 1.0)
        self.assertEqual(bnf['verdict'], 'INSUFFICIENT_EVIDENCE')          # separate cohort
        self.assertEqual(bnf['delayed_control']['paired_resolved'], 0)

    def test_export_round_trip_and_integrity(self):
        self.h.standard_day()
        raw = self.h.raw().decode()
        import hashlib
        env = {'schema': 'x1_export_v1', 'status': 'OK', 'journal_text': raw,
               'journal_sha256': hashlib.sha256(raw.encode()).hexdigest()}
        p = os.path.join(self.h.tmp, 'export.json')
        with open(p, 'w') as fh:
            json.dump(env, fh)
        journal, integrity = x1_report.load(p)
        self.assertTrue(integrity['journal_sha256_ok'])
        rep = x1_report.report(journal, integrity)
        self.assertTrue(rep['protocol_digest_matches_code'])
        self.assertEqual(rep['indices']['NF']['primary']['resolved'], 1)
        self.assertEqual(rep['indices']['NF']['verdict'], 'INSUFFICIENT_EVIDENCE')
        self.assertIsNone(rep['indices']['NF']['primary']['bootstrap_95ci_mean_net'])   # 1 week only
        env['journal_text'] = raw.replace('"NF"', '"NF" ', 1)
        with open(p, 'w') as fh:
            json.dump(env, fh)
        self.assertFalse(x1_report.load(p)[1]['journal_sha256_ok'])


class ProtocolDocument(unittest.TestCase):
    def test_frozen_protocol_document_matches_code_digest(self):
        doc = os.path.join(HERE, '..', '..', '..', '..', '..', 'docs', 'research',
                           'X1_gap_continuation_v2_causal_paper_20261017.md')
        with open(doc) as fh:
            text = fh.read()
        block = text.split('```json\n', 1)[1].split('\n```', 1)[0]
        import hashlib
        self.assertEqual(json.loads(block), pbr.PROTOCOL)
        self.assertEqual(hashlib.sha256(json.dumps(json.loads(block), sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest(), pbr.DIGEST)
        self.assertIn(pbr.DIGEST, text)


if __name__ == '__main__':
    unittest.main()
