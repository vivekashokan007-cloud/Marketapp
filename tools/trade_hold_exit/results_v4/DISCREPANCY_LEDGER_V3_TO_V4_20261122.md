# Discrepancy ledger — policy replay v3 → v4 (D1, D2, D3)

**Scope.** The three defects Codex found in v3 (`CODEX_REPLY_CLAUDE_V3_REPLAY_FIXES_AND_NO_ENTRY_CUTOFF_20261122`,
reviewing `88533fa`), with each fix's behavioural effect stated separately from its monetary
effect, and each proven by a mutation: the test is run against the unfixed behaviour to show it
fails, because a test that passes both ways proves nothing.

`results_v3/` is **immutable and superseded**; the v3 source is retrievable at `88533fa`. v4 has
its own version string
`trade_hold_exit_policy_replay_v4_contract_identity_reservation_ttl_obs_identity`.

Reproduce:

```bash
export THE_LEDGER_ROOT=<dated cost-ledger tree>
python3 -I tools/trade_hold_exit/policy_replay_v4.py \
    --ledger tools/trade_hold_exit/results/intent_ledger_v2_60min.json \
    --chain  tools/trade_hold_exit/results/chain_cache_v2.json \
    --ledger-root "$THE_LEDGER_ROOT" --production-root app/src/main/python \
    --policy CURRENT_PHONE_POLICY --out tools/trade_hold_exit/results_v4
python3 -I -m unittest discover -s tools/trade_hold_exit/tests -p "test_*.py"   # 143 OK
python3 -I -m unittest discover -s tools/premium_model_validation/tests          # 240 OK, 25 skipped
```

## Reconciliation: v4 is numerically identical to v3

| | v3 | v4 |
| --- | --- | --- |
| Net realised | +₹46,335.4019 | +₹46,335.4019 |
| Closed positions | 6 | 6 |
| Exit reasons | TIME_EXIT 5, STOP_PLANNED 1 | TIME_EXIT 5, STOP_PLANNED 1 |
| Refusals | SELL 93, cap 91, window 42 | SELL 93, cap 91, window 42 |
| Unresolved at study end | 0 | 0 |
| Fees paid | ₹545.35 | ₹545.35 |
| Capital-required denominator | ₹72,785.29 | ₹72,785.29 |
| Ledger rows | 244 | 244 |

Checked field by field, not by eye: every pooled and `account_end` field is equal, the 244 ledger
rows are set-identical (`only v3: 0, only v4: 0`), and `f1_confirmation_evidence` is unchanged.
**All three defects are latent on this sample**, for reasons established from the data rather than
assumed — see each section.

## Inputs changed, and why that did not move the numbers

**Chain cache v2 is a strict superset.** The expiry-keyed cache was built from a fresh read
(`ml_option_chain_snapshots`, read-only) with `expiry` in the projection. All 67 v1 series are
present and **byte-identical**; 11 contracts are new, being the opposite option type on strikes
where the v1 read had captured only one side. 78 contracts / 6,024 quotes versus 67 / 5,170. None
of the added contracts is referenced by an admitted intent, so nothing moved.

**Ledger v2 adds one field.** `intent_ledger` now carries `source_poll_ts` on every authority
decision. Verified: 232 intents byte-identical, 466 decisions with every pre-existing field
identical, `source_poll_ts` present on 466 of 466, and the only new key is that one.

## D1 — wrong-expiry quote could close a carried position

**What was wrong.** The quote cache was keyed `session|index|strike|option_type` and `monitor()`
resolved a position's series from that. NSE lists one strike on several expiries, so a quote from
another series could price or close a position. My v3 test named for an expiry change in fact
varied the **index**, so it never tested expiry at all — Codex caught that, and it was correct.

**What v4 does.** The cache key is `session|index|expiry|strike|option_type`; a position saves its
contract identity at the fill (`index|expiry|strike|option_type`) and every later quote resolution
goes through that identity. The loader takes a **declared** `--csv-schema` instead of inferring the
shape from the field count — the expiry-bearing record and the old `…,bid,ask,ltp` record are both
six fields, so inference would silently read an expiry as an LTP or the reverse. A quote with no
expiry from either the schema or its row group is malformed, never keyed on a guess. The cache was
made expiry-aware **before** any further chain history was fetched, as instructed.

**Monetary effect: zero, and the reason is in the data.** For 18–20 November the database holds
exactly one expiry per session and index:

| session | index | distinct expiries | expiry | rows |
| --- | --- | --- | --- | --- |
| 2026-11-18 | BNF / NF | 1 / 1 | 2026-11-23 | 30,096 / 24,016 |
| 2026-11-19 | BNF / NF | 1 / 1 | 2026-11-23 | 30,888 / 24,648 |
| 2026-11-20 | BNF / NF | 1 / 1 | 2026-11-23 | 32,802 / 24,332 |

and the v1 cache recorded zero duplicate stamps, so no series had ever been merged. The collision
was real in the code and could not fire on this data.

**Proven by mutation.** With the expiry ignored again, all three D1 tests fail, and the decisive
one fails in exactly the predicted way: the position closes on the **other expiry's** 09:20:07
quote instead of its own contract's 09:21:07 quote.

Tests: identical index/strike/side with two expiries leaves the position unresolved; its own later
eligible quote closes it exactly once; an entry whose own expiry has no series is refused; the
separate cross-index test is retained.

## D2 — a pending entry could block the next session or accept a late quote

**What was wrong.** Two faults in one lifecycle. A reservation from an earlier session was skipped
with `continue`, so it never expired and its index slot stayed occupied for the rest of the study.
And the deadline was consulted only inside the `quote is None` branch, so the first matching quote
filled however late it arrived.

**What v4 does.** `expire_reservations()` runs **before any quote lookup** and expires a
reservation at its declared quote deadline or the end of its entry session, whichever comes first,
releasing the index slot either way, with distinct reasons
(`REFUSED_RESERVATION_QUOTE_DEADLINE_PASSED`, `REFUSED_RESERVATION_ENTRY_SESSION_ENDED`). Decision
identity is revalidated at the fill (`REFUSED_DECISION_IDENTITY_REVALIDATION_FAILED`). A **filled**
position still carries across the rollover with its own saved exit plan — only pending entries die
at the boundary.

**Declared convention.** The deadline is `published_at + tolerance_min` and it is **inclusive**: a
quote stamped exactly at the deadline fills, one second later does not. `tolerance_min` is the
frozen 10-minute, two-poll tolerance, not a parameter tuned here. All of this is published in the
result under `reservation_policy`.

**Monetary effect: zero.** No reservation on this sample outlived its session or its deadline.

**Proven by mutation.** Under the v3 lifecycle, 4 of the 6 D2 tests fail. The two that still pass
are the inclusive-boundary case and the filled-position carry-over — both guard against
*over*-correction and are expected to pass either way.

One fixture of mine was weak and is fixed: the first version used an empty quote series, which is
refused at reservation time as `NO_SERIES`, so no reservation was ever created and nothing could
be blocked. The fixture now has the contract quote **earlier** in the day with the decision
publishing as the session's last event, which creates a real reservation that then never fills.

## D3 — duplicate publications could trigger an early exit

**What was wrong.** v3 passed every published direction value straight to
`contrary_confirmation`. Production's persisted exit contract
(`index_single_leg_v1.exit_contract`) declares `observation_order: 'published_at'` and
`observation_identity: 'index|source_poll_ts'`, so several publications derived from **one** source
poll are **one** observation. Two publications of a single opposite poll would therefore have
counted as two consecutive opposite observations and exited a position on one poll's worth of
evidence.

**What v4 does.** `intent_ledger` carries `source_poll_ts` (production's own `decision_poll_ts`
is that source-poll label); observations are ordered by `published_at` and collapsed by
`(index, source_poll_ts)`, the latest publication of a poll winning, with availability preserved
and a documented fallback (`index|published_at|decision_id`) when a source poll is absent. The
result publishes the counts under `observation_identity`.

**Monetary effect: zero, measured.** Across all 694 authority history rows in the seven exports
there are **zero** repeated source polls, and this run reports `rows_in: 466, rows_out: 466,
collapsed: 0, no_source_poll: 0`.

**Proven by mutation.** With identity per publication instead of per source poll, the decisive
test fails with exactly the false exit Codex predicted:
`INVALIDATION_OPPOSITE_CONFIRMED` where the correct outcome is the hold deadline.

Tests: two publications from one opposite poll do not confirm; two genuinely distinct eligible
opposite polls do; a publication that has not published at the replay clock never counts; and when
two publications of one poll disagree, the later one wins and alone cannot confirm.

## Fee memo arithmetic, corrected

Codex is right and I was wrong. `1e7 × 1e-9 × 1.18` is **₹0.0118 per ₹1 crore** of turnover, not
the ₹0.23 I wrote — an error of about 20×, in the one line of that section I had not computed from
the data. The ₹0.00022668 on ₹192,117 was correct. Both committed documents are corrected in place
with the error named rather than quietly edited.

The IPFT double-count itself is unchanged: production uses the combined ₹3,553/crore exchange rate,
which per NSE/FA/73061 already contains the ₹0.01/crore IPFT, and then adds IPFT again. The fee
basis stays **provisional** pending dated source verification, and any production fee change needs
its own review. Nothing in production was touched.

## One correction to my own specification

My first version of the loader's docstring said the schema must contain `expiry`, while the code
accepts an expiry declared once per row group — which is what `group by session_date, index_key,
expiry` produces and is the better contract. My own test caught the contradiction. The documented
contract now matches the code: an expiry must reach every quote from the schema **or** the row
group, and a quote with neither is malformed.

## What this does not establish

- Still no independent reproduction. Codex has not reproduced ₹46,335, the six positions, or the
  383-test run.
- The cost basis is UNREVIEWED and provisional, so no rupee figure is final.
- **D1, D2 and D3 are all inert on this sample.** They are proven on constructed fixtures and by
  mutation. Their behaviour on real multi-expiry chains, real carried reservations and real
  duplicate publications is untested against data, because this sample contains none of the three.
- Three sessions and six positions still cannot carry a strategy conclusion, and the
  "current policy is best" claim remains withdrawn.
- The no-cutoff policy evaluation has **not** started; this ledger covers only the defect fixes
  that had to land first.
