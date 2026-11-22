#!/usr/bin/env python3
"""A versioned successor to two frozen R7 readers that cannot read every real export.

THE DEFECT, FOUND ON REAL DATA. `premium_repricing_v2.spot_timeline` (line 382) evaluates

    ex['parts']['authority_doc']['history'].get(ix)

with no guard, and `reverse_input` (line 853) subscripts the same place. In the 16 November 2026
export `parts.authority_doc` is JSON **null**, so both raise `TypeError` and the frozen replay
cannot read that export at all.

WHY THE FROZEN MODULE IS NOT PATCHED. It is byte-identical to the base commit
`124d62ee027941e43decc737e53d202c32b1d214` and the protocol's hash test asserts that. Editing it
would break the guarantee that every earlier result was produced by exactly those bytes. So the
guard lives here, in a module with its own version, and the frozen reader is still called first
and unchanged wherever it works.

WHAT THIS IS NOT. It is not a reimplementation. `spot_timeline_guarded` delegates to the frozen
function whenever `parts.authority_doc` is a mapping, and the two are asserted to agree on such
exports. The successor path only handles the shapes the frozen one raises on, and it returns the
same tuple with an empty timeline, because an absent authority document genuinely provides no
spot history -- it is a missing input, not a zero reading.

DIRECT VERSUS PARITY IS REPORTED, NOT INFERRED. Every call returns which chain ran, so a result
can state whether it came from the frozen reader itself or from the successor. A caller that
reports neither is the thing this module exists to prevent.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'spot_path_baseline'))

import premium_repricing_v2 as R7          # noqa: E402  frozen, read never modified

FROZEN_SUCCESSOR_VERSION = 'premium_repricing_v2_successor_v2_null_authority_doc_and_bbo'

#: the chain a call actually used, carried on every result
CHAIN_DIRECT = 'DIRECT_FROZEN'
CHAIN_SUCCESSOR = 'GUARDED_SUCCESSOR'

#: why the successor was needed, when it was
REASON_AUTHORITY_DOC_NULL = 'parts.authority_doc is null'
REASON_AUTHORITY_DOC_ABSENT = 'parts.authority_doc is absent'
REASON_PARTS_ABSENT = 'parts is absent or not a mapping'
REASON_HISTORY_NOT_MAPPING = 'parts.authority_doc.history is not a mapping'
#: a record whose poll failed carries `bbo: null`. `R7.score_index` (line 514) dereferences it
#: unguarded, so one failed poll makes the frozen replay unable to read the whole session.
REASON_NULL_BBO_RECORD = 'a premium record carries bbo: null (a failed poll)'


def authority_doc_state(ex):
    """(readable_by_frozen, reason) -- can the frozen reader subscript this export?

    Named separately so a caller can test the condition without provoking the TypeError, and so
    the reason recorded on a result is the shape that was actually found.
    """
    parts = ex.get('parts')
    if not isinstance(parts, dict):
        return False, REASON_PARTS_ABSENT
    if 'authority_doc' not in parts:
        return False, REASON_AUTHORITY_DOC_ABSENT
    doc = parts['authority_doc']
    if doc is None:
        return False, REASON_AUTHORITY_DOC_NULL
    if not isinstance(doc, dict):
        return False, REASON_AUTHORITY_DOC_NULL
    hist = doc.get('history')
    if hist is not None and not isinstance(hist, dict):
        return False, REASON_HISTORY_NOT_MAPPING
    return True, None


def records_readable_by_frozen(ex, ix):
    """(readable, reason, count) -- can `R7.score_index` subscript every record of this index?

    THE THIRD UNGUARDED DEREFERENCE, found the same way as the first two: by real data arriving.
    `premium_repricing_v2.score_index` (line 514) evaluates `r['bbo'].get('bid_raw')`, and a
    record whose poll FAILED carries `bbo: null` rather than an empty book. On the 20 November
    capture one poll at 13:25 did exactly that for all six pinned contracts in both indices, so
    six records out of 414 make the frozen replay unable to read the session at all.

    The research runner guards this itself (`rej_bbo_absent`); the frozen one cannot be changed,
    so its callers check here first and report which chain they could use.
    """
    node = ((ex.get('premium') or {}).get('indices') or {}).get(ix) or {}
    bad = sum(1 for r in (node.get('records') or []) if not isinstance(r.get('bbo'), dict))
    if bad:
        return False, REASON_NULL_BBO_RECORD, bad
    return True, None, 0


def spot_timeline_guarded(ex, ix):
    """(rows, no_clock, chain, reason) -- the frozen reader where it works, else an empty timeline.

    On a readable export this IS `R7.spot_timeline`, called unchanged. On an export it raises on,
    the successor returns `([], 0)` -- the same shape, with no history, because the authority
    document is missing rather than empty. The distinction is in `reason`, never silent.
    """
    ok, why = authority_doc_state(ex)
    if ok:
        rows, no_clock = R7.spot_timeline(ex, ix)
        return rows, no_clock, CHAIN_DIRECT, None
    return [], 0, CHAIN_SUCCESSOR, why


def reverse_input_guarded(ex):
    """(ex, chain, reason) -- the frozen reverser where it works, else records and envelopes only.

    `R7.reverse_input` reverses records, envelopes AND the authority history. Where the authority
    document cannot be read, the successor reverses the first two and leaves the third alone --
    there is nothing to reverse. The order-independence check it serves is still meaningful,
    because the admission path reads records and envelopes.
    """
    ok, why = authority_doc_state(ex)
    if ok:
        return R7.reverse_input(ex), CHAIN_DIRECT, None
    for ixd in ((ex.get('premium') or {}).get('indices') or {}).values():
        if isinstance(ixd.get('records'), list):
            ixd['records'].reverse()
        if isinstance(ixd.get('slot_envelopes'), list):
            ixd['slot_envelopes'].reverse()
    return ex, CHAIN_SUCCESSOR, why


def describe(ex):
    """A one-line statement of which chain this export will take, for a result header."""
    ok, why = authority_doc_state(ex)
    if ok:
        return ('authority_doc readable: the frozen R7 readers are used directly (%s)'
                % CHAIN_DIRECT)
    return ('authority_doc NOT readable (%s): the guarded successor %s is used for the spot '
            'timeline and the input reverser; every other frozen function is unchanged'
            % (why, FROZEN_SUCCESSOR_VERSION))


if __name__ == '__main__':
    import json
    if len(sys.argv) < 2:
        print(__doc__)
        print('usage: frozen_successor.py <export.json> [<export.json> ...]')
        sys.exit(0)
    for p in sys.argv[1:]:
        with open(p, encoding='utf-8') as fh:
            doc = json.load(fh)
        ok, why = authority_doc_state(doc)
        print('%-44s %-14s %s' % (os.path.basename(p),
                                  CHAIN_DIRECT if ok else CHAIN_SUCCESSOR, why or '-'))
