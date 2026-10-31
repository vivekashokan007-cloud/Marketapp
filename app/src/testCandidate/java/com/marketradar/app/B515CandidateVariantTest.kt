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
        assertEquals(516, BuildConfig.VERSION_CODE)
        assertEquals("2.6.85", BuildConfig.VERSION_NAME)
        assertEquals("4b0d49f468e6100830967b842114e0322bb68535", BuildConfig.PWA_SOURCE_HEAD)
        val pin = listOf(File("src/candidate/pwa_expected_manifest.json"), File("app/src/candidate/pwa_expected_manifest.json")).first { it.isFile }
        assertEquals(CandidatePwaManifest.sha256Hex(pin.readBytes()), BuildConfig.PWA_MANIFEST_SHA256)
    }

    @Test fun stagedBundleVerifiesWithTheRealBuildConfigPin() {
        val dir = listOf(File("build/generated/candidatePwa/assets/pwa_candidate"), File("app/build/generated/candidatePwa/assets/pwa_candidate"))
            .first { it.isDirectory }
        val r = CandidatePwaManifest.verify(File(dir, CandidatePwaManifest.MANIFEST_ASSET).readBytes(), BuildConfig.PWA_MANIFEST_SHA256,
            BuildConfig.PWA_SOURCE_HEAD) { File(dir, it).takeIf { f -> f.isFile }?.readBytes() }
        assertTrue("$r", r is CandidatePwaManifest.Result.Verified)
        assertEquals("ca0d7a352939e93cd67bb12dce7a37c575748523731796c6d886d2d3b0df426b",
            (r as CandidatePwaManifest.Result.Verified).hashes.first { it.name == "app.js" }.sha256)
    }
}
