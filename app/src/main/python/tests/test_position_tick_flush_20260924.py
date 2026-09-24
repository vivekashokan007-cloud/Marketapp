"""Contract + behavioral mirrors for position tick flush diagnostics (2026-09-24).

Covers queue retention after HTTP rejection, timeout/network failure, eventual
success, and that a failed flush is never reported as persisted. Also pins the
Kotlin source so LogBuffer carries failure class + HTTP status.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[4]  # .../app
JAVA_APP = Path(__file__).resolve().parents[2] / "java" / "com" / "marketradar" / "app"
PTS = JAVA_APP / "PositionTickService.kt"
PTF = JAVA_APP / "PositionTickFlush.kt"
SBC = JAVA_APP / "SupabaseClient.kt"
ROOT = APP  # for build.gradle.kts version pin


# --- Pure Python mirror of classifyPositionTickFlushFailure for AGP-free runs ---

OK = "ok"
IDEMPOTENT = "idempotent_conflict"
CONFIG_AUTH = "config_auth"
SCHEMA = "schema_payload"
TRANSIENT = "transient_network"
SERVER = "server_5xx"
TRANSPORT = "transport"
UNKNOWN = "unknown"


def classify(http_status, exception_type=None, exception_message=None):
    if exception_type:
        simple = exception_type.rsplit(".", 1)[-1]
        blob = (simple + " " + (exception_message or "")).lower()
        if any(x in blob for x in ("timeout", "timedout", "sockettimeout", "unknownhost", "connectexception", "network")):
            cls = TRANSIENT
        elif any(x in blob for x in ("ssl", "certificate", "handshake")):
            cls = TRANSPORT
        else:
            cls = TRANSPORT
        return {"persisted": False, "failure_class": cls, "http_status": None, "exception_type": simple}
    if http_status is not None and 200 <= http_status <= 299:
        return {"persisted": True, "failure_class": OK, "http_status": http_status, "exception_type": None}
    if http_status == 409:
        return {"persisted": True, "failure_class": IDEMPOTENT, "http_status": 409, "exception_type": None}
    if http_status in (401, 403):
        cls = CONFIG_AUTH
    elif http_status in (400, 404, 415, 422):
        cls = SCHEMA
    elif http_status is not None and 500 <= http_status <= 599:
        cls = SERVER
    elif http_status in (408, 429):
        cls = TRANSIENT
    else:
        cls = UNKNOWN
    return {"persisted": False, "failure_class": cls, "http_status": http_status, "exception_type": None}


def apply_decision(pending_before, result):
    if result["persisted"]:
        return 0, True
    return pending_before, False


def sanitize(raw, max_len=180):
    if not raw:
        return ""
    s = raw.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*", r"\1***", s)
    s = re.sub(r"(?i)(eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-._]+)", "***jwt***", s)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def backoff_ms(failures, failure_class):
    base = 60_000
    capped = max(0, min(failures, 10))
    if failure_class in (CONFIG_AUTH, SCHEMA):
        mult = max(1, min(capped, 5))
    elif failure_class in (SERVER, TRANSIENT, TRANSPORT):
        mult = max(1, min(capped, 8))
    else:
        mult = max(1, min(capped, 6))
    return min(base * mult, 5 * 60_000)


class PositionTickFlushMirrorTests(unittest.TestCase):
    def test_http_rejection_retains_queue_not_persisted(self):
        r = classify(400)
        pending, drained = apply_decision(15, r)
        self.assertFalse(r["persisted"])
        self.assertEqual(SCHEMA, r["failure_class"])
        self.assertEqual(15, pending)
        self.assertFalse(drained)

    def test_timeout_retains_queue(self):
        r = classify(None, "java.net.SocketTimeoutException", "timeout")
        pending, drained = apply_decision(9, r)
        self.assertEqual(TRANSIENT, r["failure_class"])
        self.assertFalse(r["persisted"])
        self.assertEqual(9, pending)
        self.assertFalse(drained)

    def test_eventual_success_drains(self):
        reject = classify(503)
        pending, drained = apply_decision(7, reject)
        self.assertEqual(7, pending)
        self.assertFalse(drained)
        ok = classify(201)
        pending2, drained2 = apply_decision(pending, ok)
        self.assertTrue(ok["persisted"])
        self.assertEqual(0, pending2)
        self.assertTrue(drained2)

    def test_409_idempotent_drains(self):
        r = classify(409)
        self.assertTrue(r["persisted"])
        self.assertEqual(IDEMPOTENT, r["failure_class"])
        pending, drained = apply_decision(3, r)
        self.assertEqual(0, pending)
        self.assertTrue(drained)

    def test_sanitize_redacts_secrets(self):
        s = sanitize("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb")
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", s)

    def test_backoff_bounded(self):
        self.assertEqual(60_000, backoff_ms(1, TRANSIENT))
        self.assertEqual(5 * 60_000, backoff_ms(99, TRANSIENT))


class PositionTickFlushSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pts = PTS.read_text(encoding="utf-8")
        cls.ptf = PTF.read_text(encoding="utf-8")
        cls.sbc = SBC.read_text(encoding="utf-8")

    def test_flush_uses_detailed_insert_and_persisted_gate(self):
        self.assertIn("insertPositionTicksDetailed", self.pts)
        self.assertIn("applyPositionTickFlushDecision", self.pts)
        self.assertIn("persisted=false", self.pts)
        self.assertIn("POSITION_TICK_FLUSH_OK", self.pts)
        self.assertIn("dedupePositionTicksByTradeTs", self.pts)

    def test_fail_log_includes_class_and_status(self):
        self.assertIn("POSITION_TICK_FLUSH_FAIL:", self.pts)
        self.assertIn("class=${result.failureClass}", self.pts)
        self.assertIn("status=${result.httpStatus ?: -1}", self.pts)
        self.assertIn("backoff_ms=", self.pts)
        self.assertIn("persisted=false", self.pts)

    def test_queue_not_cleared_on_fail(self):
        # On failure branch must rewrite pending queue, not "[]"
        fail_idx = self.pts.index("POSITION_TICK_FLUSH_FAIL:")
        window = self.pts[fail_idx : fail_idx + 800]
        self.assertIn('putString(PREF_PENDING_QUEUE, queue.toString())', window)
        self.assertNotIn('putString(PREF_PENDING_QUEUE, "[]")', window)

    def test_supabase_logs_insert_fail_to_logbuffer(self):
        self.assertIn("POSITION_TICK_INSERT_FAIL:", self.sbc)
        self.assertIn("classifyPositionTickFlushFailure", self.sbc)
        self.assertIn("fun insertPositionTicksDetailed", self.sbc)

    def test_classifier_constants_present(self):
        for c in (OK, CONFIG_AUTH, SCHEMA, TRANSIENT, SERVER, TRANSPORT, IDEMPOTENT):
            self.assertIn(f'"{c}"', self.ptf)

    def test_version_unchanged_pin(self):
        gradle = (APP / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('versionName = "2.6.56"', gradle)
        self.assertIn("versionCode = 487", gradle)

    def test_request_started_vs_valuation_ts_still_present(self):
        self.assertIn("batch_b_parity_request_started_ts", self.pts)
        self.assertIn("batch_b_parity_valuation_ts", self.pts)
        self.assertIn("resolveParitySourceQuoteTiming", self.pts)


if __name__ == "__main__":
    unittest.main()
