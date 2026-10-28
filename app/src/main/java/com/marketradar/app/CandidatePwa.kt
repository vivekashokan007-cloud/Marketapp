package com.marketradar.app

import org.json.JSONObject
import java.net.URI
import java.security.MessageDigest

/**
 * b515 CANDIDATE-ONLY (DO NOT MERGE): pure routing, response planning and hash-manifest verification for the
 * bundled-PWA candidate build type. No Android imports, so every rule is JVM unit-tested.
 *
 * Only active when BuildConfig.PWA_CANDIDATE is true (the `candidate` build type). In debug/release the
 * MainActivity hook never constructs [CandidatePwaServer], so nothing here runs and no request is intercepted.
 *
 * Scope: GET/HEAD https://vivekashokan007-cloud.github.io/MarketVivi/<runtime file> only. The document URL,
 * origin and WebView profile stay the production ones, so existing localStorage/IndexedDB/cookies are used as is.
 */
object CandidatePwaRoute {
    const val HOST = "vivekashokan007-cloud.github.io"
    const val BASE_PATH = "/MarketVivi/"
    const val DOCUMENT_BASE = "https://$HOST$BASE_PATH"
    const val INDEX = "index.html"
    /** Same six files, same order, as MarketVivi tools/build_pages_site.py RUNTIME_FILES. */
    val RUNTIME_FILES: List<String> = listOf("index.html", "app.js", "api.js", "log-viewer.js", "style.css", "manifest.json")

    sealed class Decision {
        /** Not ours: let the WebView use the network exactly as the normal app does (APIs, CDNs, fonts, other paths). */
        object PassThrough : Decision()
        /** In scope and bundled: serve these verified bytes. */
        data class Serve(val file: String, val mimeType: String) : Decision()
        /** In scope (/MarketVivi/ on the production host) but not a bundled runtime file: explicit candidate error, never live. */
        data class NotBundled(val path: String, val why: String) : Decision()
    }

    fun mimeTypeOf(file: String): String = when (file.substringAfterLast('.', "")) {
        "html" -> "text/html"
        "js" -> "text/javascript"
        "css" -> "text/css"
        "json" -> if (file == "manifest.json") "application/manifest+json" else "application/json"
        else -> "application/octet-stream"
    }

    fun decide(method: String?, url: String?): Decision {
        if (url == null) return Decision.PassThrough
        val u = try { URI(url) } catch (e: Exception) { return Decision.PassThrough }
        if (!"https".equals(u.scheme, ignoreCase = true)) return Decision.PassThrough
        if (u.rawUserInfo != null) return Decision.PassThrough
        if (!HOST.equals(u.host, ignoreCase = true)) return Decision.PassThrough
        if (u.port != -1 && u.port != 443) return Decision.PassThrough
        val m = (method ?: "GET").uppercase()
        if (m != "GET" && m != "HEAD") return Decision.PassThrough
        val path = u.rawPath ?: return Decision.PassThrough
        // Outside /MarketVivi/ on the same host (other repos, /favicon.ico, the slash-less redirect) is not ours.
        if (!path.regionMatches(0, BASE_PATH, 0, BASE_PATH.length, ignoreCase = true)) return Decision.PassThrough
        // From here on the request is inside the candidate scope: it is served from the bundle or refused, never live.
        if (!path.startsWith(BASE_PATH)) return Decision.NotBundled(path, "path_case")
        val rest = path.substring(BASE_PATH.length)
        if (rest.contains('%') || rest.contains('/') || rest.contains('\\') || rest == "." || rest == "..")
            return Decision.NotBundled(path, "not_a_bundled_file")
        val file = if (rest.isEmpty()) INDEX else rest
        if (file !in RUNTIME_FILES) return Decision.NotBundled(path, "not_in_runtime_allowlist")
        return Decision.Serve(file, mimeTypeOf(file))
    }
}

/** A planned HTTP response; [CandidatePwaServer] turns it into a WebResourceResponse. */
data class CandidatePwaResponse(
    val status: Int,
    val reason: String,
    val mimeType: String,
    val encoding: String?,
    val headers: Map<String, String>,
    val body: ByteArray,
    val candidateError: String?,
)

object CandidatePwaResponses {
    /** Errors stay below 500 so MainActivity's generic "No internet connection" view does not hide the explicit candidate page. */
    const val STATUS_NOT_BUNDLED = 404
    const val STATUS_INTEGRITY = 409

    fun headers(pwaSha: String, nativeSha: String): Map<String, String> = linkedMapOf(
        "Cache-Control" to "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma" to "no-cache",
        "X-Content-Type-Options" to "nosniff",
        "X-MarketRadar-Candidate" to "native=$nativeSha pwa=$pwaSha",
    )

    /** Returns null ONLY for PassThrough. Every in-scope request gets bundled bytes or an explicit candidate error. */
    fun plan(decision: CandidatePwaRoute.Decision, state: CandidatePwaManifest.Result, nativeSha: String): CandidatePwaResponse? = when (decision) {
        is CandidatePwaRoute.Decision.PassThrough -> null
        is CandidatePwaRoute.Decision.NotBundled ->
            error(STATUS_NOT_BUNDLED, "Not Bundled", "not bundled in candidate: ${decision.path} (${decision.why})", state.pwaShaOrNone(), nativeSha)
        is CandidatePwaRoute.Decision.Serve -> when (state) {
            is CandidatePwaManifest.Result.Failed ->
                error(STATUS_INTEGRITY, "Candidate Bundle Integrity Failure", "bundle verification failed: ${state.reasons.joinToString("; ")}", state.pwaShaOrNone(), nativeSha)
            is CandidatePwaManifest.Result.Verified -> {
                val bytes = state.files[decision.file]
                if (bytes == null) error(STATUS_INTEGRITY, "Candidate Asset Missing", "required bundled asset missing: ${decision.file}", state.marketviviSha, nativeSha)
                else CandidatePwaResponse(200, "OK", decision.mimeType, "utf-8", headers(state.marketviviSha, nativeSha), bytes, null)
            }
        }
    }

    private fun error(status: Int, reason: String, detail: String, pwaSha: String, nativeSha: String): CandidatePwaResponse {
        val safe = detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        val html = "<!doctype html><meta charset=utf-8><title>Market Radar CANDIDATE error</title>" +
            "<body style=\"font-family:sans-serif;padding:24px\"><h2>Market Radar CANDIDATE build: bundled PWA error</h2>" +
            "<p>$safe</p><p>The candidate never falls back to the live site. Stop the test and report this screen.</p>" +
            "<p>native=$nativeSha pwa=$pwaSha</p></body>"
        return CandidatePwaResponse(status, reason, "text/html", "utf-8", headers(pwaSha, nativeSha), html.toByteArray(Charsets.UTF_8), detail)
    }

    private fun CandidatePwaManifest.Result.pwaShaOrNone() = when (this) {
        is CandidatePwaManifest.Result.Verified -> marketviviSha
        is CandidatePwaManifest.Result.Failed -> "unverified"
    }
}

object CandidatePwaManifest {
    const val SCHEMA = "marketradar.pwa_candidate.v1"
    const val ASSET_DIR = "pwa_candidate"
    const val MANIFEST_ASSET = "candidate_manifest.json"
    private val SHA40 = Regex("^[0-9a-f]{40}$")
    private val SHA64 = Regex("^[0-9a-f]{64}$")

    data class FileHash(val name: String, val sha256: String, val bytes: Int)

    sealed class Result {
        data class Verified(val marketviviSha: String, val manifestSha256: String, val hashes: List<FileHash>,
                            val files: Map<String, ByteArray>) : Result()
        data class Failed(val reasons: List<String>, val hashes: List<FileHash>) : Result()
    }

    fun sha256Hex(b: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

    /**
     * Verifies the packaged manifest against the build-time pin and every runtime file against the manifest.
     * [read] returns the packaged bytes of one file or null when it is absent. Fails closed on any difference.
     */
    fun verify(manifestBytes: ByteArray?, expectedManifestSha256: String, expectedMarketviviSha: String,
               read: (String) -> ByteArray?): Result {
        val reasons = mutableListOf<String>()
        val hashes = mutableListOf<FileHash>()
        if (manifestBytes == null) return Result.Failed(listOf("manifest_missing"), hashes)
        val mSha = sha256Hex(manifestBytes)
        if (!SHA64.matches(expectedManifestSha256)) reasons += "build_manifest_pin_invalid"
        else if (mSha != expectedManifestSha256) reasons += "manifest_sha256_mismatch got=$mSha"
        val json = try { JSONObject(String(manifestBytes, Charsets.UTF_8)) } catch (e: Exception) {
            return Result.Failed(reasons + "manifest_unparseable", hashes)
        }
        if (json.optString("schema") != SCHEMA) reasons += "manifest_schema"
        if (json.optString("document_base") != CandidatePwaRoute.DOCUMENT_BASE) reasons += "manifest_document_base"
        val sha = json.optString("marketvivi_sha")
        if (!SHA40.matches(sha)) reasons += "manifest_marketvivi_sha_invalid"
        if (sha != expectedMarketviviSha) reasons += "manifest_marketvivi_sha_differs_from_build"
        val files = json.optJSONObject("files")
        val names = files?.keys()?.asSequence()?.toSortedSet() ?: sortedSetOf()
        if (names != CandidatePwaRoute.RUNTIME_FILES.toSortedSet()) reasons += "manifest_files_not_runtime_allowlist $names"
        val served = linkedMapOf<String, ByteArray>()
        for (name in CandidatePwaRoute.RUNTIME_FILES) {
            val entry = files?.optJSONObject(name)
            val want = entry?.optString("sha256").orEmpty()
            val wantBytes = entry?.optInt("bytes", -1) ?: -1
            val data = try { read(name) } catch (e: Exception) { null }
            if (data == null) { reasons += "asset_missing $name"; continue }
            val got = sha256Hex(data)
            hashes += FileHash(name, got, data.size)
            if (!SHA64.matches(want)) reasons += "manifest_entry_invalid $name"
            else if (got != want || data.size != wantBytes) reasons += "asset_sha256_mismatch $name got=$got"
            else served[name] = data
        }
        return if (reasons.isEmpty()) Result.Verified(sha, mSha, hashes, served) else Result.Failed(reasons, hashes)
    }
}
