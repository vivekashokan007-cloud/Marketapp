package com.marketradar.app

import java.util.Locale

/**
 * The app's single derivation of the execution mode (paper | sandbox | live), extracted unchanged from
 * MarketWatchService.runBrainAnalysis so the Paper-only evidence capture (R3) can re-derive it fresh
 * before expensive capture work. Android-free.
 */
object ExecutionModeResolver {
    const val PAPER = "paper"

    fun derive(explicitMode: String?, sandboxEnabled: Boolean, orderProxyUrl: String?): String {
        val explicit = (explicitMode ?: "").trim().lowercase(Locale.US)
        val proxy = (orderProxyUrl ?: "").trim()
        return when {
            explicit in setOf("paper", "sandbox", "live") -> explicit
            sandboxEnabled -> "sandbox"
            proxy.startsWith("https://") -> "live"
            else -> "paper"
        }
    }

    fun isPaper(mode: String?): Boolean = mode != null && mode.trim().lowercase(Locale.US) == PAPER
}
