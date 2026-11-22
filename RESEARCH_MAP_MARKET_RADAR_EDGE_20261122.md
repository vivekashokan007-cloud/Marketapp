# Market Radar research map — where an edge could be, ranked by what the data can actually test

Market Radar · 22 November 2026 IST · research owner's edge-research handoff, section 2
**Branch** `review/premium-model-validation-20261121` · companion memos:
`CLAUDE_CODE_REPLY_PREMIUM_SPAN_EXTENDED_20261122.md` (corrected replay) and
`CLAUDE_CODE_REPLY_DIRECTION_BASELINE_20261122.md` (the direction baseline)

---

## How this map was ranked, and why I am not the one who ranked it

The handoff said to "verify which data fields exist before relying on them." So I counted them
first, over all 4,781 observations and 694 decision polls in the seven exports, before writing a
word of this map. `field_inventory.py`, committed with its own guards, reports each field as
`USABLE`, `PARTIAL`, `UNUSABLE_ALWAYS_NULL` or `UNUSABLE_ABSENT`.

The answer removed most of the choices. **Three of the six families are blocked outright by
fields that are not there**, and one more is blocked by a window constant. The ranking below is
very nearly forced by the probe; where my judgement enters at all, I say so.

### The probe's verdict, in full

| state | fields |
| --- | --- |
| `UNUSABLE_ABSENT` — the key is not in any record | `volume`, `oi`, `oi_change`, `exchange_timestamp`, `vendor_timestamp`, `greeks.delta`, `greeks.theta`, `greeks.vega`, session `margin`, session `margin_evidence` |
| `UNUSABLE_ALWAYS_NULL` — present, empty on every row | `iv.market_data` (null on 4,769 of 4,769), `why.ml.selected` (null on 467) |
| `USABLE` — on every row | `outcome`, `identity.verified`, `direction.value`, `regime`, `spot.value`, `spot.asof`, `why.act`, `why.dte`, `why.expiry` |
| `PARTIAL` — usable where present | `bbo.bid`/`ask`/`ltp`/`bid_qty`/`ask_qty`/`book_status` (4,769 of 4,781), `iv.greeks` (4,769), `direction.er`/`z_open`/`z_30` (664 of 694), `direction.strength` (**233** — null on NEUTRAL), `why.sl.*` (233), `cohort.*` (12 of 14 session/index nodes) |

Two cross-checks that the probe passed and that make me trust it: the **12** records missing
every quote field are exactly six pinned contracts × two indices on one poll — the 13:25 failure
on 20 November that §6 of the span memo reported — and the two session/index nodes missing
`cohort.*` are 16 and 17 November, the `authority_doc: null` pair.

**One defect found while writing the probe's guard, and fixed before it reached this map:** my
verdict logic was calling a field `PARTIAL` whenever its empty rows were a mix of null and
absent, even with **zero** present rows. That is exactly `iv.market_data`. "Partial" would have
invited an experiment on a field that never once carried a value.

---

## The ranked map

Ranked by **what can be disproved with the data in hand**, then by plausibility, then by effort.
Rank 1–3 are runnable now. Rank 4–5 need one capture change each. Rank 6–8 are blocked and I
say by what.

### Rank 1 — Price the Paper rule's own `BUY_CE` / `BUY_PE` intents

| | |
| --- | --- |
| **family** | strategy and risk |
| **mechanism** | the production rule already emits a directional single-leg intent per poll. If that intent makes money after costs, the edge is in the shipped rule and the research is about unblocking it, not about finding a new signal. |
| **benchmark** | `DIRECTION_ONLY` from the baseline memo, and `WAIT` |
| **data** | `why.sl.action`, `why.sl.strike`, `why.sl.state` on **233 polls** across four sessions. Debit legs need **no** margin evidence, which is what kept the credit legs refused. |
| **disproving test** | the intents lose, or win only on the call side, as the direction baseline did |
| **first comparison** | each `BUY_CE`/`BUY_PE` intent, filled one poll later, exited at +60 min on the same ledger — against the baseline's nearest-the-money rule on the same polls |
| **why rank 1** | it tests the shipped product, the fields are verified present, and it needs no new capture. It is also the one place the answer could be "the app was right and we blocked it" |
| **caveat** | 192 of those polls were blocked by `BROKER_SNAPSHOT_UNAVAILABLE`, so this measures what the rule *wanted*, not what it did. Needs its own predeclaration before a line of code. |

### Rank 2 — Does the BEAR side ever work? A down-sample test

| | |
| --- | --- |
| **family** | direction and market conditions |
| **mechanism** | the direction baseline's entire gain is call-side. Either the signal is directional and the sample is the problem, or the signal is a long bias. One sample with large down days separates them. |
| **benchmark** | the frozen `DIRECTION_ONLY`, unchanged, re-run |
| **data** | `direction.value` is `USABLE` on all 694 polls. The constraint is **sessions, not fields** — the span has one BEAR session and it lost. |
| **disproving test** | the BEAR side loses again on a sample with several large down days |
| **first comparison** | `DIRECTION_ONLY` split by signal side, per session, on any new captures |
| **why rank 2** | highest-value question in the whole map, and it needs **no new code at all** — the tool and its challenge block already compute the answer. It needs calendar time. |
| **caveat** | nothing can be done to hurry it. This is the honest shape of the sample problem. |

### Rank 3 — Spread and quote size as an execution filter, not a signal

| | |
| --- | --- |
| **family** | chain, flow and execution |
| **mechanism** | the handoff's own distinction: an advantage that comes from avoiding poor fills rather than from predicting direction. A wide spread or a thin book at the decision predicts a bad round trip regardless of which way the index goes. |
| **benchmark** | `DIRECTION_ONLY` and `RANGE_EV` unchanged, with the filter applied on top |
| **data** | `bbo.bid`, `bbo.ask`, `bbo.bid_qty`, `bbo.ask_qty`, `bbo.book_status` on 4,769 of 4,781 — all verified present. **`quantity_unit_status` is `UNKNOWN`**, so quote *size* is in unauthenticated units and only its *relative* ordering is usable. |
| **disproving test** | filtering on decision-time spread or book depth does not reduce the loss rate, or reduces it only by trading less |
| **first comparison** | bucket every scored trade in the existing replay by decision-time relative spread and by book side ratio, and compare net per trade across buckets. **This is a diagnostic on data already scored — no new arm, no new decision rule.** |
| **why rank 3** | genuinely new information, present in volume, and the cheapest of the three runnable items |
| **caveat** | it is an execution filter. If it works it reduces cost, it does not create edge, and it must be reported that way. |

### Rank 4 — Relative implied volatility, as a *change*, never as a level

| | |
| --- | --- |
| **family** | option pricing and volatility |
| **mechanism** | paying less for the same forecast is the cheapest possible edge. If decision-time IV relative to its own recent path predicts the round trip, the premium model gains an input it does not have. |
| **benchmark** | the existing `RANGE_EV` with no IV input |
| **data** | **`iv.greeks` on 4,769 rows, and that is the only IV there is.** `iv.market_data` is null on every row, `iv_source` is `GREEKS_PRESENT_ONLY`, `iv.unit_status` is `UNKNOWN` and `iv_authoritative_resolved` is `false` on every row. |
| **disproving test** | IV-relative buckets show no difference in net per trade |
| **first comparison** | same bucketing as rank 3, on each contract's IV minus its own session mean |
| **why rank 4, not higher** | the unit is unauthenticated, so **no absolute or cross-contract IV claim is permissible** — only within-contract relative movement. That is a real restriction and it is why this sits below an execution filter with clean units. |
| **blocker to lift** | have the capture record the vendor's `iv` field, or verify the greeks IV's unit once against a known contract. Either turns this into a rank-2 candidate. |

### Rank 5 — Confirmation and hysteresis on the signal, against their cost in missed trades

| | |
| --- | --- |
| **family** | direction and market conditions |
| **mechanism** | the handoff asks directly: does requiring two consecutive polls, or a `STRONG` rather than `MODERATE` reading, pay for the delay? |
| **benchmark** | `DIRECTION_ONLY`, which requires neither |
| **data** | `direction.value` on 694 polls, `direction.strength` on **233 only** (it is null on NEUTRAL, so the field's row count *is* the actionable-poll count) |
| **disproving test** | confirmation reduces trades without improving net per trade, or improves net per trade by less than it loses in missed entries |
| **first comparison** | `DIRECTION_ONLY` with a two-poll persistence requirement, and separately with `strength == STRONG`, on the same population |
| **why rank 5** | cheap and well-specified, but it is **tuning on three sessions**, and with 233 actionable polls across two indices a two-poll filter could easily leave a double-digit sample. High risk of fitting noise. |
| **condition** | I would not run this until rank 2 has a wider sample. Running it now is how a parameter gets fitted to two up days. |

### Rank 6 — Holding window beyond the session, and anything overnight

**Blocked by a constant, not by data.** The capture's declared window is `expected_slots: 69`,
ending at 15:00, while the overnight matcher wants a close mark in 15:10–15:30. The span memo
proved this on **48 real adjacent pairs**, every one refused for `no_close_mark_in_window`. The
13 November logs show polls were actually submitted at 15:05 through 15:40 — **the data exists
and the cohort window discards it.** This is an admission change, not a collection problem.

Within the session, the holding-window question is already answered and the answer is a warning:
return on risk rises monotonically with the hold (1.86% → 48.84% from 15 to 120 minutes on BNF),
which is exposure to drift rather than a timing edge. Longer holds also drop late decisions —
120 minutes scores 69 orders against 93 at 60 — so a longer window buys drift exposure and loses
population.

### Rank 7 — Contract and expiry selection

**Blocked by the capture's shape.** The cohort pins **three strikes and one expiry** per index.
Strike selection has therefore already been tested about as far as it can be: `DIRECTION_PLUS_SELECT`
kept every order and moved net by **−2.1%** (BNF) and **−3.4%** (NF). Expiry comparison is simply
impossible — there is one expiry per session, and `references.lot_resolution` is
`UNRESOLVED_REFERENCE` on every node, so even lot size is unauthenticated. Moneyness and skew
need a wider chain than six instruments.

**Blocker to lift:** capture a wider strike ladder and a second expiry. Until then nothing in
this family can be disproved, which is why it is not rank 4.

### Rank 8 — Credit structures, protected or otherwise

**Blocked outright and now counted.** Session `margin` and `margin_evidence` are
`UNUSABLE_ABSENT` on **all 14** session/index nodes. This is the same refusal that has stood for
weeks, and the probe turns it from a recollection into a count. Credit verticals and the Paper
rule's `SELL_CE`/`SELL_PE` legs stay refused. **Note that the Paper rule's *debit* legs do not
need this, which is precisely why they are rank 1.**

---

## What is dead, and should not be proposed again without new capture

| idea | why it is dead | what would revive it |
| --- | --- | --- |
| open-interest or volume flow | `oi`, `oi_change`, `volume` absent on **all 4,781** records | capture them |
| delta, theta or vega anything | `greeks.*` absent on all 4,781 | capture them |
| quote staleness, freshness gating | `exchange_timestamp` and `vendor_timestamp` absent on all 4,781 — the same gap that makes the production path report `FRESHNESS_UNVERIFIED:EXCHANGE_TIMESTAMP_MISSING` | capture a vendor or exchange stamp |
| absolute or cross-contract IV | `iv.market_data` null on all 4,769; the only IV has `unit_status: UNKNOWN` | capture vendor IV, or verify the unit once |
| any credit structure | margin absent on all 14 nodes | capture margin evidence |
| expiry selection | one expiry per session | capture a second expiry |

---

## Preserved: what has already failed, so it is not re-run by accident

| tried | result | would change my mind |
| --- | --- | --- |
| median-forecast rule, six sessions | positive totals, every one more than entirely one session; all flip on leave-one-out | a sample where no single session carries the total |
| `RANGE_EV`, six sessions | BNF survives every leave-one-out at +16,433 — but 5% of the book has no exit quote and −₹2,152 on those erases it | fix the capture window, re-measure |
| two-leg debit verticals | all four structures lose on per-leg dated costs; NIFTY bull call −₹42.8 a trade | nothing in this sample |
| strike selection by the premium model | −2.1% and −3.4% of net, every order kept | a wider strike ladder |
| the premium model as a filter | does not add at matched risk; halves the position and keeps most of the net | treat it as a size governor and test it as one |
| the app's direction signal | beats WAIT on both indices, survives every leave-one-out — **and the gain is call-side only in all eight cells** | a sample with large down days (rank 2) |

---

## Scope

Offline research on a review branch, source data read read-only. No production write, `main`
merge, build, release or pull request. No raw export, Paper position or credential in the tree.
Every rupee quoted anywhere in this map is `PROXY_SIMULATED_FILL` and 17–20 November additionally
rests on the **unreviewed** fee revision. Codex reviews this before any decision to change
recommendations.

### What I would ask you to decide

1. **Approve a predeclaration for rank 1**, the Paper rule's own debit intents. It is the only
   item that tests the shipped product with fields already verified present.
2. **Accept that ranks 6, 7 and 8 are capture changes, not research tasks**, and decide which of
   the three capture additions is worth asking for: the 15:00 window constant (rank 6, cheapest
   and already proven), a wider strike ladder plus second expiry (rank 7), or vendor IV and a
   vendor timestamp (rank 4 and the dead list).
3. **Agree that rank 5 waits for rank 2.** Tuning confirmation thresholds on three sessions, two
   of which went the same way, is how this research would start fooling itself.

---

### Reproduce the probe this map is ranked on

```bash
python3 -I tools/premium_model_validation/field_inventory.py <export...> \
    --out tools/premium_model_validation/results/span
cd tools/premium_model_validation && python3 -m unittest discover -s tests   # 218
```

| file | sha256 |
| --- | --- |
| `field_inventory.py` | `02c8b5738d45c6036b6270d786c318d3cb553286f381d21c59818beb0ce10b6a` |
| `direction_baseline.py` | `1217ef10d37d9d87c4ba270731a730ea340200a73fed8826118029091ee8421e` |
| `index_direction.py` | `64e6ed9491b87ae9d7133fed8c1067bf2eea1737789af9054938bb6393abd099` |
| `stress_range_ev.py` | `5b4a977d07f38c0042f1025b07d704b7ab3b8d425cd497df29129227b286bbee` |
