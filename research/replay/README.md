# Offline holding-horizon replay v2 (Batch 2, revised after Codex R1)

Research only. This directory lives outside `app/src/main/python`, so it is **not bundled into the APK** and has no runtime effect. It reads `app/src/main/python/contract_lot_table.py` only to resolve lots.

| File | Purpose |
| --- | --- |
| `replay_engine.py` | Parser for `nf_quotes_v2`, Stage A legacy emulation (`run_legacy_sql_v0`) and Stage B corrected engine (`run_corrected`). |
| `extract_nf_quotes.sql` | Bounded, read-only raw-row extract, run after market hours, one part at a time. Run `EXPLAIN` on part 01 first. |
| `stage_a_sql_crosscheck.sql` | The executed 5 Oct sweep SQL, reduced only where the published cell cannot be affected. Gives an independent per-day Stage A. |
| `run_stage_a_b.py` | Verifies the part manifest, runs both stages and writes the ledgers, a bridge, a report and `SHA256SUMS`. The exit code is non-zero unless the Stage A gate passes. |
| `tests/test_replay_engine.py` | 38 synthetic fixtures, rendered through the real extract format. |

## Extract format `nf_quotes_v2`

Nothing is aggregated, de-duplicated or validated in SQL.

```
S|<session>                                         date with ml_brain_snapshots rows (legacy observed calendar)
W|<session>|<E|C|L>|<any|nf>|<poll_ts>|<NF rows>    window poll: any = first poll of ANY index (sweep rule),
                                                     nf = first poll with NF rows; L|nf = last NF poll when no NF C poll
A|<poll_ts>|<expiry>|<k0>|<spot>|<straddle>|<tied strikes>|<distinct min pairs>   sweep ATM over valid rows
R|<poll_ts>|<expiry>|<strike>/<C|P>:<bid>,<ask>[/...];...   one token per STORED row (duplicates repeat)
D|<poll_ts>|<NF rows>|<distinct (strike,type)>|<valid expiries>|<non-date-expiry rows>   integrity counts
```

A part file is a header followed by the exact body that Postgres hashed:

```
#format=nf_quotes_v2
#part=NN
#range=A..B
#n=<lines>
#md5=<md5(body) from Postgres>
#body
<body>
```

The runner rejects any of the following:

- a missing, extra or misnamed part;
- a manifest gap or overlap;
- a format, part id, range, line-count or md5 mismatch;
- an `S` or `W` line outside its part's range;
- a repeated key across parts. A repeated `R` line would fabricate duplicates, so it is rejected too.

The table's unique index is `ml_ocs_unique (poll_ts, index_key, strike, option_type)`, without expiry. Two consequences:

- Stored duplicates at one key should be impossible.
- Two expiries at one poll can only occupy disjoint strikes.

The extract measures both instead of assuming them.

## Stage A: `run_legacy_sql_v0`

Stage A reproduces the sweep's own semantics:

- **Poll:** the first poll of any index in the window.
- **Quotes:** valid rows only, with duplicates counted as separate rows.
- **Expiry:** every valid expiry at the poll, cross-joined with every expiry's ATM (`ent2`).
- **Calendar:** observed calendar from 2026-06-15.
- **td:** weekdays + 0.48.
- **RV:** RMS over every cross-expiry close pair from sn−6 to sn−2.
- **Band:** `vb = v1.3`.
- **Exit:** C2, observed, on or before expiry, on the first any-index close poll, inner join on all legs.
- **Lot:** fixed 65, labelled `legacy_fixed_65_historical_assumption`. This is an assumption, not an authority.
- **Fees:** sweep fee formula.

Some rows can depend on SQL behaviour that isn't deterministic (ATM ties) or on the cross join. Those rows carry `parity_unproven`, and an exact reproduction cannot be claimed for them.

**Gate:** the run passes only if all of these hold:

- all 17 published nets match within ₹0.05;
- 23 Jul is `eligible` with outcome `missing_close`;
- there are no extra eligible dates;
- no published date is `parity_unproven`.

## Stage B: `run_corrected`

| | Rule |
| --- | --- |
| Poll | First poll with NF rows in [12:30, 12:55) / [15:20, 15:45) IST |
| Expiry | Entry: nearest valid expiry ≥ session. Exit and marks: the entry expiry, explicitly. |
| Duplicates | Identical duplicates are accepted. Any conflicting duplicate quarantines that chain. |
| ATM | Sweep metric; ties go to the lowest strike. |
| Calendar | NSE exchange sessions (asserted equal to `brain.py` `NSE_HOLIDAYS`). |
| td | Exchange sessions to expiry + (15:30 − **actual** entry poll time) / 375 min, floor 0.05. |
| RV | Exactly 5 consecutive close-to-close returns, or ineligible. Zero RV is never bucketed. |
| Lot | `contract_lot_table.resolve_contract_lot(index, as_of=session, expiry=expiry)`; unresolved or ambiguous → `lot_unresolved`, counted. |
| Fees | `replay_fee_v1`: teacher_v1 rates + STT on every option sale. |
| Structures | `IB` and `IC` only. Other names raise. Directional families are **not wired** (deferred). |

## Sequential occupancy (Stage B)

- One position at a time, held through the planned exit.
- If the planned exit is unpriced, the **same legs on the same expiry** are repriced on each later scheduled close:
  - The first complete valuation becomes a separate `deferred_mark` and frees capital that session.
  - A partial close, a missing expiry or invalid quotes do not free capital.
  - If the contract expires first, the position is `unresolved_contract_expired` and capital stays blocked. No settlement assumption is made.
  - If the cutoff comes first, it is `unresolved_open_at_cutoff`.
- Unresolved exposure, as the sum of max loss, is reported separately from resolved net P&L.

## Run

```
python3 -m pytest -q research/replay/tests          # or: python3 -m unittest discover -s research/replay/tests
python3 research/replay/run_stage_a_b.py <extract_dir> <out_dir>
```
