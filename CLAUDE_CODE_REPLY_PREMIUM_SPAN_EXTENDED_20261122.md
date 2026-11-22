# Claude Code reply — the span extended, and no edge survives it

Market Radar · 22 November 2026 IST · supersedes
`CLAUDE_CODE_REPLY_PREMIUM_SPAN_12_TO_20_NOV_20261121` now that the four missing exports arrived

**Research branch** `review/premium-model-validation-20261121` — `5125c9e` inventory + defect
fixes → `ce94a04` diagnostics, sides and spreads → `5732d79` first memo → `322663c` the span
extension → `ba595ec` this memo
**Protocol branch** `review/premium-neutral-range-comparison-20261116` — `1c9da2a` null-bbo guard
→ `5bbb748` successor v2
**Frozen R7 unchanged on both** — `git diff 124d62e -- tools/spot_path_baseline/` empty,
`premium_repricing_v2.py` still `95e53e25…`

**My previous memo said the span did not extend and that the NIFTY put result was probably
direction rather than edge. The data has now arrived and settled it: it was direction.** The put
arms invert on the three up days, the call arms invert the other way, and every pooled positive
total is **more than entirely attributable to one session**.

---

## 1 · The ledger, now six usable sessions

All four new exports are genuine captures by provenance — their digests are in the upload channel,
and each carries 414 records, 342 planned opportunities, `VERIFIED`, 09:20–15:00.

| date | NIFTY direction | NF records / usable | BNF records / usable | planned | state |
| --- | --- | ---: | ---: | ---: | --- |
| 12 Nov | **down** | 318 / 318 | 323 / 323 | 342 | VERIFIED (capture ends 13:40/13:45) |
| 13 Nov | — | **0** | **0** | — | capture present, **zero premium records** |
| 14–15 Nov | — | — | — | — | no trading session (weekend) |
| 16 Nov | **down** | 414 / 414 | 414 / 414 | 342 | VERIFIED, 100% coverage |
| 17 Nov | **UP** | 414 / 414 | 414 / 414 | 342 | VERIFIED, 100% |
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

## 2 · The put result was direction. Confirmed.

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

**Remove 20 November and every positive arm is negative.** Every arm loses the majority of
sessions. The declared downside bar is met in **1 of 12** session/index cells for BUY_ALL, **3 of
12** for the median forecast, and **0 of 12** for RANGE_EV.

## 4 · The spreads are the cleanest result, because they cap the windfall

Two-leg debit verticals, same six sessions, 323–324 scored rows each:

| idx | structure | 12th | 16th | 17th | 18th | 19th | 20th | **total** | per trade |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NF | BULL_CALL | −15,316 | −19,318 | +18,135 | −10,449 | +11,212 | +15,336 | **−399** | **−1.2** |
| NF | BEAR_PUT | +1,249 | +2,338 | −33,894 | −8,315 | −29,873 | −32,629 | −101,123 | −313 |
| BNF | BULL_CALL | −8,137 | −12,290 | −7,737 | −11,067 | +4,286 | −5,530 | −40,475 | −125 |
| BNF | BEAR_PUT | −11,848 | −11,516 | −18,284 | −12,840 | −26,819 | −31,120 | −112,427 | −348 |

**All four structures lose, and this is the more robust statement of the whole exercise.** The
short leg caps the directional windfall that made the outright call arms look positive, so the
one-day dominance of §3.2 disappears — and what is left is costs. **NIFTY bull call is −₹399
across 323 round trips, −₹1.2 a trade: an edge of exactly zero, with the costs eaten.**

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
cd tools/premium_model_validation && python3 -m unittest discover -s tests     # 132
cd tools/premium_range_compare   && python3 -m unittest discover -s tests     # 158
```

## 11 · Hashes

| file | sha256 |
| --- | --- |
| `economic_check.py` | `3ef634d7eb802da70c7b473f7013337dc00ce5ca6df121b416b98db0ac62d807` |
| `two_sided_diagnostics.py` | `8bf242c905b34c8fbafe2ebb7bb1baf41634b7a644b8278e7039347e8d645cc8` |
| `spread_replay.py` | `d376c7df9d40caad0140d1d2de445b12b7d9adfc01ef3f4b8865668d90caec83` |
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
