# VIX regime history fix (1 October 2026)

## The defect

From 10 August 2026 the brain ranked the live India VIX against closes from
25 February to 29 June 2026. On 1 October at 13:00 IST it reported VIX 15.37
at the 9.76th percentile (regime LOW). Against the previous 60 session closes
(8 July to 30 September, range 10.29 to 14.56) the correct percentile was 100,
which is VERY_HIGH by the brain's own thresholds.

**Root cause.**

1. The relative VIX regime reads its history from `premium_history`.
2. That table's only writer was the PWA. MarketVivi commit `ccd43a6` removed
   it on 3 May 2026, and no Kotlin writer replaced it, so the table stopped at
   29 June.
3. Nothing checked the age of the history. The app also passed the values as
   an undated list (`vixHistory`), so the brain had no way to check.

**Other consumers of the same stale list:**

- `vix_z` in the market verdict (`synthesize_verdict`), which picks between
  BUY PREMIUM, SELL PREMIUM and WAIT;
- the C3 context-percentile windows;
- Kotlin `calculateIvPercentile`, a display and recording field.

## The fix

### History source (app)

The app already upserts every session's polls to `app_config` under the key
`poll_history_<date>`. These records run from 22 June 2026 and have a complete
close for every session since, so no backfill is needed.

`VixDailyHistory` takes each prior session's last poll between 09:15 and 15:30
IST that has a valid VIX. The result is a dated list, oldest first:
`{date, vix, t, polls, source}`.

- The list is fetched once per day at bootstrap.
- The fetch pages 5 rows at a time (one row is about 85 KB) and only reads
  keys from the last 100 calendar days, up to but not including today.
- If any page fails, the previous list is kept.
- The app sends it to the brain as `vixDailyHistory`.

### Acceptance and freshness (brain)

`_pc2_vix_daily_history` accepts a row only if all of these hold:

- the date is a valid date earlier than today;
- the date is a weekday that is not in `NSE_HOLIDAYS`;
- the VIX is above 0 and below 100;
- the close time is between 15:00 and 15:30.

It keeps one row per date. Rows older than the 60th NSE session before today
are dropped as `older_than_window`; without this, an idle gap would let a
"last 60 rows" window reach back months. Each rejected row is counted by
reason.

The whole series is **STALE** if more than two trading sessions separate the
newest close from today. When the session date is known, an undated list is
never trusted.

The result is cached in the ctx for each poll. The cache key holds the source
objects, their lengths and the policy constants, because the regime is
evaluated once per candidate.

### Fail closed

When the history is stale, undated or missing:

- the regime takes the existing neutral path (`NORMAL`, basis
  `neutral_stale_history`, `support_status = STALE_HISTORY`);
- `vix_z` falls back to its existing absolute bands;
- the C3 daily rows are dropped. `_fresh_daily_history_rows` applies the same
  session rule and window floor to the merged daily rows. `_history_values`
  also applies the rule to each series, so a fresh row of one variable cannot
  bring stale rows of another variable back into a window.

### Observability

- `pc2_vix_regime_context` is now `pc2_vix_regime_context_live_v2`. It carries
  the history status, source, oldest and newest dates, sessions behind, and
  rejection counts.
- The analyze result has a compact `vixRegime`, which the PWA displays.

### Absolute guards (knobs, off)

These constants are all `None` by default, so behaviour is unchanged until an
owner sets them:

- `VIX_REGIME_HIGH_ABS_FLOOR`
- `VIX_REGIME_VERY_HIGH_ABS_FLOOR`
- `VIX_REGIME_LOW_ABS_CEILING`

### PWA (MarketVivi)

- The headline is the brain's relative regime, with its percentile and the
  date of its newest close. The fixed 15/20/24 band is shown underneath as a
  labelled reference.
- When the history is stale or thin, the display says so and shows the regime
  as held neutral.
- If the brain result is stale, the display falls back to the band.
- Sigma badges are rounded to 2 decimals.

## Evidence

- Unit tests:
  - `tests/test_vix_regime_history_20261001.py`: 35 tests, including the real
    closes from 22 June to 30 September as a fixture and the findings from the
    round-1 review.
  - `VixDailyHistoryTest.kt`: 10 tests.
  - MarketVivi `tests/test_vix_regime_display.mjs`: 11 tests.
- The full Python suite passes: 1,139 tests, 2 skipped.
- Mutation checks: each of the following, reverted on purpose, fails at least
  one test:
  - reverting the regime to the undated list;
  - removing the staleness rule;
  - removing the generic row guard;
  - reverting `vix_z`;
  - accepting same-day rows;
  - accepting polls after 15:30;
  - accepting today's key;
  - removing the sigma rounding;
  - removing the support check in the PWA.

## Limits

- The 60-session percentile is relative. After a calm quarter, VIX 13.5 ranks
  VERY_HIGH. The absolute guards exist for this case; choosing their values is
  an owner decision.
- `vix_z` has the same relative property, and the guards do not cover it.
- The neutral fallback is the pre-existing missing-context behaviour:
  - Force 3 is +1 for every structure;
  - directional days use the low-IV strategy lists.

  A distinct UNKNOWN regime would be a separate policy change.
- `NSE_HOLIDAYS` covers only 2026. Without the 2027 list, the staleness count
  treats 2027 holidays as sessions. That errs towards STALE, which is the safe
  direction.
