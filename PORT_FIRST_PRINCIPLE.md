# PORT_FIRST_PRINCIPLE (LOCKED 2026-05-02)

The aim of the SHIP2 migration is to make brain.py running natively in Kotlin via Chaquopy the sole source of truth, with PWA reduced to display-only. The end state requires that swapping the producer from JS to brain.py introduces no end-user-visible behavior change.

Therefore, every threshold, weight, formula coefficient, and decision rule in app.js for any function being ported gets transcribed into brain.py verbatim. Brain.py's output post-port must be numerically identical to JS output for the same inputs.

## What this means in practice:

- **No "fixing" thresholds** during port even if they look wrong
- **No "modernizing" formulas** even if cleaner equivalents exist
- **No applying Phase 10.A–H conflict resolutions** retroactively in earlier phases
- **No reinterpreting locked decision text** where the lock specifies a calibration change (the 0.5 in #27 is deferred per Item 3)
- **No new alert categories** not in JS today
- **No removing alert categories** that exist in JS today
- **No "while we're at it" cleanups** during port (Phase D v133 dead-code cleanup was the last allowable instance under build-blocker pressure; future phases halt and ask)

## Phase 10 cross-file conflicts (RATE, wall step, BNF breadth, near-ATM PCR scope, notification throttle):
These are NOT resolved retroactively during Phase E or any earlier phase. Each remains scheduled to its dedicated Phase 10 sub-sweep. If during Phase E port a JS-vs-brain.py conflict appears that's NOT in the existing 5-conflict table, log it to the table for resolution in the next Phase 10 sub-sweep. Do not resolve in Phase E. Port the JS-current value.

## Phase D Deviation 2 (wall step 200/100 in brain.py vs 100/50 in JS):
Already shipped, MD5-locked, 71 tests passing. Leave as-is. Going forward, the principle is that JS source wins on calibration ports. Paper-trade phase will surface whether wall step 200/100 breaks signal quality; if so, fix in Phase 10.B.4 dedicated sub-sweep.

## Compliance check at every code change in Phase E and beyond:
If your reasoning for a value is "I made it cleaner" or "I applied the documented fix" or "this matches brain.py internal consistency" — REJECT the change. Port the JS value.

## Documented exceptions:
None for Phase E. Any apparent "fix in JS" found during port → log to paper-trade backlog, port the JS-current value.
