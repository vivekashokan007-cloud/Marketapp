#!/usr/bin/env python3
"""Stage the hash-pinned MarketVivi PWA runtime for the Market Radar CANDIDATE build type.

CANDIDATE-ONLY / DO NOT MERGE. Used only by .github/workflows/candidate-apk-artifact.yml (and local review).

* Bytes are read from git objects at the pinned commit (``git cat-file blob <sha>:<file>``), never from the
  working tree, so local edits cannot leak into the candidate.
* Only the PWA runtime allowlist is copied: the same six files as MarketVivi tools/build_pages_site.py.
  Repository documents, tests, audit packages and credentials are never staged.
* Every file must match the committed expected manifest (sha256 + byte size). Any mismatch fails closed.
* The committed expected manifest is copied byte-for-byte next to the bundle as ``candidate_manifest.json``;
  the app re-verifies it at runtime against BuildConfig.PWA_MANIFEST_SHA256.

Modes:
  --write-expected  (review/maintenance) create the expected manifest from a pinned MarketVivi commit.
  default           stage + verify into --out (must be absent or empty).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "marketradar.pwa_candidate.v1"
DOCUMENT_BASE = "https://vivekashokan007-cloud.github.io/MarketVivi/"
# Same order and content as MarketVivi tools/build_pages_site.py RUNTIME_FILES.
RUNTIME_FILES = ("index.html", "app.js", "api.js", "log-viewer.js", "style.css", "manifest.json")
MANIFEST_ASSET_NAME = "candidate_manifest.json"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")


class CandidateError(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"CANDIDATE_PWA_ERROR: {msg}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as e:  # pragma: no cover - message path
        raise CandidateError(f"git {' '.join(args)} failed: {e.stderr.decode(errors='replace').strip()}")


def read_pinned(repo: Path, commit: str, name: str) -> bytes:
    """Bytes of `name` at `commit`; must be a regular (non-symlink, non-executable) blob."""
    tree = git(repo, "ls-tree", commit, "--", name).decode().strip()
    if not tree:
        raise CandidateError(f"required runtime file missing at {commit}: {name}")
    mode, kind = tree.split()[0], tree.split()[1]
    if kind != "blob" or mode != "100644":
        raise CandidateError(f"runtime file is not a regular blob at {commit}: {name} ({mode} {kind})")
    return git(repo, "cat-file", "blob", f"{commit}:{name}")


def load_expected(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    m = json.loads(raw)
    if m.get("schema") != SCHEMA:
        raise CandidateError(f"unexpected manifest schema: {m.get('schema')!r}")
    if not SHA40.match(str(m.get("marketvivi_sha", ""))):
        raise CandidateError("manifest marketvivi_sha is not a full 40-hex commit")
    if m.get("document_base") != DOCUMENT_BASE:
        raise CandidateError("manifest document_base differs from the production document URL")
    files = m.get("files")
    if not isinstance(files, dict) or sorted(files) != sorted(RUNTIME_FILES):
        raise CandidateError(f"manifest files must be exactly the runtime allowlist {list(RUNTIME_FILES)}")
    for name, entry in files.items():
        if not SHA64.match(str(entry.get("sha256", ""))) or not isinstance(entry.get("bytes"), int):
            raise CandidateError(f"manifest entry malformed: {name}")
    return m, raw


def build_expected(repo: Path, commit: str) -> dict:
    if not SHA40.match(commit):
        raise CandidateError("--marketvivi-sha must be a full 40-hex commit")
    files = {}
    for name in RUNTIME_FILES:
        data = read_pinned(repo, commit, name)
        files[name] = {"sha256": sha256(data), "bytes": len(data)}
    index = read_pinned(repo, commit, "index.html").decode("utf-8")
    appjs = read_pinned(repo, commit, "app.js").decode("utf-8")
    v = re.search(r'src="app\.js\?v=(\d+)"', index)
    ident = re.search(r"M0_PWA_IDENTITY\s*=\s*\{\s*version:\s*'([^']+)',\s*build:\s*(\d+),\s*app_js_v:\s*(\d+)", appjs)
    return {
        "schema": SCHEMA,
        "candidate_only": True,
        "marketvivi_sha": commit,
        "document_base": DOCUMENT_BASE,
        "pwa_identity": {
            "version": ident.group(1) if ident else None,
            "build": int(ident.group(2)) if ident else None,
            "app_js_v": int(ident.group(3)) if ident else None,
            "index_app_js_v": int(v.group(1)) if v else None,
        },
        "files": files,
    }


def verify_bytes(expected: dict, name: str, data: bytes) -> None:
    want = expected["files"][name]
    got = sha256(data)
    if got != want["sha256"] or len(data) != want["bytes"]:
        raise CandidateError(f"hash mismatch for {name}: got sha256={got} bytes={len(data)}, "
                             f"expected sha256={want['sha256']} bytes={want['bytes']}")


def stage(repo: Path, expected_path: Path, out: Path, expect_sha: str | None = None) -> list[str]:
    expected, raw = load_expected(expected_path)
    commit = expected["marketvivi_sha"]
    if expect_sha is not None and expect_sha != commit:
        raise CandidateError(f"requested PWA sha {expect_sha} != pinned manifest sha {commit}")
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    if head != commit:
        raise CandidateError(f"MarketVivi checkout HEAD {head} != pinned {commit}")
    if out.exists() and any(out.iterdir()):
        raise CandidateError(f"output directory must be absent or empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in RUNTIME_FILES:
        data = read_pinned(repo, commit, name)
        verify_bytes(expected, name, data)
        (out / name).write_bytes(data)
        lines.append(f"{sha256(data)}  {name}")
    (out / MANIFEST_ASSET_NAME).write_bytes(raw)
    # Re-read what was written (catches filesystem surprises) and ensure nothing else is in the bundle dir.
    for name in RUNTIME_FILES:
        verify_bytes(expected, name, (out / name).read_bytes())
    present = sorted(p.name for p in out.iterdir())
    if present != sorted((*RUNTIME_FILES, MANIFEST_ASSET_NAME)):
        raise CandidateError(f"unexpected files in bundle dir: {present}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--marketvivi-dir", required=True, type=Path)
    ap.add_argument("--expected-manifest", type=Path,
                    default=Path(__file__).resolve().parents[2] / "app/src/candidate/pwa_expected_manifest.json")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--expect-sha", help="optional cross-check: must equal the pinned manifest sha")
    ap.add_argument("--write-expected", metavar="SHA", help="write the expected manifest for this commit and exit")
    a = ap.parse_args(argv)
    if a.write_expected:
        m = build_expected(a.marketvivi_dir, a.write_expected)
        a.expected_manifest.parent.mkdir(parents=True, exist_ok=True)
        a.expected_manifest.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {a.expected_manifest} for MarketVivi {a.write_expected}")
        return 0
    if a.out is None:
        ap.error("--out is required unless --write-expected is used")
    for line in stage(a.marketvivi_dir, a.expected_manifest, a.out, a.expect_sha):
        print("CANDIDATE_PWA_STAGED " + line)
    print("CANDIDATE_PWA_MANIFEST_SHA256 " + sha256(a.expected_manifest.read_bytes()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
