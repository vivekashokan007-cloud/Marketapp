# b511 B4 — advice guard (2026-10-18)

Tests only; no production code changed. Real and Paper decisions are untouched.

Runtime coverage (`app/src/main/python/tests/test_b511_b4_advice_guard.py`, 5 tests):
* Real `analyze()` on the three recorded fixtures × modes paper/live/sandbox/missing: every
  watchlist, generated, ranked and below-cap candidate is a CREDIT/DEBIT/NEUTRAL spread type
  with ≥ 2 legs; the final strategy is a spread type or None; no buy/long CE/PE/call/put text.
* Final `BUY PREMIUM` only when the referenced top watchlist candidate is a debit spread with
  `entryEligible is True` (vacuous on these fixtures, enforced by the gate test below).
* Direction cannot authorise: fixture_b live and sandbox synthesise `BUY PREMIUM / BEAR_PUT`
  from direction; with no entry-eligible debit spread the final verdict is not BUY PREMIUM and
  the thesis survives only as `market_thesis` context.
* The real final gate `_align_verdict_to_watchlist(require_entry_eligible=True)`: eligible debit →
  BUY PREMIUM (positive path kept, nothing forced to WAIT); ineligible debit → WAIT with the
  standard reason; ineligible debit + eligible credit → SELL PREMIUM credit lane; direction-unsafe
  debit → WAIT.
* X1 isolation: a Paper X1 poll keeps `recommendation_authority: false`; research records never
  appear in verdict, watchlist, generated candidates or alerts.

PWA (`MarketVivi/tests/test_b511_b4_no_single_leg_advice.mjs`, 3 tests): the real b510 market-view
helpers never produce buy-call/buy-put advice for any direction × action; the real candidate-card
legs block renders BUY and SELL legs in equal numbers for debit, credit and 4-leg structures; a
bounded source scan finds no Buy CE/PE advice template in app.js (only the explicit disclaimer).

Findings: no Real behaviour defect found. Observation for scope review (not changed):
`_strategy_action` maps any non-debit type to `SELL PREMIUM`, so an unknown single-leg type
would be mislabelled rather than refused. It is unreachable today because generation emits only
the closed spread type set (asserted above).
