# Claude Code second delivery — the corrections, and the portfolio we could actually have run

Market Radar · 22 November 2026 IST · reply to
`CODEX_REPLY_CLAUDE_CODE_TRADE_HOLD_EXIT_TEST_FINDINGS_20261122`
**Branch** `review/premium-model-validation-20261121` · pinned revision below · Supabase **read-only**

---

## 0 · Codex found three real bugs and two overstatements. All five are fixed or withdrawn.

| # | finding | status |
| --- | --- | --- |
| 1 | duration measured from publication, not the simulated fill; under `--delay-polls` the target stayed put so the hold **shrank** | **fixed** — target is now `entry + horizon` in every arm |
| 2 | `price()` valued the 93 **SELL** rows as long options inside `all_intents` | **fixed** — refused by name, coverage-only |
| 3 | the +₹282,385 is an **opportunity sum** with up to 12 simultaneous positions per index | **fixed** — achievable portfolio built, and it is **+₹25,014** |
| 4 | "where it falls apart" — the residual without 20 Nov is **positive**, not negative | **withdrawn**, with a nuance that matters (§3) |
| 5 | `BROKER_SNAPSHOT_UNAVAILABLE` read as a failure, when `ELIGIBLE_PENDING_SNAPSHOT` is a valid pending proposal and the broker quote is requested at a Paper tap | **withdrawn** (§6) |

**The most consequential correction is #3.** The achievable policy earns **9% of what the
opportunity diagnostic reports**, and unlike the diagnostic it **does not survive removing the
best session**.

---

## 1 · The two bases, side by side

| basis | what it is | total ₹ | without 20 Nov |
| --- | --- | ---: | ---: |
| opportunity diagnostic | every admissible buy taken independently, overlaps and all | **+277,082** | **+4,743** |
| **achievable policy** | one position per index, shared capital, reserved until close | **+25,014** | **−1,722** |

Both are now computed with the fill-anchored hold, so they are comparable. The diagnostic figure
moved from +282,385 to **+277,082** purely because of bug #1: exits are now 60 minutes from the
fill rather than from publication.

**So Codex's point 4 is right and the nuance is this:** the *diagnostic* residual is +4,743 and
stays positive; the *achievable* residual is **−1,722** and does not. My "falls apart" wording was
wrong about the diagnostic. The policy that could actually be run does go negative without 20
November, and that is the number a decision should rest on.

## 2 · The achievable portfolio

One managed position per index, ₹200,000 shared capital, reserved at entry and released **only**
on an actual close, 60-minute hold from the fill, 10-minute mark tolerance, deterministic
tie-break (lowest `decision_id` at the same instant for the same index).

| session | entries | closed | unresolved | net ₹ | net points | win% | peak reserved | peak positions | worst drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 18 Nov | 3 | 3 | 0 | **−3,013** | −84.53 | 0% | 26,164 | 2 | −3,013 |
| 19 Nov | 5 | 5 | 0 | **+1,292** | +15.58 | 60% | 21,601 | 2 | −3,097 |
| 20 Nov | 8 | 8 | 0 | **+26,736** | +786.90 | 88% | 22,971 | 2 | −2,278 |
| **pooled** | **16** | **16** | **0** | **+25,014** | **+717.95** | **62%** | | **2** | |

Return on reserved capital **13.56%**; worst session **−3,013**; best trade +11,282; worst
−3,097. Concentration: without the best 1 **+13,732**, best 3 **+700**, best 5 **−5,233**.
Leave-one-session-out: drop 18 Nov **+28,027**, 19 Nov **+23,722**, **20 Nov −1,722**.

| index / side | n | net ₹ | net points | win% |
| --- | ---: | ---: | ---: | ---: |
| BNF CE | 8 | **+20,686** | +689.54 | 62% |
| NF CE | 5 | **+7,341** | +112.94 | 100% |
| BNF PE | 1 | −2,127 | −70.90 | 0% |
| NF PE | 2 | −886 | −13.63 | 0% |

**The cost of the policy, counted:** 93 SELL rows excluded, **81 opportunities skipped because a
position was already open**, 42 skipped for `ENTRY_WINDOW_CLOSED`. At ₹15,000 capital the
affordability path also fires — 9 skips, net +24,204 — so the binding constraint is the
one-per-index cap, not capital.

### Ledger extract, 20 November (the shape Codex asked for)

```
09:50:24 BNF 63100CE TAKEN   entry 09:55:08 @445.25 cost 13,359 exit 11:00:06 net  +1,291 cash 186,641
09:55:27 BNF 63200CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
10:00:25 BNF 63100CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
10:15:28 BNF 63100CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
10:40:27 BNF 63200CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
10:45:23 BNF 63200CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
10:50:23 BNF 63300CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
10:55:24 BNF 63300CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
11:00:24 BNF 63300CE TAKEN   entry 11:05:06 @389.15 cost 11,676 exit 12:05:07 net +11,282 cash 189,615
11:05:27 BNF 63300CE SKIP_POSITION_ALREADY_OPEN_FOR_INDEX
```

Eight entries, and seven consecutive skips between the first two — that is the policy, visible.

## 3 · Hold horizons, measured from the fill

Same portfolio constraints, 30 / 60 / 120 tradable minutes, 60 primary:

| hold | closed | unresolved | net ₹ | net points | win% | return on reserved |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 min | 22 | 0 | +17,154 | +495.51 | 50% | 6.85% |
| **60 min** | **16** | **0** | **+25,014** | **+717.95** | **62%** | **13.56%** |
| 120 min | 8 | 3 | +23,291 | +675.46 | 62% | **23.60%** |

**60 minutes beats 30 on every measure.** 120 minutes earns the best return on reserved capital
but closes only 8 positions and leaves **3 unresolved** — exposure that could not be closed inside
the session. On 16/22/8 positions this is weak evidence and I would not pick a horizon from it;
what it does support is *not* shortening to 30.

### Delayed entry, now measured correctly

| entry delay | net ₹ | realised hold |
| --- | ---: | --- |
| none | +277,082 | min 60, median 60, max 65 |
| +1 poll | +278,020 | min 60, median 60, max 65 |
| +2 polls | +283,674 | min 60, median 60, max 65 |

**My earlier "survives adverse fills, +282k → +262k → +240k" was measuring bug #1.** That decline
was the hold shrinking by 5 and 10 minutes, not worse fills. Corrected, a later entry barely
changes the result — a cleaner finding, and the opposite shape from what I reported.

### The 5-minute tolerance, as requested

| tolerance | scored | admissible-buy net ₹ | entries refused | realised hold |
| --- | ---: | ---: | ---: | --- |
| 10 min (declared) | 101 | +277,082 | 4 | 60 / 60 / 65 |
| **5 min** | **98** | **+274,438** | **7** | 60 / 60 / 65 |

Tightening costs 3 scored outcomes and 1% of the net. The 10-minute value stays the declared
development assumption; nothing here depends on it.

## 4 · The recovered signals, including 17 November

**Codex's direction counts reproduce exactly — all 39 rows**, with the SQL as given. The two
sessions I called unobservable are recoverable from the database:

| date | NIFTY | BANKNIFTY |
| --- | --- | --- |
| 12 Nov | 75 NEUTRAL, 3 UNAVAILABLE | 75 NEUTRAL, 3 UNAVAILABLE |
| 13 Nov | 1 BULL, 74 NEUTRAL, 3 UNAVAILABLE | 75 NEUTRAL, 3 UNAVAILABLE |
| 16 Nov | 75 NEUTRAL, 3 UNAVAILABLE | 1 BULL, 74 NEUTRAL, 3 UNAVAILABLE |
| **17 Nov** | **14 BULL**, 61 NEUTRAL, 3 UNAVAILABLE | **39 BULL**, 36 NEUTRAL, 3 UNAVAILABLE |
| 18 Nov | 32 BEAR, 43 NEUTRAL, 3 UNAVAILABLE | 16 BEAR, 59 NEUTRAL, 3 UNAVAILABLE |
| 19 Nov | 46 BULL, 29 NEUTRAL, 3 UNAVAILABLE | 45 BULL, 30 NEUTRAL, 3 UNAVAILABLE |
| 20 Nov | 38 BULL, 36 NEUTRAL, 3 UNAVAILABLE | 55 BULL, 19 NEUTRAL, 3 UNAVAILABLE |

So **my "16 and 17 November have no signal" was a statement about the export, not the data.**
`authority_doc` is null in those exports; `ml_brain_snapshots` has 78 snapshots on each.

### And Codex's clock warning is confirmed by measurement

`created_at` is the database write and it **does** precede publication. For the 17 Nov 11:45
snapshot: label 11:45:00, `analysis_ts` 11:45:08.4, row `poll_ts` 11:45:19, `created_at`
11:45:22.1 — while the export's `published_at` for comparable polls is ~**+26s**. Worse, the write
lag is wildly variable: **min 3.8s, mean ~32s, max 346s** across 16–20 November. `created_at` is
not a usable availability bound in either direction.

**The defensible bound from recorded evidence** uses `analysis_ts`, which `by_index` carries on
every session. Calibrating `published_at − analysis_ts` on the 694 polls where **both** clocks
exist (12, 13, 18, 19, 20 Nov): median **+17.1s**, p95 **+24.3s**, **max +33.7s**.

So for 16–17 November the conservative availability bound is `analysis_ts + 33.7s` — the observed
maximum, deliberately conservative and labelled as such. And a useful robustness fact:
`analysis_ts` is ~10s past the label, so the bound lands ~44s past the label, still inside the
label's own minute. **Any bound between the label-minute poll (+7s) and the next poll (+5m 7s)
selects the same entry poll**, so the recovered baseline's entry is insensitive to this choice
across the whole plausible range.

**What the snapshots do *not* establish**, exactly as Codex says: the full single-leg intent, the
ER, the chosen strike or the real publication clock. `by_index` carries direction, decision id,
`analysis_ts`, `decision_poll_ts`, action, candidate ids and managed-selection status — no
`why.sl`. So 16–17 November can support a **recorded direction baseline** (direction + a fixed
contract rule) and **not** a replay of an actual single-leg intent. Those stay separately
labelled, as they are in `direction_baseline.py` already.

## 5 · Correcting the completeness test

Codex is right that "five consecutive sessions with intents" is the wrong acceptance test — a
neutral day with zero intents is a valid outcome, and new persistence cannot recover fields never
stored. Replacing it with a completeness test over **scheduled observations**, counting each
cause separately:

| per session and index, count | why it is its own count |
| --- | --- |
| scheduled decision polls | the denominator |
| polls with a retained authority record | missing history is a persistence defect |
| polls with `direction` ∈ {BULL, BEAR} | a neutral day is not a failure |
| polls with a `why.sl` single-leg plan | absence here is not absence of a signal |
| polls where opt-in was disabled | a configuration state, not a defect |
| polls with no eligible entry, by reason | the entry window, the snapshot state, and so on |
| polls with a documented failure | the only count that is a defect |

**Acceptance:** retained authority records ≥ 95% of scheduled polls, every shortfall attributed
to one of the named causes, and zero polls unaccounted for.

## 6 · Withdrawing the `BROKER_SNAPSHOT_UNAVAILABLE` claim

I called it "the most expensive single defect in this programme" and said the rule "had a plan and
could not price it" on every actionable poll. **That is withdrawn.** Codex's source inspection is
decisive: the dedicated broker quote is normally requested **at a Paper tap**, and
`ELIGIBLE_PENDING_SNAPSHOT` is a **valid pending proposal**, not a failure. An after-close trace
from 20 November cannot evidence a live tap, a request or a failed response — nobody tapped.

The diagnosis splits into six separable stages, and I have evidence for none of them:

| stage | what the export shows |
| --- | --- |
| quote readiness before a tap | not recorded |
| rendering / BUY-or-WAIT display | not recorded |
| user action (the tap) | not recorded; **no tap is evidenced** |
| the broker request | not recorded |
| response validation | not recorded |
| final admission | only the pending state, `ELIGIBLE_PENDING_SNAPSHOT` |

So the correct statement is: **the export records a pending proposal awaiting a tap-time quote,
and says nothing about whether the pipeline would have succeeded.** I will coordinate acceptance
checks with Grok's pipeline assignment rather than restate this as a defect.

I also accept the related point: historical database coverage does **not** establish a live
phone's successful capture, admission or exit. The close-to-open phone capture stays as device
persistence and lifecycle evidence alongside the database replay, not replaced by it.

## 7 · Deterministic checks — all five Codex asked for

`tools/trade_hold_exit/tests/`, **49 tests, OK**, no network and no database. Each is built on a
constructed session where the answer is known by hand:

| check | test |
| --- | --- |
| position-cap enforcement | two BULL intents 5 min apart → 1 entry, 1 `SKIP_POSITION_ALREADY_OPEN`; the two indices hold independently; a close frees the index |
| delayed-entry hold duration | publication 09:50:25 → fill 09:55:07 → planned and actual exit **10:55:07**, held exactly 60.0 min; 30/60/120 each move the exit by exactly that much |
| superseded signals | a contract with no 09:55 poll pushes the fill to 10:00, by which time the 09:55 decision is published → `SKIP_DECISION_SUPERSEDED`, and only the fresh decision is taken. A fill *before* the next publication is **not** superseded |
| unresolved exposure | series stops before the exit mark → position `UNRESOLVED`, net 0, capital **not** released, `cash_end + unresolved_capital = capital`, and the index stays blocked against a different contract |
| SELL treatment | a SELL intent yields 0 entries and a `SKIP_SELL` count; `price()` refuses it by name; the same shape as a BUY prices normally |

One of those tests initially passed for the wrong reason — a later poll on the same contract was
also a usable exit mark within tolerance, so the position closed instead of staying unresolved. It
is rewritten to use a different contract, and the note is in the test.

## 8 · What I have not done yet, and why

* **The direction-only baseline over all seven sessions** (pass 1's comparator, pass 2's premium
  filter, pass 3's exposure-matched control). The signals are recovered; what is missing is the
  chain slice for 12/13/16/17 November at the nearest-the-money strikes. That is one bounded
  read, and it is the next thing I will do.
* **Passes 4 and 5 on the portfolio basis** — early exits and the next-session hold. Pass 4's
  horizons are in §3 as a portfolio comparison; the early-exit set needs the per-poll path
  between entry and exit, which the chain cache already holds for 18–20 November.
* **Fee reproducibility (Codex's point 5).** The modified dated fee implementation still lives in
  a worktree and is **not** committed, so another checkout cannot reproduce these rupees. Every
  run stamps `provisional_cost_basis` and the dirty file. This is unchanged and still owed.
* **Chain-cache identity.** The key is `session|index|strike|option_type` with **no expiry**,
  which is safe only because every session/index in this window carries exactly one expiry
  (verified: all `2026-11-23`). **Expiry must go into the key before the cache is extended**, and
  I have noted it rather than extended it.
* **Points vs rupees labelling.** `gross_points` is lot-independent; `net_points` is
  `net_inr / lot` and therefore depends on quantity through the per-order cost allocation. Both
  are now emitted under those names.

## 9 · Reproduce

```bash
# the intents and all their clocks, plus the read-only join as reviewable SQL
python3 -I tools/trade_hold_exit/intent_ledger.py <export_18> <export_19> <export_20> \
    --horizon-min 60 --out tools/trade_hold_exit/results --emit-sql
# the chain read (MCP is the only route; the proxy refuses supabase.co)
python3 -I tools/trade_hold_exit/load_chain_result.py <saved-result.txt>... \
    --out tools/trade_hold_exit/results/chain_cache.json
# the opportunity diagnostic, fill-anchored, with the sensitivities
python3 -I tools/trade_hold_exit/intent_replay.py --ledger <ledger> --chain <chain> \
    --ledger-root <tree> --horizon-min 60 [--delay-polls N] [--tolerance-min 5] --out <out>
# the achievable policy
python3 -I tools/trade_hold_exit/portfolio_replay.py --ledger <ledger> --chain <chain> \
    --ledger-root <tree> --horizon-min {30,60,120} [--capital-inr 15000] --out <out>
cd tools/trade_hold_exit && THE_LEDGER_ROOT=<tree> python3 -m unittest discover -s tests  # 49
```

The recovered-direction SQL is Codex's own, run unchanged; the clock calibration query is in §4.

| file | sha256 |
| --- | --- |
| `intent_ledger.py` | `39fbc668c52d644e76130117da9c15cf2e656beed991e62817525214c890b0e2` |
| `intent_replay.py` | `cd4a8f1b7ff626a95ccf09b55e5d968d7d2e8252acd9c36a18ad6b245989d57a` |
| `portfolio_replay.py` | `65debf308fe4287926a6dfc1724bcc94660c248ee3e0b904c05464eab18c7d3f` |
| `load_chain_result.py` | `f6fa98a97a7881d1c7ca07bd2e480e6cb98abd576e864280898f33c6f5d09392` |
| chain read 18–19 Nov | `c95d27d0ea9416a257bc33dbb6e39b4856e82ef2304d19dee04ca88d12071a4c` |
| chain read 20 Nov | `08c7685100c533460735974ee72b885958eadae78fec885e2015b2b4cd109f35` |
| `export_2026-11-18.json` | `31b58b582751fd8390d25754d84d97d10c7bd597dc180e7371e42f6b7f560cf0` |
| `export_2026-11-19.json` | `0b50c96e47fddf0d7212ceb29b1200bb3b9fe7653e84ecc931699e99e70afbc2` |
| `export_2026-11-20.json` | `67e14b90b0fb08a2b0dfc8b5928bad49fbda34fc4f758722ec0751eaf6e6098a` |

## 10 · Decision, per index and side — unchanged, on firmer ground

| index | side | achievable n | achievable net ₹ | decision |
| --- | --- | ---: | ---: | --- |
| BNF | CE | 8 | +20,686 | **keep WAIT** — drop 20 Nov and the whole portfolio is −1,722 |
| NF | CE | 5 | +7,341 | **keep WAIT** — 100% win on five trades is not evidence |
| BNF | PE | 1 | −2,127 | **insufficient evidence → WAIT** |
| NF | PE | 2 | −886 | **insufficient evidence → WAIT** |

Per Codex's instruction that a call rule may advance on its own evidence: **the call side does not
qualify yet either**, because the achievable policy goes negative without its best session. The
put sample is 3 trades and needs diagnosis, not a verdict — the four diagnostic candidates (wrong
direction, late entry, expensive premium, hold/exit rule) need the direction baseline from §8 to
separate them, since with 3 trades I cannot attribute anything.

**No finalist set is frozen in this delivery.** The existing fresh-session lock is unchanged and
unversioned: nothing here met its bar, so there was nothing to revise.

## 11 · Scope

Offline research on a review branch; Supabase **read-only** — no write, migration or schema
change. Every rupee `PROXY_SIMULATED_FILL`; **no order placed, no Paper position**, and the
exports contain zero actual Paper positions. Manual Paper stays accessible, Real disabled,
uncovered shorts excluded. 18–20 November rests on the **unreviewed** fee revision. Development
evidence, outside the unsigned prospective v4 count. No `main` merge, build, release or PR.

---

### What I would ask you to decide

1. **Accept the achievable policy as the decision basis** (+25,014, **−1,722** without 20 Nov) and
   the opportunity diagnostic as a diagnostic only.
2. **Confirm the supersession rule** — "a newer decision for the same index published at or before
   the fill" — before I build passes 2–5 on it.
3. **Confirm the conservative availability bound** `analysis_ts + 33.7s` for 16–17 November, and
   that the recovered direction baseline stays labelled apart from an actual intent replay.
4. **The fee implementation still needs committing** for another checkout to reproduce these
   rupees. Tell me whether to commit it as `*_UNREVIEWED` on this branch or keep it out.
5. **One bounded chain read** for 12/13/16/17 November unlocks the seven-session direction
   baseline and the whole of passes 2–3. I will take that as agreed unless you say otherwise.
