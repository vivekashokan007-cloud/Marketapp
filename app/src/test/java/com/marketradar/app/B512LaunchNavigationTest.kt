package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * b512 R2 regression for Codex R1 (fresh-launch openTab discarded). Behavioural, not source-substring:
 * an Activity simulator reproduces MainActivity's real call ordering (handleIntent -> request BEFORE uiAlive=true,
 * then onUiReady; onPageFinished; onNewIntent; saved-state recreation skips the launch intent; onDestroy) against a
 * fake WebView/PWA page whose boot stages are driven by the test (slow boot, page not yet defining navigation).
 */
class B512LaunchNavigationTest {
    /** Fake page: before app.js nothing exists; after app.js, b512NativeNav exists (it queues until restore). */
    class FakePage {
        var appJsLoaded = false
        var activeTab = "market"
        val switches = mutableListOf<String>()
        val scripts = mutableListOf<String>()
        fun eval(script: String): String {
            scripts += script
            val tab = Regex("(?:b512NativeNav|switchTab)\\('([A-Za-z0-9_]+)'\\)").find(script)?.groupValues?.get(1)
            if (!appJsLoaded || tab == null) return "\"notready\""
            if (script.contains("b512NativeNav")) { switches += tab; activeTab = tab; return "\"ok\"" }
            if (script.contains("switchTab(")) { switches += tab; activeTab = tab; return "null" }
            return "null"
        }
    }

    /** Single-threaded looper: evaluate callbacks and delayed retries run when the test pumps. */
    class Looper {
        private val q = ArrayDeque<Pair<Long, () -> Unit>>(); var now = 0L
        fun post(at: Long, b: () -> Unit) { q.addLast(at to b) }
        fun advance(ms: Long) {
            val until = now + ms
            while (true) {
                val next = q.filter { it.first <= until }.minByOrNull { it.first } ?: break
                q.remove(next); now = maxOf(now, next.first); next.second()
            }
            now = until
        }
    }

    class ActivitySim(val page: FakePage, val looper: Looper) {
        var destroyed = false
        val nav = LaunchNavigation(object : LaunchNavigation.Host {
            override fun evaluate(script: String, onResult: (String?) -> Unit) {
                looper.post(looper.now) { if (!destroyed) { val r = page.eval(script); looper.post(looper.now) { onResult(r) } } }
            }
            override fun postDelayed(delayMs: Long, block: () -> Unit) { looper.post(looper.now + delayMs) { if (!destroyed) block() } }
        })
        /** Mirrors MainActivity.onCreate ordering. */
        fun onCreate(savedState: Boolean, openTab: String?) {
            if (!savedState) handleIntent(openTab)     // runs before uiAlive = true
            nav.onUiReady()                            // uiAlive = true
        }
        fun handleIntent(openTab: String?) { nav.request(openTab, "intent") }
        fun onNewIntent(openTab: String?) = handleIntent(openTab)
        fun onPageFinished() = nav.onPageFinished()
        fun onDestroy() { destroyed = true; nav.destroy() }
    }

    private fun bootPage(sim: ActivitySim, page: FakePage, looper: Looper, afterMs: Long) {
        looper.advance(afterMs); page.appJsLoaded = true; looper.advance(1); sim.onPageFinished(); looper.advance(50)
    }

    @Test fun coldLaunchWithOpenTabDeliversExactlyOnceAfterPwaReady() {
        val page = FakePage(); val looper = Looper(); val sim = ActivitySim(page, looper)
        sim.onCreate(savedState = false, openTab = "ml")
        bootPage(sim, page, looper, afterMs = 900)
        looper.advance(5_000)
        assertEquals(listOf("ml"), page.switches)
        assertEquals("ml", page.activeTab)
        sim.onPageFinished(); looper.advance(5_000)         // later page-finished (e.g. reload) must not resend
        assertEquals(listOf("ml"), page.switches)
    }

    @Test fun slowPageBootStillDeliversOnceAfterRetries() {
        val page = FakePage(); val looper = Looper(); val sim = ActivitySim(page, looper)
        sim.onCreate(savedState = false, openTab = "positions")
        looper.advance(8_000)                                // app.js still not loaded: no switch, request retained
        assertTrue(page.switches.isEmpty())
        page.appJsLoaded = true; looper.advance(1_000)       // timer retry picks it up even without page-finished
        assertEquals(listOf("positions"), page.switches)
        looper.advance(10_000); assertEquals(1, page.switches.size)
    }

    @Test fun warmOnNewIntentDeliversNewestDestination() {
        val page = FakePage(); val looper = Looper(); val sim = ActivitySim(page, looper)
        sim.onCreate(savedState = false, openTab = null)
        bootPage(sim, page, looper, afterMs = 200)
        sim.onNewIntent("brain"); looper.advance(100)
        assertEquals(listOf("brain"), page.switches)
        // two intents during a reload: newest wins, delivered once
        page.appJsLoaded = false
        sim.onNewIntent("ml"); sim.onNewIntent("positions")
        bootPage(sim, page, looper, afterMs = 700); looper.advance(3_000)
        assertEquals(listOf("brain", "positions"), page.switches)
    }

    @Test fun recreationWithoutNewDestinationDoesNotReplayConsumedIntent() {
        val page = FakePage(); val looper = Looper()
        val first = ActivitySim(page, looper)
        first.onCreate(savedState = false, openTab = "ml"); bootPage(first, page, looper, 100)
        first.onDestroy()
        page.activeTab = "brain"                              // user moved on; page restore puts them back here
        val second = ActivitySim(page, looper)               // same launch intent object, but savedInstanceState != null
        second.onCreate(savedState = true, openTab = "ml"); bootPage(second, page, looper, 100); looper.advance(5_000)
        assertEquals(listOf("ml"), page.switches)
        assertEquals("brain", page.activeTab)
    }

    @Test fun destroyBeforeDeliveryDropsSafely() {
        val page = FakePage(); val looper = Looper(); val sim = ActivitySim(page, looper)
        sim.onCreate(savedState = false, openTab = "ml"); looper.advance(500)
        sim.onDestroy()
        page.appJsLoaded = true; looper.advance(10_000)
        assertTrue(page.switches.isEmpty())
        sim.nav.onPageFinished(); sim.nav.onUiReady(); looper.advance(1_000)  // late callbacks on the dead instance
        assertTrue(page.switches.isEmpty())
        assertFalse(sim.nav.request("positions", "late"))
    }

    @Test fun neverCarriesBrokerOrTradeActions() {
        val page = FakePage(); val looper = Looper(); val sim = ActivitySim(page, looper)
        page.appJsLoaded = true
        sim.onCreate(savedState = false, openTab = "ml');placeOrder('x")   // injection attempt rejected outright
        sim.onNewIntent("positions"); looper.advance(2_000)
        assertEquals(listOf("positions"), page.switches)
        for (s in page.scripts) {
            assertFalse(s, Regex("placeOrder|sendOrder|execute|submit|AndroidBridge|NativeBridge|trade", RegexOption.IGNORE_CASE).containsMatchIn(s))
        }
    }
}
