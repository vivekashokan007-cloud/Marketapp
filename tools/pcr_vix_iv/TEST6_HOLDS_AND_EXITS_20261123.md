# Test 6, part 2 — the policy's own exit signals against what the operator actually did

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535
**Excursions read from** `policy_trace_json.trusted_mfe` / `trusted_mae` (per E11, never `running_*` or `trades_v2`)

All four Paper positions were closed by the operator. The tick service recorded, at every tick,
what the exit policy would have said. That makes this a **direct comparison on real fills**, not a
counterfactual reconstruction: the policy's signal and the operator's booked outcome are both in
the data.

| | |
| --- | --- |
| Sample | **4 positions, 822 ticks** — all manual closes, all Paper |
| Thresholds | `constant_only` / `constant_safety_floor` on every tick — **no live percentile authority held** |
| Headline | **every exit signal that fired was early**, in all four positions |
| Largest single effect | **321: overriding the stop was worth +₹4,707** |
| Second effect, and the actionable one | **₹2,639 lost between the target alert and the manual execution, 1–2 minutes later** |

---

## 1. What fired, and what it would have booked

Thresholds are production's own: target `current_pnl >= 0.5 × max_profit`, stop
`current_pnl <= −0.6 × max_loss` (`TP_MULT 0.50`, `SL_MULT 0.60`), end of day at 15:15 IST.

| trade | signal | fired at | P&L at the signal | **operator booked** | difference |
| --- | --- | --- | ---: | ---: | ---: |
| **318** | SHADOW_EOD | 11-20 15:15:38 | +1,368.00 | **+2,079.00** | operator **+711.00** better |
| 318 | SHADOW_TP | 11-23 09:15:01 | +3,775.50 | +2,079.00 | operator **−1,696.50** worse |
| 318 | SHADOW_DEGRADED | 11-20 13:23:05 | (mark untrusted) | — | — |
| **319** | SHADOW_EOD | 11-20 15:15:38 | +841.75 | **+3,094.00** | operator **+2,252.25** better |
| 319 | SHADOW_TP | 11-23 09:16:05 | +4,036.50 | +3,094.00 | operator **−942.50** worse |
| 319 | SHADOW_DEGRADED | 11-20 13:23:05 | (mark untrusted) | — | — |
| **320** | **none — HOLD on all 175 ticks** | — | — | **+1,361.75** | policy had nothing to say |
| **321** | **SHADOW_SL** | 11-23 11:14:51 | **−3,147.00** | **+1,560.00** | operator **+4,707.00** better |

These are SHADOW signals: per the D4 ownership split, `PositionTickService` owns Target Near /
Stop Loss Near / Degraded / EOD and raises them as alerts; it does not execute. So "would have
booked" means "had the alert been acted on at that tick", which is exactly the decision under
test.

---

## 2. The three distinct findings

### 2.1 Overriding the stop on 321 was worth ₹4,707 — and one instance is not a case against stops

At 11:14:51 trade 321 was **−₹3,147.00**, past `−0.6 × max_loss`, and the policy raised
`SHADOW_SL`. The operator held. The position went on to trough at **−₹3,595.50** (11:16:48,
`trusted_mae`), recovered through the afternoon, and was closed at **+₹1,560.00** at 14:18:38.

Acting on that stop would have realised −3,147.00 instead of +1,560.00: a **₹4,707.00** swing on
one trade.

**This is not evidence that the stop is wrong, and I will not present it as such.** A stop exists
to bound the left tail; it is supposed to cost money on the occasions when a position would have
recovered. A single profitable override tells you nothing about the distribution — the relevant
question is how often the stop saves more than it costs, and four positions cannot answer it. What
this *does* establish is that the −0.6 threshold is **reachable by a position that ends
comfortably profitable**, on a 400-wide BNF spread held about five hours. That is worth knowing
before anyone promotes the shadow stop to an executing stop.

### 2.2 The end-of-day exit cut both overnight positions short

On 20 November at 15:15:38 the policy raised `SHADOW_EOD` on both carried positions, at **+1,368.00**
(318) and **+841.75** (319). The operator carried them over the weekend and booked **+2,079.00**
and **+3,094.00** — **₹2,963.25 more in total**.

Both ran further over the gap. Again, two instances, and the overnight gap is exactly the risk EOD
flattening is meant to avoid; 318's trough was −402.00 and 319's −230.75, so neither was ever
deeply adverse. But the direction is consistent with the Phase 1 multi-session finding already in
`brain_audit/SCOPE_multi_session_evaluator.md` (win rate 40.7% → 71.4% from day 0 to day 7, at a
real cost in max-loss realisation), and it is the first time that effect appears in **this app's
own Paper fills** rather than in historical candles.

### 2.3 The actionable one: ₹2,639 lost between the alert and the execution

This is the finding that needs no distributional argument, because it is pure latency.

| trade | target alert | P&L at the alert | manual close | P&L booked | lost in between |
| --- | --- | ---: | --- | ---: | ---: |
| 318 | 09:15:01 | +3,775.50 | 09:17:11 | +2,079.00 | **−1,696.50** |
| 319 | 09:16:05 | +4,036.50 | 09:17:16 | +3,094.00 | **−942.50** |
| | | | | | **−2,639.00** |

The operator **did** act on the target — one to two minutes later. In that window the marks fell
and ₹2,639 of signalled profit evaporated. Nothing about the threshold was wrong; the loss is
entirely in the gap between a 60-second alert and a human tap.

**One caveat, and it matters.** 318's alert fired at **09:15:01**, inside the window E2 shows is
unreliable. 318's mark there (299.05 on a 400-wide spread) is structurally legal and the tick is
`TRUSTED` with no `bound_anomaly`, so it is not provably bad — but it is the same poll that was
provably bad for 319, and I would not act on an opening-poll target. **319's alert at 09:16:05 is
sound** (it is the revalidated tick), so the ₹942.50 on 319 is the figure I would defend; the
₹1,696.50 on 318 should be treated as an upper bound.

**Recurring decision problem.** A target that is reached and alerted but executed a minute or two
later, by hand, on a position whose mark moves several percent per minute (Q7: median 4.3–6.2% per
60 s on these contracts).

**Cause.** The target is evaluated on a 60-second tick and delivered as a notification; execution
waits on a person.

**Proposed reusable rule.** Where the target alert is to be acted on at all, the **alert must carry
the quote bundle it fired on**, and the close must be priced from that bundle or refused — which is
precisely what fix 1 (`paper_close_bundle_v1_…`, commit `d2cc9f2`) now publishes. That does not
recover the ₹2,639, because the market really did move; it makes the gap **visible and attributable**
instead of silently absorbed into the booked P&L. Closing the gap itself requires either automated
execution on the alert or a wider target, and neither is a change I would make on four trades.

**Measured effect on after-cost Paper outcomes.** ₹942.50 on the one defensible instance, up to
₹2,639.00 across both, against total booked net of ₹7,627.33 on the four trades — i.e. the
alert-to-execution gap is of the same order as a third of everything these four positions made.
That is the ratio worth remembering.

---

## 3. 320 is the one the policy had no opinion about

175 ticks, **`HOLD` on every one**. No target (peak `trusted_mfe` 1,673.75 against a 2,712.125
target), no stop (trough `trusted_mae` −1,755.00 against a −2,595.45 stop), and closed at 12:31,
well before 15:15. The operator exited at **+1,361.75**, giving back 312.00 from the trusted peak.

So in one of four positions the exit policy contributed nothing at all, and the outcome was the
operator's judgement alone. Any claim about the exit policy's value has to count that case.

---

## 4. Thresholds were constant-only on every tick

On all 822 ticks `target_threshold_basis` and `stop_threshold_basis` are `constant_only` or
`constant_safety_floor`. **Not once did the live percentile authority hold.** Per the published
contract, brain publishes rupee `target_pnl_at` / `stop_pnl_at` when the percentile authority is
live and the tick composes it with the constants so publication can only fire earlier or equal;
absent or stale (>20 min), the constants stand alone.

They stood alone throughout. So this comparison tests `TP_MULT 0.50` / `SL_MULT 0.60` / 15:15, and
says nothing about the published-threshold path — which, on this evidence, has never been exercised
in Paper.

---

## 5. Register

> **E12 (new).** Every exit signal that fired on the four Paper positions was early. `SHADOW_EOD`
> cut the two overnight positions short by ₹2,963.25 combined; `SHADOW_SL` on 321 would have
> realised −₹3,147.00 on a position that closed at +₹1,560.00, a ₹4,707.00 swing. One position
> (320) never left `HOLD`. Four positions cannot establish that the thresholds are wrong — a stop
> is supposed to cost money when a position recovers — but the −0.6 stop is demonstrably reachable
> by a trade that ends comfortably profitable, which is worth settling before the shadow stop
> becomes an executing one.

> **E13 (new).** ₹2,639.00 of signalled profit was lost between the target alert and the manual
> execution one to two minutes later (318: 3,775.50 → 2,079.00; 319: 4,036.50 → 3,094.00), against
> ₹7,627.33 of total booked net on the four trades. 319's ₹942.50 is the defensible figure; 318's
> alert fired at 09:15:01, inside the unreliable window, so its ₹1,696.50 is an upper bound. The
> cause is latency, not threshold choice. Fix 1's close bundle makes the gap attributable; closing
> it needs either automated execution or a wider target, neither justified on four trades.

> **E14 (new).** `target_threshold_basis` and `stop_threshold_basis` are `constant_only` or
> `constant_safety_floor` on **all 822 ticks**. The live percentile-authority publication path has
> never been exercised in Paper, so no conclusion here applies to it.

---

## 6. Scope

Four positions. Nothing here is statistical, and the two findings that depend on a distribution
(the stop and the EOD exit) are explicitly marked as single or double instances. The managed
single-leg put `sl1_b3ae4586804c7a80abff` exited on `TIME_EXIT` at 11:00:17 for a verified
+₹3,070.09 net and is a fifth observation under a different policy (`paper_strong_buy_60m_v1`),
not folded into the tables above.

## Boundaries

Read-only database access. Review branch only. b535 frozen — **no threshold was changed, retuned
or activated, and the shadow signals remain shadow**. Original rows preserved. No production
change, merge, release, version bump or candidate publication.
