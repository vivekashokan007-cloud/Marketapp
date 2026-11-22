# Claude Code reply — the span extended, and no edge survives it

Market Radar · 22 November 2026 IST · supersedes
`CLAUDE_CODE_REPLY_PREMIUM_SPAN_12_TO_20_NOV_20261121` now that the four missing exports arrived

> **CORRECTED IN PART, 22 November, after the research owner's edge-research handoff.** Two
> things in this memo were wrong and are corrected **in place**, at §3.2 and §4, with the
> withdrawn wording left visible rather than quietly rewritten:
>
> 1. **§3.2's "remove 20 November and every positive arm is negative" is false.** BANKNIFTY
>    `RANGE_EV` stays positive under *every* leave-one-session-out, worst case **+16,433**. It is
>    the one exception, the owner caught it from my own table, and §3.2 now carries the full
>    leave-one-out grid for all eighteen arms instead of a single sentence.
> 2. **§4's spread figures charged fees on the net spread price, not on each executed leg.** Every
>    number in that table is wrong and all four are replaced. The headline claim — NIFTY bull call
>    at "−₹1.2 a trade, an edge of exactly zero" — was a fee artefact; corrected it is
>    **−₹42.8 a trade**. The §4 *conclusion* (all four structures lose) is unchanged and in fact
>    strengthened.
>
> Everything else in this memo stands as written. The corrected replay, the survival stress on the
> one surviving arm, and the research map are returned separately.

**Research branch** `review/premium-model-validation-20261121` — `5125c9e` inventory + defect
fixes → `ce94a04` diagnostics, sides and spreads → `5732d79` first memo → `322663c` the span
extension → `ba595ec` this memo
**Protocol branch** `review/premium-neutral-range-comparison-20261116` — `1c9da2a` null-bbo guard
→ `5bbb748` successor v2
**Frozen R7 unchanged on both** — `git diff 124d62e -- tools/spot_path_baseline/` empty,
`premium_repricing_v2.py` still `95e53e25…`

**My previous memo said the span did not extend and that the NIFTY put result was probably
direction rather than edge. The data has now arrived and settled it: it was direction.** The put
arms invert on the up days — **two of them confirmed against the observed index, the third
inferred from the premium proxy because its export carries no spot trail** — the call arms invert
the other way, and every pooled positive total is **more than entirely attributable to one
session**.

---

## 1 · The ledger, now six usable sessions

All four new exports are genuine captures by provenance — their digests are in the upload channel,
and each carries 414 records, 342 planned opportunities, `VERIFIED`, 09:20–15:00.

> **The "NIFTY direction" column below is a PREMIUM PROXY, and on 12 November it is wrong.** The
> owner asked for direction from time-aligned observations, and the exports carry them:
> `parts.authority_doc.history[index]` stamps an index `spot.value` with its own `spot.asof` on
> every decision poll, independent of any option quote. `index_direction.py` reads it. The
> observed series is in the table after the proxy table, and it changes two labels:
>
> * **12 November NIFTY closed UP, +65.70** (25,209.55 → 25,275.25). The proxy said down. Both
>   are defensible readings of different things — inside the capture window, which ends 13:45
>   that day, NIFTY was **down −48.55** and the median 60-minute move was **−8.25**, so the
>   replay's population really did see a falling index. The *day* did not fall. BANKNIFTY on 12
>   November disagrees on **all three** bases: proxy down, observed up on the day (+644.80), up
>   in the window (+257.75) and flat-to-up on the median horizon (+0.10).
> * **16 and 17 November carry `authority_doc: null`, so no direction is observable for them at
>   all.** The "17 Nov was UP" label rests on the premium proxy alone. **Only two of the three
>   up days are confirmed** — the 19th (+337.05) and the 20th (+430.15).
>
> 18, 19 and 20 November agree with the proxy on every basis. §2's conclusion survives — it rests
> on the 19th and 20th, both confirmed, with the 17th consistent but unobserved — but "three up
> days" should read "two confirmed and one inferred".

| date | NIFTY direction (proxy) | NF records / usable | BNF records / usable | planned | state |
| --- | --- | ---: | ---: | ---: | --- |
| 12 Nov | ~~**down**~~ **UP on the day, down in the capture window** | 318 / 318 | 323 / 323 | 342 | VERIFIED (capture ends 13:40/13:45) |
| 13 Nov | — | **0** | **0** | — | capture present, **zero premium records** |
| 14–15 Nov | — | — | — | — | no trading session (weekend) |
| 16 Nov | **down** — *proxy only, unobservable* | 414 / 414 | 414 / 414 | 342 | VERIFIED, 100% coverage |
| 17 Nov | **UP** — *proxy only, unobservable* | 414 / 414 | 414 / 414 | 342 | VERIFIED, 100% |
| 18 Nov | down | 414 / 414 | 414 / 414 | 342 | VERIFIED, 100% |
| 19 Nov | **UP** | 414 / 414 | 414 / 414 | 342 | VERIFIED, 100% |
| 20 Nov | **UP** | 414 / **408** | 414 / **408** | 342 | VERIFIED; one failed poll, §6 |

Digests: `e97198f2…` (17th), `31b58b58…` (18th), `0b50c96e…` (19th), `67e14b90…` (20th).

**The sample now contains both directions**, which is exactly the test my caveat asked for:

| session | NF calls up/down | median call move | NF puts up/down | median put move |
| --- | --- | ---: | --- | ---: |
| 12 Nov | 22 / 101 | **−27.65** | 87 / 36 | +18.77 |
| 16 Nov | 60 / 111 | −9.05 | 90 / 80 | +0.92 |
| 17 Nov | **125 / 46** | **+25.65** | 32 / 139 | −10.10 |
| 18 Nov | 68 / 103 | −7.53 | 99 / 72 | +3.47 |
| 19 Nov | **158 / 0** | **+32.95** | **0 / 159** | −18.58 |
| 20 Nov | **152 / 9** | +23.17 | 1 / 161 | −18.36 |

19 November had **every** NIFTY put fall and 158 of 159 calls rise. On 20 November BANKNIFTY
calls rose a median **+148.64** points with a best of **+842.87**.

**And now the observed index itself**, from the decision-poll spot trail rather than the premium.
`spot.asof` is the poll's own receipt stamp, not a verified exchange event time, so this is used
to diagnose the replay and never to decide inside it:

| session | idx | obs | open → close | dir | inside capture window | dir | median 60-min | dir | proxy said |
| --- | --- | ---: | ---: | --- | ---: | --- | ---: | --- | --- |
| 12 Nov | NF | 73 | 25,209.55 → 25,275.25, **+65.70** | **UP** | −48.55 | DOWN | −8.25 | DOWN | DOWN |
| 12 Nov | BNF | 73 | 60,823.15 → 61,467.95, **+644.80** | **UP** | +257.75 | UP | +0.10 | UP | **DOWN ✗** |
| 13 Nov | NF | 41 | −37.10 (series ends 12:35) | DOWN | — | — | −21.05 | DOWN | *no premium* |
| 13 Nov | BNF | 41 | −139.10 | DOWN | — | — | −32.05 | DOWN | *no premium* |
| 16 Nov | both | — | **`authority_doc: null`** | — | — | — | — | — | down |
| 17 Nov | both | — | **`authority_doc: null`** | — | — | — | — | — | UP |
| 18 Nov | NF | 76 | 25,308.30 → 25,227.95, −80.35 | DOWN | −141.70 | DOWN | −20.75 | DOWN | DOWN ✓ |
| 18 Nov | BNF | 76 | 62,197.75 → 62,038.65, −159.10 | DOWN | −341.00 | DOWN | −22.20 | DOWN | DOWN ✓ |
| 19 Nov | NF | 76 | 25,177.25 → 25,514.30, **+337.05** | UP | +259.10 | UP | +46.65 | UP | UP ✓ |
| 19 Nov | BNF | 76 | 61,984.70 → 63,063.00, **+1,078.30** | UP | +737.80 | UP | +123.38 | UP | UP ✓ |
| 20 Nov | NF | 75 | 25,370.45 → 25,800.60, **+430.15** | UP | +315.00 | UP | +36.32 | UP | UP ✓ |
| 20 Nov | BNF | 75 | 62,627.90 → 64,635.20, **+2,007.30** | UP | +1,771.15 | UP | +183.48 | UP | UP ✓ |

Three incidental findings worth keeping:

* **BANKNIFTY's 20 November was a +2,007-point day, 3.2%.** That is the session that carries every
  positive total in §3.2, and it is not a subtle regime — it is a very large one-directional move
  in a six-session sample.
* **12 November BNF's median 60-minute move is +0.10 points.** Calling that "up" is within noise;
  what is not within noise is that the premium proxy called it *down* while the index rose 644
  points on the day.
* **13 November has a complete 41-poll spot trail and zero premium records.** The brain's own
  decision trail persisted that day; the premium capture did not. That is independent evidence
  for the persistence defect in §12.1, from the same file that lacks the data.

## 2 · The put result was direction. Confirmed on two observed up days, inferred on the third.

Per-session net rupees, 60 minutes, decision-anchored, one-sided populations:

| idx | side | arm | 12th | 16th | 17th | 18th | 19th | 20th | **total** |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NF | **PE** | BUY_ALL | +81,322 | +31,421 | −122,596 | −20,738 | −223,335 | −226,493 | **−480,418** |
| NF | **PE** | RANGE_EV | +57,077 | +33,342 | −112,108 | +35,426 | −165,891 | −177,567 | **−329,722** |
| NF | **PE** | FORECAST λ=1 | +40,289 | −2,197 | −25,441 | −18,860 | −80,265 | −54,631 | **−141,105** |
| NF | CE | BUY_ALL | −171,655 | −133,217 | +88,636 | −61,778 | +339,935 | +392,098 | **+454,018** |
| BNF | CE | BUY_ALL | −15,371 | −59,005 | +197,708 | −50,820 | +413,343 | +1,078,305 | **+1,564,160** |
| BNF | PE | BUY_ALL | −87,610 | −8,287 | −200,826 | −59,888 | −251,777 | −270,332 | **−878,720** |

**The three arms that met the downside bar on 12 November NIFTY puts lose heavily over the six
sessions.** Each side is a leveraged bet on index direction; the sign of any arm's total is the
sign of the sample.

## 3 · Two findings that settle it

### 3.1 · The forecast trades *less*, not better

Mean net per scored trade, pooled over all six sessions:

| idx | side | BUY_ALL | FORECAST λ=1 | RANGE_EV | does the filter improve per-trade? |
| --- | --- | ---: | ---: | ---: | --- |
| BNF | CE | +1,639.6 | **+1,794.7** | +1,500.9 | yes, +9% |
| NF | CE | +475.9 | **+636.8** | +525.4 | yes, +34% |
| BNF | PE | −922.1 | −987.7 | −986.5 | **no, worse** |
| NF | PE | −503.6 | −542.7 | −490.7 | **no, worse** |

A filter that improves the call side and degrades the put side is **cutting size, not selecting**.
Its apparent two-sided win on NIFTY (+137,157 for λ=1 against −26,400 for BUY_ALL) comes from
taking 437 calls and only 260 puts — an accidental long-call tilt in a sample with three up days.

### 3.2 · Every pooled positive is more than entirely one session

| idx | arm | six-session total | biggest single session | its share | sessions won / lost |
| --- | --- | ---: | ---: | ---: | --- |
| BNF | BUY_ALL | +685,439 | +807,973 (**20 Nov**) | **118%** | 2 / 4 |
| BNF | FORECAST λ=1 | +200,229 | +286,015 (20 Nov) | **143%** | 2 / 4 |
| NF | FORECAST λ=1 | +137,157 | +150,471 (20 Nov) | **110%** | 2 / 4 |
| NF | BUY_ALL | −26,400 | +165,605 (20 Nov) | 627% | 2 / 4 |
| BNF | RANGE_EV | +131,251 | +114,818 (20 Nov) | 87% | 3 / 3 |

~~**Remove 20 November and every positive arm is negative.**~~ **WITHDRAWN — this was wrong, and
the owner caught it from my own table above: the BNF RANGE_EV row shows +131,251 total with only
+114,818 from 20 November, so the remainder is positive and the sentence contradicts its own
table.** The full leave-one-session-out grid, every arm and both indices, six sessions:

| idx | arm | all six | worst LOO | drop | 12th-out | 16th-out | 17th-out | 18th-out | 19th-out | 20th-out |
| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BNF | BUY_ALL | +685,439 | **−122,534** | 20th | +788,421 | +752,731 | +688,557 | +796,148 | +523,873 | −122,534 |
| BNF | FORECAST λ=1 | +200,229 | **−85,786** | 20th | +241,720 | +226,997 | +226,606 | +287,000 | +104,608 | −85,786 |
| BNF | FORECAST λ=0.5 | +53,066 | **−99,415** | 20th | +84,062 | +68,670 | +121,266 | +121,868 | −31,123 | −99,415 |
| BNF | **RANGE_EV λ=0** | **+131,251** | **+16,433** | 20th | +170,362 | +224,536 | +47,183 | +140,099 | +57,642 | **+16,433** |
| BNF | **RANGE_EV HIST** | **+129,023** | **+14,205** | 20th | +168,134 | +221,132 | +46,421 | +140,575 | +54,646 | **+14,205** |
| NF | BUY_ALL | −26,400 | −192,005 | 20th | +63,933 | +75,396 | +7,560 | +56,116 | −142,999 | −192,005 |
| NF | FORECAST λ=1 | +137,157 | **−13,314** | 20th | +137,441 | +233,306 | +154,874 | +162,098 | +11,381 | −13,314 |
| NF | FORECAST λ=0.5 | +98,573 | **−58,028** | 20th | +109,048 | +207,939 | +117,073 | +141,388 | −24,554 | −58,028 |
| NF | RANGE_EV λ=0 | −64,394 | −97,687 | 18th | −63,937 | −27,755 | +517 | −97,687 | −80,360 | −52,750 |
| NF | RANGE_EV HIST | −52,891 | −87,813 | 18th | −52,434 | −15,475 | +4,971 | −87,813 | −72,458 | −41,247 |
| NF | RANGE_EV IV_ONLY | −135,755 | −135,755 | — | — | — | — | — | — | — |

`FORECAST_RULE_HIST`, `FORECAST_RULE_IV_ONLY`, `RANGE_EV_IV_ONLY` on BNF and both
`FORECAST_RULE_E0_TILT_0` arms take **zero** trades on every session, so they are exactly ₹0 under
every leave-one-out and are not evidence of anything.

**The corrected statement: two of eighteen arms stay positive under every leave-one-session-out,
and they are the same arm on the same index — BANKNIFTY `RANGE_EV`, at λ=0 (+16,433) and on the
historical reference (+14,205).** The λ=0.5 and λ=1 forecast arms and both buy-all diagnostics do
flip, as I said; the RANGE_EV exception is real and I had asserted the opposite.

That exception is the one thing in the whole span that looked like an edge, so I stressed it
rather than reporting it. `stress_range_ev.py`, same population, same governed dated costs:

| unresolved exits priced as | n | total | win% | worst LOO | without the best 5 | return on risk |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **EXCLUDE** (the replay's basis) | 1,047 | **+131,251** | 43.6 | **+16,433** | +64,714 | 0.66% |
| AT_ARM_MEAN | 1,223 | +153,314 | 51.8 | +19,272 | +86,777 | 0.77% |
| AT_TOTAL_LOSS | 1,223 | **−2,905,099** | 37.4 | −2,639,909 | −2,971,636 | −14.55% |

It **survives** the two stresses that usually kill a result of this kind:

* **Concentration.** Remove the five best trades of 1,047 and it is still +64,714. Those five do
  carry 51% of the gain, which is heavy, but the remainder does not change sign.
* **Worse fills.** Realisation already crosses the spread and pays one declared tick each way.
  Pay a **second** adverse tick each way, with the decision left on the unstressed estimate so
  the rule cannot decline the trades its own bad fills would kill: +128,112, worst LOO +13,813.

It does **not** survive the hole in the book. **176 of 1,223 filled positions (14.4%) never got an
exit quote and are scored by nobody.** That splits in two, and only one half is a data problem:

* **115 of them sit at decision slots 14:05–14:40, where the scored count is exactly zero.** A
  60-minute hold from 14:05 targets 15:05, past the last exit the capture holds — the same
  15:00 window boundary as the overnight blocker in §7. That is not missing data, it is known at
  decision time, and it is identical for every arm, so the matched treatment is to drop those
  decisions, not to price them.
* **61 are genuine holes at slots that did score** — 5.0% of the book. **The entire +131,251 is
  erased if those 61 averaged −₹2,152.** The scored distribution's own 10th percentile is
  **−₹3,009** and its worst is −₹9,019.

**So the honest verdict on the one exception is "not established", not "an edge".** A 0.66% return
on risk measured across 86% of the book cannot be separated from a 5% hole whose plausible adverse
value is larger than the whole gain. It is a reason to fix the capture window and re-measure, not
a reason to trade. Every arm, including this one, still loses the majority of sessions, and the
declared downside bar is met in **1 of 12** session/index cells for BUY_ALL, **3 of 12** for the
median forecast, and **0 of 12** for RANGE_EV — that part of the original sentence stands.

## 4 · The spreads are the cleanest result, because they cap the windfall

> **These figures are CORRECTED. The table first published here charged the dated statutory and
> exchange levies on the *net* spread price instead of on each executed leg.** A vertical is four
> orders across two contracts, and the ledger was being handed one synthetic instrument: STT
> (0.15% of the sale) was never charged on the short leg's entry sale at all, and the exchange,
> IPFT and SEBI components saw the net rather than both legs — roughly a halving on a typical
> vertical. On one worked NIFTY vertical the turnover was understated **2.26×**, total fees were
> **₹11.71** low (10% of the correct figure), and STT came to ₹5.84 against the correct ₹13.25.
> `spread_replay.fees_for` now prices `sell_px`/`buy_px` on the entry legs and
> `exit_sell_px`/`exit_buy_px` on the exit legs, with `legs=2`.

Two-leg debit verticals, same six sessions, 323–324 scored rows each, **per-leg dated costs**:

| idx | structure | 12th | 16th | 17th | 18th | 19th | 20th | **total** | per trade |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NF | BULL_CALL | −17,536 | −20,614 | +16,956 | −12,279 | +7,619 | +12,020 | **−13,834** | **−42.8** |
| NF | BEAR_PUT | −937 | +1,098 | −34,099 | −11,956 | −31,046 | −33,558 | −110,499 | −342.1 |
| BNF | BULL_CALL | −12,293 | −16,555 | −13,532 | −13,662 | −755 | −13,380 | −70,177 | −216.6 |
| BNF | BEAR_PUT | −16,310 | −15,608 | −20,774 | −17,517 | −28,434 | −32,015 | −130,658 | −404.5 |

Superseded net-basis totals, for the reconciliation: −399 / −101,123 / −40,475 / −112,427.

**All four structures lose, and this is the more robust statement of the whole exercise.** The
short leg caps the directional windfall that made the outright call arms look positive, so the
one-day dominance of §3.2 disappears — and what is left is costs. ~~NIFTY bull call is −₹399
across 323 round trips, −₹1.2 a trade: an edge of exactly zero~~ — **withdrawn: that "exactly
zero" was my own fee defect.** Corrected, NIFTY bull call is **−₹13,834 across 323 round trips,
−₹42.8 a trade**, and BANKNIFTY bull call goes from −₹125 to −₹216.6 a trade. The conclusion is
unchanged and stronger: the one structure that looked like a free option was paying half its
fees.

Credit verticals remain **refused**, not replayed: their legs are present but no export carries
hedged-margin evidence, and that is the difference between legs existing and a position being
executable.

## 5 · The tail check across six sessions

The recentred forecast's exceedance rates stay close to nominal on the new sessions too, so the
previous round's conclusion holds: **the tail is not too fat**. What explains the losses is still
the near-cancellation measured last round — the expected-net signal is a thin residual between a
large positive upper half and a large negative lower half, 75–90% of the gross magnitude
cancelling, leaving a few rupees per unit against a round-trip cost near ₹1.2.

§4's spread result is the same statement arriving from a different direction: once the
distributional windfall is capped structurally, the residual is costs.

## 6 · A third unguarded dereference, found by the new data

One poll at **13:25 on 20 November** returned `MISSING_REQUEST_FAILED` for **all six pinned
contracts in both indices**, and those records carry **`bbo: null`** rather than an empty book.

* `range_compare.admit` dereferenced it unguarded and **crashed admission for the whole
  session** — one failed poll cost the entire day. It now rejects them by name as
  `rej_bbo_absent`, and the session scores 414 records, 408 usable.
* **The frozen module has the same defect**, at `premium_repricing_v2.py:514` in `score_index`.
  It stays byte-identical, so the guard went into the versioned successor — now
  `premium_repricing_v2_successor_v2_null_authority_doc_and_bbo` — and the two parity tests skip
  naming the condition and the record count.

That is the third such dereference in this module, and all three were found by **real data
arriving**, not by reading the code. `test_protocol_hashes` caught my stale hashes twice more in
this change.

I also replaced the research branch's **stale fork** of `premium_range_compare` with the protocol
branch's canonical tree, so the guard, the successor and the chain reporting exist once rather
than diverging.

## 7 · Overnight — the blocker is now proven on real consecutive sessions

Four genuine adjacencies (16→17, 17→18, 18→19, 19→20), identical pinned contracts, matcher
unchanged:

```
  attempted pairs 24 per index;  VALID PAIRS: 0
  refusals by reason:  no_close_mark_in_window  24   (always at 15:00)
```

**48 pairs across both indices, every one refused for the same single reason.** Yesterday I could
only show this with a synthetic relabel; it is now demonstrated on real adjacent captures.

And the near-close source still exists: the 13 November logs show `PREMIUM_CAPTURE: SUBMITTED` at
**15:00, 15:05, 15:10, 15:15, 15:20, 15:25, 15:30, 15:35 and 15:40**, covering the matcher's whole
15:10–15:30 window, while every export's manifest declares `expected_slots: 69` ending at 15:00.
**The polls exist; the cohort window discards them.** It is a constant, not collection.

## 8 · The fee position, and what is provisional

**The source-level gap I reported as open is now closed for this window.** NSE's own circulars
listing for **12–19 November 2026** contains exactly **one** Finance & Accounts circular:
**NSE/FA/77421, 13 November, "Transaction Charges for Electronic Gold Receipts (EGRs)"** — not
options. So no circular revised equity-option transaction charges or IPFT in that window, read
from the issuer's listing rather than inferred from broker pages. The 9 October – 11 November
window remains unchecked at source.

**Everything in §2–§4 for 17–20 November is provisional on an unreviewed fee revision.** I
extended the dated table to 20 November **in a worktree only**, and marked the version
`nse_equity_index_options_rates_20261120_v3_UNREVIEWED` so it cannot be mistaken for a reviewed
one. Every run stamps `provisional_cost_basis` with the dirty file named. Nothing was committed to
a fee table.

So there are now **two** fee extensions awaiting your review: 9 Oct → 16 Nov (returned yesterday
with worked cases) and 16 Nov → 20 Nov (this one, with the §8 listing evidence).

## 9 · What changes, and what does not

| claim | status |
| --- | --- |
| "the span does not extend" | **withdrawn** — it extended, to six sessions |
| "the 12/16 Nov put result is direction, not edge" | **confirmed** by the three up days (§2) |
| "WAIT is unbeaten two-sided" | **changed**: over six sessions BNF buy-all is +685,439 and NF forecast λ=1 is +137,157. But §3.2 shows both are one session, and both lose 4 of 6 |
| "the forecast adds nothing" | **refined**: it improves per-trade on calls by 9–34% and degrades it on puts. It cuts size |
| "the tail is calibrated; the loss is a near-cancellation" | **holds** on the new sessions (§5) |
| "debit spreads are defined-risk and marginally positive on NIFTY puts" | **withdrawn** — over six sessions all four structures lose (§4), and that is the cleaner result |
| "remove 20 November and every positive arm is negative" | **withdrawn, my error** — BNF RANGE_EV survives every leave-one-out at +16,433 (λ=0) and +14,205 (HIST); §3.2 now carries the full grid |
| "NIFTY bull call is an edge of exactly zero, −₹1.2 a trade" | **withdrawn, my error** — the fees were charged on the net spread price; per leg it is **−₹42.8 a trade** (§4) |
| "12 November was a down day" | **corrected** — NIFTY closed **UP +65.70**; it fell only inside the capture window (−48.55). BANKNIFTY rose on all three bases while the proxy said down (§1) |
| "the sample contains three up days" | **corrected to two confirmed and one inferred** — 16 and 17 November carry `authority_doc: null`, so no index direction is observable for either (§1) |
| the BNF RANGE_EV exception is an edge | **not established** — it survives concentration and a second adverse tick each way, but 5% of the book has no exit quote and −₹2,152 average on those 61 positions erases the whole gain, against a scored p10 of −₹3,009 (§3.2) |
| "overnight is blocked by the window constant" | **proven** on real adjacent sessions (§7) |

## 10 · Rerun commands

```bash
# the inventory, read-only, provenance-classified
python3 -I tools/premium_model_validation/inventory_span.py <dir-of-exports> \
    --from-date 2026-11-12 --to-date 2026-11-20 --out <out>
# the replay, two-sided then per side, for each of the six sessions
python3 -I tools/premium_model_validation/economic_check.py <export> <spots> \
    --ledger-root <tree> [--side CE|PE] [--risk-budget-inr 250000] --out <out>
# direction and tail diagnostics
python3 -I tools/premium_model_validation/two_sided_diagnostics.py <export> <spots> --out <out>
# two-leg debit spreads
python3 -I tools/premium_model_validation/spread_replay.py <export> <spots> \
    --ledger-root <tree> --out <out>
# overnight, four real adjacencies
python3 -I tools/premium_followon/next_session_match.py \
    <exp_16> <exp_17> <exp_18> <exp_19> <exp_20> --index NF
# which frozen chain an export can take
python3 -I tools/premium_range_compare/frozen_successor.py <export.json>
# suites
# realised index direction from the decision-poll spot trail, not the premium proxy
python3 -I tools/premium_model_validation/index_direction.py <export...> --out <out>
# the survival stress on the one arm that passes every leave-one-out
python3 -I tools/premium_model_validation/stress_range_ev.py \
    --span <dir-of-exports> --spots <spots> --ledger-root <tree> \
    --index BNF --arm RANGE_EV_E0_TILT_0 --out <out>
# suites. SR_SPAN / SR_SPOTS / SR_LEDGER_ROOT unskip the stress integration guard
cd tools/premium_model_validation && python3 -m unittest discover -s tests     # 170
cd tools/premium_range_compare   && python3 -m unittest discover -s tests     # 158
```

## 11 · Hashes

| file | sha256 |
| --- | --- |
| `economic_check.py` | `3ef634d7eb802da70c7b473f7013337dc00ce5ca6df121b416b98db0ac62d807` |
| `two_sided_diagnostics.py` | `8bf242c905b34c8fbafe2ebb7bb1baf41634b7a644b8278e7039347e8d645cc8` |
| `spread_replay.py` (per-leg fee basis; was `d376c7df…` on the net basis) | `b13d9f5c1f56368e6f9f27b20dbd447cc0bae5eae786f2410f31ee9521ffb7aa` |
| `stress_range_ev.py` | `5b4a977d07f38c0042f1025b07d704b7ab3b8d425cd497df29129227b286bbee` |
| `index_direction.py` | `64e6ed9491b87ae9d7133fed8c1067bf2eea1737789af9054938bb6393abd099` |
| `inventory_span.py` | `14edbb1a3fb60a17d2e3aa2a0544fff0fd0454639d345b3d100013ad3940f0d5` |
| `tilt_compare.py` | `3054291875958076ea7673d9617d2b02948226f8c0d4b7f4d295d18d24950c78` |
| `fee_worked_cases.py` | `c876b3b840a684f0e3a1322c7bdb724f65218c8fa904e300dcd191001519f91f` |
| `range_compare.py` (both branches) | `a691173311413b24a71e4c31d88611ddde714b674196fd86b0e5495f0314ed87` |
| `frozen_successor.py` | `aa6b2f8e27f6b4d4af58abec322fff12f5113bc9f5e156ee720335b6f88f2327` |
| `premium_repricing_v2.py` (frozen, unmodified) | `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a` |

| input | sha256 |
| --- | --- |
| `export_2026-11-12.json` | `5f399accefabd0dd3f1f1f3bb829bb71fd6ae56a309dfc92db5ac18e6d3444cb` |
| `export_2026-11-13.json` (zero premium records) | `4f544bee59af9020d5eb2846161d8ebffa7010a0d0e1492ef41a92ac41f51301` |
| `export_2026-11-16.json` | `2cd2006626d3a72713b017c9352232538abfadab07258938ed5395058b380f17` |
| `export_2026-11-17.json` | `e97198f28ea1e487183f10871a24b8f4b0deee8c8c7af0fd22bfe5f53f65756c` |
| `export_2026-11-18.json` | `31b58b582751fd8390d25754d84d97d10c7bd597dc180e7371e42f6b7f560cf0` |
| `export_2026-11-19.json` | `0b50c96e47fddf0d7212ceb29b1200bb3b9fe7653e84ecc931699e99e70afbc2` |
| `export_2026-11-20.json` | `67e14b90b0fb08a2b0dfc8b5928bad49fbda34fc4f758722ec0751eaf6e6098a` |
| `spots_20261021_20261104.jsonl` | `f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186` |

**Inputs are not committed.**

## 12 · Still open

1. **13 November persistence defect** — captures submitted on 138 polls, 09:15 to 15:40, BNF
   `ACCEPTED_STARTED` and NF `ACCEPTED_QUEUED_BEHIND_ONE` on every one, and nothing persisted.
   Unchanged by the new data and worth Vivek's attention independently.
2. **Two fee extensions awaiting review** (§8), and the 9 Oct – 11 Nov source window still
   unchecked at the issuer.
3. **No margin evidence** anywhere, so credit spreads stay unclaimable.
4. **The exchange quote clock is unverified**, so quote staleness still cannot be established.
5. **The overnight window constant** (§7), now proven rather than argued.
6. **Six sessions is still a small sample dominated by one day.** I would not build a rule on it,
   and §3.2 is why.

## 13 · Scope

Offline research and replay, source data read read-only. This span is **retrospective research
and stays outside the unsigned prospective v4 count**. No active Paper rule, app build, release,
production write, `main` merge or pull request. No database write. No raw export, Paper position
or credential in either tree. Every rupee is `PROXY_SIMULATED_FILL`; 17–20 November additionally
rests on an **unreviewed** fee revision.

---

### What I would ask you to decide

1. **Accept that no variant earns prospective Paper research on this evidence.** §3.2 and §4 are
   the reasons: one session carries every positive total, and the capped structures — where that
   session cannot dominate — lose on all four.
2. **Review the two fee extensions** (§8), or tell me after-cost results for 17–20 November stay
   provisional.
3. **Approve the overnight window constant** (§7).
4. **Decide where the `bbo` guard belongs long-term** (§6): the successor now carries it, but this
   is the third unguarded dereference in the frozen module, and at some point a successor revision
   is cheaper to maintain than a growing list of skips.
5. **The 13 November persistence defect** (§12.1) is the one item here that is about the app
   rather than the research, and it is the one I would escalate first.
