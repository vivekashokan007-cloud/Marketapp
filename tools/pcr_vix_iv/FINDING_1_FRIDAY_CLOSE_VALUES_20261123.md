# Finding 1 — the two Friday spread profits are not supported by their own closing bundles

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Assignment:** `CLAUDE_CODE_PCR_VIX_IV_GREEKS_HYPOTHESIS_20261123`, "Evidence to reproduce first"
**Status:** reproduced and resolved from the saved records. **Do not use trades 318/319 as
verified performance labels.** One sub-question (the provenance of the saved exit premium) is
still open and is named below.

## Pinned inputs

| | |
| --- | --- |
| Native | `8f9aa234a278528a09d58cd83a6aaa85254e2218` (2026-11-23T08:01:52+05:30, author Codex) |
| versionName / versionCode | `2.6.104` / `535` |
| MarketVivi pin | `cbb65bda0642a421865261a36dc62aca06e2e1d4` |
| `app.js` sha256 | `2d513a8f883ac60f8535516841877f252d4342c3871561ffc11db1db22a4fc64` |
| `pwa_identity` | `{version 2.6.104, build 535, app_js_v 1399, index_app_js_v 1399}` |
| Supabase | `fdynxkfxohbnlvayouje`, read-only |

Note: the installed build does **not** descend from `cb6ec6a` (my pin-rotation fix). Codex fixed
the same stale tripwire independently in `fcd2b16`, with identical values, so
`candidate-build/2.6.104-b535-strong-paper-pinfix` is redundant and can be deleted.

## What the saved rows actually contain

`buy_ltp` and `sell_ltp` are **entry** leg prices, not closing ones. They reconstruct the entry
debit exactly on both trades, so there is no ambiguity about which side of the trade they describe:

| id | index | buy_ltp − sell_ltp | entry_premium |
| --- | --- | --- | --- |
| 318 | BNF | 450.05 − 276.85 = **173.20** | 173.20 |
| 319 | NF | 133.50 − 71.35 = **62.15** | 62.15 |

The closing bundle is `exit_snapshot.close_leg_quotes`, and it is internally coherent as a quote
set — each leg carries bid/ask/ltp, and the stored `spread_evidence_points` equal the two legs'
bid-ask widths exactly (BNF `[21.65, 16.1]`, NF `[0.9, 1.2]`).

| id | long leg bid | short leg ask | executable close = long bid − short ask |
| --- | --- | --- | --- |
| 318 | 501.00 | 328.00 | **173.00** |
| 319 | 130.50 | 65.75 | **64.75** |

These are the values Codex cited. They are the correct way to value closing a long debit spread:
sell the long at the bid, buy the short back at the ask.

## The contradiction, inside one saved bundle

The saved `exit_premium` does not equal the executable close:

| id | executable close | saved exit_premium | gap (points) |
| --- | --- | --- | --- |
| 318 | 173.00 | 242.50 | **+69.50** |
| 319 | 64.75 | 109.75 | **+45.00** |

**The fee engine and the P&L engine disagree with each other about the same close.** The friction
breakdown's own `close_turnover` is reproduced exactly by the executable quotes and by nothing
else:

| id | stored `close_turnover` | (long bid + short ask) × lot |
| --- | --- | --- |
| 318 | 24,870.00 | (501.00 + 328.00) × 30 = **24,870.00** |
| 319 | 12,756.25 | (130.50 + 65.75) × 65 = **12,756.25** |

`entry_turnover` reconciles the same way (BNF 726.90 × 30 = 21,807; NF 204.85 × 65 = 13,315.25).
So within one record the friction model priced the close at the executable quotes while the P&L
priced it at something ~45–70 points better.

## What the result becomes on the saved executable quotes

| id | index | entry | executable close | gross | friction | **net** | booked net |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 318 | BNF | 173.20 | 173.00 | −₹6.00 | ₹127.17 | **−₹133.17** | +₹1,951.83 |
| 319 | NF | 62.15 | 64.75 | +₹169.00 | ₹112.70 | **+₹56.30** | +₹2,981.30 |
| | | | | | | **−₹76.87** | **+₹4,933.13** |

The two Friday spreads together are approximately **flat-to-slightly-negative after costs**, not
+₹4,933. The gap is about ₹5,010.

## A second, independent inconsistency

For NF 319 the booked gross (**₹3,094**) exceeds its own recorded peak (**₹267**) by more than
11×. A close cannot beat the maximum mark-to-market unless the peak series and the close were
computed from different inputs.

Both trades have `close_polls_json = null`, `journey_stats.poll_count = 0` and
`journey_stats.timeline = []`, yet carry `peak_pnl` 3,776 (BNF) and 267 (NF) with
`peak_pnl_validity: "valid"`. **There is no retained poll series behind either extremum**, so
neither peak nor drawdown is usable as evidence for these two trades, and `"valid"` overstates
what is there.

`close_trace_json.source = "manual_close"`, `exit_reason = "manual_book_profit_button"`,
`capture_phase = "POSITION_P1"` on both — the operator initiated the closes, which the assignment
asks be retained as attribution. That attribution is unaffected by this finding: the operator
pressed the button, and the number the app then showed and stored was wrong.

## Still open

Where `242.50` and `109.75` come from is **not** established. They are not reproduced by any
combination of the stored closing quotes:

| candidate | 318 | 319 |
| --- | --- | --- |
| long bid − short ask | 173.00 | 64.75 |
| long bid − short bid | 194.65 | 65.65 |
| long ask − short ask | 189.10 | 65.95 |
| long ltp − short ltp | 196.00 | 66.25 |
| mid − mid | 191.88 | 65.80 |
| **saved exit_premium** | **242.50** | **109.75** |

Solving for the implied short-leg price with the stored long bid gives 258.50 (BNF) and 20.75
(NF), against stored short asks of 328.00 and 65.75. That is consistent with the short leg having
been valued from a **different contract or a different poll** than the one stored beside it, but
the records retained on these two trades cannot confirm it: there is no poll series, and
`close_polls_json` is null. Resolving it needs the phone-side request bundle for the two
09:17 IST closes, which is the one piece of missing evidence I would ask for.

A further oddity to carry into that request, not yet explained: both long legs are quoted well
**below intrinsic** against their own stored `exit_spot` (BNF 64200CE bid 501.00 against 734.40
intrinsic at spot 64,934.40; NF 25700CE bid 130.50 against 215.40 at 25,915.40). A call cannot
trade below intrinsic, so either `exit_spot` or the leg quotes in that bundle are from a different
moment. This does not change the conclusion above — the fee engine, the turnover and the quote
spreads all agree the executable pair was 501.00/328.00 and 130.50/65.75 — but it means the
bundle's `exit_spot` should not be trusted for Greek or IV reconstruction on these two trades.

## Consequence for the rest of the assignment

1. Trades 318 and 319 are **excluded from validation labels** and kept as a diagnostic case with
   an explicit `CLOSE_VALUE_UNRESOLVED` label. Original records are preserved; nothing is edited.
2. The headline "Friday booked +₹4,933" must not be repeated as realised performance. On the
   retained executable quotes it is approximately **−₹77**.
3. This is an operational pricing/recording defect, not a model defect, and belongs in the
   implementation proposal separately from any PCR/VIX/IV model change — as the assignment
   requires.
4. The retained Friday tick gains quoted in the assignment (+30/+60/+120 min of 250.25/97.50/396.50
   for NF and 417/510/841.50 for BNF) are a **different** measurement from the close and are not
   challenged by this finding; they are also not substitutes for the closing bundle.
