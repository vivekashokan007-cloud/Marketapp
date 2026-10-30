# CAL-02 — calendar coverage data proposal (data-only, DO NOT MERGE draft)

Status: PROPOSAL. No holiday or session dates are added by this document or by Delivery A.

## Current state (Delivery A)
- `calendar_coverage_v1` evaluates coverage **per contract** (session date and the contract expiry)
  against `ev_v12_calendar.COVERED_YEARS = (2026,)`. There is no hardcoded 30-Dec cutoff.
- A contract whose session or expiry falls outside a covered year returns
  `CALENDAR_UNAVAILABLE` with the reason (e.g. `CALENDAR_YEAR_NOT_COVERED:2027`), and the index shows
  UNAVAILABLE — the scale (n sessions to expiry) is never computed from guessed dates.
- Practical effect: from late Dec 2026, any contract expiring in 2027 (BNF monthly first) becomes
  CALENDAR_UNAVAILABLE until 2027 data is added.

## Data needed (owner / exchange-sourced only)
1. NSE 2027 trading-holiday list for the F&O segment (official NSE circular), with circular number and date.
2. Any special/muhurat session dates for 2027 and their session hours (official circular).
3. Confirmation of 2027 expiry-day conventions for NIFTY (weekly) and BANKNIFTY (monthly)
   and the holiday-shift rule (previous trading day) — from the NSE contract-specification circular.

## Proposed change once data is supplied (separate PR)
- Add the 2027 list to `ev_v12_calendar` as data, include `2027` in `COVERED_YEARS`, and record the
  circular reference + sha256 of the source PDF in the PR.
- Tests: every 2027 expiry produced by the rule is a session; coverage flips from
  CALENDAR_UNAVAILABLE to COVERED only for contracts fully inside covered years.
- No parameter changes; scale method unchanged.

## Explicitly not done
- No 2027 holidays were inferred, copied from unofficial sources, or invented.
