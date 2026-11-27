"""Python half of the paired integration check (fixtures only; no network, no broker)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, '..', '..', 'app', 'src', 'main', 'python')
sys.path[:0] = [PY, os.path.join(PY, 'tests')]

import brain  # noqa: E402
import daily_review_v1 as dr  # noqa: E402
import single_leg_store_v1 as sls  # noqa: E402
from datetime import timedelta  # noqa: E402
import index_authority_fixtures as F  # noqa: E402
import index_authority_v1 as ia  # noqa: E402
import index_single_leg_v1 as sl  # noqa: E402
import managed_evidence_v1 as me  # noqa: E402
import test_index_single_leg_v1_delivery_b as TB  # noqa: E402
from test_single_leg_store_e2e_x1_identity import at  # noqa: E402

# 27 Nov: NF is retired (market_scope_v1.RETIRED_REASON = INDEX_RETIRED_BNF_ONLY) and the PWA renders BNF only,
# so the former NF fixture could no longer reach admission. The active index is BNF; the fixture is a REAL
# published plan_entry('BNF', ...) on the BNF fixture chain (step 100, lot 30, BNF expiry), built the same way
# as the NF one (synthetic chain + v3 snapshot at D-20 s). NF is still exercised, as a refusal.
IX = 'BNF'


def admissible_result(decision_id='idx1_a'):
    c = F.chain(51000.0, 100, lot=30, ts=(TB.D - timedelta(seconds=20)).isoformat(), qty=75, rel_spread=0.02)
    rows, rej = me.parse_option_contracts(IX, TB.contracts_response(c, index=IX, expiry=F.BNF_EXPIRY, lot=30, tick=5))
    assert not rej, rej
    ev = me.build_context({IX: rows}, {}, [])
    ev['snapshots'] = TB.snapshots(c)
    p = sl.plan_entry(IX, TB.dec(), c, 51000.0, session_date=F.SESSION, decision_dt=TB.D, expiry=F.BNF_EXPIRY,
                      history=[], open_trades=[], evidence=ev, captured_lot=30)
    assert p['admit'], p['reasons']
    p['decision_id'] = decision_id
    return {'direction_authority': {'status': 'OK', 'active': ia.POLICY_VERSION},
            'by_index': {IX: {'decision_id': decision_id, 'analysis_ts': TB.D.isoformat(),
                              'session_date': F.SESSION, 'direction': {'value': 'BULL'}}},
            'single_leg_by_index': {IX: p}}


def main(cmd, d, *a):
    if cmd == 'setup':
        print(json.dumps(admissible_result()))
    elif cmd == 'admit':
        rid, capital, result_path = a[0], (None if a[1] == 'null' else float(a[1])), a[2]
        ix = a[3] if len(a) > 3 else IX
        r = open(result_path).read()
        plan = json.loads(r)['single_leg_by_index'].get(ix) or json.loads(r)['single_leg_by_index'][IX]
        st = {'enabled': True, 'paper_mode': True, 'selector': 'BOTH', 'open_trades': [], 'capital': capital,
              'max_risk_pct': json.loads(brain.single_leg_policy_config())['max_risk_pct'], 'config_revision': 1,
              'decisions': {ix: {'decision_id': plan['decision_id'], 'instrument_key': plan['instrument_key']}}}
        sls._wall_clock = at(5)
        print(brain.single_leg_admit(d, r, ix, '[]', True, True, 'BOTH', capital=capital,
                                     max_risk_pct=st['max_risk_pct'], config_json=json.dumps({'revision': 1}),
                                     state_probe=lambda: json.dumps(st), request_id=rid))
    elif cmd == 'status':
        print(brain.single_leg_request_status(d, a[0]))
    elif cmd == 'cancel':
        print(brain.single_leg_request_cancel(d, a[0]))
    elif cmd == 'read':
        print(brain.single_leg_read(d))
    elif cmd == 'review':
        session = json.loads(open(a[0]).read())['by_index'][IX]['session_date']
        print(json.dumps(dr.build_report(session, {'NF': [], 'BNF': []}, [], sls.outcomes(d, session))))


if __name__ == '__main__':
    main(*sys.argv[1:])
