# Seven-check scorecard

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Pinned:** Marketapp `8f9aa234a278528a09d58cd83a6aaa85254e2218`, MarketVivi `cbb65bda…`, 2.6.104 / b535 frozen
**Access:** read-only database, review branch only, no brain behaviour altered

Status vocabulary, used strictly: **COMPLETE** (calculation done, result stands) · **PARTIAL**
(some limbs done, blocker named) · **BLOCKED** (cannot proceed, exact missing input named) ·
**INCONCLUSIVE** (ran, cannot establish a difference) · **WITHDRAWN** (claim retracted).

---

## 1. The seven checks

| # | Check | Status | Result that stands | Blocker |
| --- | --- | --- | --- | --- |
| **0** | Trade reconciliation 318–321 + managed | **PARTIAL** | Gross reconciled on all five. Managed `sl1_b3ae…` **+₹3,070.09 net verified** to the paisa against production's dated fee functions — still the only fully verified after-cost Paper outcome | **321's net ₹1,441.86 pending**: its fill's per-leg prices are not persisted anywhere (E1), so the closing fee cannot be recomputed from retained evidence |
| **1** | Opening anchor / `z_open`, `z_30` | **COMPLETE** | While the 30-min anchor is shared with the open reference, `z_30 = z_open / √(Δ/375)` — a positive multiple of the same sign — so the BEAR/BULL gate's second condition is **vacuous** in that window. Verified: 23 Nov 09:35 BNF ratio exactly 5.000000; bounded to 09:30–09:50 across 778 decisions. This directly determines that **PULLBACK cannot be published before the anchors separate** | — |
| **2a** | Eligible-expiry comparison | **BLOCKED** | Exactly one expiry captured per session and index across 12–23 Nov. Never two | A **second expiry** at the decision poll. Unchanged |
| **2b** | Outright vs protected spread, arms A/B/C | **COMPLETE** | 333 scored observations, 431-row ledger. **Arm C refutes the rule I proposed**: costs ₹513.89/observation for a 10% improvement in the observed extreme while idling 59% of the budget, and is the worst of three against the outright. Rule **parked** | — |
| **3** | Exact-contract IV + India VIX | **BLOCKED** | IV exists for **12 pinned contracts, 41 slots each, 100% present, hash-verified** — but `iv_unit_status: UNKNOWN` and `iv_authoritative_resolved: false`, so it is usable for *changes* within one convention, not as a level. Only 2 of those contracts are position legs, both short | **IV unit resolution** (M3), and exact-contract capture for the traded legs (C1/C3) |
| **4** | Premium attribution / Greeks | **BLOCKED** | **No source carries Greeks.** Tick legs have 16 keys — quotes plus provenance — and none of delta/gamma/theta/vega/iv. Production computes only `_bs_delta` and an explicitly *simplified* `_bs_theta`: no gamma, no vega, no IV inversion | An **IV solver** must be built; every output would be model-derived and labelled. Not started |
| **5** | PCR / OI provenance | **COMPLETE (provenance) · BLOCKED (predictive)** | `nf_pcr` = `nf_total_put_oi / nf_total_call_oi` at **695 of 695** polls. **`bnf_pcr` at 0 of 695** (mean abs residual 0.20–0.69 against a ratio spanning 0.71–1.39). Ruled out: one-poll lag, near-ATM PCR (null at all 695), index mix-up, moving universe (226/158 strikes, one expiry, every poll). Divergent silent fallbacks are a **candidate cause**, not established | **Winning fallback tier was never recorded** for historical rows — lineage now instrumented prospectively (fix 3b, `fb1c480`); the historical gap is preserved. **OI freshness unverifiable**: no OI clock anywhere |
| **6** | Holds and exits | **COMPLETE** | Extrema rebuilt from raw leg quotes: **808 of 808** ticks reproduce `executable_mark` and `current_pnl` exactly (max gap 0.0000). All extrema **GROSS**. Every installed exit signal that fired was early. **Pullback challengers: Reference +₹7,627.33 vs C1 −₹3,664.12 and C2 −₹4,260.88, 4 of 4 worse — both parked** | Managed position's +30/+120/near-close/next-session endpoints **blocked** (app's own observer marks them `PENDING`); M1–M2 is the minimal capture |

---

## 2. Claims withdrawn this round

Recorded so none of them can be cited back as findings.

| claim | why it fell |
| --- | --- |
| **E2 sign argument**: the 09:15 chain is *proven* corrupt because puts fell while spot fell | 23 Nov was **0 DTE** and the strikes were **OTM**, so price is vol-dominated; an opening IV crush is a **possible** explanation. 09:15 is now **SUSPECT, NOT PROVEN CORRUPT** |
| the exclusion materially affects Test 2b | **measured: exactly zero.** Runs with and without 09:15 are byte-identical |
| timing explains the chain/tick divergence | **cannot be tested**: no tick interval under 55 s exists, so the pairing-lag comparison is impossible. **Inconclusive** |
| "the two arms are indistinguishable" | similar means do not establish equivalence → **inconclusive** |
| "roughly a 3× smaller tail" | a worst *observed* loss is an observed extreme with uncertainty, not a tail multiple |
| one lot is always affordable | 12 of 333 observations exceed the budget at one outright lot |
| "size the spread by the outright's lot count" (**my own proposed rule**) | arm C computed directly: worse on return, marginal on tail, 59% budget idle |
| Upstox supplies `exchange_timestamp` and the app drops it | **never verified against the vendor response.** The snapshot carries the key with a `null` value, which says nothing about what Upstox sends |
| "0 NF / 114 BNF" means no NF IV exists | that figure is **`proposal_evidence` only**; other sources were unexamined at the time |
| **E10**: add a structural `[0, width]` bound to `PositionTickService` | it **already exists**, fired on exactly the one impossible tick of 808, and kept `trusted_mfe` correct |
| **E11**: "three extrema are stored and only one is right" | wrong about the raw pair. `running_*` vs `trusted_*` diverge **by design** (`EXTREMA_CONTRACT` admits every accepted valuation; `TRUSTED_EXTREMA_CONTRACT` admits only trusted marks and leaves the raw pair unchanged) |

### Wording correction you asked for

I had written that an opening IV crush *"explains a two-thirds fall completely"*. That treats a
**possible** explanation as an established cause. Corrected throughout: an opening IV move on
0-DTE OTM options **is consistent with** the observed fall and **removes the basis for my
corruption claim**; it is not itself demonstrated. Neither corruption nor IV dynamics is
established, and the exclusion of 09:15 stands only as a declared conservative assumption whose
cost is nil.

**And one piece of evidence runs against me, so it goes here.** The app's own quote validity flags
**8 rows** as `source_outside_regular_session` across the 330 matched rows — and they are all
**post-close (15:35 and 15:40 on 20 November)**, not the opening. The 09:15:01 and 09:16:05 rows
are flagged **`OK`**. So the app's validity check does **not** support treating 09:15 as suspect. I
expected it would, checked, and it did not.

---

## 3. The Q10 matched-row CSV — delivered

`tools/pcr_vix_iv/test2b_data/q10_matched_rows.csv` — **330 rows**, 22 columns: full contract
identity, both clocks, source age, quote status, source validity, price basis, key match, trade id,
pairing lag and the duplicate-candidate count.

| audit | result |
| --- | --- |
| `key_match` | **`EXACT` on all 330** — instrument identity confirmed, not inferred |
| `quote_status` | `OK` on all 330 |
| `price_basis` | `EXECUTABLE` on all 330 |
| `source_validity` | 322 `OK`, **8 `source_outside_regular_session`** (all post-close, §2) |
| contracts with more than one `instrument_key` | **0** |
| contracts matching more than one trade | **0** |
| candidates within ±45 s | 156 polls with 1, 174 with 2, **max 2** |
| absolute pairing lag | min 0.1 s, median 15.4 s, **max 31.5 s** |

**New finding from the clocks.** `source_age_ms` runs **−168 to +825 ms, median −47, and is
negative on 204 of 330 rows (62%)**. A negative age means the source timestamp reads *ahead of* the
device clock. So the two clocks are not aligned, `source_age_ms` is **not** a usable staleness
measure as it stands, and this independently reinforces that **no exchange-freshness claim can be
made from any retained field** — the point you made in §2 of the earlier protocol, now visible in
the data rather than argued.

---

## 4. Exact remaining blockers, and the smallest capture that clears each

| # | blocker | clears | cost |
| --- | --- | --- | --- |
| **C1** | candidate contract not quoted at the decision poll | Test 3 and Test 4 at decision time, both indices | 1 instrument per index per poll |
| **C3** | identity fields and the three clocks not persisted with candidate records (absent in all 352) | freshness and identity for anything candidate-derived | 0 extra requests |
| **C4** | second expiry never captured | **Test 2a**, the only route | 2–6 instruments per poll |
| **M1** | held `instrument_key` not requested on a schedule between admission and exit | managed position's intraday series — turns 1 scoreable endpoint into a full one | 1 per open managed position per poll |
| **M2** | observer windows (`near_close`, `next_open`, `next_60`) never requested | the December BNF 64,000 PE longer-hold comparison. **No substitute accepted** | 3 requests, once each |
| **M3** | `iv_unit_status` unresolved | IV as a **level** rather than a change | 0 extra requests — compare the Greeks-block IV against an inverted IV once per session |
| **P1** | winning fallback tier not recorded | the **BNF PCR discrepancy**, diagnosable in one query instead of by elimination | 0 — shipped prospectively in `fb1c480`; historical rows stay unexplained |
| **P2** | no OI clock in any source | OI freshness, and distinguishing a genuine intraday PCR move from a batch refresh | requires validating what the vendor actually supplies first |
| **E1** | fill's per-leg prices not persisted on close | **321's net**, and any exact fee recomputation | native half shipped (`d2cc9f2`); the PWA must consume the published bundle |
| **E11′** | validated extrema had no read path | one consistent extrema series for summary, daily review and export | read path shipped for review (`924443f`); the PWA must consume it |

---

## 5. Items carried forward with their uncertainty preserved

**The BNF PCR discrepancy stays open.** 0 of 695 is a **discrepancy finding**, not a diagnosis.
Divergent fallback chains (`brain.py:11770-11796` five tiers for the totals, `brain.py:11912` three
for the PCR, with the BNF profile carrying no `pcr` key) remain a **candidate cause** until the
actual winning inputs are established. Historical records lack that lineage and **that gap is
preserved, not filled by inference**.

**NF's internal arithmetic is provenance, not predictive value.** `nf_pcr` reconciling 695/695
means the ratio agrees with its stored totals — nothing more. It is not evidence the OI is correct,
and the predictive limb stays **blocked**: total call OI swings 2.3× (BNF) and 2.5× (NF) within a
single session on a constant single-expiry universe, which I am recording as a measured change
requiring investigation of units, universe, source and timing — **not** as an established
erroneous reading.

**Kotlin remains uncompiled here.** `PaperCloseBundleTest` (8 cases) and
`ValidatedExtremaReadPathTest` (15 cases) are both **pending build verification**; there is no
Android SDK in this sandbox and the signed-release workflow is the compile gate. Python side: the
full suite is **172 of 172 modules green** with an adequate per-module budget.

## Boundaries

Read-only database access. Review branch only; nothing merged. b535 frozen — no version bump, no
APK or PWA release, no candidate approval, no `approved_by` written, no threshold or exit rule
changed, retuned or activated. Original trade rows and open-position records preserved. No
production database write, migration or backfill. Manual Paper accessible, Real disabled,
single-leg selling excluded. No raw phone exports, Paper positions or credentials committed; no
secret, token, key or credential in any file, fixture, log or handoff.
