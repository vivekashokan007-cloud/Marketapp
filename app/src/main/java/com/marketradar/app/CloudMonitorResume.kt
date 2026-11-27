package com.marketradar.app

/** Narrow admission for resuming monitoring on the same verified phone while
 * newer local files are being uploaded. The remote claim is checked separately.
 */
object CloudMonitorResume {
    fun eligible(
        initialized: Boolean,
        localRevision: Long,
        remoteRevision: Long,
        localDevice: String,
        remoteDevice: String?,
        localManifestHash: String,
        remoteManifestHash: String,
        hasVerifiedWebEntry: Boolean,
        hasLocalWebState: Boolean,
        webCaptureFailed: Boolean,
        restorePending: Boolean
    ): Boolean = initialized && localRevision > 0 && localRevision == remoteRevision &&
        localDevice.isNotBlank() && localDevice == remoteDevice &&
        CloudVault.validHash(localManifestHash) && localManifestHash == remoteManifestHash &&
        hasVerifiedWebEntry && hasLocalWebState && !webCaptureFailed && !restorePending
}
