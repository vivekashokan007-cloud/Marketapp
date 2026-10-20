# F1 — 2027 coverage with protocol continuity (design + readiness tests, b511, 2026-10-18)

**Status:** design and fail-closed readiness tests only. No 2027 holiday, fee or lot data was added.
**Source dependency (blocking the data step):** the official NSE F&O trading-holiday circular for
calendar year 2027, plus any NSE/SEBI/stamp-duty rate notice effective on or after 2027-01-01.
Until both exist, every 2027 path below fails closed, which the handoff accepts.

## 1. Authority inventory (current main, 7037f44)

| Authority | Location | 2027 behaviour today |
|---|---|---|
| Python EV calendar | `ev_v12_calendar.py` `NSE_HOLIDAYS_2026`, `COVERED_YEARS=(2026,)`, `version = RULE + ":2026"` | `CalendarUnavailable("CALENDAR_YEAR_NOT_COVERED")` (it is also raised from next_session on 2026-12-31, because that walk enters 2027) |
| Brain holidays | `brain.py` `_CONST['NSE_HOLIDAYS']` (uses at ~2708 and ~7060); DTE coverage via `contract_lot_table.calendar_coverage_for_interval` | interval 2026-12-29→2027-01-05: `coverage_ok=False`, `missing_years=['2027']`, basis `nse_trading_calendar_incomplete` |
| Native Rule-B exit policy | `RuleBExitPolicy.kt:49` `if (d.year != 2026) throw CalendarUnavailable` | rejects every 2027 date |
| SQL receiver | `ev_private.is_nse_session` (migration 20261013150000, line 197): `NULL` outside 2026; `sessions_after` propagates NULL | production returned NULL for 2027-01-04; receipts fail closed |
| Calibration publication | `ev_v12_calibration.validate_publication`: `pub.calendar_version != cal.version` → `NO_CALIBRATION_CALENDAR_MISMATCH`; `observation_identity` binds `calendar_version` | an extension changes `cal.version` and starves every existing publication |
| Fees | `ev_v12_fees.FEE_SUPPORTED_TO = 2026-12-31` | `FeeUnavailable("FEE_DATE_OUTSIDE_RATE_TABLE")` |
| Lot sizes | `contract_lot_table` window `{start: 2024-11-20, end: None, fail_closed_outside: True}` | 2027 expiries resolve from the current circular rules (NIFTY 65), with no end date. Correct until NSE issues a revision circular; then a dated rule must be added before that circular's effective expiry. Missing package → `lot_unavailable` (fails closed) |
| X1 journal | `paper_buying_research._check_state` binds `calendar_rule_version` (the rule string), not `cal.version` | an existing journal survives a data-only extension without reset (tested) |

## 2. Frozen identities (asserted by `test_b511_f1_year_boundary_readiness.FrozenIdentities`)

| Constant | Value |
|---|---|
| X1 DIGEST | `10a6b086f1ceefbccd6d6f8a87dacda5eca29818b1c2a999c649bbdb6dad0daa` (PROTOCOL does not contain `cal.version` or the covered-year list) |
| FEE_CONTRACT_VERSION | `ev_v12_fee_teacher_v2_dated_20261013` |
| CALENDAR_RULE_VERSION | `ev_v12_nse_cal_nf_weekly_tue_prev_session_v1` |

A data-only extension changes **only** the holiday tuple, `COVERED_YEARS` and therefore `cal.version`
(`…_v1:2026,2027`). If NSE changes the expiry weekday or the holiday-adjustment rule, that is a new
rule string and goes to a new scope/protocol decision, not this plan.
FEE_CONTRACT_VERSION already describes a dated rate table. Adding a 2027 row, backed by a source,
extends `FEE_SUPPORTED_TO` and `rates_for` without changing the string, because the per-trade
`fee_identity` records `rate_date` and the per-rate versions. A rate whose source is unknown never gets a row.

## 3. Compatibility design

1. **One source of truth, four renderings.** Generate the Python tuple, the Kotlin set, the `brain._CONST` list
   and the SQL array from one reviewed JSON (`calendar/nse_fo_holidays.json`, carrying a circular id and
   sha256). A parity test fails if any rendering drifts. Today's parity is pinned by text (the Kotlin test in
   the new file, plus the existing Python/brain equality tests).
2. **Calibration: accept by covered-subset, not by string equality.** Replace the exact comparison with
   `rule(pub) == rule(cal)` **and** `years(pub) ⊆ years(cal)` **and** `holidays(cal) restricted to years(pub)` hashing
   equal to the publication's recorded holiday digest. Publication schema v2 adds `calendar_rule_version`,
   `calendar_years`, and `calendar_holiday_sha256_by_year`. A v1 publication (no per-year digest) is accepted only when
   `pub.calendar_version` equals the frozen `RULE:2026` and the receiver's 2026 holidays hash to the frozen
   2026 digest. Historical publication bytes and hashes are never rewritten. The builder writes new
   envelopes with truthful new hashes.
3. **Rollout without starvation:** (a) ship the subset-aware receiver first (it still accepts v1); (b) ship the
   2027 data to clients and the SQL function; (c) the builder publishes v2 under the extended calendar; (d) only after two
   finalized expiries, retire v1 acceptance. Each step is independently revertible. A receiver that is
   reverted to exact-match simply reports `NO_CALIBRATION_CALENDAR_MISMATCH`, which fails closed, as WAIT.
4. **No duplicate assimilation.** `observation_identity` binds `calendar_version`. Re-deriving an old observation under
   the extended calendar would create a second identity. The builder therefore deduplicates on
   `(observation natural key, calendar_rule_version)` and keeps the first-assimilated identity; the extension
   never re-derives finalized observations. Tested: the identity differs under the two versions, which shows the
   dedup key must not include the covered-year list.
5. **Cross-year mechanics** are already correct in the rule code (tested with SYNTHETIC 2027 holidays only):
   previous/next session across 31 Dec, a Tuesday expiry moved into the prior year, and trading seconds across the boundary.
6. **Fees and lots:** a date outside the dated rate table, or an expiry beyond a lot rule's end, fails closed with a
   named reason. A NSE lot revision circular is added as a dated rule (`expiry_on_or_after`) before
   its effective expiry. That is the only lot work 2027 needs.
7. **Historical verifiability:** decisions and outcomes keep their stored `calendar_version` and fee identity.
   Verification recomputes with the calendar named in the row, which needs a versioned calendar registry
   (`RULE:2026` stays loadable forever).

## 4. Tests added (`tests/test_b511_f1_year_boundary_readiness.py`, 11)

Frozen strings and digest independence; calendar, fee and DTE coverage fail closed for 2027; the Kotlin
Rule-B 2026-only guard and holiday parity; cross-year mechanics on a SYNTHETIC calendar; X1 journal
continuity across an extension, with a rule-string change still stopping; calibration starvation characterised
(`OK` under 2026 → `NO_CALIBRATION_CALENDAR_MISMATCH` after extension: the gap that §3.2 closes); observation
identity binding; open-ended lot authority with fail-closed behaviour when the table is missing.

Not feasible without data: SQL-side parity for 2027 and a receiver acceptance end-to-end test. Both need the
real holiday list and a scratch migration. The subset-aware receiver is a design decision for review
before code, because it changes what `validate_publication` accepts.
