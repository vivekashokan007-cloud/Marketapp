"""b507 — overnight comparison identity / validation / consumers (Codex overnight-integrity conditions 2, 3, 8)."""
import unittest

import brain
import overnight_inputs as ON
from overnight_fixture import ist_ms, morning, evening, pair, ctx_with_pair

S = "2026-10-15"
NOW = ist_ms(2026, 10, 15, 9, 30)


def check(ov, session=S, now=NOW):
    return ON.validate(ov, session, now)


class Convention(unittest.TestCase):
    def test_intended_session_rules(self):
        self.assertEqual(ON.intended_session(ist_ms(2026, 10, 14, 20, 0)), "2026-10-15")   # after close -> next
        self.assertEqual(ON.intended_session(ist_ms(2026, 10, 15, 8, 0)), "2026-10-15")    # same morning
        self.assertEqual(ON.intended_session(ist_ms(2026, 10, 15, 15, 29)), "2026-10-15")
        self.assertEqual(ON.intended_session(ist_ms(2026, 10, 15, 15, 30)), "2026-10-16")
        self.assertEqual(ON.intended_session(ist_ms(2026, 10, 17, 11, 0)), "2026-10-19")   # Saturday -> Monday
        self.assertEqual(ON.intended_session(ist_ms(2026, 10, 19, 20, 0)), "2026-10-21")   # 20 Oct holiday


class Validation(unittest.TestCase):
    def good(self, e_ms=None, m_ms=None):
        e = evening(e_ms or ist_ms(2026, 10, 14, 20, 0), 1, 46000, 62.5, 25200)
        m = morning(S, m_ms or ist_ms(2026, 10, 15, 9, 5), 2, 46300, 61.0, 25300)
        return pair(S, m, e)

    def test_ok(self):
        r = check(self.good())
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["values"]["dow_from"], 46000.0)
        self.assertEqual(r["identity"]["session"], S)

    def test_same_morning_reference_is_valid(self):
        r = check(self.good(e_ms=ist_ms(2026, 10, 15, 7, 45)))
        self.assertEqual(r["status"], "OK")

    def test_rollover_previous_day_pair_rejected(self):
        # 14 Oct morning + 13 Oct evening re-used on 15 Oct (the incident shape)
        e = evening(ist_ms(2026, 10, 13, 20, 0), 1, 46000, 62.5, 25200)
        m = morning("2026-10-14", ist_ms(2026, 10, 14, 9, 5), 2, 46300, 61.0, 25300)
        ov = pair("2026-10-14", m, e)
        self.assertEqual(check(ov)["reason"], "WRONG_SESSION")
        ov2 = dict(ov, session=S, pair_revision=ON.pair_revision(S, m["revision"], e["revision"]))
        self.assertEqual(check(ov2)["reason"], "MORNING_WRONG_SESSION")

    def test_mixed_pair_rejected(self):
        # fresh 15 Oct morning, evening reference from 13 Oct (intended 14 Oct)
        e = evening(ist_ms(2026, 10, 13, 20, 0), 1, 46000, 62.5, 25200)
        m = morning(S, ist_ms(2026, 10, 15, 9, 5), 2, 46300, 61.0, 25300)
        self.assertEqual(check(pair(S, m, e))["reason"], "REFERENCE_WRONG_SESSION")

    def test_stale_reference_with_forged_intended_session(self):
        e = evening(ist_ms(2026, 10, 13, 20, 0), 1, 46000, 62.5, 25200, intended=S)
        m = morning(S, ist_ms(2026, 10, 15, 9, 5), 2, 46300, 61.0, 25300)
        self.assertEqual(check(pair(S, m, e))["reason"], "REFERENCE_OUTSIDE_WINDOW")

    def test_future_capture_rejected(self):
        r = check(self.good(m_ms=ist_ms(2026, 10, 15, 9, 50)))
        self.assertEqual(r["reason"], "FUTURE_CAPTURE")

    def test_weekend_and_holiday_sessions(self):
        self.assertEqual(check(self.good(), session="2026-10-17")["reason"], "WRONG_SESSION")
        ov = {"schema": ON.SCHEMA, "session": "2026-10-20"}
        self.assertEqual(check(ov, session="2026-10-20")["reason"], "NON_TRADING_SESSION")
        self.assertEqual(check({"schema": ON.SCHEMA, "session": "2027-01-04"}, session="2027-01-04")["reason"]
                         in ("CALENDAR_UNAVAILABLE", "MORNING_NOT_LOCKED"), True)

    def test_weekend_captured_reference_valid_for_monday(self):
        e = evening(ist_ms(2026, 10, 17, 11, 0), 1, 46000, 62.5, 25200)
        m = morning("2026-10-19", ist_ms(2026, 10, 19, 9, 5), 2, 46300, 61.0, 25300)
        r = ON.validate(pair("2026-10-19", m, e), "2026-10-19", ist_ms(2026, 10, 19, 9, 30))
        self.assertEqual(r["status"], "OK")

    def test_missing_and_legacy(self):
        self.assertEqual(check(None)["reason"], "NO_INPUT_IDENTITY")
        self.assertEqual(check({"session": S})["reason"], "UNKNOWN_SCHEMA")
        m = morning(S, ist_ms(2026, 10, 15, 9, 5), 2, 46300, 61.0, 25300)
        self.assertEqual(check(pair(S, m, None))["reason"], "NO_EVENING_REFERENCE")
        self.assertEqual(check(pair(S, None, None))["reason"], "MORNING_NOT_LOCKED")
        legacy = {"dow": "46000", "crude": "62.5", "gift": "25200"}  # pre-b507 record, no identity
        self.assertEqual(check(pair(S, m, legacy))["reason"], "MISSING_IDENTITY")

    def test_malformed_values_and_tamper(self):
        ov = self.good()
        ov["evening"]["dow"] = "abc"
        self.assertEqual(check(ov)["reason"], "INVALID_VALUES")
        ov = self.good()
        ov["evening"]["dow"] = "46001"   # value changed without a new revision
        self.assertEqual(check(ov)["reason"], "REVISION_MISMATCH")
        ov = self.good()
        ov["morning"]["captured_at_ms"] = "x"
        self.assertEqual(check(ov)["reason"], "MISSING_IDENTITY")


class Consumers(unittest.TestCase):
    def base_ctx(self):
        return {'morning_input': {'fiiCash': '100', '_session': S}, 'chain_data': {}, 'yesterdayHistory': [],
                'gap': {'type': 'FLAT', 'gap': 0, 'sigma': 0.9}, 'today_ist': S, 'now_ms': NOW}

    def test_unavailable_contributes_nothing(self):
        ctx = self.base_ctx()
        ctx['eveningClose'] = {'dow': 1, 'crude': 1, 'gift': 1}          # loose legacy keys are ignored
        ctx['globalDirection'] = {'dowClose': 9, 'crudeSettle': 9, 'dowPct': 2.0, 'crudePct': -3.0, 'giftPct': 1.0}
        od = brain.compute_overnight_delta(ctx)
        self.assertEqual((od['status'], od['reason'], od['signals'], od['summary']),
                         ('UNAVAILABLE', 'NO_INPUT_IDENTITY', [], ''))
        ctx['overnightDelta'] = od
        bias = brain.compute_morning_bias(ctx, [])
        rows = [s for s in bias.get('signals', []) if s['name'].startswith('🌙')]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['abstained'])
        self.assertEqual(rows[0]['dir'], 'NEUTRAL')
        self.assertIn('NO_INPUT_IDENTITY', rows[0]['value'])
        self.assertEqual(brain._session_global_direction(ctx), {})     # undated global direction ignored
        self.assertFalse(brain._overnight_available(od))

    def test_votes_identical_with_and_without_unavailable_row(self):
        ctx = self.base_ctx()
        ctx['overnightDelta'] = None
        a = brain.compute_morning_bias(dict(ctx), [])
        ctx['overnightDelta'] = {'status': 'UNAVAILABLE', 'reason': 'X', 'signals': [], 'summary': ''}
        b = brain.compute_morning_bias(dict(ctx), [])
        self.assertEqual(a.get('bias'), b.get('bias'))
        self.assertEqual(a.get('bullVotes', a.get('votes')), b.get('bullVotes', b.get('votes')))

    def test_available_pair_drives_signals(self):
        ctx = ctx_with_pair(self.base_ctx(), eve=(46000, 62.5, 25200), mor=(46300, 61.0, 25300))
        od = brain.compute_overnight_delta(ctx)
        self.assertEqual(od['status'], 'OK')
        self.assertEqual([s['name'] for s in od['signals']], ['Dow', 'Crude', 'GIFT'])
        self.assertEqual(od['signals'][0]['pct'], 0.65)
        self.assertEqual(od['signals'][1]['pct'], -2.4)
        self.assertEqual(od['signals'][2]['pct'], 0.4)
        self.assertEqual(od['identity']['pair_revision'], ctx['overnightInputs']['pair_revision'])
        self.assertTrue(all(not s.get('isSigma') for s in od['signals']))

    def test_session_dated_global_direction_only(self):
        ctx = self.base_ctx()
        ctx['globalDirection'] = {'dowPct': 2.0, 'session': '2026-10-14'}
        self.assertEqual(brain._session_global_direction(ctx), {})
        ctx['globalDirection'] = {'dowPct': 2.0, 'session': S}
        self.assertEqual(brain._session_global_direction(ctx)['dowPct'], 2.0)


if __name__ == '__main__':
    unittest.main()
