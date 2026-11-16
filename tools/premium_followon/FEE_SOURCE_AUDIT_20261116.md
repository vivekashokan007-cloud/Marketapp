# Fee source audit — the dated research table against its primary sources (16 November 2026)

**Table audited** `research/replay/fee_models.py`, `RATES_TABLE_VERSION =
nse_equity_index_options_rates_20261009_v1`, `RATES_VERIFIED_THROUGH = 2026-10-09`.
**Question** can the verified-through date be moved past 9 October 2026, so Stage D can score a
November session? **Nothing in the frozen table was changed by this audit.**

Why it matters: with the lot authority read correctly (see `SPEC_D_COST_LEDGER_v1.md` §5), the
fee window is the **only** remaining blocker on 12 November — 240 of 318 NIFTY rows and 244 of
323 BANKNIFTY rows carry `fee_rates_unknown_for_trade_date` and nothing else.

## What was verified, from the exchange's own current page

Retrieved live on 16 November 2026, 11:51 IST:
**https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies**
(the page states "Updated on: 17/04/2026"), which is one of the sources the table already cites.

| component | table value | the page says | verdict |
| --- | --- | --- | --- |
| STT, sale of an option | `stt_option_sale = 0.0015` | "Sale of an option in securities — **0.15 per cent** w.e.f. **April 1, 2026**" (0.10% up to 31 March 2026), payable by the **seller on the option premium** | **matches**, and the page shows no later change |
| SEBI turnover fee | `sebi = 10 / crore` = `1e-6` | "All sale and purchase transactions in securities other than debt securities — **0.0001 per cent** (**Rs.10 per crore**)" | **matches** |
| stamp duty, equity options | `stamp_option_buy = 0.00003` | "Derivatives – Equity Options — **0.003%** — **Buyer**" | **matches** |
| GST | `gst = 0.18` | "Stock brokers Services — **18.00% (GST)** — Brokers and collected from their clients" | **matches** |

Four of the six components are therefore confirmed as still in force **after** 9 October 2026,
at exactly the values the table holds.

## Resolved later the same day: the two outstanding components

Retrieved 16 November 2026, later in the session, after the table above was written.

| component | table value | source | verdict |
| --- | --- | --- | --- |
| exchange transaction charge | `exchange_txn = 3552.99 / crore` | **NSE/FA/73061, 27 Feb 2026**, read in full: `https://nsearchives.nseindia.com/content/circulars/FA73061.pdf` — "Equity Options (on premium) … Revised outflow … Transaction Charges **Rs. 3,552.99 each side**", "applicable with effect from **March 1, 2026**" | **matches the issuing circular exactly** |
| NSE IPFT | `ipft = 0.01 / crore` | the same circular's table: "Contribution to NSE IPFT **Rs. 0.01 each side**", total Rs. 3,553 each side | **matches**, including the split |
| brokerage | `brokerage_per_order = 20.0` | the broker's own tariff, retrieved today: `https://upstox.com/brokerage-charges/` — "Equity Options — **Flat ₹20 per executed order**" | **matches** |

All six components are therefore verified. Two caveats are recorded rather than smoothed over:

* **I could not prove the ABSENCE of a later circular from NSE's own listing.** The JSON endpoint
  I tried (`/api/circulars?dept=FA&from_date=…&to_date=…`) returned `{"data":[]}` for the
  October–November window *and* for a February–March window known to contain FA/73061 — so the
  endpoint or its parameters are wrong and the empty result carries no information. It is not
  being treated as evidence. What stands in its place is secondary but real: three independent
  broker schedules retrieved today (Upstox, Zerodha, mstock) all present "from 1 March 2026 /
  0.03553% on premium" as the **current** window, with no later one listed.
* **The broker page contradicts itself on IPFT.** Its dated table says ₹0.01 per crore on
  premium (agreeing with the circular); a prose block lower down still says ₹0.50/lakh — the
  pre-rollback figure FA/73061 explicitly reversed. The circular governs.

## What was outstanding when this audit opened

| component | table value | source the table cites | what must be confirmed |
| --- | --- | --- | --- |
| exchange transaction charge | `exchange_txn = 3552.99 / crore` | NSE/FA/73061 (27 Feb 2026), w.e.f. 1 Mar 2026 | that no circular after 9 October 2026 revised it. The page above does not carry exchange transaction charges; this needs the NSE circulars list (`nseindia.com/resources/exchange-communication-circulars`) for the Finance / FA series |
| NSE IPFT | `ipft = 0.01 / crore` | the same circular's table | the same |
| brokerage | `brokerage_per_order = 20.0` | the broker's own tariff page (checked 9 Oct 2026) | the **current** tariff. This is a commercial schedule, not a statutory rate, so it can change without a circular, and it is the one component a research table cannot derive from a regulator |

Both were then fetched (see the section above). **No rate was ever assumed, and the frozen table
is unchanged by this audit.**

## The change this audit proposes — written as a patch, NOT applied

`patches/0001-fee-table-verified-through-20261116.patch`, which carries the citations and the
caveats above in its own header and applies cleanly (`git apply --check` passes). Keeping it a
patch is what **preserves the frozen table used for earlier research**: nothing in
`research/replay/fee_models.py` is changed on this branch. Two lines:

```python
RATES_TABLE_VERSION  = 'nse_equity_index_options_rates_20261116_v2'   # was ..._20261009_v1
RATES_VERIFIED_THROUGH = date(2026, 11, 16)                           # was date(2026, 10, 9)
```

`RATE_WINDOWS`' last entry ends at `RATES_VERIFIED_THROUGH`, so that single edit extends the
`nse_idx_opt_20260401` window and nothing else; the rates themselves do not move. If instead a
component **did** change after 9 October, the correct edit is a **new window** starting on that
component's effective date, never a widened one — the table charges each side at the rates in
force on that side's trade date, and that property is what makes it worth keeping.

`RATES_TABLE_VERSION` must be bumped with either edit, because the version names the audited
state of the table rather than its shape.

## What that would unblock, measured

With the fee window extended to cover 12 November and nothing else changed:

| index | BUY rows | blocked by fees alone | would become scorable |
| --- | ---: | ---: | ---: |
| NF | 318 | 240 | 240 |
| BNF | 323 | 244 | 244 |

The remaining refusals are not fee-related: 72 and 73 rows have no 60-minute hold window inside
a capture that stops at 13:40/13:45, and 6 rows per index are each contract's final poll, which
has no fill.

## Scope

This is an audit, not a change. The frozen fee model, its rate windows, its sources and its
`UnknownHistoricalRate` behaviour are untouched on this branch, and Stage D keeps reporting
coverage as **zero** while the window does not cover the evaluation dates — which is the correct
answer, not a limitation to work around.
