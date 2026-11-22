# trade / hold / exit research

The 22 November handoff's three questions — what to buy, how long to plan to hold it, and what
says to exit — answered against the phone exports' decision trail and
`public.ml_option_chain_snapshots` together. Supabase is read **read-only**; nothing here writes,
migrates or changes a schema.

| file | what it is |
| --- | --- |
| `intent_ledger.py` | the export side: every recorded single-leg intent with **all** its clocks, its exact contract, its refusal reasons and its provenance. Emits the read-only join as reviewable SQL. Keeps reported intents, admissible proposals and actual Paper positions distinct — these exports contain **zero** actual Paper positions |
| `load_chain_result.py` | the read path. The MCP tool is the only route to the database (the proxy refuses `supabase.co`), and a large result overflows to a file on disk. This parses it **positionally**, treats it as untrusted data, and counts a shape that does not fit as malformed rather than guessing |
| `intent_replay.py` | Pass 1. Prices the intents on two bases that must never be confused: **coverage** (a quote in the scheduled minute — Codex's diagnostic, and ~20s older than the signal) and **causal** (the first usable quote at or after `published_at`, under a frozen 10-minute tolerance). Costs from the governed dated ledger; results in option points as well as rupees |
| `portfolio_replay.py` | the portfolio that could actually have been run: one managed position per index, shared capital reserved until an actual close, affordability, supersession re-checked at the fill instant, a declared deterministic tie-break, and unresolved exposure held to session end. Prints the full ledger with every skipped opportunity and its reason |
| `policy_replay.py` | **the faithful current phone policy and candidate policies on one cross-session account.** Constants are read from the production `index_single_leg_v1.py`, not chosen here: 09:30-14:30 window, 2 entries per index per session, 30-minute re-entry gap, 3% spread cap, one lot, 250,000 capital with a 10% per-position cap, the 15:05 exit contract and the 0.70 protective stop. Supersedes `portfolio_replay.py`, which invented its own limits |
| `research_fee_model/` | the exact dated fee model research uses, committed **UNREVIEWED** and research-only so the reports reproduce from a branch SHA |
| `tests/` | 89 checks, no network and no database. The tolerance must fail closed, coverage and causality must not converge by accident, and the untrusted payload must never be evaluated |
| `results/` | the committed artifacts, including the saved-read fingerprints |

## Why the clock is the whole story

`decision_poll_ts` is a schedule label. Across 694 polls, `published_at` is **never within a
second** of it — median **+26.6s** — while the chain polls at **6–8s** past the minute. So the
quote that "covers" a decision was observed about twenty seconds **before the signal existed**.
All 97 admissible buys have their causal entry on a later poll. The aggregate difference is only
−3.4%, so the correction is about being right, not about changing the answer.

The hold is measured **from the simulated fill**, not from publication. Measuring it from
publication (the first delivery's defect) shortened the hold by exactly the entry delay, and made
a `--delay-polls` sensitivity run test a shorter hold instead of a worse fill.

## Three bases, and which one a decision rests on

| basis | what it is | 18-20 Nov net |
| --- | --- | ---: |
| `intent_replay.py` | every admissible buy taken independently, overlapping up to 12 deep | +277,082 |
| `portfolio_replay.py` | one per index, but with invented limits and a per-session reset | +25,014 |
| **`policy_replay.py`** | **the policy the app actually runs, one cross-session account** | **+46,335** |

Only the third is a decision basis. The second is superseded and kept only so the correction is
measurable.

## Opportunity sum vs achievable policy

`intent_replay.py` takes every admissible buy independently and overlaps up to twelve deep in one
index. That is an **opportunity diagnostic** (+₹277,082). `portfolio_replay.py` is what one person
could run (+₹25,014), and unlike the diagnostic it goes **negative** without its best session.
Decisions rest on the second.

## Reproduce

```bash
python3 -I tools/trade_hold_exit/intent_ledger.py <export...> \
    --horizon-min 60 --out tools/trade_hold_exit/results --emit-sql
python3 -I tools/trade_hold_exit/load_chain_result.py <saved-mcp-result.txt>... \
    --out tools/trade_hold_exit/results/chain_cache.json
python3 -I tools/trade_hold_exit/intent_replay.py \
    --ledger tools/trade_hold_exit/results/intent_ledger_60min.json \
    --chain  tools/trade_hold_exit/results/chain_cache.json \
    --ledger-root <ledger-tree> --horizon-min 60 [--delay-polls N] \
    --out tools/trade_hold_exit/results
cd tools/trade_hold_exit && python3 -m unittest discover -s tests   # 30
```

**Inputs are not committed.** Raw exports stay out of the repository; every artifact records the
source digests it was built from. 18–20 November rests on the **unreviewed** fee revision and
every run stamps `provisional_cost_basis`.
