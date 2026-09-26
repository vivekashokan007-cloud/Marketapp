"""§6 (2026-09-26): the known polluting test must redirect the parity store.

Runs the previously polluting module in-process and asserts the default
fixture path is not created and the default path is restored afterwards.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import advice_parity_instrumentation as api  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "batch_b_parity", "parity_observations.jsonl")


class TestNoParityFixturePollution(unittest.TestCase):
    def test_first_poll_module_does_not_write_default_store(self):
        existed = os.path.exists(FIXTURE)
        before = api._DEFAULT_STORE_PATH
        import test_b3_item2_first_poll_monitoring_20260926 as mod
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        # TextTestRunner runs the module's setUpModule/tearDownModule itself.
        with open(os.devnull, "w") as sink:
            result = unittest.TextTestRunner(stream=sink, verbosity=0).run(suite)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(api._DEFAULT_STORE_PATH, before)
        if not existed:
            self.assertFalse(os.path.exists(FIXTURE), "parity_observations.jsonl written into the checkout")


if __name__ == "__main__":
    unittest.main()
