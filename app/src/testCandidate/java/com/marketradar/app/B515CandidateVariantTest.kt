package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** b515 CANDIDATE-ONLY: compiled only into testCandidateUnitTest. Proves the candidate variant's real BuildConfig. */
class B515CandidateVariantTest {
    private fun pinFile() = listOf(
        File("src/candidate/pwa_expected_manifest.json"), File("app/src/candidate/pwa_expected_manifest.json")
    ).first { it.isFile }

    /**
     * Derived from the committed pin, never re-typed. This test previously carried its own literals
     * (877b02fd / 517 / 2.6.86) and, because testCandidateUnitTest runs only in the manually dispatched candidate
     * workflow, they sat two identity generations stale without failing anything. The accepted identity is typed
     * exactly once, in B515CandidatePwaTest, which compiles into this variant too — so that literal is enforced here
     * as well, and this test proves the real BuildConfig equals the pin without adding another copy of it.
     */
    @Test fun candidateVariantIdentity() {
        assertTrue(BuildConfig.PWA_CANDIDATE)
        assertEquals("candidate", BuildConfig.BUILD_TYPE)
        assertEquals("com.marketradar.app", BuildConfig.APPLICATION_ID)   // no suffix: installs in place, same data
        assertFalse(BuildConfig.DEBUG)
        val pin = pinFile()
        val obj = JSONObject(pin.readText())
        val id = obj.getJSONObject("pwa_identity")
        assertEquals(id.getInt("build"), BuildConfig.VERSION_CODE)
        assertEquals(id.getString("version"), BuildConfig.VERSION_NAME)
        assertEquals(obj.getString("marketvivi_sha"), BuildConfig.PWA_SOURCE_HEAD)
        assertEquals(CandidatePwaManifest.sha256Hex(pin.readBytes()), BuildConfig.PWA_MANIFEST_SHA256)
    }

    @Test fun stagedBundleVerifiesWithTheRealBuildConfigPin() {
        val dir = listOf(File("build/generated/candidatePwa/assets/pwa_candidate"), File("app/build/generated/candidatePwa/assets/pwa_candidate"))
            .first { it.isDirectory }
        val r = CandidatePwaManifest.verify(File(dir, CandidatePwaManifest.MANIFEST_ASSET).readBytes(), BuildConfig.PWA_MANIFEST_SHA256,
            BuildConfig.PWA_SOURCE_HEAD) { File(dir, it).takeIf { f -> f.isFile }?.readBytes() }
        assertTrue("$r", r is CandidatePwaManifest.Result.Verified)
        // Cross-checked against the committed pin rather than a typed hash. Weak on its own, since the staged manifest
        // is already bound to BuildConfig.PWA_MANIFEST_SHA256 on the line above; the independent strength comes from
        // B515CandidatePwaTest's reviewed acceptedAppJsSha256, which is enforced in this variant.
        assertEquals(JSONObject(pinFile().readText()).getJSONObject("files").getJSONObject("app.js").getString("sha256"),
            (r as CandidatePwaManifest.Result.Verified).hashes.first { it.name == "app.js" }.sha256)
    }
}
