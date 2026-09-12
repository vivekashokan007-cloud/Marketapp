package com.marketradar.app

import org.json.JSONObject

/**
 * G1 Phase 1b — smallest coordinated step toward authenticated Supabase access.
 *
 * Default is OFF. [resolveBearerToken] keeps using the publishable anon key until
 * a user JWT is stored AND the session flag is enabled. The APK/PWA must never
 * ship or accept a service_role credential.
 */
object AuthAccess {
    const val PREF_USER_ACCESS_TOKEN = "g1_supabase_user_access_token"
    const val PREF_SESSION_ENABLED = "g1_auth_session_enabled"
    const val DEFAULT_SESSION_ENABLED = false
    const val CONTAINMENT = "anon_write_until_auth_cutover"

    @Volatile
    private var sessionEnabled: Boolean = DEFAULT_SESSION_ENABLED

    @Volatile
    private var userAccessToken: String = ""

    fun isSessionEnabled(): Boolean = sessionEnabled

    fun hasUserJwt(): Boolean = userAccessToken.isNotBlank()

    fun setSessionEnabled(enabled: Boolean) {
        sessionEnabled = enabled
    }

    fun setUserAccessToken(token: String) {
        val trimmed = token.trim()
        userAccessToken = if (trimmed.isEmpty() || isDisallowedClientSecret(trimmed)) {
            ""
        } else {
            trimmed
        }
    }

    fun clearUserAccessToken() {
        userAccessToken = ""
    }

    fun resolveBearerToken(
        anonKey: String,
        userJwt: String = userAccessToken,
        enabled: Boolean = sessionEnabled
    ): String {
        if (!enabled) return anonKey
        val trimmed = userJwt.trim()
        if (trimmed.isEmpty()) return anonKey
        if (isDisallowedClientSecret(trimmed)) return anonKey
        return trimmed
    }

    fun bearerMode(
        userJwt: String = userAccessToken,
        enabled: Boolean = sessionEnabled
    ): String {
        val resolved = resolveBearerToken("ANON", userJwt, enabled)
        return if (resolved == "ANON") "anon" else "user_jwt"
    }

    fun isDisallowedClientSecret(token: String): Boolean {
        val trimmed = token.trim()
        if (trimmed.isEmpty()) return false
        if (trimmed.startsWith("sb_secret_")) return true
        val role = jwtRoleClaim(trimmed) ?: return trimmed.contains("service_role")
        return role == "service_role" || role == "supabase_admin"
    }

    fun jwtRoleClaim(token: String): String? {
        val parts = token.split('.')
        if (parts.size < 2) return null
        return try {
            val padded = parts[1]
                .replace('-', '+')
                .replace('_', '/')
                .let { chunk -> chunk + "=".repeat((4 - chunk.length % 4) % 4) }
            val json = String(java.util.Base64.getDecoder().decode(padded), Charsets.UTF_8)
            JSONObject(json).optString("role", "").takeIf { it.isNotBlank() }
        } catch (_: Exception) {
            null
        }
    }

    fun statusJson(): String {
        return JSONObject()
            .put("enabled", sessionEnabled)
            .put("has_user_jwt", hasUserJwt())
            .put("bearer_mode", bearerMode())
            .put("flag_default", DEFAULT_SESSION_ENABLED)
            .put("containment", CONTAINMENT)
            .toString()
    }
}
