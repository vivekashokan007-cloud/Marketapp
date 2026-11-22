# Paper pipeline corrections — review delivery, 22 November 2026 IST

Base: Marketapp `9f440676e6c3730fc163a62b8bcd81bd142c9b56`; MarketVivi `216a7cefd52af86bf0eec355ef683f0161a87ac9` (b534). Runtime versions remain b534 until candidate packaging. This review is not a release approval.

## Implementation

- A: preflight exact BUY quotes with request identities and original provider/client clocks, freshness expiry and named fetch/parse/validation failures. This is read-only. Tap admission always fetches again and retains native identity/config probes, lock, deadline, shared exposure cap and restart reconciliation. Store failures preserve known parse reasons in the durable request outcome.
- B: a versioned permitted-proposal view records WAIT for the uncovered SELL branch; raw action and ER remain unchanged for frozen research. Cards explicitly show WAIT/blocked and the original excluded action. BUY alerts carry the decision and instrument IDs. The independent proposal-evidence ledger retains the intended exact tuple and subsequent observations from that index's available acquired chain, with per-field absent/null/supplied status. Missing exact tuples stay missing. It is research proxy evidence, never a fill.
- C: successful raw chains persist before unrelated returns. Failed chains explicitly replace inherited chain context; the valid other index continues. Empty/one-sided chains invalidate their index. Direction does not depend on auxiliary breadth; missing BNF breadth is explicitly unavailable, not a zero breadth observation. Shared index/VIX quote failures invalidate entry reads and cached refresh inputs until successful publication. The poll's finally path enqueues open-position monitoring even if fetch, analysis or publication failed.
- Holding: new positions persist an admission-time anchor, same-session deadline and policy version; old positions retain their own saved exit contract. Both cards explain the current 15:05/30%-BUY-stop/two-opposite-observations policy. No new 30/60/120-minute or overnight economic rule is activated. Exit events are traced; the service requests an actionable Trade notification for pending and completed exits, including the recorded simulated result where available.

## Budgets and clocks

Readiness: <=1 batched GET/offer, <=2 exact keys, <=2 attempts/identity, >=15 seconds between attempts, <=90 GETs/IST session, <=64 KiB state. No candidate request starts while an open/pending position requires exit priority. A separate one-slot readiness worker prevents queued candidate requests from blocking the exit worker. Existing transport bounds: 4-second connect timeout, 5-second socket read timeout, 512 KiB body. These socket timeouts are not a strict whole-call deadline. The existing 10-second quote-latency validity gate still rejects over-budget quotes. Runtime duration is retained per completion. No readiness request can admit anything.

Proposal ledger: <=32 contract identities/session, <=1 MiB/session, ten retained session files. Coverage, missing tuples and capacity drops are explicit; the export adds the optional ledger without displacing established parts. Receipts are named `receipt_ms`; the ledger keeps provider timestamp fields under their supplied names. It does not invent exchange timestamps, missing sizes, IV, Greeks, prices or fills.

Trace: `mr.single_leg_trace.v1`, existing 384 KiB/session and seven-session retention. New allow-listed records: `N_READINESS`, `N_QUOTE_RESULT`, `N_ADMISSION_PENDING`, `N_EXIT`; existing submit/quote-start/outcome/recovery remain. Numeric fields are restricted to HTTP status and named request/receipt/runtime clocks. Global writer lock serializes bridge/worker trace writers. No account data, credentials, capital amounts or raw quote responses enter this trace.

## Managed spreads: proposed gate review only

The ordinary chain has no verified exchange quote clock. A dedicated broker snapshot may support a distinct, disclosed `BROKER_SNAPSHOT` freshness contract for exact legs, retaining provider generation and request/receipt clocks. It cannot satisfy a gate requiring verified exchange event time. A future change needs versioned leg identity, quantities, per-leg age, coherent package timing and protected entry/partial-fill/exit behavior. The existing `EXCHANGE_TIMESTAMP_MISSING` spread gate is unchanged by this delivery.

## Verification

Commands (repository roots):

```sh
PYTHONPATH=app/src/main/python:app/src/main/python/tests python3 -m unittest test_pipeline_readiness_20261122 test_single_leg_r7_admission_monitor_snapshot test_single_leg_r8_capital_state_bound test_single_leg_store_e2e_x1_identity test_index_single_leg_v1_delivery_b test_index_authority_v1_delivery_a test_expiry_day_scale_authority test_g0_session_evidence_exact
python3 tools/pipeline_review/demo.py /tmp/pipeline_lifecycles.json
MARKETAPP_DIR=$PWD MARKETVIVI_DIR=../MarketVivi node tools/paired_integration/paired_check.mjs /tmp/paired.json
node tests/test_pipeline_readiness_20261122.mjs
```

The demo uses actual Python quote parsing, admission, persistence, a separate OS process for restart and time exit, and repeats the monitor to assert close-at-most-once for both NF and BNF. Its quotes and clocks are synthetic. The paired JavaScript check uses the real PWA/Python and an admission-coordinator stand-in, so it is not evidence that Kotlin compiled or ran on a handset. JVM tests include named HTTP/timeout/body failure cases in `PipelineQuoteFailuresTest` and existing paused-transport tests in `SingleLegAdmissionCoordinatorTest`.

## Candidate phone checklist

1. Verify the paired candidate identity and source pins, preserved data, opt-in, configured capital and Real disabled. Manual Paper remains accessible without an unrelated morning-baseline display gate.
2. For each naturally eligible NF/BNF BUY: export the displayed decision ID, acquisition/source poll, exact contract, readiness request/result and quote clock/basis. Show pending, failure and expired states truthfully. Do not loosen rules to manufacture an eligible trade.
3. Tap once; retain request ID, native first-read identity, exact fresh quote response/validation, lock-time revalidation and final admission/rejection. Fetch failure must retain its reason. Repeat a stale tap and toggle/config change during a request to confirm rejection.
4. Background/restart: show the same pending request or admitted position, elapsed hold and saved deadline. Old positions retain old exit rules through an upgrade.
5. When an exit arises naturally: record the position's exact quote, pending versus actual simulated close, notification delivery and recorded costs/net outcome. Repeated callbacks must not close twice.
6. Induce per-index and shared-input failures in a controlled test build; verify the valid index continues, dependent entries invalidate, raw evidence survives and existing-position monitoring continues.
7. Measure handset poll duration, network use, retained bytes and background behavior. Host timings do not establish handset impact. A real broker request and actual phone lifecycle remain mandatory before live-path acceptance.

Claude's economic experiment continues separately. No profitability or overnight-readiness claim follows from these software checks.
