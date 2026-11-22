# Premium model validation — is the forecast's centre worth anything?

**Branch** `review/premium-model-validation-20261121`, cut from `74e0680` on
`review/premium-neutral-range-comparison-20261116`, itself cut from the verified R7 replay commit
`124d62ee027941e43decc737e53d202c32b1d214`. Offline research only. Nothing here collects, trades,
writes to a database, changes a frozen rule, or touches the installed build, Paper controls, v7
ranking, gates, sizing, exits or notifications.

The frozen R7 module is **read, never modified**: `git diff 124d62e -- tools/spot_path_baseline/`
is empty.

## What is here

| file | what it is |
| --- | --- |
| `PREDECLARATION_20261121.md` | the comparison, fixed before any variant was scored. Committed **alone** in `424d8a7` with no scoring code; a test checks that ordering from git history rather than asserting it in prose |
| `tilt_compare.py` | the runner: `E0_TILT(λ)` through the frozen model, the CRPS decomposition into a tilt cost and a shape value, the λ sensitivity curve, and the declared breakdowns |
| `economic_check.py` | the **causal** replay: WAIT, a buy-all diagnostic, the median forecast rule and the predeclared `RANGE_EV` rule, all on the same decision-time population. Costs are estimated at the decision and realised after the exit, and the two never meet. It **refuses to run** without the reviewed Stage D ledger rather than reimplement a fee table |
| `fee_worked_cases.py` | the fee arithmetic printed **by** the governed model, so a figure in a review document cannot drift from the code |
| `spread_replay.py` | two-leg debit verticals on the same population. Fees are charged on **each executed leg**, not on the net spread price — charging the net understated turnover 2.26× and skipped the short leg's entry STT entirely, which is the defect corrected on 22 November |
| `stress_range_ev.py` | the survival stress for the one arm that stays positive under every leave-one-session-out. Concentration (remove the best k), the unresolved exits priced three ways, a second adverse tick each way with the **decision left unstressed**, and the split between a capture-window boundary and a genuine hole in the book |
| `PREDECLARATION_RANGE_AWARE_RULE_20261121.md` | the one range-aware rule, committed before its implementation |
| `FEE_EXTENSION_REVIEW_20261121.md` | the unapplied fee extension, with dated sources and worked cases, returned for its own review |
| `OVERNIGHT_CAPTURE_CHECKLIST_20261121.md` | why no overnight pair can be formed from the current capture window, proven rather than inferred |
| `tests/` | 155 checks, no network. Seven need real inputs and skip loudly without them |
| `results/` | the printed runs and machine-readable scores for 12 and 16 November |

## The answer, in three lines

1. **The range is worth a lot.** Holding the centre equal, the distribution's *shape* improves
   CRPS in **16 of 16** index/horizon cells, by 1.8 to 14.8 points.
2. **The centre tilt is not.** It *costs* CRPS in **3 of the 4 primary cells** and 9 of 16
   overall, and asymmetrically: the nine positives sum **+23.834** and reach +6.372, the seven
   negatives only **−4.931** with the largest just −1.794. Median absolute error rises
   monotonically with λ in 3 of the 4 primary cells. The predeclared challenger λ = 0.5 loses the
   median-MAE half of its primary comparison in **3 of 4** cells, so the predeclared outcome is
   **split**, not an improvement.
3. **Neither earns money here.** WAIT beats every arm on both sessions and both indices —
   including the predeclared range-aware `RANGE_EV` rule, which loses in all four cells and fails
   its own declared downside bar in all four. Buying every feasible contract loses ₹197–430 a
   trade after costs.

> **The economic figures were corrected on 21 November.** Codex found two causality violations in
> the first replay and a third followed: the decision read a forecast anchored one poll *after* it
> (differing in 330 of 330 rows), priced costs with the *realised* exit fee (flipping 29 of 330
> decisions), and was offered only opportunities that later filled and exited (dropping 84 of
> 414). The replay is now causal by construction — `decision_view` is a whitelist a rule cannot
> see past — and `--legacy-noncausal` reproduces the superseded path so the reconciliation is a
> run and not a memory. **WAIT still wins, so the conclusion survived; the forecast-arm numbers
> did not and are withdrawn.** See `CLAUDE_CODE_REPLY_PREMIUM_CAUSAL_REPLAY_20261121.md`.

4. **The sharpest open question is the right tail, not the centre.** `RANGE_EV`'s entire buying
   signal is that recentring fixes the *median* on the entry midpoint while a right-skewed price
   distribution puts its *mean* ~₹1.7 higher — in 390 of 390 rows — which alone clears the ~₹1.3
   round-trip cost. The trades do not deliver it. Whether that tail is calibrated is now the
   specific thing to test, and CRPS at 80% coverage cannot answer it.

## The exact invocations

```bash
# the scored comparison, both sessions
python3 -I tools/premium_model_validation/tilt_compare.py \
    <export_2026-11-16.json> <spots_20261021_20261104.jsonl> \
    --horizons 15,30,60,120 --breakdowns --out tools/premium_model_validation/results

# the identity that makes lambda = 1 the frozen model, on its own
python3 -I tools/premium_model_validation/tilt_compare.py --self-check \
    <export.json> <spots.jsonl>

# the economic arms. --ledger-root must contain tools/premium_followon/cost_ledger.py
python3 -I tools/premium_model_validation/economic_check.py \
    <export.json> <spots.jsonl> --ledger-root <tree> \
    --out tools/premium_model_validation/results

# the tests. 7 need real inputs:
cd tools/premium_model_validation && python3 -m unittest discover -s tests   # 46 run
TILT_EXPORT=<export.json> TILT_SPOTS=<spots.jsonl> \
EC_LEDGER_ROOT=<tree> EC_EXPORT=<export.json> EC_SPOTS=<spots.jsonl> \
    python3 -m unittest discover -s tests                                   # 46 run, 0 skipped
```

**Inputs are not committed.** Raw phone exports stay out of the repository; every result records
its inputs by SHA-256 so a figure can be tied to its input without the input being in the tree.

## Three things a reader must not take on trust

* **The economic figures were produced with an UNAPPLIED patch.** The committed Stage D fee table
  is verified only through **9 October 2026**, so both November sessions fail closed on it — 330
  of 414 rows refused with `fee_rates_unknown_for_trade_date`, which is the fail-closed design
  working. The extension to 16 November is verified against primary sources but is still an
  unapplied patch (`tools/premium_followon/patches/0001-fee-table-verified-through-20261116.patch`
  on the follow-on branch) awaiting sign-off. `economic_check.py` records the ledger tree's
  revision **and its dirty files** on every run, so a patched-tree result cannot be mistaken for
  a committed-tree one. The committed results under `results/` carry
  `MODIFIED: research/replay/fee_models.py`.
* **Every rupee is a simulated fill.** Label `PROXY_SIMULATED_FILL`; no order was placed. The
  ledger's own open items ride along: quote freshness, and `quantity_unit_status` is `UNKNOWN` on
  these records.
* **The exchange quote clock is unverified.** Every instant is the app's own acquisition receipt.
  `receipt_proxy` is carried on every row and printed on every table.

## A defect in the frozen module, reported and not patched

`spot_timeline` (`premium_repricing_v2.py:382`) and `reverse_input` (`:853`) dereference
`ex['parts']['authority_doc']` with no guard. In the 16 November export that key is JSON `null`,
so both raise `TypeError`. The module is frozen and byte-identical to `124d62e`; fixing it here
would break that guarantee and the protocol hash test, so instead:

* `tilt_compare.py --self-check` falls back to the range runner's own frozen-parity chain and
  **says which chain it used**;
* the three `premium_range_compare` tests that chain to the frozen replay now **skip naming this
  reason** instead of erroring. A skip is not a pass: it records that the frozen comparison was
  unavailable on that input.

## Two digests for one session

The 16 November export arrived on 21 November with SHA-256 `2cd20066…`, not the `903225d9…`
reported independently on 17 November — yet **every figure reproduces from it exactly**. The two
files are content-identical evidence that differ in bytes. Both keys are pinned in
`premium_range_compare/tests/test_range_compare.py`: the original as the independent citation, the
new one as the local reproduction.
