import importlib.util
import os


def load_brain():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    brain_path = os.path.join(os.path.dirname(current_dir), "brain.py")
    spec = importlib.util.spec_from_file_location("brain", brain_path)
    brain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain)
    return brain


def candidate(cid, gross_edge, net_edge, gross_prob=0.75, net_prob=0.60):
    return {
        "id": cid,
        "index": "BNF",
        "lane": "BNF_intraday",
        "type": "IRON_BUTTERFLY",
        "isCredit": True,
        "probProfit": gross_prob,
        "maxProfit": 1200,
        "maxLoss": 8800,
        "premiumEdge": gross_edge,
        "netProbProfit": net_prob,
        "netMaxProfitAfterFriction": 900,
        "netMaxLossAfterFriction": 9100,
        "netPremiumEdge": net_edge,
        "frictionCost": 300,
        "varsityTier": "PRIMARY",
        "directionSafe": True,
        "capitalBlocked": False,
        "executionReady": True,
        "p_ml": 0.80,
        "mlAction": "TAKE",
        "forces": {"aligned": 0, "against": 0},
        "contextScore": 0,
        "brainScore": 0,
        "gammaRisk": 0,
        "wallScore": 0,
    }


def test_ranker_prefers_positive_net_edge_over_higher_gross_edge():
    brain = load_brain()
    gross_winner_net_loser = candidate("gross_only_winner", gross_edge=1000, net_edge=-250)
    lower_gross_net_winner = candidate("net_winner", gross_edge=100, net_edge=150)

    ranked = brain.rank_candidates([gross_winner_net_loser, lower_gross_net_winner])

    assert [row["id"] for row in ranked] == ["net_winner", "gross_only_winner"]
    assert ranked[0]["rankEconomicsBasis"] == "NET_AFTER_TEACHER_FRICTION"
    assert ranked[0]["rankEdgeScale"] == "net_premium_edge_per_net_max_loss"


def test_entry_eligibility_fails_gross_positive_but_net_negative():
    brain = load_brain()
    row = brain.annotate_candidate_entry_eligibility(
        candidate("gross_positive_net_negative", gross_edge=750, net_edge=-1),
        80,
    )

    assert row["entryGate"] == "MONITOR"
    assert "expected_value_not_positive" in row["entryEligibility"]["reasons"]
    assert row["entryEligibility"]["gross_premium_edge"] == 750
    assert row["entryEligibility"]["net_premium_edge"] == -1
    assert row["entryEligibility"]["economics_contract"].startswith("netPremiumEdge must be present")


def test_live_net_candidate_without_net_edge_fails_closed():
    brain = load_brain()
    row = candidate("net_missing", gross_edge=750, net_edge=50)
    row["netEconomicsVersion"] = brain.NET_ECONOMICS_VERSION
    row.pop("netPremiumEdge")
    row.pop("netMaxProfitAfterFriction")
    row.pop("netMaxLossAfterFriction")

    ranked_edge = brain._candidate_rank_edge(row)
    gated = brain.annotate_candidate_entry_eligibility(row, 80)

    assert ranked_edge["basis"] == "NET_UNAVAILABLE_FAIL_CLOSED"
    assert ranked_edge["status"] == "MISSING_NET"
    assert gated["entryGate"] == "MONITOR"
    assert "expected_value_missing" in gated["entryEligibility"]["reasons"]
    assert "max_profit_not_positive" in gated["entryEligibility"]["reasons"]
    assert "max_loss_not_positive" in gated["entryEligibility"]["reasons"]
    assert gated["entryEligibility"]["gross_premium_edge"] == 750


def test_build3_ev_uses_net_values_when_present():
    brain = load_brain()
    row = candidate("net_build3", gross_edge=2000, net_edge=-100, gross_prob=0.95, net_prob=0.10)

    metrics = brain._build3_candidate_ev(row)

    assert metrics["basis"] == "NET_AFTER_TEACHER_FRICTION"
    assert metrics["expected_win"] == 90.0
    assert metrics["expected_loss"] == 8190.0
    assert metrics["passes"] is False


def test_build3_ev_fails_closed_when_live_net_values_missing():
    brain = load_brain()
    row = candidate("net_build3_missing", gross_edge=2000, net_edge=100, gross_prob=0.95, net_prob=0.10)
    row["netEconomicsVersion"] = brain.NET_ECONOMICS_VERSION
    row.pop("netProbProfit")

    metrics = brain._build3_candidate_ev(row)

    assert metrics["basis"] == "NET_UNAVAILABLE_FAIL_CLOSED"
    assert metrics["missing"] is True
    assert metrics["passes"] is False


def test_apply_net_economics_preserves_teacher_gross_fields():
    brain = load_brain()
    row = {
        "maxProfit": 1000,
        "maxLoss": 9000,
        "probProfit": 0.70,
        "premiumEdge": -2000,
        "isCredit": True,
        "targetProfit": 500,
        "stopLoss": 5400,
    }

    brain._apply_net_economics(row, net_prob_profit=0.65, friction_breakdown={"total": 350, "status": "OK"})

    assert row["maxProfit"] == 1000
    assert row["maxLoss"] == 9000
    assert row["grossTargetProfit"] == 500
    assert row["netMaxProfitAfterFriction"] == 650
    assert row["netMaxLossAfterFriction"] == 9350
    assert row["targetProfit"] == 325
    assert row["decisionEconomicsBasis"] == "NET_AFTER_TEACHER_FRICTION"
