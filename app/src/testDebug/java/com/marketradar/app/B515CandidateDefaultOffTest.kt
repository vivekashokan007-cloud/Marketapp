package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test

/** b515 CANDIDATE-ONLY: the normal (debug/release-default) build has bundled routing OFF and the unchanged identity. */
class B515CandidateDefaultOffTest {
    @Test fun nonCandidateBuildDoesNotIntercept() {
        assertFalse(BuildConfig.PWA_CANDIDATE)
        assertEquals("none", BuildConfig.PWA_SOURCE_HEAD)
        assertEquals("none", BuildConfig.PWA_MANIFEST_SHA256)
        assertEquals("com.marketradar.app", BuildConfig.APPLICATION_ID)
        assertEquals("debug", BuildConfig.BUILD_TYPE)
        // With the flag off MainActivity never builds a CandidatePwaServer, so its hook returns super (null) for all URLs.
        // And even the pure route/verify path, given an unconfigured pin, can never verify a bundle.
        val r = CandidatePwaManifest.verify(B515CandidatePwaTest.manifest(), BuildConfig.PWA_MANIFEST_SHA256, BuildConfig.PWA_SOURCE_HEAD) {
            B515CandidatePwaTest.FAKE[it]
        }
        assertTrue(r is CandidatePwaManifest.Result.Failed)
    }
}
