"""Capture preflight for one session export.  Scores nothing.

A PASS here means only that the export carries premium capture output, so it may proceed to the
replay's own eligibility checks (plan R5 sections 8 and 9).  It is not itself eligibility, and it
is not a result.

R6 fix.  The first version looked for `pinned_contracts` at the top level of each index.  In a
populated export the contracts live at `premium.indices[ix].cohort.pinned_contracts`, so it
reported zero contracts and rejected both indices of the 12 November export, which carries six
each.  The harness had always resolved this correctly through `find_pinned`, a recursive
resolver written for exactly this nesting -- the preflight hand-rolled a flat lookup instead.

Two things prevent a repeat:
  * the preflight resolves contracts with the harness's OWN `find_pinned`, so there is one
    resolver rather than two;
  * it additionally reports the path it was found at, and FAILS LOUDLY if its own path walk
     disagrees with `find_pinned` on the count.

  preflight_export.py <export.json> [<export.json> ...]

Exit status is 0 when every export's capture preflight passes, 1 otherwise, so it can gate a
replay.
"""
import collections
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from premium_repricing_v2 import find_pinned, resolve_pinned   # noqa: E402  shared resolver



def preflight(path):
    raw = open(path, 'rb').read()
    ex = json.loads(raw)
    m = ex.get('manifest') or {}
    out = []
    say = out.append

    say('=' * 92)
    say('CAPTURE PREFLIGHT  %s' % os.path.basename(path))
    say('=' * 92)
    say('bytes           %d (bundle cap %s)'
        % (len(raw), (m.get('limits') or {}).get('max_bundle_bytes')))
    say('sha256          %s' % hashlib.sha256(raw).hexdigest())
    say('session_date    %s' % m.get('session_date_used'))
    say('created_at      %s' % m.get('created_at'))

    p = ex.get('premium') or {}
    say('')
    say('-- premium section --')
    for k in ('state', 'has_data', 'capture_enabled', 'concurrent_capture_write_detected',
              'requested_session_date', 'session_date_substitution', 'section_version'):
        say('  %-34s %s' % (k, p.get(k)))

    verdicts, mismatches = {}, []
    for ix in sorted(p.get('indices') or {}):
        d = p['indices'][ix] or {}
        recs = d.get('records') or []
        envs = d.get('slot_envelopes') or []
        pins, pin_path, pin_note = resolve_pinned(d)
        # the preflight must never disagree with the harness about what exists
        harness_n = len(find_pinned(d) or [])
        if harness_n != len(pins) and pin_path != 'not present':
            mismatches.append((ix, 'recursive=%d' % harness_n, 'declared=%d' % len(pins)))
        slots = sorted({r.get('slot') for r in recs if r.get('slot')})

        missing = []
        if not recs:
            missing.append('records')
        if not envs:
            missing.append('slot_envelopes')
        if not pins:
            missing.append('pinned_contracts')
        ok = not missing
        verdicts[ix] = ok

        say('')
        say('  -- %s --' % ix)
        say('    state                          %s' % d.get('state'))
        say('    records                        %d' % len(recs))
        say('    slot_envelopes                 %d' % len(envs))
        say('    pinned_contracts               %d   (at %s)' % (len(pins), pin_path))
        if pin_note:
            say('    resolver note                  %s' % pin_note)
        if pins:
            say('    expiries                       %s'
                % sorted({str(c.get('expiry')) for c in pins}))
        say('    first / last premium slot      %s / %s'
            % (slots[0] if slots else 'none', slots[-1] if slots else 'none'))
        if ok:
            say('    CAPTURE PREFLIGHT              PASS  -> may proceed to the replay\'s '
                'eligibility checks')
        else:
            if 'pinned_contracts' in missing and pin_note and pin_note.startswith('REFUSED'):
                why = ('the contract list could not be resolved: %s' % pin_note)
                if len(missing) > 1:
                    why += '; also no ' + ', '.join(m for m in missing if m != 'pinned_contracts')
            else:
                why = 'premium capture produced no ' + ', '.join(missing)
            say('    CAPTURE PREFLIGHT              FAIL  -> index %s is excluded; %s'
                % (ix, why))

    say('')
    say('-- authority spot path --')
    for ix in sorted((ex.get('parts', {}).get('authority_doc', {}).get('history') or {})):
        h = [r for r in ex['parts']['authority_doc']['history'][ix]
             if (r.get('spot') or {}).get('value')]
        if not h:
            say('  %-4s none' % ix)
            continue
        say('  %-4s %d records, asof %s -> %s, last receipt_ts %s, flags %s'
            % (ix, len(h), str(h[0]['spot']['asof'])[11:19], str(h[-1]['spot']['asof'])[11:19],
               str(h[-1].get('receipt_ts'))[11:19],
               dict(collections.Counter(r.get('availability') for r in h))))

    if mismatches:
        say('')
        say('!! RESOLVER MISMATCH -- the preflight and the harness disagree: %s' % mismatches)
        say('   Treat the export as unverifiable until this is reconciled.')

    if verdicts and not any(verdicts.values()):
        say('')
        say('-- capture trace: which stage does the evidence confirm? --')
        sc = ((ex.get('parts') or {}).get('learning_ledger') or {}).get('scales') or {}
        for ix in sorted(sc):
            d = sc[ix]
            try:
                par = d['k0'] + d['ce_mid'] - d['pe_mid']
                say('  %-4s opening scale %s at %s: k0 %s, ce_mid %s, pe_mid %s; forward %.3f '
                    'vs parity k0+C-P %.3f, residual %.9f'
                    % (ix, d.get('status'), str(d.get('observed_at'))[11:19], d['k0'],
                       d['ce_mid'], d['pe_mid'], d['forward'], par, d['forward'] - par))
            except (KeyError, TypeError):
                say('  %-4s opening scale present but incomplete: %s' % (ix, sorted(d)))
        c1 = ((ex.get('parts') or {}).get('c1_capture') or {}).get('evidence') or {}
        for ix in sorted(c1):
            obs = ((c1[ix] or {}).get('observations')) or []
            q = [o for o in obs if o.get('kind') == 'QUOTE']
            if q:
                o = q[0]
                say('  %-4s C1 QUOTE at %s: bid %s ask %s latency_ms %s age_at_receipt_s %s'
                    % (ix, str(o.get('generation_ts'))[11:19], o.get('bid'), o.get('ask'),
                       o.get('latency_ms'), o.get('age_at_receipt_s')))
        ls = (ex.get('parts') or {}).get('learning_ledger_status') or {}
        say('  ledger status %s, errors %s, written %s'
            % (ls.get('status'), ls.get('errors'), ls.get('written')))
        say('  authority_dropped %s, admission_join %s'
            % ((m.get('capture') or {}).get('authority_dropped'),
               (m.get('capture') or {}).get('admission_join')))
        logs = (ex.get('parts') or {}).get('logs') or []
        say('  retained log lines %d, of which premium-related %d'
            % (len(logs), len([l for l in logs if 'premium' in str(l).lower()])))
        say('')
        say('  LIMIT OF THIS EVIDENCE: the scale and C1 rows show that OTHER app paths fetched')
        say('  option quotes that day. They say nothing about whether the premium capture path')
        say('  ran. Skipped, refused, and fetched-but-not-retained are all still open, and this')
        say('  export carries no premium-capture attempt or refusal counter to separate them.')

    return out, (bool(verdicts) and all(verdicts.values())), verdicts


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    all_ok = True
    for path in sys.argv[1:]:
        lines, ok, _v = preflight(path)
        print('\n'.join(lines))
        print('')
        print('OVERALL CAPTURE PREFLIGHT: %s' % ('PASS' if ok else 'FAIL'))
        print('')
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
