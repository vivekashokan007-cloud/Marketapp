"""X1 fee identity: 2026 regression pin and synthetic compatibility counterexamples (Codex 2026-10-29 B3).

Design-only support for docs/x1/X1_FEE_CALENDAR_DESIGN_NOTE_20261029.md. Changes no fee, calendar,
protocol or journal behaviour. Every 2027 value below is SYNTHETIC and patched only inside a test.
"""
import copy
import hashlib
import json
import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', '..', '..', '..', 'tools'))
sys.path.insert(0, HERE)

import ev_v12_fees as FEES  # noqa: E402
import paper_buying_research as pbr  # noqa: E402
import x1_report  # noqa: E402
from test_b510_paper_buying_x1 import MON, Harness  # noqa: E402
from test_b510_x1_raw_feed_identity import g  # noqa: E402

# sha256 of every 2026 date's (fee_identity, entry fee, exit fee) for fixed synthetic legs; computed on
# Marketapp main 8e709bb. A 2027 data-only extension must leave this byte-identical.


def fees_2026_digest():
    d = date(2026, 1, 1)
    out = []
    while d.year == 2026:
        s = d.isoformat()
        out.append([s, FEES.fee_identity(s), round(FEES.one_side_fee(0.0, 123.45, 1, 65, s), 9),
                    round(FEES.one_side_fee(98.7, 0.0, 1, 30, s), 9)])
        d += timedelta(days=1)
    return hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()


PINNED = '15778a8d700e7a78c443f282b05ce92cf4edb9503718cb47e2633c21cdd8123e'


class Fees2026Regression(unittest.TestCase):
    def test_every_2026_date_reproduces_pinned_fees_and_identity(self):
        self.assertEqual(fees_2026_digest(), PINNED)

    def test_contract_version_unchanged(self):
        self.assertEqual(FEES.FEE_CONTRACT_VERSION, 'ev_v12_fee_teacher_v2_dated_20261013')


class IdentityGap(unittest.TestCase):
    def test_non_versioned_component_change_is_invisible_in_row_identity(self):
        """A SEBI/stamp/GST/IPFT/brokerage change alters the fee but NOT fee_identity()."""
        s = '2026-11-02'
        fid, fee = FEES.fee_identity(s), FEES.one_side_fee(0.0, 123.45, 1, 65, s)
        for key, val in (('sebi_rate', 0.000002), ('stamp_duty_buy_rate', 0.00005), ('gst_rate', 0.20),
                         ('ipft_rate', 0.000000005), ('brokerage_per_order', 25.0)):
            with mock.patch.dict(FEES._BASE, {key: val}):
                self.assertNotEqual(FEES.one_side_fee(0.0, 123.45, 1, 65, s), fee, key)
                self.assertEqual(FEES.fee_identity(s), fid, key)          # the gap Claude identified


class ReadersTolerateExtension(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.run(self.h.ctx('2026-10-16', '15:20:30', g('fri_1520_reference', 'NF'), g('fri_1520_reference', 'BNF')))
        self.h.run(self.h.ctx(MON, '09:20:40', g('mon_0920_entry', 'NF'), g('mon_0920_entry', 'BNF')))

    def tearDown(self):
        self.h.close()

    def test_extra_fee_identity_keys_pass_state_check_and_replay(self):
        st = self.h.state()
        ext = copy.deepcopy(st)
        for r in ext['records']:
            if isinstance(r.get('fee_identity'), dict):
                r['fee_identity'].update(sebi_rate_version='sebi_synthetic_v1', stamp_rate_version='stamp_synthetic_v1')
        pbr._check_state(ext)                                   # no _Stop
        self.assertEqual(x1_report.validate(ext)[0], x1_report.validate(st)[0])
        self.assertEqual(ext['protocol_digest'], pbr.DIGEST)

    def test_replay_does_not_verify_fee_identity_at_all(self):
        """Finding: x1_report accepts rows whose fee_identity is absent; identity checking is not enforced."""
        st = self.h.state()
        for r in st['records']:
            r.pop('fee_identity', None)
        self.assertEqual(x1_report.validate(st)[0], [])

    def test_contract_version_bump_stops_journal(self):
        st = self.h.state()
        with mock.patch.object(pbr, 'FEE_CONTRACT_VERSION', 'ev_v12_fee_teacher_v3_synthetic'):
            with self.assertRaises(pbr._Stop) as e:
                pbr._check_state(st)
        self.assertEqual(e.exception.status, 'BLOCK_CONTRACT_MISMATCH')


if __name__ == '__main__':
    unittest.main()
