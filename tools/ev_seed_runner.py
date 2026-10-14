"""Implied-EV v1.2 one-time calibration SEED runner (manual, service-role, server-side only).

Reuses the reviewed ev_calibration_builder.seed_plan() and the service-role-only RPC public.ev_builder_seed.
Never run by the phone/PWA. Credentials come ONLY from the environment (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
they are never printed, written, or passed on a command line.

  python3 tools/ev_seed_runner.py                      # prepare-only (no network, no write): sanitized summary
  python3 tools/ev_seed_runner.py --confirm-seed --receipt seed_receipt.json   # the one explicit production seed

Flow with --confirm-seed: config check -> read builder state (reconcile, never overwrite) -> build ONE body with
available_at = now -> POST the identical bytes (bounded retries while fresh) -> verify ACK (SEEDED / DUPLICATE) ->
read back builder state + the matching publication + phone-side validator -> sanitized receipt.
Exit codes: 0 ok / already consistently seeded, 2 not confirmed (prepare only), 3 missing config,
4 existing state incompatible (no write), 5 request rejected, 6 ambiguous / stale (reconcile before retrying),
7 readback verification failed.
"""
import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "src", "main", "python"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ev_calibration_builder as BLD  # noqa: E402
import ev_v12_calendar as CAL  # noqa: E402
import ev_v12_calibration as CB  # noqa: E402

FRESH_LIMIT = timedelta(minutes=12)   # stop retrying well inside the server's 15-minute available_at window
MAX_ATTEMPTS = 4
RECEIPT_SCHEMA = "ev_seed_receipt_v1"


class SeedError(Exception):
    def __init__(self, code, reason, detail=None):
        super().__init__(reason)
        self.code, self.reason, self.detail = code, reason, detail


class Transport:
    """Minimal PostgREST client. The key lives only in the headers of the in-memory request object."""

    def __init__(self, url, key, timeout=60, prefix="/rest/v1"):
        self._url, self._key, self._timeout = url.rstrip("/") + prefix, key, timeout

    def _req(self, method, path, data=None, params=None):
        q = ("?" + urllib.parse.urlencode(params, safe="(),.:*")) if params else ""
        h = {"apikey": self._key, "Authorization": "Bearer " + self._key, "Accept": "application/json"}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url + "/" + path + q, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def rpc(self, fn, body_bytes):
        return self._req("POST", "rpc/" + fn, data=body_bytes)

    def get(self, path, params):
        return self._req("GET", path, params=params)


def _now():
    return datetime.now(timezone.utc).astimezone(CAL.IST)


def _err_code(body):
    try:
        d = json.loads(body)
        return str(d.get("code")), str(d.get("message"))[:120]
    except (ValueError, AttributeError):
        return None, None


def config(env=None):
    env = os.environ if env is None else env
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY") if not env.get(k)]
    if missing:
        raise SeedError(3, "MISSING_CONFIG", {"missing": missing})
    return env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"]


def read_state(tr):
    st, body = tr.rpc("ev_builder_state", b"{}")
    if st == 200:
        return json.loads(body)
    code, msg = _err_code(body)
    if msg and "EV_BUILDER_NOT_SEEDED" in msg:
        return None
    raise SeedError(5, "STATE_READ_REJECTED", {"http": st, "code": code, "message": msg})


def read_publication(tr, version):
    st, body = tr.get("ev_calibration_published", {"select": "calibration_version,payload,payload_sha256,"
                                                   "content_sha256,available_at,latest_processed_expiry",
                                                   "calibration_version": "eq." + version})
    if st != 200:
        code, msg = _err_code(body)
        raise SeedError(7, "PUBLICATION_READ_FAILED", {"http": st, "code": code})
    rows = json.loads(body)
    return rows[0] if rows else None


def reconcile_existing(tr, state, plan):
    """Existing state: consistent only if it is THIS reviewed seed (family/R/source and the seed publication with
    the same version + content identity). Never reset or overwrite."""
    pub = plan["p_publication"]
    same = (state.get("calibration_family") == plan["p_family"] and state.get("R_source") == plan["p_r_source"]
            and str(state.get("R")) == plan["p_r_value"])
    row = read_publication(tr, pub["calibration_version"]) if same else None
    if same and row and row.get("content_sha256") == pub["content_sha256"]:
        return {"status": "ALREADY_SEEDED_CONSISTENT", "calibration_version": pub["calibration_version"],
                "content_sha256": pub["content_sha256"], "watermark_expiry": state.get("watermark_expiry")}
    raise SeedError(4, "EXISTING_STATE_INCOMPATIBLE", {
        "server_family": state.get("calibration_family"), "server_R_source": state.get("R_source"),
        "server_watermark": state.get("watermark_expiry"), "expected_version": pub["calibration_version"]})


def prepare(now=None):
    """ONE complete seed body (full reviewed observation history, frozen R/source/family, watermark, hashes,
    retrospective_seed=true) with available_at = the actual run time. Serialized once; retries reuse the bytes."""
    now = now or _now()
    plan = BLD.seed_plan(available_at=now.isoformat())
    pub = plan["p_publication"]
    assert pub["retrospective_seed"] is True and plan["p_watermark_expiry"] == pub["latest_processed_expiry"]
    return plan, json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"), now


def summary(plan):
    pub = plan["p_publication"]
    return {"calibration_version": pub["calibration_version"], "content_sha256": pub["content_sha256"],
            "payload_sha256": pub["payload_sha256"], "available_at": pub["available_at"],
            "watermark_expiry": plan["p_watermark_expiry"], "family": plan["p_family"], "R_source": plan["p_r_source"],
            "n_observations": len(plan["p_observations"]),
            "status_counts": dict(sorted(collections.Counter(o["status"] for o in plan["p_observations"]).items()))}


def send(tr, body, prepared_at, clock=_now, sleep=time.sleep, attempts=MAX_ATTEMPTS):
    """POST the identical bytes; bounded retries on transport errors / 5xx while the body is fresh."""
    last = None
    for i in range(attempts):
        if clock() - prepared_at > FRESH_LIMIT:
            raise SeedError(6, "STALE_RECONCILE_REQUIRED", {"attempts": i, "last": last})
        try:
            st, resp = tr.rpc("ev_builder_seed", body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = {"transport_error": type(e).__name__}
            sleep(min(30, 2 ** i))
            continue
        if st >= 500:
            last = {"http": st}
            sleep(min(30, 2 ** i))
            continue
        if st != 200:
            code, msg = _err_code(resp)
            raise SeedError(5, "SEED_REJECTED", {"http": st, "code": code, "message": msg, "attempt": i + 1})
        return json.loads(resp), i + 1
    raise SeedError(6, "AMBIGUOUS_DELIVERY_RECONCILE_REQUIRED", {"attempts": attempts, "last": last})


def verify(tr, plan, ack, clock=_now):
    pub = plan["p_publication"]
    if ack.get("status") not in ("SEEDED", "DUPLICATE") or ack.get("calibration_version") != pub["calibration_version"]:
        raise SeedError(7, "ACK_MISMATCH", {"ack_status": ack.get("status")})
    if ack.get("status") == "SEEDED" and int(ack.get("n_observations", -1)) != len(plan["p_observations"]):
        raise SeedError(7, "ACK_COUNT_MISMATCH", {"ack_n": ack.get("n_observations")})
    st = read_state(tr)
    if st is None:
        raise SeedError(7, "STATE_MISSING_AFTER_ACK")
    want = collections.Counter(o["status"] for o in plan["p_observations"])
    got = collections.Counter((o or {}).get("status") for o in st.get("observations") or [])
    checks = {"watermark": st.get("watermark_expiry") == plan["p_watermark_expiry"],
              "R": str(st.get("R")) == plan["p_r_value"], "R_source": st.get("R_source") == plan["p_r_source"],
              "family": st.get("calibration_family") == plan["p_family"],
              "observations": len(st.get("observations") or []) == len(plan["p_observations"]) and got == want}
    row = read_publication(tr, pub["calibration_version"])
    checks["publication_present"] = row is not None
    if row:
        checks["content_identity"] = row["content_sha256"] == pub["content_sha256"] and \
            row["payload_sha256"] == pub["payload_sha256"] and row["payload"].get("content_sha256") == pub["content_sha256"]
        if ack.get("status") == "SEEDED":
            checks["available_at"] = row["payload"].get("available_at") == pub["available_at"]
        avail = datetime.fromisoformat(row["payload"]["available_at"])
        _ks, vstat = CB.validate_publication(row["payload"], now_dt=max(clock(), avail + timedelta(seconds=1)),
                                             cal=CAL.DEFAULT_CALENDAR)
        checks["phone_validator"] = vstat == "OK"
    bad = sorted(k for k, v in checks.items() if not v)
    if bad:
        raise SeedError(7, "READBACK_MISMATCH", {"failed": bad})
    return checks, row


def run(argv=None, env=None, transport=None, clock=_now, sleep=time.sleep, out=sys.stdout):
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-seed", action="store_true", help="perform the one explicit production seed")
    ap.add_argument("--receipt", help="write the sanitized receipt JSON here")
    a = ap.parse_args(argv)
    receipt = {"schema": RECEIPT_SCHEMA, "run_sha": (env or os.environ).get("GITHUB_SHA"),
               "started_at": clock().isoformat()}
    code = 0
    try:
        if not a.confirm_seed:
            plan, body, _ = prepare(clock())
            receipt.update(status="PREPARED_NOT_CONFIRMED", plan=summary(plan), body_bytes=len(body))
            code = 2
        else:
            url, key = config(env)
            # SUPABASE_REST_PREFIX: test-only override for a bare scratch PostgREST (production: /rest/v1)
            tr = transport or Transport(url, key, prefix=(env or os.environ).get("SUPABASE_REST_PREFIX", "/rest/v1"))
            plan0, _b, _t = prepare(clock())
            existing = read_state(tr)
            if existing is not None:
                receipt.update(reconcile_existing(tr, existing, plan0))
            else:
                plan, body, prepared_at = prepare(clock())   # availability = immediately before the request
                ack, attempts = send(tr, body, prepared_at, clock=clock, sleep=sleep)
                checks, row = verify(tr, plan, ack, clock=clock)
                receipt.update(status=ack["status"], attempts=attempts, plan=summary(plan), checks=checks,
                               server_available_at=row["payload"]["available_at"])
    except SeedError as e:
        receipt.update(status="FAILED", reason=e.reason, detail=e.detail)
        code = e.code
    receipt["finished_at"] = clock().isoformat()
    txt = json.dumps(receipt, indent=1, sort_keys=True, default=str)
    if a.receipt:
        with open(a.receipt, "w") as f:
            f.write(txt + "\n")
    print(txt, file=out)
    return code


if __name__ == "__main__":
    raise SystemExit(run())
