# Direction authority diagnostics

Read-only diagnostics for `app/src/main/python/index_authority_v1.py`. They import the
module and execute it against local fixtures. **Nothing here is production code, nothing is
imported by the app, and nothing writes to a database or a network.**

Produced for the 5 November 2026 market-direction implementation assessment
(handoff §78, §112). Pinned revision: `3ffe72bd46c6f1798cc05a96e5f23bccfd97b6e0`
(main, v2.6.94/b525). The `index_authority_v1.py` body at this revision is unchanged
from the b524 replay baseline `e5e0fe9a`, so the results apply to both.

## Running

```
python3 -I brain_audit/direction_diag/D1_scale_authority_proof.py
python3 -I brain_audit/direction_diag/D2_index_isolation.py
python3 -I brain_audit/direction_diag/D3_nov05_0930_reproduction.py
```

`MR_ROOT` defaults to this repository, so they run in place from any checkout; set it to
point at another checkout. Each script exits 0 on success. The `.out.txt` files are the
recorded outputs of the committed versions, produced with no `MR_ROOT` set.

Verified on CPython 3.11.18 (desktop). Not measured on Android — no device figures are
claimed anywhere in these scripts.

## What each one establishes

| Script | Claim |
|---|---|
| `D1_scale_authority_proof.py` | The option/calendar scale is a hard single point of failure. Five cases share one healthy 09:20–11:00 spot path and vary only the scale: a healthy scale yields BULL/STRONG, and each of four scale failures (`EXPIRY_DAY_CHAIN_ONLY`, `CALENDAR_YEAR_NOT_COVERED`, `NO_VALID_PARITY_PAIR`, m outside `SCALE_M_BOUNDS`) yields `UNAVAILABLE`. The scale is observed once in `OPEN_REF_WINDOW` and frozen, so one unusable observation removes direction for that index for the rest of the session. |
| `D2_index_isolation.py` | Per-index isolation (handoff §78). A BNF path **shape** change leaves NF byte-identical while BNF responds NEUTRAL → BULL/STRONG. Separately, a uniform rescaling of BNF's whole series changes nothing for either index — direction is invariant to uniform rescaling because both z-scores are log ratios against a reference inside the same series. A test that rescales uniformly therefore proves nothing about isolation. |
| `D3_nov05_0930_reproduction.py` | Reproduces the published 5 November 09:30 records for both indices from the handoff's §46–54 table: the `(z_open, z_30)` pair is self-consistent under the deployed forms, the solved m lies inside `SCALE_M_BOUNDS`, a **time-scaled** reading of `z_open` is **excluded** because it would require m above the 0.05 ceiling, and `compute_direction` reproduces NEUTRAL/`['WEAK']` plus `z_open`, `z_30` and ER to three decimals. |

## Evidence classification

- **Phone-verified** (handoff §46–54; Codex reproduced all 78 polls at b524): the 5 November
  09:20 reference, 09:30 spot, `z_open`, `z_30`, ER and the production labels.
- **Solved / reconstructed here, not read from any record**: m, and the 09:25 spots in `D3`.
  The 09:25 spot is not unique — for NF it is the single-reversal solution on a 3-poll path;
  for BNF, ER = 1.000 only establishes that the path was monotone, so the value used is the
  degenerate member of a family and must not be read as an observation.
- **Fixture, not NSE**: the calendar in `_ia.py` treats every weekday as a session and has no
  holidays. Adequate for these tests, inadequate for anything about holidays.

## Known limits

- `D2`'s fixture has 30-minute gaps, so ER returns `null` / `SPARSE_PATH` there. That is an
  artifact of the chosen poll times, not a finding.
- `D1` case 5 surfaces reason code `SCALE_SCALE_INVALID` — a doubled prefix from
  `'SCALE_' + reason` in `compute_direction`. Cosmetic, but it matters to anything parsing
  reason codes.
- These diagnostics establish mechanisms. They measure no historical incidence and make no
  forecast-quality claim.
