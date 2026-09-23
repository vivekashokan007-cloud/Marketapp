"""Batch C C1 — entry cohorts + role memberships (no double-counted economics)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Set

ENTRY_COHORT_MEMBERSHIPS_VERSION = "entry_cohort_memberships_v1_batch_c_20260923"

# Registered cohorts only — do not invent more thresholds.
COHORT_SELL_PREMIUM = "SELL_PREMIUM"
COHORT_SELL_PREMIUM_GE55 = "SELL_PREMIUM_GE55"
COHORT_SELL_PREMIUM_GE70 = "SELL_PREMIUM_GE70"
COHORT_WAIT_CONTROL = "WAIT_CONTROL"

REGISTERED_COHORTS = (
    COHORT_SELL_PREMIUM,
    COHORT_SELL_PREMIUM_GE55,
    COHORT_SELL_PREMIUM_GE70,
    COHORT_WAIT_CONTROL,
)

# Roles as memberships on one outcome row — not separate economic rows.
ROLE_RESEARCH_TOP = "research_top"
ROLE_ENTRY_TOP_GLOBAL = "entry_top_global"
ROLE_ENTRY_TOP_PER_INDEX = "entry_top_per_index"
ROLE_PAPER_USER_EXIT_OBSERVED = "paper_user_exit_observed"

REGISTERED_ROLES = (
    ROLE_RESEARCH_TOP,
    ROLE_ENTRY_TOP_GLOBAL,
    ROLE_ENTRY_TOP_PER_INDEX,
    ROLE_PAPER_USER_EXIT_OBSERVED,
)


def classify_entry_cohort(
    *,
    action: Optional[str] = None,
    confidence: Optional[float] = None,
    recommendation: Optional[str] = None,
) -> str:
    """Map an entry decision into one registered cohort.

    SELL_PREMIUM_GE70 ⊆ GE55 ⊆ SELL_PREMIUM for membership tagging, but the
    *primary* cohort label is the most specific match. WAIT is a control.
    """
    act = (action or recommendation or "").strip().upper().replace(" ", "_")
    if act in ("WAIT", "MONITOR", "WAIT_CONTROL"):
        return COHORT_WAIT_CONTROL
    if "SELL" not in act and "PREMIUM" not in act and act not in ("SELL_PREMIUM",):
        # Unknown / non-sell → treat as WAIT control for research isolation.
        if act in ("", "NONE", "UNKNOWN"):
            return COHORT_WAIT_CONTROL
        if "WAIT" in act or "MONITOR" in act:
            return COHORT_WAIT_CONTROL
    conf: Optional[float]
    try:
        conf = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        conf = None
    if conf is not None and conf >= 70:
        return COHORT_SELL_PREMIUM_GE70
    if conf is not None and conf >= 55:
        return COHORT_SELL_PREMIUM_GE55
    if "SELL" in act or "PREMIUM" in act or act == "SELL_PREMIUM":
        return COHORT_SELL_PREMIUM
    return COHORT_WAIT_CONTROL


def cohort_membership_set(primary_cohort: str) -> List[str]:
    """Inclusive membership tags without inventing extra thresholds.

    GE70 also belongs to GE55 and SELL_PREMIUM sets for slice filters;
    economics remain on a single outcome row.
    """
    if primary_cohort not in REGISTERED_COHORTS:
        raise KeyError(f"unknown_cohort:{primary_cohort}")
    if primary_cohort == COHORT_WAIT_CONTROL:
        return [COHORT_WAIT_CONTROL]
    if primary_cohort == COHORT_SELL_PREMIUM_GE70:
        return [COHORT_SELL_PREMIUM, COHORT_SELL_PREMIUM_GE55, COHORT_SELL_PREMIUM_GE70]
    if primary_cohort == COHORT_SELL_PREMIUM_GE55:
        return [COHORT_SELL_PREMIUM, COHORT_SELL_PREMIUM_GE55]
    return [COHORT_SELL_PREMIUM]


def build_membership_record(
    *,
    entry_identity: str,
    primary_cohort: str,
    roles: Optional[Sequence[str]] = None,
    notification_delivered: Optional[bool] = None,
    notification_modeled_separately: bool = True,
) -> Dict[str, Any]:
    role_list = []
    seen: Set[str] = set()
    for r in roles or []:
        if r not in REGISTERED_ROLES:
            raise KeyError(f"unknown_role:{r}")
        if r not in seen:
            role_list.append(r)
            seen.add(r)
    return {
        "contract_version": ENTRY_COHORT_MEMBERSHIPS_VERSION,
        "entry_identity": entry_identity,
        "primary_cohort": primary_cohort,
        "cohort_memberships": cohort_membership_set(primary_cohort),
        "roles": role_list,
        "notification_delivered": notification_delivered,
        "notification_modeled_separately": bool(notification_modeled_separately),
        "economics_row_count": 1,
        "double_count_forbidden": True,
    }


def attach_roles_without_duplicating_economics(
    outcome_row: Dict[str, Any],
    membership: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach cohort/role memberships onto one outcome; never clone P&L."""
    out = deepcopy(outcome_row)
    if "net_rupees" in out and "economics_clone" in out:
        raise ValueError("economics_clone_forbidden")
    out["membership"] = {
        "primary_cohort": membership["primary_cohort"],
        "cohort_memberships": list(membership["cohort_memberships"]),
        "roles": list(membership["roles"]),
        "notification_delivered": membership.get("notification_delivered"),
        "notification_modeled_separately": membership.get(
            "notification_modeled_separately", True
        ),
    }
    out["economics_row_count"] = 1
    return out
