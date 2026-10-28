package com.marketradar.app

import android.content.Context
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import com.marketradar.app.util.LogBuffer
import java.io.ByteArrayInputStream
import java.io.IOException

/**
 * b515 CANDIDATE-ONLY (DO NOT MERGE): Android adapter that serves the hash-verified bundled PWA runtime for
 * https://vivekashokan007-cloud.github.io/MarketVivi/ from APK assets (assets/pwa_candidate/), on the production origin.
 *
 * Constructed by MainActivity ONLY when BuildConfig.PWA_CANDIDATE is true (candidate build type). Debug/release never
 * construct it, so their WebView requests are untouched. It never clears or migrates any data, cookie or storage.
 * Verification runs once per process (cold boot); every Activity instance (including restored/re-created ones) logs
 * the identity again with its own seq/kind.
 */
class CandidatePwaServer private constructor(
    val state: CandidatePwaManifest.Result,
    private val verifiedThisCall: Boolean,
) {
    val verified: Boolean get() = state is CandidatePwaManifest.Result.Verified

    companion object {
        private const val TAG = "MainActivity"
        private var cached: CandidatePwaManifest.Result? = null

        @Synchronized
        fun obtain(context: Context): CandidatePwaServer {
            val first = cached == null
            val st = cached ?: verifyPackaged(context.applicationContext).also { cached = it }
            return CandidatePwaServer(st, first)
        }

        private fun verifyPackaged(ctx: Context): CandidatePwaManifest.Result {
            fun read(name: String): ByteArray? = try {
                ctx.assets.open("${CandidatePwaManifest.ASSET_DIR}/$name").use { it.readBytes() }
            } catch (e: IOException) { null }
            return CandidatePwaManifest.verify(read(CandidatePwaManifest.MANIFEST_ASSET), BuildConfig.PWA_MANIFEST_SHA256,
                BuildConfig.PWA_SOURCE_HEAD) { read(it) }
        }
    }

    /** Identity lines for LogBuffer: native SHA, bundled PWA SHA, manifest SHA and per-file runtime sha256. */
    fun identityLines(seq: Int, kind: String, restored: Boolean): List<String> {
        val head = "PWA_CANDIDATE: build=${BuildConfig.BUILD_TYPE} app=${BuildConfig.APPLICATION_ID} " +
            "version=${BuildConfig.VERSION_NAME}/${BuildConfig.VERSION_CODE} native=${BuildConfig.SOURCE_HEAD} " +
            "pwa=${BuildConfig.PWA_SOURCE_HEAD} manifestPin=${BuildConfig.PWA_MANIFEST_SHA256} " +
            "state=${if (verified) "VERIFIED" else "FAILED"} verifiedThisCall=$verifiedThisCall restored=$restored seq=$seq kind=$kind"
        val hashes = when (val s = state) {
            is CandidatePwaManifest.Result.Verified -> s.hashes
            is CandidatePwaManifest.Result.Failed -> s.hashes
        }
        val files = hashes.map { "PWA_CANDIDATE_FILE: name=${it.name} sha256=${it.sha256} bytes=${it.bytes} seq=$seq" }
        val fail = (state as? CandidatePwaManifest.Result.Failed)?.let { listOf("PWA_CANDIDATE_ERROR: bundle_verification_failed reasons=${it.reasons.joinToString("|")} seq=$seq") }.orEmpty()
        return listOf(head) + files + fail
    }

    fun hashOf(file: String): String? = (state as? CandidatePwaManifest.Result.Verified)?.hashes?.firstOrNull { it.name == file }?.sha256

    /** null = not in candidate scope (network as normal). Otherwise bundled bytes or an explicit candidate error. */
    fun intercept(request: WebResourceRequest?, seq: Int, kind: String, via: String = "webview"): WebResourceResponse? {
        val url = request?.url?.toString()
        val decision = CandidatePwaRoute.decide(request?.method, url)
        val plan = CandidatePwaResponses.plan(decision, state, BuildConfig.SOURCE_HEAD) ?: return null
        val mainFrame = request?.isForMainFrame == true
        if (plan.candidateError == null) {
            val file = (decision as CandidatePwaRoute.Decision.Serve).file
            LogBuffer.add('I', TAG, "PWA_CANDIDATE_SERVE: url=$url file=$file sha256=${hashOf(file)} bytes=${plan.body.size} " +
                "status=${plan.status} mainFrame=$mainFrame via=$via seq=$seq kind=$kind")
        } else {
            LogBuffer.add('E', TAG, "PWA_CANDIDATE_ERROR: url=$url status=${plan.status} detail=${plan.candidateError} " +
                "mainFrame=$mainFrame via=$via seq=$seq kind=$kind")
        }
        return WebResourceResponse(plan.mimeType, plan.encoding, plan.status, plan.reason, plan.headers, ByteArrayInputStream(plan.body))
    }
}
