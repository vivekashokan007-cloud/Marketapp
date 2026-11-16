# Premium follow-on preparation — Stages C, G and D

**Branch** `review/claude-premium-followon-prep-20261116`, cut from the verified R7 commit
`124d62ee027941e43decc737e53d202c32b1d214` (the head of
`review/claude-spot-path-baseline-20261113`).

Offline research tooling only. Nothing here collects, trades, writes to a database, changes a
frozen rule, or touches the installed b530, Paper controls, v7 ranking, gates, sizing, exits,
teacher fee or notifications. Every tool is read-only with respect to its inputs.

## What is here

| file | stage | what it does |
| --- | --- | --- |
| `input_inventory.py` | C.1 | inventories session exports and spot histories by index, date, clock, expiry coverage, source and checksum, and labels every date `INSPECTED` / `INDETERMINATE_NOT_RESERVABLE` / `SYNTHETIC_NOT_A_SESSION` / `RESERVED_PROSPECTIVE` |
| `PROTOCOL_C_RV_SCALE_v1.md` | C.2 | the versioned protocol: E0 against one recent realised-variance scale — lookback, clocks, causal inputs, warm-up, exclusions, chronological split, metrics, and the input-support verdict for HAR-RV and implied variance |
| `rv_scale_protocol.py` | C.2 | the runnable scaffold: per-index, per-session CRPS / median MAE / coverage, reusing the frozen `rv_scale` and `build_namespace`, with a self-check that needs no inputs and a cross-check against the pinned `analyze.py` scorer |
| `SPEC_G_NEXT_SESSION_MATCH_v1.md` | G | the next-session matching specification |
| `next_session_match.py` | G | identity, the two clocks, close and open marks, post-open marks, the calendar read from the app's own holiday list, and every declared refusal |
| `SPEC_D_COST_LEDGER_v1.md` | D | the cost-ledger specification |
| `cost_ledger.py` | D | ask-to-enter / bid-to-exit with a declared fill delay and slippage, the dated fee model, the capture's own lot authority, net ₹, risk-budget return, downside, coverage, and WAIT at exactly zero |
| `tests/` | all | 87 synthetic checks, no network and no real-data dependency |

## Run it

```bash
# Stage C
python3 -I tools/premium_followon/input_inventory.py <dir-or-file> [...] [--json OUT]
python3 -I tools/premium_followon/rv_scale_protocol.py --self-check
python3 -I tools/premium_followon/rv_scale_protocol.py --handoff-dir <13 Nov handoff> --json OUT

# Stage G
python3 -I tools/premium_followon/next_session_match.py <close.json> <next.json> --index NF

# Stage D
python3 -I tools/premium_followon/cost_ledger.py --self-check
python3 -I tools/premium_followon/cost_ledger.py <export.json> --index NF --hold-min 60

# every check
python3 -I -W error::ResourceWarning -m unittest discover \
    -s tools/premium_followon/tests -p "test_*.py"
```

## What the frozen tools are, and that they are unchanged

Imported, never modified: `tools/spot_path_baseline/rv_scale_compare.py` (`rv_scale`,
`build_namespace`), the pinned `reproduction/analyze.py` scorer it builds, and
`research/replay/fee_models.py` (`fees_v2`, `gross_inr`, `UnknownHistoricalRate`). Also
untouched: `premium_repricing_v2.py`, `premium_60min_compare.py`, `spot_path_baseline.py`,
`preflight_export.py`, `test_repricing_fixes.py`, `PLAN_PREMIUM_FOLLOWUP_20261113.md`
(sha256 `ca70006a…`) and `RULES_FROZEN.md` (sha256 `e021fb78…`), both verified at this base.

## The three blockers these tools measured

1. **No reservable session exists offline.** 13 dates, all `INSPECTED`; the one future-dated
   file is synthetic and labelled as such. So Protocol C ran as a development dry run only.
2. **No Stage G pair can be formed.** The only premium session stops capturing at 13:40/13:45,
   so it has no close mark in 15:10–15:30, and the next session has no premium capture.
3. **No Stage D row can be scored.** Every 12 November envelope carries an unresolved producer
   lot claim, and the dated fee table is verified only through 2026-10-09.

Each is reported with the exact evidence needed, in the stage's own document.
