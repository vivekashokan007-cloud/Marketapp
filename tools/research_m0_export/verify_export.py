#!/usr/bin/env python3
"""M0 Stage 2 offline tools for a research_m0_export_v1 zip (read-only; never writes evidence, never touches Supabase).

  verify <export.zip>                       independent readback: every entry vs manifest sha/size, SHA256SUMS,
                                            sealed-record digests re-checked; exit 0 VERIFIED, 3 INVALID.
  b9 <export.zip> --slot-id S --attempt-id A --rows R1.json [--rows2 R2.json]
                                            CHAIN_POLL server reproduction. R1 = rows extracted read-only for the
                                            captured index + exact poll_ts (columns: id, poll_ts, index_key, expiry,
                                            strike, option_type, bid, ask, ltp). R2 = independent second extraction
                                            by the source row ids of R1 (WHERE id = ANY(...)). Instrument keys are
                                            resolved ONLY via the attempt's referenced phone instrument-map version;
                                            missing/ambiguous/duplicate keys are rejected. The export must
                                            first pass `verify` (else EXPORT_INVALID, exit 3); referenced records
                                            must be SEALED_OK (else EVIDENCE_UNVERIFIABLE, exit 2). Each readback
                                            must pass on its own. Exit 0 MATCH, 2 MISMATCH/UNVERIFIABLE, 3 INVALID.
The digest is ResearchCollector.canonicalRowsDigest: sha256("m0-chain-rows-v1\\n<index>\\n<n>\\n" + sorted rows).
"""
import argparse, hashlib, json, sys, zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation

ROOT = "research_m0/"


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Invalid(Exception):
    pass


def canon_num(v):
    if v is None:
        return "null"
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return "invalid:" + str(v)
    if not d.is_finite():
        return "invalid:" + str(v)
    d = d.normalize()
    s = format(d, "f")
    return "0" if s in ("-0", "0") or d == 0 else s


def canon_text(v):
    return "null" if v is None else str(v).strip()


def canon_type(v):
    t = canon_text(v).upper()
    return "CE" if t in ("CE", "CALL", "C") else "PE" if t in ("PE", "PUT", "P") else "invalid:" + canon_text(v)


def canonical_row(expiry, strike, typ, key, bid, ask, ltp):
    return "|".join([canon_text(expiry), canon_num(strike), canon_type(typ), canon_text(key), canon_num(bid), canon_num(ask), canon_num(ltp)])


def rows_digest(index, rows):
    return sha(("m0-chain-rows-v1\n%s\n%d\n" % (index, len(rows)) + "\n".join(sorted(rows))).encode())


def open_zip(path):
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise Invalid("not a readable zip: %s" % e)
    names = z.namelist()
    if len(names) != len(set(names)):
        raise Invalid("duplicate zip entries")
    if "export_manifest.json" not in names:
        raise Invalid("export_manifest.json absent")
    try:
        man = json.loads(z.read("export_manifest.json"))
    except ValueError:
        raise Invalid("manifest is not JSON")
    if not isinstance(man, dict) or man.get("schema") != "research_m0_export_v1" or not isinstance(man.get("files"), list):
        raise Invalid("manifest schema/files invalid")
    return z, man


def kind_of(name):
    """Mirror of M0Durable.kindOf: basename without .tmp/.json, up to the first '_' then the first '.'."""
    base = name.rsplit("/", 1)[-1]
    for suf in (".tmp", ".json"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base.split("_", 1)[0].split(".", 1)[0]


def seal_check(raw, name):
    """(body_or_None, reason). reason in OK / MALFORMED / NOT_SEALED / WRONG_OWNER / WRONG_KIND / DIGEST_MISMATCH /
    BODY_MALFORMED - the same fail states M0Durable.validate distinguishes."""
    try:
        o = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, "MALFORMED"
    if not isinstance(o, dict):
        return None, "MALFORMED"
    if o.get("m0_sealed") != 1:
        return None, "NOT_SEALED"
    if o.get("owner") != "research_m0":
        return None, "WRONG_OWNER"
    if o.get("kind") != kind_of(name):
        return None, "WRONG_KIND"
    body = o.get("body")
    if not isinstance(body, str) or sha(body.encode()) != o.get("sha256"):
        return None, "DIGEST_MISMATCH"
    try:
        b = json.loads(body)
    except ValueError:
        return None, "BODY_MALFORMED"
    if not isinstance(b, dict):
        return None, "BODY_MALFORMED"
    return b, "OK"


def unseal(raw, name="x.json"):
    return seal_check(raw, name)[0]


def verify(path):
    z, man = open_zip(path)
    problems = []
    listed = set()
    for f in man["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("sha256"), str) or not isinstance(f.get("size"), int):
            raise Invalid("manifest file entry has wrong types")
        p = f["path"]
        listed.add(p)
        if p not in z.namelist():
            problems.append(p + ": missing from zip")
            continue
        b = z.read(p)
        if sha(b) != f["sha256"] or len(b) != f["size"]:
            problems.append(p + ": bytes differ from manifest")
        rec = f.get("record")
        if rec == "SEALED_OK" and unseal(b, p) is None:
            problems.append(p + ": manifest says SEALED_OK but seal does not verify (" + seal_check(b, p)[1] + ")")
        if rec == "CORRUPT" and unseal(b, p) is not None:
            problems.append(p + ": manifest says CORRUPT but seal verifies")
    for n in z.namelist():
        if n.startswith(ROOT) and n not in listed:
            problems.append(n + ": in zip but not in manifest")
    sums = {}
    if "SHA256SUMS.txt" not in z.namelist():
        raise Invalid("SHA256SUMS.txt absent")
    for line in z.read("SHA256SUMS.txt").decode().splitlines():
        h, _, p = line.partition("  ")
        sums[p] = h
    for p, h in sums.items():
        if p not in z.namelist() or sha(z.read(p)) != h:
            problems.append("SHA256SUMS mismatch: " + p)
    if set(sums) != listed | {"export_manifest.json"}:
        problems.append("SHA256SUMS does not list exactly the manifest files + manifest")
    return {"result": "VERIFIED" if not problems else "INVALID", "files": len(listed), "problems": problems,
            "evidence_status": man.get("evidence_status"), "corrupt": man.get("corrupt"),
            "missing_references": man.get("missing_references")}


def _load_rows(p):
    try:
        with open(p) as fh:
            rows = json.load(fh)
    except (OSError, ValueError) as e:
        raise Invalid("rows file unreadable: %s" % e)
    if not isinstance(rows, list):
        raise Invalid("rows file must be a JSON array")
    for r in rows:
        if not isinstance(r, dict) or not all(k in r for k in ("id", "poll_ts", "index_key", "expiry", "strike", "option_type", "bid", "ask", "ltp")):
            raise Invalid("row missing required columns")
        if not isinstance(r["id"], int) or not isinstance(r["strike"], (int, float)) or isinstance(r["strike"], bool):
            raise Invalid("row id/strike wrong type")
    return rows


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise Invalid("bad poll_ts: %r" % s)


def _record(z, inv, rel):
    """Referenced record for B9: must be listed in the verified inventory as SEALED_OK and re-verify here.
    Returns (body, None) or (None, fail_state)."""
    path = ROOT + rel
    ent = inv.get(path)
    if ent is None or path not in z.namelist():
        return None, "REFERENCED_RECORD_MISSING"
    rec = ent.get("record")
    if rec == "CORRUPT":
        return None, "REFERENCED_RECORD_CORRUPT_AT_SOURCE"     # faithfully copied original corruption
    if rec != "SEALED_OK":
        return None, "REFERENCED_RECORD_UNCHECKED:" + str(rec)
    body, why = seal_check(z.read(path), path)
    if body is None:
        return None, "REFERENCED_RECORD_" + why
    return body, None


def b9(path, slot_id, attempt_id, rows_path, rows2_path=None):
    """CHAIN_POLL server reproduction. Fail-closed order:
    1. the export must pass verify() (inventory sizes/hashes, SHA256SUMS, seal claims) -> else EXPORT_INVALID (exit 3);
    2. the session manifest and referenced instrument map must be SEALED_OK in that inventory and re-verify with the
       right owner/kind -> else EVIDENCE_UNVERIFIABLE (exit 2; a copied-as-is corrupt original is reported as such);
    3. EACH readback independently: no findings (no row discarded), row count == chain_rows, digest ==
       source_rows_sha256; readback 2 must also have exactly readback 1's source-row id set. Any failure -> MISMATCH."""
    v = verify(path)
    if v["result"] != "VERIFIED":
        return {"result": "EXPORT_INVALID", "slot_id": slot_id, "attempt_id": attempt_id, "export_problems": v["problems"]}
    z, man = open_zip(path)
    inv = {f["path"]: f for f in man["files"]}
    parts = slot_id.split("|")
    if len(parts) != 3:
        raise Invalid("slot id must be index|session|ordinal")
    index, session, ordinal = parts
    base = {"slot_id": slot_id, "attempt_id": attempt_id, "export": "VERIFIED", "export_evidence_status": v["evidence_status"]}
    m, fail = _record(z, inv, "manifest_%s.json" % session)
    if fail:
        return dict(base, result="EVIDENCE_UNVERIFIABLE", fail_state=fail, record="manifest_%s.json" % session)
    slot = (((m.get("indices") or {}).get(index) or {}).get("slots") or {}).get(ordinal)
    att = next((a for a in (slot or {}).get("attempts", []) if isinstance(a, dict) and a.get("attempt_id") == attempt_id), None)
    if att is None or not isinstance(att.get("capture"), dict):
        raise Invalid("attempt/capture not found")
    cap = att["capture"]
    want, imv, poll_ts, n_exp = cap.get("source_rows_sha256"), cap.get("imap_version"), cap.get("poll_ts"), cap.get("chain_rows")
    if not isinstance(want, str) or not isinstance(imv, str) or not isinstance(poll_ts, str) or not isinstance(n_exp, int):
        raise Invalid("capture lacks/mistypes source_rows_sha256 / imap_version / poll_ts / chain_rows")
    imap, fail = _record(z, inv, "imap_%s.json" % imv)
    if fail:
        return dict(base, result="EVIDENCE_UNVERIFIABLE", fail_state=fail, record="imap_%s.json" % imv)
    if not isinstance(imap.get("lines"), str):
        return dict(base, result="EVIDENCE_UNVERIFIABLE", fail_state="REFERENCED_RECORD_BODY_SHAPE", record="imap_%s.json" % imv)
    keys, dup = {}, set()
    for line in imap["lines"].split("\n"):
        f = line.split("|")
        if len(f) < 5:
            continue
        k = (f[0], f[1], f[2], f[3])
        if k in keys:
            dup.add(k)
        keys[k] = "|".join(f[4:])
    t0 = _ts(poll_ts)

    def build(rows, label):
        out, ids, seen, findings = [], [], set(), []
        for r in rows:
            if r["index_key"] != index or _ts(r["poll_ts"]) != t0:
                findings.append({"readback": label, "id": r["id"], "field": "poll/index", "reason": "row from another poll or index"})
                continue
            k = (index, canon_text(r["expiry"]), str(int(r["strike"])), canon_type(r["option_type"]))
            if k in seen:
                findings.append({"readback": label, "id": r["id"], "reason": "duplicate server row for key", "key": "|".join(k)})
                continue
            seen.add(k)
            if k in dup:
                findings.append({"readback": label, "id": r["id"], "reason": "ambiguous instrument key in map", "key": "|".join(k)})
                continue
            if k not in keys:
                findings.append({"readback": label, "id": r["id"], "reason": "instrument key missing from map", "key": "|".join(k)})
                continue
            out.append(canonical_row(r["expiry"], r["strike"], r["option_type"], keys[k], r["bid"], r["ask"], r["ltp"]))
            ids.append(r["id"])
        d = rows_digest(index, out)
        rb = {"input_rows": len(rows), "accepted_rows": len(out), "digest": d, "source_row_ids": sorted(ids), "findings": findings}
        fails = []
        if findings:
            fails.append("findings")
        if len(rows) != len(out):
            fails.append("rows_discarded")
        if len(out) != n_exp:
            fails.append("row_count_%d_expected_%d" % (len(out), n_exp))
        if d != want:
            fails.append("digest_mismatch")
        rb["failures"] = fails
        rb["ok"] = not fails
        return rb

    r1 = build(_load_rows(rows_path), 1)
    res = dict(base, imap_version=imv, poll_ts=poll_ts, expected_rows=n_exp, expected_sha256=want, readback_1=r1,
               digest_1=r1["digest"], source_row_ids=r1["source_row_ids"], server_rows=r1["accepted_rows"])
    failures = ["readback_1:" + f for f in r1["failures"]]
    findings = list(r1["findings"])
    if rows2_path:
        r2 = build(_load_rows(rows2_path), 2)
        res["readback_2"] = r2
        res["digest_2"] = r2["digest"]
        res["ids_equal"] = r1["source_row_ids"] == r2["source_row_ids"]
        failures += ["readback_2:" + f for f in r2["failures"]]
        findings += r2["findings"]
        if not res["ids_equal"]:
            failures.append("readback_2:source_row_ids_differ_from_readback_1")
    res["findings"] = findings
    res["failures"] = failures
    res["result"] = "MATCH" if not failures else "MISMATCH"
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    v = sp.add_parser("verify"); v.add_argument("zip")
    b = sp.add_parser("b9"); b.add_argument("zip"); b.add_argument("--slot-id", required=True); b.add_argument("--attempt-id", required=True)
    b.add_argument("--rows", required=True); b.add_argument("--rows2")
    a = ap.parse_args(argv)
    try:
        r = verify(a.zip) if a.cmd == "verify" else b9(a.zip, a.slot_id, a.attempt_id, a.rows, a.rows2)
    except Invalid as e:
        print(json.dumps({"result": "INVALID", "reason": str(e)}))
        return 3
    print(json.dumps(r, indent=2))
    return {"VERIFIED": 0, "MATCH": 0, "MISMATCH": 2, "EVIDENCE_UNVERIFIABLE": 2}.get(r["result"], 3)


if __name__ == "__main__":
    sys.exit(main())
