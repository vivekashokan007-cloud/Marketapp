#!/usr/bin/env python3
"""Worked fee cases a reviewer can check by hand, printed from the governed model itself.

Codex asked for the fee extension to come back "separately with dated authoritative sources and
worked cases for review". The sources live in `FEE_EXTENSION_REVIEW_20261121.md`; the arithmetic
lives here, and it is printed BY the model rather than typed beside it, so a figure in the
document cannot drift from the code that produced it.

Each case shows every component separately, with the rate it came from and the base it was
applied to, so each line can be recomputed with a calculator from the circular alone.

Run:
    python3 -I tools/premium_model_validation/fee_worked_cases.py --ledger-root <tree>
"""
import argparse
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import economic_check as EC                                            # noqa: E402

#: Cases chosen to exercise each component, not to flatter the model. The first two are real
#: 16 November NIFTY/BANKNIFTY rows; the rest are edge cases a reviewer should see.
CASES = (
    ('a real 16 Nov NIFTY row: buy 139.15, sell 141.50, lot 65',
     '2026-11-16', '2026-11-16', 65, 139.20, 141.45),
    ('a real 16 Nov BANKNIFTY row: buy 420.00, sell 405.00, lot 35',
     '2026-11-16', '2026-11-16', 35, 420.05, 404.95),
    ('a losing round trip, to show STT is charged on the SALE regardless of profit',
     '2026-11-16', '2026-11-16', 65, 200.00, 100.00),
    ('a one-rupee premium, where the Rs 20 per-order brokerage dominates',
     '2026-11-16', '2026-11-16', 65, 1.00, 1.00),
    ('the day the window opens, 1 April 2026 (STT 0.15% applies)',
     '2026-04-01', '2026-04-01', 65, 100.00, 100.00),
    ('the day before it opens, 31 March 2026 (the PREVIOUS window, STT 0.10%)',
     '2026-03-31', '2026-03-31', 65, 100.00, 100.00),
)

#: Dates that must be REFUSED, not estimated. The whole point of a dated table.
REFUSALS = ('2026-11-17', '2026-12-01', '2027-01-05')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--ledger-root', required=True)
    a = ap.parse_args(argv)
    led, why = EC.load_ledger(a.ledger_root)
    if led is None:
        print('REFUSED: %s' % why)
        return 2
    F = led.FEES
    tree = EC.ledger_tree_state(a.ledger_root)

    print('=' * 100)
    print('WORKED FEE CASES   %s' % F.RATES_TABLE_VERSION)
    print('=' * 100)
    print('verified through %s -- a later trade date is REFUSED, never estimated'
          % F.RATES_VERIFIED_THROUGH.isoformat())
    print('tree %s%s' % ((tree.get('revision') or '?')[:12],
                         '  MODIFIED: ' + ', '.join(tree['modified'])
                         if tree.get('modified') else '  clean'))
    print('convention: an outright long. Entry buys at `buy_px`; exit sells at `sell_px`.')
    print('            entry = {sell_px: 0, buy_px: paid}; exit = {exit_sell_px: got,')
    print('            exit_buy_px: 0}; legs = 1, so brokerage is 2 orders.')
    print()

    for label, edate, xdate, lot, buy_px, sell_px in CASES:
        ed = datetime.date.fromisoformat(edate)
        xd = datetime.date.fromisoformat(xdate)
        try:
            f = F.fees_v2(ed, xd, 1, lot,
                          {'sell_px': 0.0, 'buy_px': buy_px},
                          {'exit_sell_px': sell_px, 'exit_buy_px': 0.0})
        except Exception as exc:
            print('--- %s' % label)
            print('    REFUSED: %r' % exc)
            print()
            continue
        we = f['entry_rates']
        buy_turnover = buy_px * lot
        sell_turnover = sell_px * lot
        gross = F.gross_inr(lot, {'sell_px': 0.0, 'buy_px': buy_px},
                            {'exit_sell_px': sell_px, 'exit_buy_px': 0.0})
        print('--- %s' % label)
        print('    window %s  (%s to %s)' % (we['window_id'], we['start'], we['end']))
        print('    buy  turnover  %10.2f = %8.2f x %d' % (buy_turnover, buy_px, lot))
        print('    sell turnover  %10.2f = %8.2f x %d' % (sell_turnover, sell_px, lot))
        print('    %-12s %12s   %s' % ('component', 'INR', 'how'))
        print('    %-12s %12.4f   Rs %.0f per executed order x 2 orders'
              % ('brokerage', f['brokerage'], we['brokerage_per_order']))
        print('    %-12s %12.4f   %.7f%% x (%.2f + %.2f) turnover, each side'
              % ('exchange', f['exchange'], 100 * we['exchange_txn'],
                 buy_turnover, sell_turnover))
        print('    %-12s %12.4f   %.7f%% x (%.2f + %.2f) turnover, each side'
              % ('ipft', f['ipft'], 100 * we['ipft'], buy_turnover, sell_turnover))
        print('    %-12s %12.4f   %.4f%% x %.2f  (SALE only, seller pays)'
              % ('stt', f['stt'], 100 * we['stt_option_sale'], f['stt_base']))
        print('    %-12s %12.4f   %.4f%% x %.2f  (BUY only, buyer pays)'
              % ('stamp', f['stamp'], 100 * we['stamp_option_buy'], f['stamp_base']))
        print('    %-12s %12.4f   %.4f%% x %.2f turnover'
              % ('sebi', f['sebi'], 100 * we['sebi'], f['turnover']))
        print('    %-12s %12.4f   %.0f%% x (brokerage + exchange + ipft)'
              % ('gst', f['gst'], 100 * we['gst']))
        print('    %-12s %12.4f' % ('TOTAL', f['total']))
        print('    gross P&L      %12.4f   (%.2f - %.2f) x %d'
              % (gross, sell_px, buy_px, lot))
        print('    net P&L        %12.4f   gross - fees' % (gross - f['total']))
        # the arithmetic check a reviewer would do: components must sum to the total
        parts = sum(f[k] for k in ('brokerage', 'exchange', 'ipft', 'stt', 'stamp',
                                   'sebi', 'gst'))
        assert abs(parts - f['total']) < 1e-9, 'components do not sum to the total'
        print('    components sum to the total: %.10f == %.10f' % (parts, f['total']))
        print()

    print('--- dates the table must REFUSE rather than estimate ---')
    for d in REFUSALS:
        dd = datetime.date.fromisoformat(d)
        try:
            F.fees_v2(dd, dd, 1, 65, {'sell_px': 0.0, 'buy_px': 100.0},
                      {'exit_sell_px': 100.0, 'exit_buy_px': 0.0})
            print('    %s  NOT REFUSED -- this is a defect' % d)
        except Exception as exc:
            print('    %s  refused: %s' % (d, type(exc).__name__))
    print()
    print('--- a zero or missing lot must refuse, never default ---')
    for lot in (0, None):
        try:
            F.fees_v2(datetime.date(2026, 11, 16), datetime.date(2026, 11, 16), 1, lot,
                      {'sell_px': 0.0, 'buy_px': 100.0},
                      {'exit_sell_px': 100.0, 'exit_buy_px': 0.0})
            print('    lot=%r  NOT REFUSED -- this is a defect' % lot)
        except Exception as exc:
            print('    lot=%r  refused: %s' % (lot, type(exc).__name__))
    print()
    print('=' * 100)
    print('Every line above is printed by the model, so a figure in the review document')
    print('cannot drift from the code. The sources for each rate are in')
    print('FEE_EXTENSION_REVIEW_20261121.md, with what remains unproven stated there.')
    print('=' * 100)
    return 0


if __name__ == '__main__':
    sys.exit(main())
