# X1 v2 — prospective Paper option-buying quote simulation (FROZEN protocol)

Protocol id: `X1_gap_continuation_v2_causal_paper_20261017`
Collector contract: `x1_collector_contract_v1_20261017`
Protocol digest (sha256 of canonical JSON: sorted keys, separators `,` `:`): **`cbbebdfddadfecdad29eb4c5a9150c72a1aff94ccd1d66e5125ff2860f355519`**
Frozen: 17 October 2026 (IST), before any observation. Status: research only; no trading authority.

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

## Success review (never automatic authority)

Per index separately: primary mean net > 0 AND week-cluster bootstrap 95% lower bound > 0 AND paired
resolved delayed-control mean net > 0. Bootstrap: ISO-week clusters, 10,000 resamples, seed 20261017.
Fewer than 60 resolved => INSUFFICIENT_EVIDENCE. Unique sessions/weeks, unresolved and missing-control
coverage, side and expiry concentration are always reported. 60 observations are not 60 independent
samples. A positive mean with badly incomplete controls is not an accepted experiment; the control
coverage threshold is NOT pre-set numerically here and must be decided at review (open item).

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
  "collector_contract_version": "x1_collector_contract_v1_20261017",
  "control": "same primary contract and side; fresh ask in control window; own 30-40 min exit",
  "control_window_ist": [
    "09:25:00",
    "09:26:30"
  ],
  "endpoint": "exactly 60 resolved PRIMARY observations per index; delayed controls finish",
  "entry_price": "observed ask",
  "entry_window_ist": [
    "09:20:00",
    "09:21:30"
  ],
  "exit_price": "observed bid, same instrument",
  "exit_window_after_entry_quote_minutes": [
    30,
    40
  ],
  "expiry_roll": "SKIP",
  "fee_contract_version": "ev_v12_fee_teacher_v2_dated_20261013",
  "fees": "ev_v12_fees one_side_fee per side, one order per side, dated by session",
  "forward": "K + CE mid - PE mid",
  "gap_straddle_threshold": 0.15,
  "gap_trigger": "|F_now - F_prev| > 0.15 x current ATM straddle mid (equality skips)",
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
  "quote_validity": "finite 0 < bid <= ask, instrument key, index-consistent symbol, expiry >= session; no LTP/zero fallback",
  "reference_session": "previous actual session (versioned NSE calendar)",
  "same_expiry_required": true,
  "side": "CE if gap > 0 else PE; independent of any brain verdict",
  "storage": {
    "max_records": 800,
    "max_state_bytes": 1000000,
    "on_capacity": "stop collection; review required; no pruning"
  },
  "success_review": "primary mean net > 0 AND 95% lower bound > 0 AND paired resolved delayed-control mean net > 0; no automatic trading authority",
  "target_resolved_per_index": 60,
  "time_basis": "PHONE_FETCH_COMPLETION_MS"
}
```

## Operational appendix (not part of the digest input)

### Storage estimate (measured with the real collector on synthetic chains)

| Item | Bytes (compact JSON) |
|---|---|
| Block metadata | ~810 |
| Latest reference per index (kept only for the most recent session) | ~540 total |
| Resolved primary row | ~1,280 |
| Resolved delayed-control row | ~1,130 |
| SKIP row | ~200-300 |
| Journal after one reference + one fully entered session (4 rows) | 6,561 |
| status.json (bounded, <= 16 KB) | ~3,300 |

- A fully entered session adds about 4.8 KB; a no-trigger session adds about 1 KB (SKIP rows).
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

`python3 tools/x1_report.py <export-or-journal.json> [--json out.json]` verifies the journal sha and that the
protocol digest matches the code. It reports per index: resolved, unresolved, open and skip counts by reason;
sessions and weeks; mean, median and range of net; the week-cluster bootstrap CI; delayed-control pairing coverage
and mean; and side and expiry concentration. Verdicts: `INSUFFICIENT_EVIDENCE`, `CRITERIA_MET_REVIEW_REQUIRED` or
`CRITERIA_NOT_MET`, always with `trading_authority: false`. The output is deterministic for a given journal (fixed seed).

### Known limitation: poll alignment

Windows are 90 s wide, and the decision time is the poll's pre-analyze timestamp. A 5-minute poll that lands
outside [09:20:00, 09:21:30] (or [09:25:00, 09:26:30] / [15:20:00, 15:21:30]) misses that arm and is recorded as a
SKIP with its reason. Field timing coverage must be reviewed after the first week. The protocol is
not widened retroactively.
