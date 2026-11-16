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
| `SPEC_G_NEXT_SESSION_MATCH_v2.md` | G | the matching specification, the readiness correction and the research capture path |
| `next_session_match.py` | G | explicit identity, session-aligned receipts, agreeing exchange-clock fields, close and open marks with a declared tolerance, post-open movement reported apart, the calendar read from the app's own holiday list, and every declared refusal |
| `SPEC_D_COST_LEDGER_v2.md` | D | the cost-ledger specification, with the corrected lot path and the measured timing bounds |
| `cost_ledger.py` | D | ask-to-enter / bid-to-exit, receipt-clock timing bounds, executable-price and tick checks, the production lot authority, net ₹, risk-budget return, downside, coverage, every blocking reason, and WAIT at exactly zero |
| `FEE_SOURCE_AUDIT_20261116.md` | D | the dated fee table audited against its primary sources: four components confirmed, two outstanding, one conditional line proposed |
| `tests/` | all | 115 Python checks, no network and no real-data dependency |

Beside them, in the app's own tree:

| file | what it does |
| --- | --- |
| `app/src/main/java/com/marketradar/app/PremiumFollowOnCapture.kt` | the separately identified research capture: its own 15:10-15:30 and 09:15-09:45 windows, its own store root, a durable carry so the next session pins yesterday's contracts, the production lot authority written into its own tree, and its own export key |
| `app/src/test/java/com/marketradar/app/PremiumFollowOnCaptureTest.kt` | 13 JVM checks, including that the installed b530 window cannot reach the close |
| `app/src/test/resources/premium_capture/followon_export_2026-10-{16,19}.json` | that capture's OWN exports, which the Python matcher pairs in CI |

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
    -s tools/premium_followon/tests -p "test_*.py"          # 115 OK

# the Kotlin side, on a plain JVM (no Android SDK): PremiumCaptureTest +
# PremiumFollowOnCaptureTest
run_premium_tests.sh app/src/test/java/com/marketradar/app/PremiumFollowOnCaptureTest.kt
```

## What the frozen tools are, and that they are unchanged

Imported, never modified: `tools/spot_path_baseline/rv_scale_compare.py` (`rv_scale`,
`build_namespace`), the pinned `reproduction/analyze.py` scorer it builds, and
`research/replay/fee_models.py` (`fees_v2`, `gross_inr`, `UnknownHistoricalRate`). Also
untouched: `premium_repricing_v2.py`, `premium_60min_compare.py`, `spot_path_baseline.py`,
`preflight_export.py`, `test_repricing_fixes.py`, `PLAN_PREMIUM_FOLLOWUP_20261113.md`
(sha256 `ca70006a…`) and `RULES_FROZEN.md` (sha256 `e021fb78…`), both verified at this base.

## What these tools measured

1. **No reservable session exists offline.** 13 dates, all `INSPECTED`; the one future-dated
   file is synthetic and labelled as such. So Protocol C ran as a development dry run only.
2. **No Stage G pair can be formed from existing evidence, and the installed build cannot
   produce one.** b530 captures `[09:20, 15:05)`, so its last slot is 15:00 and the close mark
   it needs is 15:10-15:30. The capture path that can is prepared here, and its own exports are
   paired end to end in CI.
3. **Stage D coverage is zero for exactly one reason: the fee window.** The lot authority
   resolves for both indices (NIFTY 65, BANKNIFTY 30) once read from the production authority
   record rather than the per-slot claim, so 240 NIFTY and 244 BANKNIFTY rows now carry the fee
   window as their only refusal. Four of the six fee components are confirmed unchanged against
   the exchange's own page; two are outstanding.

Each is reported with the exact evidence needed, in the stage's own document.
