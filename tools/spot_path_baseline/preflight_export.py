"""Preflight one session export against the frozen plan (R5 sections 8, 9 and 10).

Reports eligibility and, where premium capture is empty, traces how far the pipeline got from
the evidence actually present in the export.  Scores nothing.

  preflight_export.py <export.json>
"""
import json, sys, collections, hashlib, os

def main():
    path = sys.argv[1]
    raw = open(path, 'rb').read()
    ex = json.loads(raw)
    m = ex['manifest']
    print('export          %s' % os.path.basename(path))
    print('bytes           %d (cap %s)' % (len(raw), (m.get('limits') or {}).get('max_bundle_bytes')))
    print('sha256          %s' % hashlib.sha256(raw).hexdigest())
    print('session_date    %s' % m.get('session_date_used'))
    print('created_at      %s' % m.get('created_at'))
    p = ex.get('premium') or {}
    print('\n-- premium section --')
    for k in ('state', 'has_data', 'capture_enabled', 'concurrent_capture_write_detected',
              'requested_session_date', 'session_date_substitution', 'section_version'):
        print('  %-34s %s' % (k, p.get(k)))
    eligible = {}
    for ix in sorted((p.get('indices') or {})):
        d = p['indices'][ix]
        recs, envs = d.get('records') or [], d.get('slot_envelopes') or []
        pins = d.get('pinned_contracts') or []
        slots = sorted({r.get('slot') for r in recs if r.get('slot')})
        print('\n  -- %s --' % ix)
        print('    state                          %s' % d.get('state'))
        print('    records / envelopes / pinned    %d / %d / %d' % (len(recs), len(envs), len(pins)))
        print('    first / last premium slot       %s / %s'
              % (slots[0] if slots else 'none', slots[-1] if slots else 'none'))
        ok = bool(recs) and bool(envs) and bool(pins)
        eligible[ix] = ok
        print('    ELIGIBLE under the frozen plan  %s%s'
              % ('YES' if ok else 'NO',
                 '' if ok else '   reason: premium capture produced no %s'
                 % ('records' if not recs else 'envelopes' if not envs else 'pinned contracts')))
    print('\n-- authority spot path (where the evidence stops) --')
    for ix in sorted((ex.get('parts', {}).get('authority_doc', {}).get('history') or {})):
        h = [r for r in ex['parts']['authority_doc']['history'][ix]
             if (r.get('spot') or {}).get('value')]
        if not h:
            print('  %-4s none' % ix)
            continue
        print('  %-4s %d records, first asof %s, last asof %s, last receipt_ts %s, flags %s'
              % (ix, len(h), str(h[0]['spot']['asof'])[11:19], str(h[-1]['spot']['asof'])[11:19],
                 str(h[-1].get('receipt_ts'))[11:19],
                 dict(collections.Counter(r.get('availability') for r in h))))
    if not any(eligible.values()):
        print('\n-- capture trace: how far did the pipeline get? --')
        sc = ((ex.get('parts') or {}).get('learning_ledger') or {}).get('scales') or {}
        for ix in sorted(sc):
            d = sc[ix]
            par = d['k0'] + d['ce_mid'] - d['pe_mid']
            print('  %-4s opening scale %s at %s: k0 %s, ce_mid %s, pe_mid %s'
                  % (ix, d.get('status'), str(d.get('observed_at'))[11:19], d['k0'],
                     d['ce_mid'], d['pe_mid']))
            print('       -> OPTION QUOTES WERE FETCHED. app forward %.3f, parity k0+C-P %.3f, '
                  'residual %.9f' % (d['forward'], par, d['forward'] - par))
        c1 = ((ex.get('parts') or {}).get('c1_capture') or {}).get('evidence') or {}
        for ix in sorted(c1):
            obs = ((c1[ix] or {}).get('observations')) or []
            q = [o for o in obs if o.get('kind') == 'QUOTE']
            if q:
                o = q[0]
                print('  %-4s C1 QUOTE at %s: bid %s ask %s latency_ms %s age_at_receipt_s %s'
                      % (ix, str(o.get('generation_ts'))[11:19], o.get('bid'), o.get('ask'),
                         o.get('latency_ms'), o.get('age_at_receipt_s')))
        ls = (ex.get('parts') or {}).get('learning_ledger_status') or {}
        print('  ledger status %s, errors %s, written %s'
              % (ls.get('status'), ls.get('errors'), ls.get('written')))
        print('  authority_dropped %s, admission_join %s'
              % ((m.get('capture') or {}).get('authority_dropped'),
                 (m.get('capture') or {}).get('admission_join')))
        logs = (ex.get('parts') or {}).get('logs') or []
        prem = [l for l in logs if 'premium' in str(l).lower()]
        print('  retained log lines %d, of which premium-related %d' % (len(logs), len(prem)))
    return 0

if __name__ == '__main__':
    sys.exit(main())
