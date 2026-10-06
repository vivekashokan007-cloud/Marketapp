"""Generate the non-Paper FII golden fixture from a given brain.py checkout.

Usage: python generate_fii_non_paper_golden.py <dir containing brain.py> <out.json>

Run against the pre-patch base (857dedc) to freeze legacy sandbox / live /
unresolved-mode output. test_fii_short_paper_abstention_20261006 then requires
the patched brain to reproduce every serialized output byte for byte.
"""
import copy
import hashlib
import json
import sys


def cases():
    session, prev = '2026-10-06', '2026-10-05'
    decision_ms = 1791258600000  # 2026-10-06 09:20 IST
    good_prov = {'fii_short_pct_source': 'nse_participant_oi_eod', 'fii_short_pct_published_ms': 1791207000000}  # 2026-10-05 19:00 IST
    histories = {
        'none': [],
        'stale': [{'date': '2026-09-29', 'fii_short_pct': 86}],
        'prev_rising': [{'date': prev, 'fii_short_pct': 86}],
        'prev_covering': [{'date': prev, 'fii_short_pct': 90}],
        'prev_flat': [{'date': prev, 'fii_short_pct': 88}],
        'prev_with_provenance': [dict({'date': prev, 'fii_short_pct': 86}, **good_prov)],
        'prev_nonnumeric': [{'date': prev, 'fii_short_pct': 'x'}],
        'prev_null': [{'date': prev, 'fii_short_pct': None}],
        'multi_unordered': [{'date': '2026-10-01', 'fii_short_pct': 75}, {'date': prev, 'fii_short_pct': 86}],
    }
    mornings = {
        'fsp88': {'fiiShortPct': '88'}, 'fsp65': {'fiiShortPct': '65'}, 'fsp78': {'fiiShortPct': '78'},
        'fsp_inf': {'fiiShortPct': 'inf'}, 'fsp101': {'fiiShortPct': '101'}, 'fsp_neg': {'fiiShortPct': '-5'},
        'fsp_abc': {'fiiShortPct': 'abc'}, 'fsp_blank': {'fiiShortPct': ''},
        'full_bear': {'fiiShortPct': '88', 'fiiCash': '-600', 'upstoxBias': 'Bearish'},
        'full_bull': {'fiiShortPct': '65', 'fiiCash': '900', 'upstoxBias': 'Bullish'},
    }
    modes = {
        'live': {'executionMode': 'live'}, 'sandbox': {'executionMode': 'sandbox'},
        'snake_live': {'execution_mode': 'live'}, 'missing': {}, 'blank': {'executionMode': ''},
        'unknown_real': {'executionMode': 'real'}, 'nonstring': {'executionMode': 1},
        'conflict': {'executionMode': 'paper', 'execution_mode': 'live'},
    }
    for mk, mode in modes.items():
        for hk, hist in histories.items():
            for wk, morning in mornings.items():
                ctx = {'morning_input': dict(morning), 'chain_data': {}, 'yesterdayHistory': copy.deepcopy(hist),
                       'today_ist': session, 'now_ms': decision_ms}
                ctx.update(mode)
                yield f'{mk}|{hk}|{wk}', ctx


def digest(obj):
    """sha256 of the canonical serialization (sorted keys, UTF-8, no ASCII escaping)."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def main(brain_dir, out_path):
    sys.path.insert(0, brain_dir)
    import brain  # noqa: E402
    golden = {}
    for key, ctx in cases():
        entry = {}
        try:
            bias = brain.compute_morning_bias(copy.deepcopy(ctx), [])
            entry['bias_sha256'] = digest(bias)
            try:
                menu = brain._get_varsity_filter(bias, 12.0, 'intraday', False, copy.deepcopy(ctx))
                entry['menu_sha256'] = digest(menu)
            except Exception as exc:  # pragma: no cover - recorded, not hidden
                entry['menu_exception'] = type(exc).__name__
        except Exception as exc:
            entry['bias_exception'] = type(exc).__name__
        golden[key] = entry
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump({'source': 'brain.py @ 857dedc (pre-patch base)', 'cases': golden}, fh, sort_keys=True,
                  ensure_ascii=False, indent=0)
        fh.write('\n')
    print(len(golden), 'cases')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
