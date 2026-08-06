import importlib.util
import os


def load_brain():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    brain_path = os.path.join(os.path.dirname(current_dir), "brain.py")
    spec = importlib.util.spec_from_file_location("brain", brain_path)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    return brain


def cand(cid, index="NF", lane="NF_intraday", is_credit=True, prob=0.50, max_profit=1000, max_loss=1000):
    return {
        "id": cid,
        "index": index,
        "lane": lane,
        "type": "BEAR_CALL" if is_credit else "BEAR_PUT",
        "isCredit": is_credit,
        "probProfit": prob,
        "maxProfit": max_profit,
        "maxLoss": max_loss,
        "premiumEdge": 0,
        "ev": 0,
        "varsityTier": "PRIMARY",
        "directionSafe": True,
        "capitalBlocked": False,
    }


def test_credit_ev_negative_released_under_shadow_a8():
    brain = load_brain()
    survivors, rejected, summary = brain._build3_apply_a8_ev_gate([
        cand("credit_bad", is_credit=True, prob=0.50, max_profit=1000, max_loss=1000)
    ])
    assert [c["id"] for c in survivors] == ["credit_bad"]
    assert len(rejected) == 1
    assert rejected[0]["rejection_stage"] == "ev_below_floor"
    assert rejected[0]["candidate_released_to_ranking"] is True
    assert summary["a8_gate_verdict"] == "PASS"
    assert summary["a8_gate_mode"] == "SHADOW_ONLY"
    assert summary["n_ev_below_floor_released_to_ranking"] == 1


def test_debit_ev_negative_released_under_shadow_a8():
    brain = load_brain()
    survivors, rejected, summary = brain._build3_apply_a8_ev_gate([
        cand("debit_bad", is_credit=False, prob=0.45, max_profit=1000, max_loss=1000)
    ])
    assert [c["id"] for c in survivors] == ["debit_bad"]
    assert len(rejected) == 1
    assert summary["n_ev_below_floor"] == 1


def test_credit_ev_positive_survives_a8():
    brain = load_brain()
    survivors, rejected, summary = brain._build3_apply_a8_ev_gate([
        cand("credit_good", is_credit=True, prob=0.70, max_profit=1000, max_loss=500)
    ])
    assert [c["id"] for c in survivors] == ["credit_good"]
    assert rejected == []
    assert summary["a8_gate_verdict"] == "PASS"


def test_all_negative_candidate_set_returns_wait_reason():
    brain = load_brain()
    survivors, _, summary = brain._build3_apply_a8_ev_gate([
        cand("bad1", prob=0.50, max_profit=1000, max_loss=1000),
        cand("bad2", prob=0.40, max_profit=1000, max_loss=1000),
    ])
    assert [c["id"] for c in survivors] == ["bad1", "bad2"]
    assert summary["a8_gate_verdict"] == "PASS"
    assert summary["a8_gate_reason"] == "EV_BELOW_FLOOR_SOFTENED_TO_RANKING"


def test_a8_ev_ratio_is_explicit_and_uses_unhaircuted_expected_values():
    brain = load_brain()
    raw_prob = (892 + 6442) / (11558 + 6442)
    rounded_payload_prob = round(raw_prob, 3)
    ab_676 = cand("ab_676", prob=rounded_payload_prob, max_profit=11558, max_loss=6442)
    ab_676["premiumEdge"] = round(raw_prob * ab_676["maxProfit"] - (1 - raw_prob) * ab_676["maxLoss"])
    survivors, rejected, summary = brain._build3_apply_a8_ev_gate([ab_676])
    assert survivors == [ab_676]
    assert rejected == []
    assert summary["a8_gate_reason"] == "NONE"
    assert ab_676["probProfit"] == 0.407
    assert ab_676["premiumEdge"] == 892
    assert ab_676["a8_expected_win"] == 4704.11
    assert ab_676["a8_expected_loss"] == 3820.11
    assert ab_676["a8_ev_floor"] == 4202.12
    assert ab_676["a8_ev_ratio"] == 1.2314
    assert ab_676["a8_pass"] is True


def test_ranking_missing_premium_edge_loses_to_edge_candidate():
    brain = load_brain()
    missing = cand("missing_edge", prob=0.70)
    missing.pop("premiumEdge")
    missing["ev"] = 999999
    with_edge = cand("with_edge", prob=0.50)
    with_edge["premiumEdge"] = -10
    ranked = brain.rank_candidates([missing, with_edge])
    assert [c["id"] for c in ranked] == ["with_edge", "missing_edge"]
    assert missing["premium_edge_status"] == "MISSING"
    assert with_edge["premium_edge_status"] == "OK"


def test_calm_regime_with_nf_survivor_removes_bnf_intraday():
    brain = load_brain()
    regime = {"type": "range", "sigma": 0.20}
    survivors, summary = brain._build3_apply_calm_nf_lane_gate(
        [
            cand("nf", index="NF", lane="NF_intraday"),
            cand("bnf", index="BNF", lane="BNF_intraday"),
        ],
        "intraday",
        regime,
        12,
    )
    assert [c["id"] for c in survivors] == ["nf"]
    assert summary["lane_gate_reason"] == "CALM_NF_LANE_RESTRICTION"
    assert summary["n_bnf_removed_by_calm_lane_gate"] == 1
    assert summary["n_removed_by_lane_gate"] == 1
    assert summary["removed_candidate_ids"] == ["bnf"]
    assert summary["removed_candidates"][0]["id"] == "bnf"
    assert summary["removed_by_lane"] == {"BNF_intraday": 1}


def test_calm_regime_with_only_bnf_falls_back_to_bnf():
    brain = load_brain()
    regime = {"type": "range", "sigma": 0.20}
    survivors, summary = brain._build3_apply_calm_nf_lane_gate(
        [cand("bnf", index="BNF", lane="BNF_intraday")],
        "intraday",
        regime,
        12,
    )
    assert [c["id"] for c in survivors] == ["bnf"]
    assert summary["lane_gate_reason"] == "CALM_BNF_ONLY_FALLBACK"
    assert summary["n_bnf_removed_by_calm_lane_gate"] == 0
    assert summary["n_removed_by_lane_gate"] == 0
    assert summary["removed_candidate_ids"] == []
    assert summary["removed_candidates"] == []
    assert summary["removed_by_family"] == {}


def test_non_calm_regime_does_not_lane_gate():
    brain = load_brain()
    regime = {"type": "trend", "sigma": 0.80}
    original = [
        cand("nf", index="NF", lane="NF_intraday"),
        cand("bnf", index="BNF", lane="BNF_intraday"),
    ]
    survivors, summary = brain._build3_apply_calm_nf_lane_gate(original, "intraday", regime, 22)
    assert [c["id"] for c in survivors] == ["nf", "bnf"]
    assert summary["lane_gate_reason"] == "NONE"


def test_build3_flow_reports_persistence_truncation():
    brain = load_brain()
    built = [cand(f"c{i}") for i in range(35)]
    persisted = built[:30]
    evidence = brain._build3_ranked_candidate_evidence(built, watchlist=built[:6])
    flow = brain._build3_candidate_flow_summary(
        built_candidates=built,
        rejected_candidates=[],
        old_ranked=built,
        a8_summary={"n_candidates_pre_a8": 35, "n_ev_below_floor": 0, "n_candidates_after_a8": 35},
        lane_summary={"lane_gate_reason": "NONE", "n_removed_by_lane_gate": 0, "n_candidates_after_lane_gate": 35},
        gated_candidates=built,
        ranked_final=built,
        watchlist=built[:6],
        persisted_generated=persisted,
        ranked_evidence=evidence,
    )
    assert flow["schema_version"] == "build3_flow_v1"
    assert flow["candidate_total_built"] == 35
    assert flow["ranked_before_persistence"] == 35
    assert flow["generated_persisted"] == 30
    assert flow["generated_candidate_cap"] == 30
    assert flow["ranked_evidence_persisted"] == 35
    assert flow["truncated_at_ranked_evidence"] == 0
    assert flow["truncated_at_persistence"] == 5


def test_ranked_candidate_evidence_extends_beyond_ui_cap():
    brain = load_brain()
    built = [cand(f"c{i}") for i in range(40)]
    evidence = brain._build3_ranked_candidate_evidence(built, watchlist=built[:2])
    assert len(evidence) == 40
    assert evidence[0]["rank"] == 1
    assert evidence[0]["watchlist_rank"] == 1
    assert evidence[31]["rank"] == 32
    assert evidence[31]["candidate_id"] == "c31"


def test_ranked_candidate_evidence_has_safety_cap():
    brain = load_brain()
    built = [cand(f"c{i}") for i in range(brain.BUILD3_RANKED_EVIDENCE_CAP + 5)]
    evidence = brain._build3_ranked_candidate_evidence(built)
    assert len(evidence) == brain.BUILD3_RANKED_EVIDENCE_CAP
    assert evidence[-1]["rank"] == brain.BUILD3_RANKED_EVIDENCE_CAP


def test_build3_flow_reports_capital_blocked_ranking_drop():
    brain = load_brain()
    open_cand = cand("open")
    blocked = cand("blocked")
    blocked["capitalBlocked"] = True
    flow = brain._build3_candidate_flow_summary(
        built_candidates=[open_cand, blocked],
        rejected_candidates=[],
        old_ranked=[open_cand],
        a8_summary={"n_candidates_pre_a8": 2, "n_ev_below_floor": 0, "n_candidates_after_a8": 2},
        lane_summary={"lane_gate_reason": "NONE", "n_removed_by_lane_gate": 0, "n_candidates_after_lane_gate": 2},
        gated_candidates=[open_cand, blocked],
        ranked_final=[open_cand],
        watchlist=[open_cand],
        persisted_generated=[open_cand],
    )
    assert flow["capital_blocked_before_build3_rank"] == 1
    assert flow["capital_blocked_after_lane_gate"] == 1
    assert flow["dropped_capital_at_build3_rank"] == 1
    assert flow["dropped_capital_at_final_rank"] == 1
    assert flow["unexplained_drop_at_build3_rank"] == 0
    assert flow["unexplained_drop_at_final_rank"] == 0


def test_old_picker_counterfactual_uses_original_pool_when_new_waits():
    brain = load_brain()
    old_ranked = [cand("old_bnf", index="BNF", lane="BNF_intraday")]
    payload = brain._build3_make_ab_payload(
        ctx={"tradeMode": "intraday", "today_ist": "2026-07-07"},
        latest_poll={"t": "09:20", "vix": 12},
        session_date="2026-07-07",
        poll_number=2,
        old_ranked=old_ranked,
        new_ranked=[],
        a8_summary={
            "a8_gate_reason": "NONE",
            "n_candidates_pre_a8": 1,
            "n_candidates_after_a8": 1,
            "n_ev_below_floor": 0,
        },
        lane_summary={
            "lane_gate_reason": "CALM_NF_ONLY_WAIT",
            "n_candidates_after_lane_gate": 0,
            "n_bnf_removed_by_calm_lane_gate": 1,
            "n_removed_by_lane_gate": 1,
            "n_nf_survivors_after_a8": 0,
            "vix": 12,
            "range_sigma": 0.2,
            "regime_type": "range",
        },
        new_verdict={"action": "WAIT"},
        original_count=1,
    )
    assert payload["old_pick_candidate_id"] == "old_bnf"
    assert payload["new_pick_candidate_id"] is None
    assert payload["old_would_have_taken"] is True
    assert payload["new_actor_verdict"] == "WAIT"
    assert payload["teacher_first_active"] is False


if __name__ == "__main__":
    test_credit_ev_negative_released_under_shadow_a8()
    test_debit_ev_negative_released_under_shadow_a8()
    test_credit_ev_positive_survives_a8()
    test_all_negative_candidate_set_returns_wait_reason()
    test_calm_regime_with_nf_survivor_removes_bnf_intraday()
    test_calm_regime_with_only_bnf_falls_back_to_bnf()
    test_non_calm_regime_does_not_lane_gate()
    test_build3_flow_reports_persistence_truncation()
    test_ranked_candidate_evidence_extends_beyond_ui_cap()
    test_ranked_candidate_evidence_has_safety_cap()
    test_build3_flow_reports_capital_blocked_ranking_drop()
    test_old_picker_counterfactual_uses_original_pool_when_new_waits()
    print("BUILD 3 A8/NF/AB TESTS: PASSED")
