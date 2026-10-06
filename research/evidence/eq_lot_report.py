"""Turn executed read-only query outputs into the versioned EQ manifest v2 and the lot cross-check report.

Inputs are the JSON results of eq_rules_v2_versions.sql, eq_rules_v2_counts.sql and
lot_captured_vs_authority.sql (saved verbatim), plus the frozen nf_quotes_v2 extract for EQ7.
Nothing here queries a database; it is deterministic over the saved outputs.
"""
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'replay'))
import replay_engine as re_  # noqa: E402

MANIFEST_VERSION = 'evidence_quality_rules_v2_20261006'


def boundary_from_versions(versions: dict) -> dict:
    first = versions.get('first_snapshot_ge_2_6_66')
    last = versions.get('last_session_checked')
    if first:
        return {'status': 'observed', 'first_snapshot_id': first['id'], 'first_poll_ts': first['poll_ts'],
                'boundary_session': first['session_date'], 'version': first['v'],
                'note': 'EQ1/EQ2 windows end at this IST session (exclusive)'}
    end = (date.fromisoformat(last) + timedelta(days=1)).isoformat() if last else None
    return {'status': 'not_observed', 'last_session_checked': last, 'boundary_session': end,
            'note': 'no stored snapshot at or above 2.6.66; rules stay open and are applied through the last '
                    'session checked'}


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
            'unparseable_version_rows': sum(r.get('unparseable') or 0 for r in per)}


def build_manifest(versions: dict, counts: dict, ds, extract_range) -> dict:
    b = boundary_from_versions(versions)
    return {
        'version': MANIFEST_VERSION,
        'supersedes': 'evidence_quality_rules_v1_20261006 (unimplemented manifest)',
        'reader': {'sql': ['research/evidence/eq_rules_v2_versions.sql', 'research/evidence/eq_rules_v2_counts.sql'],
                   'python': 'research/evidence/eq_lot_report.py', 'extract': 'nf_quotes_v2 parts (EQ7)'},
        'boundary_2_6_66': b,
        'rules': {
            'EQ1_vix_direction_invalid': {'window': ['2026-07-02', b['boundary_session']], 'action': 'exclude',
                                          'executed': counts.get('eq1_vix_direction_invalid')},
            'EQ2_fii_deriv_net_missing_as_zero': {'window': ['2026-07-02', b['boundary_session']],
                                                  'action': 'zero_means_missing',
                                                  'executed': counts.get('eq2_fii_deriv_net_missing_as_zero')},
            'EQ3_fii_short_vote_missing_history': {'action': 'exclude_from_fii_skill',
                                                   'executed': counts.get('eq3_fii_short_vote_missing_history')},
            'EQ4_chain_snapshots_frozen': {'action': 'no_valid_rows', 'executed': None,
                                           'status': 'not_executed_table_schema_unverified'},
            'EQ5_premium_history_frozen': {'action': 'absent', 'executed': None,
                                           'status': 'not_executed_table_schema_unverified'},
            'EQ6_option_chain_partial_capture': {'action': 'exclude_full_chain_research',
                                                 'executed': counts.get('eq6_option_chain_partial_capture')},
            'EQ7_partial_sessions': {'action': 'scheduled_close_missing_outcome_unknown',
                                     'executed': eq7_from_extract(ds, *extract_range)},
            'EQ8_paper_flag_semantics': {'action': 'no_verified_live_money_record',
                                         'executed': counts.get('eq8_paper_flag_semantics')},
            'EQ9_teacher_same_session_only': {'action': 'same_session_horizon_only', 'executed': None,
                                              'status': 'established_by_code_reading_not_by_count'},
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
