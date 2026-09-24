"""Contract + behavioral mirrors for position tick flush diagnostics (R7 2026-09-24).

Covers:
- generic/unverified HTTP 409 retains queue (never drains on status alone)
- verified exact-duplicate 409 drains
- overflow admit preserves prior rows and marks tracking incomplete
- privacy-safe diagnostics (no raw bodies / tick values)
- dedupe same-key same/different payload
- R8 fingerprint covers lot authority + policy_trace (Codex counterexample)
- R8 tracking_complete in mark broadcast / PositionMarkStore
- source contracts through PositionTickService / SupabaseClient / PositionTickFlush
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[4]  # .../app
JAVA_APP = Path(__file__).resolve().parents[2] / "java" / "com" / "marketradar" / "app"
PTS = JAVA_APP / "PositionTickService.kt"
PTF = JAVA_APP / "PositionTickFlush.kt"
SBC = JAVA_APP / "SupabaseClient.kt"
ROOT = APP  # for build.gradle.kts version pin


# --- Pure Python mirror of classify / admit / dedupe for AGP-free runs ---

OK = "ok"
IDEMPOTENT = "idempotent_conflict"
CONFLICT_UNVERIFIED = "conflict_unverified"
CONFIG_AUTH = "config_auth"
SCHEMA = "schema_payload"
TRANSIENT = "transient_network"
SERVER = "server_5xx"
TRANSPORT = "transport"
UNKNOWN = "unknown"

ALLOWLIST_CODE = re.compile(r"^(?:[0-9A-Z]{5}|PGRST[0-9A-Z]+)$", re.I)


def normalize_code(raw):
    if not raw:
        return None
    c = str(raw).strip()
    return c[:32] if ALLOWLIST_CODE.match(c) else None


def extract_code(raw_body):
    if not raw_body:
        return None
    try:
        obj = json.loads(raw_body)
        code = obj.get("code") or (obj.get("error") or {}).get("code")
        return normalize_code(code)
    except Exception:
        m = re.search(r'"code"\s*:\s*"([A-Za-z0-9_]+)"', raw_body)
        return normalize_code(m.group(1) if m else None)


def classify(http_status, exception_type=None, exception_message=None,
             allowlisted_server_code=None, verified_exact_duplicates=False):
    if exception_type:
        simple = exception_type.rsplit(".", 1)[-1]
        blob = (simple + " " + (exception_message or "")).lower()
        if any(x in blob for x in ("timeout", "timedout", "sockettimeout", "unknownhost",
                                   "connectexception", "network")):
            cls = TRANSIENT
        elif any(x in blob for x in ("ssl", "certificate", "handshake")):
            cls = TRANSPORT
        else:
            cls = TRANSPORT
        return {"persisted": False, "failure_class": cls, "http_status": None,
                "exception_type": simple, "allowlisted_server_code": None}
    if http_status is not None and 200 <= http_status <= 299:
        return {"persisted": True, "failure_class": OK, "http_status": http_status,
                "exception_type": None, "allowlisted_server_code": None}
    if http_status == 409:
        if verified_exact_duplicates:
            return {"persisted": True, "failure_class": IDEMPOTENT, "http_status": 409,
                    "exception_type": None,
                    "allowlisted_server_code": normalize_code(allowlisted_server_code)}
        return {"persisted": False, "failure_class": CONFLICT_UNVERIFIED, "http_status": 409,
                "exception_type": None,
                "allowlisted_server_code": normalize_code(allowlisted_server_code)}
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
    return {"persisted": False, "failure_class": cls, "http_status": http_status,
            "exception_type": None,
            "allowlisted_server_code": normalize_code(allowlisted_server_code)}


def apply_decision(pending_before, result):
    if result["persisted"]:
        return 0, True
    return pending_before, False


def admit(existing, incoming, max_pending):
    out = list(existing)
    admitted = 0
    rejected = 0
    for row in incoming:
        if len(out) < max_pending:
            out.append(row)
            admitted += 1
        else:
            rejected += 1
    overflow = rejected > 0 or len(out) > max_pending
    return {
        "queue": out,
        "admitted": admitted,
        "rejected": rejected,
        "overflow_active": overflow,
        "tracking_complete": not overflow,
    }


FINGERPRINT_KEYS = [
    "trade_id", "tick_ts", "session_date", "source", "auth_source",
    "index_key", "strategy_type", "status", "leg_count",
    "quantity_units", "contract_lot_size", "number_of_lots", "lot_authoritative",
    "valuation_quality", "mark_basis",
    "executable_mark", "mid_mark", "ltp_mark",
    "current_pnl", "current_pnl_r", "running_mae", "running_mfe",
    "policy_action", "policy_reason", "policy_trace_json", "legs_json",
]


def fingerprint(row):
    parts = []
    for k in FINGERPRINT_KEYS:
        v = row.get(k, "<missing>")
        parts.append(f"{k}={v}")
    return "|".join(parts)


def dedupe(queue):
    groups = {}
    order = []
    for row in queue:
        key = f"{row.get('trade_id', '__missing__')}|{row.get('tick_ts', '__missing__')}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out = []
    exact = 0
    conflicts = 0
    for key in order:
        rows = groups[key]
        if len(rows) == 1:
            out.append(rows[0])
            continue
        fps = {fingerprint(r) for r in rows}
        if len(fps) == 1:
            out.append(rows[-1])
            exact += len(rows) - 1
        else:
            conflicts += 1
            out.extend(rows)
    return {"queue": out, "exact_dup_dropped": exact, "content_conflicts": conflicts}


def format_insert_fail(result):
    status = result["http_status"] if result["http_status"] is not None else -1
    code = result.get("allowlisted_server_code") or "-"
    ex = result.get("exception_type") or "-"
    return (
        f"POSITION_TICK_INSERT_FAIL: class={result['failure_class']} status={status} "
        f"server_code={code} rows=2 ex={ex} detail=conflict_unverified persisted=false"
    )


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

    def test_409_generic_unverified_retains(self):
        r = classify(409)
        self.assertFalse(r["persisted"])
        self.assertEqual(CONFLICT_UNVERIFIED, r["failure_class"])
        pending, drained = apply_decision(3, r)
        self.assertEqual(3, pending)
        self.assertFalse(drained)

    def test_409_body_duplicate_word_still_unverified(self):
        code = extract_code(
            '{"code":"23505","message":"duplicate key","details":"Key (trade_id)=(286)"}'
        )
        self.assertEqual("23505", code)
        r = classify(409, allowlisted_server_code=code, verified_exact_duplicates=False)
        self.assertEqual(CONFLICT_UNVERIFIED, r["failure_class"])
        self.assertFalse(r["persisted"])

    def test_409_verified_exact_drains(self):
        r = classify(409, allowlisted_server_code="23505", verified_exact_duplicates=True)
        self.assertTrue(r["persisted"])
        self.assertEqual(IDEMPOTENT, r["failure_class"])
        pending, drained = apply_decision(4, r)
        self.assertEqual(0, pending)
        self.assertTrue(drained)

    def test_409_mixed_partial_fail_closed(self):
        r = classify(409, verified_exact_duplicates=False)
        pending, drained = apply_decision(10, r)
        self.assertEqual(10, pending)
        self.assertFalse(drained)

    def test_overflow_preserves_prior_rows(self):
        max_pending = 5
        queue = []
        for i in range(max_pending):
            adm = admit(queue, [{"trade_id": f"T{i}", "tick_ts": f"ts{i}"}], max_pending)
            self.assertEqual(1, adm["admitted"])
            queue = adm["queue"]
        first = queue[0]["trade_id"]
        for round_i in range(20):
            adm = admit(queue, [{"trade_id": f"NEW{round_i}", "tick_ts": f"n{round_i}"}], max_pending)
            self.assertEqual(0, adm["admitted"])
            self.assertEqual(1, adm["rejected"])
            self.assertTrue(adm["overflow_active"])
            self.assertFalse(adm["tracking_complete"])
            queue = adm["queue"]
            self.assertEqual(max_pending, len(queue))
            self.assertEqual(first, queue[0]["trade_id"])
            self.assertTrue(all(r["trade_id"] != f"NEW{round_i}" for r in queue))

    def test_dedupe_same_key_same_payload(self):
        row = {"trade_id": "286", "tick_ts": "t1", "current_pnl": 1.5}
        result = dedupe([dict(row), dict(row)])
        self.assertEqual(1, result["exact_dup_dropped"])
        self.assertEqual(0, result["content_conflicts"])
        self.assertEqual(1, len(result["queue"]))

    def test_dedupe_same_key_different_payload(self):
        q = [
            {"trade_id": "286", "tick_ts": "t1", "current_pnl": 1.0},
            {"trade_id": "286", "tick_ts": "t1", "current_pnl": 2.0},
            {"trade_id": "286", "tick_ts": "t2", "current_pnl": 3.0},
        ]
        result = dedupe(q)
        self.assertEqual(0, result["exact_dup_dropped"])
        self.assertEqual(1, result["content_conflicts"])
        self.assertEqual(3, len(result["queue"]))

    def test_privacy_log_excludes_body_values(self):
        body = (
            '{"code":"23505","details":"Key (trade_id, tick_ts)=(286, 2026-09-24T06:44:00Z)",'
            '"premium":349.54,"authorization":"Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa.bbb",'
            '"apikey":"sb_secret_supersecretvalue123"}'
        )
        code = extract_code(body)
        r = classify(409, allowlisted_server_code=code)
        line = format_insert_fail(r)
        self.assertIn("23505", line)
        self.assertNotIn("286", line)
        self.assertNotIn("349.54", line)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", line)
        self.assertNotIn("sb_secret_supersecretvalue123", line)
        self.assertNotIn("already exists", line)

    def test_backoff_bounded(self):
        def backoff_ms(failures, failure_class):
            base = 60_000
            capped = max(0, min(failures, 10))
            if failure_class in (CONFIG_AUTH, SCHEMA, CONFLICT_UNVERIFIED):
                mult = max(1, min(capped, 5))
            elif failure_class in (SERVER, TRANSIENT, TRANSPORT):
                mult = max(1, min(capped, 8))
            else:
                mult = max(1, min(capped, 6))
            return min(base * mult, 5 * 60_000)

        self.assertEqual(60_000, backoff_ms(1, TRANSIENT))
        self.assertEqual(5 * 60_000, backoff_ms(99, TRANSIENT))


    def test_codex_counterexample_lot_authority_policy_trace_not_duplicates(self):
        """Same trade/ts/marks/P&L but different lot authority + policy_trace → retain both."""
        base = {
            "trade_id": "286",
            "tick_ts": "2026-09-24T05:00:00.000Z",
            "executable_mark": 42.5,
            "current_pnl": 1677.0,
            "quantity_units": 30,
            "contract_lot_size": 15,
            "number_of_lots": 2,
        }
        a = dict(base, lot_authoritative=True, policy_trace_json={"path": "A"})
        b = dict(base, lot_authoritative=False, policy_trace_json={"path": "B"})
        self.assertNotEqual(fingerprint(a), fingerprint(b))
        result = dedupe([a, b])
        self.assertEqual(0, result["exact_dup_dropped"])
        self.assertEqual(1, result["content_conflicts"])
        self.assertEqual(2, len(result["queue"]))

    def test_fingerprint_covers_builder_lot_and_policy_fields(self):
        for k in (
            "quantity_units",
            "contract_lot_size",
            "number_of_lots",
            "lot_authoritative",
            "policy_trace_json",
            "auth_source",
            "legs_json",
        ):
            self.assertIn(k, FINGERPRINT_KEYS)


class PositionTickFlushSourceContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pts = PTS.read_text(encoding="utf-8")
        cls.ptf = PTF.read_text(encoding="utf-8")
        cls.sbc = SBC.read_text(encoding="utf-8")

    def test_flush_uses_detailed_insert_and_persisted_gate(self):
        self.assertIn("insertPositionTicksDetailed", self.pts)
        self.assertIn("applyPositionTickFlushDecision", self.pts)
        self.assertIn("POSITION_TICK_FLUSH_OK", self.pts)
        self.assertIn("dedupePositionTicksByTradeTs", self.pts)
        self.assertIn("admitPositionTicksToBoundedQueue", self.pts)
        # persisted=false is emitted by the shared formatter (privacy-safe).
        self.assertIn("persisted=false", self.ptf)
        self.assertIn("formatPositionTickFlushFailLog", self.pts)

    def test_no_silent_trim_or_queue_drop(self):
        self.assertNotIn("private fun trimQueue", self.pts)
        self.assertNotIn("POSITION_TICK_QUEUE_DROP", self.pts)
        self.assertIn("POSITION_TICK_QUEUE_OVERFLOW", self.pts)
        self.assertIn("tracking_complete=false", self.pts)
        self.assertIn("PREF_OVERFLOW_ACTIVE", self.pts)

    def test_fail_log_includes_class_and_status(self):
        self.assertIn("formatPositionTickFlushFailLog", self.pts)
        self.assertIn("POSITION_TICK_FLUSH_FAIL:", self.ptf)
        self.assertIn("tracking_complete=", self.ptf)

    def test_queue_not_cleared_on_fail(self):
        fail_idx = self.pts.index("formatPositionTickFlushFailLog")
        window = self.pts[fail_idx : fail_idx + 900]
        self.assertIn('putString(PREF_PENDING_QUEUE, queue.toString())', window)
        self.assertNotIn('putString(PREF_PENDING_QUEUE, "[]")', window)

    def test_supabase_never_passes_raw_body_to_classifier(self):
        self.assertIn("extractAllowlistedServerErrorCode", self.sbc)
        self.assertIn("formatPositionTickInsertFailLog", self.sbc)
        self.assertIn("verifiedExactDuplicates = false", self.sbc)
        # Must not pass responseBodySnippet / rawBody into classify.
        insert_idx = self.sbc.index("fun insertPositionTicksDetailed")
        window = self.sbc[insert_idx : insert_idx + 2500]
        self.assertNotIn("responseBodySnippet", window)
        self.assertIn("allowlistedServerErrorCode = serverCode", window)
        # Literal fail tag lives in the shared privacy-safe formatter.
        self.assertIn("POSITION_TICK_INSERT_FAIL:", self.ptf)
        self.assertIn("formatPositionTickInsertFailLog(diag)", window)

    def test_classifier_409_fail_closed_constant(self):
        self.assertIn(f'"{CONFLICT_UNVERIFIED}"', self.ptf)
        self.assertIn("verifiedExactDuplicates", self.ptf)
        # Generic 409 path must set persisted=false for unverified.
        self.assertIn("POSITION_TICK_FLUSH_CONFLICT_UNVERIFIED", self.ptf)

    def test_classifier_constants_present(self):
        for c in (OK, CONFIG_AUTH, SCHEMA, TRANSIENT, SERVER, TRANSPORT, IDEMPOTENT, CONFLICT_UNVERIFIED):
            self.assertIn(f'"{c}"', self.ptf)

    def test_version_unchanged_pin(self):
        gradle = (APP / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('versionName = "2.6.56"', gradle)
        self.assertIn("versionCode = 487", gradle)

    def test_request_started_vs_valuation_ts_still_present(self):
        self.assertIn("batch_b_parity_request_started_ts", self.pts)
        self.assertIn("batch_b_parity_valuation_ts", self.pts)
        self.assertIn("resolveParitySourceQuoteTiming", self.pts)

    def test_dedupe_content_conflict_path_present(self):
        self.assertIn("contentConflicts", self.ptf)
        self.assertIn("POSITION_TICK_QUEUE_CONTENT_CONFLICT", self.pts)
        self.assertIn("positionTickImmutableFingerprint", self.ptf)

    def test_tracking_complete_in_mark_broadcast_and_store(self):
        self.assertIn("EXTRA_POSITION_TICK_TRACKING_COMPLETE", self.ptf)
        self.assertIn("positionTickTrackingBroadcastPayload", self.ptf)
        self.assertIn("POSITION_MARK_BROADCAST_SENT", self.pts)
        pms = (JAVA_APP / "PositionMarkStore.kt").read_text(encoding="utf-8")
        self.assertIn('put("tracking_complete"', pms)
        self.assertIn("readPositionTickTrackingStatus", pms)

    def test_verified_exact_duplicates_remain_unconditional_false(self):
        """Known recovery limitation: live insert path never claims verifiedExactDuplicates."""
        self.assertIn("verifiedExactDuplicates = false", self.sbc)
        self.assertNotIn("verifiedExactDuplicates = true", self.sbc)


if __name__ == "__main__":
    unittest.main()
