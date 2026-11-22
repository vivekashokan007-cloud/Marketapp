# Discrepancy ledger — policy replay v2 → v3

**Scope.** Every behavioural difference between `policy_replay.py` (v2,
`results/policy_current_phone_policy_60min.json`) and `policy_replay_v3.py` (v3,
`results_v3/v3_current_phone_policy_60min.json`), with its monetary effect stated separately from
its behavioural effect. v2 and its results are **immutable and superseded**; nothing in them was
edited.

Reproduce:

```bash
export THE_LEDGER_ROOT=<dated cost-ledger tree>
python3 -I tools/trade_hold_exit/policy_replay_v3.py \
    --ledger tools/trade_hold_exit/results/intent_ledger_60min.json \
    --chain  tools/trade_hold_exit/results/chain_cache.json \
    --ledger-root "$THE_LEDGER_ROOT" --production-root app/src/main/python \
    --policy CURRENT_PHONE_POLICY --out tools/trade_hold_exit/results_v3
python3 -I -m unittest discover -s tools/trade_hold_exit/tests -p "test_*.py"   # 122 OK
python3 -I -m unittest discover -s tools/premium_model_validation/tests          # 240 OK, 25 skipped
```

## Headline

| | v2 | v3 |
| --- | --- | --- |
| Net realised | +₹46,335.4021 | +₹46,335.4019 |
| Closed positions | 6 | 6 |
| Unresolved at end of study | 0 | 0 |
| Fees paid | ₹545.3479 | ₹545.3481 |
| Capital-required denominator | ₹72,611.00 | ₹72,785.29 |
| Return on that denominator | 63.81% | 63.66% |
| Exit reasons | TIME 5, STOP 1 | TIME 5, STOP 1 |

**The money did not move. The denominator did, and one claim about correctness did.** Net changed
by **−₹0.000227**, which is not rounding noise — it is attributed exactly in F5 below.

## Per-finding

### F1 — opposite-confirmation exit: implemented, fires zero times here, **proven**

v2 had no `INVALIDATION_OPPOSITE_CONFIRMED` exit at all. v3 calls production's own
`index_authority_v1.contrary_confirmation(entry_direction, sequence, 2)`.

Monetary effect: **zero**, and this is evidence, not an assumption. `f1_confirmation_evidence` in
the v3 result carries the per-position observation sequence:

| index | contract | session | entry dir | post-entry obs | opposite | max consecutive | confirmed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NF | 25250PE | 11-18 | BEAR | 71 | 0 | 0 | False |
| BNF | 61800PE | 11-18 | BEAR | 71 | 0 | 0 | False |
| BNF | 62400CE | 11-19 | BULL | 65 | 0 | 0 | False |
| NF | 25400CE | 11-19 | BULL | 44 | 0 | 0 | False |
| BNF | 63100CE | 11-20 | BULL | 69 | 0 | 0 | False |
| NF | 25550CE | 11-20 | BULL | 44 | 0 | 0 | False |

Not one opposite observation exists after any entry, because each session carries a single
direction: 18 November is BEAR with no BULL, 19 and 20 November are BULL with no BEAR. The rule
cannot fire on this sample for a structural reason, so a positive trigger is proven on a
constructed sequence instead — `F1PositiveTrigger` forces two consecutive opposite observations
and asserts the exit, plus the NEUTRAL-reset and UNAVAILABLE-reset cases, against production's
function.

### F2 — entry window: four reclassifications, **zero** monetary effect

v2 compared `HH:MM:SS` strings against `'14:30:00'`. Production's `_hm` returns `(hour, minute)`,
so `(9,30) <= hm <= (14,30)` **admits** 14:30:07 and 14:30:59 and refuses 14:31:00.

Checked against production itself, not against a copy of it (`F2WindowDifferential`):

| second | production | v3 | v2 |
| --- | --- | --- | --- |
| 09:29:59 | False | False | False |
| 09:30:00 | True | True | True |
| 09:30:07 | True | True | True |
| 14:30:00 | True | True | True |
| 14:30:07 | True | True | **False** |
| 14:30:59 | True | True | **False** |
| 14:31:00 | False | False | False |

Four decisions move, and they are the complete set:

| session | decision | v2 | v3 |
| --- | --- | --- | --- |
| 2026-11-19 | 14:30:27 (BNF) | REFUSED_ENTRY_WINDOW_CLOSED | REFUSED_MANAGED_POLICY_CAP_ONE_PER_INDEX |
| 2026-11-19 | 14:30:27 (NF) | REFUSED_ENTRY_WINDOW_CLOSED | REFUSED_MANAGED_POLICY_CAP_ONE_PER_INDEX |
| 2026-11-20 | 14:30:33 (BNF) | REFUSED_ENTRY_WINDOW_CLOSED | REFUSED_MANAGED_POLICY_CAP_ONE_PER_INDEX |
| 2026-11-20 | 14:30:33 (NF) | REFUSED_ENTRY_WINDOW_CLOSED | REFUSED_MANAGED_POLICY_CAP_ONE_PER_INDEX |

Hence the refusal counts: window 46 → 42, exposure cap 87 → 91, SELL 93 → 93. v3 admits all four
past the window and then refuses them because both indices already held a position at 14:30. **The
correction is real and its monetary effect is nil on this sample** — which is the evidence a
zero-impact correction needs, rather than an assertion that it does not matter.

The remaining 226 refusal differences are the `SKIP_` → `REFUSED_` rename and nothing else.

### F3 — cross-session close: zero here, and it **caught two latent defects**

v2's `close_due` never looked past the entry session, so a carried position could not close. v3
monitors every session in the study, including sessions with no entry intents.

Monetary effect on this sample: **zero** — nothing carried overnight; all six positions closed on
their entry day.

The new tests nonetheless found two genuine defects in my own v3, both invisible on this sample,
and both now fixed:

1. **Cross-session contract lookup keyed to the entry session.** `monitor()` resolved the quote
   series with `contract_series(p['session_date'], ...)`, so on a later session it read the entry
   day's series and the carried position could never close — the very defect F3 exists to fix,
   reintroduced one line deeper. Now `contract_series(session, ...)`.
2. **Look-ahead in the F1 sequence.** The confirmation sequence was built from *all* of the
   session's observations rather than those published at or before the poll being evaluated, so an
   exit could fire before its own evidence existed. `F1PositiveTrigger` exposed it as an exit at
   09:40:07 justified by observations published at 09:45 and 09:50. Now bounded by `t <= now`.

Neither changes any number in the v3 result (re-run before and after the fix: identical, including
`f1_confirmation_evidence`), because this sample has no carried positions and no opposite
observations. That is precisely why the differential harness was required rather than a numeric
reconciliation.

### F4 — reservation lifecycle: zero monetary effect

v2 declared a `pending` state and never filled from it. v3 runs
`RESERVED → FILLED | SUPERSEDED | UNFILLED`, a reservation occupies its index, and a reservation
superseded by a later authority decision before its fill is cancelled rather than filled.
No position on this sample was superseded between reservation and fill, so no rupee moves; the
lifecycle is exercised by `F4PendingLifecycle`.

### F5 — fees in affordability and cash: **denominator only**, plus a named production defect

Two changes:

**(a) Affordability.** v2 tested `fill * lot` against the ₹25,000 per-position cap. v3 tests
`fill * lot + entry_fee`, using production's `entry_fee`. No position on this sample sits in the
band where that flips the answer, so no entry changes; the boundary is exercised directly
(`test_affordability_uses_premium_plus_entry_fee`: at a ₹832.80 ask the ₹832.85 fill costs
₹24,985.50 in premium, which fits the ₹25,000 cap, and ₹25,020.35 once the ₹34.85 entry fee is
added, which does not).

What does change is the **capital-required denominator**: ₹72,611.00 → ₹72,785.29 (+₹174.29, the
six entry fees), so return on capital required reads 63.66% rather than 63.81%. The ₹46,335 net is
unchanged; only the ratio's denominator is now honest about what had to be committed.

**(b) A production fee defect, ₹0.000227 on this sample.** v3 charges fees through production's
`entry_fee`/`exit_fee` instead of the research ledger's table. Per executed leg production is
higher by `turnover × 1e-9 × 1.18`:

| | research ledger | production |
| --- | --- | --- |
| exchange transaction | 0.000355299 (₹3,552.99/crore) | 0.0003553 (₹3,553/crore) |
| IPFT | 1e-9 (₹0.01/crore) | 1e-9 (₹0.01/crore) |

Per NSE/FA/73061 the ₹3,553/crore figure **already includes** the ₹0.01/crore IPFT. Production
uses the combined rate and then adds IPFT a second time, so it double-counts IPFT and grosses the
excess up by GST. Checked on one real leg (BNF 61800PE, turnover ₹14,019): ledger ₹29.912110826,
production ₹29.9121273684, difference ₹0.0000165424 — matching
`14019 × 1e-9 × 1.18 = 0.0000165424` to 1e-9. Across all twelve executed legs (turnover
₹192,117) the predicted total is ₹0.00022670 against an observed net difference of ₹0.00022668.

So the delta is **fully attributed and is not floating-point noise**. The money is negligible
(₹0.23 per ₹1 crore of turnover) but the defect is real and belongs in production's fee table, not
in this replay. Recorded here; **not** fixed in this change, which touches no production code.

### F6 — provisional flag: corrected claim, no numbers

v2 derived `provisional_cost_basis` from git cleanliness, so committing the table would have
cleared the flag while the table was still unreviewed. v3 keeps `review_status`,
`reviewed_through_date`, `research_extension_through_date`, `unchecked_source_window` and
`git_tree_modified` as independent facts and sets `provisional_cost_basis` from the review status
and the unchecked window, with git recorded separately and explicitly **not** the review test.

Current state: `review_status: UNREVIEWED`, reviewed through 2026-10-09, research extension through
2026-11-20, unchecked source window 2026-10-10..2026-11-11, rate-value difference versus the
reviewed table **ZERO** (version string and verified-through date only). `provisional_cost_basis`
stays `true`, and would stay `true` on a clean tree.

## Equity

`account_end` asserts the long-only cash identity
(`cash = capital + realised − open_premium − fees`, holding on this sample) and publishes
`equity_lower_bound_inr` = cash with `equity_upper_bound_inr: null`. At-cost is a **reference**,
not an upper bound: with unmarked open exposure the upper bound is UNKNOWN, not `cash + reserved`.
On this sample nothing is open at the end, so the interval collapses — the field is still reported
as UNKNOWN-capable rather than silently finite.

## What this does not establish

- Nothing here is independent reproduction. Codex has not reproduced ₹46,335, the six positions,
  or the test run.
- The cost basis is provisional (F6). No rupee figure in v3 is final while that holds.
- `CURRENT_PHONE_POLICY` fidelity is now checked at the six window seconds, the exit precedence and
  the confirmation rule. It is **not** a claim that this policy is best; the owner has rejected that
  conclusion and 14:30/15:15 are not accepted as universal cutoffs for the completed product.
- F1 and F3 are proven correct on constructed sequences and inert on this sample. Their behaviour on
  real carried positions and real opposite observations is untested against data, because this
  sample contains neither.
