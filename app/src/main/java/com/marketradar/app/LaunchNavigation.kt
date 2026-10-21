package com.marketradar.app

/**
 * b512 R2 (Codex R1): native -> PWA tab navigation that survives a cold launch and a slow page boot.
 * Pure / Android-free so it is JVM-testable; MainActivity supplies the [Host] (WebView evaluate + Handler delay).
 *
 * Rules:
 *  - Only a tab name is ever retained (MA14 charset). No broker/trade/order action is carried, so nothing that could
 *    execute can be replayed by navigation recovery.
 *  - A fresh explicit request (cold launch intent or onNewIntent) replaces any older pending one: newest wins.
 *  - Delivery is attempted only after [onUiReady]; it is retried on page-finished and on a bounded timer until the
 *    PWA acknowledges ("ok" = switched, "queued" = PWA holds it until its own restore completes, "rejected" = unknown tab, not retried). Once acknowledged
 *    the request is consumed and never re-sent (ordinary recreation does not replay a consumed launch intent:
 *    MainActivity only calls [request] for the launch intent when savedInstanceState == null).
 *  - [destroy] drops anything pending and ignores late callbacks/timers.
 */
class LaunchNavigation(private val host: Host, private val retryDelayMs: Long = 300L, private val maxAttempts: Int = 60) {
    interface Host {
        fun evaluate(script: String, onResult: (String?) -> Unit)
        fun postDelayed(delayMs: Long, block: () -> Unit)
        fun log(message: String) {}
    }

    private var alive = true
    private var ready = false
    private var pending: String? = null
    private var pendingId = 0
    private var inFlightId = 0
    private var attempts = 0
    private var retryScheduled = false
    var delivered = 0; private set
    var dropped = 0; private set

    val pendingTab: String? get() = pending

    /** Returns false (and keeps nothing) for a missing/unsafe tab. */
    fun request(rawTab: String?, source: String): Boolean {
        if (rawTab == null) return false
        if (!isSafeTab(rawTab)) { host.log("NAV_REJECTED_UNSAFE: source=$source"); return false }
        if (!alive) { dropped++; return false }
        pending = rawTab; pendingId++; attempts = 0; inFlightId = 0
        host.log("NAV_REQUEST: tab=$rawTab source=$source id=$pendingId")
        tryDeliver()
        return true
    }

    fun onUiReady() { if (!alive) return; ready = true; tryDeliver() }
    fun onPageFinished() { if (!alive) return; attempts = 0; tryDeliver() }

    /** Renderer gone / view replaced: keep the pending request but stop sending until ready again. */
    fun onUiLost() { ready = false; inFlightId = 0 }

    fun destroy() {
        if (pending != null) { dropped++; host.log("NAV_DROPPED_ON_DESTROY: tab=$pending") }
        alive = false; ready = false; pending = null; inFlightId = 0
    }

    private fun tryDeliver() {
        val tab = pending ?: return
        if (!alive || !ready || inFlightId == pendingId) return
        val id = pendingId
        inFlightId = id
        attempts++
        host.evaluate(script(tab)) { raw ->
            if (!alive || id != pendingId) return@evaluate          // superseded or destroyed
            inFlightId = 0
            when (raw?.trim('"', ' ')) {
                "ok", "queued", "rejected" -> { pending = null; delivered++; host.log("NAV_DELIVERED: tab=$tab id=$id attempts=$attempts") }
                else -> scheduleRetry()
            }
        }
    }

    private fun scheduleRetry() {
        if (retryScheduled || pending == null) return
        if (attempts >= maxAttempts) { host.log("NAV_WAITING_FOR_PAGE: tab=$pending attempts=$attempts"); return } // page-finished re-arms
        retryScheduled = true
        host.postDelayed(retryDelayMs) { retryScheduled = false; if (alive) tryDeliver() }
    }

    companion object {
        fun isSafeTab(t: String) = t.isNotEmpty() && t.length <= 40 && t.all { it.isLetterOrDigit() || it == '_' }

        /** The only JS ever sent for navigation: a tab switch request. "notready" until the PWA nav entry exists. */
        fun script(tab: String): String {
            require(isSafeTab(tab))
            return "(function(){try{if(typeof b512NativeNav==='function')return b512NativeNav('$tab');" +
                "if(document.readyState==='complete'&&typeof switchTab==='function'){switchTab('$tab');return 'ok';}}catch(e){}" +
                "return 'notready';})()"
        }
    }
}
