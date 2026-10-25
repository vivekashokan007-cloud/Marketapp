"""Verify a separate freeze record against immutable protocol bytes.

Exit 0: valid FREEZE; 2: blank/incomplete; 3: invalid/unreadable; 4: DO_NOT_FREEZE.
Checks declared record/hash/date consistency, not human identity or approval authenticity.
Usage: python3 -I verify_freeze_record_20261023.py PROTOCOL.md RECORD.json
"""
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

FIRST_SPANNING = [('BNF H5/H5b', date(2026, 10, 28)), ('BNF H4 k=5', date(2026, 11, 2)),
                  ('NF H5b', date(2026, 11, 4)), ('BNF H4 k=2', date(2026, 11, 5)),
                  ('BNF H1', date(2026, 11, 6))]
REQUIRED = ['protocol_sha256', 'decision', 'decided_by', 'decision_ist_datetime', 'codex_review_reference']


def invalid(reason):
    print('INVALID RECORD: ' + reason)
    return 3


def main(proto_path, record_path):
    try:
        sha = hashlib.sha256(Path(proto_path).read_bytes()).hexdigest()
        rec = json.loads(Path(record_path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        return invalid('unreadable protocol/record: ' + type(exc).__name__)
    print('protocol_sha256 = %s  (%s)' % (sha, proto_path))
    if not isinstance(rec, dict):
        return invalid('record must be a JSON object')
    blank = [key for key in REQUIRED if rec.get(key) is None or rec.get(key) == '']
    if blank:
        print('NOT FROZEN: record incomplete; blank fields: ' + ', '.join(blank))
        return 2
    wrong_types = [key for key in REQUIRED if not isinstance(rec[key], str)]
    if wrong_types:
        return invalid('fields must be strings: ' + ', '.join(wrong_types))
    if not all(rec[key].strip() for key in REQUIRED):
        return invalid('required strings must not be whitespace-only')
    if rec.get('record_schema') != 'special_session_decision_record_v1':
        return invalid('record_schema')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', rec['protocol_sha256']) or rec['protocol_sha256'].lower() != sha:
        return invalid('protocol_sha256 does not match the protocol file')
    if rec['decision'] not in ('FREEZE', 'DO_NOT_FREEZE'):
        return invalid('decision must be FREEZE or DO_NOT_FREEZE')
    if rec['decided_by'] != 'Vivek':
        return invalid('decided_by must be Vivek')
    stamp = rec['decision_ist_datetime']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+05:30', stamp):
        return invalid('decision_ist_datetime must be YYYY-MM-DDTHH:MM+05:30')
    try:
        decided_on = datetime.fromisoformat(stamp).date()
    except ValueError:
        return invalid('decision_ist_datetime is not a valid calendar date/time')
    if rec['decision'] == 'DO_NOT_FREEZE':
        print('DECISION: DO_NOT_FREEZE; this record establishes no freeze')
        return 4
    print('FROZEN: decision IST date %s by %s; Codex ref %s' %
          (decided_on.isoformat(), rec['decided_by'], rec['codex_review_reference']))
    for label, entry in FIRST_SPANNING:
        state = 'PROSPECTIVE' if decided_on < entry else 'RULE_POST_HOC'
        print('  %s: first spanning entry %s -> %s (strictly-before, date-only)' % (label, entry.isoformat(), state))
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(3)
    sys.exit(main(sys.argv[1], sys.argv[2]))
