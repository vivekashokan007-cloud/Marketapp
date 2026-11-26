package com.marketradar.app

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.widget.*
import org.json.JSONObject
import java.util.concurrent.Executors

/** Native app-account setup. Secrets never pass through the JS bridge or exports. */
class CloudSetupActivity: Activity() {
    private val io = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var note: TextView
    private val refresh = object: Runnable {
        override fun run() { renderStatus(); handler.postDelayed(this, 2000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 32, 32, 32) }
        layout.addView(TextView(this).apply { text = "Market Radar cloud recovery"; textSize = 23f })
        layout.addView(TextView(this).apply {
            text = "Use the same app account on your next phone. This is separate from Supabase dashboard sign-in. " +
                "Files, Paper state, history and settings use a private Supabase checkpoint. " +
                "Pending uploads remain on this phone. Broker login must be entered again."
        })
        status = TextView(this); layout.addView(status)
        val email = EditText(this).apply { hint = "App account email"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS }
        val password = EditText(this).apply { hint = "App account password"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD }
        layout.addView(email); layout.addView(password)
        note = TextView(this); layout.addView(note)
        fun action(label: String, block: () -> Unit) { layout.addView(Button(this).apply { text = label; setOnClickListener { block() } }) }
        fun authenticate(signup: Boolean) {
            val e = email.text.toString().trim(); val p = password.text.toString()
            password.text.clear(); note.text = "Checking app account…"
            io.execute {
                try {
                    val session = CloudSession(applicationContext)
                    if (signup && !session.signUp(e, p)) {
                        runOnUiThread { note.text = "Check the confirmation email, then return here and sign in. No checkpoint is confirmed yet." }
                    } else {
                        if (!signup) session.signIn(e, p)
                        CloudRecovery.runPass(applicationContext)
                        runOnUiThread { note.text = "Account checked. Review checkpoint status below before continuing."; renderStatus() }
                    }
                } catch (ex: Exception) {
                    runOnUiThread { note.text = ex.message?.takeIf { it.matches(Regex("[A-Z0-9_]+")) } ?: "Account check failed; retry after checking your connection." }
                }
            }
        }
        action("Sign in to app account") { authenticate(false) }
        action("Create app account") { authenticate(true) }
        action("Retry cloud sync") { CloudRecovery.request(applicationContext); note.text = "Sync requested. Keep this screen open during first upload." }
        action("Continue to Paper app") {
            if (CloudRecovery.canOpen()) { startActivity(Intent(this, MainActivity::class.java)); finish() }
            else note.text = "Finish sign-in and verified recovery first. Existing phone data has been retained."
        }
        action("Open local Paper cache — backup incomplete") {
            if (CloudRecovery.openLocalPaperCache(applicationContext)) {
                startActivity(Intent(this, MainActivity::class.java)); finish()
            } else note.text = "A restore is unfinished. Finish recovery before using this cache."
        }
        layout.addView(TextView(this).apply {
            text = "Keep your exported backup until a fresh-device restore has been verified. " +
                "This screen never closes a trade or replays an entry request."
        })
        setContentView(ScrollView(this).apply { addView(layout) })
        CloudRecovery.start(applicationContext)
        handler.post(refresh)
    }
    private fun renderStatus() {
        if (!::status.isInitialized) return
        val s = JSONObject(CloudRecovery.status(applicationContext))
        val description = when(s.optString("state")) {
            "APP_SIGN_IN_REQUIRED" -> "Sign into your app account to back up or restore."
            "RESTORING_VERIFIED_CLOUD_STATE" -> "Restoring your verified cloud data. Monitoring is paused."
            "CHECKING_PENDING_CACHE" -> "Checking saved files: ${s.optInt("files_checked")} / ${s.optInt("files_total")}"
            "UPLOADING_RECOVERY_DATA", "UPLOADING_CHECKPOINT" -> "Uploading and verifying: ${s.optInt("files_checked")} / ${s.optInt("files_total")} files. Keep the app open."
            "PWA_CAPTURE_REQUIRED" -> "Native data is saved. Continue to the Paper app to include its saved screen data."
            "VERIFIED_CHECKPOINT" -> "Cloud checkpoint verified."
            "LOCAL_CHANGES_PENDING_UPLOAD" -> "New changes are waiting to upload. Keep this phone's data."
            "LOCAL_PAPER_CACHE_NOT_BACKED_UP" -> "Local Paper only. Cloud backup is incomplete; automatic monitoring is paused."
            else -> "Cloud backup or restore is pending. Keep the phone export and retry."
        }
        status.text = "\n$description\nLast verified cloud checkpoint: ${s.optString("verified_at").ifBlank { "none" }}" +
            "\nVerified: ${s.optInt("verified_files")} files · ${s.optLong("verified_bytes") / (1024 * 1024)} MiB" +
            "\nCheckpoint: ${s.optString("checkpoint_sha256").take(12)}" +
            "\nRevision ${s.optLong("revision")} · monitoring ${if (s.optBoolean("monitor_ready")) "ready" else "paused"}" +
            "\n${s.optString("last_error")}\n"
    }
    override fun onDestroy() { handler.removeCallbacks(refresh); io.shutdown(); super.onDestroy() }
}
