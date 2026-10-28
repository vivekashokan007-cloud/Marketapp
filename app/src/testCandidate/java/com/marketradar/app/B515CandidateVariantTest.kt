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
        assertEquals(515, BuildConfig.VERSION_CODE)
        assertEquals("2.6.84", BuildConfig.VERSION_NAME)
        assertEquals("6cb5ad51376314b9dbff0cb1df3e0e08aff75df3", BuildConfig.PWA_SOURCE_HEAD)
        val pin = listOf(File("src/candidate/pwa_expected_manifest.json"), File("app/src/candidate/pwa_expected_manifest.json")).first { it.isFile }
        assertEquals(CandidatePwaManifest.sha256Hex(pin.readBytes()), BuildConfig.PWA_MANIFEST_SHA256)
    }

    @Test fun stagedBundleVerifiesWithTheRealBuildConfigPin() {
        val dir = listOf(File("build/generated/candidatePwa/assets/pwa_candidate"), File("app/build/generated/candidatePwa/assets/pwa_candidate"))
            .first { it.isDirectory }
        val r = CandidatePwaManifest.verify(File(dir, CandidatePwaManifest.MANIFEST_ASSET).readBytes(), BuildConfig.PWA_MANIFEST_SHA256,
            BuildConfig.PWA_SOURCE_HEAD) { File(dir, it).takeIf { f -> f.isFile }?.readBytes() }
        assertTrue("$r", r is CandidatePwaManifest.Result.Verified)
        assertEquals("1241d33ad57280ecd6bb994fd4c9e332a7bacbfa9e4a898b18c5ee511e9286f0",
            (r as CandidatePwaManifest.Result.Verified).hashes.first { it.name == "app.js" }.sha256)
    }
}
