# The research fee model — **UNREVIEWED**, research-only

Committed under the authority of §7 of `CODEX_REPLY_CLAUDE_CODE_CONDITION_STRATEGY_HOLD_EXIT_ASSIGNMENT_20261122`:
*"Commit the exact fee implementation used by research as an explicitly `UNREVIEWED`
research-only revision on the existing review branch… Keep the production fee authority separate
until reviewed."*

**This is not the production fee authority and must not be promoted into one.** It exists so that
a reviewer can reproduce the rupee figures in the trade/hold/exit reports from a branch SHA plus
the fingerprinted read-only inputs, which until now they could not: the file was live only in a
scratch worktree.

| | |
| --- | --- |
| file | `fee_models_UNREVIEWED.py` |
| sha256 | `d9794134fe9f57d11d3013717df85cd5d3336a0a69f58b8bab23478b5d1c06e6` |
| `RATES_TABLE_VERSION` | `nse_equity_index_options_rates_20261120_v3_UNREVIEWED` |
| `RATES_VERIFIED_THROUGH` | **2026-11-20** |
| copied from | `tools/premium_followon/` tree at revision `c73b28cd7358ac7e0307af48f8effe40ee430492`, **working-tree modified** |
| loaded by research as | `research/replay/fee_models.py`, via `cost_ledger.py` (`sha256 319baaa617d83180b5f1e41b3a1c87087eaaf3a7249e8f7478485e4a9d1d1cf0`) |

## What is actually unreviewed — and it is narrower than I have been saying

I have been describing this as "an unreviewed fee revision", which overstates it. The complete
diff against the reviewed `nse_equity_index_options_rates_20261009_v1` is **two metadata lines**:

```diff
-RATES_TABLE_VERSION = 'nse_equity_index_options_rates_20261009_v1'
+RATES_TABLE_VERSION = 'nse_equity_index_options_rates_20261120_v3_UNREVIEWED'
-RATES_VERIFIED_THROUGH = date(2026, 10, 9)
+RATES_VERIFIED_THROUGH = date(2026, 11, 20)
```

**No rate value differs.** Every charge, every dated window and every source citation is the
reviewed table's. `RATES_VERIFIED_THROUGH` is also the end date of the open rate window
(`nse_idx_opt_20260401`), so moving it is precisely what lets a trade dated after 9 October be
priced at all instead of failing closed.

So the unreviewed claim is exactly one claim: **that no circular changed an equity-index option
charge between 10 October and 20 November 2026.** Everything else is already reviewed.

**The evidence for that claim**, as reported in the span memo §8: NSE's own circulars listing for
12–19 November 2026 contains exactly one Finance & Accounts circular, **NSE/FA/77421** (13
November), *"Transaction Charges for Electronic Gold Receipts (EGRs)"* — not options. Read from
the issuer's listing, not inferred from a broker page.

**What that evidence does not cover: 9 October – 11 November.** That window is still unchecked at
source. It is the reason this stays `UNREVIEWED` and why every run stamps
`provisional_cost_basis`.

## The dated rate windows, as committed

| window | from | to | STT on sale | exchange txn | IPFT | sources |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `nse_idx_opt_20241001` | 2024-10-01 | 2026-02-28 | 0.10% | ₹3,503/crore | ₹50/crore | FATAX/63809 |
| `nse_idx_opt_20260301` | 2026-03-01 | 2026-03-31 | 0.10% | ₹3,552.99/crore | ₹0.01/crore | FATAX/63809 |
| **`nse_idx_opt_20260401`** | **2026-04-01** | **`RATES_VERIFIED_THROUGH`** | **0.15%** | ₹3,552.99/crore | ₹0.01/crore | **FATAX/73524**, Finance Act 2026 |

Common to all: SEBI ₹10/crore of turnover · stamp 0.003% on the **buy** side only · GST 18% on
(brokerage + exchange + SEBI) · brokerage ₹20 per order.

A check worth recording because it looked wrong at first: the model charges STT at **0.15%**
while the `stt_2024_10_01` source says 0.10%. That is correct — 0.15% belongs to the
`nse_idx_opt_20260401` window and is sourced to **NSE/FATAX/73524** (31 March 2026, Finance Act
2026), with the 0.10% citation attached to the two earlier windows. No defect.

## Worked examples

A **long option** round trip, one leg: buy to open at the ask, sell to close at the bid. These are
the exact figures behind the reports, produced by this file.

### 1 · BANKNIFTY 63100 CE, the real 20 November 09:55 fill — lot 30, 445.30 → 500.00

| component | ₹ |
| --- | ---: |
| brokerage (2 orders × ₹20) | 40.0000 |
| exchange (₹3,552.99/crore × 28,359) | 10.0759 |
| IPFT | 0.0000 |
| GST (18% of brokerage + exchange + SEBI) | 9.0137 |
| STT (0.15% × 15,000 sale) | 22.5000 |
| stamp (0.003% × 13,359 buy) | 0.4008 |
| SEBI (₹10/crore × 28,359) | 0.0284 |
| **total** | **82.0188** |

turnover 28,359.00 · gross **+1,641.00** · net **+1,558.98** · net points **+51.9660**

### 2 · NIFTY, a round 100 → 110 — lot 65

brokerage 40.0000 · exchange 4.8498 · GST 8.0730 · STT 10.7250 · stamp 0.1950 · SEBI 0.0136 ·
**total 63.8565** · turnover 13,650.00 · gross **+650.00** · net **+586.14** · net points
**+9.0176**

### 3 · BANKNIFTY, a loser 400 → 380 — lot 30

brokerage 40.0000 · exchange 8.3140 · GST 8.6965 · STT 17.1000 · stamp 0.3600 · SEBI 0.0234 ·
**total 74.4939** · turnover 23,400.00 · gross **−600.00** · net **−674.49** · net points
**−22.4831**

**Reproduce:**

```bash
python3 -I - <<'PY'
import sys, datetime
sys.path.insert(0, 'tools/trade_hold_exit/research_fee_model')
import fee_models_UNREVIEWED as F
print(F.RATES_TABLE_VERSION, F.RATES_VERIFIED_THROUGH)
print(F.fees_v2(datetime.date(2026,11,20), datetime.date(2026,11,20), 1, 30,
                {'sell_px':0.0,'buy_px':445.30}, {'exit_sell_px':500.00,'exit_buy_px':0.0}))
PY
```

## Lot and tick authority

| | value | authority |
| --- | ---: | --- |
| BANKNIFTY lot | **30** | `cost_ledger` production-authority resolution, cross-checked against `brain.py` `BNF_LOT` |
| NIFTY lot | **65** | same, against `NF_LOT` |
| tick | **₹0.05** | one declared tick of adverse execution charged on each side |

**Still unresolved:** the exports' own `references.contract_lot_table` resolves to
`UNRESOLVED_REFERENCE` on every session/index node, so the lot is taken from the ledger's
constant rather than from a dated contract table the export authenticates. Gross option points
are lot-independent; **net points are `net_inr / lot` and therefore depend on quantity** through
the per-order brokerage and the fixed components. Both are labelled that way in every result.

## Scope

Research-only, on a review branch, `UNREVIEWED`. The production fee authority is untouched and
stays separate until reviewed. Promotion of any rule whose economics rest on this file requires
that review first.
