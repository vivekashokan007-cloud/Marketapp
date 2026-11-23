# Quote-quality diagnostic, verified — and my Test 2b block withdrawn

**Date:** 23 November 2026 · **Branch:** `review/pcr-vix-iv-greeks-20261123`
**Supersedes:** the Test 2b verdict in `TEST2B_QUOTE_QUALITY_20261123.md`
**Queries:** `tools/pcr_vix_iv/queries_20261123.sql` Q7–Q10

**Both of your objections were correct, and together they overturn my conclusion.**
Test 2b is **NOT blocked on quote quality**. It is runnable, with one poll excluded.

---

## 1. The timing control I should have run first

You said 31.5 s of pairing lag cannot rule out timing. It cannot — and when measured, timing
explains the whole effect.

Within the **trusted tick series alone** (one source, no cross-source join), the same four
contracts move over ~60 s by:

| strike | n | median move | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| NF 25450 PE | 173 | **6.22%** | 14.65% | 26.76% |
| NF 25600 PE | 173 | **4.33%** | 11.66% | 23.57% |
| BNF 62800 PE | 278 | **6.11%** | 16.17% | 42.42% |
| BNF 63200 PE | 278 | **5.00%** | 14.42% | 41.37% |

My chain-vs-tick differences were median **2.3–3.3%**, p90 8.4–11.5%, max 16.6–21.1%, at a lag of
up to 31.5 s — i.e. about **half a minute, and about half the one-minute move**. The differences
are *smaller* than the contracts' own movement over the pairing interval.

**That measurement therefore carries no evidence that the chain is unreliable.** I presented it as
if it did. Withdrawn.

## 2. The arbitrage test I ran on the wrong sides

You said to distinguish indicative bid-price inconsistency from executable arbitrage. My "20–27%
monotonicity violations" compared **bid against bid**, which is an indicative inconsistency and is
fully expected when per-strike rows are not refreshed simultaneously.

The executable test — for calls `K1 < K2`, a locked profit requires `bid(K1) − ask(K2) > K2 − K1`
— over 18–23 November, every poll, both indices:

| poll | adjacent pairs | executable arbitrage |
| --- | ---: | ---: |
| 09:15 | 1,359 | **0** |
| 09:20 | 1,446 | **0** |
| 09:25 | 1,454 | 2 (0.14%) |
| 09:30 | 1,454 | **0** |
| 09:35 | 1,447 | **0** |
| 09:40–10:20 | ~1,443 each | 0–4 (≤0.28%) |

**Essentially zero executable arbitrage, with no time-of-day pattern.** My earlier figure measured
something real but harmless, and I labelled it as if it disqualified the source.

## 3. The level check, done properly — and here my *original* finding survives

The one defect that is real needs a **verified spot** and a **tradeable strike band**. Spot comes
from the export's authority history (independent of any trade's own `exit_snapshot`); I confirmed
BNF 09:15 = 64,934.40 and NF 09:15 = 25,915.40 there.

Restricted to strikes 100–1,200 points ITM (BNF) and 100–450 (NF) — the band that would actually
be traded:

| index | poll | near-ITM calls | below intrinsic | worst shortfall | median shortfall |
| --- | --- | ---: | ---: | ---: | ---: |
| BNF | **09:15** | 11 | **9 (81.8%)** | **394.4** | **+187.1** |
| BNF | 09:35 | 11 | 0 | 0.8 | −103.1 |
| BNF | 09:50 | 11 | 3 | 54.6 | −62.2 |
| BNF | 10:10 | 11 | 2 | 21.5 | −56.0 |
| BNF | 11:00 / 12:00 / 12:40 | 11 | 0–1 | ≤1.2 | −56.7 to −25.3 |
| NF | **09:15** | 7 | **7 (100%)** | **145.4** | **+105.3** |
| NF | 09:35 onward | 7 | 0–2 | ≤10.5 | −20.6 to −2.1 |

A negative median shortfall means bids sit **above** intrinsic, as they should.

**So the 09:15 opening poll is genuinely broken in the tradeable band, and every later poll is
sound.** My first characterisation of E2 was right; my second ("flat across the day") was wrong,
and it was wrong because that run included the illiquid deep wings — where a 4,364-point shortfall
on a strike 18,000 points ITM is illiquidity, not a data defect — and used bid-vs-bid.

## 4. Corrected register entry

> **E2 (corrected).** The **09:15 opening-auction poll** of `ml_option_chain_snapshots` carries
> near-money ITM call bids below intrinsic (BNF 82% of the band, median 187 points below; NF 100%,
> median 105 below) and must be excluded as an executable mark. **Polls from 09:20 onward are
> sound** on the evidence available: no executable arbitrage, and divergence from the trusted tick
> path within the contracts' own 30-second movement. The 09:20 poll is not directly sampled in §3
> and should be treated as unverified rather than assumed either way.

This also re-explains the 318/319 case without needing the chain to be broadly unreliable: those
closes were priced from the **09:15:02** poll specifically, which is the one poll that is bad.

---

## 5. Consequences

| claim I made | status |
| --- | --- |
| "Test 2b blocked on quote quality" | **withdrawn** |
| "chain unusable at all times of day" | **withdrawn** |
| "20–27% violations all day" | **withdrawn as evidence** — indicative, not executable |
| "chain-tick divergence shows chain error" | **withdrawn** — within normal 30 s movement |
| "09:15 chain rows are below intrinsic near the money" | **stands, now properly measured** |
| Tests 3/4/6 premium limbs blocked | **withdrawn** — they are limited by *coverage*, not quality |

**Test 2b is runnable**, excluding the 09:15 poll, with 09:20 flagged unverified. The predeclared
rules from the earlier checkpoint stand and are now usable. Test 2a stays blocked — that block was
about a second expiry never being captured, which no quote-quality finding touches.

I have now been wrong in both directions on this question inside one day. The guard I am adopting:
**no quote-quality claim without a same-source timing control and an executable-side test**, both
of which are now committed as Q7–Q10 so the next such claim can be checked in one command.

---

## 6. Still outstanding from your list

These are acknowledged, not yet done:

- **Matched-row supply with identity/duplicate checks** — Q10 emits the matched rows with
  `instrument_key`, expiry, side, both clocks and the pairing lag; I have not yet run it to a
  committed CSV and audited for duplicate joins.
- **Usable coverage by session, index and strategy** — valid entry-and-exit pairs, exclusions and
  WAIT outcomes. Needed before any limited scorecard.
- **Input qualification** — snapshot IV provenance and freshness; OI timestamps, expiry scope and
  completeness for PCR and max pain. Both must precede Tests 3 and 5 respectively.
- **Tick extrema validation** — recompute peak/trough from eligible timestamped marks and
  reconcile against the saved journey fields before using them for exit-policy conclusions. I
  reported the disagreement but have not yet recomputed from first principles.
- **Capture correction specification for Grok** — candidate contracts, comparison expiries,
  horizon quotes, with request and storage costs.

Test 5 is next, with its OI input verification first.

## Boundaries

Read-only database access. Review branch only. b535 frozen. Original rows preserved. No production
change, merge, release or candidate publication.
