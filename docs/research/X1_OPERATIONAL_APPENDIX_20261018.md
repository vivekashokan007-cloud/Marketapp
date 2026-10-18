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

## C. Week-one report (counts only, no P&L)

Per date × index (NF, BNF separately):
- primary arm: status (OPEN/RESOLVED/UNRESOLVED/SKIP) and reason;
- delayed control arm: status and reason;
- reference capture: previous-session reference present, capture timestamp, source;
- `entry_decision_ts_ms`, `exit_quote_ts_ms`, `exit_decision_ts_ms`;
- `hold_ms_from_entry_decision`;
- failure counts by class: engine, staleness, window, identity.

No premium, P&L, return, bootstrap or endpoint verdict appears in week one.

**Sources:** a bounded read of `market_forces_json.paper_buying_research` (BEGIN READ ONLY, statement timeout,
date-bounded, no secret-bearing `app_config` keys), plus the on-device journal export
(`NativeBridge.exportPaperBuyingResearchJournal`, schema `x1_export_v1`). Ask Vivek for the export after the Friday
close, and record the sha256 of the received bytes before parsing. Run the report with
`python3 tools/x1_report.py EXPORT.json --json OUT.json`. **Never reset the journal**; journal contents stay
out of review ZIPs unless explicitly requested.

## D. Telemetry key compatibility

`candle_paper_policy` stays as emitted. A future, more general `candle_policy` key must be added alongside it,
with `candle_paper_policy` kept as an alias carrying the same value, so consumers and stored rows that read the old key keep working.
