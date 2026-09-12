package com.marketradar.app

/**
 * Legacy live SHADOW_* reference (gross current_pnl vs these multipliers).
 *
 * G4 net-basis alignment is a NEW policy version — see [PositionExitPolicy]
 * (`position_exit_policy_v1_net_20260912`). Do not silently reinterpret
 * these constants as net, and do not change TP_MULT / SL_MULT / EOD_HH_MM
 * here. A later cutoff or multiplier change is a separately versioned
 * experiment.
 *
 * Schedule (shared; do not randomly change unrelated windows):
 *   policy exit intent / no new intraday entry = 15:15 IST (this object)
 *   native poll/session close                  = 15:40 IST
 *   Python readiness helper                    = 15:30 IST
 */
object PositionPolicyV1 {
    const val SL_MULT = 0.60
    const val TP_MULT = 0.50
    const val EOD_HH_MM = "15:15"
    const val VERSION = "POSITION_POLICY_V1"
    /** Pointer only — live SHADOW_* still uses [VERSION]. */
    const val NET_CONTRACT_VERSION = "position_exit_policy_v1_net_20260912"
}
