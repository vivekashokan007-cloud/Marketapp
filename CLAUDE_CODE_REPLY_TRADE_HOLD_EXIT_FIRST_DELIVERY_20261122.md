# Claude Code first delivery — trade, hold and exit research

Market Radar · 22 November 2026 IST · handoff `HANDOFF_CLAUDE_CODE_MARKET_RADAR_TRADE_HOLD_EXIT_PLAN_20261122`
**Branch** `review/premium-model-validation-20261121` · Supabase accessed **read-only**
**Reconciliation: Codex's 232 / 229 / 97 / 95 all reproduce exactly.**

---

## 0 · The headline, and a correction to my own prior conclusion

**Codex was right and I was wrong about what was possible.** Yesterday I filed a refusal: 0 of 233
Paper-rule intents could be priced, because the phone export's cohort pins three strikes at 09:20
while the rule tracks the live spot. That finding is still true **of the phone export**. But
`public.ml_option_chain_snapshots` holds what the export lacks, and the experiment I called
infeasible is not. I have now run it.

The database is far richer than the research position I reported:

| | phone export | `ml_option_chain_snapshots` |
| --- | --- | --- |
| sessions | 7 (2 with no decision trail) | **124**, 25 May → 20 Nov 2026 |
| rows | 4,781 observations | **4,544,494** |
| strikes per index/session | **3** pinned at 09:20 | **142–213** |
| last poll | **15:00** | **15:40** |
| 12–20 Nov window | — | 373,794 rows — matches Codex exactly |

So two of the six capture requirements I sent Grok yesterday (**R1** wider ladder, **R2** extend to
15:30) are **already satisfied in the database**. The overnight blocker I proved on 48 adjacent
pairs is a *phone-export* window constant, not a data gap. I should have checked the database
before scoping a capture change around its absence, and the Grok specification needs re-issuing
on that basis.

**And the binding constraint has moved.** It is no longer quotes. Of seven sessions, the decision
trail yields intents on **three**: 12 November has **zero**, 13 November **one**, and 16–17
November carry `authority_doc: null`. There are 124 sessions of chain data and 3 sessions of
decision trail.

---

## 1 · Coverage reconciliation — exact

`intent_ledger.py` reads the exports; `load_chain_result.py` parses the read; `intent_replay.py`
joins and prices. All read-only.

| count | mine | Codex | |
| --- | ---: | ---: | --- |
| recorded single-leg intents, 18–20 Nov | **232** | 232 | match |
| with a usable quote in the scheduled decision minute | **229** | 229 | match |
| buys | **139** | 139 | match |
| buys without `ENTRY_WINDOW_CLOSED` | **97** | 97 | match |
| of those, also with a quote 60 minutes on | **95** | 95 | match |
| intents with no quote series at all | **0** | — | |

Identity note: the chain table carries **no instrument key**, so identity is the tuple
`(session_date, index_key, expiry, strike, option_type)`. All 232 tuples are complete, every
session/index carries exactly **one** expiry (`2026-11-23`, matching each intent's `why.exp`), and
there were **no duplicate stamps** on any of the 67 contracts pulled (5,170 quotes, 0 malformed).

**`BROKER_SNAPSHOT_UNAVAILABLE` is on 232 of 232 intents** — every single one. Not one got through.

## 2 · The causal clock — why same-minute coverage is not an entry

This is the handoff's §2 requirement, and the data settles it:

* `decision_poll_ts` is a **schedule label**. Across all 694 polls in the seven exports,
  `published_at` is **never within a second of it**: min +3.4s, median **+26.6s**, p90 +33.1s.
* The chain polls at **6–8 seconds** past the minute (432 of ~460 polls on 18–20 Nov).
* So the quote that "covers" a decision was observed roughly **twenty seconds before the signal
  existed.**

**All 97 admissible buys have their causal entry on a later poll than the covering quote — 100%
of them.** Median entry delay 5 minutes (one poll), max 10.

A **frozen tolerance of 10 minutes** (two polls at the 5-minute cadence) bounds both marks: a
quote further than that from the instant sought is refused, never stretched. Without it "the first
quote at or after the target" would reach arbitrarily far forward and a declared 60-minute hold
could silently become hours. With it, the realised hold is **min 55, median 60, max 65 minutes**.

### The five traceable examples

**(1) Same-minute coverage fails the causal replay — the case the handoff asked for.**
20 Nov, BNF 63700 CE. Scheduled poll **12:00:00**, published **12:00:29**. The covering quote at
**12:00:08** shows ask **402.70** — but it was observed **21 seconds before the signal was
published**. The causal entry, the first usable quote at or after publication, is **12:05:07** at
ask **512.30**: **+109.60 points, 27.2% of the premium, higher than the price the coverage basis
would have claimed.** Exit 13:05:07 at bid 810.20, held 60 min, **net +₹8,833 (+294.44 points)**.

**(2) The same effect, second largest.** 20 Nov BNF 63500 CE, poll 11:55:00, published 11:55:24,
covering quote 11:55:07 ask 401.00, causal entry 12:00:08 ask **509.55** (+108.55, 27.1%). Exit
13:00:06 bid 908.35, net **+₹11,855**.

**(3) The gap runs both ways, so this is not a manufactured bias.** 20 Nov BNF 64300 CE, poll
12:40:00, published 12:40:32, covering quote 12:40:08 ask 453.95, causal entry 12:45:06 ask
**377.85** — **−76.10 points, i.e. the causal entry was cheaper.** Exit 13:45:07 bid 488.80, net
**+₹3,245**.

**(4) The worst scored trade.** 19 Nov BNF 62400 CE, published 10:15:34, entry 10:20:06 @ 504.55,
exit 11:20:06 @ 404.00, **net −₹3,097 (−103.23 points)**.

**(5) A refused entry.** 19 Nov BNF 63100 CE, published **15:40:25**. The last poll of the session
is **15:40:07**, so there is no quote at or after publication and the outcome is refused, not
approximated. All four refused entries are of this shape, every one published at 15:40:25–26.

### And the aggregate effect of the correction is small — which I will not overstate

| basis | n | net |
| --- | ---: | ---: |
| coverage (the look-ahead) | 95 | +₹272,850 |
| **causal (defensible)** | **97** | **+₹282,385** |
| difference | | **−₹9,536, i.e. −3.4%** |

The per-trade gaps reach ±27% of premium and move **every** entry, but they **largely cancel in
aggregate on this sample** — and the coverage basis comes out *lower*, not higher. The reason to
use the causal basis is that it is correct, **not** that it changes the answer here. I am saying so
explicitly because the opposite claim would have been the convenient one.

## 3 · Pass 1 — recorded buy intents vs WAIT, net of dated costs

Ask to buy, bid to sell, one declared tick of adverse execution each way, the governed dated fee
ledger, lot **BNF 30 / NF 65** from the ledger's own production authority. Reported in **option
points as well as rupees**, because the exports' `contract_lot_table` reference resolves to
`UNRESOLVED_REFERENCE` and points are lot-independent.

| population | scored | net ₹ | net points | mean ₹ | win% | return on risk | expectancy / eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| all 232 intents | 177 | +230,528 | +7,561.07 | +1,302 | 56% | 12.23% | +₹994 |
| 139 buys | 101 | +283,443 | +8,941.60 | +2,806 | 80% | 25.06% | +₹2,039 |
| **97 admissible buys** | **97** | **+282,385** | **+8,905.59** | **+2,911** | **81%** | **25.93%** | **+₹2,911** |
| **WAIT** | — | **+0** | **0** | — | — | — | **₹0** |

Unresolved and refused outcomes, kept visible: **51** intents have no exit quote within tolerance
(34 of them buys) — **every one from a 14:40 or later decision**, because 60 minutes past 14:40 is
15:40, the last poll. **4** have no entry quote (publication after the final poll). Nothing was
dropped silently.

### Then the robustness checks, which are where it falls apart

**Leave-one-session-out:**

| drop | remaining net | trades |
| --- | ---: | ---: |
| 18 Nov | +287,632 | 90 |
| 19 Nov | +272,348 | 78 |
| **20 Nov** | **+4,790** | **26** |

**One session carries 98.3% of the result.** Drop only 20 Nov BANKNIFTY and +282,385 becomes
+32,382. That session is the +2,007-point (3.2%) BANKNIFTY day.

**By side:**

| side | scored | net ₹ | net points | win% | return on risk |
| --- | ---: | ---: | ---: | ---: | ---: |
| CE | 90 | **+287,632** | +9,045.59 | 87% | 28.60% |
| PE | 7 | **−5,247** | −139.99 | **14%** | −6.29% |

Excluding 20 November: CE **+10,037** on 19 trades, PE **−5,247** on 7. **The put side loses on
every basis available**, and seven trades is not a sample.

**Per session / index / side:**

| cell | scored | net ₹ | net points | win% | return on risk |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-11-18 BNF PE | 2 | −3,303 | −110.08 | 0% | −12.49% |
| 2026-11-18 NF PE | 5 | −1,944 | −29.91 | 20% | −3.41% |
| 2026-11-19 BNF CE | 15 | +7,424 | +247.46 | 67% | 4.04% |
| 2026-11-19 NF CE | 4 | +2,613 | +40.20 | 100% | 7.56% |
| **2026-11-20 BNF CE** | 44 | **+250,003** | +8,333.43 | 89% | **44.10%** |
| 2026-11-20 NF CE | 27 | +27,592 | +424.49 | 93% | 12.53% |

**Concentration** is *not* the problem — the best 10 of 97 trades carry 54%, win rate is 81.4%,
median trade +₹963. **Session concentration is.** This is one day, not a handful of lucky fills.

**Adverse-fill sensitivity** (entry delayed a further 1 and 2 polls, decision unchanged):
+282,385 → **+261,947** → **+239,673**. It survives execution degradation comfortably. The put
side actually improves (−5,247 → −1,603 → +394), which tells you how little those 7 trades mean.

## 4 · Decision, per index and side

As the handoff requires: advance to fresh validation, repair a named evidence defect, or keep WAIT.

| index | side | n | decision | why |
| --- | --- | ---: | --- | --- |
| BNF | CE | 59 | **keep WAIT — repair the evidence defect first** | +257,427 total but 97% from one 3.2% trending session; 2 other sessions give +7,424 |
| NF | CE | 31 | **keep WAIT — repair the evidence defect first** | +30,205 total, of which +27,592 is 20 Nov; the remainder is 4 trades |
| BNF | PE | 2 | **insufficient evidence → WAIT** | 2 trades, both losses |
| NF | PE | 5 | **insufficient evidence → WAIT** | 5 trades, 1 win, −₹1,944 |

**No index/side advances to fresh validation on this evidence.** The after-cost expectancy is
genuinely positive and large, but it does not survive removal of the best session, the put path
has no result of its own, and the usable sample is three sessions because of a data defect rather
than because three sessions is all that exists.

**The named defect to repair is the decision trail, not the quotes.** 124 sessions of chain data
sit beside 3 sessions of intents. Fix that and this exact replay re-runs over months.

**Supported hold and exit rule:** none yet. Passes 4 and 5 need a population that is not one
session; running a 30/60/120 comparison or an early-exit policy on 20 November alone would fit
the exit rule to a single trend day. Deferred deliberately, not forgotten.

## 5 · Ranked app change list — smallest change that makes the next result trustworthy first

| # | change | owner | acceptance test |
| ---: | --- | --- | --- |
| **1** | **Persist `authority_doc` on every session.** 16 and 17 November carry `authority_doc: null`, and 12 November carries a trail with zero single-leg intents. This is the binding constraint on every pass in this handoff. | Grok (capture) | Five consecutive sessions export a non-null `parts.authority_doc.history[index]` with ≥ 60 polls per index, and `intent_ledger.py` reports intents on all five. |
| **2** | **Trace and repair `BROKER_SNAPSHOT_UNAVAILABLE`.** 232 of 232 intents blocked; the rule had a directionally-correct plan on every actionable poll and could never price it. Verify snapshot *freshness* separately from *availability*. | Grok (app) | One session where ≥ 80% of actionable polls reach a state other than `ELIGIBLE_PENDING_SNAPSHOT`, with the snapshot's own age recorded per poll. |
| **3** | **Record the publication clock in anything that will be replayed, and bind entries to it.** Publication is a median 26.6s after the scheduled poll; a replay or a live entry anchored on the poll label is anchored on a label. | Claude (replay, done) / Grok (app) | The app records `published_at` per decision (it already does) **and** any Paper entry records the mark it actually filled at, so `entry_poll ≥ published_at` is checkable in the export. |
| **4** | **Capture the intended *and* selected contract per poll.** The export records `why.sl.k`/`ot`; it does not record what was actually bound or filled. | Grok (app) | An export carries, per Paper entry, the intended tuple, the selected tuple, the quote used and its provenance. |
| **5** | **Extend the phone export's own window to 15:30+ and widen its ladder, or stop using it for economics.** The database already has both; the export's 3-strike 09:20 ladder cannot evidence the rule (0 of 233) and its 15:00 cut blocks overnight pairs. | Grok (capture) | `paper_intent_feasibility.py` on a new export reports `debit_priceable_share ≥ 0.95`; `next_session_match.py` returns ≥ 1 valid pair. |
| **6** | **Quote identity and units.** The chain table has no instrument key, `quantity_unit_status` is `UNKNOWN`, `iv.market_data` is null on every row, and `references.lot_resolution` is `UNRESOLVED_REFERENCE`. | Grok (capture) / Codex (review) | The chain carries an instrument key, and `field_inventory.py` reports the lot reference resolved. |

Items 1 and 2 are the only ones that change what the *next* result can say. Everything else is
housekeeping by comparison.

## 6 · Reproduce

```bash
# 1. the export side: intents, all clocks, provenance, and the read-only join as SQL
python3 -I tools/trade_hold_exit/intent_ledger.py \
    <export_2026-11-18.json> <export_2026-11-19.json> <export_2026-11-20.json> \
    --horizon-min 60 --out tools/trade_hold_exit/results --emit-sql
# 2. the database side, read-only. The emitted SQL is the reviewable join; the bulk pull used
#    here was the same predicate over the 47 named contracts, returned as CSV and saved to disk
#    (the MCP tool is the only route: the proxy refuses supabase.co, so no psql or PostgREST).
python3 -I tools/trade_hold_exit/load_chain_result.py <saved-result.txt>... \
    --out tools/trade_hold_exit/results/chain_cache.json
# 3. the replay: coverage reconciliation, causal marks, Pass 1 pricing
python3 -I tools/trade_hold_exit/intent_replay.py \
    --ledger tools/trade_hold_exit/results/intent_ledger_60min.json \
    --chain  tools/trade_hold_exit/results/chain_cache.json \
    --ledger-root <ledger-tree> --horizon-min 60 \
    --out tools/trade_hold_exit/results
# adverse-fill sensitivity
... --delay-polls 1   # and 2
```

### Source fingerprints

| source | sha256 |
| --- | --- |
| `export_2026-11-18.json` | `31b58b582751fd8390d25754d84d97d10c7bd597dc180e7371e42f6b7f560cf0` |
| `export_2026-11-19.json` | `0b50c96e47fddf0d7212ceb29b1200bb3b9fe7653e84ecc931699e99e70afbc2` |
| `export_2026-11-20.json` | `67e14b90b0fb08a2b0dfc8b5928bad49fbda34fc4f758722ec0751eaf6e6098a` |
| chain read, 18–19 Nov (saved MCP result) | `c95d27d0ea9416a257bc33dbb6e39b4856e82ef2304d19dee04ca88d12071a4c` |
| chain read, 20 Nov (saved MCP result) | `08c7685100c533460735974ee72b885958eadae78fec885e2015b2b4cd109f35` |

Database: project `fdynxkfxohbnlvayouje`, `public.ml_option_chain_snapshots`, **read-only**; no
write, migration or schema change of any kind. Raw exports stay out of the repository.

## 7 · Scope and what stays provisional

Offline research on a review branch; Supabase read-only. Every rupee is `PROXY_SIMULATED_FILL` —
**no order was placed and nothing here is a Paper position.** The exports contain **zero actual
Paper positions**, so Pass 1 measures what the rule *wanted*, never what it did.

18–20 November rests on the **unreviewed** fee revision
`nse_equity_index_options_rates_20261120_v3_UNREVIEWED`, held in a worktree and never committed to
a fee table; every run stamps `provisional_cost_basis`. The two fee extensions remain awaiting
review, and no promotion decision may rest on these rupees until they are reviewed.

Retrospective development evidence, outside the unsigned prospective v4 count. The existing
fresh-session lock is **unchanged and unversioned** by this delivery — nothing here met its bar,
so there was nothing to revise. No production write, `main` merge, build, release or pull request.

---

### What I would ask you to decide

1. **Accept that the usable sample is three sessions, and that this is a defect rather than a
   limit.** 124 sessions of chain data against 3 of decision trail. App change #1.
2. **`BROKER_SNAPSHOT_UNAVAILABLE` at 232 of 232** is now the most expensive single defect in this
   programme: it blocks execution *and* evaluation of the shipped rule. App change #2.
3. **Re-issue the Grok capture specification.** Two of its six requirements are already met in the
   database; I scoped them against the export alone and should have checked first.
4. **Confirm the 10-minute frozen tolerance** (§2) before passes 4 and 5 are built on it.
5. **Agree to defer passes 4 and 5** until the population is not one session. Fitting a hold
   horizon or an exit rule to 20 November would produce a number I would not defend.
