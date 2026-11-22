# Claude Code reply — the direction-only baseline, and the first thing that beats WAIT

Market Radar · 22 November 2026 IST · research owner's edge-research handoff, section 1
**Branch** `review/premium-model-validation-20261121` — `133eeb5` per-leg spread fees and the
leave-one-out grid → `bb1bf9d` realised index direction → `badb3f1` **the predeclaration, alone**
→ this memo's code and results

---

## The short version

**`DIRECTION_ONLY` beats `WAIT` on both indices, and it is the first arm in this entire
exercise to do so.** +₹318,743 on BANKNIFTY at 17.0% return on risk, +₹51,586 on NIFTY at 4.5%,
and unlike every premium arm it stays positive under **every** leave-one-session-out.

**And it is not a directional edge.** The put side loses in all eight arm/index cells. The one
session the signal called BEAR, it lost money on both indices. Return on risk rises monotonically
with the holding period — 1.9% at 15 minutes, 4.5% at 30, 17.0% at 60, 48.8% at 120 — which is
the signature of a long option held through drift, not of a timing edge. **What this measures is
long calls in a three-session sample containing a +2,007-point BANKNIFTY day.**

**The premium model does not add.** It cuts risk by 61% (BNF) and 47% (NF) and keeps most of the
net, which improves return on risk on NIFTY from 4.54% to 7.60% — but it does not beat the
baseline on net rupees at matched risk, which is what the predeclaration required.

**One app finding worth more than any of the above:** the current Paper rule recorded a
directionally-correct single-leg intent on nearly every poll — `BUY_CE` 47 times out of 47
actionable polls on 20 November, `SELL_CE`/`BUY_PE` on the 18th — and was blocked **every single
time** by `BROKER_SNAPSHOT_UNAVAILABLE`, 192 times across the four sessions.

---

## 1 · What was frozen, and when

`PREDECLARATION_DIRECTION_BASELINE_20261122.md`, committed **alone** in `badb3f1` before
`direction_baseline.py` existed. A test checks that ordering against git history. It fixes the
signal, the contract rule, the exit, the five arms, what counts as a win, and the population —
including the exclusions, which were stated before anything was scored.

**The signal is the app's own `index_direction_v1`**, read from
`parts.authority_doc.history[index][poll].direction.value` as published at that poll. It is the
production signal, not a reconstruction, and it is **causal by construction with the export
proving it**: `decision_poll_ts`, `computed_at` and `receipt_ts` sit at the poll, and
`lineage.open_ref_ts` / `lineage.anchor_30_ts` name the *earlier* instants `z_open` and `z_30`
are measured against. A test asserts both references are at or before the poll on every scored
row, and it passes on all eight cells.

**Only `direction.value` is read.** `strength`, `er`, `z_open`, `z_30` and `regime` all exist in
the trail and are all tempting. They are recorded as evidence and read by no arm; a test
inspects `choose()` and fails if any of them appears in it. Putting a threshold on them is a
separate experiment that needs its own predeclaration.

The contract rule is nearest-the-money among the three pinned strikes, fixed in advance
precisely because choosing otherwise is what the premium model is given the chance to do. Exit,
fill delay, fee model and capital ledger are the causal replay's, unchanged, 60-minute primary.

## 2 · The population, which is small and was declared small

| session | signal | slots scored | values |
| --- | --- | ---: | --- |
| 12 Nov | `index_direction_v1` | 54 BNF / 53 NF | **all NEUTRAL** (52/51) + 2 UNAVAILABLE |
| 13 Nov | present | **0** | has the signal, has no premium records |
| 16 Nov | **`authority_doc: null`** | — | **no signal at all** |
| 17 Nov | **`authority_doc: null`** | — | **no signal at all** |
| 18 Nov | `index_direction_v1` | 69 / 69 | BEAR 11 BNF / 27 NF, rest NEUTRAL |
| 19 Nov | `index_direction_v1` | 69 / 69 | BULL 38 / 39 |
| 20 Nov | `index_direction_v1` | 68 / 68 | BULL 47 / 30 |

So the arms trade on **three sessions**, not six: 12 November is entirely NEUTRAL and takes no
position at all. Of those three, **two are the big up days and one is the down day.** That is the
whole evidence base, and no amount of rupees makes it bigger.

## 3 · The result

Pooled over the signal sessions, both indices, 60-minute primary:

| idx | arm | orders | scored | net ₹ | risk ₹ | return on risk | sessions W/L |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| BNF | **DIRECTION_ONLY** | 93 | 70 | **+318,743** | 1,870,467 | **17.04%** | 2 / 1 |
| BNF | DIRECTION_PLUS_SELECT | 93 | 70 | +312,087 | 1,768,686 | 17.65% | 2 / 1 |
| BNF | DIRECTION_PLUS_BOTH | 36 | 27 | +97,759 | 728,042 | 13.43% | 2 / 1 |
| BNF | DIRECTION_PLUS_DECLINE | 34 | 25 | +90,423 | 640,412 | 14.12% | 2 / 1 |
| BNF | WAIT | 0 | 0 | **+0** | 0 | — | 0 / 0 |
| NF | **DIRECTION_ONLY** | 87 | 67 | **+51,586** | 1,137,110 | 4.54% | 2 / 1 |
| NF | DIRECTION_PLUS_SELECT | 87 | 67 | +49,838 | 1,068,408 | 4.66% | 2 / 1 |
| NF | DIRECTION_PLUS_BOTH | 43 | 35 | +45,374 | 597,382 | **7.60%** | 2 / 1 |
| NF | DIRECTION_PLUS_DECLINE | 42 | 34 | +43,394 | 575,594 | 7.54% | 2 / 1 |
| NF | WAIT | 0 | 0 | +0 | 0 | — | 0 / 0 |

Per session, and the leave-one-out that killed every premium arm in the previous memo:

| idx | arm | 12 Nov | 18 Nov | 19 Nov | 20 Nov | total | worst LOO |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BNF | DIRECTION_ONLY | 0 | **−31,164** | +59,606 | +290,301 | +318,743 | **+28,442** |
| BNF | DIRECTION_PLUS_BOTH | 0 | −5,655 | +29,742 | +73,672 | +97,759 | +24,087 |
| NF | DIRECTION_ONLY | 0 | **−26,308** | +39,354 | +38,540 | +51,586 | **+12,232** |
| NF | DIRECTION_PLUS_BOTH | 0 | −4,157 | +14,286 | +35,245 | +45,374 | +10,129 |

**Every direction arm stays positive under every leave-one-out, on both indices.** In the
previous memo every premium arm except BANKNIFTY `RANGE_EV` flipped sign the moment 20 November
was removed. These do not. On NIFTY the two winning sessions are nearly equal (+39,354 and
+38,540), so there is no single dominant day at all — which is a materially different shape of
result from anything earlier in this work.

That is the strongest-looking number this research has produced. So here is why it still is not
an edge.

## 4 · The challenge, which it fails

### 4.1 · The gain is call-side only, in all eight cells

| idx | arm | calls n | calls ₹ | puts n | puts ₹ | directional? |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| BNF | DIRECTION_ONLY | 60 | **+349,908** | 10 | **−31,164** | **no** |
| BNF | DIRECTION_PLUS_SELECT | 60 | +342,018 | 10 | −29,930 | no |
| BNF | DIRECTION_PLUS_BOTH | 25 | +103,414 | 2 | −5,655 | no |
| BNF | DIRECTION_PLUS_DECLINE | 23 | +96,229 | 2 | −5,806 | no |
| NF | DIRECTION_ONLY | 44 | **+77,894** | 23 | **−26,308** | **no** |
| NF | DIRECTION_PLUS_SELECT | 44 | +74,637 | 23 | −24,799 | no |
| NF | DIRECTION_PLUS_BOTH | 24 | +49,531 | 11 | −4,157 | no |
| NF | DIRECTION_PLUS_DECLINE | 23 | +47,464 | 11 | −4,071 | no |

**The put side loses in every cell.** The signal's BEAR calls are the only test of whether it
works in both directions, and they cost money on both indices. The total is calls minus puts —
BNF +349,908 − 31,164 — so the arm is a long-call rule with a loss-making short-side appendage.
In a sample with two large up days and one down day, that is the confound, not the finding. It is
the same trap §3.1 of the previous memo identified in the forecast filter, now reproduced on the
app's own production signal.

A test asserts this is true in every cell and fails if it ever stops being true — in which case
the memo gets rewritten, not the test relaxed.

### 4.2 · Return on risk rises monotonically with the hold

| holding period | BNF DIRECTION_ONLY return on risk | NF |
| --- | ---: | ---: |
| 15 min | 1.86% | 0.49% |
| 30 min | 4.48% | 1.82% |
| **60 min (primary)** | **17.04%** | **4.54%** |
| 120 min | **48.84%** | 13.43% |

Nothing about entry timing improves as the hold lengthens. What lengthens is exposure to the
index move. A timing edge would show up at the short horizons and decay; this does the opposite.
This is drift captured by a long option, and the longer you hold it in a rising market the more
you get. A test asserts the monotonicity so that a future sample breaking it is noticed.

### 4.3 · The hole, again — and NIFTY does not survive it

Same structural issue as the premium arms: filled positions whose exit quote never arrived.

| idx | orders | scored | unresolved | total ₹ | needs each unresolved to average | worst scored loss | survives? |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| BNF | 93 | 70 | 22 | +318,743 | **−14,488** | −5,293 | **yes** — 2.7× worse than anything observed |
| NF | 87 | 67 | 20 | +51,586 | **−2,579** | −5,210 | **no** — well inside the observed loss range |

So BANKNIFTY's gain is robust to the missing exits and **NIFTY's is not**. That asymmetry is
reported rather than averaged away.

### 4.4 · Concentration

| idx | total | without best 1 | without best 3 | without best 5 | win rate | best trade |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BNF | +318,743 | +295,416 | +252,368 | +214,678 | 80.0% | +23,328 |
| NF | +51,586 | +45,925 | +35,448 | +25,938 | 71.6% | +5,661 |

This it passes: removing the best five of seventy trades leaves two thirds of the gain, and the
win rate is high rather than lottery-shaped. Concentration is not the problem here. Direction is.

## 5 · What the premium model does, precisely

| idx | arm | net vs baseline | risk vs baseline | orders vs baseline | return on risk |
| --- | --- | ---: | ---: | ---: | ---: |
| BNF | DIRECTION_PLUS_BOTH | 31% | **39%** | 39% | 13.43% vs 17.04% |
| NF | DIRECTION_PLUS_BOTH | 88% | **53%** | 49% | **7.60% vs 4.54%** |

**It does not add, by the predeclared test**: it fails the matched-risk condition outright, at
0.39× and 0.53× the baseline's capital. What it actually does is **halve the position and keep
most of the money**, which on NIFTY is a 67% improvement in return on risk. The predeclaration
anticipated exactly this and named it: a rupee improvement that arrives with a proportional fall
in trades and risk is **size reduction, not selection**, and it is reported as such.

The two single ablations separate the two mechanisms, which is what they are for:

* **Declining** does nearly all of it. `DIRECTION_PLUS_DECLINE` alone cuts BNF orders from 93 to
  34 and NF from 87 to 42.
* **Selecting a strike** does almost nothing. `DIRECTION_PLUS_SELECT` keeps every order and moves
  BNF by −2.1% and NF by −3.4% of net. With only three pinned strikes there is very little for
  selection to do, and what it does is slightly negative.

So if the premium model has a use on this evidence, it is as a **size governor**, not as a
contract chooser. That is a narrower claim than anything previously proposed for it, and on
three sessions it is still not established.

## 6 · The current Paper rule — refused as rupees, and the finding is the refusal

The predeclaration said it could not be reconstructed as rupees: it is a credit single leg
needing hedged-margin evidence no export carries. That holds. What the trail *does* record is its
own intent per poll at `why.sl`, and the coverage is the finding:

| session | idx | recorded single-leg intent | blocked by |
| --- | --- | --- | --- |
| 18 Nov | BNF | `SELL_CE` 9, `BUY_PE` 2 | `BROKER_SNAPSHOT_UNAVAILABLE` 11, `ENTRY_WINDOW_CLOSED` 1 |
| 18 Nov | NF | `SELL_CE` 22, `BUY_PE` 5 | `BROKER_SNAPSHOT_UNAVAILABLE` 27, `ENTRY_WINDOW_CLOSED` 4 |
| 19 Nov | BNF | `SELL_PE` 20, `BUY_CE` 18 | `BROKER_SNAPSHOT_UNAVAILABLE` 38, `ENTRY_WINDOW_CLOSED` 3 |
| 19 Nov | NF | `SELL_PE` 32, `BUY_CE` 7 | `BROKER_SNAPSHOT_UNAVAILABLE` 39, `ENTRY_WINDOW_CLOSED` 5 |
| 20 Nov | BNF | **`BUY_CE` 47, nothing else** | `BROKER_SNAPSHOT_UNAVAILABLE` 47, `ENTRY_WINDOW_CLOSED` 3 |
| 20 Nov | NF | **`BUY_CE` 30** | `BROKER_SNAPSHOT_UNAVAILABLE` 30, `ENTRY_WINDOW_CLOSED` 3 |

Three things follow, and the first is the one I would act on:

1. **`BROKER_SNAPSHOT_UNAVAILABLE` blocked the Paper rule 192 times across four sessions, and
   on 20 November it blocked every single actionable poll on both indices.** The rule was not
   declining to trade; it had a plan and could not price it. On the evidence here it never once
   got through. That is an app defect with research consequences, and it sits alongside the 13
   November persistence defect as the second thing in this round that is about Market Radar
   rather than about a model.
2. **Its direction was right.** `BUY_CE` on the 20th (observed +2,007 BNF points), `SELL_CE` and
   `BUY_PE` on the 18th (observed −159). The app's own rule was pointing the right way on each
   of the three directional sessions.
3. **`BUY_CE` and `BUY_PE` are debit legs and need no hedged margin**, so unlike the credit legs
   they *could* be priced from these exports. **I have not priced them**, because the
   predeclaration said the Paper rule stays refused and I will not widen a frozen comparison
   after seeing its result. It is the highest-value entry in the ranked follow-ups, and it needs
   its own predeclaration.

Separately, `why.ml` reports `NO_ADMISSIBLE_CANDIDATE` on every poll where it ran, with
`FRESHNESS_UNVERIFIED:EXCHANGE_TIMESTAMP_MISSING` as the reason — the same unverified exchange
clock that has been open in this research for weeks, now shown to be blocking the production
candidate path too.

## 7 · Verdict against the predeclared criteria

| predeclared question | answer |
| --- | --- |
| does `DIRECTION_ONLY` beat `WAIT`? | **yes**, on both indices, and it survives every leave-one-out |
| does the premium model add at matched risk? | **no** — it fails the 10% risk-match outright (0.39×, 0.53×) |
| is the improvement selection or size? | **size reduction**, and the ablation shows declining does nearly all of it |
| is the directional claim supported? | **no** — call-side only in all eight cells; the one BEAR session lost |
| is this an edge? | **no.** It is long-call exposure to two large up days in a three-session sample |
| is the sample adequate? | **no.** Three active sessions, two of them the same direction |

**A tie, a deterioration or insufficient evidence is a valid result, and this is the third.** The
honest summary is that the app's direction signal looks good here for the same reason buying
calls looked good: the sample went up. The test that would change my mind is a sample with
several large *down* days in it, where the BEAR side has to carry its own weight.

## 8 · Reproduce

```bash
# the baseline and all four arms, primary plus the horizon diagnostics
for H in 15 30 60 120; do
  python3 -I tools/premium_model_validation/direction_baseline.py \
      --span <dir-of-exports> --spots <spots> --ledger-root <tree> \
      --horizon-min $H --out tools/premium_model_validation/results/span
done
# the realised direction the sessions are labelled by
python3 -I tools/premium_model_validation/index_direction.py <export...> --out <out>
cd tools/premium_model_validation && python3 -m unittest discover -s tests   # 202
```

| file | sha256 |
| --- | --- |
| `direction_baseline.py` | `1217ef10d37d9d87c4ba270731a730ea340200a73fed8826118029091ee8421e` |
| `index_direction.py` | `64e6ed9491b87ae9d7133fed8c1067bf2eea1737789af9054938bb6393abd099` |
| `stress_range_ev.py` | `5b4a977d07f38c0042f1025b07d704b7ab3b8d425cd497df29129227b286bbee` |

## 9 · Scope

Offline replay on a review branch, source data read read-only. Every rupee is
`PROXY_SIMULATED_FILL` — no order was placed and nothing here is a Paper position. 18–20 November
rests on the **unreviewed** `nse_equity_index_options_rates_20261120_v3_UNREVIEWED` fee revision
and every run stamps `provisional_cost_basis`. Retrospective research; stays **outside** the
unsigned prospective v4 count. No raw export, Paper position or credential in the tree. No
production write, `main` merge, build, release or pull request.

---

### What I would ask you to decide

1. **Accept that the direction baseline is not an edge on this evidence**, for the reason in §4.1
   rather than because the rupees are small — they are not small.
2. **Treat `BROKER_SNAPSHOT_UNAVAILABLE` (§6.1) as a defect to fix**, not a research finding to
   file. The production Paper rule had a directionally-correct plan on every actionable poll of
   three sessions and was blocked on all of them.
3. **Approve a predeclaration for pricing the Paper rule's own `BUY_CE`/`BUY_PE` intents**
   (§6.3), which need no margin evidence and are therefore scorable from the exports already in
   hand. This is the single highest-value follow-up I can see.
4. **Tell me whether the premium model's size-governor reading (§5) is worth pursuing** on its
   own terms, or whether it should wait for a sample where the baseline is not already confounded.
