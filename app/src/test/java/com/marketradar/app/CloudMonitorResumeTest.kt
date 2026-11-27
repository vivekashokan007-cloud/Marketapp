package com.marketradar.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CloudMonitorResumeTest {
    private fun allowed(
        initialized: Boolean = true,
        localRevision: Long = 17,
        remoteRevision: Long = 17,
        localDevice: String = "same-phone",
        remoteDevice: String? = "same-phone",
        localManifestHash: String = "a".repeat(64),
        remoteManifestHash: String = "a".repeat(64),
        hasVerifiedWebEntry: Boolean = true,
        hasLocalWebState: Boolean = true,
        webCaptureFailed: Boolean = false,
        restorePending: Boolean = false
    ) = CloudMonitorResume.eligible(initialized, localRevision, remoteRevision, localDevice,
        remoteDevice, localManifestHash, remoteManifestHash, hasVerifiedWebEntry,
        hasLocalWebState, webCaptureFailed, restorePending)

    @Test fun sameVerifiedPhoneCanResumeMarketCaptureWhileNewFilesUpload() {
        assertTrue(allowed())
        assertFalse(allowed(initialized = false))
        assertFalse(allowed(remoteRevision = 18))
        assertFalse(allowed(remoteDevice = "other-phone"))
        assertFalse(allowed(remoteManifestHash = "b".repeat(64)))
        assertFalse(allowed(hasVerifiedWebEntry = false))
        assertFalse(allowed(hasLocalWebState = false))
        assertFalse(allowed(webCaptureFailed = true))
        assertFalse(allowed(restorePending = true))
    }
}
