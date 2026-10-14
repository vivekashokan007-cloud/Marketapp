"""Unit tests for tools/ev_seed_runner.py with a fake transport (no network, no database, no credentials).
The real scratch PostgREST round trip lives in tools/ev_db_suite/seed_runner_suite.py."""
import io
import json
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "tools"))
import ev_seed_runner as SR  # noqa: E402

KEY = "service-role-secret-value-never-printed"
CFG = {"SUPABASE_URL": "https://example.invalid", "SUPABASE_SERVICE_ROLE_KEY": KEY, "GITHUB_SHA": "b" * 40}


class Fake:
    """Emulates ev_builder_state / ev_builder_seed / the publication read with server semantics."""

    def __init__(self, lose_first=False, reject=None, transport_errors=0):
        self.state, self.pub, self.calls, self.bodies = None, None, [], []
        self.lose_first, self.reject, self.transport_errors = lose_first, reject, transport_errors

    def rpc(self, fn, body):
        self.calls.append(fn)
        if fn == "ev_builder_state":
            if self.state is None:
                return 400, json.dumps({"code": "P0002", "message": "EV_BUILDER_NOT_SEEDED"})
            return 200, json.dumps(self.state)
        assert fn == "ev_builder_seed"
        self.bodies.append(body)
        if self.transport_errors:
            self.transport_errors -= 1
            raise ConnectionError("down")
        if self.reject:
            return 400, json.dumps({"code": "23514", "message": self.reject})
        b = json.loads(body)
        p = b["p_publication"]
        if self.state is not None:
            if self.pub and self.pub["content_sha256"] == p["content_sha256"]:
                return 200, json.dumps({"status": "DUPLICATE", "calibration_version": p["calibration_version"]})
            return 400, json.dumps({"code": "23514", "message": "EV_BUILDER_ALREADY_SEEDED"})
        self.state = {"watermark_expiry": b["p_watermark_expiry"], "R": b["p_r_value"], "R_source": b["p_r_source"],
                      "calibration_family": b["p_family"], "checkpoint": None, "observations": b["p_observations"]}
        self.pub = {"calibration_version": p["calibration_version"], "payload": p, "payload_sha256": p["payload_sha256"],
                    "content_sha256": p["content_sha256"], "available_at": p["available_at"]}
        if self.lose_first:
            self.lose_first = False
            raise TimeoutError("response lost after commit")
        return 200, json.dumps({"status": "SEEDED", "n_observations": len(b["p_observations"]),
                                "calibration_version": p["calibration_version"]})

    def get(self, path, params):
        self.calls.append("GET " + path)
        ok = self.pub and params["calibration_version"] == "eq." + self.pub["calibration_version"]
        return 200, json.dumps([self.pub] if ok else [])


def go(argv, fake=None, env=CFG, clock=SR._now):
    out = io.StringIO()
    code = SR.run(argv, env=env, transport=fake, clock=clock, sleep=lambda s: None, out=out)
    return code, json.loads(out.getvalue()), out.getvalue()


class SeedRunner(unittest.TestCase):
    def test_missing_config_fails_before_any_request(self):
        f = Fake()
        for env in ({}, {"SUPABASE_URL": "x"}, {"SUPABASE_SERVICE_ROLE_KEY": KEY}):
            c, r, _ = go(["--confirm-seed"], f, env=env)
            self.assertEqual((c, r["reason"]), (3, "MISSING_CONFIG"))
        self.assertEqual(f.calls, [])

    def test_default_is_prepare_only(self):
        f = Fake()
        c, r, _ = go([], f)
        self.assertEqual((c, r["status"]), (2, "PREPARED_NOT_CONFIRMED"))
        self.assertEqual(f.calls, [])
        self.assertEqual(r["plan"]["n_observations"], 47)

    def test_valid_seed_ack_and_readback(self):
        f = Fake()
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"]), (0, "SEEDED"))
        self.assertTrue(all(r["checks"].values()), r["checks"])
        self.assertEqual(f.bodies.__len__(), 1)
        b = json.loads(f.bodies[0])
        self.assertIs(b["p_publication"]["retrospective_seed"], True)
        self.assertEqual(b["p_watermark_expiry"], b["p_publication"]["latest_processed_expiry"])
        self.assertEqual(len(b["p_observations"]), 47)                    # complete history, not just the publication
        self.assertNotIn(KEY, txt)

    def test_rejected_request(self):
        c, r, _ = go(["--confirm-seed"], Fake(reject="EV_SEED_INCONSISTENT"))
        self.assertEqual((c, r["reason"], r["detail"]["message"]), (5, "SEED_REJECTED", "EV_SEED_INCONSISTENT"))

    def test_lost_response_retry_identical_bytes_duplicate(self):
        f = Fake(lose_first=True)
        c, r, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"], r["attempts"]), (0, "DUPLICATE", 2))
        self.assertEqual(f.bodies[0], f.bodies[1])
        self.assertTrue(all(r["checks"].values()))

    def test_transport_errors_bounded_then_ambiguous(self):
        f = Fake(transport_errors=99)
        c, r, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r["reason"]), (6, "AMBIGUOUS_DELIVERY_RECONCILE_REQUIRED"))
        self.assertEqual(len(f.bodies), SR.MAX_ATTEMPTS)
        self.assertEqual(len(set(f.bodies)), 1)

    def test_existing_consistent_state_is_reconciled_not_rewritten(self):
        f = Fake()
        go(["--confirm-seed"], f)
        n = len(f.bodies)
        c, r, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"]), (0, "ALREADY_SEEDED_CONSISTENT"))
        self.assertEqual(len(f.bodies), n)

    def test_incompatible_state_refused(self):
        f = Fake()
        go(["--confirm-seed"], f)
        f.state["calibration_family"] = "other"
        n = len(f.bodies)
        c, r, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r["reason"]), (4, "EXISTING_STATE_INCOMPATIBLE"))
        self.assertEqual(len(f.bodies), n)

    def test_stale_body_not_sent(self):
        t0 = SR._now()
        ticks = iter([t0] * 3 + [t0 + timedelta(minutes=13)] * 20)
        f = Fake()
        c, r, _ = go(["--confirm-seed"], f, clock=lambda: next(ticks))
        self.assertEqual((c, r["reason"]), (6, "STALE_RECONCILE_REQUIRED"))
        self.assertEqual(f.bodies, [])

    def test_receipt_is_sanitized_and_bounded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.json")
            out = io.StringIO()
            SR.run(["--confirm-seed", "--receipt", p], env=CFG, transport=Fake(), sleep=lambda s: None, out=out)
            rec = open(p).read()
        self.assertNotIn(KEY, rec)
        self.assertNotIn("Bearer", rec)
        self.assertNotIn("p_observations", rec)            # summary only, never the full request
        self.assertLess(len(rec), 4096)
        self.assertEqual(json.loads(rec)["run_sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()


class SeedWorkflow(unittest.TestCase):
    """Text-level guard on .github/workflows/ev-calibration-seed.yml (no YAML dependency in CI)."""
    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..")

    def setUp(self):
        self.w = open(os.path.join(self.ROOT, ".github", "workflows", "ev-calibration-seed.yml")).read()
        self.code = "\n".join(l for l in self.w.splitlines() if not l.lstrip().startswith("#"))

    def test_manual_trusted_only(self):
        on = self.code.split("\non:")[1].split("\npermissions:")[0]
        self.assertIn("workflow_dispatch:", on)
        for t in ("push:", "pull_request", "schedule:", "cron", "ref:", "repository:"):
            self.assertNotIn(t, on)
        self.assertIn("default: false", on)
        self.assertIn("contents: read", self.code)
        self.assertIn("group: ev-calibration-builder", self.code)
        self.assertIn("cancel-in-progress: false", self.code)
        self.assertNotIn("contents: write", self.code)
        self.assertEqual(self.code.count("timeout-minutes:"), 2)
        self.assertIn("inputs.confirm_seed == true && vars.EV_BUILDER_ENABLED == 'true' && "
                      "github.ref == 'refs/heads/main'", self.code)

    def test_secrets_only_in_seed_step(self):
        prepare, seed = self.code.split("\n  seed:")
        self.assertNotIn("secrets.", prepare)
        steps = seed.split("\n      - ")
        with_secrets = [s for s in steps if "secrets." in s]
        self.assertEqual(len(with_secrets), 1)
        self.assertIn("--confirm-seed", with_secrets[0])
        for bad in ("echo", "set -x", "curl", "env |", "printenv"):
            self.assertNotIn(bad, with_secrets[0])
