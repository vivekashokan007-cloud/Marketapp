import os
import sys
import unittest

PY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from notification_lineage import (
    IDEMPOTENCY_SCOPE_POSITION,
    build_notification_lineage,
    merge_agent_state_for_recovery,
    should_suppress_duplicate,
)
from e3_persistence_contract import (
    E3_PERSISTENCE_CONTRACT_VERSION,
    advance_cursor_after_batch,
    build_batch_persistence_cursor,
    mark_streaming_aggregation_verified,
    refuse_false_complete,
)


class NotificationLineageTests(unittest.TestCase):
    def test_idempotency_key_stable_and_suppresses_duplicates(self):
        a = build_notification_lineage(
            scope=IDEMPOTENCY_SCOPE_POSITION,
            alert_key="POS_TARGET_273",
            trade_id=273,
            decision_type="TARGET",
            owner="tick_service",
            session_date="2026-09-12",
        )
        b = build_notification_lineage(
            scope=IDEMPOTENCY_SCOPE_POSITION,
            alert_key="POS_TARGET_273",
            trade_id=273,
            decision_type="TARGET",
            owner="tick_service",
            session_date="2026-09-12",
        )
        self.assertEqual(a["idempotency_key"], b["idempotency_key"])
        seen = set()
        self.assertFalse(should_suppress_duplicate(a, seen))
        self.assertTrue(should_suppress_duplicate(b, seen))

    def test_device_recovery_merge_keeps_latest_ack(self):
        existing = {
            "position_alert_keys": ["POS_TARGET_1"],
            "position_alert_states": {"1:POS_TARGET": 1000},
        }
        incoming = {
            "position_alert_keys": ["POS_STOP_1"],
            "position_alert_states": {"1:POS_TARGET": 2000, "1:POS_STOP": 1500},
        }
        merged = merge_agent_state_for_recovery(existing, incoming)
        self.assertEqual(merged["position_alert_states"]["1:POS_TARGET"], 2000)
        self.assertIn("POS_TARGET_1", merged["position_alert_keys"])
        self.assertIn("POS_STOP_1", merged["position_alert_keys"])


class E3PersistenceContractTests(unittest.TestCase):
    def test_incomplete_cannot_look_complete(self):
        cursor = build_batch_persistence_cursor(
            session_date="2026-09-12",
            completed_snapshots=3,
            total_snapshots=10,
            produced_count=30,
            persisted_count=10,
            phase="batch_persist",
        )
        self.assertEqual(cursor["contract_version"], E3_PERSISTENCE_CONTRACT_VERSION)
        self.assertFalse(cursor["complete"])
        cursor = advance_cursor_after_batch(
            cursor, batch_produced=5, batch_persisted=5, completed_snapshots=4, last_snapshot_id=99
        )
        self.assertFalse(cursor["complete"])
        verified = mark_streaming_aggregation_verified(cursor)
        self.assertFalse(verified["complete"])
        verified["phase"] = "verified"
        verified["complete"] = True
        guarded = refuse_false_complete(verified)
        self.assertFalse(guarded["complete"])

    def test_full_stream_can_verify(self):
        cursor = build_batch_persistence_cursor(
            session_date="2026-09-12",
            completed_snapshots=10,
            total_snapshots=10,
            produced_count=40,
            persisted_count=40,
            phase="stream_aggregate",
        )
        verified = mark_streaming_aggregation_verified(cursor)
        self.assertTrue(verified["complete"])
        self.assertEqual(verified["phase"], "verified")


if __name__ == "__main__":
    unittest.main()
