"""B1.1 (27 Sep 2026): cross-language reference for position_tick_client_event_id_v1.

The Kotlin implementation lives in PositionTickIdentity.kt. This file is an
independent Python implementation of the same canonical form; both must produce
the same digest for the shared golden fixture (PositionTickDrainTest.GOLDEN_ID).
It can also be used offline to audit or (if ever approved) plan a backfill.
It performs no network or database access.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from decimal import Decimal
from pathlib import Path

CONTRACT = "position_tick_client_event_id_v1"
EXCLUDED = {"id", "created_at", "client_event_id"}
JAVA_APP = Path(__file__).resolve().parents[2] / "java" / "com" / "marketradar" / "app"
TEST_KT = Path(__file__).resolve().parents[3] / "test" / "java" / "com" / "marketradar" / "app" / "PositionTickDrainTest.kt"
MIGRATION = Path(__file__).resolve().parents[5] / "supabase" / "migrations" / "20260927090000_position_ticks_client_event_id.sql"


def _num(d: Decimal) -> str:
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def _str(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def canonical(v, top: bool = False) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, Decimal):
        return _num(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, list):
        return "[" + ",".join(canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted(k for k in v if not (top and k in EXCLUDED))
        return "{" + ",".join(_str(k) + ":" + canonical(v[k]) for k in keys) + "}"
    raise TypeError(type(v))


def client_event_id(row_json: str) -> str:
    row = json.loads(row_json, parse_float=Decimal, parse_int=Decimal)
    pre = CONTRACT + "\n" + canonical(row, top=True)
    return hashlib.sha256(pre.encode("utf-8")).hexdigest()


GOLDEN_ROW = (
    '{"trade_id":"T42","tick_ts":"2026-09-24T06:31:55.581Z","leg_count":4,'
    '"quantity_units":30.0,"lot_authoritative":true,"ltp_mark":null,"current_pnl":-1234.50,'
    '"policy_trace_json":{"b":[1,2.50,"x\\"y"],"a":{"z":null,"k":"\\u00e9\\n"}},'
    '"id":99,"created_at":"2026-09-24T06:31:56Z"}'
)


class TickIdentityReferenceTests(unittest.TestCase):
    def test_canonical_form(self):
        row = json.loads(GOLDEN_ROW, parse_float=Decimal, parse_int=Decimal)
        self.assertEqual(
            '{"current_pnl":-1234.5,"leg_count":4,"lot_authoritative":true,"ltp_mark":null,'
            '"policy_trace_json":{"a":{"k":"\u00e9\\u000a","z":null},"b":[1,2.5,"x\\"y"]},'
            '"quantity_units":30,"tick_ts":"2026-09-24T06:31:55.581Z","trade_id":"T42"}',
            canonical(row, top=True),
        )

    def test_golden_digest_matches_kotlin_test(self):
        digest = client_event_id(GOLDEN_ROW)
        kt = TEST_KT.read_text(encoding="utf-8")
        m = re.search(r'const val GOLDEN_ID = "([0-9a-f]{64})"', kt)
        self.assertIsNotNone(m, "Kotlin golden id missing")
        self.assertEqual(m.group(1), digest)

    def test_number_forms_and_exclusions(self):
        a = client_event_id('{"trade_id":"T1","current_pnl":30,"x":1.0E-7}')
        b = client_event_id('{"x":0.0000001,"current_pnl":30.000,"trade_id":"T1","id":5,"created_at":"z","client_event_id":"q"}')
        self.assertEqual(a, b)
        self.assertNotEqual(a, client_event_id('{"trade_id":"T1","current_pnl":30,"x":1.0E-7,"ltp_mark":null}'))

    def test_producer_gate_pinned_off(self):
        src = (JAVA_APP / "PositionTickIdentity.kt").read_text(encoding="utf-8")
        self.assertIn("internal const val POSITION_TICK_CLIENT_EVENT_ID_SEND_COMPILED = false", src)

    def test_migration_prepared_not_partial_and_marked_not_applied(self):
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("NOT APPLIED", sql)
        self.assertIn("add column if not exists client_event_id text", sql)
        self.assertRegex(sql, r"create unique index if not exists position_ticks_client_event_id_uidx\s+on public\.position_ticks \(client_event_id\) nulls distinct;")
        body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        self.assertNotIn("update public.position_ticks", body.lower())
        self.assertNotIn("delete", body.lower())


if __name__ == "__main__":
    unittest.main()
