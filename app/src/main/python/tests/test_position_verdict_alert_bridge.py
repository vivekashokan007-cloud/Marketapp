import unittest

import brain


class PositionVerdictAlertBridgeTests(unittest.TestCase):
    def test_urgent_structural_book_verdict_emits_brain_alert(self):
        trade = {
            'id': 275, 'index_key': 'NF', 'strategy_type': 'IRON_BUTTERFLY',
            'sell_strike': 23350, 'current_pnl': 1807,
            'max_profit': 11330, 'max_loss': 8170,
            'valuation_quality': 'full', 'forces': {'aligned': 2},
        }
        result = {'positions': {'275': {'verdict': {
            'action': 'BOOK', 'urgency': 'NOW', 'reason': 'expiry day'
        }}}}
        alerts = brain.evaluate_alerts([trade], [], result, {'mins_since_open': 15, 'now_ms': 1789000000000})
        found = [a for a in alerts if a.get('key') == 'POS_VERDICT_BOOK_275']
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['priority'], 'urgent')

    def test_non_urgent_verdict_stays_silent(self):
        trade = {'id': 1, 'index_key': 'NF', 'strategy_type': 'IRON_BUTTERFLY',
                 'sell_strike': 23350, 'current_pnl': 100,
                 'max_profit': 1000, 'max_loss': 1000, 'valuation_quality': 'full'}
        result = {'positions': {1: {'verdict': {'action': 'BOOK', 'urgency': 'SOON'}}}}
        alerts = brain.evaluate_alerts([trade], [], result, {'mins_since_open': 15, 'now_ms': 1789000000000})
        self.assertFalse(any(str(a.get('key', '')).startswith('POS_VERDICT_') for a in alerts))


if __name__ == '__main__':
    unittest.main()
