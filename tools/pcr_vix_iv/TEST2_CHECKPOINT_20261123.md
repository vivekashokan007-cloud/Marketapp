# Check 0 update (trade 321) + Test 2 checkpoint

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CODEX_REPLY_CLAUDE_CODE_COMPLETE_SEVEN_CHECKS_20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, 2.6.104 / b535

| Check | Verdict |
| --- | --- |
| **0** — trade 321 close | **PASSED for P&L**, fee input stale — amount kept pending |
| **2a** — eligible-expiry comparison | **BLOCKED** — structural, exact missing evidence below |
| **2b** — outright buys vs protected spreads | **feasible, not yet run** |

---

## Check 0 — trade 321 closed, reconciled

Vivek is right: 321 is no longer open. Persisted close found and matched.

| | |
| --- | --- |
| Close | **2026-11-23 14:18:38.719 IST**, `manual_book_profit_button`, `close_trace_json.source = manual_close` |
| Request | `22ede924-6990-44d8-804e-428bc71a11c8` |
| Source quote | **14:18:35.295 IST** — 3.4 s before the fill, `mark_trust_state TRUSTED` |
| Entry | 09:37:16.628, debit 167.60 |
| Booked exit | 219.60 |
| Hold | 4 h 41 m 22 s = **281.37 min** (recorded `exit_hold_minutes` 281) |

**Gross and net reconcile exactly:**
`(219.60 − 167.60) × 30 = ₹1,560.00` = recorded `actual_pnl`; `1,560.00 − 118.14 = ₹1,441.86` =
recorded `net_pnl`. **So 321 closed at a profit**, as reported.

The booked close is corroborated by the position ticks either side: 14:16:45 → 237.60,
**14:17:42 → 221.00**, fill 219.60 at 14:18:35.295. Monotone decline, fresh quote, consistent.

### The fee input is stale again — fourth instance of E1

`exit_snapshot.close_leg_quotes` gives long 353.05 / short 103.15 → **249.90**, and
`close_turnover` 13,686.00 = (353.05 + 103.15) × 30 confirms the fee engine used that pair. That
is **~30 points away from the fill** and roughly four minutes stale.

Unlike 318/319/320, this bundle **does not reproduce any retained position tick exactly**. The
nearest is 14:14:40.471 (long 351.10/352.90, short 102.35/102.70 → 248.40), about 1.5 points
away. So its exact source is **not established from the database**; it is consistent with a
separate display-path refresh (`PositionMarkStore`) rather than the tick series.

| source | close value | turnover basis |
| --- | ---: | ---: |
| fee bundle (≈14:14–14:15, source unidentified) | 249.90 | 13,686.00 |
| 14:17:42 tick | 221.00 | 11,064.00 |
| booked fill 14:18:35.295 | 219.60 | not persisted |

**Note the sign flips.** On 318/319 the stale bundle *understated* turnover; here it *overstates*
it by about 24%. The error is not systematically one-directional, so it cannot be corrected by a
constant.

Exact fee recomputation is **blocked**: the fill's per-leg prices are not persisted anywhere, only
the net 219.60. That is precisely what `d2cc9f2` fix 1 publishes, and it is why I am keeping the
₹1,441.86 **net amount pending** while recording the gross as reconciled.

### Extrema still disagree with the ticks (E3, fourth instance)

`trades_v2` peak 2,457 / trough −3,147 against tick `running_mfe` **2,775.00** / `running_mae`
**−3,595.50**. The journey fields and the tick series remain disconnected.

### Attribution

| trade | source | action |
| --- | --- | --- |
| 318, 319 | operator | Manual Paper close, Book Profit, 09:17 |
| 320 | operator | Manual Paper close, Book Profit, 12:31:50.093 |
| **321** | **operator** | **Manual Paper close, Book Profit, 14:18:38.719** |
| `sl1_b3ae…` | **managed** | `paper_strong_buy_60m_v1` TIME_EXIT 11:00:17.118, **₹3,070.09 verified net** |

The managed single-leg result remains the only **fully verified** after-cost Paper outcome: its
fees were recomputed against production's own dated functions to the paisa.

---

## Test 2a — eligible-expiry comparison: BLOCKED

Measured across every captured session, 12–23 November:

| session | BNF expiry (DTE) | NF expiry (DTE) |
| --- | --- | --- |
| 11-12 | 2026-11-23 (11) | 2026-11-17 (5) |
| 11-13 | 2026-11-23 (10) | 2026-11-17 (4) |
| 11-16 | 2026-11-23 (7) | 2026-11-17 (1) |
| 11-17 | 2026-11-23 (6) | 2026-11-17 (0) |
| 11-18 | 2026-11-23 (5) | 2026-11-23 (5) |
| 11-19 | 2026-11-23 (4) | 2026-11-23 (4) |
| 11-20 | 2026-11-23 (3) | 2026-11-23 (3) |
| 11-23 | 2026-11-23 (0) | 2026-11-23 (0) |

**Exactly one expiry is captured per session and index. Never two.** There is therefore **no
second expiry at any decision time**, so "compare the existing eligible contract with feasible
later expiries on the same signal" cannot be scored on any retained data — not for one session,
not for one decision.

This is the same capture limitation as **E7**: the chain capture records only the front series,
which is also why the managed BNF put's 2026-12-29 contract has no mark at any time of day.

**What would unblock it.** Capturing a second expiry — or, more cheaply and more precisely, the
**traded `instrument_key`** — at each poll. Widening the strike range does not help.

**What I will not do.** DTE does vary *between* sessions (BNF 11 → 0, NF 5 → 0), so a
between-session DTE comparison is arithmetically possible. I am not reporting one as the Test 2
result, because DTE is perfectly confounded with the session: the BNF DTE-11 observation *is*
12 November. With 6–8 sessions that comparison measures which days trended, not what expiry
choice is worth. Treating Gemini's DTE ≥ 1 suggestion that way would manufacture a result.

---

## Test 2b — outright buys vs protected spreads: feasible, not yet run

This half **is** scoreable, because both legs of a protected spread come from the same captured
expiry as the outright buy. Predeclared before any scoring:

| rule | value |
| --- | --- |
| Signal set | b535 authority decisions with a single-leg plan (`why.sl`), both indices |
| Contract | the exact strike the brain named, and for the spread its displayed protected pair |
| Entry price | long at **ask**, short at **bid**; exit long at **bid**, short at **ask** |
| Fill | first poll at or after publication, within the frozen **10-minute** tolerance |
| Quote freshness | a mark is refused if it fails a **no-arbitrage check against spot** (E2) |
| Leg timing | both legs from the **same poll**; asynchronous legs are refused, not interpolated |
| Fees | production `index_single_leg_v1.entry_fee` / `exit_fee`, dated to the trade date |
| Lot | dated `contract_lot_table_v3_20260914` — BNF 30, NF 65 |
| Loss budget | matched: the spread's max loss sets the outright's quantity cap |
| Horizon | 60 min primary; +30, +120, near close as declared secondaries |
| WAIT | included in the policy-level comparison, not only the executed subset |

**One measurement must precede it.** E2 is established at the 09:15 and 09:20 polls on 23 November
(call bids below intrinsic), but the **precise extent of the degraded window is not yet measured**
— I attempted a scan and it exceeded the query timeout. Until that window is bounded per session,
I cannot state eligible coverage honestly, and coverage is half of what this test must report.
Measuring it is the next step and needs an offline join of the export spot path against a bounded
chain read, not a server-side lateral join.

---

## Status of the remaining checks

| Check | Status | Blocking evidence |
| --- | --- | --- |
| 1 | passed (mechanics + variants); **user-facing text not yet verified** | needs a build reading the new lineage fields |
| 2a | **blocked** | a second expiry, or the traded `instrument_key`, per poll |
| 2b | feasible | E2 window must be bounded first |
| 3 | not started | exact-contract IV needs the same per-contract capture |
| 4 | not started | depends on 3 |
| 5 | not started | PCR lineage (fix 3b) not implemented |
| 6 | partly evidenced | 321 now has a real manual exit to compare; +120 for the 64000 PE needs its own exact-contract quote (E7) |

For check 6, 321 now supplies the comparison you asked for: manual exit at 14:18:38.719 for
**+₹1,441.86 net**, against a retained peak of **₹2,775.00** and a trough of **−₹3,595.50** on the
tick series — a giveback of about ₹1,333 from peak, and a position that spent the morning well
under water before recovering. That is a genuine hold-and-exit observation, and it is one trade.

---

## Coordination with Grok

`d2cc9f2` publishes the native close bundle (`legs`, `close_leg_quotes`, `close_bundle_complete`,
`close_bundle_unavailable_reason`, under `paper_close_bundle_v1_…`) plus the anchor lineage fields.
**The MarketVivi close must consume the accepted request's bundle for gross P&L, fees and the
persisted evidence.** I have not touched MarketVivi and will not without agreeing ownership.

The three close-correctness properties you want proven — a fresh close quote beats a stale card
mark, overlapping requests stay tied to their own quote, and each trade closes exactly once across
resume and restart — span the native store and the PWA. The first is covered by
`PaperCloseBundleTest`; the other two must be proven where the close is performed. The eight
Kotlin tests remain **pending build verification**; there is no Android SDK in this sandbox.

Trade 321 is the fourth independent instance of E1 and the first where the stale bundle matches no
retained tick — worth handing over, because it means the PWA fix must take the bundle from the
close request itself rather than from any card or tick path.

## Boundaries

Read-only database access. Review branch only. Original rows preserved — 321 recorded as the user
closed it, with its net amount flagged pending on the fee input. No production change, merge,
release, version bump or candidate publication.
