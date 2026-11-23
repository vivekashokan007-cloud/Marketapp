# Test 0 checkpoint — baseline freeze and evidence join

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Assignment:** `CODEX_HANDOFF_CLAUDE_CODE_SEQUENTIAL_FACTOR_TESTS_20261123`, Test 0
**Result:** **Case 1 PASSED** (reconciled). **Case 2 partially joined.** **Case 3 BLOCKED** — the
record is not in the database.

You were right that neither of my two earlier estimates established the actual close. The
position-tick path does, and it says something different from both.

## Pinned baseline

| | |
| --- | --- |
| Marketapp | `8f9aa234a278528a09d58cd83a6aaa85254e2218` (2026-11-23T08:01:52+05:30) |
| MarketVivi | `cbb65bda0642a421865261a36dc62aca06e2e1d4` |
| versionName / versionCode | `2.6.104` / `535`, `app_js_v` 1399 |
| `app.js` sha256 | `2d513a8f883ac60f8535516841877f252d4342c3871561ffc11db1db22a4fc64` |
| Supabase | `fdynxkfxohbnlvayouje`, read-only |
| Extraction | 2026-11-23, this session; queries in `queries_20261123.sql` |

Lot authority, taken from the tick records rather than assumed: `contract_lot_size` 30 (BNF) and
65 (NF), `lot_authoritative: true`, `policy_trace_json.lot_as_of: "2026-11-20"` — i.e. the lot
rule in force is dated to Friday, not to the close session.

---

## Case 1 — trades 318/319: RECONCILED, and my earlier numbers were both wrong

### The decisive record

`position_ticks` carries a tick at **09:17:10.312 IST** for both trades, 1.6 s before the BNF
close (09:17:11.888) and 6.5 s before the NF close (09:17:16.816). It holds **per-leg executable
quotes with instrument keys and source timestamps**, and `mark_trust.state = "TRUSTED"`,
`basis GROSS_EXECUTABLE`, `source_validity OK`, `key_match EXACT` on every leg.

| trade | leg | instrument | bid | ask | executable |
| --- | --- | --- | ---: | ---: | ---: |
| 318 | LONG 64200 CE | `NSE_FO\|62656` | 711.60 | 720.65 | 711.60 |
| 318 | SHORT 64600 CE | `NSE_FO\|62668` | 457.40 | 460.95 | 460.95 |
| | | | | | **250.65** |
| 319 | LONG 25700 CE | `NSE_FO\|64481` | 240.00 | 240.60 | 240.00 |
| 319 | SHORT 25850 CE | `NSE_FO\|64491` | 128.40 | 128.65 | 128.65 |
| | | | | | **111.35** |

`executable_mark` on the tick equals the long bid minus the short ask exactly, so the convention
is the one you specified (long out at bid, short back at ask).

### Against the saved close

| trade | tick executable (09:17:10) | saved exit_premium (09:17:11/16) | difference |
| --- | ---: | ---: | ---: |
| 318 | 250.65 | 242.50 | −8.15 |
| 319 | 111.35 | 109.75 | −1.60 |

**The saved close is slightly conservative against the tick taken seconds earlier, not inflated.**
A fresh at-tap quote a few seconds later explains a move of this size, and the booked P&L is
therefore substantially correct.

### Why both of my earlier numbers were wrong

Both used `ml_option_chain_snapshots`, and **those opening rows are broken**. The test is
no-arbitrage against the bundle's own `exit_spot` of 64,934.40 (BNF) and 25,915.40 (NF):

| source | BNF 64200 CE bid | intrinsic | NF 25700 CE bid | intrinsic | verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| chain 09:15:02 | 501.00 | 734.40 | 130.50 | 215.40 | **233 / 85 below intrinsic — impossible** |
| chain 09:20:06 | 506.90 | 734.40 | 193.65 | 215.40 | **still below intrinsic** |
| tick 09:17:10 | 711.60 | 734.40 | 240.00 | 215.40 | coherent |

So my "−₹77" (09:15 chain) and "+₹2,197" (09:20 chain) are **both withdrawn**. I twice anchored on
chain snapshots without checking them against intrinsic, which is the check that disqualifies
them. The 09:20 poll is not a "first clean poll"; it is also degraded.

### Reconciled result

| trade | entry | close used by P&L | gross | friction | net |
| --- | ---: | ---: | ---: | ---: | ---: |
| 318 BNF | 173.20 | 242.50 | ₹2,079.00 | ₹127.17 | **+₹1,951.83** |
| 319 NF | 62.15 | 109.75 | ₹3,094.00 | ₹112.70 | **+₹2,981.30** |
| | | | | | **+₹4,933.13** |

Marked at the 09:17:10 tick instead, the pair would be ₹2,323.50 + ₹3,198.00 gross, i.e. about
**₹350 better** than booked. The booked figures stand as a conservative, defensible record of the
close.

### But the fee bundle is stale — this is the real defect

`exit_snapshot.close_leg_quotes` reproduces the **09:15:02 chain poll exactly on all four legs**,
and the friction engine's `close_turnover` is computed from it:

| trade | stored close_turnover | from the 09:15 bundle | from the 09:17:10 tick |
| --- | ---: | ---: | ---: |
| 318 | 24,870.00 | (501.00 + 328.00) × 30 = 24,870.00 | (711.60 + 460.95) × 30 = **35,176.50** |
| 319 | 12,756.25 | (130.50 + 65.75) × 65 = 12,756.25 | (240.00 + 128.65) × 65 = **23,962.25** |

**P&L used a fresh quote; fees used a stale, sub-intrinsic chain print.** BNF close turnover is
understated by about 41%, NF by about 88%, so the recorded friction is too low. The direction of
the error is small in rupees here (STT and exchange charges scale with turnover) but the
mechanism is systematic.

### Extrema do not reconcile either

| trade | `trades_v2.peak_pnl` | tick `running_mfe` |
| --- | ---: | ---: |
| 318 | 3,776 | 4,476.00 |
| 319 | 267 | 8,586.50 |

318's stored peak equals the **09:15:01 tick's `current_pnl`** (3,775.50), not the running maximum
— it was stamped from one poll. 319's 267 matches no tick at all. Both rows carry
`journey_stats.poll_count = 0`, an empty `timeline` and `peak_pnl_validity: "valid"`, while the
tick table holds the real series. The journey fields and the tick table are not connected.

### The degraded opening mark, confirmed

319's 09:15:01.432 tick records `executable_mark 194.25` with
`policy_action SHADOW_DEGRADED`, `policy_reason "mark_untrusted:UNRESOLVED"`, and `ltp_mark 122`
against a 194.25 executable. **The app detected and labelled this itself.** That is the 194.25
you flagged. It is a mark on the spread, not a leg price, and it is the first tick of the session.

---

## Case 2 — trades 320/321: partially joined

Entries 09:37:06.819 (NF) and 09:37:16.628 (BNF), saved debits 66.55 and 167.60, both from 09:35
quotes per the assignment. `entry_pcr` 1.7488017883190936 and `entry_atm_iv` 62.485 on 321 match
the 09:35:23 finalisation frame exactly, so the entry context is joined to the snapshot clock.

Still to do: the nearest eligible exact-contract quotes at the tap versus the saved entry prices,
and the first valid marks. Given Case 1, **I will source those from `position_ticks` legs, not
from `ml_option_chain_snapshots`**, and will state quote age against `source_ts` on each leg.

---

## Case 3 — managed put `sl1_b3ae4586804c7a80abff`: BLOCKED

`position_ticks` has **no rows** for this trade id, and it is not in `trades_v2`. The phone shows
it under "Local Paper positions" with `simulated fill (broker snapshot)` and
`paper_strong_buy_60m_v1`. It is an on-device record, so the admission at 09:56:49.090, the
TIME_EXIT at 11:00:17.118, the +₹3,070.09 net, the ₹154.91 fees and the instrument
`NSE_FO|59956` (64000 PE, 29 Dec, 30 units) **cannot be reconciled from the database**.

**Evidence request:** the on-device single-leg store export for 23 Nov, with the entry and exit
quote bundles. Without it I cannot attribute the 3m 28s delay against the 60-minute target, and I
will not infer it from the notification text alone.

The stale OPEN display against the persisted CLOSED record is separately visible in your
screenshots: at 11:05 (poll 23) the Market tab still lists the position as `OPEN · 68 min elapsed`
with `exit by 10:56:49`, while the 11:00 notification already reports it CLOSED with TIME_EXIT.

---

## Engineering findings, with reproduction

| # | Finding | Reproduce |
| --- | --- | --- |
| E1 | Fee engine prices the close from a stale chain bundle while P&L uses a fresh quote | Q2 vs the 09:17:10 tick legs |
| E2 | `ml_option_chain_snapshots` opening rows are below intrinsic and must not be used as marks | the intrinsic table above |
| E3 | `journey_stats` peak/trough are stamped from a single poll, report `poll_count 0`, and ignore the tick series | Q3 vs `running_mfe` |
| E4 | Position display stays OPEN after the persisted record is CLOSED | 11:00 notification vs 11:05 Market tab |
| E5 | Horizon label "last 30 min" with a 27.5-min constant and a clamped 15-min actual cover | prior checkpoint §2 |
| E6 | `bnf_pcr` built by multi-source coalesce; `bnf_near_atm_pcr` null throughout | prior checkpoint §3 |

E1–E3 corrupt labels and so gate Tests 2–6. E2 is the most consequential: **any replay that marks
positions from the chain table in the first 10 minutes of a session is unsound**, including parts
of my own earlier premium-model work, which I will re-check.

---

## Dependency status for the next tests

- **Test 1** can run now. It needs the direction path and the tick series, both clean.
- **Test 2** needs E2 respected: expiry/execution economics must be priced from tick legs or from
  chain rows that pass an intrinsic check.
- **Tests 3–5** need the PCR/IV rebuild already specified.
- **Test 6** needs Case 3's on-device export for the 64000 PE +120 scoring at 11:56:49.090.
