"""Unit tests for tools/ev_seed_runner.py with a fake transport (no network, no database, no credentials).
The real scratch PostgREST round trip lives in tools/ev_db_suite/seed_runner_suite.py.
Codex R1 probes (14 Oct 2026) are encoded as tests here: reconciliation must verify the complete unadvanced seed,
transport/parse/shape faults must keep the receipt/exit contract, and readback binds every observation."""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "tools"))
import ev_seed_runner as SR  # noqa: E402
import ev_v12_calibration as CB  # noqa: E402

KEY = "service-role-secret-value-never-printed"
CFG = {"SUPABASE_URL": "https://example.invalid", "SUPABASE_SERVICE_ROLE_KEY": KEY, "GITHUB_SHA": "b" * 40}
LEAK = "secret-exception-text"


def _utc(ts):
    """PostgREST renders timestamptz in UTC: same instant, different text."""
    from datetime import datetime
    return datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat()


class Fake:
    """Emulates ev_builder_state / ev_builder_seed / the publication read with server semantics.
    faults: {"state"|"seed"|"pub": [None | Exception | (status, body) | "commit_then_<x>", ...]} consumed per call."""

    def __init__(self, faults=None):
        self.state, self.row, self.calls, self.bodies = None, None, [], []
        self.faults = {k: list(v) for k, v in (faults or {}).items()}

    def _fault(self, kind):
        q = self.faults.get(kind)
        return q.pop(0) if q else None

    def rpc(self, fn, body):
        self.calls.append(fn)
        if fn == "ev_builder_state":
            f = self._fault("state")
            if isinstance(f, Exception):
                raise f
            if isinstance(f, tuple):
                return f
            if self.state is None:
                return 400, json.dumps({"code": "P0002", "message": "EV_BUILDER_NOT_SEEDED"})
            return 200, json.dumps(self.state)
        assert fn == "ev_builder_seed"
        self.bodies.append(body)
        f = self._fault("seed")
        if isinstance(f, Exception):
            raise f
        if isinstance(f, tuple):
            return f
        b = json.loads(body)
        p = b["p_publication"]
        if self.state is not None:
            if self.row and self.row["content_sha256"] == p["content_sha256"]:
                ack = (200, json.dumps({"status": "DUPLICATE", "calibration_version": p["calibration_version"]}))
            else:
                return 400, json.dumps({"code": "23514", "message": "EV_BUILDER_ALREADY_SEEDED"})
        else:
            self.state = {"watermark_expiry": b["p_watermark_expiry"], "R": b["p_r_value"], "R_source": b["p_r_source"],
                          "calibration_family": b["p_family"], "checkpoint": None, "observations": b["p_observations"]}
            self.row = {"calibration_version": p["calibration_version"], "payload": p,
                        "payload_sha256": p["payload_sha256"], "content_sha256": p["content_sha256"],
                        "available_at": _utc(p["available_at"]), "latest_processed_expiry": p["latest_processed_expiry"]}
            ack = (200, json.dumps({"status": "SEEDED", "n_observations": len(b["p_observations"]),
                                    "calibration_version": p["calibration_version"]}))
        if f == "commit_then_lost":
            raise TimeoutError(LEAK)
        if f == "commit_then_malformed":
            return 200, "{not json" + LEAK
        if f == "commit_then_list":
            return 200, "[]"
        return ack

    def get(self, path, params):
        self.calls.append("GET " + path)
        f = self._fault("pub")
        if isinstance(f, Exception):
            raise f
        if isinstance(f, tuple):
            return f
        ok = self.row and params["calibration_version"] == "eq." + self.row["calibration_version"]
        return 200, json.dumps([self.row] if ok else [])

    def writes(self):
        return self.calls.count("ev_builder_seed")


def go(argv, fake=None, env=CFG, clock=SR._now):
    out = io.StringIO()
    code = SR.run(argv, env=env, transport=fake, clock=clock, sleep=lambda s: None, out=out)
    txt = out.getvalue()
    return code, json.loads(txt), txt


def seeded():
    f = Fake()
    c, r, _ = go(["--confirm-seed"], f)
    assert (c, r["status"]) == (0, "SEEDED"), r
    return f, r


def repub(row, **changes):
    """Rewrite the stored payload consistently (recomputed payload digest) so only the named field differs."""
    pl = dict(row["payload"], **changes)
    pl["payload_sha256"] = CB.sha256_text(CB.canon({k: v for k, v in pl.items() if k != "payload_sha256"}))
    row.update(payload=pl, payload_sha256=pl["payload_sha256"])
    if "available_at" in changes:
        row["available_at"] = _utc(changes["available_at"])


class SeedRunner(unittest.TestCase):
    def assertSanitized(self, txt):
        for bad in (KEY, "Bearer", "apikey", LEAK, "p_observations"):
            self.assertNotIn(bad, txt)

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
        f, r = seeded()
        self.assertTrue(all(r["checks"].values()), r["checks"])
        for k in ("observation_digests_recomputed", "observation_set_exact", "payload_digest_recomputed",
                  "row_payload_available_at_agree", "checkpoint_null", "available_at_is_this_run", "phone_validator"):
            self.assertIn(k, r["checks"])
        self.assertEqual(len(f.bodies), 1)
        b = json.loads(f.bodies[0])
        self.assertIs(b["p_publication"]["retrospective_seed"], True)
        self.assertEqual(b["p_watermark_expiry"], b["p_publication"]["latest_processed_expiry"])
        self.assertEqual(len(b["p_observations"]), 47)
        self.assertEqual(r["persisted"]["phone_validator_status"], "OK")
        self.assertEqual(r["persisted"]["observation_set_sha256"], r["plan"]["observation_set_sha256"])

    def test_rejected_request(self):
        c, r, _ = go(["--confirm-seed"], Fake({"seed": [(400, json.dumps({"code": "23514",
                                                                          "message": "EV_SEED_INCONSISTENT"}))]}))
        self.assertEqual((c, r["reason"], r["detail"]["message"]), (5, "SEED_REJECTED", "EV_SEED_INCONSISTENT"))

    def test_unsafe_server_message_is_redacted(self):
        c, r, txt = go(["--confirm-seed"], Fake({"seed": [(400, json.dumps({"code": "x", "message": KEY + "\n<html>"}))]}))
        self.assertEqual(r["detail"]["message"], "<redacted>")
        self.assertSanitized(txt)

    # ---- R1 evidence 4: lost response / ambiguous run ----
    def test_lost_response_retry_identical_bytes_duplicate(self):
        f = Fake({"seed": ["commit_then_lost"]})
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"], r["attempts"]), (0, "DUPLICATE", 2))
        self.assertEqual(f.bodies[0], f.bodies[1])
        self.assertTrue(all(r["checks"].values()))
        self.assertNotIn("available_at_is_this_run", r["checks"])      # DUPLICATE: persisted availability is authoritative
        self.assertSanitized(txt)

    def test_transport_errors_bounded_then_ambiguous(self):
        f = Fake({"seed": [TimeoutError(LEAK)] * 99})
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["reason"], r["write_may_exist"]), (6, "AMBIGUOUS_DELIVERY_RECONCILE_REQUIRED", True))
        self.assertEqual(len(f.bodies), SR.MAX_ATTEMPTS)
        self.assertEqual(len(set(f.bodies)), 1)
        self.assertSanitized(txt)

    def test_ambiguous_run_then_reconcile_keeps_original_availability_and_digest(self):
        f = Fake({"seed": ["commit_then_lost"] + [TimeoutError()] * 9})
        c, r1, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r1["write_may_exist"]), (6, True))
        orig = copy.deepcopy(f.row)
        n = f.writes()
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"]), (0, "ALREADY_SEEDED_CONSISTENT"))
        self.assertEqual(f.writes(), n)                                 # reconciliation makes no write
        self.assertEqual(r["server_available_at"], orig["payload"]["available_at"])
        self.assertEqual(r["persisted"]["payload_sha256"], orig["payload_sha256"])
        self.assertEqual(r["persisted"]["available_at"], r1["plan"]["available_at"])
        self.assertNotEqual(r["persisted"]["available_at"], r["plan"]["available_at"])
        self.assertTrue(all(r["checks"].values()))
        self.assertEqual(f.row, orig)
        self.assertSanitized(txt)

    def test_existing_consistent_state_is_reconciled_not_rewritten(self):
        f, _ = seeded()
        n = len(f.bodies)
        c, r, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"]), (0, "ALREADY_SEEDED_CONSISTENT"))
        self.assertEqual(len(f.bodies), n)
        self.assertEqual(r["persisted"]["n_observations"], 47)

    # ---- Finding 1 / R1 evidence 2: reconciliation probes (each a single change before the rerun) ----
    def _reconcile_probe(self, mutate, reason="EXISTING_STATE_INCOMPATIBLE", failed=None):
        f, _ = seeded()
        mutate(f)
        snap, n = copy.deepcopy((f.state, f.row)), f.writes()
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"], r["reason"]), (4, "FAILED", reason), r)
        self.assertEqual(f.writes(), n)
        self.assertEqual((f.state, f.row), snap)                       # never updated / reset / reseeded
        if failed:
            self.assertIn(failed, r["detail"]["failed"])
        self.assertSanitized(txt)
        return r

    def test_probe_watermark_regressed(self):
        self._reconcile_probe(lambda f: f.state.update(watermark_expiry="2026-10-06"), failed="watermark")

    def test_probe_observations_empty(self):
        self._reconcile_probe(lambda f: f.state.update(observations=[]), failed="observation_set_exact")

    def test_probe_publication_payload_empty(self):
        self._reconcile_probe(lambda f: f.row.update(payload={}), failed="payload_digest_recomputed")

    def test_probe_invalid_publication_digest(self):
        def m(f):
            f.row["payload"] = dict(f.row["payload"], payload_sha256="0" * 64)
            f.row["payload_sha256"] = "0" * 64
        self._reconcile_probe(m, failed="payload_digest_recomputed")

    def test_probe_altered_observation_r_keeps_digest(self):
        def m(f):
            o = next(o for o in f.state["observations"] if o["status"] == "OK")
            o["r"] = o["r"] * 1.01
        self._reconcile_probe(m, failed="observation_digests_recomputed")

    def test_probe_altered_observation_r_and_digest(self):
        def m(f):
            o = next(o for o in f.state["observations"] if o["status"] == "OK")
            o["r"] = o["r"] * 1.01
            o["observation_sha256"] = CB.observation_identity(o)
        self._reconcile_probe(m, failed="observation_set_exact")

    def test_probe_altered_observation_digest_only(self):
        self._reconcile_probe(lambda f: f.state["observations"][0].update(observation_sha256="f" * 64),
                              failed="observation_digests_recomputed")

    def test_probe_duplicate_observation(self):
        def m(f):
            obs = f.state["observations"]
            same = [o for o in obs if o["status"] == obs[0]["status"]]
            obs[obs.index(same[1])] = copy.deepcopy(same[0])        # count + status distribution unchanged
        self._reconcile_probe(m, failed="observations_no_duplicates")

    def test_probe_missing_observation(self):
        self._reconcile_probe(lambda f: f.state["observations"].pop(), failed="observation_set_exact")

    def test_probe_row_payload_availability_disagree(self):
        def m(f):
            f.row["available_at"] = _utc((SR._now() - timedelta(minutes=3)).isoformat())
        self._reconcile_probe(m, failed="row_payload_available_at_agree")

    def test_probe_future_publication_availability(self):
        self._reconcile_probe(lambda f: repub(f.row, available_at=(SR._now() + timedelta(hours=2)).isoformat()),
                              failed="phone_validator")

    def test_probe_row_latest_processed_expiry_disagree(self):
        self._reconcile_probe(lambda f: f.row.update(latest_processed_expiry="2026-10-06"),
                              failed="row_latest_processed_expiry")

    def test_probe_advanced_watermark_is_named_non_success(self):
        self._reconcile_probe(lambda f: f.state.update(watermark_expiry="2026-10-20"), reason="STATE_ADVANCED_INVESTIGATE")

    def test_probe_checkpoint_present_is_named_non_success(self):
        self._reconcile_probe(lambda f: f.state.update(checkpoint={"through_expiry": "2026-08-11"}),
                              reason="STATE_ADVANCED_INVESTIGATE")

    def test_incompatible_family_refused(self):
        self._reconcile_probe(lambda f: f.state.update(calibration_family="other"), failed="family")

    # ---- Finding 3: fresh-write readback binds every observation (exit 7) ----
    def _fresh_probe(self, mutate, failed):
        class Tamper(Fake):
            def rpc(s, fn, body):
                res = Fake.rpc(s, fn, body)
                if fn == "ev_builder_seed" and not getattr(s, "done", False):
                    s.done = True
                    mutate(s)
                return res
        f = Tamper()
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["reason"]), (7, "READBACK_MISMATCH"), r)
        self.assertIn(failed, r["detail"]["failed"])
        self.assertTrue(r["write_may_exist"])
        self.assertSanitized(txt)

    def test_fresh_altered_r_same_counts_fails(self):
        def m(f):
            o = next(o for o in f.state["observations"] if o["status"] == "OK")
            o["r"] += 0.001
        self._fresh_probe(m, "observation_digests_recomputed")

    def test_fresh_future_availability_uses_real_clock(self):
        self._fresh_probe(lambda f: repub(f.row, available_at=(SR._now() + timedelta(hours=2)).isoformat()),
                          "phone_validator")

    def test_fresh_available_at_not_this_run(self):
        self._fresh_probe(lambda f: repub(f.row, available_at=(SR._now() - timedelta(minutes=5)).isoformat()),
                          "available_at_is_this_run")

    # ---- Finding 2 / R1 evidence 3: transport, parse and shape faults ----
    def _fault(self, faults, code, reason, may_exist):
        f = Fake(faults)
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["status"], r["reason"], r["write_may_exist"]), (code, "FAILED", reason, may_exist), r)
        self.assertIn("calibration_version", r["plan"])                # plan identity retained in failure receipts
        self.assertSanitized(txt)
        return f, r

    def test_initial_state_read_timeout(self):
        f, r = self._fault({"state": [TimeoutError(LEAK)]}, 5, "STATE_READ_FAILED", False)
        self.assertEqual(f.writes(), 0)
        self.assertEqual(r["detail"], {"transport_error": "TimeoutError", "phase": "pre_write"})

    def test_initial_state_read_disconnect(self):
        f, _ = self._fault({"state": [ConnectionResetError(LEAK)]}, 5, "STATE_READ_FAILED", False)
        self.assertEqual(f.writes(), 0)

    def test_initial_state_malformed_json(self):
        self._fault({"state": [(200, "<html>" + LEAK)]}, 5, "STATE_READ_FAILED", False)

    def test_initial_state_wrong_shape(self):
        self._fault({"state": [(200, "[1,2]")]}, 5, "STATE_READ_FAILED", False)
        self._fault({"state": [(200, json.dumps({"observations": "x"}))]}, 5, "STATE_READ_FAILED", False)

    def test_post_ack_state_readback_timeout(self):
        self._fault({"state": [None, TimeoutError(LEAK)]}, 6, "READBACK_FAILED_WRITE_MAY_EXIST", True)

    def test_post_ack_state_readback_malformed(self):
        self._fault({"state": [None, (200, "{" + LEAK)]}, 6, "READBACK_FAILED_WRITE_MAY_EXIST", True)

    def test_post_ack_publication_readback_disconnect(self):
        f, r = self._fault({"pub": [ConnectionError(LEAK)]}, 6, "READBACK_FAILED_WRITE_MAY_EXIST", True)
        self.assertEqual(r["detail"]["read"], "publication")

    def test_post_ack_publication_wrong_shape(self):
        self._fault({"pub": [(200, json.dumps({"payload": 1}))]}, 6, "READBACK_FAILED_WRITE_MAY_EXIST", True)

    def test_post_ack_publication_http_error(self):
        self._fault({"pub": [(503, LEAK)]}, 6, "READBACK_FAILED_WRITE_MAY_EXIST", True)

    def test_malformed_ack_after_commit_is_ambiguous_then_reconciles(self):
        f, r = self._fault({"seed": ["commit_then_malformed"]}, 6, "ACK_MALFORMED_WRITE_MAY_EXIST", True)
        self.assertIsNotNone(f.state)                                  # it did commit: never inferred otherwise
        c, r2, _ = go(["--confirm-seed"], f)
        self.assertEqual((c, r2["status"]), (0, "ALREADY_SEEDED_CONSISTENT"))

    def test_wrong_shaped_ack(self):
        self._fault({"seed": ["commit_then_list"]}, 6, "ACK_MALFORMED_WRITE_MAY_EXIST", True)
        self._fault({"seed": [(200, json.dumps({"status": "SEEDED", "calibration_version": "x",
                                                "n_observations": "47"}))]}, 6, "ACK_MALFORMED_WRITE_MAY_EXIST", True)
        self._fault({"seed": [(200, json.dumps({"status": "OK"}))]}, 6, "ACK_MALFORMED_WRITE_MAY_EXIST", True)

    def test_reconcile_publication_read_failure(self):
        f, _ = seeded()
        f.faults["pub"] = [TimeoutError(LEAK)]
        c, r, txt = go(["--confirm-seed"], f)
        self.assertEqual((c, r["reason"]), (6, "RECONCILE_READ_FAILED"))
        self.assertSanitized(txt)

    def test_stale_body_not_sent(self):
        t0 = SR._now()
        ticks = iter([t0] * 3 + [t0 + timedelta(minutes=13)] * 20)
        f = Fake()
        c, r, _ = go(["--confirm-seed"], f, clock=lambda: next(ticks))
        self.assertEqual((c, r["reason"], r["write_may_exist"]), (6, "STALE_RECONCILE_REQUIRED", False))
        self.assertEqual(f.bodies, [])

    def test_receipt_is_sanitized_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.json")
            out = io.StringIO()
            SR.run(["--confirm-seed", "--receipt", p], env=CFG, transport=Fake(), sleep=lambda s: None, out=out)
            rec = open(p).read()
        self.assertSanitized(rec)
        self.assertLess(len(rec), 8192)
        self.assertEqual(json.loads(rec)["run_sha"], "b" * 40)

    def test_receipt_write_failure_is_explicit(self):
        out = io.StringIO()
        c = SR.run(["--confirm-seed", "--receipt", "/nonexistent-dir/x/r.json"], env=CFG, transport=Fake(),
                   sleep=lambda s: None, out=out)
        r = json.loads(out.getvalue())
        self.assertEqual((c, r["status"], r["reason"]), (8, "FAILED", "RECEIPT_WRITE_FAILED"))

    def test_unexpected_error_still_emits_receipt(self):
        class Boom(Fake):
            def get(s, path, params):
                raise RuntimeError(LEAK)
        c, r, txt = go(["--confirm-seed"], Boom())
        self.assertEqual((c, r["reason"], r["write_may_exist"]), (9, "INTERNAL_ERROR", True))
        self.assertSanitized(txt)


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
