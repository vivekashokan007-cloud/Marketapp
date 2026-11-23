# Exact-contract inventory, and the bounded causal exit comparison

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CODEX_REPLY_CLAUDE_FOCUSED_COMPLETION_20261123` §1 and §2
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, b535 frozen

> **Decision answered: NO.** Neither pullback response improves the observed exit outcome.
> Reference **+₹7,627.33** net; Challenger 1 **−₹3,664.12**; Challenger 2 **−₹4,260.88**.
> Every figure below sums from the per-position rows, so the table is checkable line by line.
> **4 of 4 positions worse under both.** Both challengers are **parked**, not selected, and no
> fresh-session comparison is justified for either.

---

## 1. Exact-contract inventory

### 1.1 Sources examined, and what each actually carries

| source | identity | clocks | bid/ask | IV | Greeks | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **`premium.indices[ix]`** (C1 device capture) | full: `instrument_key`, strike, option_type, expiry, `underlying_key`, lot table version | `slot` + acquisition receipt; **`clock_provenance: DEVICE_LOCAL_ACQUISITION_RECEIPT_EXCHANGE_EVENT_SEMANTICS_UNVERIFIED`** | bid/ask/bid_qty/ask_qty/ltp, all `PRESENT_VALID`, `book_status TWO_SIDED` | **present** (`iv_greeks_raw`), but **`iv_unit_status UNKNOWN`**, `iv_authoritative_resolved false` | **none** | **richest source**; 12 pinned contracts only |
| **`position_ticks`** | `instrument_key`, strike, option_type, side, `key_match` | `source_ts`, `source_age_ms`, `source_validity`, `last_trade_ts` | bid/ask/ltp/`executable_price` per leg | **none** | **none** | **only source with BOTH legs of each spread** |
| **`single_leg.closed/positions`** | full contract block incl. `tick_size` + `tick_unit` provenance | `ts`, `receipt_ms`, `request_start_ms`, `age_s`; **disclosure: "exchange quote age unavailable"** | bid/ask/qty at **2 moments only** | none | none | entry + exit only |
| **`single_leg.hold_observations.marks`** | per-endpoint windows declared by the app | window start / stop_exclusive | `same_60` only | none | none | **declares its own endpoint statuses** |
| **`ml_option_chain_snapshots`** | index, expiry, strike, option_type; **no `instrument_key`** | `poll_ts` only | bid/ask/ltp | none | none | breadth, no clocks |
| **`parts.proposal_evidence`** | status only | all three timestamp fields **absent in all 352** | 114 BNF / 0 NF | 114 BNF / 0 NF | 114 BNF / 0 NF | candidate-capture audit |
| `chain_snapshots` | — | — | — | — | — | **dead since 2026-06-29** |

**The premium capture is better than I had credited.** 12 pinned contracts, **41 slots each, 246
records per index, 100% `PRESENT_VALID` on bid/ask/qty/ltp, 100% `TWO_SIDED`, 100% IV present, all
`OBSERVED`, every record hash-verified** (`payload_sha256`, `hash_basis` —
`authenticates_vendor_truth: false`, stated by the app itself).

Pinned: BNF 64400/64500/64600 CE+PE and NF 25800/25850/25900 CE+PE, all expiry 2026-11-23.

**But it intersects the traded positions in exactly two legs, and neither is a long leg:**

| position | long leg | short leg | pinned? |
| --- | --- | --- | --- |
| 318 BNF BULL_CALL | 64200 CE | **64600 CE** `NSE_FO|62668` | **short only** |
| 319 NF BULL_CALL | 25700 CE | **25850 CE** `NSE_FO|64491` | **short only** |
| 320 NF BEAR_PUT | 25600 PE | 25450 PE | no |
| 321 BNF BEAR_PUT | 63200 PE | 62800 PE | no |
| managed BNF BUY_PE | 64000 PE **exp 2026-12-29** `NSE_FO|59956` | — | no (different expiry) |

A spread needs **both** legs at the same moment, so the premium capture **cannot score any
position**. It is **diagnostic-only** for 318's and 319's short legs — though with IV at 41 slots
it is the only viable basis for a future Test 3, subject to the unit question.

### 1.2 Inventory table — one row per position per endpoint

Entry/quantity/contract fixed. Pricing source for the four spreads is `position_ticks` (both legs,
60-second ticks, `mark_trust` gated); for the managed leg it is the broker snapshots.

| position | endpoint | status | source | quote time | basis |
| --- | --- | --- | --- | --- | --- |
| **318** BNF 64200/64600 CE | entry 11-20 12:38:53 | **scoreable** | ticks + `buy_ltp`/`sell_ltp` | 12:38:54 | both legs |
| 318 | +30 / +60 / +120 | **scoreable** | ticks | 13:08 / 13:38 / 14:38 | both legs |
| 318 | near-close 11-20 | **scoreable** | ticks | to 15:40:57 | both legs |
| 318 | next session 11-23 | **scoreable** | ticks | 09:15:01, 09:16:05, 09:17:10 | 3 ticks only; 09:15 inside the suspect window |
| **319** NF 25700/25850 CE | as 318 | **scoreable** | ticks | same windows | both legs |
| **320** NF 25600/25450 PE | entry 09:37:06, +30/+60/+120 | **scoreable** | ticks | 10:07 / 10:37 / 11:37 | both legs |
| 320 | near-close, next session | **not applicable** | — | — | closed 12:31:50 |
| **321** BNF 63200/62800 PE | entry 09:37:16, +30/+60/+120 | **scoreable** | ticks | 10:07 / 10:37 / 11:37 | both legs |
| 321 | near-close, next session | **not applicable** | — | — | closed 14:18:38 |
| **managed** BNF 64000 PE **Dec** | entry 09:56:49 | **scoreable** | broker snapshot | 09:56:49.054 | bid 1420.90 / ask 1427.50, `age_s 0.036` |
| managed | **+60 (`same_60`)** | **scoreable** | broker snapshot | 11:00:17.082 | bid 1535.10 / ask 1541.70; app marks `SCORED` |
| managed | +30, +120 | **blocked** | — | — | no quote; endpoint not even declared by the observer |
| managed | **near_close** | **blocked** | — | window 15:25:00→15:30:00 | app marks **`PENDING`** |
| managed | **next_open** | **blocked** | — | 11-25 09:15→09:25 | app marks **`PENDING`** |
| managed | **next_60** | **blocked** | — | 11-25 10:15→10:25 | app marks **`PENDING`** |

**No substitution was made for the December BNF 64,000 PE.** Its blocked endpoints are blocked on
the app's own declaration, and the managed position has **zero rows in `position_ticks`** — it is
monitored by a separate path whose only persisted marks are the two broker snapshots.

### 1.3 Missing raw quotes vs computable Greeks — the distinction you asked for

| gap | kind |
| --- | --- |
| managed +30/+120/near_close/next_open/next_60 | **missing raw quote** — nothing can be computed |
| Greeks on every source | **computable, not missing raw input**: strike, expiry, bid/ask and a spot path exist, so delta/gamma/theta/vega can be derived. Production supplies only `_bs_delta` and an explicitly *simplified* `_bs_theta`; no gamma, no vega, no IV inversion. Every such quantity must be labelled model-derived |
| IV on the premium capture | **present but unusable as a level** until `iv_unit_status` resolves (71.53 — percent or fraction?). Usable for *changes* within one unit convention; not for an absolute comparison |
| exchange clock everywhere | **missing and unverifiable.** Every source that discloses its clock says the exchange semantics are unverified. My earlier claim that Upstox supplies `exchange_timestamp` and the app drops it remains **withdrawn** — the snapshot carries the key with a `null` value, which says nothing about the vendor |

**Solver assumptions, declared for any future inversion:** r = 0.065 (production's
`_bs_delta`/`_bs_theta`), T on production's trading-day basis (`_option_years_from_trading_dte`,
`TEACHER_TIME_BASIS_DAYS`), European convention, mid as the inversion target with bid/ask as the
bound. **Clock uncertainty:** receipt-time only, magnitude unmeasurable.

### 1.4 Smallest additional capture to close the blocked endpoints

The mechanism already exists — it produced the managed leg's entry and exit snapshots. It is not
invoked on a schedule.

| # | change | cost |
| --- | --- | --- |
| **M1** | request the **held** `instrument_key` through the existing targeted single-leg quote path at each 5-minute poll from admission to exit | 1 instrument per open managed position |
| **M2** | request it again inside each declared observer window (`near_close`, `next_open`, `next_60`) and persist with the window id | 3 requests per position, once each |
| **M3** | persist `iv_unit_status` resolution once per session by comparing the Greeks-block IV against an inverted IV on the same contract | 0 extra requests |

M1 alone converts the managed position from 1 scoreable endpoint to a full intraday series.

---

## 2. The bounded causal exit comparison

### 2.1 Policies, and the trigger definition read from production

`PULLBACK` is **not** a direction value. It is a published **reason code** on a NEUTRAL decision
(`index_authority_v1.py:559`):

```
if   z_open >=  0.50 and z_30 >= 0 :  BULL
elif z_open <= -0.50 and z_30 <= 0 :  BEAR
else                               :  NEUTRAL,
     reason_code = 'WEAK' if abs(z_open) < 0.50 else 'PULLBACK'
```

So **PULLBACK = NEUTRAL with |z_open| ≥ 0.50** — the opening trend is still strong while the
30-minute term disagrees in sign. On 23 November **every one of the 17 NEUTRAL publications is
PULLBACK** (none WEAK), so the two trigger definitions coincide for that session.

**This also bounds when the challengers can fire at all.** While the 30-minute anchor is shared
with the open reference, `z_30 = z_open / √(Δ/375)` — a positive multiple of the same sign — so the
second condition is vacuous and PULLBACK **cannot** be published. The earliest observed PULLBACK on
23 November is **10:05**, consistent with the anchors separating around 09:50.

| policy | rule as scored |
| --- | --- |
| **Reference** | the installed policy reproduced; for 318–321 the operator controlled the exit, so the **Manual Paper actual** is shown as the Reference and labelled as such |
| **Challenger 1** | exit at the first eligible quote after the **first** published NEUTRAL/PULLBACK following entry |
| **Challenger 2** | exit after **two consecutive** published NEUTRAL/PULLBACK observations |

**Declared tolerance:** first `position_ticks` mark at or after the triggering publication minute,
within 10 minutes, with `mark_trust TRUSTED` and `bound_anomaly false`; otherwise blocked. All
eight challenger endpoints resolved with a delay of **0.05–0.80 minutes**.

**Installed handling of opposite observations, stops and deadlines, documented.** The managed
policy's `holding_plan` declares `early_exit_conditions: [buy_bid_at_or_below_70pct_entry,
two_confirmed_opposite_observations]` with `exit_deadline` = admission + 60 min and
`clock_anchor: admission_time`. **"Opposite" means the opposing direction, not NEUTRAL** — for a
BEAR position that is BULL. There were no BULL publications on 23 November and the position records
`contrary_valid_polls: 0`, so the installed confirmation rule never fired. The challengers are
therefore genuinely different from installed behaviour, not a restatement of it. Protective
controls are retained: a challenger exit later than an installed stop or deadline is superseded by
the installed control.

### 2.2 Result — net, with both fee sides modelled by production at executed quantity

Fee basis `ev_v12_fee_teacher_v2_dated_20261013`, dated per trade date. Entry fees modelled from
the booked entry legs reconcile exactly against production's booked accounting (modelled entry
+ implied booked exit = booked total, to the paisa, on all four).

| pos | arm | exit mark | gross | entry fee | exit fee | **net** | vs Reference |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **318** | Reference (manual) | booked | 2,079.00 | 69.23 | 57.94 | **1,951.83** | — |
| 318 | C1 @13:30:03 | 170.65 | −76.50 | 69.23 | 76.98 | **−222.71** | **−2,174.54** |
| 318 | C2 @13:35:06 | 177.85 | 139.50 | 69.23 | 78.52 | **−8.25** | **−1,960.08** |
| **319** | Reference (manual) | booked | 3,094.00 | 60.01 | 52.69 | **2,981.30** | — |
| 319 | C1 @13:20:08 | 62.30 | 9.75 | 60.01 | 65.72 | **−115.98** | **−3,097.28** |
| 319 | C2 @13:35:06 | 60.75 | −91.00 | 60.01 | 65.00 | **−216.01** | **−3,197.31** |
| **320** | Reference (manual) | booked | 1,361.75 | 57.42 | 51.99 | **1,252.34** | — |
| 320 | C1 @10:05:24 | 47.20 | −1,257.75 | 57.42 | 56.94 | **−1,372.12** | **−2,624.46** |
| 320 | C2 @11:30:42 | 48.25 | −1,189.50 | 57.42 | 55.71 | **−1,302.63** | **−2,554.97** |
| **321** | Reference (manual) | booked | 1,560.00 | 65.10 | 53.04 | **1,441.86** | — |
| 321 | C1 @10:05:24 | 106.70 | −1,827.00 | 65.10 | 61.22 | **−1,953.31** | **−3,395.17** |
| 321 | C2 @11:20:48 | 80.50 | −2,613.00 | 65.10 | 55.89 | **−2,733.99** | **−4,175.85** |
| **total** | **Reference** | | | | | **+7,627.33** | — |
| | **C1** | | | | | **−3,664.12** | **−11,291.45** |
| | **C2** | | | | | **−4,260.88** | **−11,888.21** |

**Gross/net qualification.** All four Reference figures are the booked amounts; **321's net
₹1,441.86 remains pending exact closing fee inputs** (its fill's per-leg prices are not persisted —
E1), with gross ₹1,560.00 reconciled. Challenger nets are fully modelled from the quoted legs at
the challenger exit, so they carry no such qualification.

### 2.3 Downside, giveback and missed continuation

Extrema are **gross**, from `policy_trace_json.trusted_mfe`/`trusted_mae`.

| pos | trusted peak | trusted trough | Reference gross | giveback from peak | C1 missed continuation | C2 missed continuation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 318 | 4,476.00 | −402.00 | 2,079.00 | 2,397.00 | **2,155.50** | 1,939.50 |
| 319 | 4,036.50 | −230.75 | 3,094.00 | 942.50 | **3,084.25** | 3,185.00 |
| 320 | 1,673.75 | −1,755.00 | 1,361.75 | 312.00 | **2,619.50** | 2,551.25 |
| 321 | 2,775.00 | −3,595.50 | 1,560.00 | 1,215.00 | **3,387.00** | 4,173.00 |

Both challengers exit **into** the adverse excursion rather than avoiding it: on 320 and 321 the
C1 exit is taken at −1,257.75 and −1,827.00, i.e. the challenger *realises* a drawdown the position
subsequently recovered from. **No avoided loss appears anywhere in the four positions** — the
challengers' only effect is to convert four profitable outcomes into four losses.

### 2.4 Dates and dependence

**Two distinct trading dates**, not four observations:

- 318 and 319 trigger on **20 November**, both `BULL_CALL` (long calls) in the same session, both
  exited 23 November 09:17. Highly dependent.
- 320 and 321 trigger on **23 November**, both `BEAR_PUT` in the same session, same direction
  regime (`TREND_DOWN`). Highly dependent.

So the effective sample is **two independent session-direction episodes**, not four trades, and the
managed position adds nothing because both its challenger endpoints are blocked. With n ≈ 2 this
**cannot establish a general rule** — which is why the result matters only in one direction: it
cannot license adopting a pullback exit, and the magnitude and unanimity (4 of 4, −₹11.3k to
−₹11.9k) make it ample grounds to **stop pursuing one**.

### 2.5 Decision, mechanism, counterexamples, uncertainty

**Decision: NO.** Neither pullback response improves the observed exit outcome, and both do severe
damage. Both are **parked**. **No fresh-session comparison is justified**, because the failure is
not marginal.

**Reusable mechanism — why it fails, and this is the transferable part.** A PULLBACK publication is
by construction a **counter-trend signal within an intact trend**: `|z_open| ≥ 0.50` says the
session trend is still strong, while `z_30` of the opposite sign says the last half hour went the
other way. Exiting a trend-aligned position on that signal sells the retracement of the move you
are positioned for. On 320 and 321 — BEAR_PUT positions in a `TREND_DOWN` regime — every PULLBACK
is the index bouncing against them, which is exactly when a holder should not be forced out. **The
rule is structurally a whipsaw generator, not a risk control**, and the installed policy's choice
to require two *opposite-direction* confirmations rather than NEUTRAL ones now looks deliberate and
correct.

**Counterexamples within the sample.** 318's C2 (−₹8.25) nearly breaks even and is far better than
its C1 (−₹222.71), so waiting for confirmation does help *relative to C1* on that one position. But
319 reverses it — C2 (−₹216.01) is worse than C1 (−₹115.98). So confirmation is not consistently
protective even inside four observations, and no ordering between C1 and C2 is established.

**Uncertainty.** Two independent episodes. All four positions are debit spreads held in
trend-aligned directions; nothing here speaks to a counter-trend position, to a losing position, or
to a session that actually reversed and kept going. The sample contains **no sustained-reversal
case**, which is precisely the case a pullback exit would exist to handle — so the test is, in
fairness to the challengers, run on the population least favourable to them. That is a real
limitation and I am stating it rather than claiming a general refutation.

---

## Boundaries

Read-only database access. Review branch only. b535 frozen; no exit rule changed, retuned or
activated, and both challengers are parked. Original rows preserved. No production change, merge,
release, version bump or candidate publication. No substitution for the December BNF 64,000 PE.
Manual Paper accessible, Real disabled, single-leg selling excluded.
