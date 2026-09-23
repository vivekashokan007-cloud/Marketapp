"""Read-only research export of persisted position_ticks for advice parity.

Server-side / research tooling only. NEVER bakes privileged credentials into
Android or PWA runtime. Callers supply a read-only page client/adapter (mock,
PostgREST env client, or any callable that returns actual DB rows).

Pages all matching rows in stable ``(tick_ts, id)`` order and returns them for
the existing idempotent importer + join. Clearly labels source, scope, count,
ordering, and whether the database was reachable. Fixture-only evidence must
never be labeled as production readback.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

REQUIRED_COLUMNS = ("id", "trade_id", "session_date", "tick_ts", "policy_trace_json")
DEFAULT_PAGE_SIZE = 100
ORDERING = "(tick_ts, id)"


class PositionTicksPageClient(Protocol):
    """Read-only paging adapter over persisted position_ticks."""

    def fetch_page(
        self,
        *,
        after_tick_ts: Optional[str],
        after_id: Optional[Any],
        limit: int,
        session_date: Optional[str] = None,
        trade_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the next page ordered by (tick_ts, id), strictly after cursor."""


def _coerce_id_sort_key(id_val: Any) -> Tuple[int, Any]:
    """Prefer numeric ID ordering so 1,2,10 not 1,10,2 (string sort bug)."""
    if id_val is None:
        return (2, "")
    if isinstance(id_val, bool):
        return (1, str(id_val))
    if isinstance(id_val, int):
        return (0, id_val)
    try:
        s = str(id_val).strip()
        if s and (s.isdigit() or (s[0] == "-" and s[1:].isdigit())):
            return (0, int(s))
        # Accept plain decimal integers without float coercion surprises.
        as_int = int(s)
        return (0, as_int)
    except (TypeError, ValueError):
        return (1, str(id_val))


def _row_sort_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    return (str(row.get("tick_ts") or ""), _coerce_id_sort_key(row.get("id")))


def _is_mock_or_fixture_client(client: Any) -> bool:
    if isinstance(client, MockMultiPagePositionTicksClient):
        return True
    if getattr(client, "fixture_only_client", False) or getattr(client, "is_fixture_client", False):
        return True
    return False


def _client_supports_live_production_readback(client: Any) -> bool:
    """Live label requires real live/production client capability — never flag alone."""
    if _is_mock_or_fixture_client(client):
        return False
    if isinstance(client, EnvSupabaseRestPositionTicksClient):
        return True
    return bool(getattr(client, "supports_live_production_readback", False))


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: row.get(k) for k in REQUIRED_COLUMNS}
    # Preserve extra columns when present (research diagnostics).
    for k, v in row.items():
        if k not in out:
            out[k] = v
    return out


def fetch_all_position_ticks_readonly(
    client: PositionTicksPageClient,
    *,
    session_date: Optional[str] = None,
    trade_id: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    source_label: str = "readonly_page_client",
    live_production_readback: bool = False,
) -> Dict[str, Any]:
    """Page every matching position_ticks row via ``client`` in stable order.

    Returns a manifest with rows + provenance. ``db_reachable`` is True when
    every requested page completed without raising (including empty result).
    Mid-pagination failures fail closed (``status=partial_read_rejected``,
    empty rows). Mock/fixture clients are never labeled live regardless of the
    ``live_production_readback`` caller flag.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    rows: List[Dict[str, Any]] = []
    after_tick_ts: Optional[str] = None
    after_id: Optional[Any] = None
    pages = 0
    db_reachable = False
    error: Optional[str] = None
    try:
        while True:
            page = client.fetch_page(
                after_tick_ts=after_tick_ts,
                after_id=after_id,
                limit=page_size,
                session_date=session_date,
                trade_id=trade_id,
            )
            db_reachable = True
            pages += 1
            if not page:
                break
            for raw in page:
                if not isinstance(raw, dict):
                    raise ValueError("position_ticks_page_row_must_be_dict")
                rows.append(_normalize_row(raw))
            last = page[-1]
            after_tick_ts = str(last.get("tick_ts") or "") or None
            after_id = last.get("id")
            if len(page) < page_size:
                break
            # Defensive: avoid infinite loops on non-advancing cursors.
            if pages > 100_000:
                raise RuntimeError("position_ticks_readonly_pagination_guard")
    except Exception as exc:  # noqa: BLE001 — research tooling surfaces reachability
        error = f"{type(exc).__name__}:{exc}"
        # Fail closed on mid-pagination failure: never return partial rows for
        # import/join (reviewer reproduced false parity agreement from page-1-only).
        if pages > 0 and rows:
            status = "partial_read_rejected"
            rows = []
        else:
            status = "page_fetch_failed"
        db_reachable = False
    else:
        status = "ok"

    fixture_only = _is_mock_or_fixture_client(client)
    live_ok = bool(
        live_production_readback
        and _client_supports_live_production_readback(client)
        and (not fixture_only)
        and db_reachable
        and error is None
        and status == "ok"
    )

    # Enforce stable order even if a buggy adapter returns unsorted pages.
    rows.sort(key=_row_sort_key)
    return {
        "rows": rows,
        "count": len(rows),
        "pages_fetched": pages,
        "ordering": ORDERING,
        "source": source_label,
        "session_date": session_date,
        "trade_id": trade_id,
        "page_size": page_size,
        "db_reachable": db_reachable,
        "live_production_readback": live_ok,
        "fixture_only": fixture_only,
        "status": status,
        "partial_read_rejected": status == "partial_read_rejected",
        "error": error,
        "required_columns": list(REQUIRED_COLUMNS),
    }


class MockMultiPagePositionTicksClient:
    """Acceptance-test double: serves preloaded rows across multiple pages."""

    def __init__(self, rows: Iterable[Dict[str, Any]]):
        self._rows = sorted((_normalize_row(r) for r in rows), key=_row_sort_key)

    def fetch_page(
        self,
        *,
        after_tick_ts: Optional[str],
        after_id: Optional[Any],
        limit: int,
        session_date: Optional[str] = None,
        trade_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in self._rows:
            if session_date is not None and str(row.get("session_date")) != str(session_date):
                continue
            if trade_id is not None and str(row.get("trade_id")) != str(trade_id):
                continue
            key = _row_sort_key(row)
            if after_tick_ts is not None and after_id is not None:
                cursor = (str(after_tick_ts), _coerce_id_sort_key(after_id))
                if key <= cursor:
                    continue
            out.append(dict(row))
            if len(out) >= limit:
                break
        return out


class EnvSupabaseRestPositionTicksClient:
    """PostgREST read-only SELECT using env credentials (research host only).

    Env:
      SUPABASE_URL
      SUPABASE_SERVICE_ROLE_KEY (preferred) or SUPABASE_KEY / SUPABASE_ANON_KEY
        Note: anon SELECT on position_ticks is dropped by migration — service
        role (or another authorized server credential) is required for live rows.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.base_url = (base_url or os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
            or ""
        )
        if not self.base_url or not self.api_key:
            raise RuntimeError(
                "supabase_env_credentials_unavailable:"
                "set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for live readback"
            )

    def fetch_page(
        self,
        *,
        after_tick_ts: Optional[str],
        after_id: Optional[Any],
        limit: int,
        session_date: Optional[str] = None,
        trade_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: List[Tuple[str, str]] = [
            ("select", "id,trade_id,session_date,tick_ts,policy_trace_json"),
            ("order", "tick_ts.asc,id.asc"),
            ("limit", str(limit)),
        ]
        if session_date is not None:
            params.append(("session_date", f"eq.{session_date}"))
        if trade_id is not None:
            params.append(("trade_id", f"eq.{trade_id}"))
        if after_tick_ts is not None and after_id is not None:
            # Keyset: (tick_ts, id) > (after_tick_ts, after_id)
            params.append(
                (
                    "or",
                    (
                        f"(tick_ts.gt.{after_tick_ts},"
                        f"and(tick_ts.eq.{after_tick_ts},id.gt.{after_id}))"
                    ),
                )
            )
        url = f"{self.base_url}/rest/v1/position_ticks?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": self.api_key,
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"supabase_rest_http_{exc.code}:{detail[:400]}") from exc
        data = json.loads(body) if body else []
        if not isinstance(data, list):
            raise RuntimeError("supabase_rest_expected_list")
        return [dict(r) for r in data]


def write_export_jsonl(manifest: Dict[str, Any], path: str) -> str:
    """Write meta + rows as JSONL. Meta line is prefixed with ``_export_meta``."""
    meta = {k: v for k, v in manifest.items() if k != "rows"}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_export_meta": meta}, default=str) + "\n")
        for row in manifest.get("rows") or []:
            fh.write(json.dumps(row, default=str) + "\n")
    return path


def load_export_jsonl(path: str) -> Dict[str, Any]:
    """Load an export produced by :func:`write_export_jsonl` or a plain JSONL dump."""
    rows: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and "_export_meta" in obj and len(obj) == 1:
                meta = dict(obj["_export_meta"] or {})
                continue
            if isinstance(obj, dict):
                rows.append(_normalize_row(obj))
    rows.sort(key=_row_sort_key)
    out = {
        "rows": rows,
        "count": len(rows),
        "ordering": meta.get("ordering", ORDERING),
        "source": meta.get("source", "jsonl_export_file"),
        "session_date": meta.get("session_date"),
        "trade_id": meta.get("trade_id"),
        "db_reachable": meta.get("db_reachable"),
        "live_production_readback": bool(meta.get("live_production_readback")),
        "fixture_only": bool(meta.get("fixture_only", False)),
        "export_meta": meta,
        "persisted_path": path,
    }
    return out


def build_client_from_env() -> EnvSupabaseRestPositionTicksClient:
    return EnvSupabaseRestPositionTicksClient()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only research export of position_ticks for advice parity."
    )
    parser.add_argument("--out", required=True, help="JSONL output path")
    parser.add_argument("--session-date", default=None)
    parser.add_argument("--trade-id", default=None)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--allow-missing-env",
        action="store_true",
        help="Exit 2 (soft) when env credentials are absent instead of failing hard",
    )
    args = parser.parse_args(argv)
    try:
        client = build_client_from_env()
    except RuntimeError as exc:
        if args.allow_missing_env:
            print(json.dumps({"ok": False, "db_reachable": False, "error": str(exc)}))
            return 2
        print(str(exc), file=sys.stderr)
        return 1
    manifest = fetch_all_position_ticks_readonly(
        client,
        session_date=args.session_date,
        trade_id=args.trade_id,
        page_size=args.page_size,
        source_label="env_supabase_rest_position_ticks",
        live_production_readback=True,
    )
    write_export_jsonl(manifest, args.out)
    summary = {k: v for k, v in manifest.items() if k != "rows"}
    summary["out"] = args.out
    print(json.dumps(summary, default=str))
    return 0 if manifest.get("db_reachable") and not manifest.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
