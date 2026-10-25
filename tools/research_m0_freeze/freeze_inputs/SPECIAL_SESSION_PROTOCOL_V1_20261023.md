# Special-session protocol v1 (Muhurat, Sun 8 Nov 2026): protocol text

- **Version identity:** `special_session_protocol_v1`.
- **Author:** Claude, 23 Oct 2026. Text corrected for CDX-M0-PLAN-V2 F4 (P6 no-reselect clarification).
- **This file contains no decision and no hash of itself.** It is the immutable payload. A freeze exists only as a **separate** decision record (`SPECIAL_SESSION_DECISION_RECORD_V1_20261023.json`) that names this file's full-file SHA-256, the decision, the decision maker, the actual IST decision date and time, and the Codex review reference. The record is checked by `verify_freeze_record_20261023.py`.
- **Any edit to this file creates a new version** (new file name `…_V1_1_…`, new SHA-256). A decision record naming an older SHA-256 does not freeze the edited text.
- **Scope:** the research protocol (A2/A2.1 full-day study, `fullday_research_v1`). It does not change the accepted M0 code, trading, polling, evaluation or EV v1.2.

---

## 1. Universe statement and its limitation

**Universe (unchanged, A2 §4):**
- nearest expiry as captured, per index, no roll;
- NF weekly and BNF monthly kept separate.

**What this study covers and what it does not:**

| Scope | Covered? |
|---|---|
| Entry while the contract is the nearest expiry, then hold **that exact contract** to the protocol's exit (H1/H4/H5/H5b; H6 blocked) | **Yes**, subject to source and mark availability |
| A contract's **whole life from first exchange listing**, including when it was the next or far month (for example BNF from about 3 months before expiry) | **No** |

- **Start-to-expiry under this study** means from the session after the previous expiry (about 20–24 BNF sessions; about 4–5 NF sessions) to expiry. It does not mean from listing.
- An earlier-life / next-month extension is a **separately scoped later question**. It would need second-expiry capture with `instrument_key` identity (D7), maturity matching, feasibility and storage review, and its own universe and protocol identity.
- Entries before that extension's own freeze can never become prospective. Entries after a valid new freeze can be prospective under the new identity if the evidence is collected.

## 2. Sources

| Source | Date | Use | Who verified |
|---|---|---|---|
| NSE/FAOP/68747 | 25 Jun 2025 | Current expiry rule: NIFTY weekly on Tuesday; BANKNIFTY monthly/quarterly on the last Tuesday; existing and new contracts; dated contract-master files | Codex retrieved it (23 Oct). Not reachable from Claude's session. |
| NSE BANKNIFTY product specification | (page) | Options expire on the last Tuesday, or the previous trading day if that Tuesday is a holiday; near/next/far monthly cycle plus quarterlies | Codex (23 Oct) |
| NSE/FAOP/71777 | 12 Dec 2025 | 2026 F&O holidays, including Tue 20 Oct, Tue 10 Nov, Tue 24 Nov; Muhurat on Sun 8 Nov | Claude and Codex |
| NSE/FAOP/76940 | 19 Oct 2026 | Muhurat F&O timetable. Pre-open order entry 14:05–14:10 (limit and market orders); 14:10–14:15 (limit orders only; no modification or cancellation of market orders; random closure in the last 2 minutes); pre-open matching 14:15–14:17; buffer 14:17–14:20; normal market 14:20–15:40; trade modification to 15:50; price-band update 15:15. No expiry change stated. | Claude and Codex |
| Captured live chain (`ml_option_chain_snapshots`) | per poll | Feed-observed BNF expiry 2026-10-27, first seen 30 Sep and last seen 23 Oct | Claude's query; Codex did not re-query |

## 3. Protocol clauses

**P1. Counting.**
- Horizons count regular sessions only.
- 8 Nov is never an entry session, an endpoint or a k-step.

**P2. Tagging.**
- A cohort whose [entry session, exit session] contains 8 Nov is `SPAN_SPECIAL_SESSION`.
- It is reported separately and excluded from the regime-primary estimate.
- Its regime tag is R1.

**P3. Special-session observations.** 8 Nov observations are recorded as the collector actually saw them.
- With the current driver, which does not poll on Sundays, every 8 Nov slot is `MISSING(special_session_not_polled)` and stays in its denominators.
- If a later **approved** driver captures the session, those observations are preserved and used as observations. They are never automatically marked missing.

**P4. Fixed-endpoint arms** (all frozen primaries and H1/H4/H5/H5b as defined):
- assessable if the required entry evidence and the **same-contract** exit mark exist;
- carry the spanning tag;
- reported separately.

**P5. Path-dependent arms** (any arm whose exit can be triggered by an intra-holding observation: stop, target, trailing or checkpoint rule):
- If an unobserved interval, such as 8 Nov or any MISSING slot, **could have changed the exit**, the outcome is `UNRESOLVED(path_gap:<reason>)` and **not scoreable**.
- The only exception is a method for that gap that was frozen in advance and validated for the claimed outcome.
- **Prohibited:**
  - assuming no trigger occurred;
  - substituting a later endpoint as the managed exit;
  - counting the cohort as a win, a loss or WAIT;
  - dropping it.
- It stays in the accountability and missingness denominators, and the limitation is reported.
- *The frozen primary has no intra-holding triggers. This clause guards later managed-exit arms.*

**P6. Expiry identity at entry (no reselection).**
- **The held contract is exactly the contract the decision selected:** index, expiry, strike and option type (plus `instrument_key` where it was captured). Reading the entry quote **never rolls, reselects or substitutes** a different expiry, strike or type. This clarifies A2's existing no-roll rule; it is not a new selection rule.
- The held contract's expiry is the **expiry field of that selected contract's own rows in the entry-quote chain**: feed-observed at entry, with the poll's fetch/receipt timestamps and content identity (`source_rows_sha256` / manifest hash).
- The exit session is computed **at entry** from that feed-observed expiry and the calendar version in force at entry.
- **Fail states fixed before entry:**
  - the selected contract's rows are absent from the entry-quote chain ⇒ `ENTRY_UNAVAILABLE(selected_contract_absent)`; no other contract is substituted;
  - the selected legs' expiry field is empty, unparseable or inconsistent across the legs ⇒ `ENTRY_UNAVAILABLE(expiry_unobserved)`;
  - the calendar does not cover entry → expiry ⇒ `HORIZON_UNRESOLVED(calendar_authority)`.
- If the feed-observed expiry differs from the planning value (rule-derived), **the feed-observed value governs**, because it is the contract held. The difference is flagged `PLAN_MISMATCH`.
- If the exchange later changes the calendar or expiry after entry, the cohort is tagged `CALENDAR_CHANGED_AFTER_ENTRY` and reported separately. Its exit is the originally scheduled same-contract mark if that session exists; otherwise `EXIT_UNRESOLVED(calendar_changed)`.
- **A contract-master extract** obtained later is **verification only**, recorded with its own date and availability time. It is never presented as entry-time evidence.

**P7. Expiry and settlement.**
- 8 Nov is not an expiry day (FAOP76940 states no change).
- H6 stays BLOCKED.
- 8 Nov settlement obligations do not affect research holds.

**P8. NF that week.** With the rule-derived NF expiry of Mon 9 Nov (because Tue 10 Nov is a holiday):
- NF H1/H4/H5 spanning 8 Nov are `HORIZON_INELIGIBLE` (the exit lands on expiry day);
- NF H5b entries on 4–6 Nov are eligible and spanning.
- If the feed-observed NF expiry differs, P6 governs and the eligibility is recomputed **at entry**.

**P9. Prospective status (date-only, accepted code).**
- `PROSPECTIVE` iff the decision record's IST decision date for **this file's SHA-256** is strictly before the cohort's entry IST date. Otherwise `RULE_POST_HOC`.
- No backdating. The decision date is the actual date of decision, never a pre-filled or target date.
- The practical target "18:00 IST" is a planning margin, not part of the rule.

**P10. Collection** may proceed regardless of freeze status. Earlier observations are never called untouched prospective validation.

## 4. Horizon / deadline table (rule-based planning arithmetic; `freeze_calc_20261023.py` v2, reproduced by Codex)

| Index / horizon | First spanning entry (IST) | Exit in the calculation | Decision date must be on or before |
|---|---|---|---|
| **BNF H5** | **Wed 28 Oct, 14:30** | Fri 20 Nov, 15:05 | **Tue 27 Oct** |
| **BNF H5b** | **Wed 28 Oct, 14:30** | Mon 23 Nov, 15:05 | **Tue 27 Oct** |
| BNF H4 k=5 | Mon 2 Nov, 15:05 | Mon 9 Nov | 1 Nov |
| NF H5b | Wed 4 Nov, 14:30 | Mon 9 Nov, 15:05 | 3 Nov |
| BNF H4 k=2 | Thu 5 Nov, 15:05 | Mon 9 Nov | 4 Nov |
| BNF H1 | Fri 6 Nov, 15:05 | Mon 9 Nov | 5 Nov |
| NF H1, H4, H5 | none eligible across 8 Nov (assumed 9 Nov NF expiry) | — | — |
| H0, H2 | never span | — | — |
| H6 | BLOCKED | — | — |

**Assumptions behind the table:**
- The 28 Oct entries assume the Nov BNF contract becomes front after the captured 27 Oct expiry.
- November exits depend on the actual Nov expiry (rule-derived 23 Nov). Under P6 they are fixed at entry from the feed-observed value.
- The 27 Oct date is a **rule** deadline. It is not a promise that collection will be ready for 28 Oct entries (CDX-M0-PLAN-V2 §8).

## 5. Expiry labels (unchanged until evidence arrives)

| Contract | Planning value | Label |
|---|---|---|
| BNF Oct | 27 Oct | Feed-observed (captured chain) |
| BNF Nov | 23 Nov | Rule-derived; contract-level unconfirmed |
| BNF Dec | 29 Dec | Rule-derived; contract-level unconfirmed |
| NF, week of 8 Nov | 9 Nov | Rule-derived; contract-level unconfirmed |

## 6. What a freeze of this text does not do

- It does not make an unusable collector usable.
- It does not supply missing causal entry data or preservation.
- It does not approve installation, release or migration.
