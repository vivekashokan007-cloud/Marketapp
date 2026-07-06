package com.marketradar.app

import org.json.JSONObject

object TeacherTruthConfig {
    const val LABEL_VERSION = "teacher_v1"
    const val CONFIG_VERSION = "tc_2026_07_A"

    private const val TEACHER_TIME_BASIS_DAYS = 252.0
    private const val TP_CAPTURE_PCT = 0.50
    private const val SL_LOSS_MULTIPLE = 0.60
    private const val BROKERAGE_PER_ORDER = 20.0
    private const val GST_RATE = 0.18
    private const val STAMP_DUTY_BUY_RATE = 0.00003
    private const val SEBI_RATE = 0.000001
    private const val IPFT_RATE = 0.000000001
    private const val EXCHANGE_OPTIONS_RATE_PRE_2026_03_01 = 0.0003503
    private const val EXCHANGE_OPTIONS_RATE_FROM_2026_03_01 = 0.0003553
    private const val STT_OPTIONS_RATE_PRE_2026_04_01 = 0.0010
    private const val STT_OPTIONS_RATE_FROM_2026_04_01 = 0.0015
    private const val BID_ASK_SLIPPAGE_FALLBACK_POINTS = 0.25

    fun toJson(): JSONObject = JSONObject().apply {
        put("label_version", LABEL_VERSION)
        put("config_version", CONFIG_VERSION)
        put("tp_capture_pct", TP_CAPTURE_PCT)
        put("sl_loss_multiple", SL_LOSS_MULTIPLE)
        put("option_time_basis", "trading_252")
        put("teacher_time_basis_days", TEACHER_TIME_BASIS_DAYS)
        put("tp_threshold_basis", "net_pnl_vs_net_max_profit")
        put("sl_threshold_basis", "net_pnl_vs_0.6_max_loss_no_breach_gate")
        put("brokerage_per_order", BROKERAGE_PER_ORDER)
        put("gst_rate", GST_RATE)
        put("stamp_duty_buy_rate", STAMP_DUTY_BUY_RATE)
        put("sebi_rate", SEBI_RATE)
        put("ipft_rate", IPFT_RATE)
        put("exchange_options_rate_pre_2026_03_01", EXCHANGE_OPTIONS_RATE_PRE_2026_03_01)
        put("exchange_options_rate_from_2026_03_01", EXCHANGE_OPTIONS_RATE_FROM_2026_03_01)
        put("stt_options_rate_pre_2026_04_01", STT_OPTIONS_RATE_PRE_2026_04_01)
        put("stt_options_rate_from_2026_04_01", STT_OPTIONS_RATE_FROM_2026_04_01)
        put("bid_ask_slippage_fallback_points", BID_ASK_SLIPPAGE_FALLBACK_POINTS)
        put(
            "vix_buckets",
            JSONObject()
                .put("very_low_max", 14.0)
                .put("low_max", 16.0)
                .put("normal_max", 19.0)
                .put("high_max", 24.0)
        )
    }
}
