"""Bounded replay coverage for index_direction_v1 (Delivery A, draft).

Input: a JSON file {"sessions": [{"date": "YYYY-MM-DD", "decisions": [{"polls": [...], "ctx": {...},
"analysis_clock_ms": <frozen analysis clock>}]}]}
captured offline (no network, no Supabase).  For every decision record it recomputes the per-index
authority with brain.compute_index_authority and classifies the outcome into exactly one bucket:

  MISSING_EVIDENCE  - the decision could not be evaluated because an input was absent/invalid
                      (spot, open reference, scale, calendar, chain, lot, quote timestamp ...)
  POLICY_NO_TRADE   - inputs present; the frozen policy itself said no new entry (neutral/unclear,
                      warmup, window closed, no structure allowed ...)
  POLICY_CANDIDATES - inputs present; the policy produced candidates

Parameters are frozen; this tool never tunes anything.  Bounds: MAX_SESSIONS / MAX_DECISIONS.
"""
import json
import os
import sys
import tempfile

MAX_SESSIONS = 60
MAX_DECISIONS = 20000

MISSING_EVIDENCE_CODES = frozenset({
    'SPOT_MISSING', 'SPOT_INVALID', 'SPOT_STALE', 'OPEN_REF_MISSING', 'OPEN_REF_LATE', 'ANCHOR_MISSING',
    'SCALE_INVALID', 'NO_VALID_PARITY_PAIR', 'CALENDAR_UNAVAILABLE', 'EXPIRY_INVALID', 'EXPIRY_NOT_A_SESSION',
    'CHAIN_MISSING', 'CONSTRUCTION_CHAIN_MISSING', 'LOT_UNAVAILABLE', 'QUOTE_INCOMPLETE', 'INDEX_UNAVAILABLE',
    'SESSION_DATE_INVALID', 'DECISION_TIME_INVALID', 'SPARSE_PATH', 'AUTHORITY_UNAVAILABLE',
    'EXCHANGE_TIMESTAMP_MISSING', 'EXCHANGE_TIMESTAMP_INVALID', 'FRESHNESS_UNVERIFIED',
    'ANALYSIS_CLOCK_MISSING', 'CLOCK_SKEW',
})


def _codes(dec):
    out = list(dec.get('reason_codes') or [])
    out += list((dec.get('action') or {}).get('reasons') or [])
    out += list((dec.get('direction') or {}).get('reason_codes') or [])
    flat = []
    for c in out:
        flat += str(c).split('+')
    return flat


def classify(dec):
    if not isinstance(dec, dict):
        return 'MISSING_EVIDENCE', ['AUTHORITY_UNAVAILABLE']
    codes = _codes(dec)
    unavailable = (dec.get('direction') or {}).get('value') == 'UNAVAILABLE' and 'WARMUP' not in codes
    if any(c.startswith('SCALE_') for c in codes) or any(c in MISSING_EVIDENCE_CODES for c in codes) or unavailable:
        return 'MISSING_EVIDENCE', sorted(set(c for c in codes if c in MISSING_EVIDENCE_CODES or c.startswith('SCALE_')))
    if (dec.get('action') or {}).get('value') == 'CANDIDATES':
        return 'POLICY_CANDIDATES', []
    return 'POLICY_NO_TRADE', sorted(set(codes))


def replay(doc, brain):
    out = {'schema': 'direction_replay_coverage_v1', 'bounds': {'max_sessions': MAX_SESSIONS,
           'max_decisions': MAX_DECISIONS}, 'truncated': False, 'sessions': 0, 'decisions': 0, 'by_index': {}}
    n = 0
    sessions = list((doc or {}).get('sessions') or [])
    if len(sessions) > MAX_SESSIONS:
        sessions, out['truncated'] = sessions[:MAX_SESSIONS], True
    for s in sessions:
        out['sessions'] += 1
        # a fresh throwaway state dir per session (scale observed at the open-reference poll is persisted
        # there exactly as on device); never the device state dir
        tmp = tempfile.TemporaryDirectory(prefix='ia_replay_')
        for rec in s.get('decisions') or []:
            if n >= MAX_DECISIONS:
                out['truncated'] = True
                break
            n += 1
            ctx = dict(rec.get('ctx') or {})
            # R5 R1: replay runs at an EXPLICIT frozen analysis clock recorded with the decision; without one
            # the authority is ANALYSIS_CLOCK_MISSING (missing evidence), never the poll's own time
            ctx.pop('analysisClockMs', None)
            if isinstance(rec.get('analysis_clock_ms'), (int, float)) and rec['analysis_clock_ms'] > 0:
                ctx['analysisClockMs'] = int(rec['analysis_clock_ms'])
            ctx['indexAuthorityStateDir'] = os.path.join(tmp.name, 'index_authority_v1')
            try:
                auth = brain.compute_index_authority(rec.get('polls') or [], ctx) or {}
            except Exception as exc:
                auth = {'NF': None, 'BNF': None, '_error': type(exc).__name__}
            for ix in ('NF', 'BNF'):
                b = out['by_index'].setdefault(ix, {'MISSING_EVIDENCE': 0, 'POLICY_NO_TRADE': 0,
                                                    'POLICY_CANDIDATES': 0, 'missing_codes': {}, 'policy_codes': {}})
                bucket, codes = classify(auth.get(ix))
                b[bucket] += 1
                key = 'missing_codes' if bucket == 'MISSING_EVIDENCE' else 'policy_codes'
                for c in codes:
                    b[key][c] = b[key].get(c, 0) + 1
        tmp.cleanup()
    out['decisions'] = n
    return out


def main(argv):
    if len(argv) != 2:
        print('usage: replay_coverage.py <captured_sessions.json>', file=sys.stderr)
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, '..', '..', 'app', 'src', 'main', 'python'))
    import brain
    with open(argv[1], encoding='utf-8') as fh:
        doc = json.load(fh)
    print(json.dumps(replay(doc, brain), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
