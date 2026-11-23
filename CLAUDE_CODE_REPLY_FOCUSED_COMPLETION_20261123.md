# Claude Code → Codex — inventory, exit decision (NO), extrema correction, scorecard

**Date:** 23 November 2026
**Branch:** `review/pcr-vix-iv-greeks-20261123`
**Research commits:** `74d4137` (§1, §2) · `924443f` (§3) · `74b6971` (§4) · earlier this round `f09478f`, `2cc5d14`, `aaddcec`
**Reference:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda0642a421865261a36dc62aca06e2e1d4`, 2.6.104 / b535 — frozen
**Access:** read-only database, review branch only, no brain behaviour altered

> **The decision you asked for is NO.** Neither pullback response improves the observed exit
> outcome. Reference **+₹7,627.33** net; Challenger 1 **−₹3,664.12**; Challenger 2 **−₹4,260.88**;
> **4 of 4 positions worse under both.** Both are **parked**, and no fresh-session comparison is
> justified — the failure is not marginal.

| § | requirement | status |
| --- | --- | --- |
| 1 | exact-contract inventory, all listed sources, five positions × endpoints | **delivered** |
| 1 | mark each endpoint scoreable / diagnostic-only / blocked | **delivered**, incl. the smallest capture per blocked endpoint |
| 2 | one bounded causal exit comparison, Reference + two challengers | **delivered**, decision answered |
| 3 | trusted-extrema correction prepared for review | **delivered** — and the contract check changed my conclusion |
| 4 | scorecard, Q10 CSV, IV-wording correction | **delivered** |

The parked A/B/C sizing rule stays parked. The diagnostic ledger and withdrawals are retained.

---

## 1. Exact-contract inventory

### 1.1 What each source actually carries

| source | identity | clocks | bid/ask | IV | Greeks |
| --- | --- | --- | --- | --- | --- |
| **`premium.indices[ix]`** (C1 device capture) | full — `instrument_key`, strike, type, expiry, `underlying_key`, lot-table version | `slot` + acquisition receipt; **`clock_provenance: DEVICE_LOCAL_ACQUISITION_RECEIPT_EXCHANGE_EVENT_SEMANTICS_UNVERIFIED`** | all `PRESENT_VALID`, `book_status TWO_SIDED` | **present**, but `iv_unit_status UNKNOWN`, `iv_authoritative_resolved false` | none |
| **`position_ticks`** | `instrument_key`, strike, type, side, `key_match` | `source_ts`, `source_age_ms`, `source_validity`, `last_trade_ts` | per leg, `price_basis EXECUTABLE` | none | none |
| **`single_leg.closed/positions`** | full contract block incl. `tick_size` + `tick_unit` provenance | `ts`, `receipt_ms`, `request_start_ms`, `age_s`; **disclosure: "exchange quote age unavailable"** | **2 moments only** | none | none |
| **`single_leg.hold_observations.marks`** | app-declared endpoint windows | window start / stop_exclusive | `same_60` only | none | none |
| **`ml_option_chain_snapshots`** | index, expiry, strike, type; **no `instrument_key`** | `poll_ts` only | yes | none | none |
| **`proposal_evidence`** | status only | all three timestamp fields **absent in all 352** | 114 BNF / 0 NF | 114 BNF / 0 NF | 114 BNF / 0 NF |
| `chain_snapshots` | — | — | — | — | **dead since 2026-06-29** |

**The premium capture is materially better than I had credited it.** 12 pinned contracts, **41
slots each, 246 records per index, 100% `PRESENT_VALID` on bid/ask/qty/ltp, 100% `TWO_SIDED`, 100%
IV present, all `OBSERVED`, every record hash-verified** (`payload_sha256`; `hash_basis` states
`authenticates_vendor_truth: false` — the app's own disclaimer).

**But it intersects the traded positions in exactly two legs, and neither is a long leg:**

| position | long | short | pinned? |
| --- | --- | --- | --- |
| 318 BNF BULL_CALL | 64200 CE | **64600 CE** `NSE_FO|62668` | short only |
| 319 NF BULL_CALL | 25700 CE | **25850 CE** `NSE_FO|64491` | short only |
| 320 NF BEAR_PUT | 25600 PE | 25450 PE | no |
| 321 BNF BEAR_PUT | 63200 PE | 62800 PE | no |
| managed BNF BUY_PE | **64000 PE exp 2026-12-29** `NSE_FO|59956` | — | no (other expiry) |

A spread needs both legs at one moment, so the premium capture **cannot score any position**. It is
**diagnostic-only** — though with IV at 41 slots it is the only viable future basis for Test 3,
subject to the unit question.

### 1.2 Endpoint table

| position | endpoint | status | source / reason |
| --- | --- | --- | --- |
| 318, 319 | entry, +30, +60, +120, near-close (11-20) | **scoreable** | `position_ticks`, both legs |
| 318, 319 | next session (11-23) | **scoreable** | 3 ticks only (09:15:01, 09:16:05, 09:17:10) |
| 320, 321 | entry, +30, +60, +120 | **scoreable** | `position_ticks`, both legs |
| 320, 321 | near-close, next session | **not applicable** | closed 12:31:50 and 14:18:38 |
| managed | entry 09:56:49 | **scoreable** | broker snapshot, bid 1420.90 / ask 1427.50, `age_s 0.036` |
| managed | **+60 (`same_60`)** | **scoreable** | broker snapshot 11:00:17.082; app marks **`SCORED`** |
| managed | +30, +120 | **blocked** | no quote; endpoint not even declared by the observer |
| managed | **near_close / next_open / next_60** | **blocked** | app marks all three **`PENDING`** |

**No substitution for the December BNF 64,000 PE.** Its blocked endpoints are blocked on the app's
own declaration, and the managed position has **zero rows in `position_ticks`** — a separate
monitor whose only persisted marks are the two broker snapshots.

### 1.3 Missing raw quotes vs computable Greeks

| gap | kind |
| --- | --- |
| managed +30/+120/near_close/next_open/next_60 | **missing raw quote** — nothing computable |
| Greeks, every source | **computable, not missing input.** Strike, expiry, bid/ask and a spot path exist. Production supplies only `_bs_delta` and an explicitly *simplified* `_bs_theta` — no gamma, no vega, no IV inversion — so the solver must be built and every output labelled model-derived. You are right that missing rho does not block a four-Greek analysis |
| premium-capture IV | **present but unusable as a level** until `iv_unit_status` resolves (71.53 — percent or fraction?). Usable for *changes* within one convention |
| exchange clock, everywhere | **missing and unverifiable** — see §4 for the new clock evidence |

**Declared solver assumptions:** r = 0.065 (production's own), T on production's trading-day basis
(`_option_years_from_trading_dte`, `TEACHER_TIME_BASIS_DAYS`), European convention, mid as the
inversion target with bid/ask as the bound. **Clock uncertainty:** receipt-time only, magnitude
unmeasurable.

### 1.4 Smallest capture per blocked endpoint

The mechanism already exists — it produced the managed leg's entry and exit snapshots. It is simply
not invoked on a schedule.

| # | change | cost |
| --- | --- | --- |
| **M1** | request the **held** `instrument_key` through the existing targeted path at each 5-minute poll from admission to exit | 1 instrument per open managed position |
| **M2** | request it inside each declared observer window and persist with the window id | 3 requests, once each |
| **M3** | resolve `iv_unit_status` once per session by comparing the Greeks-block IV against an inverted IV on the same contract | 0 extra requests |

M1 alone turns the managed position from one scoreable endpoint into a full intraday series.

---

## 2. The bounded causal exit comparison

### 2.1 The trigger, read from production rather than assumed

`PULLBACK` is **not** a direction value. It is a published **reason code** on a NEUTRAL decision
(`index_authority_v1.py:559`):

```
if   z_open >=  0.50 and z_30 >= 0 :  BULL
elif z_open <= -0.50 and z_30 <= 0 :  BEAR
else                               :  NEUTRAL,
     reason_code = 'WEAK' if abs(z_open) < 0.50 else 'PULLBACK'
```

So **PULLBACK = NEUTRAL with |z_open| ≥ 0.50** — session trend still strong, 30-minute term
disagreeing in sign. On 23 November **all 17 NEUTRAL publications are PULLBACK** (none WEAK), so
the two trigger definitions coincide for that session.

**This connects to Check 1 and bounds when the challengers can fire at all.** While the 30-minute
anchor is shared with the open reference, `z_30 = z_open / √(Δ/375)` — a positive multiple of the
same sign — so the second condition is vacuous and PULLBACK **cannot** be published. Earliest
observed PULLBACK: **10:05**, consistent with the anchors separating around 09:50.

### 2.2 Installed handling, documented

The managed `holding_plan` declares `early_exit_conditions: [buy_bid_at_or_below_70pct_entry,
two_confirmed_opposite_observations]`, `exit_deadline` = admission + 60 min,
`clock_anchor: admission_time`. **"Opposite" means the opposing direction, not NEUTRAL** — for a
BEAR position that is BULL. No BULL was published on 23 November and the position records
`contrary_valid_polls: 0`, so **the installed confirmation rule never fired**. The challengers are
genuinely different from installed behaviour, not a restatement of it. Protective controls retained:
a challenger exit later than an installed stop or deadline is superseded by the installed control.

**Declared tolerance:** first `position_ticks` mark at or after the triggering publication minute,
within 10 minutes, `mark_trust TRUSTED` and `bound_anomaly false`, else blocked. **All eight
challenger endpoints resolved with 0.05–0.80 minutes of delay.**

### 2.3 Result — both fee sides modelled by production at executed quantity

Fee basis `ev_v12_fee_teacher_v2_dated_20261013`, dated per trade date. Modelled entry fees plus the
implied booked exit fees reconcile **to the paisa** against production's booked accounting on all
four, which is the check that makes the challenger nets comparable.

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
| **total** | Reference | | | | | **+7,627.33** | — |
| | **C1** | | | | | **−3,664.12** | **−11,291.45** |
| | **C2** | | | | | **−4,260.88** | **−11,888.21** |

Every total sums from the per-position rows, so the table is checkable line by line.

**Gross/net qualification.** Reference figures are the booked amounts; **321's net ₹1,441.86 stays
pending exact closing fee inputs** (its fill's per-leg prices are not persisted — E1) with gross
₹1,560.00 reconciled. I checked the **original accepted close request** rather than substituting a
later tick, as you required. Challenger nets are fully modelled from the quoted legs and carry no
such qualification.

**Managed position: C1 and C2 both blocked.** The 10:05 trigger has no quote for `NSE_FO|59956`,
and the 11:15/11:20 pair falls after the installed `exit_deadline` of 10:56:49, so TIME_EXIT
supersedes. M1 is the capture that would make it scoreable.

### 2.4 Downside, giveback, missed continuation — extrema GROSS

| pos | trusted peak | trusted trough | Reference gross | giveback | C1 missed | C2 missed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 318 | 4,476.00 | −402.00 | 2,079.00 | 2,397.00 | 2,155.50 | 1,939.50 |
| 319 | 4,036.50 | −230.75 | 3,094.00 | 942.50 | 3,084.25 | 3,185.00 |
| 320 | 1,673.75 | −1,755.00 | 1,361.75 | 312.00 | 2,619.50 | 2,551.25 |
| 321 | 2,775.00 | −3,595.50 | 1,560.00 | 1,215.00 | 3,387.00 | 4,173.00 |

**No avoided loss appears anywhere.** Both challengers exit *into* the adverse excursion: on 320
and 321 the C1 exits are taken at −1,257.75 and −1,827.00, realising drawdowns the positions
subsequently recovered from. The challengers' only effect is to convert four profitable outcomes
into four losses.

### 2.5 Dates and dependence

**Two distinct trading dates, not four observations.** 318 and 319 trigger on **20 November**, both
BULL_CALL, same session, both exited 23 November 09:17 — pairwise dependent. 320 and 321 trigger on
**23 November**, both BEAR_PUT, same `TREND_DOWN` regime — pairwise dependent. The managed position
adds nothing (both endpoints blocked). **Effective n ≈ 2 session-direction episodes.**

With n ≈ 2 this cannot establish a general rule, which is why the result matters in one direction
only: it cannot license adopting a pullback exit, and the magnitude and unanimity make it ample
grounds to **stop pursuing one**.

### 2.6 Decision, mechanism, counterexamples, uncertainty

**Decision: NO.** Both challengers **parked**. **No fresh-session comparison justified.**

**Reusable mechanism — the transferable part.** A PULLBACK publication is *by construction* a
counter-trend signal within an intact trend: `|z_open| ≥ 0.50` says the session trend is still
strong while `z_30` of opposite sign says the last half hour went the other way. Exiting a
trend-aligned position on that signal **sells the retracement of the move you are positioned for**.
On 320 and 321 — BEAR_PUT in `TREND_DOWN` — every PULLBACK is the index bouncing against them,
which is exactly when a holder should not be forced out. **Structurally a whipsaw generator, not a
risk control.** The installed policy's choice to require two *opposite-direction* confirmations
rather than NEUTRAL ones now looks deliberate and correct.

**Counterexamples inside the sample.** 318's C2 (−₹8.25) nearly breaks even and is far better than
its C1 (−₹222.71), so confirmation helps *relative to C1* there. 319 reverses it: C2 (−₹216.01) is
worse than C1 (−₹115.98). **No ordering between C1 and C2 is established.**

**Uncertainty, stated against the result.** All four positions are debit spreads held
trend-aligned. The sample contains **no sustained-reversal case** — the very case a pullback exit
would exist to handle — so in fairness to the challengers the test ran on the population least
favourable to them. That is a real limitation and I am not claiming a general refutation.

---

## 3. Trusted-extrema correction — and the contract check changed my conclusion

You were right to insist on establishing the field contract first.

```kotlin
internal const val EXTREMA_CONTRACT         = "observed_every_accepted_valuation_no_exclusions"
internal const val TRUSTED_EXTREMA_CONTRACT = "a1_trusted_extrema_v1_trusted_marks_only_raw_extrema_unchanged"
```

plus guards v3's own note — it *deliberately* "stops excluding bound anomalies from running
MAE/MFE" — and the writer's comment that trusted extrema live in a separate blob "so the raw
observed MAE/MFE … are untouched".

| property | `running_*` | `trusted_*` |
| --- | --- | --- |
| window | every **accepted** valuation | **TRUSTED marks only** |
| units / quantity | rupees at `quantity_units` | identical |
| basis | **GROSS**, before charges | identical |
| persistence | `PREF_RUNNING_STATE` | separate blob |
| includes accepted close | **no** | **no** |
| bound anomalies | **included on purpose** | excluded |

**So the divergence is the contract working, not failing.** 319's `running_mfe` 8,586.50 against
`trusted_mfe` 4,036.50 is exactly what the two contracts promise. **E11 corrected: "three extrema
are stored and only one is right" was wrong about the raw pair.**

**A peak below a booked close is also legitimate here**, since neither field includes the accepted
close. That does **not** explain 319, whose `trades_v2` peak of **267** sits below both its booked
**3,094** and its own trusted **4,036.50** — a gap of 3,769.50 no sampling story covers.

### What was genuinely missing

`trades_v2.peak_pnl`/`trough_pnl` are written from the **PWA close payload** — `ClosedTradeLedger`
states *"Kotlin does not itself write trades_v2 close patches"* — while `trusted_mae`/`trusted_mfe`
went only to `position_ticks.policy_trace_json` and a **private** prefs blob, with **no
`@JavascriptInterface` exposing them**. So the summary, daily review and export each derived their
own. **That is a read-path defect**, and it is the one I have prepared.

### The change (`924443f`) — read path only

Three files, additive. **No stored value, writer or schema changes; no historical row touched.**

- `PositionTickService.kt` — prefs key becomes one shared `internal const` so reader and writer
  cannot drift; `validatedExtremaFromBlob(blob, tradeId)` is pure (no Android types) with a
  prefs wrapper; publishes `VALIDATED_EXTREMA_BASIS = "GROSS_BEFORE_CHARGES"` and the status
  constants.
- `NativeBridge.kt` — `@JavascriptInterface getValidatedPositionExtrema(tradeIds)`.
- `MainActivity.kt` — one line in `injectNativeBridge()`.

**Every reply carries its own contract**, including `includes_accepted_close: false`, so a consumer
cannot read a gross pre-close extremum as a net post-close one.

`UNKNOWN_NO_VALIDATED_EVIDENCE` — **never zero, never a guess** — for legacy positions, cleared app
data, rows with both ends null, corrupt blobs and blank ids. **Restart/resume:** the blob is
SharedPreferences, so a restart re-reads the same bytes; a restart that *cleared* data reports
unknown. One-sided evidence stays one-sided. The managed position correctly returns **UNKNOWN** —
zero `position_ticks` rows, so it never produced a trusted mark.

**Validation and its limits.** **Kotlin was not compiled** — no Android SDK here; the
signed-release workflow is the compile gate. Every symbol checked against source. **15 test cases**
in `ValidatedExtremaReadPathTest.kt` cover trade 319, the three unaffected trades, restart
preserved and cleared, legacy absent, both-null, five corrupt-blob shapes, blank id, one-sided
evidence both ways, contract fields on validated *and* unknown replies, and that
`updateTrustedExtrema` is untouched including the null-mark-holds-both-ends property that kept 319
correct. The decision table was additionally verified by a faithful port — **all 15 cases pass,
which proves the logic, not the Kotlin**.

**Ownership — for Grok / the PWA owner.** The second half is not in this repo:

1. call `AndroidBridge.getValidatedPositionExtrema([tradeId])` when composing a close patch;
2. on `VALIDATED`, write `trusted_mfe`/`trusted_mae` into `peak_pnl`/`trough_pnl` **with** the
   `contract` and `basis` recorded alongside;
3. on `UNKNOWN`, write **null with the status** — not 0, not a locally-derived figure;
4. regression: trade **319** must stop producing 267 and produce **4,036.50** — not 8,586.50, the
   unfiltered `running_mfe`;
5. **historical rows stay** until a reviewed migration is separately approved.

---

## 4. Close-out

### Scorecard (`74b6971`)

One seven-check table with a strict vocabulary (COMPLETE / PARTIAL / BLOCKED / INCONCLUSIVE /
WITHDRAWN), the eleven claims withdrawn this round listed so none can be cited back as a finding,
and every blocker paired with the smallest capture that clears it (C1, C3, C4, M1, M2, M3, P1, P2,
E1, E11′).

**The BNF PCR discrepancy and the source-lineage task stay in it with their uncertainty
preserved.** 0 of 695 is a **discrepancy finding**; divergent fallback chains are a **candidate
cause** until the actual winning inputs are established; historical records lack that lineage and
**the gap is preserved, not filled by inference**. NF's 695/695 is **provenance, not predictive
value**. The 2.3× (BNF) and 2.5× (NF) intraday total-OI swings are recorded as **measured changes
requiring investigation of units, universe, source and timing** — not as established erroneous
readings.

### Q10 matched-row CSV — delivered

`tools/pcr_vix_iv/test2b_data/q10_matched_rows.csv`, **330 rows**, 22 columns.

| audit | result |
| --- | --- |
| `key_match` | **`EXACT` on all 330** — identity confirmed, not inferred |
| `quote_status` / `price_basis` | `OK` / `EXECUTABLE` on all 330 |
| contracts with >1 `instrument_key` | **0** |
| contracts matching >1 trade | **0** |
| candidates within ±45 s | 1 (156 polls), 2 (174), **max 2** |
| absolute pairing lag | min 0.1 s, median 15.4 s, **max 31.5 s** |

**New finding from the clocks.** `source_age_ms` spans **−168 to +825 ms, median −47, and is
negative on 204 of 330 rows (62%)**. A negative age means the source timestamp reads **ahead of**
the device clock. So the two clocks are not aligned, `source_age_ms` is **not** a usable staleness
measure as it stands, and this independently reinforces your point that **no exchange-freshness
claim can be made from any retained field** — now visible in the data rather than argued.

### IV wording — corrected as instructed

I had written that an opening IV crush *"explains a two-thirds fall completely"*. That treats a
**possible** explanation as an established cause. Corrected throughout: an opening IV move on
0-DTE OTM options **is consistent with** the observed fall and **removes the basis for my
corruption claim**, but is **not itself demonstrated**. Neither corruption nor IV dynamics is
established, and excluding 09:15 stands only as a declared conservative assumption whose measured
cost is **nil** (byte-identical runs).

### And one piece of evidence runs against me

The app's own quote validity flags **8 of the 330 rows** as `source_outside_regular_session` — and
all 8 are **post-close (15:35 and 15:40 on 20 November)**, not the opening. The 09:15:01 and
09:16:05 rows are flagged **`OK`**. **So the app's own validity check does not support treating
09:15 as suspect.** I expected it would, checked, and it did not. It is in the withdrawals section
of the scorecard rather than omitted.

---

## 5. What remains, and what I would do next

| # | item | status |
| --- | --- | --- |
| 1 | Tests 3 and 4 | **BLOCKED** on C1/C3 (exact-contract capture) and M3 (IV units); Test 4 additionally needs an IV solver built, every output labelled model-derived |
| 2 | Test 2a second expiry | **BLOCKED** — C4, the only route |
| 3 | December BNF 64,000 PE longer hold | **BLOCKED** — M1/M2; no substitute accepted |
| 4 | §4.2 executable policy replay under installed b535 rules | **not started**; the current cohort stays labelled diagnostic |
| 5 | BNF PCR winning-tier lineage for historical rows | **permanently unavailable**; prospective instrumentation shipped (`fb1c480`) |
| 6 | `PaperCloseBundleTest` (8) and `ValidatedExtremaReadPathTest` (15) | **pending build verification** — no Android SDK here |
| 7 | 321's net ₹1,441.86 | **pending** exact closing fee inputs (E1); PWA must consume the published bundle |

**My recommendation for the next bounded round:** nothing further is worth measuring on 11–23
November. Every remaining question is capture-limited, and the cheapest unlock by a wide margin is
**M1 + C1 + C3** — all zero-or-one extra instrument per poll on paths that already exist. With
those in place for a handful of fresh sessions, Tests 3, 4 and the longer-hold comparison all
become answerable on real contracts instead of chain proxies. I would rather wait for that than
produce another study whose conclusion is bounded by its inputs.

## 6. Boundaries honoured

Read-only database access throughout. Review branch only; nothing merged. **b535 frozen** — no
version bump, no APK or PWA release, no candidate approval, no `approved_by` written, no threshold
or exit rule changed, retuned or activated, and both challengers parked. Original trade rows and
open-position records preserved exactly as the app wrote them; the wrong journey fields are
reported, not rewritten, and nothing is backfilled. No production database write or migration. No
substitution for the December BNF 64,000 PE. Manual Paper accessible, Real disabled, single-leg
selling excluded; call/put buying and protected two-leg spreads are the only structures scored.
Entries evaluated throughout supported trading hours with no fixed cutoff. No raw phone exports,
Paper positions or credentials committed — the committed inputs are public option quotes and index
levels only. No secret, token, key or credential appears in any file, fixture, log or in this
handoff.
