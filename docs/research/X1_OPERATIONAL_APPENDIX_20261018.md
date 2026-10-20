# X1 v2 — operational appendix (b511, 2026-10-18)

This appendix sits **outside** the frozen protocol. It changes no rule; the protocol document
(`X1_gap_continuation_v2_causal_paper_20261017.md`, guarded by `tools/x1_protocol_doc.py --check`) and
`DIGEST = 10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa` are unchanged.

## A. Cadence limitation (recorded, not a rule change)

The 30–40 minute exit rule is unchanged. With the regular 5-minute poll cadence, an entry decision taken just
after a poll boundary (e.g. 09:20:02, a 2 s lag) is still OPEN at the 09:50:00 quote: its hold is 29 min 58 s,
which is below 30 min. The first eligible quote is 09:55:00, giving a hold of 34.9667 min (Codex measurement).
So realised holds cluster near 35 min, not across the full 30–40 band. If one poll is missed, the next quote can
fall after 40 min, and the arm becomes UNRESOLVED; it is not stretched. The report must describe holds as
"cadence-quantised" and must not read the 30–40 band as uniformly sampled.

## B. Historical P&L estimates

Claude's historical P&L estimates for gap continuation are **unverified** until a reproduction package
exists: input data with byte hashes, the exact script, the environment, and outputs that reproduce to the stated tolerance.
They must not appear in any report as verified, or as a prior for the endpoint.

## C. Week-one report (counts only)

Per date × index (NF, BNF separately):
- primary arm: status (OPEN/RESOLVED/UNRESOLVED/SKIP) and reason;
- delayed control arm: status and reason;
- per arm, `previous_session_reference_used`: the PREVIOUS session's reference consumed by that row:
  - `RETAINED`, with its session and quote timestamp;
  - `USED_NOT_RETAINED` (GAP_BELOW_THRESHOLD / EXPIRY_ROLL rows do not store it);
  - `NONE_RECORDED`.

  This is not today's capture.
- per date × index, `reference_capture`: the 15:20 capture result from retained evidence only, giving status,
  session and timestamps (never prices, strikes or keys). Evidence comes from `journal.references`
  (`JOURNAL_REFERENCE`), a next-session row that consumed it (`CONSUMED_BY_NEXT_SESSION_ROW`), or a
  reason-coded attempt (`JOURNAL_ATTEMPT`).
  - The journal keeps only the latest reference and attempt per index, so overwritten history is
    `UNKNOWN` / `NOT_RETAINED` with `cross_check_required=true`.
  - For those dates the bounded snapshot `reference_capture` cross-check is **required**.
  - A reference from another session is never assigned to a date.
  - Dates with reference evidence but no arm row are still listed.
- `entry_decision_ts_ms`, `entry_quote_ts_ms`, `exit_quote_ts_ms`, `exit_decision_ts_ms`;
- `hold_ms_from_entry_decision`;
- failure counts by class: engine, staleness, window, identity (plus data/authority).

Week-one outputs include no premium, prices, gross, net, fees, P&L, means, confidence intervals, bootstrap,
endpoint flag or statistical verdict.

**Procedure:**
1. Ask Vivek for the on-device journal export (`NativeBridge.exportPaperBuyingResearchJournal`, schema
   `x1_export_v1`) after the Friday close. Record the sha256 of the received bytes before parsing.
2. Run the counts-only path: `python3 tools/x1_week1_counts.py EXPORT.json --json WEEK1_COUNTS.json`.
   - It applies the same blocking integrity checks as the endpoint reporter: export hash, schema,
     protocol digest, block contract, record validity and pairing.
   - It emits only an explicit field allowlist and refuses to emit any prohibited field.
   - Exit code 2 means an integrity failure; only integrity codes and the source hash are then emitted.
3. Optionally cross-check per-poll status with a bounded read of `market_forces_json.paper_buying_research`
   (BEGIN READ ONLY, statement timeout, date-bounded, no secret-bearing `app_config` keys).

**Do not run `tools/x1_report.py` in week one.** It is the pre-registered endpoint reporter, and before the
60-result endpoint it still prints running means and confidence intervals. It is reserved for the endpoint.
**Never reset the journal.** Journal contents stay out of review ZIPs unless explicitly requested.
Verified by `tests/test_b511_x1_week1_counts.py` on a synthetic pre-endpoint export (stdout, JSON and saved file).

## D. Telemetry key compatibility

`candle_paper_policy` stays as emitted. A future, more general `candle_policy` key must be added alongside it,
with `candle_paper_policy` kept as an alias carrying the same value, so consumers and stored rows that read the old key keep working.
