# Sandbox order-execution corrections G1–G4 (18 Oct 2026)

Implements §2 of Codex's "decision on Claude R3.3" only. §3–§5 (future controller, research, broker-limit) are spec only and
have no code here.

Unchanged by this patch:
- the sandbox endpoint lock (`https://sandbox.upstox.com`, `EXECUTION_MODE = SANDBOX`);
- Real and Paper behaviour, execution-mode derivation and versions;
- Supabase, tokens and orders.

All tests use fake transports; nothing calls the broker.

## G1: no implicit LTP fallback (`OrderExecutionService.resolveLimitPrice`)
| Source | Requirement | Provenance recorded |
|---|---|---|
| `REQUESTED_LIMIT` | Positive leg `price` **and** an explicit `price_source` label (`requested_limit`, `requested`, `explicit_limit` or `explicit`, on the leg or root) | `requested_price`, `executable_quote=false`, `fill_claim=false` |
| `QUOTE_DERIVED_LIMIT` | No leg price, and exactly one quote object (`quotes`/`quote_map`[token] or `leg.quote`) that passes the checks below | quote side, value, timestamp, age, `fill_claim=false` |

Quote checks:
- identity matches the token (the map key, or the quote's own `instrument_key`);
- not marked invalid;
- required side present and positive (BUY `ask`, SELL `bid`);
- book not crossed;
- timestamp present (`ts_ms` / `quote_ts_ms` / `timestamp_ms`) and age within 0..10 s (the caller may tighten this with
  `max_quote_age_ms`, never widen it).

Refusal codes:
| Code | Cause |
|---|---|
| `PRICE_SOURCE_AMBIGUOUS` | The payload cannot tell its price apart: a positive unlabelled leg price, or a price labelled as a quote |
| `PRICE_SOURCE_UNKNOWN` | Unrecognised label |
| `EXECUTABLE_QUOTE_REQUIRED`, `QUOTE_SIDE_REQUIRED`, `QUOTE_SIDE_INVALID` | No quote, or a missing/zero/invalid side. **LTP is never substituted.** |
| `QUOTE_IDENTITY_*`, `QUOTE_STALE`, `QUOTE_CROSSED`, `QUOTE_TIMESTAMP_REQUIRED`, `QUOTE_INVALID` | Failed quote checks |
| `DUPLICATE_CORRELATION_ID`, `CORRELATION_ID_INVALID` | Line identity (>20 characters for the latter) |

Provenance is returned in `pricing[]`. The order JSON sent to the broker is unchanged (same 13 keys).

Consequence: the existing MarketVivi BUILD-preview sends only candidate `ltp` (`notes.price_source = candidate_ltp_fallback`).
It now gets a truthful `EXECUTABLE_QUOTE_REQUIRED` instead of a LIMIT built from LTP. It remains preview-only with no
dispatch.

## G2: multi-order body parsed per line (`parseMultiPlacement`)
- Every requested `correlation_id` gets an outcome: ACCEPTED (broker IDs), REJECTED (per-line errors), PARTIAL (sliced
  line with a mix) or UNKNOWN.
- Accepted IDs are always kept in `order_ids` / `accepted_order_ids`, including IDs whose correlation_id doesn't match
  (`unmatched_accepted_order_ids`).
- Aggregate `placement_status`:

  | Status | When |
  |---|---|
  | `ALL_ACCEPTED` | Every line accepted and **no anomaly** |
  | `ALL_REJECTED` | Every line rejected and no accepted ID |
  | `PARTIAL` | Some IDs accepted and some lines not |
  | `UNKNOWN` | Otherwise |

- Anomalies: malformed body or 2xx, missing data, malformed or ID-less entries, unmatched IDs, summary/body-status/HTTP
  mismatch, unverifiable slice counts, no HTTP response.
- `ok` is true only for `ALL_ACCEPTED`.
- 4xx (except 408) without line detail rejects the whole request. 408, 5xx and timeouts are UNKNOWN.
- `fill_status = UNKNOWN_NOT_RECONCILED` and `fill_implied = false` always.

## G3: sequential debug helper stops (`placeSequential`)
- Submits in build order (BUY legs first) and stops the whole remaining sequence after the first REJECTED or UNKNOWN
  submission.
- Returns `accepted_order_ids` (earlier IDs kept), `not_submitted_correlation_ids`, `stopped_at_index` and `placement_status`
  (`ALL_ACCEPTED`, `STOPPED_AFTER_REJECTION` or `STOPPED_AFTER_UNCERTAIN`).
- Single-order ACCEPTED requires 2xx, `status` success (if given), a broker `order_id` and no errors.
- One transport call per dispatch, with no retry loop. The OkHttp client now sets `retryOnConnectionFailure(false)` and
  doesn't follow redirects.
- Even `ALL_ACCEPTED` is placement-only: `dependent_live_short_entry_authorized = false`. No future controller is added.

## G4: configured ≠ verified-capable
- `ExecutionInfraStatus` (used by `NativeBridge.getExecutionInfraStatus`):
  - `liveReady` keeps its name but now means verified-capable, so it is **always false**;
  - `liveConfigured` carries the old token + HTTPS-proxy meaning;
  - `liveCapability` is `UNVERIFIED` / `NOT_CONFIGURED` / `UNKNOWN`, with `liveCapabilityVerified = false` and a reason;
  - paper and sandbox fields are unchanged.
- `brain.check_execution_readiness`: additive `liveDispatchVerified = False`, `liveDispatchCapability` and
  `checks.proxyConfigured`. **`ready`, `gate` and `reasons` are unchanged**, because Real entry authority
  (`finalEntryAuthorization` in MarketVivi) reads them.
- No IP registration, route or order is used to obtain a true result.
- PWA: the unchanged MarketVivi template now renders "Proxy: CONFIGURED · Live WAIT · Paper READY" for token + proxy only.
  It previously rendered "Live READY". Verified by `tools/sandbox_exec/test_g4_pwa_infra_render.mjs` on the Android golden.
  No MarketVivi change.
