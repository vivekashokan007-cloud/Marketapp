# Test 0 revised manifest + Test 1 checkpoint

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CODEX_REPLY_TO_CLAUDE_CODE_TEST0_EVIDENCE_AND_TEST1_20261123`

| Test | Result |
| --- | --- |
| Test 0, Case 3 (managed single-leg) | **PASSED** — reconciles to the paisa against production's own dated fee functions |
| Test 0, Case 1 (318/319) | **held UNVERIFIED for scoring**, as you instructed |
| Test 0, Case 2 (320/321) | **partially joined** — carried forward |
| Test 1 (opening anchor) | **PASSED** — the clamp window is exactly 09:30–09:50 and the effect is quantified |

## Import and provenance

Per the standing rule that raw phone exports, Paper positions and credentials stay out of the
repository, the two exports are **not committed**. They are pinned by digest and kept outside the
tree; everything below is reproducible from them.

| File | sha256 | bytes |
| --- | --- | --- |
| `mr_session_evidence_2026-11-23.json` | `8cd38f25c7d8a581d197efa0d8fc3b61d70624da5d7f7e48a5943db87c6519ff` | 2,129,000 |
| `daily_review_2026-11-23 (1).json` | `49364fcee2ed946c4027364ff4355b4861235ebeac866a810e329bb3d78270a4` | 17,990 |

Export identity: app/brain `2.6.104`, policy `index_direction_v1`, single-leg
`index_single_leg_v1`, calendar `ev_v12_nse_cal_nf_weekly_tue_prev_session_v1:2026`. Daily review
`input_hash 4bfc4b59…a713`, revision 1, `report_status COMPLETE`, `single_leg_outcome_rows 1`,
`single_leg_outcomes_sha 4e55c3af269a41b6`.

Pinned code: Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`,
2.6.104 / b535 / app_js_v 1399.

---

## Test 0 Case 3 — `sl1_b3ae4586804c7a80abff`: PASSED

Joined `parts.single_leg.positions`, `.closed` and `.hold_observations` on the position id;
decision `idx1_8463f989441e69b2d171` and `decision_poll_ts 09:55:00` agree across all three.
Contract `NSE_FO|59956`, BNF 64000 PE, expiry **2026-12-29**, lot 30 from
`contract_lot_table_v3_20260914` (`source: authoritative_contract_rule`), tick 0.05 derived from
`tick_size_raw 5` under `UPSTOX_PAISE`.

**Every figure reconciles, and the fees reconcile against production's own functions, not against
the export's arithmetic:**

| Item | Recorded | Recomputed | Δ |
| --- | ---: | ---: | ---: |
| Entry fill (ask 1427.50 + 1 tick) | 1427.55 | 1427.55 | 0 |
| Exit fill (bid 1535.10 − 1 tick) | 1535.05 | 1535.05 | 0 |
| Gross (107.50 × 30) | 3,225.00 | 3,225.00 | 0 |
| Entry fee | 42.88 | 42.8829 | +0.0029 |
| Exit fee | 112.03 | 112.0306 | +0.0006 |
| Fees total | 154.91 | 154.9135 | +0.0035 |
| **Net** | **3,070.09** | **3,070.09** | **0** |
| Capital required (1427.55×30 + entry fee) | 42,869.38 | 42,869.38 | 0 |

Recomputation used `index_single_leg_v1.entry_fee` / `exit_fee` at `8f9aa234` for trade date
2026-11-23; residuals are rounding only. Fee identity on the record:
`ev_v12_fee_teacher_v2_dated_20261013`, `rate_date 2026-11-23`, `exch_from_2026-03-01`,
`stt_from_2026-04-01` — dated, and matching the date of the trade rather than of the position.

**One-tick adjustment confirmed on each side**, in the conservative direction both times (pay the
ask plus a tick, receive the bid minus a tick).

**Broker snapshot timestamps.** Entry: `request_start 09:56:49.054`-ish
(`request_start_ms 1795408008780`), `receipt_ms 1795408008903`, latency **123 ms**, quote ts
09:56:49.054, `age_s 0.036`, `response_symbol NSE_FO:BANKNIFTY26DEC64000PE` — the response symbol
independently confirms the December contract. Exit: `request_start_ms 1795411816990`,
`receipt_ms 1795411817106`, latency **116 ms**, quote ts 11:00:17.082, `age_s 0.036`, ask_qty 60 /
bid_qty 30. Basis `BROKER_SNAPSHOT` with `paper_broker_snapshot_v1` and the explicit disclosure
that exchange quote age is unavailable — recorded as a limitation, not inferred away.

**Overrun.** Deadline 10:56:49.090, exit 11:00:17.118 → **0:03:28.028**, matching your figure
exactly. The deadline falls between the 10:55 and 11:00 polls. The 11:00 brain result published at
11:00:16.442, the quote request started at 11:00:16.990, the snapshot returned 116 ms later, and
the exit stamped at 11:00:17.118. **Poll cadence is supported as the cause**, and I am attributing
it no further than the timing record supports: the monitor could not have run between 10:56:49 and
11:00:16 because no poll occurred there.

**One observation, not two.** `hold_observations.marks.same_60` carries `quote_ts
11:00:17.082`, identical to the exit quote, with `status SCORED`. Counted once.

**+120 is unscored, and the gap is structural rather than a sampling miss.** I checked
independently: for session 2026-11-23, `ml_option_chain_snapshots` holds BNF rows for **expiry
2026-11-23 only** — the December series the trade actually used is not captured at all, at any
time of day, not merely near 11:56:49.090. The other marks are `PENDING`: `near_close`
15:25–15:30, `next_open` 2026-11-25 09:15–09:25, `next_60` 2026-11-25 10:15–10:25 (next session
25 Nov).

**Collection requirement (E7, new):** when the expiry-day fallback
(`expiry_day_paper_buy_fallback_v1`) moves the traded contract to a later series —
here `trade_expiry 2026-11-23` → `contract_expiry 2026-12-29` — the chain capture continues to
record only the front expiry, so **every later hold mark for that position is unscoreable by
construction**. Capturing the traded `instrument_key` is the fix, not a wider strike range.

**Realised Paper observation recorded:** BNF, BUY_PE, +₹3,070.09 net, TIME_EXIT, capital basis
`LONG_PREMIUM_PLUS_FEE` ₹42,869.38, `source ACTUAL_PAPER`, policy `paper_strong_buy_60m_v1`.
This agrees with the daily review (`net_pnl_after_costs 3070.09`, `fees_total 154.91`,
`resolved_of_entries 1/1`). **n = 1.**

Stale OPEN card kept as a separate UI defect (E4): the 11:00 notification reports CLOSED while the
11:05 Market tab still shows `OPEN · 68 min elapsed · exit by 10:56:49`.

---

## Test 1 — opening anchor and price-only replay: PASSED

`tools/pcr_vix_iv/anchor_audit.py`, run over 7 session exports (12, 13, 18, 19, 20, 23 Nov with
authority history; 16/17 Nov carry none). **778 authority decisions, 742 with a direction score**,
371 per index.

```
python3 -I tools/pcr_vix_iv/anchor_audit.py <exports>/export_*.json \
    <exports>/mr_session_evidence_2026-11-23.json --csv anchor_audit_rows.csv
```

### The clamp window is exactly 09:30–09:50

| poll | n | shared anchor | share |
| --- | ---: | ---: | ---: |
| 09:30 | 12 | 12 | **100%** |
| 09:35 | 12 | 12 | **100%** |
| 09:40 | 12 | 12 | **100%** |
| 09:45 | 12 | 12 | **100%** |
| 09:50 | 12 | 12 | **100%** |
| 09:55 | 12 | 0 | 0% |
| 10:00 and later | 12 each | 0 | 0% |

Overall 30 of 371 scored decisions per index (8.1%) run on a shared anchor, and they are the same
five polls every session on both indices.

### Your 09:50 point is confirmed, and the reason is subtler than the clamp

At 09:50 the anchor is **still 09:20 in all 12 cases**, `elapsed 30.0 min`. This is not the
fallback clamp firing. The lookback target at 09:50 is 09:22:30, and the eligible set is
`[open_ref, target]` = polls in [09:20, 09:22:30], whose only member **is** the open reference. So
`cands[-1]` returns the 09:20 poll and `anchor == open_ref` by selection, not by fallback. At
09:55 the target moves to 09:27:30, the 09:25 poll becomes eligible, and the share drops straight
to 0%. A rule that only replaced the fallback would therefore still leave 09:50 shared.

### The dependence is deterministic, and measured

While shared, `z_30 = z_open / sqrt(Δ/375)`. Across the 60 shared scored rows the observed ratio
matches the prediction with **max |residual| = 0.0103** (the residual is the difference between
poll labels and true observation times). The 23 Nov 09:35 BNF row is exact to six figures:
z_open −1.694597, z_30 −8.472985, ratio **5.000000** against √(15/375) = 0.2.

### The gate is not vacuous in general — only inside the window

Of 357 scored rows where |z_open| ≥ 0.50 (so the second condition is actually reached), z_30
carries the same sign as z_open in **294**, and **63 are counter-examples where z_30 would refuse
what z_open admits**. Every one of the 63 has a **distinct** anchor; 30 of them are the rebound
case (z_open < 0, z_30 > 0). Spread across sessions: 18 Nov 13, 19 Nov 17, 20 Nov 16, 23 Nov 17;
by index NF 35, BNF 28.

So the confirmation term does real work — just never between 09:30 and 09:50, where it is
provably inert.

### What that cost on 23 November

| poll | BNF spot | dir | z_open | z_30 | anchor |
| --- | ---: | --- | ---: | ---: | --- |
| 09:35 | 63,290.35 | BEAR STRONG | −1.6946 | −8.4730 | shared |
| 09:40 | 63,657.80 | BEAR STRONG | −1.1608 | −5.0264 | shared |
| 09:45 | 63,693.15 | BEAR STRONG | −1.1096 | −4.2975 | shared |
| 09:50 | 63,502.30 | BEAR STRONG | −1.3863 | −4.9014 | shared |
| 09:55 | 63,605.55 | BEAR STRONG | −1.2365 | −1.8927 | distinct |
| 10:00 | 63,497.30 | BEAR STRONG | −1.3936 | −1.1838 | distinct |
| **10:05** | 63,446.90 | **NEUTRAL (PULLBACK)** | −1.4668 | **+0.8054** | distinct |

BNF rebounded **+367.45 points** between 09:35 and 09:40. The direction rule did not register it
until **10:05** — a **25-minute delay**, and the delay is fully attributable to the shared anchor:
z_30 only became informative once the anchor became distinct at 09:55, and crossed zero at 10:05.
NF behaves identically (09:35 low 25,576.80 → 09:40 25,670.80, BEAR until NEUTRAL at 10:05).

The two Manual Paper spreads were tapped at 09:37, i.e. **two minutes after the session low**,
under a signal that could not downgrade for another half hour.

### Variations — not yet run

The three single-variable variants (distinct recent anchor; a shorter valid recent-return measure
for early observations; an extension/pullback feature) are **specified but not scored**, because
scoring them on paired outcomes requires entry and exit marks for the affected opportunities, and
the only marks available in the 09:30–09:50 window come from the chain table, which **E2 shows is
unusable there** (opening rows below intrinsic). I will score them from `position_ticks` legs and
broker snapshots where those exist, and report coverage honestly where they do not. Thresholds
will be learned on development sessions only, with 23 Nov retained in development as instructed.

---

## Carried forward

- **Case 1 (318/319):** booked net held out of verified scoring until the actual close bundles are
  joined and the fee and P&L calculations reconcile from the same quotes. The 09:17:10 ticks
  support profitable marks (executable 250.65 / 111.35 against saved closes 242.50 / 109.75), but
  that is a nearby mark, not the close bundle.
- **Case 2 (320/321):** 09:35–09:38 join to finish, sourced from tick legs rather than the chain
  table, reporting repricing versus unresolved uncertainty.
- **Test 1 variants:** as above.

## Engineering findings register

| # | Finding | Status |
| --- | --- | --- |
| E1 | Fee engine prices the close from a stale chain bundle while P&L uses a fresh quote | open |
| E2 | Chain-table opening rows sit below intrinsic; unusable as marks | open, blocks early-session scoring |
| E3 | `journey_stats` peak/trough stamped from one poll, `poll_count 0`, ignores the tick series | open |
| E4 | Position card stays OPEN after the persisted record is CLOSED | open |
| E5 | "last 30 min" label with a 27.5-min constant and a clamped 15-min actual cover | open |
| E6 | `bnf_pcr` multi-source coalesce; `bnf_near_atm_pcr` null | open |
| **E7** | **Expiry-day fallback trades a contract the chain capture never records, so later hold marks are unscoreable** | **new** |

## Boundaries

Read-only database access. Raw exports pinned by digest, not committed. No production change, no
merge, no release, no candidate publication, no fabricated approval. Original Paper records
preserved; 320 and 321 remain OPEN under their original rules; the closed single-leg position is
recorded as closed, as the persisted evidence shows.
