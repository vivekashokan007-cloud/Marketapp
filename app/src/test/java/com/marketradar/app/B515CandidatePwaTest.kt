package com.marketradar.app

import com.marketradar.app.CandidatePwaRoute.Decision
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File

/**
 * b515 CANDIDATE-ONLY (DO NOT MERGE): bundled-PWA candidate routing, response planning, hash-manifest verification and
 * source contracts (same applicationId/signing, release unchanged, no data-clearing). JVM only: no device/emulator.
 * Runs in every unit-test variant (testDebugUnitTest and testCandidateUnitTest).
 */
class B515CandidatePwaTest {
    private val base = "https://vivekashokan007-cloud.github.io/MarketVivi/"
    private fun file(rel: String) = listOf(File(rel), File("app/$rel")).first { it.exists() }
    private fun src(n: String) = file("src/main/java/com/marketradar/app/$n").readText()
    private fun serve(url: String, method: String = "GET") = CandidatePwaRoute.decide(method, url)

    // ---------- routing: only the production host + /MarketVivi/ ----------
    @Test fun routesOnlyTheProductionHostAndMarketViviPath() {
        for (u in listOf(
            "https://fdynxkfxohbnlvayouje.supabase.co/rest/v1/trades_v2?select=*",
            "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2",
            "https://fonts.googleapis.com/css2?family=Roboto",
            "https://api.github.com/repos/vivekashokan007-cloud/Marketapp/releases/latest",
            "https://api.upstox.com/v2/market-quote/ltp",
            "https://vivekashokan007-cloud.github.io/",
            "https://vivekashokan007-cloud.github.io/favicon.ico",
            "https://vivekashokan007-cloud.github.io/MarketVivi",            // slash-less redirect stays live
            "https://vivekashokan007-cloud.github.io/Marketapp/app.js",
            "https://vivekashokan007-cloud.github.io/MarketViviX/app.js",
            "https://other-user.github.io/MarketVivi/app.js",
            "https://vivekashokan007-cloud.github.io.evil.example/MarketVivi/app.js",
            "https://evil.example/MarketVivi/app.js?h=vivekashokan007-cloud.github.io",
            "http://vivekashokan007-cloud.github.io/MarketVivi/app.js",       // not https
            "https://vivekashokan007-cloud.github.io:8443/MarketVivi/app.js",
            "https://x@vivekashokan007-cloud.github.io/MarketVivi/app.js",
            "file:///android_asset/pwa_candidate/app.js",
            "https://appassets.androidplatform.net/assets/pwa_candidate/app.js",
            "data:text/html,hi", "about:blank", "not a url", "",
        )) assertSame(u, Decision.PassThrough, serve(u))
        assertSame(Decision.PassThrough, CandidatePwaRoute.decide("GET", null))
        assertSame("POST is never intercepted", Decision.PassThrough, serve(base + "app.js", "POST"))
        assertSame("OPTIONS is never intercepted", Decision.PassThrough, serve(base, "OPTIONS"))
        assertEquals(Decision.Serve("app.js", "text/javascript"), serve(base + "app.js", "HEAD"))
        assertEquals(Decision.Serve("app.js", "text/javascript"), serve("HTTPS://VIVEKASHOKAN007-CLOUD.GITHUB.IO/MarketVivi/app.js"))
        assertEquals(Decision.Serve("app.js", "text/javascript"), serve("https://vivekashokan007-cloud.github.io:443/MarketVivi/app.js"))
    }

    @Test fun indexResolutionQueryStringsAndFragments() {
        assertEquals(Decision.Serve("index.html", "text/html"), serve(base))
        assertEquals(Decision.Serve("index.html", "text/html"), serve(base + "index.html"))
        assertEquals(Decision.Serve("index.html", "text/html"), serve(base + "?v=1380"))
        assertEquals(Decision.Serve("index.html", "text/html"), serve(base + "#tab=positions"))
        assertEquals(Decision.Serve("index.html", "text/html"), serve(base + "index.html?x=1#scroll"))
        assertEquals(Decision.Serve("app.js", "text/javascript"), serve(base + "app.js?v=1380"))
        assertEquals(Decision.Serve("api.js", "text/javascript"), serve(base + "api.js?v=1134"))
        assertEquals(Decision.Serve("log-viewer.js", "text/javascript"), serve(base + "log-viewer.js?v=1161"))
        assertEquals(Decision.Serve("style.css", "text/css"), serve(base + "style.css?v=1142"))
        assertEquals(Decision.Serve("manifest.json", "application/manifest+json"), serve(base + "manifest.json"))
        // A different ?v= is still served from the bundle (the bundle, not the query, decides the bytes).
        assertEquals(Decision.Serve("app.js", "text/javascript"), serve(base + "app.js?v=1378"))
    }

    @Test fun relativeResourcePathsOfTheBundledIndexAllResolveToBundledFiles() {
        val index = String(pinnedBytes("index.html") ?: return println("SKIPPED real-index relative paths: MARKETVIVI_DIR not set"), Charsets.UTF_8)
        val refs = Regex("""(?:src|href)="([^"]+)"""").findAll(index).map { it.groupValues[1] }
            .filter { !it.startsWith("http") && !it.startsWith("data:") && !it.startsWith("#") }.toList()
        assertTrue("index.html must reference the runtime files relatively: $refs", refs.size >= 5)
        for (r in refs) {
            val resolved = java.net.URI(base).resolve(r).toString()
            val d = serve(resolved)
            assertTrue("$r -> $resolved -> $d", d is Decision.Serve)
        }
        println("REAL index.html relative refs all served from bundle: $refs")
    }

    @Test fun contentTypesForEveryRuntimeFile() {
        val want = mapOf("index.html" to "text/html", "app.js" to "text/javascript", "api.js" to "text/javascript",
            "log-viewer.js" to "text/javascript", "style.css" to "text/css", "manifest.json" to "application/manifest+json")
        assertEquals(CandidatePwaRoute.RUNTIME_FILES.toSet(), want.keys)
        for ((f, t) in want) assertEquals(f, t, CandidatePwaRoute.mimeTypeOf(f))
    }

    @Test fun inScopeButNotBundledIsAnExplicitErrorNeverLive() {
        for (p in listOf("missing.js", "README.md", "CLAUDE.md", "PROJECT_KNOWLEDGE.md", "docs/", "docs/a.md", "tests/test_x.mjs",
                "tools/build_pages_site.py", ".git/config", "sub/", "sub/app.js", "../app.js", "..", ".", "%2e%2e/app.js",
                "app%2Ejs", "index.html/", "/app.js", "App.js", "app.js.map", ".nojekyll", "credentials.json", "sw.js")) {
            val d = serve(base + p)
            assertTrue("$p -> $d", d is Decision.NotBundled)
        }
        assertTrue(serve("https://vivekashokan007-cloud.github.io/marketvivi/app.js") is Decision.NotBundled)
        val state = verifiedState()
        val plan = CandidatePwaResponses.plan(serve(base + "missing.js"), state, "n" + "0".repeat(39))!!
        assertEquals(404, plan.status)
        assertTrue(String(plan.body).contains("CANDIDATE"))
        assertTrue(String(plan.body).contains("never falls back to the live site"))
        assertNotNull(plan.candidateError)
    }

    // ---------- response planning ----------
    @Test fun verifiedBundleServesExactBytesWithNoStoreAndIdentityHeaders() {
        val state = verifiedState()
        for (f in CandidatePwaRoute.RUNTIME_FILES) {
            val plan = CandidatePwaResponses.plan(serve(base + f + "?v=1"), state, NATIVE)!!
            assertEquals(200, plan.status)
            assertArrayEquals(f, FAKE.getValue(f), plan.body)
            assertEquals("utf-8", plan.encoding)
            assertTrue(plan.headers.getValue("Cache-Control").contains("no-store"))
            assertEquals("native=$NATIVE pwa=$PWA", plan.headers.getValue("X-MarketRadar-Candidate"))
            assertNull(plan.candidateError)
        }
        assertNull("pass-through must return null (normal network)", CandidatePwaResponses.plan(Decision.PassThrough, state, NATIVE))
    }

    @Test fun missingRequiredAssetOrFailedVerificationIsAnExplicitCandidateErrorBelow500() {
        val missing = CandidatePwaManifest.verify(manifest(), sha(manifest()), PWA) { if (it == "app.js") null else FAKE[it] }
        assertTrue(missing is CandidatePwaManifest.Result.Failed)
        assertTrue((missing as CandidatePwaManifest.Result.Failed).reasons.contains("asset_missing app.js"))
        for (f in CandidatePwaRoute.RUNTIME_FILES) {
            val plan = CandidatePwaResponses.plan(serve(base + f), missing, NATIVE)!!
            assertEquals(409, plan.status)
            assertTrue(plan.status < 500)   // < 500 so the generic "No internet connection" view does not mask it
            assertTrue(String(plan.body).contains("bundle verification failed"))
        }
        // Even a Verified state that somehow lacks a file never yields null (null would mean "go live").
        val partial = (verifiedState() as CandidatePwaManifest.Result.Verified).let { it.copy(files = it.files - "style.css") }
        val p = CandidatePwaResponses.plan(serve(base + "style.css"), partial, NATIVE)!!
        assertEquals(409, p.status)
        assertTrue(p.candidateError!!.contains("required bundled asset missing: style.css"))
    }

    // ---------- hash manifest verification ----------
    @Test fun manifestVerificationPassesOnExactBytesAndFailsOnAnyTamper() {
        assertTrue(verifiedState() is CandidatePwaManifest.Result.Verified)
        for (f in CandidatePwaRoute.RUNTIME_FILES) {
            val tampered = FAKE.toMutableMap().apply { put(f, getValue(f).copyOf().also { it[0] = (it[0] + 1).toByte() }) }
            val r = CandidatePwaManifest.verify(manifest(), sha(manifest()), PWA) { tampered[it] }
            assertTrue(f, r is CandidatePwaManifest.Result.Failed)
            assertTrue(f, (r as CandidatePwaManifest.Result.Failed).reasons.any { it.startsWith("asset_sha256_mismatch $f") })
            val appended = FAKE.toMutableMap().apply { put(f, getValue(f) + byteArrayOf(10)) }
            assertTrue(CandidatePwaManifest.verify(manifest(), sha(manifest()), PWA) { appended[it] } is CandidatePwaManifest.Result.Failed)
        }
        val m = manifest()
        fun failed(r: CandidatePwaManifest.Result, why: String) =
            assertTrue(why, r is CandidatePwaManifest.Result.Failed && r.reasons.any { it.startsWith(why) })
        failed(CandidatePwaManifest.verify(null, sha(m), PWA) { FAKE[it] }, "manifest_missing")
        failed(CandidatePwaManifest.verify(m, "0".repeat(64), PWA) { FAKE[it] }, "manifest_sha256_mismatch")
        failed(CandidatePwaManifest.verify(m, "none", PWA) { FAKE[it] }, "build_manifest_pin_invalid")
        failed(CandidatePwaManifest.verify(m, sha(m), "f".repeat(40)) { FAKE[it] }, "manifest_marketvivi_sha_differs_from_build")
        val extra = manifest { it.getJSONObject("files").put("README.md", JSONObject().put("sha256", "a".repeat(64)).put("bytes", 1)) }
        failed(CandidatePwaManifest.verify(extra, sha(extra), PWA) { FAKE[it] }, "manifest_files_not_runtime_allowlist")
        val fewer = manifest { it.getJSONObject("files").remove("api.js") }
        failed(CandidatePwaManifest.verify(fewer, sha(fewer), PWA) { FAKE[it] }, "manifest_files_not_runtime_allowlist")
        val otherBase = manifest { it.put("document_base", "https://appassets.androidplatform.net/") }
        failed(CandidatePwaManifest.verify(otherBase, sha(otherBase), PWA) { FAKE[it] }, "manifest_document_base")
        val shortSha = manifest { it.put("marketvivi_sha", "6cb5ad51") }
        failed(CandidatePwaManifest.verify(shortSha, sha(shortSha), "6cb5ad51") { FAKE[it] }, "manifest_marketvivi_sha_invalid")
        failed(CandidatePwaManifest.verify("{".toByteArray(), sha("{".toByteArray()), PWA) { FAKE[it] }, "manifest_unparseable")
    }

    @Test fun committedPinIsTheAcceptedMarketViviCommitAndRuntimeAllowlist() {
        val pin = JSONObject(file("src/candidate/pwa_expected_manifest.json").readText())
        assertEquals("aabccc26b828b03935ff93f3b54ae50c97ce9e84", pin.getString("marketvivi_sha"))
        assertEquals(CandidatePwaManifest.SCHEMA, pin.getString("schema"))
        assertEquals(base, pin.getString("document_base"))
        assertEquals(CandidatePwaRoute.RUNTIME_FILES.toSortedSet(), pin.getJSONObject("files").keys().asSequence().toSortedSet())
        assertEquals("ca0d7a352939e93cd67bb12dce7a37c575748523731796c6d886d2d3b0df426b",
            pin.getJSONObject("files").getJSONObject("app.js").getString("sha256"))
        val id = pin.getJSONObject("pwa_identity")
        assertEquals("2.6.85", id.getString("version")); assertEquals(516, id.getInt("build")); assertEquals(1380, id.getInt("app_js_v"))
        // When a MarketVivi checkout at the pinned commit is available (local review), its real bytes verify too.
        val mv = System.getenv("MARKETVIVI_DIR")?.let { File(it) }?.takeIf { it.isDirectory }
            ?: return println("SKIPPED real-bytes pin check: MARKETVIVI_DIR not set")
        val raw = file("src/candidate/pwa_expected_manifest.json").readBytes()
        val r = CandidatePwaManifest.verify(raw, CandidatePwaManifest.sha256Hex(raw), pin.getString("marketvivi_sha")) {
            File(mv, it).takeIf { f -> f.isFile }?.readBytes()
        }
        assertTrue("real MarketVivi bytes at $mv: $r", r is CandidatePwaManifest.Result.Verified)
        println("REAL MarketVivi bytes at $mv verify against the committed pin: ${(r as CandidatePwaManifest.Result.Verified).hashes}")
    }

    // ---------- source contracts: same identity/signing, release unchanged, hook gated, nothing cleared ----------
    @Test fun candidateBuildTypeKeepsApplicationIdAndReleaseSigning() {
        val g = file("build.gradle.kts").readText()
        assertTrue(g.contains("applicationId = \"com.marketradar.app\""))
        assertTrue(g.contains("versionCode = 516")); assertTrue(g.contains("versionName = \"2.6.85\""))
        for (bad in listOf("applicationIdSuffix", "versionNameSuffix", "isDebuggable = true", "debuggable true", "setDataDirectorySuffix"))
            assertFalse(bad, g.contains(bad))
        val cand = g.substring(g.indexOf("create(\"candidate\") {")).let { it.substring(0, it.indexOf("\n        }\n")) }
        assertTrue(cand.contains("initWith(getByName(\"release\"))"))
        assertTrue(cand.contains("signingConfig = signingConfigs.getByName(\"release\")"))
        assertTrue(cand.contains("buildConfigField(\"boolean\", \"PWA_CANDIDATE\", \"true\")"))
        assertFalse(cand.contains("applicationId"))
        assertFalse(cand.contains("signingConfigs.getByName(\"debug\")"))
        // The release block is byte-identical to 3768131f's.
        val release = g.substring(g.indexOf("        release {"), g.indexOf("        // b515 CANDIDATE-ONLY (DO NOT MERGE): same applicationId"))
        assertEquals("""        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
""", release)
        // Defaults (debug/release): routing off.
        assertTrue(g.contains("buildConfigField(\"boolean\", \"PWA_CANDIDATE\", \"false\")"))
        // Build-time bundle verification is wired only into candidate tasks.
        assertTrue(g.contains("it.name == \"mergeCandidateAssets\" || it.name == \"preCandidateBuild\""))
    }

    @Test fun mainActivityHookIsGatedAndDocumentUrlIsUnchanged() {
        val a = src("MainActivity.kt")
        assertTrue(a.contains("private val APP_URL = \"https://vivekashokan007-cloud.github.io/MarketVivi/\""))
        assertEquals(CandidatePwaRoute.DOCUMENT_BASE, "https://vivekashokan007-cloud.github.io/MarketVivi/")
        assertTrue(a.contains("candidatePwa?.intercept(request, instanceSeq, \"\$createKind\") ?: super.shouldInterceptRequest(view, request)"))
        // candidatePwa is only ever assigned inside setUpCandidatePwa, which is only called under BuildConfig.PWA_CANDIDATE.
        assertEquals(1, Regex("""candidatePwa = """).findAll(a).count())
        assertTrue(a.contains("if (BuildConfig.PWA_CANDIDATE) setUpCandidatePwa(savedInstanceState != null)"))
        assertEquals(1, Regex("""setUpCandidatePwa\(""").findAll(a).count() - 1)
        assertEquals(1, Regex("""CandidatePwaServer\.obtain""").findAll(a).count())
        // No load of a candidate-specific origin, file:// or appassets URL: the document URL stays the production one.
        for (bad in listOf("appassets.androidplatform.net", "file:///android_asset", "WebViewAssetLoader", "loadDataWithBaseURL"))
            assertFalse(bad, a.contains(bad))
        // Candidate never offers the normal update and never writes the web-version pref.
        val upd = a.substring(a.indexOf("private fun checkForUpdates(isManual: Boolean) {")).let { it.substring(0, it.indexOf("Request.Builder()")) }
        assertTrue(upd.contains("if (BuildConfig.PWA_CANDIDATE) {") && upd.contains("            return\n"))
        assertTrue(a.contains("if (BuildConfig.PWA_CANDIDATE) logCandidatePage(view, url, version)\n                        else if (version.isNotEmpty()) checkVersionUpdate(version)"))
    }

    @Test fun serviceWorkerFetchesUseTheSameBundledRouteAndPageReportsSwControl() {
        val a = src("MainActivity.kt")
        val cand = a.substring(a.indexOf("private fun setUpCandidatePwa("), a.indexOf("private fun hideSplashAfterDelay()"))
        assertTrue(cand.contains("ServiceWorkerController.getInstance().setServiceWorkerClient(object : ServiceWorkerClient() {"))
        assertTrue(cand.contains("server.intercept(request, seq, kind, \"service_worker\")"))
        assertTrue(cand.contains("PWA_CANDIDATE_SW_CLIENT: unavailable"))
        // A page still controlled by a worker is logged as an error so the phone test catches it.
        assertTrue(cand.contains("navigator.serviceWorker.controller") && cand.contains("if (sw == \"none\") 'I' else 'E'"))
        // No sw script is bundled, so a registration attempt hits the explicit in-scope 404, never live bytes.
        assertFalse(CandidatePwaRoute.RUNTIME_FILES.any { it.startsWith("sw") || it.contains("worker") })
        // Real bundled index.html (when the pinned checkout is available) unregisters workers and drops Cache Storage.
        val mv = System.getenv("MARKETVIVI_DIR")?.let { File(it) }?.takeIf { it.isDirectory }
            ?: return println("SKIPPED real index.html SW check: MARKETVIVI_DIR not set")
        val idx = File(mv, "index.html").readText()
        assertTrue(idx.contains("getRegistrations") && idx.contains("unregister") && idx.contains("caches"))
        assertFalse(idx.contains("serviceWorker.register("))
    }

    @Test fun candidateCodeNeverClearsDataStorageOrCookies() {
        val clearing = listOf("clearCache", "clearHistory", "clearFormData", "clearMatches", "clearSslPreferences", "deleteDatabase",
            "WebStorage", "deleteAllData", "CookieManager", "removeAllCookie", "clearApplicationUserData", "setDataDirectorySuffix",
            "deleteSharedPreferences", ".clear()", ".edit()", "deleteRecursively", ".delete()", "evaluateJavascript(\"localStorage")
        for (n in listOf("CandidatePwa.kt", "CandidatePwaServer.kt")) {
            val s = src(n)
            for (c in clearing) assertFalse("$n contains $c", s.contains(c))
        }
        val a = src("MainActivity.kt")
        val cand = a.substring(a.indexOf("private fun setUpCandidatePwa("), a.indexOf("private fun hideSplashAfterDelay()"))
        for (c in clearing) assertFalse("candidate MainActivity code contains $c", cand.contains(c))
        // Whole MainActivity: no storage/cookie/profile wipes at all; the only cache call is the pre-existing HTTP-cache trim.
        for (c in listOf("deleteDatabase", "WebStorage", "CookieManager", "clearApplicationUserData", "setDataDirectorySuffix", "clearHistory", "clearFormData"))
            assertFalse(c, a.contains(c))
        assertEquals("only the pre-existing onTrimMemory HTTP-cache trim", 1, Regex("""clearCache\(""").findAll(a).count())
        assertTrue(a.contains("webView.clearCache(false)"))   // false = keep disk files; never storage
    }

    @Test fun candidateAssetsAreNeverCommitted() {
        val dir = file("src/candidate")
        val committed = dir.walkTopDown().filter { it.isFile }.map { it.relativeTo(dir).path }.toList()
        assertEquals(listOf("pwa_expected_manifest.json"), committed)
    }

    companion object {
        const val PWA = "aabccc26b828b03935ff93f3b54ae50c97ce9e84"
        const val NATIVE = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
        val FAKE: Map<String, ByteArray> = CandidatePwaRoute.RUNTIME_FILES.associateWith { "/* bundled $it */\n".toByteArray() }
        fun sha(b: ByteArray) = CandidatePwaManifest.sha256Hex(b)
        fun manifest(edit: (JSONObject) -> Unit = {}): ByteArray {
            val files = JSONObject()
            for ((n, b) in FAKE) files.put(n, JSONObject().put("sha256", sha(b)).put("bytes", b.size))
            val m = JSONObject().put("schema", CandidatePwaManifest.SCHEMA).put("marketvivi_sha", PWA)
                .put("document_base", CandidatePwaRoute.DOCUMENT_BASE).put("files", files)
            edit(m)
            return m.toString().toByteArray()
        }
        fun verifiedState(): CandidatePwaManifest.Result = CandidatePwaManifest.verify(manifest(), sha(manifest()), PWA) { FAKE[it] }
        fun pinnedBytes(name: String): ByteArray? =
            System.getenv("MARKETVIVI_DIR")?.let { File(it, name) }?.takeIf { it.isFile }?.readBytes()
    }
}
