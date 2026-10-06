# Offline holding-horizon replay (Batch 2)

Research only. This directory lives outside `app/src/main/python`, so it is **not bundled into the APK** and has no runtime effect.

| File | Purpose |
| --- | --- |
| `replay_engine.py` | Engine: two definition sets, an eligibility ledger, outcomes with explicit statuses, sequential occupancy, conservation counts and output hashes. |
| `extract_nf_quotes.sql` | Rule-scoped, bounded, read-only NF quote extract, run after market hours in parts. Postgres returns an md5 for each part. |
| `run_stage_a_b.py` | Verifies each part's md5, runs Stage A (`LEGACY_V0`) and Stage B (`CORRECTED_V1`), then writes the ledgers, a bridge, a report and `SHA256SUMS`. |
| `tests/test_replay_engine.py` | Synthetic regression fixtures (listed below). |

## Extract format

The data is UTF-8 text, one record per line:

```
S|YYYY-MM-DD                                   brain-snapshot session (legacy observed calendar)
P|<poll_ts UTC ISO>|<slot E|C|L>|<index>|<expiry YYYY-MM-DD>|<strike>:<ce_bid>,<ce_ask>,<pe_bid>,<pe_ask>;...
```

- An empty price field means the quote row was absent. Validity (`bid >= 0`, `ask > 0`, `ask >= bid`) is applied by the engine, never by the extract.
- A part file is a header followed by the exact body that Postgres hashed:

  ```
  #part=NN
  #range=A..B
  #md5=<md5(body) from Postgres>
  #body
  <body>
  ```

## Definitions

| | `LEGACY_V0` (Stage A, provenance fixture) | `CORRECTED_V1` (Stage B) |
| --- | --- | --- |
| Calendar | Dates that have brain snapshots (device outages shorten it) | NSE exchange sessions (weekdays minus `NSE_HOLIDAYS`, asserted equal to `brain.py`) |
| Holding sessions | Counted on the observed calendar | Counted on the exchange calendar |
| `td` | Weekdays to expiry + (15:30 − **nominal** 12:30) / 375 min | Exchange sessions to expiry + (15:30 − **actual** entry poll time) / 375 min |
| RV | RMS of whatever close-to-close returns exist among the 5 ending at the previous session (≥ 1) | Exactly 5 consecutive returns (6 valid closes) or ineligible; zero RV is never bucketed |
| Fees | The 5 Oct sweep formula (GST on brokerage and exchange only; no SEBI or IPFT) | `replay_fee_v1`: teacher_v1 rates plus STT on every option sale, including selling longs at exit. Spread is never added again as slippage. |
| Missing exit | Unknown, kept in the ledger | Unknown, kept in the ledger |

**Shared rules:**

- **Entry:** the first poll in [12:30, 12:55) IST.
- **Scheduled close:** the first poll in [15:20, 15:45). It is never replaced by an earlier poll.
- **Expiry:** one explicitly selected nearest expiry per poll. Conflicting duplicates are rejected.
- **Exit expiry:** must equal the entry expiry.
- **Lots:** from a dated authority table. An unknown index or date fails closed.
- **`exit_on_expiry`:** stored separately from `holding_sessions`.
- **Horizon past expiry:** an entry whose planned exit falls after expiry is ineligible at decision time.

## Sequential occupancy

- One position at a time, occupied through the planned exit date.
- An unpriced planned exit keeps capital blocked until the first later session whose scheduled close exists. That mark is never added to resolved P&L.

## Regression fixtures

- 23 July-style missing scheduled close: the entry is retained and capital stays blocked.
- Expiry-coincident two-session exit.
- Holiday crossing (2 Oct, 14 Sep).
- An observed-calendar outage versus the exchange calendar.
- Duplicate and multi-expiry chains.
- Zero RV.
- Five-consecutive versus any-available RV.
- Debit cost above width; credit above width.
- Unknown index or unverified lot date.
- Teacher fee reproduction of trade #278.
- Determinism and conservation.

Run with `python3 -m pytest -q research/replay/tests`.
