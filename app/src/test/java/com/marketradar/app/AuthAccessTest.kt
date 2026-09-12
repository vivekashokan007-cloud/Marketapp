package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Base64

class AuthAccessTest {
    private val anonKey = "anon-publishable-key"

    private fun jwtForRole(role: String): String {
        val header = Base64.getUrlEncoder().withoutPadding()
            .encodeToString("""{"alg":"HS256","typ":"JWT"}""".toByteArray())
        val payload = Base64.getUrlEncoder().withoutPadding()
            .encodeToString("""{"role":"$role","sub":"user-1"}""".toByteArray())
        return "$header.$payload.sig"
    }

    @Test
    fun flagOffAlwaysUsesAnonEvenWhenJwtPresent() {
        val user = jwtForRole("authenticated")
        assertEquals(anonKey, AuthAccess.resolveBearerToken(anonKey, user, enabled = false))
        assertEquals("anon", AuthAccess.bearerMode(user, enabled = false))
    }

    @Test
    fun flagOnFallsBackToAnonWhenJwtMissing() {
        assertEquals(anonKey, AuthAccess.resolveBearerToken(anonKey, "", enabled = true))
        assertEquals("anon", AuthAccess.bearerMode("", enabled = true))
    }

    @Test
    fun flagOnUsesUserJwtWhenPresent() {
        val user = jwtForRole("authenticated")
        assertEquals(user, AuthAccess.resolveBearerToken(anonKey, user, enabled = true))
        assertEquals("user_jwt", AuthAccess.bearerMode(user, enabled = true))
    }

    @Test
    fun serviceRoleJwtIsRejected() {
        val secret = jwtForRole("service_role")
        assertTrue(AuthAccess.isDisallowedClientSecret(secret))
        assertEquals(anonKey, AuthAccess.resolveBearerToken(anonKey, secret, enabled = true))
    }

    @Test
    fun modernSecretPrefixIsRejected() {
        assertTrue(AuthAccess.isDisallowedClientSecret("sb_secret_example"))
        assertEquals(
            anonKey,
            AuthAccess.resolveBearerToken(anonKey, "sb_secret_example", enabled = true)
        )
    }

    @Test
    fun defaultFlagRemainsOff() {
        assertFalse(AuthAccess.DEFAULT_SESSION_ENABLED)
        assertEquals("anon_write_until_auth_cutover", AuthAccess.CONTAINMENT)
    }

    @Test
    fun supabaseClientUsesAuthAccessAndNeverEmbedsServiceRole() {
        val candidates = listOf(
            java.io.File("src/main/java/com/marketradar/app/SupabaseClient.kt"),
            java.io.File("app/src/main/java/com/marketradar/app/SupabaseClient.kt")
        )
        val source = candidates.first { it.exists() }.readText()
        assertTrue(source.contains("AuthAccess.resolveBearerToken(ANON_KEY)"))
        assertTrue(source.contains(".addHeader(\"apikey\", ANON_KEY)"))
        assertFalse(source.contains("SERVICE_ROLE"))
        assertFalse(source.contains("service_role"))
    }
}
