"""Implied-EV v1.2 one-time calibration SEED runner (manual, service-role, server-side only).

Reuses the reviewed ev_calibration_builder.seed_plan() and the service-role-only RPC public.ev_builder_seed.
Never run by the phone/PWA. Credentials come ONLY from the environment (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
they are never printed, written, or passed on a command line.

  python3 tools/ev_seed_runner.py                      # prepare-only (no network, no write): sanitized summary
  python3 tools/ev_seed_runner.py --confirm-seed --receipt seed_receipt.json   # the one explicit production seed

Flow with --confirm-seed: config check -> read builder state (reconcile, never overwrite) -> build ONE body with
available_at = now -> POST the identical bytes (bounded retries while fresh) -> verify ACK (SEEDED / DUPLICATE) ->
read back builder state + the matching publication + phone-side validator -> sanitized receipt.
Exit codes: 0 SEEDED / DUPLICATE / ALREADY_SEEDED_CONSISTENT (all fully verified), 2 not confirmed (prepare only),
3 missing config, 4 existing state incompatible or advanced (no write; investigate), 5 rejected or pre-write read
failure (nothing written by this run), 6 ambiguous: the write MAY exist (stale body, lost/malformed response,
post-write readback failure) -> re-run to reconcile, 7 readback verification mismatch, 8 receipt could not be
written, 9 unexpected internal error (write_may_exist in the receipt says whether a POST was attempted).
"""
import argparse
import collections
import json
import os
import re
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


TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)
_SAFE_MSG = re.compile(r"^[A-Za-z0-9_ .,:()'-]{0,120}$")


class SeedError(Exception):
    def __init__(self, code, reason, detail=None):
        super().__init__(reason)
        self.code, self.reason, self.detail = code, reason, detail


class Ambiguous(SeedError):
    """The write may have committed: never infer that it did not; reconcile by re-running."""


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
    """(code, message) from a PostgREST error body, sanitized: never the raw body."""
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(d, dict):
        return None, None
    code, msg = d.get("code"), d.get("message")
    code = str(code) if isinstance(code, (str, int)) and _SAFE_MSG.match(str(code)) else None
    msg = msg if isinstance(msg, str) and _SAFE_MSG.match(msg) else ("<redacted>" if msg is not None else None)
    return code, msg


def config(env=None):
    env = os.environ if env is None else env
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY") if not env.get(k)]
    if missing:
        raise SeedError(3, "MISSING_CONFIG", {"missing": missing})
    return env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"]


def _call(fn, err):
    """Run one request; transport faults become err(type name) - never the exception text."""
    try:
        return fn()
    except TRANSPORT_ERRORS as e:
        raise err({"transport_error": type(e).__name__})


def _json(body, kind, err):
    try:
        v = json.loads(body)
    except (ValueError, TypeError):
        raise err({"malformed": kind})
    return v


def _aware(ts):
    """Aware datetime from an ISO string, else None."""
    if not isinstance(ts, str):
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


def read_state(tr, phase):
    """phase 'pre_write' (nothing written by this run) or 'post_write' (a POST was attempted)."""
    if phase == "pre_write":
        def err(d, reason="STATE_READ_FAILED"):
            return SeedError(5, reason, dict(d, phase=phase))
    else:
        def err(d, reason="READBACK_FAILED_WRITE_MAY_EXIST"):
            return Ambiguous(6, reason, dict(d, phase=phase))
    st, body = _call(lambda: tr.rpc("ev_builder_state", b"{}"), err)
    if st == 200:
        v = _json(body, "state", err)
        if not isinstance(v, dict) or not isinstance(v.get("observations"), list) \
                or not all(isinstance(o, dict) for o in v["observations"]) \
                or not (v.get("checkpoint") is None or isinstance(v.get("checkpoint"), dict)):
            raise err({"malformed": "state_shape"})
        return v
    code, msg = _err_code(body)
    if msg == "EV_BUILDER_NOT_SEEDED":
        return None
    if phase == "pre_write":
        raise SeedError(5, "STATE_READ_REJECTED", {"http": st, "code": code, "message": msg, "phase": phase})
    raise err({"http": st, "code": code})


def read_publication(tr, version, phase):
    def err(d):
        if phase == "post_write":
            return Ambiguous(6, "READBACK_FAILED_WRITE_MAY_EXIST", dict(d, phase=phase, read="publication"))
        return SeedError(6, "RECONCILE_READ_FAILED", dict(d, phase=phase, read="publication"))
    st, body = _call(lambda: tr.get("ev_calibration_published", {
        "select": "calibration_version,payload,payload_sha256,content_sha256,available_at,latest_processed_expiry",
        "calibration_version": "eq." + version}), err)
    if st != 200:
        code, _m = _err_code(body)
        raise err({"http": st, "code": code})
    rows = _json(body, "publication", err)
    if not isinstance(rows, list) or len(rows) > 1 or not all(isinstance(r, dict) for r in rows) \
            or (rows and not isinstance(rows[0].get("payload"), dict)):
        raise err({"malformed": "publication_shape"})
    return rows[0] if rows else None


def prepare(now=None):
    """ONE complete seed body (full reviewed observation history, frozen R/source/family, watermark, hashes,
    retrospective_seed=true) with available_at = the actual run time. Serialized once; retries reuse the bytes."""
    now = now or _now()
    plan = BLD.seed_plan(available_at=now.isoformat())
    pub = plan["p_publication"]
    assert pub["retrospective_seed"] is True and plan["p_watermark_expiry"] == pub["latest_processed_expiry"]
    return plan, json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"), now


def _status_counts(obs):
    return dict(sorted(collections.Counter(str(o.get("status")) for o in obs).items()))


def _obs_set_sha(idents):
    return CB.sha256_text("\n".join(sorted(idents)))


def summary(plan):
    pub = plan["p_publication"]
    return {"calibration_version": pub["calibration_version"], "content_sha256": pub["content_sha256"],
            "payload_sha256": pub["payload_sha256"], "available_at": pub["available_at"],
            "observation_set_sha256": pub["observation_set_sha256"],
            "watermark_expiry": plan["p_watermark_expiry"], "family": plan["p_family"], "R_source": plan["p_r_source"],
            "n_observations": len(plan["p_observations"]), "status_counts": _status_counts(plan["p_observations"])}


def send(tr, body, prepared_at, clock=_now, sleep=time.sleep, attempts=MAX_ATTEMPTS, progress=None):
    """POST the identical bytes; bounded retries on transport errors / 5xx while the body is fresh.
    progress (dict) records attempts as they happen so failure receipts keep them."""
    progress = progress if progress is not None else {}
    last = None
    for i in range(attempts):
        if clock() - prepared_at > FRESH_LIMIT:
            if i == 0:
                raise SeedError(6, "STALE_RECONCILE_REQUIRED", {"attempts": 0, "sent": False})
            raise Ambiguous(6, "STALE_RECONCILE_REQUIRED", {"attempts": i, "last": last, "sent": True})
        progress["attempts"] = i + 1
        progress["write_may_exist"] = True
        try:
            st, resp = tr.rpc("ev_builder_seed", body)
        except TRANSPORT_ERRORS as e:
            last = {"transport_error": type(e).__name__}
            sleep(min(30, 2 ** i))
            continue
        if st >= 500:
            last = {"http": st}
            sleep(min(30, 2 ** i))
            continue
        if st != 200:
            code, msg = _err_code(resp)
            if i > 0:     # an earlier attempt may have committed: do not claim nothing was written
                raise Ambiguous(6, "AMBIGUOUS_DELIVERY_RECONCILE_REQUIRED",
                                {"attempts": i + 1, "http": st, "code": code, "message": msg})
            raise SeedError(5, "SEED_REJECTED", {"http": st, "code": code, "message": msg, "attempt": i + 1})
        try:
            ack = json.loads(resp)
        except (ValueError, TypeError):
            raise Ambiguous(6, "ACK_MALFORMED_WRITE_MAY_EXIST", {"attempts": i + 1, "malformed": "ack"})
        if not isinstance(ack, dict) or ack.get("status") not in ("SEEDED", "DUPLICATE") \
                or not isinstance(ack.get("calibration_version"), str) \
                or (ack["status"] == "SEEDED" and (not isinstance(ack.get("n_observations"), int)
                                                   or isinstance(ack.get("n_observations"), bool))):
            raise Ambiguous(6, "ACK_MALFORMED_WRITE_MAY_EXIST", {"attempts": i + 1, "malformed": "ack_shape"})
        return ack, i + 1
    raise Ambiguous(6, "AMBIGUOUS_DELIVERY_RECONCILE_REQUIRED", {"attempts": attempts, "last": last})


def verify_persisted(st, row, plan, clock=_now, fresh_available_at=None):
    """Complete verification of an UNADVANCED seed, shared by the fresh-write and the reconciliation paths.
    Uses the persisted publication's own available_at and recomputes its digests; never rewrites anything.
    fresh_available_at: for a SEEDED ack the persisted availability must equal the body this run sent.
    Returns (checks, evidence)."""
    pub = plan["p_publication"]
    plan_ids = [o["observation_sha256"] for o in plan["p_observations"]]
    obs = st.get("observations") or []
    recomputed, stored_ok = [], True
    for o in obs:
        try:
            rid = CB.observation_identity(o)
        except (TypeError, ValueError, KeyError):
            rid = None
        recomputed.append(rid)
        stored_ok = stored_ok and rid is not None and o.get("observation_sha256") == rid
    checks = {
        "watermark": st.get("watermark_expiry") == plan["p_watermark_expiry"],
        "R": str(st.get("R")) == plan["p_r_value"],
        "R_source": st.get("R_source") == plan["p_r_source"],
        "family": st.get("calibration_family") == plan["p_family"],
        "checkpoint_null": st.get("checkpoint") is None,
        "observation_digests_recomputed": stored_ok,
        "observations_no_duplicates": len(set(recomputed)) == len(recomputed),
        "observation_set_exact": sorted(r for r in recomputed if r) == sorted(plan_ids),
        "status_counts": _status_counts(obs) == _status_counts(plan["p_observations"]),
        "publication_present": row is not None,
    }
    ev = {"n_observations": len(obs), "status_counts": _status_counts(obs),
          "observation_set_sha256": _obs_set_sha([r for r in recomputed if r]),
          "watermark_expiry": st.get("watermark_expiry"), "checkpoint": "null" if st.get("checkpoint") is None
          else "present"}
    if row is not None:
        pl = row["payload"]
        body = {k: v for k, v in pl.items() if k != "payload_sha256"}
        payload_digest = CB.sha256_text(CB.canon(body))
        row_at, pl_at = _aware(row.get("available_at")), _aware(pl.get("available_at"))
        checks.update({
            "row_version": row.get("calibration_version") == pub["calibration_version"]
            and pl.get("calibration_version") == pub["calibration_version"],
            "content_identity": row.get("content_sha256") == pub["content_sha256"]
            and pl.get("content_sha256") == pub["content_sha256"] and CB._content_sha(pl) == pub["content_sha256"],
            "payload_digest_recomputed": payload_digest == pl.get("payload_sha256") == row.get("payload_sha256"),
            "publication_observation_set": pl.get("observation_set_sha256") == pub["observation_set_sha256"]
            == _obs_set_sha(plan_ids),
            "row_latest_processed_expiry": row.get("latest_processed_expiry") == pl.get("latest_processed_expiry")
            == plan["p_watermark_expiry"],
            "available_at_aware": row_at is not None and pl_at is not None,
            "row_payload_available_at_agree": row_at is not None and pl_at is not None and row_at == pl_at,
            "retrospective_seed": pl.get("retrospective_seed") is True,
        })
        if fresh_available_at is not None:
            checks["available_at_is_this_run"] = pl.get("available_at") == fresh_available_at
        vstat = "NO_CALIBRATION_INVALID"
        if pl_at is not None:
            try:
                _ks, vstat = CB.validate_publication(pl, now_dt=clock(), cal=CAL.DEFAULT_CALENDAR)  # real clock
            except Exception:  # noqa: BLE001 - a malformed payload is a failed check, never a crash
                vstat = "NO_CALIBRATION_INVALID"
        checks["phone_validator"] = vstat == "OK"
        ev.update(calibration_version=row.get("calibration_version"), content_sha256=row.get("content_sha256"),
                  payload_sha256=row.get("payload_sha256"), payload_sha256_recomputed=payload_digest,
                  available_at=pl.get("available_at"), row_available_at=row.get("available_at"),
                  phone_validator_status=vstat)
    return checks, ev


def _advanced(st, plan):
    """Legitimately advanced past the seed by a later publication (watermark) or a compaction (checkpoint)."""
    wm = st.get("watermark_expiry")
    return st.get("checkpoint") is not None or (isinstance(wm, str) and wm > plan["p_watermark_expiry"])


def reconcile_existing(tr, state, plan, clock=_now):
    """Existing state: ALREADY_SEEDED_CONSISTENT only after the complete verification of THIS reviewed, unadvanced
    seed. Advanced (later publication/compaction) state is a named non-success. Never reset or overwrite."""
    pub = plan["p_publication"]
    ident = (state.get("calibration_family") == plan["p_family"] and state.get("R_source") == plan["p_r_source"]
             and str(state.get("R")) == plan["p_r_value"])
    if ident and _advanced(state, plan):
        raise SeedError(4, "STATE_ADVANCED_INVESTIGATE", {
            "server_watermark": state.get("watermark_expiry"), "checkpoint": state.get("checkpoint") is not None,
            "seed_watermark": plan["p_watermark_expiry"], "expected_version": pub["calibration_version"]})
    row = read_publication(tr, pub["calibration_version"], "reconcile") if ident else None
    checks, ev = verify_persisted(state, row, plan, clock=clock)
    bad = sorted(k for k, v in checks.items() if not v)
    if bad:
        raise SeedError(4, "EXISTING_STATE_INCOMPATIBLE", {
            "failed": bad, "server_family": state.get("calibration_family"),
            "server_R_source": state.get("R_source"), "server_watermark": state.get("watermark_expiry"),
            "expected_version": pub["calibration_version"]})
    return {"status": "ALREADY_SEEDED_CONSISTENT", "checks": checks, "persisted": ev,
            "server_available_at": ev.get("available_at")}


def verify(tr, plan, ack, clock=_now):
    """Post-ACK readback. Read failures keep the ambiguity (the write may exist); mismatches are exit 7."""
    pub = plan["p_publication"]
    if ack["calibration_version"] != pub["calibration_version"]:
        raise SeedError(7, "ACK_MISMATCH", {"ack_status": ack["status"]})
    if ack["status"] == "SEEDED" and ack["n_observations"] != len(plan["p_observations"]):
        raise SeedError(7, "ACK_COUNT_MISMATCH", {"ack_n": ack["n_observations"]})
    st = read_state(tr, "post_write")
    if st is None:
        raise SeedError(7, "STATE_MISSING_AFTER_ACK")
    row = read_publication(tr, pub["calibration_version"], "post_write")
    checks, ev = verify_persisted(st, row, plan, clock=clock,
                                  fresh_available_at=pub["available_at"] if ack["status"] == "SEEDED" else None)
    bad = sorted(k for k, v in checks.items() if not v)
    if bad:
        raise SeedError(7, "READBACK_MISMATCH", {"failed": bad, "persisted": ev})
    return checks, ev


def run(argv=None, env=None, transport=None, clock=_now, sleep=time.sleep, out=sys.stdout):
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-seed", action="store_true", help="perform the one explicit production seed")
    ap.add_argument("--receipt", help="write the sanitized receipt JSON here")
    a = ap.parse_args(argv)
    env = os.environ if env is None else env
    receipt = {"schema": RECEIPT_SCHEMA, "run_sha": env.get("GITHUB_SHA"), "started_at": clock().isoformat(),
               "write_may_exist": False, "attempts": 0}
    progress = {"attempts": 0, "write_may_exist": False}
    code = 0
    try:
        if not a.confirm_seed:
            plan, body, _ = prepare(clock())
            receipt.update(status="PREPARED_NOT_CONFIRMED", plan=summary(plan), body_bytes=len(body))
            code = 2
        else:
            url, key = config(env)
            # SUPABASE_REST_PREFIX: test-only override for a bare scratch PostgREST (production: /rest/v1)
            tr = transport or Transport(url, key, prefix=env.get("SUPABASE_REST_PREFIX", "/rest/v1"))
            plan0, _b, _t = prepare(clock())
            receipt["plan"] = summary(plan0)
            existing = read_state(tr, "pre_write")
            if existing is not None:
                receipt["phase"] = "reconcile"
                receipt.update(reconcile_existing(tr, existing, plan0, clock=clock))
            else:
                plan, body, prepared_at = prepare(clock())   # availability = immediately before the request
                receipt.update(plan=summary(plan), body_bytes=len(body), phase="write")
                ack, _n = send(tr, body, prepared_at, clock=clock, sleep=sleep, progress=progress)
                receipt["ack_status"] = ack["status"]
                checks, ev = verify(tr, plan, ack, clock=clock)
                receipt.update(status=ack["status"], checks=checks, persisted=ev,
                               server_available_at=ev.get("available_at"))
    except SeedError as e:
        receipt.update(status="FAILED", reason=e.reason, detail=e.detail, failure_class=type(e).__name__)
        code = e.code
    except Exception as e:  # noqa: BLE001 - never lose the receipt; type name only, never the message
        receipt.update(status="FAILED", reason="INTERNAL_ERROR", detail={"error_type": type(e).__name__})
        code = 9
    receipt["attempts"] = progress["attempts"]
    receipt["write_may_exist"] = progress["write_may_exist"]
    receipt["finished_at"] = clock().isoformat()
    txt = json.dumps(receipt, indent=1, sort_keys=True, default=str)
    if a.receipt:
        try:
            with open(a.receipt, "w") as f:
                f.write(txt + "\n")
        except OSError as e:
            receipt.update(receipt_write_failed=type(e).__name__)
            if code == 0:
                receipt.update(status="FAILED", reason="RECEIPT_WRITE_FAILED")
            code = 8 if code in (0, 2) else code
            txt = json.dumps(receipt, indent=1, sort_keys=True, default=str)
    print(txt, file=out)
    return code


if __name__ == "__main__":
    raise SystemExit(run())
