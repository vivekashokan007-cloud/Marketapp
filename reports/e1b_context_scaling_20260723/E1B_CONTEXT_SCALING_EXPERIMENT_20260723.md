# E1-B — TESTING THE SCALING HYPOTHESIS (the argument for TabICL, made measurable)
**Date:** 2026-07-23 · **Origin:** Vivek's argument that an in-context model *becomes intelligent as data accumulates*, which is why it suits a dynamic market
**Status:** experiment spec — pre-registration required before any result is computed

---

## §1 — THE ARGUMENT IS CORRECT, AND STRONGER THAN STATED

Vivek's point, restated precisely: a fitted model is frozen at its training distribution and decays as the market moves; an in-context model updates by **swapping its context**, with no retraining, no version management, no pipeline.

**This is not speculation — it is a diagnosis of the failure this project is living inside right now.** The deployed model scores AUC **0.4267**, worse than random. Why? It was fitted once on the backtest CSV, frozen, and the market moved. And retraining is *disabled* (`ml_train.py:434`, "pending canonical_won unification"), so it cannot even be refreshed. **The static-model failure mode is not hypothetical here; it is the current state of the brain.**

An in-context model does not have that failure mode by construction. Yesterday's labels enter today's context automatically. That is a genuine architectural advantage, and it is the correct answer to "how do we track a dynamic market without a retraining treadmill."

**Where my earlier ruling was too narrow:** I judged TabICL on a static snapshot — 637 rows, one moment — and concluded "logistic matches it." That comparison is valid *today* and says nothing about *trajectory*. Logistic regression is a fixed linear form: it converges near its asymptote quickly and then plateaus no matter how much data arrives. A context-scaling model does not plateau the same way. **Comparing them at one point on the curve, then generalising, is exactly the composition error this project keeps logging.** Vivek caught it.

## §2 — BUT IT IS A TESTABLE CLAIM, AND WE CAN TEST IT NOW

"Gets smarter with data" makes a falsifiable prediction: **AUC should rise with context size.** If it rises, the strategic case is evidenced and the 637-row result is a floor, not a verdict. If it is flat, the case is not evidenced and we have saved ourselves months.

And here is the part that changes the timeline — **we do not have to wait for data to accumulate. We already have 13× more than E1 used.**

E1 trained on the **primary-only** path: **637 rows**. But the label base also holds **8,107 secondary rows** across 26 days, all `new_price_integrity='OK'` with real outcomes. Those are genuine candidates with genuine labels; they were simply not the top-ranked pick that poll. The primary-only filter is inherited from `ml_train.py:347` — a constraint of the *old fitted* pipeline, where it mattered which distribution you fitted to.

**For an in-context model, that filter is a self-inflicted wound.** More context is the entire mechanism. **Total available context today: 8,744 rows.**

So Vivek's hypothesis can be tested this week, not next year.

## §3 — THE EXPERIMENT

**E1-B: does TabICL's performance scale with context size?**

**Arms (identical folds, identical test rows, identical walk-forward discipline as E1):**

| arm | context | test rows |
|---|---|---|
| A1 | primary only, 25% subsample | primary, day T |
| A2 | primary only, 50% | primary, day T |
| A3 | primary only, 100% (= E1's 637) | primary, day T |
| **A4** | **primary + secondary, 100% (≈8,744)** | **primary, day T** |

Test rows stay **primary-only in every arm** so the arms are directly comparable — only the context changes. Walk-forward unchanged: context is strictly prior days.

**Run the same ladder for logistic** (and for a calibrated logistic). The deliverable is **two learning curves on one chart.** That is the whole question: do they diverge, converge, or run parallel?

**Pre-registered interpretation, fixed before results are seen:**
- **TabICL's curve rises materially from A3→A4 and out-paces logistic's** ⇒ the scaling hypothesis is evidenced; today's parity with logistic is a small-data artifact; TabICL's cost becomes justifiable and the deployment problem (§4) becomes worth solving.
- **Both curves rise together, gap unchanged** ⇒ more data helps everything; TabICL still unproven as *the* answer.
- **TabICL flat or degrading with more context** ⇒ scaling hypothesis not evidenced on this data; revisit only after regime diversity exists.

**Watch items:** context-window and latency ceilings — 8,744 rows is 13× E1's context, and E1 already showed a 37.8 s worst case. If A4 becomes unrunnable, *that itself* is a finding about the deployable ceiling. Also record whether secondary rows differ distributionally from primary (they are by definition lower-ranked); include `role` as a feature so the model can condition on it rather than being confused by it.

## §4 — IF THE HYPOTHESIS HOLDS, DEPLOYMENT HAS AN ANSWER

The obvious objection — "a 4.9 GB model cannot live on the phone" — has a standard resolution that fits this architecture exactly: **distillation.**

TabICL runs where it is cheap (PC/Spotter side), against the full and growing context. Each night it scores a broad candidate set, and a **small student model is fitted to reproduce TabICL's outputs**, then shipped as the daily artifact the brain consults. The brain stays deterministic; it looks up a versioned, SHA-stamped artifact exactly as the architecture law requires. TabICL's intelligence reaches the phone; TabICL's footprint does not.

This also preserves the property Vivek is actually after: the student is **re-fitted daily from a teacher whose context grew yesterday**, so the deployed artifact tracks the market without a retraining treadmill or a frozen-model decay path.

**Not proposed for build.** Recorded now so that if E1-B evidences the scaling hypothesis, the deployment objection does not kill a validated model for want of an architecture — and so nobody later mistakes "TabICL can't run on the phone" for "in-context learning can't reach the phone."

## §5 — REVISED PRIORITY

1. **E1-B context-scaling ladder** — the decisive test of the strategic case, runnable now, no new data required. **Promoted above the licence/export check**, because it decides whether the deployment problem is worth solving at all.
2. **B — licence + exportability** — still a kill-switch, still cheap; run alongside.
3. **C — calibration, with a calibrated-logistic arm** — unchanged.
4. TabPFN blocked on Vivek's token decision; TabFM ceiling still optional.

**Unchanged:** nothing integrates, nothing touches live ranking, no phone code, until E1 is adjudicated complete. The censoring argument still forbids live evaluation of a ranking model.

*The 8,744-row / 637-row context asymmetry and the primary-only filter at `ml_train.py:347`: VERIFIED. The scaling hypothesis: Vivek's, and explicitly untested — that is what E1-B is for.*
