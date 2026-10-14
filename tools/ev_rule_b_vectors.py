"""Generate the shared Rule B vectors (Python reference -> Kotlin RuleBExitPolicy parity).

Usage: PYTHONPATH=app/src/main/python python3 tools/ev_rule_b_vectors.py > app/src/test/resources/ev_v12/rule_b_vectors.json
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "src", "main", "python"))
import ev_v12_lifecycle as L  # noqa: E402

POS = {"policy_id": "paper_rule_b_v1", "state": "OPEN", "planned_exit_session": "2026-10-12", "expiry": "2026-10-13",
       "legs": [{"qty": -1, "instrument_key": "S"}, {"qty": 1, "instrument_key": "L"}]}


def q(ts_iso, age_ms=1000, sb=10.0, sa=10.5, lb=2.0, la=2.2, drop=None):
    ms = datetime.fromisoformat(ts_iso).timestamp() * 1000 - age_ms
    out = {"S": {"bid": sb, "ask": sa, "ts_ms": ms}, "L": {"bid": lb, "ask": la, "ts_ms": ms}}
    if drop:
        out.pop(drop)
    return {"ts_iso": ts_iso, "quotes": out}


CASES = [
    ("before_planned_session", {}, q("2026-10-09T15:20:00+05:30")),
    ("planned_before_window", {}, q("2026-10-12T15:14:59+05:30")),
    ("planned_window_open_fill", {}, q("2026-10-12T15:15:00+05:30")),
    ("planned_window_last_second", {}, q("2026-10-12T15:27:59+05:30")),
    ("planned_window_end_marks_late", {}, q("2026-10-12T15:28:00+05:30")),
    ("planned_stale_quote_waits", {}, q("2026-10-12T15:20:00+05:30", age_ms=121000)),
    ("planned_missing_leg_waits", {}, q("2026-10-12T15:20:00+05:30", drop="L")),
    ("planned_crossed_quote_waits", {}, q("2026-10-12T15:20:00+05:30", sb=11.0, sa=10.0)),
    ("late_pending_same_day_no_fill", {"state": "LATE_EXIT_PENDING"}, q("2026-10-12T15:29:00+05:30")),
    ("late_pending_next_session_before_0920", {"state": "LATE_EXIT_PENDING"}, q("2026-10-13T09:19:59+05:30")),
    ("late_pending_next_session_fill", {"state": "LATE_EXIT_PENDING"}, q("2026-10-13T09:20:00+05:30")),
    ("late_pending_invalid_quote", {"state": "LATE_EXIT_PENDING"}, q("2026-10-13T10:00:00+05:30", drop="S")),
    ("expiry_deadline_unresolved", {"state": "LATE_EXIT_PENDING"}, q("2026-10-13T15:28:00+05:30")),
    ("after_expiry_unresolved", {}, q("2026-10-14T10:00:00+05:30")),
    ("missed_planned_session_open_next_day", {}, q("2026-10-13T11:00:00+05:30")),
    ("terminal_filled_idempotent", {"state": "EXIT_FILLED"}, q("2026-10-12T15:20:00+05:30")),
    ("terminal_unresolved_idempotent", {"state": "UNRESOLVED"}, q("2026-10-12T15:20:00+05:30")),
    ("unknown_policy_fails_closed", {"policy_id": "paper_rule_c"}, q("2026-10-12T15:20:00+05:30")),
    ("future_quote_rejected", {}, q("2026-10-12T15:20:00+05:30", age_ms=-6000)),
    ("weekend_late_pending", {"state": "LATE_EXIT_PENDING", "planned_exit_session": "2026-10-09"},
     q("2026-10-10T10:00:00+05:30")),
]


def vectors():
    out = []
    for name, over, tick in CASES:
        pos = dict(POS, **over)
        out.append({"name": name, "position": pos, "tick": tick, "expect": L.rule_b_exit_step(pos, tick)})
    return {"schema": "ev_v12_rule_b_vectors_v1", "policy_id": L.POLICY_ID, "vectors": out}


if __name__ == "__main__":
    print(json.dumps(vectors(), indent=1, sort_keys=True))
