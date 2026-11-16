# Stage D v1 — cost ledger, outright buying against WAIT

**Version** `premium_followon_stage_d_cost_ledger_v1` · **declared** 16 November 2026 IST ·
**implementation** `cost_ledger.py` · **checks** `tests/test_cost_ledger.py` (21 cases) plus the
bundled `--self-check` (10 cases)

The question is narrow on purpose: for one eligible decision on one exact option contract, held
for the declared window, **what is left in rupees after the charges that are actually levied** —
and is that better than not trading? **WAIT is the comparison, and WAIT is exactly zero**: no
premium, no fees, no slippage, `net_inr = 0.0`.

Outright buying only. The first phone execution target is an outright buy, so no spread, no
short leg, no margin model.

## 1 · What is charged

| | rule |
| --- | --- |
| entry | the **ask**, never the mid. A buyer pays the offer. |
| exit | the **bid**, never the mid. |
| fill delay | `FILL_DELAY_POLLS = 1` — the fill comes from the poll **after** the decision poll. A decision cannot be filled at the quote that produced it. With no later poll the row is unscorable (`fill_poll_absent`), never filled at its own quote. |
| extra slippage | `EXTRA_SLIPPAGE_TICKS = 1` tick of `TICK_INR = 0.05`, applied adversely on **each** side, on top of crossing the spread. Declared as this stage's own conservatism and **reported both ways** (`with_declared_slippage` / `without_declared_slippage`), so a reader can remove it instead of arguing about it. |
| fees | `research/replay/fee_models.fees_v2`, imported unchanged: a dated, versioned table (`nse_equity_index_options_rates_20261009_v1`) with primary-source references for STT, exchange transaction, IPFT, SEBI turnover, stamp duty, GST and the declared brokerage, charging each side at the rates in force on that side's trade date. |
| lot | from the **capture's own authority block**, and only when `lot_authoritative_resolved` is true. |

**The lot is never a constant.** The envelope records a producer claim separately from whether a
resolver validated a dated table row for that underlying effective at the session date; only the
second is a lot. A lot taken from a constant would silently rescale every rupee in the ledger —
and the 12 November NIFTY envelope claims **65**, not the 75 an older constant would have
supplied.

## 2 · What is reported per row

`gross_inr`, `fees_inr` with its full component breakdown and rate-window ids, `net_inr`,
`risk_budget_inr` (the debit plus entry charges — for a long option that is the whole capital at
risk), `risk_budget_return = net_inr / risk_budget_inr`, `max_loss_inr` (the structural floor:
the option expires worthless and every charge is still paid), `observed_downside_inr` (the net
had the position been exited at the **worst bid observed inside the holding window** — labelled a
proxy), `beats_wait`, and `gross_gain_consumed_by_costs` for the case that matters most to this
question: the quote moved the right way and the round trip still lost money.

## 3 · What is kept apart

- **The midpoint forecast never enters the money.** A forecast is scored on a mid; a trade pays
  the ask and receives the bid. `forecast_view` is carried beside the ledger and a test asserts
  that adding one cannot change `net_inr` by a rupee.
- **Proxy fills are labelled.** Every historical or simulated fill is `PROXY_SIMULATED_FILL`. An
  actual Paper outcome, where one exists, is `ACTUAL_PAPER_OUTCOME` and is never merged into the
  proxy population.
- **Rows inside one (index, session) are correlated** — one price path, overlapping strikes,
  polls minutes apart. Aggregation is therefore by **(index, session) cluster with equal weight
  per cluster**, never by row, and the cluster count and coverage are printed beside every mean.

## 4 · Declared unscorable reasons

`entry_quote_missing`, `entry_quote_unusable`, `fill_poll_absent`, `exit_quote_missing`,
`exit_quote_unusable`, `lot_authority_unresolved`, `fee_rates_unknown_for_trade_date`,
`contract_identity_unverified`, `hold_window_not_covered`.

Identity is checked **before** the book, with its own reason: "the contract could not be
confirmed" and "the book was unusable" are different findings and must be counted apart. Each
reason has a test, and the test also asserts the declared list contains no reason the code cannot
emit.

## 5 · Two measured blockers

### 5.1 The fee table stops before the sessions we would evaluate

`RATES_VERIFIED_THROUGH = 2026-10-09`. Verified directly:

```
2026-10-09 -> nse_idx_opt_20260401
2026-11-12 -> UnknownHistoricalRate
2026-11-16 -> UnknownHistoricalRate
```

So **every row on any session after 9 October 2026 is unscorable** for
`fee_rates_unknown_for_trade_date`, which is the model behaving exactly as designed: it never
invents a rate. **Evidence needed:** the rate windows re-checked against their named primary
sources (NSE circulars for STT, transaction charge and IPFT; the stamp-duty notification; the
SEBI turnover fee; the broker tariff) through the evaluation date, and `RATE_WINDOWS` extended
accordingly. That is a change to a frozen research tool and a claim about statutory rates, so it
needs its own review — it is **not** done here, and no rate has been assumed.

### 5.2 The existing capture carries no resolved lot

```
python3 -I tools/premium_followon/cost_ledger.py phone_2026-11-12.json --index NF --hold-min 60
```

| index | BUY rows | scorable | `lot_authority_unresolved` | `hold_window_not_covered` | `fill_poll_absent` |
| --- | ---: | ---: | ---: | ---: | ---: |
| NF | 318 | **0** | 240 | 72 | 6 |
| BNF | 323 | **0** | 245 | 72 | 6 |

Every 12 November envelope reports `lot_status: PRODUCER_CLAIM_UNRESOLVED`,
`lot_authoritative_resolved: false`, `lot_resolution: UNRESOLVED_NO_METADATA_SUPPLIED`. The
`hold_window_not_covered` rows are the last 60 minutes of a capture that stops at 13:40/13:45,
and `fill_poll_absent` is each contract's final poll.

The two blockers are independent: the lot refusal fires first on 12 November, and the fee window
would refuse the same rows on its own.

### Minimum new evidence for one scorable Stage D row

1. An envelope with `lot_authoritative_resolved: true` — the resolver validated a dated table row
   for that underlying at that session date (the capture already carries the field and the
   requirement string; what is missing is the resolved metadata).
2. A trade date inside a verified fee-rate window (§5.1).
3. At least `hold_min` of capture **after** the fill poll, so the hold window closes inside the
   session.
4. Two-sided positive books with verified identity at both the fill and the exit poll.

## 6 · Out of scope here

No live behaviour, no production write, no activation claim. Ask-to-bid P&L remains a **proxy**
while quote freshness and quantity units are unresolved, exactly as the frozen plan states. The
frozen fee model and the frozen repricing tooling are imported, not modified.
