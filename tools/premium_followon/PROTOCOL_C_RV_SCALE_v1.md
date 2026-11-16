# Protocol C v1 — the empirical E0 reference against one recent realised-variance scale

**Version** `premium_followon_protocol_c_rv_scale_v1` · **declared** 16 November 2026 IST ·
**base commit** `124d62ee027941e43decc737e53d202c32b1d214`

Declared **before** any reservable session is scored. Nothing in it may be tuned on a session it
is later used to judge, and the one dry run below is on inspected development data, labelled as
such by the tooling itself.

**This protocol is a draft for review.** It fixes the challenger, the inputs, the metrics and the
exclusions so that they cannot be chosen afterwards. It does **not** claim a session budget or a
stopping rule: those belong to Codex's review, and until they are agreed nothing here is run on a
reservable session.

## 1 · What is being compared

| | |
| --- | --- |
| reference | **E0**, unchanged — the empirical pool of same-clock returns from strictly prior sessions, exactly as the pinned protocol fits it. No parameter, no offset, no fitted scale. |
| challenger | **E0_RV1** — the same pool, each member multiplied by `s_q / s_i`. Nothing else differs: same pool, same rows, same metric, same weighting. |
| scale | `s = sqrt(mean(r²))` over the trailing **60 minutes** of **strictly five-minute-spaced** log returns of the same index, within the same session, ending at or before the anchor minute. Uncentred, as realised variance normally is. |
| minimum support | **6 returns** in the window, else the row abstains. |

The scale function is **not reimplemented**. `tools/premium_followon/rv_scale_protocol.py`
imports `rv_scale` from the frozen `tools/spot_path_baseline/rv_scale_compare.py`, including its
13 November correction that a return counts only when its two observations are exactly five
minutes apart — otherwise a 10- or 15-minute move is scored as a five-minute one and the scale is
inflated.

## 2 · Clocks, causality and warm-up

- **Anchor** — the minute whose forecast is being made, on the clock basis declared for the run:
  `retained` (the minute the reading carries) or `nominal` (floored to the five-minute grid).
  One basis per run, stated in the output.
- **Remaining-window clock** — a row is scored only when `anchor + horizon ≤ 15:30 IST`
  (minute 930). A horizon that would run past the close abstains as
  `horizon_past_session_close`; it is not truncated and not extended into the next session.
  Horizons: **15, 30, 60 minutes**, as pinned.
- **Causal inputs, both sides** — the query scale uses only minutes `≤` the anchor in the query
  session. Each history member's scale is computed **at its own anchor, inside its own session**,
  from minutes `≤` that anchor. The pool itself contains only **strictly prior** sessions. No
  quantity anywhere in the comparison reads a minute later than the anchor it belongs to.
- **Warm-up** — not a clock constant. On a complete five-minute grid, six strictly-spaced returns
  first exist 35 minutes after the session's first reading, so an anchor before about **09:50**
  abstains by the minimum-support rule rather than by a separate cut-off. A session with gaps
  warms up later, and the abstention says so.
- **Matched pools** — a history member whose own scale is unavailable is dropped from **both**
  samples, so E0 and E0_RV1 are always scored on identical observations. The count of dropped
  members is reported.

## 3 · Metrics

Per row, then averaged per **(index, horizon, session)** — never pooled across sessions by row,
because rows inside one session share one price path:

| metric | definition |
| --- | --- |
| **CRPS** | the sorted-sample identity: `mean|xᵢ − y| − ½·mean|xᵢ − xⱼ|`, in basis points |
| **median MAE** | `|median(sample) − y|`, in basis points |
| **80% coverage** | 1 when the 10th–90th percentile interval contains `y` |
| **80% width** | `p90 − p10`, in basis points, reported so a coverage change can be read |

Both CRPS and median MAE are reported every time. A CRPS gain carrying an MAE cost is reported as
exactly that — the 12 November premium result was such a split, and this protocol inherits the
rule that it may never be described as an unqualified improvement.

**Session labels** reuse the frozen plan's §6 partition unchanged: **SUPPORTED** needs both
Δ metrics `< 0`; exactly one is a **SPLIT**; neither is **ADVERSE**. Three SPLITs do not
aggregate into support.

The scorer is checked rather than trusted: on a real run the scaffold scores every row again with
the pinned `analyze.py` `score()` and prints the largest absolute disagreement. The dry run below
reports **0.000e+00 across 1,108 rows**.

## 4 · Declared exclusions

Seven reasons, declared here, counted by `(index, horizon, reason)` and printed in full:

| reason | meaning |
| --- | --- |
| `horizon_past_session_close` | `anchor + horizon` beyond 15:30 IST |
| `label_unavailable` | no realised return for that (index, session, anchor, horizon) |
| `query_feature_unavailable` | the reference's own feature set has no entry for that anchor |
| `insufficient_training_sessions` | fewer prior sessions than the pinned minimum (5) |
| `query_insufficient_window_returns` | fewer than 6 strictly-spaced returns at the query anchor |
| `query_non_positive_scale` | a zero or non-finite query scale — never divided by |
| `insufficient_matched_training` | too few history members keep a scale of their own |

No row is excluded on the size of a model's error or on any outcome-dependent quantity. Missing
data is **never interpolated**: a gap simply yields fewer returns and the minimum-support test
then decides.

## 5 · Chronological split

- The pool for a session is strictly earlier sessions. A session never trains on itself or on
  anything later.
- Evaluation sessions are taken in date order and reported separately before any pooling.
- **Freshness is decided by `input_inventory.py`, not by this document.** A session is admissible
  as evidence only when the inventory labels its date `RESERVED_PROSPECTIVE`. An `INSPECTED` or
  `INDETERMINATE_NOT_RESERVABLE` date is development data, and a file whose own provenance says
  it is not a live capture is `SYNTHETIC_NOT_A_SESSION` whatever date it carries.
- As of the inventory run on 16 November 2026, **there are no reservable sessions offline**
  (§7), so this protocol has been run on development data only.

## 6 · Development dry run — inspected data, no conclusion drawn

```
python3 -I tools/premium_followon/rv_scale_protocol.py --self-check
python3 -I tools/premium_followon/rv_scale_protocol.py \
    --handoff-dir <13 Nov handoff> --json protocol_c_dryrun_retained.json
```

Pool: the pinned `spots_20261021_20261104.jsonl`, sha256
`f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186`, 11 sessions. Evaluation
sessions: the protocol's five held-out dates (2026-10-29, 10-30, 11-02, 11-03, 11-04) — **all
five already scored during development**. Clock basis `retained`. 30 index × horizon × session
cells, 1,108 matched rows.

| index | horizon | SUPPORTED | SPLIT | ADVERSE |
| --- | ---: | ---: | ---: | ---: |
| BNF | 15 | 3 | 1 | 1 |
| BNF | 30 | 3 | 0 | 2 |
| BNF | 60 | 4 | 1 | 0 |
| NF | 15 | 3 | 1 | 1 |
| NF | 30 | 3 | 0 | 2 |
| NF | 60 | 4 | 0 | 1 |

Largest single-session effects at the 60-minute horizon: NIFTY 2026-11-03 ΔCRPS −2.455 bp with
ΔMAE −2.476 bp; NIFTY 2026-10-29 −1.882 / −2.599. The adverse cells concentrate on
**2026-11-04**, the last session in the pool — consistent with the staleness limit the frozen
plan already records, not evidence about the scale itself.

Two things this dry run is **not**. It is not evidence for or against E0_RV1: every evaluation
session was inspected while the challenger was being built. And it is not a premium result: it
scores the **index path**, not an option premium.

**One diagnostic worth carrying forward.** Nominal 80% intervals cover between **0.24 and 0.89**
of outcomes here, for *both* models — E0's own calibration, not a challenger effect. Where the
RV scale sharpens the distribution it usually lowers coverage further (NIFTY 2026-11-03,
0.89 → 0.73 at 60 minutes). Any later reading of a CRPS gain should be accompanied by this
coverage number, because a sharper distribution can win CRPS while being worse calibrated.

## 7 · Input support for the two later comparisons

Measured by `input_inventory.py` over everything available offline, not estimated:

| candidate | what it needs | what exists offline | verdict |
| --- | --- | --- | --- |
| **HAR-RV** (day + week + month components) | a daily realised-variance series of ≥ 22 prior sessions, plus warm-up — about 27 contiguous prior sessions of intraday five-minute index data | **13 distinct sessions**: the pinned 11 (2026-10-21 … 11-04) plus 2026-11-12 and 11-13 from the authority spot path in the exports | **NOT SUPPORTED.** Needs the 45-session five-minute poll series (`replay/inputs/polls_raw.txt`, sha256 `46b74933…` in the handoff's `PROVENANCE.json`) or an equivalent re-export; only its hash is present offline, not the file. |
| **implied variance** at the anchor | an IV reading at the anchor clock for the query session **and** for each prior session in the pool — so ≥ 6 sessions carrying IV on a common clock grid | **1 session**: 2026-11-12 (318 NF and 323 BNF records, every one carrying `iv.iv_greeks_raw`, 6 pinned contracts per index across 53/54 five-minute slots, one expiry per index) | **NOT SUPPORTED.** Premium capture already records what is needed; the series is one session long. Each further captured session adds one, so ≥ 6 captured premium sessions make the comparison possible. |

Both are therefore **recorded as unsupported for now**, with the exact missing input named. No
part of this protocol depends on either.

## 8 · What this protocol does not touch

The frozen repricing plan and `RULES_FROZEN.md`; `premium_repricing_v2.py`,
`rv_scale_compare.py`, `premium_60min_compare.py`, `spot_path_baseline.py`,
`preflight_export.py` and `test_repricing_fixes.py` — all unchanged on this branch; v7 ranking,
gates, sizing, exits, teacher fee and notifications; the installed b530 and its capture; any
production database write. Nothing here is an activation claim.
