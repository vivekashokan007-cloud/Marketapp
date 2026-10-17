# X1 v2 — prospective Paper option-buying quote simulation (FROZEN protocol)

Protocol id: `X1_gap_continuation_v2_causal_paper_20261017`
Collector contract: `x1_collector_contract_v2_20261017`
Protocol digest (sha256 of canonical JSON: sorted keys, separators `,` `:`): **`10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa`**
Frozen: 17 October 2026 (IST), before any observation. Status: research only; no trading authority.
Revision: Codex b510 R1 corrections (R1 identity, R2 report integrity, R3 coverage policy, R4 hold anchor),
17 October 2026 (IST). **No prospective block exists or was reused:** the collector has never run on a phone
(b510 is unreleased), no journal was created, and no historical records were imported. The digest changed with
these corrections, so any journal written under an earlier digest would be refused (`PROTOCOL_DIGEST_MISMATCH`),
never continued.

## What this is (and is not)

- A prospective observation block inside **Paper** mode. It is not a new execution mode, not a candidate,
  never ranked, notified, ordered, or used as a training label. No `trades_v2` row is opened.
- A **quote simulation**: entries at the observed ask, exits at the observed bid, using the phone's
  option-chain HTTP fetch-completion time (`PHONE_FETCH_COMPLETION_MS`). This is not an exchange timestamp and
  not a fill guarantee.
- NONE of Claude's development trades (b502 extracts, `replay_fee_v1`) are imported. Those results were
  development data with different timing (10-minute entry tolerance, mixed expiries, research fees,
  repeated peeking from 40 to 60); they do not forecast this protocol.

## Deliberate v2 changes versus Claude X1

1. Same-expiry previous-session reference; an expiry roll is a SKIP (no synthetic continuity).
2. Causal timestamps: decision and quote receipt within fixed 90-second windows; quote age <= 90 s.
3. Canonical dated teacher fees (`ev_v12_fees.one_side_fee`, one order per side) and dated lot authority.
4. Delayed control = the SAME 09:20 contract and side re-entered at a fresh 09:25 ask (latency isolation),
   with its own 30-40 minute exit. It never re-selects a signal.
5. One fixed endpoint: exactly 60 resolved primary observations per index, evaluated once.
6. The hold is anchored at the **entry decision** (R4): an exit needs an exit-quote receipt at or after entry
   decision + 30 min and an exit decision no later than entry decision + 40 min (quote age <= 90 s). The entry quote
   age stays explicit (`entry_quote_ts_ms`). The delayed arm uses its own decision the same way. With 5-minute polls,
   the first eligible exit is usually the poll about 30–35 minutes after entry, depending on fetch lag.

## Contract identity (R1)

Upstox's documented `/v2/option/chain` response has `underlying_key`, `strike_price` and `expiry` on each row, and
`instrument_key`, `market_data` and `option_greeks` on each leg. It has **no** leg `trading_symbol`. Identity
therefore comes from documented fields and from the request the phone made:

- The native formatter copies each row's `underlying_key` and `expiry` onto both legs (`contract_expiry`). `mergeChain`
  stamps `requested_underlying_key` (`NSE_INDEX|Nifty 50` / `NSE_INDEX|Nifty Bank`) and `requested_expiry`
  (the same values as the option/chain URL).
- The collector requires, per leg: underlying == the index's underlying == the requested key; row expiry == requested
  expiry; a non-empty `NSE_FO|` instrument key that is unique in the chain, with CE != PE. A `trading_symbol` is
  optional, but if present it must agree with the index. The index is never inferred from an opaque instrument key.
  The delayed entry and every exit require the same instrument key, strike, side and expiry as the primary.
- Failures are bounded reasons: `CHAIN_INDEX_MISMATCH` (wrong or swapped underlying, other-index request, wrong
  symbol), `EXPIRY_MISMATCH`, `CHAIN_IDENTITY_UNAVAILABLE` (identity fields missing, e.g. an older formatter),
  `CONTRACT_IDENTITY_MISMATCH`. A reference-window poll that cannot capture a reference returns status
  `REFERENCE_NOT_CAPTURED`, with `reference_capture` per index. The reason is persisted in `attempts`.
- Evidence: a documented raw fixture goes through the real `OptionChainBrainFormatter` (JVM golden test) into the
  collector, covering reference, entry, control and both exits.

## Success review (never automatic authority)

Per index separately, evaluated in this order by `tools/x1_report.py`:

1. **Integrity gate (R2).** Any failure gives `BLOCKED_INTEGRITY_FAILURE`, with no statistics and CLI exit 2:
   - an export hash that is missing, false or malformed;
   - a wrong schema, protocol id, digest or block contract;
   - a corruption or contract stop;
   - duplicate or invalid rows;
   - non-finite or inconsistent economics;
   - an exit-timing violation, invalid pairing, or more than 60 resolved.

   A raw journal has no export hash and is checked as a journal. A capacity stop stays descriptive, flagged
   `capacity_stop_endpoint_reviewable`.
2. Fewer than 60 resolved primaries, or any OPEN row: `INSUFFICIENT_EVIDENCE`.
3. **Coverage policy (R3)** `x1_coverage_policy_v1_20261017`. If either rule below fails, the verdict is
   `INSUFFICIENT_COVERAGE`:
   - paired resolved delayed controls >= 80% of resolved primaries;
   - unresolved primaries <= 20% of attempted primaries (OPEN + RESOLVED + UNRESOLVED).

   Comparisons are inclusive and integer-exact. For example, 48/60 passes and 47/60 fails; 15/75 passes and
   16/76 fails.
4. `CRITERIA_MET_REVIEW_REQUIRED` only if all of the following hold; otherwise `CRITERIA_NOT_MET`:
   - primary mean net > 0;
   - week-cluster bootstrap 95% lower bound > 0 (ISO-week clusters, 10,000 resamples, seed 20261017);
   - paired resolved delayed-control mean net > 0.

The coverage thresholds were **chosen on 17 October 2026, before any prospective block or data existed**, and are
part of the digest. Reason: the control re-quotes the same contract only five minutes later, so its quote availability
should match the primary's. If more than one in five controls or primary exits is missing, non-random capture loss
(app killed, network) could dominate both the latency comparison and the mean net. The thresholds may be vetoed
before collection starts; any change needs a new digest and must never follow observed returns. Unique
sessions and weeks, side and expiry concentration, and the exact coverage numerators and denominators are always reported.
60 observations are not 60 independent samples.

## Block, versioning and storage

- Block metadata is written on the first live poll: block id, start time, collector contract, app/brain
  version, fee contract, calendar rule and version, lot-table version and sha256, protocol digest,
  time basis, capture source, imported_records = 0.
- A journal whose schema, protocol id, digest, collector contract, fee contract or calendar rule differs
  is refused (stopped, never reset). A changed protocol must use a new protocol id and state directory
  (`files/paper_buying_x1_v2` is specific to this one).
- Caps: 800 records and 1000000 bytes. On capacity: stop, review required, no pruning.
  Calendar/fee tables cover 2026 only: 2027 sessions fail closed (CALENDAR_UNSUPPORTED /
  FEE_AUTHORITY_UNAVAILABLE) until a reviewed table extension.

## Canonical protocol (digest input)

```json
{
  "atm": "valid paired CE/PE minimizing |CE mid - PE mid|; tie -> lower strike",
  "authority": "Paper research only; quote simulation; no candidate, ranking, notification, order or label",
  "calendar_rule_version": "ev_v12_nse_cal_nf_weekly_tue_prev_session_v1",
  "cohorts": "NF and BNF reported separately",
  "collector_contract_version": "x1_collector_contract_v2_20261017",
  "contract_identity": "per leg: chain-row underlying_key == index underlying (NF NSE_INDEX|Nifty 50, BNF NSE_INDEX|Nifty Bank) == native requested_underlying_key; chain-row expiry == native requested_expiry; non-empty NSE_FO| instrument_key unique in the chain, CE != PE; trading_symbol optional (absent from the documented feed) but must match the index if present; the index is never inferred from an opaque instrument key; same instrument key required for delayed entry and exits",
  "control": "same primary contract and side; fresh ask in control window; own 30-40 min exit",
  "control_window_ist": [
    "09:25:00",
    "09:26:30"
  ],
  "coverage_policy": {
    "attempted_primary": "primary rows with status OPEN, RESOLVED or UNRESOLVED",
    "comparison": "100 x paired >= 80 x resolved_primary; 100 x unresolved <= 20 x attempted_primary",
    "frozen_before_any_data": true,
    "on_fail": "INSUFFICIENT_COVERAGE",
    "open_rows_at_review": "not allowed (review only after every OPEN row is finalized)",
    "paired_control_min_pct": 80,
    "rationale": "the control re-quotes the same contract 5 minutes later, so its quote availability should match the primary; >20% missing controls or >20% missing primary exits would let non-random capture loss dominate the latency comparison and the mean net",
    "unresolved_primary_max_pct": 20,
    "version": "x1_coverage_policy_v1_20261017"
  },
  "endpoint": "exactly 60 resolved PRIMARY observations per index; delayed controls finish",
  "entry_price": "observed ask",
  "entry_rule": "entry simulated at the entry decision at the latest valid quote (age <= 90 s, kept as entry_quote_ts_ms)",
  "entry_window_ist": [
    "09:20:00",
    "09:21:30"
  ],
  "exit_price": "observed bid, same instrument",
  "exit_rule": "exit quote receipt >= entry decision + 30 min AND exit decision <= entry decision + 40 min; exit quote age <= 90 s at its decision; first such quote wins; same for the delayed arm; otherwise UNRESOLVED EXIT_QUOTE_DEADLINE once a decision is past entry decision + 40 min",
  "exit_window_after_entry_decision_minutes": [
    30,
    40
  ],
  "expected_underlying_key": {
    "BNF": "NSE_INDEX|Nifty Bank",
    "NF": "NSE_INDEX|Nifty 50"
  },
  "expiry_roll": "SKIP",
  "fee_contract_version": "ev_v12_fee_teacher_v2_dated_20261013",
  "fees": "ev_v12_fees one_side_fee per side, one order per side, dated by session",
  "forward": "K + CE mid - PE mid",
  "gap_straddle_threshold": 0.15,
  "gap_trigger": "|F_now - F_prev| > 0.15 x current ATM straddle mid (equality skips)",
  "hold_anchor": "entry decision timestamp (the poll that simulates the entry), not the earlier quote receipt",
  "indices": [
    "NF",
    "BNF"
  ],
  "inference": {
    "ci": 0.95,
    "cluster": "iso_week",
    "resamples": 10000,
    "seed": 20261017
  },
  "lot": "contract_lot_table.resolve_contract_lot(index, session, expiry, instrument); unresolved -> SKIP",
  "lots": 1,
  "max_quote_age_ms": 90000,
  "missing_exit": "UNRESOLVED (never zero P&L)",
  "previous_reference_window_ist": [
    "15:20:00",
    "15:21:30"
  ],
  "protocol_id": "X1_gap_continuation_v2_causal_paper_20261017",
  "quote_session_ist": [
    "09:16:00",
    "15:30:00 exclusive"
  ],
  "quote_validity": "finite 0 < bid <= ask; no LTP/zero fallback; expiry >= session",
  "reference_session": "previous actual session (versioned NSE calendar)",
  "same_expiry_required": true,
  "side": "CE if gap > 0 else PE; independent of any brain verdict",
  "storage": {
    "max_records": 800,
    "max_state_bytes": 1000000,
    "on_capacity": "stop collection; review required; no pruning"
  },
  "success_review": "only with 60 resolved primaries, no OPEN rows and the coverage policy met: primary mean net > 0 AND 95% lower bound > 0 AND paired resolved delayed-control mean net > 0; otherwise INSUFFICIENT_EVIDENCE / INSUFFICIENT_COVERAGE; any integrity failure blocks; no automatic trading authority",
  "target_resolved_per_index": 60,
  "time_basis": "PHONE_FETCH_COMPLETION_MS"
}
```

## Operational appendix (not part of the digest input)

### Storage estimate (measured with the real collector on the documented-feed fixture)

| Item | Bytes (compact JSON) |
|---|---|
| Block metadata | ~810 |
| Latest reference per index (kept only for the most recent session) | ~480 total |
| Resolved primary row | ~1,310–1,320 |
| Resolved delayed-control row | ~1,160–1,170 |
| SKIP row | ~200-300 |
| Journal after one reference + one fully entered session (4 rows) | 6,649 |
| status.json (bounded, <= 16 KB) | ~3,300 |
| Per-poll bounded snapshot view (`market_forces_json.paper_buying_research`), excluding transitions | ~1,500 |

- A fully entered session adds about 5 KB; a no-trigger session adds about 1 KB (SKIP rows).
- Each index needs at least 60 trigger sessions. At an assumed ~50% trigger rate (not a forecast),
  the block runs roughly 120 sessions and uses ~350 KB. The 1 MB cap allows about 200 fully entered
  sessions, which matches the 800-record cap (4 rows per session).
- When capacity is reached, collection stops truthfully: open rows still finalize, no new rows are added, and a sticky
  `JOURNAL_SIZE_LIMIT` / `JOURNAL_FULL_REVIEW_REQUIRED` is persisted. Nothing is pruned.

### Export and recovery

- The journal lives on the phone at `files/paper_buying_x1_v2/journal.json` and is written atomically under a file lock.
  It survives app restarts and APK updates; uninstalling the app deletes it.
- ML tab, "Paper X1 buying research" card, **Export research journal**: `NativeBridge.exportPaperBuyingResearchJournal()`
  writes `Downloads/MarketRadar_X1_research_<date>_<ms>.json`. That is an `x1_export_v1` envelope with the raw journal text,
  its sha256 and the bounded status. It contains no local path. A corrupt journal is still exported for review.
- There is intentionally **no import path**. Records cannot be fabricated or replayed into the journal. A lost or
  corrupt journal is never reset in place. Recovery means: export it, review it, and start a new block (a new
  protocol id or state dir) only by an explicit reviewed change.

### Offline report

`python3 tools/x1_report.py <export-or-journal.json> [--json out.json]` runs the integrity gate first (see Success
review). If the gate passes, it reports per index: resolved, unresolved, open and skip counts by reason;
sessions and weeks; mean, median and range of net; the week-cluster bootstrap CI; delayed-control pairing; the
coverage policy version with exact numerators and denominators; side and expiry concentration; the verdict; and
`verdict_reason`. A blocked input prints `BLOCKED_INTEGRITY_FAILURE` with every failure code and exits 2.
`trading_authority` is always false, and the output is deterministic for a given journal.

### Known limitation: poll alignment

Windows are 90 s wide, and the decision time is the poll's pre-analyze timestamp. A 5-minute poll that lands
outside [09:20:00, 09:21:30] (or [09:25:00, 09:26:30] / [15:20:00, 15:21:30]) misses that arm and is recorded as a
SKIP with its reason. Field timing coverage must be reviewed after the first week. The protocol is
not widened retroactively.
