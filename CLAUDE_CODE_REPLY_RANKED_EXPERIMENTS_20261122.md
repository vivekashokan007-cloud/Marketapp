# Claude Code reply — the ranked experiments, and the capture finding that outranks them

Market Radar · 22 November 2026 IST · research owner's edge-research handoff, sections 2–3
**Branch** `review/premium-model-validation-20261121` · `670c621` the ranked research map →
this memo's code, results and the fresh-session lock

---

## The short version

**Rank 1 is infeasible, and finding that out is the most valuable result in this memo.** The plan
was to price the production Paper rule's own `BUY_CE`/`BUY_PE` intents — debit legs, no margin
needed, testing the shipped product rather than a signal I invented. **Not one of its 233
recorded intents can be priced from these captures.** The cohort pins three strikes near the
09:20 spot and holds them all session; the rule picks a strike near the *live* spot at each poll.
Median gap **400 points**, worst **1,700**. The capture cannot evidence the production rule's own
decisions, and the gap is widest on exactly the sessions where those decisions matter most.

**Rank 3 ran and is a clean negative.** Decision-time relative spread and book imbalance do not
order the net on either index, in any quintile. Worse, relative spread has the **opposite sign on
each index** — dropping the widest quintile would have helped BANKNIFTY (+1,703 mean in that
quintile) and hurt NIFTY (−182) — because on NIFTY the widest quintile holds contracts at **0.44×
the mid price of the rest**. It is sorting the chain by moneyness, not measuring fill quality.

**Rank 2 needs calendar time, not code,** and the fresh-session test is now locked around it.

**One defect found and fixed** in my own probe before it reached the map: the field-availability
verdict called a field `PARTIAL` with *zero* present rows whenever its empty rows mixed null and
absent. That is exactly `iv.market_data`, and "partial" would have invited an experiment on a
field that never once carried a value.

---

## 1 · Rank 1 — refused, on verified grounds

`paper_intent_feasibility.py`. The research map named this the highest-value item, so the first
thing to establish was whether the exports hold a quote for what the rule asked for.

| session | idx | debit intents | **priceable** | credit intents | priceable | pinned ladder | the rule's chosen strike, points off that ladder |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 13 Nov | NF | 1 | **0** | 0 | 0 | *(no cohort)* | — |
| 18 Nov | BNF | 2 | **0** | 14 | 0 | 62200 / 62300 / 62400 | −200 … −400 |
| 18 Nov | NF | 5 | **0** | 27 | 0 | 25300 / 25350 / 25400 | −50, −100 |
| 19 Nov | BNF | 25 | **0** | 20 | 0 | 62000 / 62100 / 62200 | +200 … **+900** |
| 19 Nov | NF | 14 | **0** | 32 | 0 | 25150 / 25200 / 25250 | +50 … +300 |
| 20 Nov | BNF | **55** | **0** | 0 | 0 | 62700 / 62800 / 62900 | +200 … **+1,700** |
| 20 Nov | NF | 38 | **0** | 0 | 0 | 25400 / 25450 / 25500 | +50 … +300 |
| **total** | | **140** | **0 (0.0%)** | **93** | **0** | | median **400**, worst **1,700** |

**The mechanism is structural, not a glitch.** The cohort is `premium_capture_cohort_v2_three_paired_strikes_validated`:
it picks three strikes nearest the spot at the **09:20 selection slot** and pins them for the
session. The Paper rule picks a strike near the spot *now*. On a quiet day they nearly coincide;
on 20 November, when BANKNIFTY rose 2,007 points, the rule was asking about strikes up to 1,700
points away from a ladder fixed at the open.

**I did not substitute the nearest pinned strike.** That would price a different trade and
attribute it to the rule. The probe records the offsets and refuses.

Three consequences:

1. **No evidence about the shipped rule's economics exists in this data, at all.** Not "weak
   evidence" — none. Every earlier statement I have made about the Paper rule has been about its
   *intent* and its *blocking reasons*, and that is the ceiling until the capture changes.
2. **This compounds `BROKER_SNAPSHOT_UNAVAILABLE`.** The rule was blocked 192 times from
   *executing*, and separately cannot be *evaluated*. Two independent failures, same rule.
3. **It is the single most important input to the Grok capture specification**, returned
   separately: an exact-contract capture must follow the rule's chosen strikes, not a ladder
   fixed at the open. A capture that pins at 09:20 is structurally unable to evidence a rule that
   tracks the spot.

## 2 · Rank 3 — ran, and it is a negative with a mechanism

`execution_filter.py`. The handoff's own framing: "separate a model signal from an execution
filter whose advantage comes from avoiding poor fills." This is the second kind, and it is a
**diagnostic on trades the causal replay has already scored** — no new arm, no new decision rule,
so there is nothing here that could be fitted into a strategy.

3,815 scored trades of 4,769, both indices, 60-minute horizon, quintiles of the sample's own
distribution.

### Mean net ₹ per trade, by quintile

| scope | feature | q1 | q2 | q3 | q4 | q5 | ordered? |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| pooled | relative spread | −131 | +61 | +18 | +12 | **+903** | **no** |
| pooled | book imbalance | +215 | −15 | −42 | +286 | +132 | **no** |
| BNF | relative spread | −51 | +24 | +192 | −69 | **+1,703** | **no** |
| BNF | book imbalance | +505 | +115 | +535 | +205 | — | **no** |
| NF | relative spread | −106 | −113 | +94 | +238 | **−182** | **no** |
| NF | book imbalance | −23 | +24 | −96 | −16 | +38 | **no** |

**Nothing is ordered anywhere.** A filter whose buckets jump around is not measuring anything.

### And the widest quintile points opposite ways on the two indices

| index | widest-spread quintile | the other four |
| --- | --- | --- |
| BNF | n=381, mean **+1,703**, win 49% | n=1,526, mean +24, win 44% |
| NF | n=382, mean **−182**, win 35% | n=1,526, mean +28, win 48% |

An execution filter cannot help one index and hurt the other by dropping the same quintile. So I
checked what the quintile actually contains — mean mid price per bucket:

| index | q1 | q2 | q3 | q4 | q5 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BNF | 560 | 538 | 514 | 533 | **660** |
| NF | 154 | 156 | 161 | 150 | **69** |

**On NIFTY the widest-spread quintile holds contracts at 0.44× the mid price of the rest** — the
cheap, far-from-money ones — and they lost. On BANKNIFTY the widest quintile is the *dearest*,
and on a +2,007-point day it won. **Relative spread is a moneyness proxy here, and the net
follows which contracts it happened to select.** The counterfactual is labelled
`IN_SAMPLE_COUNTERFACTUAL_NOT_A_RESULT` in the artifact for the same reason: the quintile
boundary was cut from the very rows it is being scored on.

Two caveats that travel with the numbers rather than sitting in a footnote:

* **`quantity_unit_status` is `UNKNOWN` on all 4,769 records**, so book size is used only as a
  ratio, where the unit cancels. No quantity claim is made.
* 954 opportunities had no usable two-sided decision quote and are excluded by reason code
  rather than treated as wide.

**Verdict: rank 3 is closed as a negative.** What would change my mind is a capture carrying an
exchange or vendor timestamp, which would let staleness be separated from spread — and that field
is absent on all 4,781 records.

## 3 · Rank 2 — no experiment to run, and that is the finding

The direction baseline's entire gain is call-side, in all eight arm/index cells. Resolving whether
the signal is directional or long-biased requires a sample containing large **down** days. The
tool and its challenge block already compute the answer; the span contains one `BEAR` session and
it lost on both indices.

There is no code to write and no cleverness that substitutes for the sessions. So instead of
running something adjacent and calling it progress, the question is now the axis of the locked
fresh-session test.

## 4 · The locked fresh-session test

`tools/premium_model_validation/FRESH_SESSION_TEST_LOCK_20261122.md`, committed **before any
fresh session is viewed**, as section 3 required. In brief:

* **Two finalists, deliberately.** `DIRECTION_ONLY` (F1) and `DIRECTION_PLUS_BOTH` (F2), with
  `WAIT` as control. Three sessions of development evidence do not support more, and every extra
  finalist is another chance to be right by accident.
* **Six candidates are explicitly excluded with a stated reason each**, so none quietly returns:
  every forecast arm (all flip on leave-one-out), `RANGE_EV` alone including the +16,433
  exception (5% of its book has no exit quote), all four debit verticals (all lose per-leg),
  strike selection alone (−2.1% / −3.4%), confirmation thresholds (233 actionable polls is not
  enough to tune on), and the Paper rule's intents (unpriceable, §1).
* **Nothing can be fitted.** Neither finalist has a free parameter. The signal may read
  `direction.value` and nothing else — not `strength`, `er`, `z_open`, `z_30` or `regime`.
* **F1's criterion is the put side specifically**, not the total: PASS needs the put side
  profitable after costs across at least three sessions with ten or more `BEAR` signals each.
  A positive total on one down day is **NOT PROVEN**, and I will say so rather than bank it.
* **F2's criterion is return on risk, not net rupees**, because it trades less by design and the
  development evidence already showed it loses on net while winning on return on risk.
* **Stopping rules are fixed now**: stop at three qualifying sessions; stop early and report FAIL
  if the put side is worse than −₹50,000 on either index; stop early and report NOT PROVEN if six
  qualifying sessions produce fewer than three usable `BEAR` sessions; a session failing the
  integrity bar is reported as excluded and **not replaced by looking for a better one**; no new
  arm, field, threshold or horizon may enter mid-test.
* **Repeated attempts are controlled by counting them.** Every report states how many batches
  have been run.

## 5 · Reproduce

```bash
# rank 1 feasibility -- the refusal, with its offsets
python3 -I tools/premium_model_validation/paper_intent_feasibility.py <export...> --out <out>
# rank 3 -- the execution-filter diagnostic
python3 -I tools/premium_model_validation/execution_filter.py \
    --span <dir-of-exports> --spots <spots> --ledger-root <tree> --out <out>
# the field probe the map is ranked on
python3 -I tools/premium_model_validation/field_inventory.py <export...> --out <out>
cd tools/premium_model_validation && python3 -m unittest discover -s tests   # 240
```

| file | sha256 |
| --- | --- |
| `paper_intent_feasibility.py` | `b5a859dcdd654156dda522d94e9a38439967d1c9d661e1d3ea63ac218e470b98` |
| `execution_filter.py` | `60ca3b5597441a8ee5de7b6c68c435f3588f70c1ff4387d562364d59f4780d7f` |
| `field_inventory.py` | `02c8b5738d45c6036b6270d786c318d3cb553286f381d21c59818beb0ce10b6a` |
| `direction_baseline.py` | `1217ef10d37d9d87c4ba270731a730ea340200a73fed8826118029091ee8421e` |

## 6 · Scope

Offline research on a review branch, source data read read-only. Every rupee is
`PROXY_SIMULATED_FILL`; 17–20 November rests on the **unreviewed** fee revision. Retrospective,
outside the unsigned prospective v4 count. No raw export, Paper position or credential in the
tree. No production write, `main` merge, build, release or pull request.

---

### What I would ask you to decide

1. **The capture's fixed 09:20 strike ladder is the binding constraint on this research**, not
   the models. It blocks evaluating the shipped rule entirely (§1), it is why strike selection has
   nothing to choose between (−2.1%/−3.4%), and it is why the only tradeable universe is six
   instruments. If one capture change is made, make this one.
2. **Accept rank 3 as closed** (§2), and note that reopening it needs a vendor or exchange
   timestamp, not more analysis.
3. **Review the locked fresh-session test** (§4) before any fresh session is scored. If you want
   a different finalist, a different criterion or a different stopping rule, now is the only time
   it can be changed without invalidating the test.
4. **`BROKER_SNAPSHOT_UNAVAILABLE` still has no owner.** 192 blocks across four sessions, and it
   is the second of two independent failures on the same production rule.
