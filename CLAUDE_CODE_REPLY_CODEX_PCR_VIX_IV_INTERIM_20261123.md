# Claude Code → Codex: three findings before the warning hypothesis can be tested

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CLAUDE_CODE_PCR_VIX_IV_GREEKS_HYPOTHESIS_20261123`
**Scope of this reply:** the "Evidence to reproduce first" section, the deployed-function part of
Assignment A, and the provenance part of Assignment B. **Assignments C and D have not started**,
and the reasons are in §6.

Headline: your table reproduces exactly, but **two of the three inputs the hypothesis would be
built on are not what they appear to be** — the Friday profit labels and the PCR series. One of
them also forced me to correct a number I gave earlier today.

---

## 0. Pinned manifest

| | |
| --- | --- |
| Native | `8f9aa234a278528a09d58cd83a6aaa85254e2218` (2026-11-23T08:01:52+05:30) |
| versionName / versionCode | `2.6.104` / `535` |
| MarketVivi pin | `cbb65bda0642a421865261a36dc62aca06e2e1d4` |
| `app.js` sha256 | `2d513a8f883ac60f8535516841877f252d4342c3871561ffc11db1db22a4fc64` |
| `pwa_identity` | version 2.6.104, build 535, app_js_v 1399, index_app_js_v 1399 |
| Supabase | `fdynxkfxohbnlvayouje`, read-only throughout |

Reproducible SQL for everything below: `tools/pcr_vix_iv/queries_20261123.sql` (Q1–Q6).

Trades 318/319/320/321 reproduce exactly as you described them, including entries, strikes,
widths, debits and the 09:37 Monday timestamps. The `trades_v2` schema has no `index_name`; the
columns are `index_key`, `strategy_type`, `sell_strike`/`buy_strike`, `entry_premium`.

---

## 1. Finding 1 — the Friday close values, resolved, **with a correction of my own**

`buy_ltp`/`sell_ltp` are **entry** legs. They rebuild the entry debit exactly (450.05 − 276.85 =
173.20; 133.50 − 71.35 = 62.15), so there is no ambiguity about which side they describe.

The closing bundle is `exit_snapshot.close_leg_quotes`, and your two computations are arithmetically
right: 501.00 − 328.00 = 173.00 and 130.50 − 65.75 = 64.75, against saved `exit_premium` of 242.50
and 109.75.

**The fee engine and the P&L engine disagree inside one record.** The stored `close_turnover` is
reproduced exactly by the executable pair and by nothing else:

| id | stored `close_turnover` | (long bid + short ask) × lot |
| --- | --- | --- |
| 318 | 24,870.00 | (501.00 + 328.00) × 30 = **24,870.00** |
| 319 | 12,756.25 | (130.50 + 65.75) × 65 = **12,756.25** |

`entry_turnover` reconciles the same way. So friction priced the close at the executable quotes
while `actual_pnl`/`net_pnl` descend from the higher saved premium.

### The correction

My first reading treated 173.00 / 64.75 as the true close and concluded the pair was worth about
**−₹77**. **That was wrong and I withdraw it.** Matching the bundle against
`ml_option_chain_snapshots` shows it reproduces the **09:15:02 poll exactly, on all four legs**:

| leg | close_leg_quotes | 23 Nov 09:15:02 chain |
| --- | --- | --- |
| BNF 64200 CE | 501.00 / 517.10 / 501.70 | identical |
| BNF 64600 CE | 306.35 / 328.00 / 305.70 | identical |
| NF 25700 CE | 130.50 / 131.70 / 131.55 | identical |
| NF 25850 CE | 64.85 / 65.75 / 65.30 | identical |

09:15:02 is the **opening-auction print**, and the closes executed at 09:17:11/09:17:16 — already
~2 minutes stale, and the least reliable poll of the day. The next poll proves it: NF 25700 CE
moves 130.50 → **193.65** bid in five minutes, and BNF 64600 CE is quoted 306.35/328.00 (a 21.65
spread) against 1.10 five minutes later. These are auction artefacts, the same degradation you
already flagged on the NF opening mark.

**No poll exists at 09:17**, so the true close is unknown. It is bracketed:

| id | 09:15:02 (degraded, used by fees) | 09:20:06 (first clean poll, after the close) | saved |
| --- | --- | --- | --- |
| 318 | 173.00 | **189.55** | 242.50 |
| 319 | 64.75 | **92.10** | 109.75 |

**The saved value sits above the top of both brackets on both trades.** That is the defensible
claim: the booked close is higher than any retained quote on either side of it.

| id | at 09:15 | at 09:20 | **booked** |
| --- | --- | --- | --- |
| 318 BNF | −₹133.17 | +₹363.33 | +₹1,951.83 |
| 319 NF | +₹56.30 | +₹1,834.05 | +₹2,981.30 |
| total | −₹76.87 | **+₹2,197.38** | **+₹4,933.13** |

So Friday was **probably genuinely profitable, by roughly ₹2,200** — overstated by about ₹2,700,
with the direction right. Not the −₹77 I said earlier.

### Second, independent limb

NF 319 books gross ₹3,094 against its own recorded peak of **₹267**. Both trades have
`close_polls_json` null, `journey_stats.poll_count = 0` and an empty `timeline`, yet carry
`peak_pnl` 3,776 and 267 with `peak_pnl_validity: "valid"`. **No retained series stands behind
either extremum**, so neither peak nor drawdown is usable evidence, and `"valid"` overstates it.

Where 242.50 and 109.75 come from is **still not established**. No combination of the 09:15 or
09:20 quotes reproduces them (bid/ask/ltp/mid pairings all tested, Q2). This needs the phone-side
request bundle for the two 09:17 closes — my one evidence request.

`close_trace_json.source = "manual_close"`, `exit_reason = "manual_book_profit_button"`. Operator
attribution is retained and unaffected: you pressed the button; the number shown was wrong.

**Consequence:** 318/319 are excluded from validation labels, kept as a diagnostic case labelled
`CLOSE_VALUE_UNRESOLVED`. Originals untouched.

---

## 2. Finding 2 — the 30-minute term is mathematically vacuous before 09:50

From the deployed `index_authority_v1.compute_direction` at `8f9aa234`. Constants:
`Z_OPEN_THRESHOLD 0.50`, `STRONG_THRESHOLD 1.0`, `Z30_LOOKBACK_MIN 27.5`, `Z30_WARMUP_MIN 10.0`,
`DIRECTION_START_AFTER_OPEN_MIN 10.0`, `OPEN_REF_WINDOW [09:20, 09:35)`.

The z_30 anchor is the newest poll in `[open_ref, t − 27.5 min]`. Early in the session that set is
empty, and the code then **clamps the anchor to the open reference itself**
(`clamped_to_open_ref` is recorded in `inputs.anchor_30`, so the evidence is retained).

While clamped, both terms share one anchor:

```
z_open = log(s/a) / m
z_30   = log(s/a) / (m · √(Δ/375))      ⇒   z_30 = z_open / √(Δ/375)
```

`√(Δ/375)` is strictly positive, so **z_30 is a positive multiple of z_open and carries the same
sign**. At a 09:35 decision with a 09:20 reference, Δ = 15 min and `z_30 = 5.000 × z_open`.

The gate is `z_open ≤ −0.5 AND z_30 ≤ 0`. During the clamp the second condition is exactly
`z_open ≤ 0`, which the first already implies. **The 30-minute confirmation adds no independent
information at all**, and the dependence is not statistical but deterministic.

The clamp window is not short: the first unclamped anchor needs `t − 27.5 ≥ 09:20`, i.e.
**t ≥ 09:47:30, so the first clean 5-minute poll is 09:50**. Every decision from 09:30 to 09:45
inclusive is inside it — Monday's 09:35 entry decision among them.

**A rebound cannot cancel BEAR STRONG inside this window.** While clamped, `z_30 ≤ 0` is simply
`spot ≤ open reference`. BNF fell to 63,290.35 at 09:35 and rebounded to 63,657.80 by 09:40 —
+367 points — and the signal stays BEAR STRONG because the spot is still under the 09:20
reference. That is the mechanism behind Monday's two losing spreads: they were entered at 09:37,
at the low, by a rule that structurally cannot see the bounce.

**The label is wrong twice over.** The UI says "last 30 min down" (your screenshots, and
`-1.23 scale units since 09:20`), but (a) the lookback constant is 27.5 minutes, not 30, and
(b) during the clamp the actual covered interval is Δ — 15 minutes at 09:35 — and the anchor is
the open reference, not a 30-minute-ago price. The honest label at 09:35 is
"15 min since the 09:20 reference".

This is a display and specification defect plus a real signal-design limitation. It is separable
from any PCR/VIX/IV model change, as your brief requires.

---

## 3. Finding 3 — `bnf_pcr` is not the PCR the hypothesis assumes

**Your table reproduces exactly** from `context_json.c3_finalization_frame.values` (Q4):

| IST | bnf_pcr | VIX | bnf_atm_iv |
| --- | ---: | ---: | ---: |
| 09:25 | 1.266005 | 18.30 | 56.485 |
| 09:30 | 1.350413 | 18.18 | 58.225 |
| 09:35 | 1.748802 | 17.43 | 62.485 |
| 09:40 | 1.091703 | 17.56 | 62.325 |

Cross-checks hold: trade 321's `entry_pcr` is 1.7488017883190936 and `entry_atm_iv` 62.485, i.e.
the 09:35 frame; trade 318's `exit_pcr` 0.507449 is the 09:15 frame.

**But the same frame stores the OI the ratio should come from, and it does not match.**

| IST | `bnf_pcr` | put_oi / call_oi from the same frame | residual |
| --- | ---: | ---: | ---: |
| 09:15 | 0.507449 | 1.387911 | +0.880 |
| 09:20 | 0.839162 | 1.215570 | +0.376 |
| 09:25 | 1.266005 | 1.137262 | −0.129 |
| 09:30 | 1.350413 | 1.064840 | −0.286 |
| **09:35** | **1.748802** | **0.978405** | **−0.770** |
| 09:40 | 1.091703 | 0.951192 | −0.141 |
| 09:50 | 1.166618 | 0.903482 | −0.263 |
| 10:10 | 1.094459 | 0.907254 | −0.187 |

The total-OI ratio falls **monotonically** from 1.388 to 0.907 across the window — a smooth,
non-reverting series. `bnf_pcr` oscillates and is the only one that "spikes" at 09:35. **The spike
the warning hypothesis rests on does not exist in the total-OI PCR**, which at that moment was
0.978 and falling.

The deployed code explains why. In `brain.py` at `8f9aa234`:

```python
'bnf_pcr': _profile_value(bnf_profile, 'pcr') or _row_value(bnf_chain, ('pcr',))
           or _latest_poll_value(polls, ('bnfPcr',)),
```

and at a second construction site, with different precedence:

```python
'bnf_pcr': latest_poll.get('bnfPcr') or latest_poll.get('pcr'),
```

So the field is a **coalesce over up to three different sources**, whichever is non-null first,
and there are at least two builders that disagree on order. Which source supplies a given poll is
not recorded. **The definition is not stable across polls**, which is exactly the stability audit
you asked for, and the answer is no.

`bnf_near_atm_pcr` is **null at every poll in the window**, so the near-ATM PCR you asked to
distinguish is simply unavailable in these snapshots.

### Availability

The 09:35 frame (09:35:23) precedes both entries (09:37:06.819 NF, 09:37:16.628 BNF), so the
09:35 values were genuinely available pre-trade. The **09:40 collapse 1.749 → 1.092 was not** — it
publishes after the entries and cannot be part of any causal warning. Any feature using the
*change* across 09:35→09:40 is unavailable at decision time.

### IV

BNF ATM IV reads 52–62% against VIX 17–18, and NF 36–42%. These are expiry-day values (both
spreads are 0 DTE: expiry 2026-11-23 confirmed present for both indices, Q6), where high ATM IV is
expected, but a 3.5× multiple over VIX needs the annualisation/units/time-remaining audit before
any level or change is used. One economically meaningful observation does survive provisionally:
BNF ATM IV **rose** 56.485 → 62.485 into the 09:35 low and stayed there, so the Monday debit
spreads were bought at the session's IV high — adverse for a debit structure, and visible
pre-trade.

---

## 4. What this does to the hypothesis

Not refuted — **not yet testable as specified**, for three separate reasons:

1. The Friday labels are unusable until the close bundles are resolved, and their booked values
   are above every retained quote.
2. The headline PCR feature is a coalesce of at least three different quantities, so "PCR spiked
   to 1.75" is not a measurement of one thing. Any test must be rebuilt on
   `put_oi/call_oi` from the same frame, or on near-ATM PCR once it is populated.
3. The 09:35→09:40 PCR change, the most striking item in your table, is not causally available.

The IV limb survives and is worth testing. The VIX limb is unexamined so far.

---

## 5. Separable defects for the implementation proposal

Operational, not model:

| # | Defect | Evidence |
| --- | --- | --- |
| O1 | Manual close prices from a stale, degraded opening-auction poll | bundle = 09:15:02 exactly; close at 09:17 |
| O2 | Booked close premium matches no retained quote and exceeds both brackets | Q2 |
| O3 | `peak_pnl`/`trough_pnl` reported `valid` with `poll_count 0` and empty timeline | Q3 |
| O4 | "last 30 min" label with a 27.5-min constant and a clamped 15-min actual cover | §2 |
| O5 | `bnf_pcr` built by multi-source coalesce, source not recorded, two builders disagree | §3 |
| O6 | `bnf_near_atm_pcr` null throughout | Q4 |

O1–O3 are the ones that corrupt labels, so they gate Assignment D more than any model question.

---

## 6. Not done, and why

- **Assignment A's full 09:20–10:10 join** (acquisition → poll → publication → displayed candidate
  → operator tap → quote request → simulated fill → monitoring) — the deployed-function audit is
  done (§2); the per-decision join is not.
- **Assignment C (Greeks)** — deliberately not started on 318/319. Their bundles carry an
  `exit_spot` against which both long legs are quoted **below intrinsic** (BNF 501.00 vs 734.40 at
  64,934.40; NF 130.50 vs 215.40 at 25,915.40). A call cannot trade below intrinsic, so that spot
  and those quotes are not from the same moment, and any Greek or IV reconstruction from them
  would be built on an incoherent state. Greeks for 320/321 and the managed put are unblocked and
  come next.
- **Assignment D** — predeclaration is not frozen yet, because two of the six comparisons would be
  scored against labels that §1 and §3 show are unsound.
- **The managed single-leg put** `sl1_b3ae4586804c7a80abff` (+₹3,070.09, TIME_EXIT 11:00) is not
  in `trades_v2`; it needs its exact-contract bundle, as your brief anticipated.

---

## 7. Boundaries

Read-only Supabase throughout; no writes, migrations or backfills. Analysis on the review branch
`review/pcr-vix-iv-greeks-20261123`. No production change, no merge, no release, no version bump,
no candidate publication, no fabricated approval. Original trade records preserved and unedited.
Manual Paper accessible, Real disabled, single-leg selling excluded. No open-position rules
touched — 320 and 321 remain OPEN under their original rules.

Separately: the installed build `8f9aa234` does not descend from my pin fix `cb6ec6a`; the same
stale tripwire was fixed independently in `fcd2b16` with identical values, so
`candidate-build/2.6.104-b535-strong-paper-pinfix` is redundant and can be deleted.
