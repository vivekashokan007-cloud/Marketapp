import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import index_authority_v1 as ia
import proposal_evidence_v1 as pe
import single_leg_store_v1 as store
import session_evidence_v1 as session_export
import strong_hold_observer_v1 as observer
from test_index_single_leg_v1_delivery_b import chain, snapshots, D
from test_single_leg_store_e2e_x1_identity import OK, admissible_result


class PaperEvidenceIntegration(unittest.TestCase):
    def test_native_quote_bridge_reaches_durable_research_without_changing_position_state(self):
        import brain
        with TemporaryDirectory() as root:
            path = Path(root)/'single_leg_v1'
            path.mkdir()
            position_doc = {'schema': 'mr.single_leg_store.v1', 'positions': [], 'closed': [], 'history': []}
            (path/'positions.json').write_text(json.dumps(position_doc))
            original = (path/'positions.json').read_bytes()
            ms = int(D.timestamp()*1000)
            key = 'NSE_FO|fixture'
            body = {'status':'success','data':{'symbol':{'instrument_token':key,'timestamp':ms,
                'depth':{'buy':[{'price':100,'quantity':65}], 'sell':[{'price':101,'quantity':65}]}}}}
            result = json.loads(brain.single_leg_put_quotes(str(path),json.dumps([key]),json.dumps(body),ms-20,ms,120))
            self.assertEqual(result['stored'],1)
            self.assertEqual(result['research_evidence']['status'],'RECORDED')
            evidence = Path(root)/'proposal_evidence_v1'/f'{D.date()}.json'
            samples = json.loads(evidence.read_text())['exact_quote_samples']
            self.assertEqual(samples[0]['snapshot']['instrument_key'],key)
            self.assertEqual(samples[0]['snapshot']['receipt_ms'],ms)
            self.assertEqual(samples[0]['option_greeks']['iv']['status'],'absent')
            failed = json.loads(brain.single_leg_put_quotes(str(path),json.dumps([key]),'{}',ms+5,ms+10,130))
            self.assertEqual(failed['stored'],0)
            self.assertEqual(failed['research_evidence']['status'],'RECORDED')
            latest = json.loads(evidence.read_text())['exact_quote_samples'][-1]
            self.assertEqual(latest['status'],'MISSING_QUOTE')
            self.assertEqual((path/'positions.json').read_bytes(),original)

    def test_30_and_120_are_research_only_and_continue_after_60_min_exit(self):
        with TemporaryDirectory() as root:
            path = str(Path(root) / 'single_leg_v1')
            direction = {'value': 'BULL', 'strength': 'STRONG', 'er': .6}
            result = admissible_result(decision={'direction': direction}, strong_pilot=True)
            result['by_index']['NF']['direction'] = direction
            self.assertEqual(store.admit(path, result, 'NF', clock=lambda: D, **OK)['status'], 'ADMITTED')
            def tick(minutes):
                now = D + timedelta(minutes=minutes, seconds=2)
                store.monitor(path, snapshots(chain(), gen=now-timedelta(seconds=1)), {}, clock=lambda: now)
            tick(30)
            self.assertEqual(len(store.open_positions(path)), 1)
            tick(60)
            self.assertEqual(store.open_positions(path), [])
            accounts = store.read(path)['closed']
            tick(120)
            doc = store.read(path)
            self.assertEqual(doc['closed'], accounts)
            for key in ('same_30', 'same_60', 'same_120'):
                mark = doc['hold_observations'][0]['marks'][key]
                self.assertEqual(mark['status'], 'SCORED')
                self.assertEqual(mark['iv_provenance']['value'], None)
                self.assertIn('request_start_ms', mark['snapshot'])
                self.assertIn('receipt_ms', mark['snapshot'])
            self.assertEqual(store.open_positions(path), [])

    def test_missed_windows_are_missing_and_cannot_be_filled_retrospectively(self):
        pos = dict(entry_ts=D.isoformat(), holding_plan={'exit_deadline':(D+timedelta(hours=1)).isoformat()},
            expiry='2026-11-03', position_id='p', entry_decision_id='d', index='NF', instrument_key='x',
            action='BUY_CE', strike=24000, entry_fill=150, entry_fee=30, tick_size=.05, units=65)
        row = observer.new_observation(pos, None)
        observer.advance([row], {}, D+timedelta(minutes=45))
        self.assertEqual(row['marks']['same_30']['status'], 'MISSING_QUOTE')
        observer.advance([row], {}, D+timedelta(minutes=30))
        self.assertEqual(row['marks']['same_30']['status'], 'MISSING_QUOTE')
        self.assertEqual(row['marks']['same_120']['status'], 'PENDING')

    def test_exact_reads_keep_contract_clocks_missing_fields_and_explicit_coverage(self):
        with TemporaryDirectory() as root:
            ms = int(D.timestamp()*1000)
            context = {'x':[{'index':'BNF','expiry':'2026-12-29','strike':64000,'decision_id':'d'}]}
            snapshot = {'instrument_key':'x','generation_ts_raw':ms-100,'request_start_ms':ms-200,
                        'receipt_ms':ms,'bid':100,'ask':101}
            pe.record_quote_samples(root, ['x','missing'], {'x':snapshot}, ms-200, ms, 400, context)
            path = Path(root)/'proposal_evidence_v1'/f'{D.date()}.json'
            doc = json.loads(path.read_text()); before = path.read_bytes()
            pe.record_quote_samples(root, ['x','missing'], {'x':snapshot}, ms-200, ms, 400, context)
            self.assertEqual(path.read_bytes(), before)
            by_key = {r['instrument_key']:r for r in doc['exact_quote_samples']}
            self.assertEqual(by_key['x']['snapshot'], snapshot)
            self.assertEqual(by_key['x']['context'], context['x'])
            self.assertEqual(by_key['x']['option_greeks']['iv']['status'], 'absent')
            self.assertEqual(by_key['x']['exchange_quote_ts'], None)
            self.assertEqual(by_key['missing']['status'], 'MISSING_QUOTE')
            self.assertFalse((Path(root)/'single_leg_v1').exists())
            authority = Path(root)/'index_authority_v1'
            authority.mkdir()
            (authority/f'{D.date()}.json').write_text(json.dumps(
                {'session_date': str(D.date()), 'history': {'NF': [], 'BNF': []}}))
            bundle, _ = session_export.build(root, str(D.date()), require_exact=True)
            self.assertEqual(bundle['manifest']['parts']['proposal_evidence']['status'], 'PRESENT')
            self.assertEqual(bundle['parts']['proposal_evidence']['exact_quote_samples'], doc['exact_quote_samples'])
            with patch.object(pe, 'MAX_BYTES', len(before)+8200):
                for n in range(5):
                    pe.record_quote_samples(root,['x'],{'x':snapshot},ms,ms+n+1,401)
                bounded = json.loads(path.read_text())
                self.assertTrue(bounded['truncated']);self.assertGreater(bounded['dropped'],0)
                self.assertLess(path.stat().st_size, pe.MAX_BYTES)

if __name__ == '__main__':
    unittest.main()
