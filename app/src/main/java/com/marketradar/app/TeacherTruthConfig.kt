package com.marketradar.app

import org.json.JSONObject

object TeacherTruthConfig {
    const val LABEL_VERSION = "teacher_v1"
    const val CONFIG_VERSION = "2026-06-15"

    private const val TP_CAPTURE_PCT = 0.50
    private const val SL_LOSS_MULTIPLE = 1.00
    private const val STT_OPTIONS = 0.0015
    private const val BROKERAGE_PER_ORDER = 20.0
    private const val EXCHANGE_PER_LEG = 15.0
    private const val GST_RATE = 0.18
    private const val FIXED_BUFFER = 3.0

    private const val NF_2LEG_SLIPPAGE = 1.0
    private const val NF_4LEG_SLIPPAGE = 2.0
    private const val BNF_2LEG_SLIPPAGE = 2.0
    private const val BNF_4LEG_SLIPPAGE = 4.0

    fun toJson(): JSONObject = JSONObject().apply {
        put("label_version", LABEL_VERSION)
        put("config_version", CONFIG_VERSION)
        put("tp_capture_pct", TP_CAPTURE_PCT)
        put("sl_loss_multiple", SL_LOSS_MULTIPLE)
        put("stt_options", STT_OPTIONS)
        put("brokerage_per_order", BROKERAGE_PER_ORDER)
        put("exchange_per_leg", EXCHANGE_PER_LEG)
        put("gst_rate", GST_RATE)
        put("fixed_buffer", FIXED_BUFFER)
        put(
            "slippage",
            JSONObject()
                .put("NF_2LEG", NF_2LEG_SLIPPAGE)
                .put("NF_4LEG", NF_4LEG_SLIPPAGE)
                .put("BNF_2LEG", BNF_2LEG_SLIPPAGE)
                .put("BNF_4LEG", BNF_4LEG_SLIPPAGE)
        )
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
