# Claude Consultation: Morning Entry-Confidence and Candidate-Eligibility Failure

**Date:** 2026-08-14
**System:** Market Radar Android app with embedded Python brain and MarketVivi UI
**Observed release:** Android/PWA `2.5.77 / b408`; Python brain `2.5.77`
**Operating scope:** Paper research. The owner has committed to no real-money trading for at least six months.
**Requested review:** Independent architectural ruling with explicit corrections and test criteria.

## 1. Executive Summary

During the 14 August morning session, Market Radar correctly recognized a mildly bearish market and selected `BEAR_CALL / SELL PREMIUM` as the preferred strategy family. However, the system then promoted economically weak candidates as the paper primary, displayed a green `GO` banner, and presented a generic `45%` brain confidence.

The evidence indicates that the system currently conflates three different decisions:

1. market-direction conviction;
2. strategy-family suitability;
3. candidate-specific entry eligibility.

The first two may be reasonable while the third is false. The current implementation can still label such a result `GO`, rank it `#1`, align the verdict to it, and potentially issue an entry notification if the generic verdict confidence reaches 55% and remains stable for two polls.

This is not Bear Call-specific. The same generic alignment, readiness, ranking, banner, and notification paths apply to every strategy family.

## 2. Primary Evidence

### 2.1 Screenshots

Please review these screenshots together:

1. Morning market verdict and 45% confidence:
   `/tmp/codex-web-uploads/f-1CY315/Screenshot_20260814_093119.jpg`
2. NF candidate at 09:31:
   `/tmp/codex-web-uploads/f-yG1eLs/Screenshot_20260814_093159.jpg`
   `/tmp/codex-web-uploads/f-tZcXBB/Screenshot_20260814_093145.jpg`
3. Market signals and proxy candle evidence:
   `/tmp/codex-web-uploads/f-npgCpX/Screenshot_20260814_093131.jpg`
   `/tmp/codex-web-uploads/f-fHdStJ/Screenshot_20260814_093125.jpg`
4. NF candidate at 10:04:
   `/tmp/codex-web-uploads/f-ycExqp/Screenshot_20260814_100421.jpg`
   `/tmp/codex-web-uploads/f-dAIdtD/Screenshot_20260814_100430.jpg`
5. BNF candidate at 10:04:
   `/tmp/codex-web-uploads/f-U59U7W/Screenshot_20260814_100612.jpg`
   `/tmp/codex-web-uploads/f-GZymaE/Screenshot_20260814_100616.jpg`

### 2.2 NF primary candidate

The NF Bear Call displayed:

| Field | Value |
|---|---:|
| Max profit | INR 29 |
| Max loss | INR 9,721 |
| Displayed P(profit) | 98.6% |
| Upstox probability | 1% |
| ML probability/action | 7% / SKIP |
| EV per INR 1,000 margin | -INR 3 |
| UI status | CONDITIONAL / EXEC MONITOR |
| Economic warning | premium edge <= 0; R:R below 0.10 |
| Rank | #1 NF |

One maximum loss consumes approximately `9,721 / 29 = 335.2` maximum-profit wins.

The terminal-payoff break-even probability is:

```text
9,721 / (9,721 + 29) = 99.70%
```

Therefore, even the displayed 98.6% probability does not compensate for the payoff asymmetry before costs and slippage.

### 2.3 BNF primary candidate

The BNF Bear Call displayed:

| Field | Value |
|---|---:|
| Max profit | INR 5,300 |
| Max loss | INR 9,700 |
| Displayed P(profit) | 62.6% |
| Upstox probability | 31% |
| ML probability/action | 89% / TAKE |
| EV per INR 1,000 margin | -INR 33 |
| UI status | CONDITIONAL / EXEC MONITOR |
| Economic warning | premium edge <= 0 |
| Rank | #1 BNF |

The terminal-payoff break-even probability is:

```text
9,700 / (9,700 + 5,300) = 64.67%
```

Using the displayed 62.6% probability:

```text
EV = 0.626 * 5,300 - 0.374 * 9,700
   = approximately -INR 310
```

The BNF structure is more plausible than the NF structure, but its displayed probability remains below economic break-even and its persisted premium edge is negative.

### 2.4 Shared ML out-of-distribution evidence

Both NF and BNF candidates displayed the same warning:

```text
gap_sigma = 416.48
training range = [-5.24, 5.45]
```

This value is roughly 80 times the upper training bound. Because the same value appears across indices and candidates, it looks like a shared poll-context feature, unit mismatch, scaling defect, or feature-contract mismatch rather than candidate-specific evidence.

Despite this OOD condition, the UI still displayed actionable model labels, including `ML 89% TAKE` on BNF. The model warning therefore does not currently force abstention.

## 3. Code-Level Proof

### 3.1 Generic confidence floors

File: `app/src/main/python/brain.py`, function `_align_verdict_to_watchlist`, approximately lines 12156-12189.

```python
aligned = (top.get('forces') or {}).get('aligned', 0)
floor = 0
if isinstance(aligned, (int, float)):
    floor = 70 if aligned >= 3 else 45 if aligned >= 2 else 30
...
if floor:
    final['confidence'] = max(final.get('confidence') or 0, floor)
```

Consequences:

- one aligned force guarantees at least 30%;
- two aligned forces guarantee at least 45%;
- three aligned forces guarantee at least 70%;
- the floor is based on generic force count, not candidate-specific calibrated success probability or expectancy;
- this applies to all strategy families.

The observed 45% is therefore not evidence that the NF or BNF trade has a verified 45% likelihood of success. It is the two-force alignment floor.

### 3.2 A8 economic gate remains shadow-only

File: `app/src/main/python/brain.py`, line 18 and function `_build3_apply_a8_ev_gate`, approximately lines 9693-9730.

```python
BUILD3_A8_HARD_GATE_ACTIVE = False
...
if metrics.get('passes') or not BUILD3_A8_HARD_GATE_ACTIVE:
    survivors.append(cand)
```

The summary explicitly reports `a8_gate_mode = SHADOW_ONLY` and counts failed candidates released to ranking. Negative-EV candidates can therefore remain rankable and become paper primary.

### 3.3 Ranking can choose the least-bad negative candidate

File: `app/src/main/python/brain.py`, functions `_build3_rank_fingerprint` and `rank_candidates`, approximately lines 10110-10205 and 11773-11869.

Risk-normalized premium edge is an ordering field, but it is not an eligibility threshold. Sorting negative candidates still produces a rank `#1`. ML probability is near the end of the lexicographic tuple and is only a tiebreaker. Thus `ML SKIP` does not disqualify a candidate, and `ML TAKE` can remain visible even when its input is OOD.

### 3.4 Execution readiness checks infrastructure, not trade quality

File: `app/src/main/python/brain.py`, function `check_execution_readiness`, approximately lines 10978-11033.

The function checks:

- instrument keys;
- authentication token for sandbox/live;
- sandbox enablement;
- proxy URL for live;
- market hours for sandbox/live.

It does not require:

- `build3EvPass`;
- positive `premiumEdge`;
- minimum risk/reward;
- in-distribution ML inputs;
- a usable candidate-specific probability;
- positive teacher expectancy.

Paper mode can therefore return `executionReady = True` for a candidate that the UI correctly describes as economically weak.

### 3.5 The green GO banner measures menu count

File: `MarketVivi/app.js`, approximately lines 4682-4704.

```javascript
const executable = brainWatchlist.length;
const goClass = executable >= 3 ? 'go-banner go-green' : ...;
...
${executable >= 1 ? 'GO' : 'WAIT'}
```

`GO` means only that one or more watchlist rows exist. Green means at least three rows exist. It does not mean any candidate passed economics, ML quality, or entry eligibility.

### 3.6 UI-only economics protection

File: `MarketVivi/app.js`, approximately lines 4787-4808 and 4898-4946.

The UI identifies weak economics when a credit candidate has `premiumEdge <= 0` or reward/risk below 0.10. It then changes the display to `EXEC MONITOR`, `Gate: MONITOR`, and disables the real-trade button with `REVIEW EDGE`.

This is useful presentation protection, but it does not change the Python candidate's backend eligibility or notification contract.

### 3.7 Entry notification consumes generic verdict confidence

File: `app/src/main/python/brain.py`, `NotificationAgent.process_contract`, approximately lines 18935-19081.

The entry path reads:

```python
confidence = verdict.get('confidence')
...
elif action != 'WAIT' and confidence >= 55 \
        and entry_window_active and best_id:
    stable_action = last two actions match
    stable_best = last two best candidate IDs match
    if stable_action and stable_best:
        reason_code = 'SETUP_READY'
        notify_user = True
```

The best-candidate filter excludes capital-blocked, direction-unsafe, explicitly blocked, or `executionReady=False` candidates. It does not exclude negative-EV, OOD, weak-economics, or ML-SKIP candidates.

At the observed 45%, no entry notification should be produced. However, three aligned forces force confidence to at least 70%, above the 55% notification threshold. After two stable polls during the entry window, the same weak candidate could become an entry notification.

### 3.8 Android dispatch defaults to live notification transport

File: `app/src/main/java/com/marketradar/app/MarketWatchService.kt`, approximately lines 2974-3035.

The notification transport preference defaults to `live`. A contract with `notify_user=true` is dispatched unless transport mode is explicitly `shadow`. Notification transport is separate from paper/sandbox/live trade execution mode.

## 4. Current Interpretation

The bearish market thesis and Bear Call family choice may be reasonable. The failure occurs when market and structure evidence are treated as sufficient entry evidence.

The correct statement for this morning appears to be:

> Market mildly bearish. Bear Call is structurally suitable, but no candidate currently offers validated positive expectancy with trustworthy probability inputs. Continue monitoring; do not issue an entry notification.

The app instead effectively says:

> Many candidates exist, therefore GO. The top candidate has two aligned forces, therefore confidence is at least 45%.

## 5. Proposed Architectural Correction for Review

Please evaluate this separation of concerns.

### Layer 1: Market thesis

Output direction, regime, and market conviction. This must not be labeled trade success probability.

### Layer 2: Strategy-family suitability

Determine which strategy families fit the thesis and regime. A family can be suitable even when no current strike/expiry candidate is worth entering.

### Layer 3: Candidate economics and data quality

Each candidate should receive an explicit entry-eligibility result. Suggested minimum requirements:

- positive and present expected value/premium edge under one documented probability contract;
- risk/reward and payoff-tail sanity;
- no critical feature OOD or schema/unit mismatch;
- candidate probability semantics identified and calibrated;
- capital and direction safety;
- strategy/candidate identity matches the verdict;
- stable across the required polls.

Candidates that fail should remain in the research menu as `MONITOR`, but must not become actionable primary, `GO`, `READY`, or entry-notification eligible.

### Layer 4: Candidate-specific entry confidence

Entry confidence should be computed for the actual candidate. Force alignment may contribute bounded evidence, but it should not impose 30/45/70 floors. If the required probability/economic evidence is absent or OOD, confidence should be unavailable and the action should remain `MONITOR`.

### Layer 5: Notification contract

Require the candidate-specific eligibility contract in addition to confidence, entry window, and stability. Notification should fail closed for economic failure, critical OOD, probability-contract ambiguity, or candidate/verdict mismatch.

## 6. Questions Requiring Claude's Explicit Ruling

Please answer each question separately and challenge our assumptions where needed.

1. Do the screenshots and code prove that the current 45% is a force-alignment floor rather than candidate-calibrated confidence?
2. Is it architecturally valid for any A8/negative-EV failure to remain rankable as research while being categorically excluded from actionable primary and notifications?
3. Should positive EV be a hard entry-eligibility requirement, or should a lower-confidence statistical guard be used because probability estimates are imperfect? Specify the safer paper-research design.
4. How should the system handle the discrepancy between brain POP, Upstox POP, ML target probability, and teacher outcome probability? Define names and permitted uses for each probability.
5. Does `gap_sigma=416.48` against a training range of `[-5.24, 5.45]` justify poll-wide ML abstention? What diagnostics should distinguish genuine extreme markets from a unit/scaling contract defect?
6. Should OOD be an unconditional veto on ML contribution while leaving deterministic/percentile research active?
7. Is removing the 30/45/70 confidence floors sufficient, or should the generic verdict confidence field be renamed to `market_conviction` to prevent future misuse?
8. Define an entry-confidence formula or contract that cannot turn a structurally aligned but economically negative candidate into an entry recommendation.
9. What should `GO` mean? Should it require at least one `ENTRY_ELIGIBLE` candidate, while menu availability is reported separately?
10. Review the notification path. List every condition that should be mandatory before `SETUP_READY` can set `notify_user=true`.
11. Should the changes apply identically to Bear Call, Bull Put, Iron Condor, Iron Butterfly, and debit strategies, with strategy-specific economics only where mathematically necessary?
12. Propose focused tests that prove a negative-EV, OOD, ML-SKIP, or probability-ambiguous candidate can never produce `GO`, actionable confidence, or an entry notification.

## 7. Requested Deliverable

Please return:

1. a concise verdict on whether our diagnosis is correct;
2. any factual or mathematical correction;
3. a recommended candidate-eligibility and confidence contract;
4. a notification fail-closed contract;
5. a strategy-agnostic implementation sequence;
6. exact unit, integration, replay, and historical tests required before release;
7. any reason the proposed correction would damage PC2 paper research or hide valuable rejected-candidate evidence.

Do not recommend live-money promotion. The required outcome is a safer and more scientifically honest paper-research system.
