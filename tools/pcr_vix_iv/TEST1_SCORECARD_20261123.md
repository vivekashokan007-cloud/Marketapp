# Test 1 scorecard + Test 0 manifest update

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CODEX_REPLY_TEST1_AND_MANUAL_PAPER_TRADE_UPDATE_20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, 2.6.104 / b535

| Item | Result |
| --- | --- |
| Test 1 mechanics | passed previously; unchanged |
| Test 1 economics, 3 variants | **run — all three are net negative on this sample** |
| Trade 320 close | **cause established**; booked ₹1,252.34 kept, reconciliation now explained |
| Trade 321 | **OPEN**, mark recorded with timestamp |
| My 25-minute attribution | **corrected** — you were right |

---

## 1. Correction: the 25-minute figure was overstated

I attributed the whole 09:40 → 10:05 interval to the shared anchor. That is wrong, and your
reasoning is the right one: the anchors separated at 09:55 and BNF still read BEAR at 09:55 and
10:00 with a **distinct** anchor, so those two polls are not explained by the clamp.

| poll | anchor | z_30 | label | attributable to the shared anchor? |
| --- | --- | ---: | --- | --- |
| 09:40 | shared | −5.0264 | BEAR STRONG | **yes** — z_30 cannot differ in sign from z_open |
| 09:45 | shared | −4.2975 | BEAR STRONG | **yes** |
| 09:50 | shared | −4.9014 | BEAR STRONG | **yes** |
| 09:55 | distinct | −1.8927 | BEAR STRONG | no — the term was free and still agreed |
| 10:00 | distinct | −1.1838 | BEAR STRONG | no |
| 10:05 | distinct | **+0.8054** | NEUTRAL (PULLBACK) | no — the term worked |

**Corrected claim: at most 15 minutes (three polls, 09:40–09:50) are attributable to the shared
anchor.** The remaining 10 minutes are the confirmation term operating normally and not yet
crossing zero — partly because the rebound had already faded (63,657.80 at 09:40 against
63,605.55 at 09:55).

---

## 2. Trade 320 — cause established

Closed 12:31:50.093, `manual_book_profit_button`, `brain_book_event` absent, source
`manual_close`. Booked: entry 66.55, exit 87.50, gross ₹1,361.75, costs ₹109.41,
**net ₹1,252.34**. Your reproduction of the gross is confirmed.

**The 4.35-point divergence is a stale fee bundle, and the records establish it.**

The saved `exit_snapshot.close_leg_quotes` — long 25600 PE 132.05/132.35, short 25450 PE
40.10/40.20, executable **91.85** — reproduces the **12:29:44.749 position tick exactly**, on both
legs, including `executable_mark 91.85`. That tick is **2 min 05 s older than the close**.

The P&L came from a different request. `close_trace_json.paper_close_quote`:

```
source            P1_REST_60S_MANUAL_CLOSE
request_id        0d60f511-b6c5-4bf3-a0fd-e313568786a0
source_quote_ts   2026-11-23T12:31:46.921+05:30
quoted_at_ms      1795417306926      expires_at_ms 1795417336926   (30 s validity)
mark_basis        EXECUTABLE         mark_trust_state TRUSTED      leg_count 2
contract_version  paper_close_quote_v1
```

The intervening tick confirms the direction of travel: executable 91.85 (12:29:44) → **90.00**
(12:30:47) → 87.50 at the fill (12:31:46.921). Monotone decline as NIFTY rallied, so 87.50 is the
consistent fresh value and 91.85 is simply two minutes stale.

**So the booked ₹1,252.34 is priced from the better source.** What is defective is that
`close_turnover` = (132.05 + 40.20) × 65 = **11,196.25** is computed from the stale bundle rather
than from the close-quote legs. Third instance of E1, and this one isolates the mechanism: **the
`paper_close_quote_v1` legs are not persisted into `exit_snapshot`**, so the fee path falls back
to the last periodic tick.

Manifest entry: booked ₹1,252.34 retained, quote reconciliation **explained but not yet corrected
in the record**; fee input stale by 2 min 05 s; original row untouched.

## 3. Trade 321 — OPEN, with an explicit mark

Status **OPEN**, no realised exit P&L. Latest retained tick **13:13:12.936 IST**:
`executable_mark 147.70`, gross MTM **−₹597.00**, `valuation_quality OK`, `policy_action HOLD`.

Your 13:07:20.908 check (−₹835.50) is consistent with a volatile series: −₹126.00 at 13:11:11.834,
−₹129.00 at 13:12:12.824, −₹597.00 at 13:13:12.936. `running_mae` **−₹3,595.50**,
`running_mfe` **−₹126.00** — the position has never been in profit at any retained tick.

This is the 63200/62800 BNF Bear Put spread and is **separate** from the 64000 PE single-leg pilot
already reconciled.

---

## 4. Test 1 variants — the economic comparison

`tools/pcr_vix_iv/anchor_variants.py`, 778 decisions, 6 sessions, both indices, 60-minute horizon.

```
python3 -I tools/pcr_vix_iv/anchor_variants.py <exports>/export_*.json \
    <exports>/mr_session_evidence_2026-11-23.json --horizon-min 60 [--retrace R]
```

**Outcome basis is `DIRECTIONAL_ONLY` — index spot points, not money.** Contract-level after-cost
scoring of counterfactual signals is not available: in the clamp window the chain rows fail the
no-arbitrage check (E2), and broker snapshots exist only for requests actually made. Eligible
exact-leg quote coverage for counterfactual signals in 09:30–09:50 is therefore **zero**, and the
reason is capture, not sampling.

| variant | signals | scored | unscored | win rate | total pts | mean pts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BASELINE (b535) | 294 | 223 | 71 | 79.4% | 19,815.9 | 88.86 |
| V1 distinct anchor | 280 | 209 | 71 | 79.9% | 18,637.7 | 89.18 |
| V2 short distinct return (10 min) | 290 | 219 | 71 | 79.0% | 18,965.6 | 86.60 |
| V3 pullback guard (50%) | 294 | 223 | 71 | 79.4% | 19,815.9 | 88.86 |

**Paired effect on the signals each variant suppresses:**

| variant | suppressed | scored | losses avoided | continuations missed | net |
| --- | ---: | ---: | --- | --- | ---: |
| V1 | 14 | 14 | 4 (−375.0 pts) | 10 (+1,553.2 pts) | **−1,178.3 pts** |
| V2 | 4 | 4 | 0 | 4 (+850.3 pts) | **−850.3 pts** |
| V3 @50% | 0 | 0 | — | — | 0 |

By index, V1: BNF −896.1, NF −282.1. V2: BNF −685.9, NF −164.4.

**V3 never binds at 50%**, so I swept it rather than report a null:

| retrace | suppressed | scored | losses avoided | continuations missed | net |
| ---: | ---: | ---: | --- | --- | ---: |
| 15% | 38 | 23 | 1 (−166.5) | 22 (+3,115.7) | −2,949.2 |
| 20% | 30 | 15 | 1 (−166.5) | 14 (+1,950.4) | −1,783.9 |
| 25% | 24 | 11 | 0 | 11 (+1,675.5) | −1,675.5 |
| 30% | 18 | 7 | 0 | 7 (+934.9) | −934.9 |
| 40% | 4 | 1 | 0 | 1 (+96.9) | −96.9 |

For reference, 23 Nov BNF retraced only **31.3%** of its open-to-low move at 09:40 (open ref
64,464.25, low 63,290.35, rebound to 63,657.80), so a guard loose enough to be plausible would not
have stopped Monday's entry anyway.

### Verdict

**The mechanics finding does not translate into an economic improvement on this sample.** All
three variants remove more profitable continuations than losses, at every threshold tested. The
shared anchor is a genuine design defect — the confirmation term provably cannot refuse anything
between 09:30 and 09:50 — but on 6 sessions the signals it wrongly sustains are, on balance,
*profitable* ones, because those sessions trended.

### Why this is not yet decisive — stated plainly

- **Overlapping labels.** 223 scored signals come from only **12 session-index units**. Every
  5-minute poll re-signals the same move, so win rate (79%) and mean points (+88.9) are inflated
  and the variants' differences are driven by a handful of sessions. These are not independent
  observations and must not be read as performance.
- **Directional only.** No premium, no costs, no contract choice. A 60-minute index move maps very
  differently onto a 0-DTE debit spread than onto a December single leg.
- **One regime.** The sample is dominated by trending sessions, which is exactly the regime where
  suppressing a sustained signal is most costly. A choppy sample could reverse the sign.

I am therefore recording Test 1's economic limb as **inconclusive and currently negative**, not as
a rejection of the variants. No variant is proposed for Paper review.

---

## 5. Managed BNF put — observer windows

**+120 remains unscored** as instructed. The b535 observer's own capture evidence shows
`near_close` 15:25–15:30, `next_open` 2026-11-25 09:15–09:25 and `next_60` 2026-11-25 10:15–10:25,
all `PENDING`. I will check their own capture evidence when those windows elapse rather than
substituting another source. E7 stands as the risk: the traded contract is the 2026-12-29 series
and the chain capture records only the front expiry, so these marks depend entirely on the
observer making its own exact-contract requests.

## 6. Register

E1 (fee/P&L quote split) now has **three instances** — 318, 319 and 320 — and 320 isolates the
mechanism: `paper_close_quote_v1` legs are not persisted into `exit_snapshot`. E2–E7 unchanged.

## 7. Boundaries

Read-only database access. Raw exports pinned by digest, not committed. Original records
untouched; 321 remains OPEN under its original rules; 320 recorded as closed by the user's Book
Profit action. No production change, merge, release or candidate publication.
