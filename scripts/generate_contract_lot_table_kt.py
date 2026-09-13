#!/usr/bin/env python3
"""Generate Kotlin ContractRule list from assets/contract_lot_table_v2.json (SSOT).

Usage:
  python3 scripts/generate_contract_lot_table_kt.py
  python3 scripts/generate_contract_lot_table_kt.py --check   # full block compare
"""
from __future__ import annotations

import argparse
import json
import re
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
    if lines:
        lines[-1] = lines[-1].rstrip(",")
    return "\n".join(lines)


def extract_kt_rules_block(text: str) -> str:
    start = text.find("    private val rules = listOf(")
    if start < 0:
        raise SystemExit("rules block start not found")
    end = text.find("\n    )", start)
    if end < 0:
        raise SystemExit("rules block end not found")
    return text[start:end + len("\n    )")]


def extract_kt_version(text: str) -> str | None:
    m = re.search(r'const val VERSION_ID = "([^"]+)"', text)
    return m.group(1) if m else None


def parse_kt_rule_fields(text: str) -> list[dict]:
    """Parse ContractRule(...) entries for full-field compare."""
    pattern = re.compile(
        r'ContractRule\("(?P<rule_id>[^"]+)",\s*"(?P<index>[^"]+)",\s*'
        r'(?P<cycle>null|"[^"]*"),\s*'
        r'(?P<a>null|LocalDate\.parse\("[^"]+"\)),\s*'
        r'(?P<b>null|LocalDate\.parse\("[^"]+"\)),\s*'
        r'(?P<c>null|LocalDate\.parse\("[^"]+"\)),\s*'
        r'(?P<d>null|LocalDate\.parse\("[^"]+"\)),\s*'
        r'(?P<lot>\d+),\s*"(?P<source>[^"]+)"\)'
    )
    out = []
    for m in pattern.finditer(text):
        def unld(s):
            if s == "null":
                return None
            return s[len('LocalDate.parse("'):-2]
        cycle = m.group("cycle")
        out.append({
            "rule_id": m.group("rule_id"),
            "index": m.group("index"),
            "expiry_cycle": None if cycle == "null" else cycle.strip('"'),
            "expiry_on_or_after": unld(m.group("a")),
            "expiry_on_or_before": unld(m.group("b")),
            "observation_on_or_after": unld(m.group("c")),
            "observation_on_or_before": unld(m.group("d")),
            "contract_lot_size": int(m.group("lot")),
            "source_id": m.group("source"),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    data = json.loads(JSON_PATH.read_text())
    rules = data["authoritative_contract_rules"]
    version = str(data.get("version_id") or "")
    kt_text = KT_PATH.read_text()
    expected_block_inner = render_rules(rules)
    expected_block = "    private val rules = listOf(\n" + expected_block_inner + "\n    )"
    actual_block = extract_kt_rules_block(kt_text)
    kt_version = extract_kt_version(kt_text)
    kt_rules = parse_kt_rule_fields(kt_text)

    if args.check:
        errors = []
        if version and kt_version and version != kt_version:
            errors.append(f"version drift json={version} kt={kt_version}")
        if actual_block.replace(" ", "").replace("\n", "") != expected_block.replace(" ", "").replace("\n", ""):
            # Structure/byte compare of rendered block
            if actual_block != expected_block:
                errors.append("rules block structure/byte mismatch vs regenerated JSON")
        if len(rules) != len(kt_rules):
            errors.append(f"rule count json={len(rules)} kt={len(kt_rules)}")
        for i, (jr, kr) in enumerate(zip(rules, kt_rules)):
            for field in (
                "rule_id", "index", "expiry_cycle",
                "expiry_on_or_after", "expiry_on_or_before",
                "observation_on_or_after", "observation_on_or_before",
                "contract_lot_size", "source_id",
            ):
                jv = jr.get(field)
                kv = kr.get(field)
                if field == "contract_lot_size":
                    jv = int(jv)
                    kv = int(kv)
                if jv != kv:
                    errors.append(f"field drift rule[{i}] {jr.get('rule_id')} {field}: json={jv!r} kt={kv!r}")
        if errors:
            raise SystemExit("DRIFT:\n  - " + "\n  - ".join(errors[:40]))
        print(f"OK full-block drift-free: {len(rules)} rules version={version or kt_version}")
        return

    start = kt_text.find("    private val rules = listOf(")
    end = kt_text.find("\n    )", start)
    if start < 0 or end < 0:
        raise SystemExit("rules block not found")
    new = kt_text[: start + len("    private val rules = listOf(")] + "\n" + expected_block_inner + kt_text[end:]
    KT_PATH.write_text(new)
    print(f"wrote {len(rules)} rules into ContractLotTable.kt")


if __name__ == "__main__":
    main()
