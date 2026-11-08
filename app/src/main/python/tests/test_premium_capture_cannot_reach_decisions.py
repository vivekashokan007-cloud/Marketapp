"""No decision path can read premium-capture output.

This is the Python half of the previously-admitted gap: "Control OFF -> unchanged
trading result". The Kotlin half shows the poll's chain inputs are byte-identical
after capture runs (captureOnLeavesThePollsBrainInputsByteIdenticalToCaptureOff).
This half shows the decision engine cannot see capture output at all, so there is
no path by which capture could change a verdict even if it wrote something odd.

Together they bound the claim WITHOUT running Chaquopy, which I had wrongly
treated as the only possible route. What neither establishes is device behaviour;
that remains an explicit phone smoke check.

The guard is deliberately a reachability argument rather than a diff of two brain
runs. A diff of two runs with capture off vs on would be vacuous here: the brain
is never given a capture input to vary, which is exactly the property worth
pinning. If capture output is ever threaded into the engine, this test fails and
the claim in the handoff has to be withdrawn rather than quietly weakened.
"""

import os
import re
import unittest

PY_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# The capture surface, by the names it would have to be referenced by.
CAPTURE_TOKENS = (
    "premium_capture",
    "PremiumCapture",
    "PremiumObservationContract",
    "premium_observation",
    "premium_slot_envelope",
    "lot_authority.json",
    "last_run.json",
)

# Files that legitimately TALK ABOUT capture without consuming it.
ALLOWED_FILES = {
    "test_premium_capture_cannot_reach_decisions.py",
}


def _engine_files():
    out = []
    for base, _dirs, files in os.walk(PY_DIR):
        # the tests tree may name capture freely; only the engine is constrained
        if os.path.basename(base) == "tests" or f"{os.sep}tests{os.sep}" in base + os.sep:
            continue
        for n in sorted(files):
            if n.endswith(".py"):
                out.append(os.path.join(base, n))
    return out


class PremiumCaptureCannotReachDecisions(unittest.TestCase):
    def test_the_engine_tree_never_references_capture(self):
        engine = _engine_files()
        self.assertTrue(engine, "the engine tree must be found for this to mean anything")
        self.assertIn(
            os.path.join(PY_DIR, "brain.py"), engine,
            "brain.py must be among the files checked",
        )
        offenders = {}
        for path in engine:
            if os.path.basename(path) in ALLOWED_FILES:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            hits = sorted({t for t in CAPTURE_TOKENS if t in src})
            if hits:
                offenders[os.path.relpath(path, PY_DIR)] = hits
        self.assertEqual(
            {}, offenders,
            "no decision-engine module may reference premium capture; "
            "if capture output is ever threaded into the engine, the "
            "'capture cannot change a trading result' claim must be withdrawn, "
            "not this test relaxed",
        )

    def test_brain_analyze_takes_no_capture_input(self):
        """brain.analyze's signature must not grow a capture argument."""
        with open(os.path.join(PY_DIR, "brain.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"\ndef analyze\(([^)]*)\)", src, re.S)
        self.assertIsNotNone(m, "brain.analyze must be found")
        params = m.group(1)
        for token in CAPTURE_TOKENS:
            self.assertNotIn(
                token.lower(), params.lower(),
                "brain.analyze must take no premium-capture input",
            )

    def test_the_capture_directory_name_is_not_read_anywhere_in_the_engine(self):
        """The on-disk directory capture writes to must not be opened by the engine."""
        for path in _engine_files():
            if os.path.basename(path) in ALLOWED_FILES:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            self.assertNotIn(
                '"premium_capture"', src,
                f"{os.path.relpath(path, PY_DIR)} must not open the capture directory",
            )


if __name__ == "__main__":
    unittest.main()
