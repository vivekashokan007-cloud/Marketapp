#!/usr/bin/env python3
"""Generate Kotlin ContractRule list from assets/contract_lot_table_v2.json (SSOT).

Usage:
  python3 scripts/generate_contract_lot_table_kt.py
  python3 scripts/generate_contract_lot_table_kt.py --check   # drift only
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "app/src/main/assets/contract_lot_table_v2.json"
KT_PATH = ROOT / "app/src/main/java/com/marketradar/app/ContractLotTable.kt"


def ld(v):
    if v is None:
        return "null"
    return f'LocalDate.parse("{v}")'


def render_rules(rules):
    lines = []
    for r in rules:
        lines.append(
            '        ContractRule("{rid}", "{idx}", {cyc}, {a}, {b}, {c}, {d}, {lot}, "{src}"),'.format(
                rid=r["rule_id"],
                idx=r["index"],
                cyc=("null" if r.get("expiry_cycle") is None else f'"{r["expiry_cycle"]}"'),
                a=ld(r.get("expiry_on_or_after")),
                b=ld(r.get("expiry_on_or_before")),
                c=ld(r.get("observation_on_or_after")),
                d=ld(r.get("observation_on_or_before")),
                lot=int(r["contract_lot_size"]),
                src=r["source_id"],
            )
        )
    # drop trailing comma on last
    if lines:
        lines[-1] = lines[-1].rstrip(",")
    return "\n".join(lines)


def extract_kt_rule_ids(text: str) -> list[str]:
    import re
    return re.findall(r'ContractRule\("([^"]+)"', text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    data = json.loads(JSON_PATH.read_text())
    rules = data["authoritative_contract_rules"]
    json_ids = [r["rule_id"] for r in rules]
    kt_text = KT_PATH.read_text()
    kt_ids = extract_kt_rule_ids(kt_text)
    missing = [i for i in json_ids if i not in kt_ids]
    extra = [i for i in kt_ids if i not in json_ids]
    if args.check:
        if missing or extra or len(json_ids) != len(set(kt_ids)):
            raise SystemExit(f"DRIFT missing={missing} extra={extra} json={len(json_ids)} kt={len(kt_ids)}")
        print(f"OK drift-free: {len(json_ids)} rules")
        return
    block = render_rules(rules)
    start = kt_text.find("    private val rules = listOf(")
    end = kt_text.find("\n    )", start)
    if start < 0 or end < 0:
        raise SystemExit("rules block not found")
    new = kt_text[: start + len("    private val rules = listOf(")] + "\n" + block + kt_text[end:]
    KT_PATH.write_text(new)
    print(f"wrote {len(rules)} rules into ContractLotTable.kt")


if __name__ == "__main__":
    main()
