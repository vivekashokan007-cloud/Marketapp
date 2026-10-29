# X1 dated fee identity and EV v1.2 calendar compatibility: design note (DESIGN ONLY)

Date: 29 Oct 2026 IST. Author: Grok (implementer), for Codex/Claude review. Base: Marketapp `main` `8e709bb`.
Scope: Codex 2026-10-29 task B3. **Nothing in this note is implemented.** No 2027 holiday, rate or lot data are
added. No shared fee or calendar behaviour, authority horizon, APK or protocol migration is part of this batch.

## 1. What X1 reads today (verified in code)

| Reader | Fee/calendar identity it checks | Source |
|---|---|---|
| `paper_buying_research._check_state` (every poll) | `block.fee_contract_version == FEE_CONTRACT_VERSION`, `block.calendar_rule_version == CALENDAR_RULE_VERSION`, `protocol_digest == DIGEST` | `paper_buying_research.py` L347 |
| X1 entry (primary) | computes `entry_fee = one_side_fee(..., session)` and stores `fee_identity(session)` per row | L554-567 |
| X1 control (delayed) | recomputes `entry_fee` for its own quote on the same session, **inherits** the primary's `fee_identity`, lot and rule id | L610-620 |
| X1 exit/outcome | `exit_fee = one_side_fee(leg.bid, 0, 1, units, session)` on the same session; no separate exit fee identity is stored | L477-486 |
| `tools/x1_report.validate` (replay/report) | journal schema, protocol id/digest, block contract strings; recomputes `gross`/`net` consistency; **does not read `fee_identity` at all** and tolerates unknown row keys | `tools/x1_report.py` L92-185 |
| Status view (`_compact_row`) | carries no fee or lot identity | L380 |

Synthetic tests added in this branch (`test_x1_fee_identity_compat.py`) pin these facts:
- every 2026 date reproduces a pinned digest of `fee_identity` + entry/exit fees (`15778a8d…123e`);
- changing SEBI, stamp, GST, IPFT or brokerage changes the fee but **not** `fee_identity()` (identity gap);
- extra `fee_identity` keys pass `_check_state` and `x1_report.validate` unchanged; the digest is unchanged;
- `x1_report` also accepts rows with **no** `fee_identity` (identity is recorded, never verified);
- a `FEE_CONTRACT_VERSION` bump stops the journal with `BLOCK_CONTRACT_MISMATCH`.

## 2. Proposed dated fee identity (for review; not applied)

1. Keep `FEE_CONTRACT_VERSION` byte-identical **only if** the change is a genuine data-only extension under the
   existing contract's own semantics ("dated rate table, extend only with a reviewed rate-table entry"). A changed
   formula, component set or rounding is a rule change and needs a protocol migration, not a preserved string.
2. Give every component its own dated version key, added to `fee_identity()` output for NEW rows only:
   `brokerage_version`, `gst_rate_version`, `stamp_rate_version`, `sebi_rate_version`, `ipft_rate_version`
   (2026 values named e.g. `sebi_2026_base`). Existing `exchange_rate_version` / `stt_rate_version` unchanged.
3. Historical rows are immutable: no rewrite or re-keying of existing journal rows; readers must accept rows with
   the old 5-key identity. The 2026 regression digest above must stay byte-identical.
4. `x1_report` should (separately reviewed) verify that each RESOLVED row's `fee_identity` is either the legacy
   5-key form or the extended form, and that its `rate_date == session_date`; today it does not check at all.
5. 2027 rates enter only with dated evidence for **each** component (exchange, STT, SEBI, stamp, GST, IPFT,
   brokerage assumption), including evidence that unchanged components are unchanged.

Open question for Codex: whether growing the row `fee_identity` object is acceptable under the frozen X1
protocol (keys are outside the digest; readers tolerate them, as tested). Not decided here.

## 3. EV v1.2 calendar compatibility (option A, design only)

Today `ev_v12_calibration.validate_publication` requires `pub.calendar_version == cal.version` exactly, so any
covered-year extension makes every existing publication `NO_CALIBRATION_CALENDAR_MISMATCH` (pinned by
`test_b511_f1_year_boundary_readiness`). `observation_identity` binds `calendar_version`.

Option A (compatibility) would accept a publication iff:
- `CALENDAR_RULE_VERSION` is equal; and
- the holiday set, special sessions, expiry rule and time rules are identical over **every year and interval the
  publication covers** (verified from the full historical semantics, not from the version string); and
- observation identities stay immutable (computed under the original `calendar_version`); new observations are
  never re-keyed onto old ones; conflicting historical data are rejected, not merged.

Option B (rebuild at cutover) remains available. Whether EV v1.2's own protocol identity binds the calendar
version must be checked before choosing. X1 is unaffected (it binds only the rule string and pauses safely with
`CALENDAR_UNSUPPORTED` from the first 2027 date). X1's safe pause is not a deadline promise for other calendar
consumers (`nf_front_expiry` refuses from 30 Dec 2026; `RuleBExitPolicy.kt` is 2026-only).

## 4. Explicitly not done

No 2027 holiday/rate/lot data; no change to `ev_v12_fees`, `ev_v12_calendar`, `contract_lot_table`,
`RuleBExitPolicy.kt`, X1 protocol or journal; no app-wide lot authority horizon; no APK.
