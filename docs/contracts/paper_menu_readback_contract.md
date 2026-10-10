# Paper decision-menu durable-content and readback contract (R4 + R2-review Blocker A)

Status: branch `work/evidence-batch1-20261009` (local, not pushed). Supersedes `DURABLE_CONTENT_READBACK_CONTRACT.md` of the R1 return
(§1, §2, §4 unchanged; §3 and §5 replaced). Migration 20261009120000 is unchanged by this correction and is **not applied** to production.

## 1. Digests (unchanged from R1)
| Digest | Input | Role |
|---|---|---|
| `payload_sha256` | SHA-256 of the frozen device file bytes | payload identity on every RPC; not content verification |
| `candidate_ids_sha256` | SHA-256(join("\n", candidate_id by generated_rank)) | membership/order only |
| row `content_sha256` | SHA-256(row `content_canonical`) | CHECK-bound per-row content (`pdmc_content_bound`) |
| `content_sha256` / `durable_content_sha256` | SHA-256(join("\n", row content_sha256 by rank)); 0 rows -> SHA-256("") | durable-content digest |

Full frozen-file / manifest-byte verification is **not** claimed (the server never receives the full file).

## 2. Canonicalization (unchanged)
`pc2_compact_contract_v4_pinned_canonical` = `Pc2CompactBatch.canonicalJson`; the server stores and hashes the client's exact bytes.

## 3. Upload and acknowledgement (PaperMenuUploader.upload; SupabaseClient.savePaperDecisionMenu delegates, SupabaseClient.kt:1945)
1. `paper_decision_menu_ingest_manifest` -> ACCEPTED | REPLAY | REJECTED_CONFLICT.
2. `paper_decision_menu_ingest_candidates` in chunks of 200 -> OK | REJECTED_TERMINAL | REJECTED_CONFLICT.
3. `paper_decision_menu_finalize` -> completeness_state, `durable_candidate_rows`, `durable_content_sha256`.
4. If COMPLETE / NO_ROWS_EXPECTED: `durable_content_sha256` must equal the local digest (else MISMATCH `DURABLE_DIGEST_DIFFERS`, PaperMenuUploader.kt:128), then **readback** (§4). Readback now also runs for empty menus and MISSED manifests (one empty page).
5. ACKNOWLEDGED only for `Readback.Verified`.

## 4. Readback (PaperMenuUploader.readback, PaperMenuUploader.kt:145-188)
Request (PaperMenuUploader.kt:155-156):
`GET ml_paper_decision_menu_candidates?menu_id=eq.<id>&generated_rank=gt.<cursor>&select=candidate_id,generated_rank,content_sha256&order=generated_rank.asc&limit=500` with `Prefer: count=exact`.

- **Keyset, not offset.** The cursor starts at 0 (`generated_rank > 0` is a table CHECK) and becomes the last rank seen. Ranks are unique per menu (`pdmc_rank_unique`).
- **A short page is not end-of-data.** The server may cap a page below the requested 500 (PostgREST `db-max-rows`; Supabase default 1,000; the project's effective cap is unknown). The loop continues.
- **End of data is proven only by**: (a) an empty page after the cursor (with any exact count = 0) — PaperMenuUploader.kt:163-166; or (b) the page's exact remaining count (Content-Range total) equal to the rows on that page — PaperMenuUploader.kt:180.
- **Progress / duplicate**: every row's rank must be > cursor (PaperMenuUploader.kt:172), so a repeated or non-advancing page is detected.
- **Count consistency**: each page's remaining count must equal the previous remaining count minus the previous page's rows (PaperMenuUploader.kt:161-162); an empty page with a non-zero count, or a count below the page's rows, is inconsistent (:164, :167-168).
- **Bounded**: at most `local rows + 2` requests (PaperMenuUploader.kt:150); reading stops once more rows than declared/local are seen (:177-178).
- **Server agreement**: the proven-complete row count must equal finalize's `durable_candidate_rows` (PaperMenuUploader.kt:184-185); a missing declared count is unproven (:146).
- **Comparison**: `PaperMenuCapture.readbackDifference(local, collected)` (PaperMenuUploader.kt:186) — same count, candidate_id, rank and SHA-256 of the local canonical bytes, row by row.

## 5. Dispositions and outbox states
| Uploader disposition | Meaning | Outbox state (PaperMenuOutbox.drain) | `readback_state` |
|---|---|---|---|
| ACKNOWLEDGED | finalize COMPLETE/NO_ROWS_EXPECTED + digest equal + readback Verified | ACKED (only if the state write succeeded) | VERIFIED |
| READBACK_INCOMPLETE | readback not proven complete: HTTP error/timeout on any page, unparseable page, non-progressing/duplicate rows, inconsistent counts, page budget, disagreement with the declared count | RETRY_PENDING -> EXHAUSTED after 12 attempts in a session; one new round per new session | INCOMPLETE |
| MISMATCH | established difference: durable digest differs, server MISMATCH, or a proven-complete count-consistent readback whose rows differ (e.g. a changed hash) | MISMATCH_RETAINED (never auto-retried, never pruned) | MISMATCH |
| RETRY / CONFLICT / REJECTED_PERMANENT | unchanged from R4 | RETRY_PENDING / CONFLICT_RETAINED / REJECTED_RETAINED | — |

An omitted row is classified as INCOMPLETE (the readback disagrees with the server's own declared count, or the remaining counts are inconsistent), not MISMATCH: the server already declared COMPLETE with an equal durable digest, so a missing row in a response is a transport/readback anomaly. It is never acknowledged.

## 6. Evidence
`EvidenceBatch1R6ReadbackPaginationTest` (11/11): real OkHttp transport against a loopback fake PostgREST that enforces a row cap, models the RPCs (manifest REPLAY, candidate replay, terminal finalize) and Content-Range counts:
1,100 rows / cap 1,000 -> ACKNOWLEDGED (3 GETs with count; 4 without); 740 rows / cap 100 and 200 -> all pages; exact page boundaries (500/1000/200/400 rows); empty menu; later-page timeout and 503 -> RETRY_PENDING/INCOMPLETE then identical frozen bytes ACKED; duplicate/non-progressing page -> INCOMPLETE, never ACKED, ends EXHAUSTED not MISMATCH; omitted row (with/without count) -> INCOMPLETE; changed hash -> MISMATCH_RETAINED, not retried, not pruned; cap of 1 row -> bounded 31 GETs for 30 rows.

## 7. Limits
- The fake server models PostgREST semantics (`gt`, `order`, `limit`, `Prefer: count=exact`, `Content-Range`, cap below `limit`); it is not the Supabase PostgREST. The project's effective `db-max-rows` is unverified.
- `count=exact` costs one count per page on the server (≤ ~3 pages for 1,100 rows at a 500-row page).
- RPC behaviour itself remains tested by direct SQL on disposable PG17 (R4 suite), not through Supabase HTTP.
