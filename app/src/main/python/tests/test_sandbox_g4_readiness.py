"""G4 (Codex sandbox corrections, 18 Oct 2026): the Python readiness contract reports live dispatch as configured /
unverified and never verified. It leaves ready/gate/reasons unchanged, so Real and Paper behaviour is preserved."""
import unittest

import brain

CAND = {'sellInstrumentKey': 'NSE_FO|1', 'buyInstrumentKey': 'NSE_FO|2', 'type': 'BEAR_CALL'}


class G4ReadinessCapability(unittest.TestCase):
    def run_ctx(self, **ctx):
        return brain.check_execution_readiness(dict(CAND), {}, ctx)

    def test_token_plus_https_proxy_is_configured_not_verified(self):
        r = self.run_ctx(executionMode='live', authToken='t', orderProxyUrl='https://proxy.example')
        self.assertIs(r['liveDispatchVerified'], False)
        self.assertEqual(r['liveDispatchCapability'], 'UNVERIFIED')
        self.assertIs(r['checks']['proxyConfigured'], True)
        self.assertIs(r['checks']['proxyReady'], True)          # compatible field kept
        self.assertNotIn('proxy not configured', r['reasons'])

    def test_absent_configuration(self):
        r = self.run_ctx(executionMode='live')
        self.assertIs(r['liveDispatchVerified'], False)
        self.assertEqual(r['liveDispatchCapability'], 'NOT_CONFIGURED')
        self.assertIn('proxy not configured', r['reasons'])
        self.assertIs(r['ready'], False)

    def test_ready_gate_and_reasons_unchanged_for_every_mode(self):
        # Real entry authority reads ready/gate; G4 adds fields only.
        for ctx in ({'executionMode': 'paper'}, {'executionMode': 'sandbox', 'authToken': 't', 'sandboxEnabled': True},
                    {'executionMode': 'live', 'authToken': 't', 'orderProxyUrl': 'https://p'}, {'executionMode': 'live'}):
            r = self.run_ctx(**ctx)
            base = {k: v for k, v in r.items() if k not in ('liveDispatchVerified', 'liveDispatchCapability')}
            self.assertNotIn('live dispatch unverified', ' '.join(r['reasons']))
            self.assertEqual(r['gate'], 'READY' if r['ready'] else 'WAIT')
            self.assertEqual(base['ready'], len(r['reasons']) == 0)

    def test_paper_availability_unaffected(self):
        r = self.run_ctx(executionMode='paper', orderProxyUrl='https://p', authToken='t')
        self.assertEqual(r['mode'], 'paper')
        self.assertIs(r['ready'], True)
        self.assertEqual(r['gate'], 'READY')


if __name__ == '__main__':
    unittest.main()
