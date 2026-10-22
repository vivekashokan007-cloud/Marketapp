"""M0 full-day research collector: raw (pre-alignment) forecast capture.

Collection only. This module never changes the verdict, ranking, gates, sizing, exits or any EV v1.2
journal. It copies the synthesis verdict exactly as synthesize_verdict() emitted it, BEFORE
_align_verdict_to_watchlist() can overwrite action/strategy/direction with the execution lane, and records
which poll inputs the brain actually saw (input watermark).

Field rules (Codex M0 brief B4):
  * no probability or horizon is invented: the synthesis verdict declares neither, so `horizon` is
    'UNDECLARED' and `probability` is absent; `confidence` is the raw 0-100 score the rule emitted;
  * the scope is the brain's own declared scope (MARKET_VIEW_SCOPE, a combined NF+BNF context) - it is NOT
    relabelled as a per-index forecast;
  * status: VALID (BULL/BEAR), NEUTRAL (a real NEUTRAL synthesis), MISSING (no synthesis verdict),
    MALFORMED (present but unusable). Neutral and missing are never merged.
The Kotlin collector (ResearchCollector.kt) links the envelope to the scheduled slot, stamps availability
time and refuses it as an earlier slot's forecast if the input watermark names a later poll.
"""
from __future__ import annotations

import copy
import math

RESULT_KEY = 'research_forecast_raw'
SCHEMA = 'research_m0_raw_forecast_v1'
RAW_FIELDS = ('action', 'strategy', 'direction', 'market_direction', 'confidence', 'market_confidence',
              'market_bull_score', 'market_bear_score', 'market_view_status', 'market_view_source',
              'market_direction_scope', 'decision_gate')
DIRECTIONS = ('BULL', 'BEAR', 'NEUTRAL')


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _watermark(polls):
    if not isinstance(polls, list) or not polls:
        return {'input_poll_count': 0, 'input_last_poll_slot_key': None, 'input_last_poll_time': None}
    last = polls[-1] if isinstance(polls[-1], dict) else {}
    slot = last.get('slot_key')
    t = last.get('t', last.get('time'))
    return {
        'input_poll_count': len(polls),
        'input_last_poll_slot_key': slot if isinstance(slot, str) and slot else None,
        'input_last_poll_time': t if isinstance(t, str) and t else None,
    }


def capture(verdict, polls, brain_version, synthesis_error=None):
    """Return the immutable raw envelope body (a fresh dict; the verdict is deep-copied)."""
    out = {'schema': SCHEMA, 'brain_version': brain_version, 'horizon': 'UNDECLARED',
           'semantics': 'synthesize_verdict market view; confidence is a 0-100 rule score, not a probability'}
    out.update(_watermark(polls))
    if synthesis_error is not None or not isinstance(verdict, dict):
        out.update({'status': 'MISSING',
                    'reason': 'synthesis_exception' if synthesis_error is not None else 'no_synthesis_verdict',
                    'raw': None})
        return out
    raw = {k: copy.deepcopy(verdict.get(k)) for k in RAW_FIELDS if k in verdict}
    out['raw'] = raw
    if verdict.get('execution_aligned'):
        out.update({'status': 'MALFORMED', 'reason': 'already_execution_aligned'})
        return out
    md = verdict.get('market_direction', verdict.get('direction'))
    conf = verdict.get('market_confidence', verdict.get('confidence'))
    if verdict.get('market_view_status') != 'AVAILABLE':
        out.update({'status': 'MALFORMED', 'reason': 'market_view_unavailable'})
    elif md not in DIRECTIONS:
        out.update({'status': 'MALFORMED', 'reason': 'unknown_direction'})
    elif not _finite(conf):
        out.update({'status': 'MALFORMED', 'reason': 'non_finite_confidence'})
    elif md == 'NEUTRAL':
        out.update({'status': 'NEUTRAL', 'reason': None})
    else:
        out.update({'status': 'VALID', 'reason': None})
    out['direction'] = md if md in DIRECTIONS else None
    out['confidence'] = float(conf) if _finite(conf) else None
    out['scope'] = verdict.get('market_direction_scope')
    return out
