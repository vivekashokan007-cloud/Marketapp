# Fee extension for review — dated sources and worked cases

**21 November 2026 IST · returned separately, as asked · NOT applied to any committed tree**

Codex: *"Use a clean, pinned fee revision for reproducible rupee results. Return the fee
extension separately with dated authoritative sources and worked cases for review. Until then,
label any provisional cost results and preserve refusal for unsupported dates."*

This is that separate return. The patch itself is
`tools/premium_followon/patches/0001-fee-table-verified-through-20261116.patch` on
`review/claude-premium-followon-prep-20261116`, still **unapplied**. The sources below are its
audit, re-checked and extended on 21 November; the worked cases are new and are printed by the
model rather than typed beside it.

**Until you sign this off**, every rupee figure in the economic replay is stamped
`provisional_cost_basis` with the ledger tree's dirty files named on the run, and dates the table
does not cover are **refused**, never estimated. Both behaviours are asserted by tests.

---

## 1 · What the patch changes: two lines, and a boundary proof

```diff
-RATES_TABLE_VERSION = 'nse_equity_index_options_rates_20261009_v1'
+RATES_TABLE_VERSION = 'nse_equity_index_options_rates_20261116_v2'
-RATES_VERIFIED_THROUGH = date(2026, 10, 9)
+RATES_VERIFIED_THROUGH = date(2026, 11, 16)
```

`RATES_VERIFIED_THROUGH` is the `end` of the **last** window (`nse_idx_opt_20260401`), so the
edit extends that window and nothing else. **No rate moves. No window is added** — because no
component changed value after 1 April 2026. Had one changed, the correct edit would be a NEW
window starting on its effective date, never a widened one.

**That claim is checkable, and the worked cases check it.** Case 6 prices a trade on **31 March
2026** and resolves to window `nse_idx_opt_20260301` at **STT 0.10%**, while case 5 on 1 April
resolves to `nse_idx_opt_20260401` at **0.15%**. If the patch had widened an earlier window or
disturbed a rate, those two cases would not differ exactly at the boundary.

## 2 · Sources, per component, with retrieval dates

| # | component | rate | source |
| --- | --- | --- | --- |
| 1 | STT, option **sale** | 0.15% of premium, seller, w.e.f. 1 Apr 2026 | NSE, "SEBI Turnover Fees, STT and Other levies" (page states *Updated on: 17/04/2026*). Corroborated by Upstox's dated schedule: *"STT/CTT (From 1st April 2026) … Equity Options 0.15% on sell side (on premium)"* |
| 2 | SEBI turnover fee | ₹10 per crore (0.0001%), both sides | Same NSE page; Upstox *"SEBI Charges … ₹10/crore"* |
| 3 | Stamp duty | 0.003%, **buyer** | Same NSE page; Upstox *"Stamp Duty … Equity Options 0.003% or ₹300/crore on buy side"* |
| 4 | GST | 18% on brokerage + exchange + IPFT | Same NSE page; Upstox *"GST … 18% (on brokerage + transaction charges + IPFT charges)"* |
| 5 | Exchange transaction charge | **₹3,552.99 per crore** of premium, each side | **PRIMARY, read in full:** NSE/FA/73061, *Revision in Transaction Charges*, 27 Feb 2026, `nsearchives.nseindia.com/content/circulars/FA73061.pdf` |
| 6 | NSE IPFT | **₹0.01 per crore** of premium, each side | Same circular. Its table reads, for "Equity Options (on premium)": transaction charges *"₹3,552.99 each side"*, IPFT *"₹0.01 each side"*, total *"₹3,553 each side"*, effective **1 March 2026** |
| 7 | Brokerage | ₹20 per executed order | Upstox tariff, `upstox.com/brokerage-charges/`. **A commercial tariff, not a statutory rate** — the one component with no regulator behind it and the one most likely to move without notice |

Components 5 and 6 match the issuing circular exactly, **including the split** between the
transaction charge and the IPFT contribution.

### 2.1 · New corroboration, retrieved 21 November 2026

* **Angel One's published schedule** gives NSE equity-options transaction charges
  **0.0355299%** and IPFT **0.0000001%** — that is ₹3,552.99 and ₹0.01 per crore, matching
  FA/73061 **to every digit, including the split**. A third independent schedule agreeing at
  full precision, not just to two decimals.
* **The NSE circulars page's own listing** for its default window (12–19 November 2026) contains
  exactly **one** Finance & Accounts circular: **NSE/FA/77421, 13 November 2026, "Transaction
  Charges for Electronic Gold Receipts (EGRs)"**. So the most recent FA circular before the new
  verified-through date does not touch equity options.

## 3 · What is still NOT proven — and a control test that says so

The gap is unchanged from the original audit, and I now have a **control test** establishing that
the easy route to closing it does not work rather than merely failing:

> I cannot establish **from NSE's own circular listing** that no FA-series circular between
> **9 October and 12 November 2026** revised components 5 or 6.

`GET /api/circulars?fromDate=…&toDate=…&dept=FA` returns `{"data":[]}` for 9 Oct – 19 Nov. I then
queried **12–19 November**, a window the rendered page *demonstrably* shows contains FA/77421 —
and it also returned `{"data":[]}`. **So the endpoint is wrong or needs a browser session, and an
empty result carries no information.** Three parameter spellings were tried; direct `curl` to
`nseindia.com` is refused by this sandbox's proxy. I am not treating any of it as evidence of
absence.

What stands in its place is secondary but real, and now stronger than at the original audit:

* three independent broker schedules (Upstox, Zerodha, mstock) present *"from 1 March 2026 /
  0.03553% on premium"* as the **current** window with no later one listed;
* Angel One agrees to full precision (§2.1);
* the newest FA circular, dated 13 November, is about EGRs (§2.1).

Brokers bill these charges and update these pages when a circular lands, so their dated windows
are a reasonable check — but they are not the issuing source. **The remaining step is the
FA-series circular list for 9 Oct – 12 Nov 2026**, which needs a browser session this sandbox
cannot establish.

**A second recorded inconsistency, unchanged:** Upstox's page contradicts itself on IPFT. Its
dated table says *"₹0.01 per crore … (On Premium)"*, agreeing with the circular, while a prose
block lower down still says *"₹0.50/lakh on Equity Options premium"* — the pre-rollback figure
FA/73061 explicitly reversed. The circular governs. Noted because a reader checking that page
alone would meet the contradiction.

## 4 · Worked cases

Generated by `tools/premium_model_validation/fee_worked_cases.py`, output committed at
`results/fee_worked_cases.txt`. Each line names the rate and the base it was applied to, so it
can be recomputed from the circular with a calculator. Every case also asserts the seven
components sum to the printed total.

```bash
python3 -I tools/premium_model_validation/fee_worked_cases.py --ledger-root <tree>
```

**Case 1 — a real 16 November NIFTY row.** Buy 139.20, sell 141.45, lot 65, window
`nse_idx_opt_20260401`:

| component | ₹ | how |
| --- | ---: | --- |
| brokerage | 40.0000 | ₹20 per executed order × 2 orders |
| exchange | 6.4815 | 0.0355299% × (9,048.00 + 9,194.25) |
| ipft | 0.0000 | 0.0000001% × (9,048.00 + 9,194.25) |
| stt | 13.7914 | 0.1500% × 9,194.25 — **sale only** |
| stamp | 0.2714 | 0.0030% × 9,048.00 — **buy only** |
| sebi | 0.0182 | 0.0001% × 18,242.25 |
| gst | 8.3667 | 18% × (brokerage + exchange + ipft) |
| **total** | **68.9292** | components sum to 68.9291935315 |
| gross P&L | 146.2500 | (141.45 − 139.20) × 65 |
| **net P&L** | **77.3208** | gross − fees |

**This reproduces the Stage D ledger's own figure for that row to ten decimal places**
(`fees_inr: 68.9291935315`, `net_inr: 77.32080646849815`), so the worked case and the ledger are
not two different calculations.

The remaining cases, in the committed output:

| case | what it demonstrates |
| --- | --- |
| 2 — a real BANKNIFTY row, lot 35 | the lot comes from the row's own authority, not a constant; gross −528.50 → net −609.54 |
| 3 — a losing round trip | **STT is charged on the sale regardless of profit**, so a loss still pays it |
| 4 — a ₹1 premium | the ₹20-per-order brokerage dominates entirely at small size |
| 5 — 1 April 2026 | the window opens: STT 0.15% |
| 6 — **31 March 2026** | the **previous** window: STT **0.10%**. The boundary proof of §1 |
| refusals — 17 Nov, 1 Dec 2026, 5 Jan 2027 | all raise `UnknownHistoricalRate`: **refused, never estimated** |
| lot = 0 and lot = None | both refused. A lot is never defaulted |

## 5 · What applying it unblocks, measured

With the window extended and nothing else changed, on the **12 November** capture: **240 of 318**
NIFTY and **244 of 323** BANKNIFTY BUY rows carry `fee_rates_unknown_for_trade_date`
**and nothing else**, so they become scorable. The remainder are refused for reasons that are not
fee-related: 72–73 rows have no 60-minute hold window inside a capture that stops at 13:40/13:45,
and 6 rows per index are each contract's final poll with no fill.

On **16 November**, 330 of 414 rows per index are refused for the fee date alone.

**Until it is applied**, after-cost coverage stays at exactly **zero** by construction, and the
replay reports the fee window as the single blocker rather than estimating a cost it cannot
source. That is the current committed behaviour and I am not proposing to change it unilaterally.

## 6 · What I am asking for

1. **Sign off the extension**, or tell me the source-level FA-list check in §3 must be closed
   first — in which case the economic replay stays at zero after-cost coverage and I will say so
   rather than ship provisional numbers.
2. If you sign it off, say whether it should be **applied on the follow-on branch** (where the
   patch lives) or carried as a **new pinned revision** on the research branch. A clean committed
   revision is what makes the rupee figures reproducible from a committed tree, which is the
   whole point of your request.
3. **Component 7 (brokerage) deserves its own decision.** It is a commercial tariff with no
   regulator behind it. If you would rather the replay treated brokerage as a declared parameter
   than as an audited rate, that is a different and arguably more honest design, and it is your
   call, not mine.

## 7 · Scope

Nothing here is applied, committed to a fee table, or run against production. The frozen table
`nse_equity_index_options_rates_20261009_v1` is preserved by the extension being a patch: earlier
research keeps the exact table it ran against.
