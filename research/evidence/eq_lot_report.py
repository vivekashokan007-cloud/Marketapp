"""Turn executed read-only query outputs into the versioned EQ manifest v2 and the lot cross-check report.

Inputs are the JSON results of eq_rules_v2_versions.sql, eq_boundary_snapshots.sql, eq_rules_v2_counts.sql,
eq_rules_v2_eq4_eq5_eq9.sql and lot_captured_vs_authority.sql (saved verbatim), plus the frozen nf_quotes_v2
extract for EQ7.
Nothing here queries a database; it is deterministic over the saved outputs.
"""
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'replay'))
import replay_engine as re_  # noqa: E402

MANIFEST_VERSION = 'evidence_quality_rules_v2_1_20261006'
SUPERSEDES = ('evidence_quality_rules_v2_20261006 (first-candidate-only boundary, EQ2 outside-window zeros '
              'mislabelled legitimate, EQ4/EQ5/EQ9 unexecuted)')


CANDIDATE_SOURCE = 'ml_generated_candidates.brain_version'
SNAPSHOT_SOURCE = 'ml_brain_snapshots.context_json.snapshot_brain_version'


def _snapshot_boundary(snapshots: dict, window) -> dict:
    """First stored snapshot at or above 2.6.66 inside a bounded window. The window must bracket the change:
    its first session needs snapshots and none of them at or above 2.6.66, otherwise an earlier snapshot could
    exist outside it and the claim is refused (fail closed)."""
    per = snapshots.get('per_session') or []
    first = snapshots.get('first_snapshot_ge_2_6_66')
    out = {'source': SNAPSHOT_SOURCE, 'window_checked': [str(w) for w in window] if window else None,
           'per_session': [{k: r.get(k) for k in ('session_date', 'snapshots', 'version_list', 'ge_2_6_66',
                                                  'null_version_rows', 'first_poll', 'last_poll')} for r in per]}
    head = per[0] if per else None
    if not window or not head or str(head['session_date']) != str(window[0]) or not head.get('snapshots') \
            or head.get('ge_2_6_66'):
        out.update(status='window_not_bracketing', first_snapshot_ts=None)
        return out
    if not first:
        out.update(status='not_observed_in_window', first_snapshot_ts=None)
        return out
    out.update(status='observed', first_snapshot_ts=first['poll_ts'], first_snapshot_session=str(first['session_date']),
               version=first['v'],
               scope='first stored snapshot at or above 2.6.66 within the window; the window start session holds only '
                     'lower versions, and snapshots before the window are not read (the candidate-row read covers '
                     'every earlier session)')
    return out


def boundary_from_versions(versions: dict, snapshots: dict = None, snapshot_window=None) -> dict:
    """2.6.66 boundary. The first-candidate time comes from candidate rows; the first-snapshot time, when a
    bounded snapshot read is supplied, from the snapshot record. The exclusion boundary is the earliest IST
    session at which either source first shows 2.6.66 (EQ1/EQ2 windows end there, exclusive)."""
    first = versions.get('first_poll_ge_2_6_66')
    last = versions.get('last_session_checked')
    cand = {'source': CANDIDATE_SOURCE,
            'first_candidate_ts': first['poll_ts'] if first else None,
            'first_candidate_session': str(first['session_date']) if first else None,
            'note': 'polls that generated no candidate leave no row here'}
    snap = _snapshot_boundary(snapshots, snapshot_window) if snapshots is not None else None
    sessions = [s for s in (cand['first_candidate_session'],
                            snap.get('first_snapshot_session') if snap and snap['status'] == 'observed' else None) if s]
    out = {'first_candidate': cand, 'first_snapshot': snap}
    if snap is not None and snap['status'] == 'window_not_bracketing':
        out.update(status='refused_window_not_bracketing', boundary_session=None,
                   note='snapshot window does not bracket the version change; widen it before claiming a boundary')
        return out
    if sessions:
        out.update(status='observed', boundary_session=min(sessions),
                   version='2.6.66', note='EQ1/EQ2 windows end at this IST session (exclusive)')
        return out
    end = (date.fromisoformat(str(last)) + timedelta(days=1)).isoformat() if last else None
    out.update(status='not_observed', last_session_checked=str(last) if last else None, boundary_session=end,
               note='no stored poll at or above 2.6.66; rules stay open and are applied through the last session checked')
    return out


def eq7_from_extract(ds: 're_.Dataset', start: date, end: date) -> dict:
    rows = []
    for d in re_.exchange_sessions(start, end):
        c = ds.windows.get((d, 'C', 'nf'))
        if c and c[0]:
            continue
        last = ds.windows.get((d, 'L', 'nf'))
        rows.append({'session': d.isoformat(),
                     'last_nf_poll_ts': last[0].isoformat() if last and last[0] else None,
                     'kind': 'partial_session_no_scheduled_close' if last and last[0] else 'no_nf_polls'})
    return {'sessions_checked': len(re_.exchange_sessions(start, end)), 'sessions_without_scheduled_close': rows}


def eq10_from_versions(versions: dict) -> dict:
    per = versions.get('per_session') or []
    mixed = [r for r in per if (r.get('versions') or 0) > 1]
    return {'sessions_checked': len(per), 'mixed_version_sessions': mixed,
            'null_version_rows': sum(r.get('null_version_rows') or 0 for r in per),
            'unparseable_version_rows': sum(r.get('unparseable_rows') or 0 for r in per),
            'sessions_without_any_version': [r['session_date'] for r in per if not r.get('versions')]}


def _frozen_verdict(x: dict) -> dict:
    if x is None:
        return {'status': 'not_executed', 'executed': None}
    ok = x.get('rows_on_or_after_frozen') == 0 and x.get('rows_created_on_or_after_frozen') == 0
    return {'status': 'confirmed_no_row_on_or_after_frozen_date' if ok else 'violated_rows_exist_after_frozen_date',
            'executed': x}


def _eq9_verdict(x: dict) -> dict:
    if x is None:
        return {'status': 'not_executed', 'executed': None}
    if x.get('exit_after_session'):
        status = 'violated_labels_exit_after_entry_session'
    elif x.get('exit_ts_null') or x.get('exit_before_session') or x.get('session_date_null'):
        status = 'confirmed_no_overnight_exit_with_unclassified_rows'
    else:
        status = 'confirmed_all_exits_on_entry_session'
    return {'status': status, 'executed': x}


def build_manifest(versions: dict, counts: dict, ds, extract_range, snapshots: dict = None,
                   snapshot_window=None, eq459: dict = None) -> dict:
    b = boundary_from_versions(versions, snapshots, snapshot_window)
    eq459 = eq459 or {}
    sql = ['research/evidence/eq_rules_v2_versions.sql', 'research/evidence/eq_rules_v2_counts.sql']
    if snapshots is not None:
        sql.append('research/evidence/eq_boundary_snapshots.sql')
    if eq459:
        sql.append('research/evidence/eq_rules_v2_eq4_eq5_eq9.sql')
    return {
        'version': MANIFEST_VERSION,
        'supersedes': SUPERSEDES,
        'reader': {'sql': sql, 'python': 'research/evidence/eq_lot_report.py', 'extract': 'nf_quotes_v2 parts (EQ7)'},
        'boundary_2_6_66': b,
        'rules': {
            'EQ1_vix_direction_invalid': {'window': ['2026-07-02', b['boundary_session']], 'action': 'exclude',
                                          'executed': counts.get('eq1_vix_direction_invalid')},
            'EQ2_fii_deriv_net_missing_as_zero': {'window': ['2026-07-02', b['boundary_session']],
                                                  'action': 'zero_means_missing',
                                                  'outside_window_zeros': 'unverified; not admissible to training '
                                                                          'without row-level provenance',
                                                  'executed': counts.get('eq2_fii_deriv_net_missing_as_zero')},
            'EQ3_fii_short_vote_missing_history': {'action': 'exclude_from_fii_skill',
                                                   'executed': counts.get('eq3_fii_short_vote_missing_history')},
            'EQ4_chain_snapshots_frozen': dict(action='no_valid_rows',
                                               **_frozen_verdict(eq459.get('eq4_chain_snapshots_frozen'))),
            'EQ5_premium_history_frozen': dict(action='absent',
                                               **_frozen_verdict(eq459.get('eq5_premium_history_frozen'))),
            'EQ6_option_chain_partial_capture': {'action': 'exclude_full_chain_research',
                                                 'executed': counts.get('eq6_option_chain_partial_capture')},
            'EQ7_partial_sessions': {'action': 'scheduled_close_missing_outcome_unknown',
                                     'executed': eq7_from_extract(ds, *extract_range)},
            'EQ8_paper_flag_semantics': {'action': 'no_verified_live_money_record',
                                         'executed': counts.get('eq8_paper_flag_semantics')},
            'EQ9_teacher_same_session_only': dict(action='same_session_horizon_only',
                                                  **_eq9_verdict(eq459.get('eq9_teacher_same_session_only'))),
            'EQ10_device_version_mixing': {'action': 'mixed_or_partial_sessions_not_homogeneous',
                                           'executed': eq10_from_versions(versions)},
        },
    }


def lot_report(rows: list, resolver=re_.project_lot_resolver) -> dict:
    out, agree, disagree, gaps, unresolved = [], 0, 0, 0, 0
    for r in rows:
        idx = {'NIFTY': 'NF', 'NF': 'NF', 'BANKNIFTY': 'BNF', 'BNF': 'BNF'}.get(str(r.get('index_key')).upper())
        d = date.fromisoformat(r['d'])
        exp = date.fromisoformat(r['expiry']) if r.get('expiry') else None
        lot, prov = (resolver(idx, d, exp) if idx and exp else (None, {'unavailable_reason': 'identity_incomplete'}))
        cap = r.get('captured_contract_lot')
        try:
            cap_i = int(float(cap)) if cap not in (None, '') and float(cap) == int(float(cap)) else None
        except ValueError:
            cap_i = None
        if lot is None:
            verdict = 'authority_unresolved'
            unresolved += r['n']
        elif cap_i is None:
            verdict = 'captured_missing'
            gaps += r['n']
        elif cap_i == lot:
            verdict = 'agree'
            agree += r['n']
        else:
            verdict = 'disagree'
            disagree += r['n']
        out.append(dict(r, index=idx, authority_lot=lot, authority_source=prov.get('lot_provenance'),
                        authority_rule=prov.get('rule_id'), unavailable_reason=prov.get('unavailable_reason'),
                        verdict=verdict))
    return {'trades_agree': agree, 'trades_disagree': disagree, 'trades_captured_missing': gaps,
            'trades_authority_unresolved': unresolved, 'rows': out}


if __name__ == '__main__':
    print(json.dumps({'version': MANIFEST_VERSION}))
