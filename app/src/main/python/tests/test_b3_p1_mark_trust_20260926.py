"""B3 + A1 (2026-09-26): Brain P1 bridge honours the native mark-trust contract.

A LIVE_FULL mark whose native trust assessment is UNTRUSTED (A1 bound/quote
failure) or whose per-leg quote validity is INVALID must not become the Paper
Brain valuation source. Marks from older stores (no trust fields) keep the
prior contract (mixed-version compatibility). Real trades never use P1.
Also a source-contract guard that the tick service keeps POS_VERDICT/POS_BOOK
out of its ownership and does not hard-code a 09:17 ban or two-tick delay.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain  # noqa: E402
from test_paper_brain_p1_bridge_20260924 import _p1_mark, _trade_284  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
KT = os.path.join(ROOT, "app", "src", "main", "java", "com", "marketradar", "app")


def _apply(trade=None, **mark_extra):
    trade = trade or _trade_284()
    ctx = {"p1_position_marks": {"284": _p1_mark(**mark_extra)}, "bnfDTE": 3}
    result = {"position_live": {}}
    ok = brain._try_apply_paper_p1_valuation(trade, result, 284, ctx, spot=52010)
    return ok, trade, result


class TestP1MarkTrustGate(unittest.TestCase):
    def test_trusted_valid_mark_applies_and_records_provenance(self):
        ok, trade, result = _apply(
            mark_trust_state="TRUSTED",
            quote_validity_state="VALID",
            freshness_basis="SOURCE_QUOTE_TIME_AND_LOCAL_RECORD_TIME",
            last_valid_source_quote_ts="2026-09-24T09:38:40.8+05:30",
        )
        self.assertTrue(ok)
        row = result["position_live"][284]
        self.assertEqual(row["p1_mark_trust_state"], "TRUSTED")
        self.assertEqual(row["p1_source_quote_ts"], "2026-09-24T09:38:40.8+05:30")
        self.assertEqual(trade["valuation_quality"], "full")

    def test_untrusted_mark_is_not_applied_even_if_live_full(self):
        for cause in ("WIDE_LIQUIDATION_BOOK", "WIDE_EXECUTABLE_BOOK", "BOUND_REFERENCE_MISMATCH", "UNRESOLVED",
                      "QUOTE_INVALIDITY", "AWAITING_REVALIDATION"):
            ok, trade, result = _apply(mark_trust_state="UNTRUSTED", mark_trust_cause=cause,
                                       quote_validity_state="VALID")
            self.assertFalse(ok, cause)
            self.assertNotIn(284, result["position_live"])
            self.assertNotEqual(trade.get("valuation_quality"), "full")

    def test_invalid_quote_validity_is_not_applied(self):
        ok, _, _ = _apply(mark_trust_state="TRUSTED", quote_validity_state="INVALID")
        self.assertFalse(ok)

    def test_legacy_store_without_trust_fields_keeps_prior_contract(self):
        ok, trade, result = _apply()
        self.assertTrue(ok)
        self.assertNotIn("p1_mark_trust_state", result["position_live"][284])

    def test_real_trade_still_ignores_p1_even_when_trusted(self):
        ok, _, _ = _apply(trade=_trade_284(paper=False), mark_trust_state="TRUSTED",
                          quote_validity_state="VALID")
        self.assertFalse(ok)


class TestTickServiceOwnershipAndNoPolicyShortcuts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(KT, "PositionTickService.kt"), encoding="utf-8") as f:
            cls.service = f.read()
        with open(os.path.join(KT, "PositionQuoteValidity.kt"), encoding="utf-8") as f:
            cls.validity = f.read()
        with open(os.path.join(KT, "ShadowExitNotification.kt"), encoding="utf-8") as f:
            cls.shadow = f.read()

    def test_pos_verdict_and_pos_book_stay_brain_owned(self):
        for src in (self.service, self.validity):
            self.assertNotIn("POS_VERDICT", src)
            self.assertNotIn("POS_BOOK", src)

    def test_no_hard_coded_0917_ban_or_two_tick_stop_delay(self):
        for src in (self.service, self.validity):
            self.assertIsNone(re.search(r"9\s*\*\s*60\s*\+\s*17\b", src))
            self.assertNotIn("09:17", src)
            self.assertNotRegex(src, re.compile(r"two_tick|TWO_TICK|consecutiveStop", re.I))

    def test_price_gate_is_paper_only_and_eod_precedes_untrusted(self):
        self.assertIn("val priceGateApplied = isPaper && !a1.trust.trusted", self.service)
        # B3.1: the precedence moved verbatim into the pure decideShadowAction,
        # which evaluateShadowPolicy calls (and which the JVM tests execute).
        self.assertIn("val action = decideShadowAction(", self.service)
        block = re.search(r"internal fun decideShadowAction\([\s\S]*?\n\}", self.shadow).group(0)
        self.assertLess(block.index("eod -> \"SHADOW_EOD\""),
                        block.index("priceUntrustedCause != null -> \"SHADOW_DEGRADED\""))

    def test_paper_consumes_only_on_post(self):
        # Owner decision 1 (26 Sep 2026): POSTED-only consumption for Paper and Real.
        self.assertIn("internal fun shadowAlertAttemptConsumes(deliveryClass: String): Boolean =\n"
                      "    deliveryClass == DELIVERY_POSTED", self.validity)
        self.assertIn("val consumed = shadowAlertAttemptConsumes(deliveryClass)", self.service)
        self.assertIn("if (consumed) {", self.service)


if __name__ == "__main__":
    unittest.main()
