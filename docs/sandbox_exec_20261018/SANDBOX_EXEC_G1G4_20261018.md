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
- **both** sides present, finite and positive, with ask ≥ bid (R3). A one-sided book is refused with
  `QUOTE_OPPOSITE_SIDE_REQUIRED` / `QUOTE_OPPOSITE_SIDE_INVALID`, any negative side with `QUOTE_SIDE_NEGATIVE`, and a
  crossed book with `QUOTE_CROSSED`;
- timestamp present (`ts_ms` / `quote_ts_ms` / `timestamp_ms`). The quote age (now − ts) must lie in **−2 s .. +10 s**:
  - it allows up to 2 s of future clock skew, so the window is not strictly 0..10 s;
  - the 10 s ceiling is a conservative sandbox engineering choice, not a research claim about fill reliability;
  - a caller may tighten it with `max_quote_age_ms`, never widen it.

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

## Codex PR #20 review follow-up (R1–R3, 20 Oct 2026)
- **R1** — one conservative classification combines transport status, body status, line evidence and dispatch uncertainty.
  - A non-2xx response can never be `ALL_ACCEPTED`.
  - 408, 5xx, other non-2xx statuses and no response (`UNCERTAIN_TRANSPORT_STATUS` / `NO_HTTP_RESPONSE`) make the result
    UNKNOWN.
  - A 4xx carrying order IDs is contradictory (`REJECTION_STATUS_WITH_ORDER_IDS`) and also UNKNOWN.
  - Lines with IDs under such a status are UNKNOWN. All IDs, errors and raw records are kept.
  - Public `place_multi` returns `ok` only when dispatch's ok (2xx, no error) **and** `ALL_ACCEPTED` both hold. Otherwise
    it keeps the dispatch error code (e.g. `HTTP_503`) or sets `PLACEMENT_<status>`.
  - A normal complete placement still succeeds.
- **R2** — coherent identity.
  - An unsliced accepted line needs exactly one broker ID (`MULTIPLE_IDS_FOR_UNSLICED_LINE`).
  - A broker ID can't belong to two lines (`DUPLICATE_BROKER_ORDER_ID`). These contradictions make the result UNKNOWN.
  - An exact repeated record is `DUPLICATE_RESPONSE_RECORD` and never counts as a second placement.
  - `accepted_order_ids` holds distinct observed IDs. The summary is checked against distinct IDs and against
    success > requested.
  - Any unmatched or batch error (`UNMATCHED_OR_BATCH_ERROR`) blocks `ALL_ACCEPTED`.
  - Raw evidence is in `raw_data_records` / `raw_error_records`. Sliced lines stay conservative (`SLICE_COUNT_UNVERIFIED`).
- **R3** — the two-sided book requirement above applies to BUY and SELL alike. An explicit `REQUESTED_LIMIT` is unchanged
  and needs no quote.
- Tests: `Pr20CounterexampleTest` holds Codex's nine named counterexamples (BUY and SELL symmetric) plus three
  preservation cases.
