package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** b515 CANDIDATE-ONLY: compiled only into testCandidateUnitTest. Proves the candidate variant's real BuildConfig. */
class B515CandidateVariantTest {
    @Test fun candidateVariantIdentity() {
        assertTrue(BuildConfig.PWA_CANDIDATE)
        assertEquals("candidate", BuildConfig.BUILD_TYPE)
        assertEquals("com.marketradar.app", BuildConfig.APPLICATION_ID)   // no suffix: installs in place, same data
        assertFalse(BuildConfig.DEBUG)
        assertEquals(517, BuildConfig.VERSION_CODE)
        assertEquals("2.6.86", BuildConfig.VERSION_NAME)
        assertEquals("877b02fd6d703ca43148a0f119b60216d9b509ce", BuildConfig.PWA_SOURCE_HEAD)
        val pin = listOf(File("src/candidate/pwa_expected_manifest.json"), File("app/src/candidate/pwa_expected_manifest.json")).first { it.isFile }
        assertEquals(CandidatePwaManifest.sha256Hex(pin.readBytes()), BuildConfig.PWA_MANIFEST_SHA256)
    }

    @Test fun stagedBundleVerifiesWithTheRealBuildConfigPin() {
        val dir = listOf(File("build/generated/candidatePwa/assets/pwa_candidate"), File("app/build/generated/candidatePwa/assets/pwa_candidate"))
            .first { it.isDirectory }
        val r = CandidatePwaManifest.verify(File(dir, CandidatePwaManifest.MANIFEST_ASSET).readBytes(), BuildConfig.PWA_MANIFEST_SHA256,
            BuildConfig.PWA_SOURCE_HEAD) { File(dir, it).takeIf { f -> f.isFile }?.readBytes() }
        assertTrue("$r", r is CandidatePwaManifest.Result.Verified)
        assertEquals("004a9b9de17455165d79e53c8b2e70a92797f798d15c3226c0172d2c92a25230",
            (r as CandidatePwaManifest.Result.Verified).hashes.first { it.name == "app.js" }.sha256)
    }
}
