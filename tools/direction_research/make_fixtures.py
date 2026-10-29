"""Write deterministic SYNTHETIC fixtures and the golden audit. Usage: python3 make_fixtures.py [--check]"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'tests'))
import dr_core as C  # noqa: E402
import scenarios  # noqa: E402

FIX = os.path.join(HERE, 'fixtures')


def build():
    sc = {name: fn() for name, fn in sorted(scenarios.ALL.items())}
    fx = (json.dumps({'synthetic': True, 'note': 'SYNTHETIC ONLY; not market data; calendars have no holidays',
                      'scenarios': sc}, sort_keys=True, separators=(',', ':')) + '\n').encode()
    b = sc['base']
    st = C.Study(b['sessions'], start=b['start'])
    rows = st.audit(b['forecasts'], b['spots'])
    tbl = C.session_table(rows)
    golden = (json.dumps({'synthetic': True, 'rows': rows, 'sessions': tbl,
                          'monitor': C.blinded_monitor(rows, tbl)}, sort_keys=True, separators=(',', ':')) + '\n').encode()
    return {'fx_counterexamples_v0.json': fx, 'fx_golden_audit_base_v0.json': golden}


def main():
    out = build()
    bad = 0
    for name, data in sorted(out.items()):
        path = os.path.join(FIX, name)
        if '--check' in sys.argv:
            with open(path, 'rb') as fh:
                same = fh.read() == data
            print(('OK   ' if same else 'DIFF ') + name, C.sha256_hex(data))
            bad += not same
        else:
            with open(path, 'wb') as fh:
                fh.write(data)
            print(name, C.sha256_hex(data))
    return bad


if __name__ == '__main__':
    sys.exit(main())
