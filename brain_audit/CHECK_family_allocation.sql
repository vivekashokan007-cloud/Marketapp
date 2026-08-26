-- =====================================================================================
-- CHECK_family_allocation.sql  ·  repeatable family-allocation / selector-quality check
-- Companion to brain_audit/FAMILY_ALLOCATION_REVIEW_20260825.md
--
-- WHAT IT DOES
--   Reproduces the family-allocation decomposition as a set of PASS/FAIL checks so it can
--   be re-run verbatim once real v5 (v2.6.0) data exists. Same basis as the review:
--   ml_brain_snapshots primary ⋈ ml_recommendation_outcomes, price_integrity='OK',
--   role in (primary,secondary), r_multiple normalized by 0.6×max_loss (M9.3).
--
-- HOW TO RUN
--   Edit ONLY the `params` CTE below, then run the whole file (Supabase SQL editor, or via
--   the Supabase MCP execute_sql tool). It returns one row per check with a verdict.
--
--   To grade the SHIPPED v2.6.0 selector:   sel_like = 'pc2_paper_primary_v5'
--   To reproduce the pre-fix baseline:       sel_like = '%'   (all selectors)
--   min_days gates the verdicts: below it, checks read INSUFFICIENT_DATA rather than lie.
--
-- ACCEPTANCE BARS (from the review, § "MEASUREMENT")
--   data_sufficiency ............ n_days >= min_days           (else all verdicts untrusted)
--   single_day_artifact ......... book mean R must stay same sign after dropping the best day
--                                 (project rule: reject if one day drives the result)
--   family_choice_vs_random ..... >= 0        (v5 goal: family choice no longer sub-random)
--   within_family_strike_skill .. >= +0.035   (the strike edge the sigma/net-edge selector adds)
--   no_trade_gate_size .......... informational: % of picks whose whole menu tops out <= 0R
--                                 (the size of the §6.1 conviction-gate opportunity)
--
-- INTERPRETATION
--   Two numbers decide whether v5 fixed anything: family_choice_vs_random (does it climb to
--   >= 0?) and within_family_strike_skill (does it hold >= +0.035?). If family choice is still
--   sub-random under v5, the lever is the conviction/no-trade gate, not the ranking key.
-- =====================================================================================

WITH params AS (
  SELECT
    DATE '2026-06-01'            AS min_date,   -- floor session_date
    'pc2_paper_primary_v5'       AS sel_like,   -- selector to grade; '%' = all (baseline)
    10                           AS min_days    -- trusted-verdict threshold (distinct days)
),
scope AS (   -- snapshots whose PRIMARY was chosen by the target selector, on/after min_date
  SELECT s.id AS snapshot_id, s.session_date
  FROM ml_brain_snapshots s, params p
  WHERE s.session_date >= p.min_date
    AND COALESCE(s.primary_candidate_json->>'pc2PaperSelectorVersion','') LIKE p.sel_like
),
menu AS (    -- that snapshot's realized menu (primary + alternatives)
  SELECT o.snapshot_id, o.session_date, o.strategy_type, o.r_multiple, (o.role='primary') AS is_primary
  FROM ml_recommendation_outcomes o
  JOIN scope sc ON sc.snapshot_id = o.snapshot_id
  WHERE o.price_integrity = 'OK' AND o.role IN ('primary','secondary') AND o.r_multiple IS NOT NULL
),
prim AS (SELECT * FROM menu WHERE is_primary),
fam  AS (SELECT snapshot_id, strategy_type, avg(r_multiple) AS fm, count(*) AS n FROM menu GROUP BY 1,2),
famq AS (SELECT * FROM fam WHERE n >= 3),                       -- families with real support
snapagg AS (SELECT snapshot_id, max(fm) AS best_fm, avg(fm) AS rand_fm,
                   count(*) AS fams FROM famq GROUP BY 1),
brainfam AS (   -- brain's achieved strike R + its chosen family's expected R, per snapshot
  SELECT p2.snapshot_id, avg(p2.r_multiple) AS achieved, max(f.fm) AS brain_fm
  FROM prim p2 JOIN famq f ON f.snapshot_id = p2.snapshot_id AND f.strategy_type = p2.strategy_type
  GROUP BY 1
),
decomp AS (   -- one row per decision snapshot with >=2 families available
  SELECT b.achieved, b.brain_fm, a.rand_fm, a.best_fm
  FROM brainfam b JOIN snapagg a ON a.snapshot_id = b.snapshot_id
  WHERE a.fams >= 2 AND b.brain_fm IS NOT NULL
),
daybook AS (SELECT session_date, sum(r_multiple) AS day_total FROM prim GROUP BY 1),
best_day AS (SELECT session_date FROM daybook ORDER BY day_total DESC NULLS LAST LIMIT 1),
scal AS (
  SELECT
    (SELECT count(*)                     FROM prim)                                       AS n_primaries,
    (SELECT count(DISTINCT session_date) FROM prim)                                       AS n_days,
    (SELECT avg(r_multiple)              FROM prim)                                       AS book_mean,
    (SELECT avg(r_multiple) FROM prim WHERE session_date <> (SELECT session_date FROM best_day)) AS book_mean_ex_best_day,
    (SELECT avg(brain_fm - rand_fm)      FROM decomp)                                     AS family_vs_random,
    (SELECT avg(achieved - brain_fm)     FROM decomp)                                     AS strike_skill,
    (SELECT 100.0*avg((best_fm <= 0)::int) FROM decomp)                                   AS pct_no_positive_family,
    (SELECT min_days FROM params)                                                         AS min_days,
    (SELECT sel_like FROM params)                                                         AS sel_like
)
SELECT ord, check_name, value, threshold, verdict FROM (
  SELECT 0 AS ord, 'selector_graded' AS check_name, sel_like AS value,
         'edit params.sel_like' AS threshold, 'INFO' AS verdict FROM scal
  UNION ALL
  SELECT 1, 'data_sufficiency', n_days::text || ' days · ' || n_primaries::text || ' primaries',
         '>= ' || min_days || ' days',
         CASE WHEN n_days >= min_days THEN 'PASS' ELSE 'INSUFFICIENT_DATA' END FROM scal
  UNION ALL
  SELECT 2, 'book_mean_R', round(book_mean::numeric,4)::text, 'positive is good',
         CASE WHEN book_mean IS NULL THEN 'NO_DATA'
              WHEN book_mean > 0 THEN 'POSITIVE' ELSE 'NEGATIVE' END FROM scal
  UNION ALL
  SELECT 3, 'single_day_artifact', 'ex-best-day mean = ' || round(book_mean_ex_best_day::numeric,4)::text,
         'sign survives dropping best day',
         CASE WHEN book_mean IS NULL OR book_mean <= 0 THEN 'N/A (book<=0)'
              WHEN book_mean_ex_best_day > 0 THEN 'PASS'
              ELSE 'FAIL (one-day artifact)' END FROM scal
  UNION ALL
  SELECT 4, 'family_choice_vs_random', round(family_vs_random::numeric,4)::text, '>= 0',
         CASE WHEN family_vs_random IS NULL THEN 'NO_DATA'
              WHEN family_vs_random >= 0 THEN 'PASS' ELSE 'FAIL (sub-random family choice)' END FROM scal
  UNION ALL
  SELECT 5, 'within_family_strike_skill', round(strike_skill::numeric,4)::text, '>= 0.035',
         CASE WHEN strike_skill IS NULL THEN 'NO_DATA'
              WHEN strike_skill >= 0.035 THEN 'PASS' ELSE 'WEAK' END FROM scal
  UNION ALL
  SELECT 6, 'no_trade_gate_size', round(pct_no_positive_family::numeric,1)::text || '% of picks',
         'menu best-family <= 0R (gate opportunity)', 'INFO' FROM scal
) q ORDER BY ord;
