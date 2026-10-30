"""Complete keep / replace / demote gate map for the index_direction_v1 cutover (Delivery A).

Mechanism of the cutover: the legacy pipeline still executes (so X1 / EV v1.2 / evaluator evidence stays
byte-identical), but every decision-bearing output (verdict, per-index action, candidate set, Paper primary,
eligibility of authority candidates, watchlist authority, position contract) is re-derived from by_index[i]
in brain._ia_apply_cutover.  Rows marked DEMOTE / RETIRE_FROM_DECISION therefore still compute and are
recorded under legacy evidence, but cannot veto, rank or select.  KEEP rows are re-applied to authority
candidates by index_construction_v1 / brain._ia_candidate_from_construction.
"""

K, R, D, X, O = 'KEEP', 'REPLACE', 'DEMOTE', 'RETIRE_FROM_DECISION', 'OUT_OF_SCOPE_FLAGGED'

GATE_MAP = [
    # --- direction / regime / structure authority -------------------------------------------------
    {'gate': 'synthesize_verdict', 'where': 'brain.synthesize_verdict', 'decision': R, 'by': 'by_index[i] + legacy_view adapter', 'class': 'direction'},
    {'gate': 'compute_effective_bias', 'where': 'brain.compute_effective_bias', 'decision': X, 'by': 'index_direction_v1', 'class': 'direction'},
    {'gate': 'compute_morning_bias', 'where': 'brain.compute_morning_bias (BNF only)', 'decision': X, 'by': 'context only', 'class': 'direction'},
    {'gate': 'detect_regime/rangeSigma', 'where': 'brain.detect_regime', 'decision': X, 'by': 'index_regime_v1', 'class': 'regime'},
    {'gate': 'varsity_filter', 'where': 'brain._get_varsity_filter (incl. IV branches, VERY_HIGH and range overrides, default IB block)', 'decision': R, 'by': 'index_structure_policy_v1', 'class': 'structure'},
    {'gate': '_verdict_vix_z', 'where': 'brain._verdict_vix_z thesis VIX bands', 'decision': X, 'by': 'vol context only (used_for_blocking false)', 'class': 'structure'},
    {'gate': 'pc2_vix_regime_context', 'where': 'brain._pc2_vix_regime_context', 'decision': D, 'by': 'recorded vol_context', 'class': 'structure'},
    {'gate': 'learned_strategy_allow', 'where': 'brain._learned_branch_overrides strategy_allow/strategy_block', 'decision': D, 'by': 'recorded, not applied (D8)', 'class': 'learned'},
    {'gate': '_proposal_matches', 'where': 'brain._proposal_matches/_normalize_approved_branch_proposals', 'decision': D, 'by': 'recorded, not applied', 'class': 'learned'},
    {'gate': '_credit_sigma_limits', 'where': 'brain._credit_sigma_limits (learned sigma bounds)', 'decision': X, 'by': 'index_construction_v1 strikes', 'class': 'constructor'},
    # --- constructors -------------------------------------------------------------------------
    {'gate': 'width_ladder', 'where': 'brain._pc2_batch_f_width_ladder / NF_WIDTHS / BNF_WIDTHS', 'decision': R, 'by': 'index_construction_v1 default width (NF 150, BNF 400)', 'class': 'constructor'},
    {'gate': 'debit_range_budget', 'where': 'generate_candidates width > trade_sigma*1.2 (VIX-derived)', 'decision': X, 'by': 'none (no VIX width budget)', 'class': 'constructor'},
    {'gate': 'ic_sigma_spacing', 'where': 'generate_candidates IC sigma_multipliers/_daily_sigma(vix)', 'decision': R, 'by': 'index_construction_v1 IC geometry', 'class': 'constructor'},
    {'gate': 'ib_center_atm', 'where': 'generate_candidates IB at chain atm', 'decision': R, 'by': 'IB at parity K0', 'class': 'constructor'},
    {'gate': 'MIN_WIDTH_NF/MIN_WIDTH_BNF', 'where': '_CONST', 'decision': K, 'by': 'floors inside index_construction_v1', 'class': 'constructor'},
    {'gate': 'sigma_otm_too_close/far', 'where': '_build_candidate soft opportunity gates', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'credit_ratio_below_floor', 'where': '_build_candidate MIN_CREDIT_RATIO', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'iv_not_rich', 'where': '_build_candidate IV_RICH_MIN', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'credit_prob_below_floor', 'where': '_build_candidate MIN_PROB', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'debit_prob_below_floor', 'where': '_build_candidate prob < 0.40 (L14406)', 'decision': D, 'by': 'recorded; not applied to authority candidates', 'class': 'predictive'},
    {'gate': 'expected_win_1_10_floor', 'where': 'BUILD3 1.10x expected_win floor', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'credit_dte_below_floor', 'where': '_build_candidate MIN_CREDIT_DTE', 'decision': D, 'by': 'not applied to authority candidates; own-expiry day already UNAVAILABLE (flag for review)', 'class': 'time'},
    {'gate': 'gamma_block_swing', 'where': 'generate_candidates gammaRisk>=0.7 swing credit -> capitalBlocked', 'decision': D, 'by': 'not applied (model greek); flag for review', 'class': 'predictive'},
    {'gate': 'ic_ib_intraday_only', 'where': "generate_candidates trade_mode != 'swing' / mins_since_open < 300", 'decision': D, 'by': 'not applied; managed exits are intraday (no expiry hold); flag for review', 'class': 'time'},
    # --- eligibility ---------------------------------------------------------------------------
    {'gate': 'direction_unsafe', 'where': 'annotate_candidate_entry_eligibility directionSafe=f1>=0 (legacy bias)', 'decision': R, 'by': 'type in by_index[i].structure_policy.allowed', 'class': 'direction'},
    {'gate': '_get_forces', 'where': 'brain._get_forces/_assess_force1/_assess_force3', 'decision': X, 'by': 'recorded context', 'class': 'direction'},
    {'gate': 'capital_blocked', 'where': 'eligibility + max_loss <= capital x 10%', 'decision': K, 'by': 'index_construction_v1 budget check', 'class': 'capital'},
    {'gate': 'candidate_blocked', 'where': 'eligibility', 'decision': K, 'by': 'construction status', 'class': 'execution'},
    {'gate': 'execution_not_ready', 'where': 'eligibility', 'decision': K, 'by': 'construction status / quote completeness', 'class': 'execution'},
    {'gate': 'quote_incomplete', 'where': 'eligibility (two-sided bid/ask per leg; no LTP)', 'decision': K, 'by': 'QUOTE_INCOMPLETE', 'class': 'execution'},
    {'gate': 'friction_unavailable', 'where': 'eligibility', 'decision': K, 'by': '_candidate_decision_friction on authority candidate', 'class': 'execution'},
    {'gate': 'max_profit_not_positive', 'where': 'eligibility', 'decision': K, 'by': 'construction', 'class': 'execution'},
    {'gate': 'max_loss_not_positive', 'where': 'eligibility', 'decision': K, 'by': 'construction', 'class': 'execution'},
    {'gate': 'lot_unavailable', 'where': 'new', 'decision': K, 'by': 'chain lotSize metadata; no constant fallback for rupee economics', 'class': 'identity'},
    {'gate': 'expected_value_not_positive', 'where': 'eligibility netPremiumEdge', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'expected_value_missing', 'where': 'eligibility', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'ml_out_of_distribution', 'where': 'eligibility', 'decision': D, 'by': 'recorded', 'class': 'ml'},
    {'gate': 'ml_action_*', 'where': 'eligibility BLOCKED/SKIP/UNSURE/missing', 'decision': D, 'by': 'recorded', 'class': 'ml'},
    {'gate': 'ml_probability_missing/invalid', 'where': 'eligibility', 'decision': D, 'by': 'recorded', 'class': 'ml'},
    {'gate': 'entry_confidence_below_minimum', 'where': 'eligibility min(market_fit, p_ml*100)', 'decision': D, 'by': 'null, not_used_in_decision', 'class': 'ml'},
    {'gate': 'strategy_market_fit_unavailable', 'where': 'eligibility _neutral_structure_market_fit', 'decision': D, 'by': 'recorded', 'class': 'predictive'},
    {'gate': 'temporal_model_blend', 'where': 'mlTemporalActive', 'decision': D, 'by': 'recorded', 'class': 'ml'},
    # --- ranking / selection ------------------------------------------------------------------
    {'gate': 'sort_tuple', 'where': 'rank_candidates / _pc2_paper_primary_sort_components (directionSafe, netPremiumEdge, varsityTier, teacher*, contextPercentile, calibrationWinRate, forces, pMlEffective, ...)', 'decision': R, 'by': 'one candidate per allowed structure; no cross-structure/cross-index ranking', 'class': 'ranking'},
    {'gate': 'select_pc2_paper_primary', 'where': 'brain.select_pc2_paper_primary', 'decision': R, 'by': 'per-index action; managed admission gate', 'class': 'ranking'},
    {'gate': '_build3_rank_fingerprint', 'where': 'brain._build3_*', 'decision': D, 'by': 'recorded', 'class': 'ranking'},
    {'gate': '_align_verdict_to_watchlist/execution_candidate_index', 'where': 'alignment', 'decision': X, 'by': 'retired; per-index action', 'class': 'ranking'},
    {'gate': '_usable_calibration', 'where': 'calibration consumers L1662-1769, verdict L5890-6234', 'decision': X, 'by': 'no decision effect (verdict re-derived); per-index calibration key is future work', 'class': 'calibration'},
    # --- monitoring / display ------------------------------------------------------------------
    {'gate': 'update_watchlist_forces', 'where': 'brain.update_watchlist_forces', 'decision': D, 'by': 'legacy_forces_not_used + index_decision_id', 'class': 'monitor'},
    {'gate': 'position_verdict', 'where': 'brain.position_verdict', 'decision': K, 'by': 'existing positions keep original contract (legacy_combined_v0)', 'class': 'monitor'},
    {'gate': 'managed_invalidation', 'where': 'new', 'decision': K, 'by': 'index_managed_v1 exit contract only', 'class': 'monitor'},
    {'gate': 'entry_window', 'where': '09:20-15:15', 'decision': K, 'by': 'now gates the per-index action', 'class': 'time'},
    {'gate': 'daily_risk_state', 'where': 'brain._apply_daily_risk_state', 'decision': K, 'by': 'unchanged', 'class': 'capital'},
    {'gate': 'quote_freshness_v1', 'where': 'new (managed entries only)', 'decision': K, 'by': 'FRESHNESS_VERIFIED required for managed entry', 'class': 'execution'},
    {'gate': 'managed_cap_one_per_index', 'where': 'new (managed policy only)', 'decision': K, 'by': 'never applied to manual Paper', 'class': 'capital'},
    {'gate': 'evaluate_alerts/notifications', 'where': 'brain.evaluate_alerts / process_contract', 'decision': R, 'by': 'read the re-derived verdict (WAIT, PER_INDEX_AUTHORITY); per-index alert payloads are follow-up', 'class': 'display'},
    {'gate': 'X1 admission', 'where': 'paper_buying_research', 'decision': K, 'by': 'unchanged; runs before the authority', 'class': 'x1'},
    {'gate': 'implied_ev_v12_experiment', 'where': 'ev_v12_integration (opt-in evExperimentMode)', 'decision': O, 'by': 'unchanged; EV-based Paper experiment with its own engine positions; owner decision needed', 'class': 'experiment'},
]
