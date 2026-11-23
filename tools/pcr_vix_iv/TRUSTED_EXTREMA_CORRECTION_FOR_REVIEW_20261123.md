# Trusted-extrema correction — prepared for review, not for integration

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**In reply to:** `CODEX_REPLY_CLAUDE_FOCUSED_COMPLETION_20261123` §3
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, b535 frozen
**Status:** **requires review before integration.** Kotlin could not be compiled here.

> **Establishing the field contract first changed my conclusion.** You were right to insist on it.
> The `running_*` vs `trusted_*` divergence I called a defect is **by design**, and I am
> withdrawing that part of E11. What remains is a genuine and narrower gap: the validated pair is
> not readable outside the service that computes it.

---

## 1. The field contract, verified before anything is called erroneous

Read from the source, not inferred:

```kotlin
internal const val EXTREMA_CONTRACT         = "observed_every_accepted_valuation_no_exclusions"
internal const val TRUSTED_EXTREMA_CONTRACT = "a1_trusted_extrema_v1_trusted_marks_only_raw_extrema_unchanged"
```

and from `POSITION_TICK_GUARDS_VERSION`'s own history note — v3 deliberately *"stops excluding
bound anomalies from running MAE/MFE (EXTREMA_CONTRACT: every accepted valuation contributes)"* —
plus the writer's comment: *"trusted extrema live in their own prefs blob so the raw observed
MAE/MFE … are untouched."*

| property | `running_mae` / `running_mfe` | `trusted_mae` / `trusted_mfe` |
| --- | --- | --- |
| **observation window** | every **accepted** valuation | **TRUSTED marks only** |
| **units / quantity** | rupees at `quantity_units` | identical |
| **gross or net** | **GROSS** — mark − entry premium, × quantity, before charges | identical |
| **persistence** | `PREF_RUNNING_STATE` | `PREF_TRUSTED_EXTREMA_STATE`, separate blob |
| **includes the accepted close** | **no**, unless a tick happened to observe it | **no**, same |
| **bound anomalies** | **included on purpose** (v3) | excluded |

**So the divergence is the contract working, not failing.** On trade 319 `running_mfe` 8,586.50 and
`trusted_mfe` 4,036.50 differ because one tick was rejected — exactly what the two contracts
promise. **Neither field is wrong**, and my earlier "three extrema are stored and only one is
right" was wrong about the raw pair. Corrected below.

**And a peak below a booked close is legitimate here.** Neither field includes the accepted close,
so a position whose best *sampled* mark preceded a better exit will show a peak under its booked
P&L. That is correct under the contract. It does **not** explain trade 319, whose `trades_v2`
peak of **267** sits below its booked **3,094** *and* below its own trusted peak of **4,036.50** —
a gap of 3,769.50 that no sampling story covers.

---

## 2. The gap that is real

`trades_v2.peak_pnl` / `trough_pnl` are **not written by this repo**. From `ClosedTradeLedger`:

> *"the pending/closed JSON row is opaque — peak_pnl / trough_pnl and extrema validity metadata
> supplied by **the PWA close payload** are retained verbatim … **Kotlin does not itself write
> trades_v2 close patches**."*

And `trusted_mae` / `trusted_mfe` were written to exactly two places, **neither reachable by the
PWA**: `position_ticks.policy_trace_json` (server-side, after the fact) and a **private** prefs
blob. There was no `@JavascriptInterface` exposing them — verified by grep: the only occurrences
outside the service are none.

**So the summary, daily review and export each had to derive their own extrema, because the
validated pair was not available to them.** That is the defect, and it is a **read-path** defect.

---

## 3. The change — a read path only

Three files, additive, reusing the existing validated path. **No stored value changes, no writer
is touched, no historical row is modified.**

| file | change |
| --- | --- |
| `PositionTickService.kt` | the prefs key becomes `internal const PREF_TRUSTED_EXTREMA_BLOB` (one spelling, shared by reader and writer so they cannot drift); adds `validatedExtremaFromBlob(blob, tradeId)` — pure, no Android types — plus a prefs-backed wrapper; publishes `VALIDATED_EXTREMA_BASIS = "GROSS_BEFORE_CHARGES"` and the two status constants |
| `NativeBridge.kt` | adds `@JavascriptInterface getValidatedPositionExtrema(tradeIds)`, taking a JSON array of ids and returning per-id `{status, contract, basis, includes_accepted_close, trusted_mae?, trusted_mfe?, updated_at?}` |
| `MainActivity.kt` | one line in `injectNativeBridge()` so the PWA can call it |

**Every reply carries its own contract**, so a consumer cannot read a gross pre-close extremum as
a net post-close one. `includes_accepted_close: false` is stated explicitly on every reply,
including the unknowns.

### Legacy and failure handling, as you required

`UNKNOWN_NO_VALIDATED_EVIDENCE` — **never zero, never a guess** — for all of:

- a position closed before this contract shipped (id absent from the blob);
- a restart that cleared app data (blob absent);
- a row present with both ends null (no TRUSTED mark ever observed);
- a corrupt or unparseable blob;
- a blank trade id.

**Restart / resume:** the blob is SharedPreferences, so a process restart or service resume
re-reads the same bytes — nothing is recomputed and nothing is lost. A restart that *cleared* data
yields `UNKNOWN`, not a silent 0.0. Both cases are tested.

**One-sided evidence is preserved as one-sided:** if only a favourable mark was ever trusted,
`trusted_mfe` is returned and `trusted_mae` is **absent**, not 0.0.

---

## 4. Validation, and its limits

**Kotlin was not compiled.** There is no Android SDK in this sandbox; the signed-release workflow
remains the compile gate. Every symbol used was checked against source (`optNullableDouble`,
`putOptNumber` are file-private in the same file; `JSONArray`/`JSONObject`/`Log`/`TAG`/`prefs` all
in scope in `NativeBridge`).

**15 Kotlin test cases** in `app/src/test/java/com/marketradar/app/ValidatedExtremaReadPathTest.kt`
covering trade 319, the three unaffected trades, restart-preserved, restart-cleared, legacy absent,
both-null, five corrupt-blob shapes, blank id, one-sided evidence in both directions, the contract
fields on validated *and* unknown replies, and that `updateTrustedExtrema` — the only writer — is
unchanged, including the null-mark-holds-both-ends property that kept 319 correct.

**Logic verified by a faithful port**, since the Kotlin cannot run here: all 15 decision cases pass
(`/tmp/.../scratchpad/extrema_logic.py`). The port is behaviourally equivalent at the boundaries
but not line-identical — `JSONObject("[1,2,3]")` and `JSONObject("null")` *throw* in org.json and
are caught, where the Python guards on type instead. Both reach `UNKNOWN`. **This proves the
decision table, not the Kotlin.**

### Before / after evidence

| trade | before: what a consumer could read | after: `getValidatedPositionExtrema` |
| --- | --- | --- |
| 318 | `trades_v2.peak_pnl` 3,776 | `trusted_mfe` **4,476.00**, `trusted_mae` −402.00, `VALIDATED` |
| **319** | `trades_v2.peak_pnl` **267** (below its own booked 3,094) | `trusted_mfe` **4,036.50**, `trusted_mae` −230.75, `VALIDATED` |
| 320 | 1,645 | **1,673.75** / −1,755.00, `VALIDATED` |
| 321 | 2,457 | **2,775.00** / −3,595.50, `VALIDATED` |
| managed `sl1_b3ae…` | — | **`UNKNOWN_NO_VALIDATED_EVIDENCE`** — it has zero `position_ticks` rows, so it never produced a trusted mark |

That last row is the honest outcome of the contract, not an omission: the managed single-leg
position is monitored by a different path and this read path correctly declines to invent extrema
for it.

---

## 5. Ownership, and what I have not done

**The second half of this fix is not in this repo.** Exposing the read path does not change
`trades_v2`; the PWA must consume `getValidatedPositionExtrema` in its close payload. That is
MarketVivi, and I have not touched it.

**For Grok / the PWA owner:**

1. Call `AndroidBridge.getValidatedPositionExtrema([tradeId])` when composing a close patch.
2. On `VALIDATED`, write `trusted_mfe`/`trusted_mae` into `peak_pnl`/`trough_pnl` and record
   `contract` and `basis` alongside, so a reader knows they are gross and exclude the close.
3. On `UNKNOWN_NO_VALIDATED_EVIDENCE`, write **null with the status**, not 0 and not a
   locally-derived figure. A documented unknown is the required fallback.
4. Regression case: trade **319** must stop producing 267, and must produce 4,036.50 — not
   8,586.50, which is the unfiltered `running_mfe` including the tick the structural guard
   rejected.
5. **Historical rows stay as they are.** Nothing here backfills or rewrites; 318–321 keep their
   current values until a reviewed migration is separately approved.

**Not done:** no migration, no backfill, no change to any writer, and the Kotlin is uncompiled.

---

## 6. Register correction

> **E11 (corrected).** My claim that "three extrema are stored per position and only one is right"
> was **wrong about the raw pair**. `running_mae`/`running_mfe` and `trusted_mae`/`trusted_mfe`
> diverge **by design** — `EXTREMA_CONTRACT` admits every accepted valuation and
> `TRUSTED_EXTREMA_CONTRACT` admits only trusted marks, with the raw pair explicitly left
> unchanged. Both are correct for their declared purpose. What is real: the validated pair had
> **no read path outside PositionTickService**, so the summary, daily review and export each
> derived their own, and `trades_v2.peak_pnl` — written by the PWA, not by this repo — matches
> neither field on any of the four trades. Trade 319's stored 267 remains unexplained by any
> sampling or basis argument, since it is below both its booked 3,094 and its trusted 4,036.50.

## Boundaries

Review branch only; **requires review before integration**. b535 frozen — no version bump, no
release, no candidate publication. Historical rows preserved. Read path only: no stored value,
writer or schema changed. Kotlin uncompiled here; the signed-release workflow is the compile gate.
