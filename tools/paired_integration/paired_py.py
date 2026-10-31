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
from test_single_leg_store_e2e_x1_identity import admissible_result, at  # noqa: E402


def main(cmd, d, *a):
    if cmd == 'setup':
        print(json.dumps(admissible_result()))
    elif cmd == 'admit':
        rid, capital, result_path = a[0], (None if a[1] == 'null' else float(a[1])), a[2]
        r = open(result_path).read()
        plan = json.loads(r)['single_leg_by_index']['NF']
        st = {'enabled': True, 'paper_mode': True, 'selector': 'BOTH', 'open_trades': [], 'capital': capital,
              'max_risk_pct': json.loads(brain.single_leg_policy_config())['max_risk_pct'], 'config_revision': 1,
              'decisions': {'NF': {'decision_id': plan['decision_id'], 'instrument_key': plan['instrument_key']}}}
        sls._wall_clock = at(5)
        print(brain.single_leg_admit(d, r, 'NF', '[]', True, True, 'BOTH', capital=capital,
                                     max_risk_pct=st['max_risk_pct'], config_json=json.dumps({'revision': 1}),
                                     state_probe=lambda: json.dumps(st), request_id=rid))
    elif cmd == 'status':
        print(brain.single_leg_request_status(d, a[0]))
    elif cmd == 'read':
        print(brain.single_leg_read(d))
    elif cmd == 'review':
        session = json.loads(open(a[0]).read())['by_index']['NF']['session_date']
        print(json.dumps(dr.build_report(session, {'NF': [], 'BNF': []}, [], sls.outcomes(d, session))))


if __name__ == '__main__':
    main(*sys.argv[1:])
