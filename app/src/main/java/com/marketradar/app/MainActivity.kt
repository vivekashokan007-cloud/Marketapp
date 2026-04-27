package com.marketradar.app

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import java.io.File
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.app.DownloadManager
import android.net.Uri
import android.os.Environment
import android.view.Menu
import android.view.MenuItem
import android.view.ViewGroup.LayoutParams
import android.webkit.*
import android.widget.*
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.appbar.AppBarLayout
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import okhttp3.*
import java.io.IOException
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var loadingOverlay: View
    private lateinit var errorView: View
    private lateinit var topProgressBar: ProgressBar

    private val APP_URL = "https://vivekashokan007-cloud.github.io/MarketVivi/"
    private val UPDATE_URL = "https://api.github.com/repos/vivekashokan007-cloud/Marketapp/releases/latest"
    private val PURPLE = Color.parseColor("#7B2FC4")
    private var isManualRefresh = false
    private val client = OkHttpClient()

    private val pollReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            Log.d("MainActivity", "BROADCAST_RECEIVED: POLL_TICK")
            if (!::webView.isInitialized) return

            val data = intent?.getStringExtra("data")

            webView.post {
                if (data != null) {
                    // Full brain result + spots + candidates from Chaquopy — pass to syncFromNative
                    val escaped = data
                        .replace("\\", "\\\\")
                        .replace("'", "\\'")
                        .replace("\n", "")
                        .replace("\r", "")
                    Log.d("MainActivity", "EVALUATE_JS_CALLED: syncFromNative with data")
                    webView.evaluateJavascript(
                        "(function(){ if(typeof syncFromNative==='function') syncFromNative('$escaped'); else console.warn('[APK] syncFromNative not found'); })()",
                        null
                    )
                } else {
                    // Brain failed or no data, but still wake the UI to refresh poll badge
                    Log.d("MainActivity", "EVALUATE_JS_CALLED: syncFromNative (no data, poll-only wake)")
                    webView.evaluateJavascript(
                        "(function(){ if(typeof syncFromNative==='function') syncFromNative(null); })()",
                        null
                    )
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Request notification permission (Android 13+)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001
                )
            }
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LayoutParams(MATCH_PARENT, MATCH_PARENT)
            fitsSystemWindows = true
            setBackgroundColor(Color.WHITE)
        }

        // ── 0. Material 3 Top Bar ─────────────────────────────────────────
        val appBarLayout = AppBarLayout(this).apply {
            layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
            elevation = 0f
            setBackgroundColor(Color.WHITE)
        }

        val toolbar = MaterialToolbar(this).apply {
            title = "Market Radar"
            setTitleTextColor(PURPLE)
            setBackgroundColor(Color.WHITE)
            
            // Add Settings Icon
            val settingsItem = menu.add(Menu.NONE, 1, Menu.NONE, "Settings")
            settingsItem.setIcon(android.R.drawable.ic_menu_preferences)
            settingsItem.setShowAsAction(MenuItem.SHOW_AS_ACTION_ALWAYS)
            
            setOnMenuItemClickListener { item ->
                if (item.itemId == 1) {
                    showVersionDialog()
                    true
                } else false
            }
        }
        appBarLayout.addView(toolbar)
        container.addView(appBarLayout)

        // ── 1. WebView & Refresh — fills remaining space ──────────────────
        webView = WebView(this).apply {
            layoutParams = LayoutParams(MATCH_PARENT, MATCH_PARENT)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.cacheMode = WebSettings.LOAD_DEFAULT
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            settings.userAgentString = settings.userAgentString + " MarketRadarApp/2.1"
            settings.setSupportZoom(false)
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true

            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(
                    view: WebView?, request: WebResourceRequest?
                ) = false

                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    swipeRefresh.isRefreshing = false
                    hideSplashAfterDelay()
                    injectNativeBridge()

                    // Check if web app version changed since last load
                    view?.evaluateJavascript(
                        "(function() { var s = document.querySelector('script[src*=\"app.js\"]'); return s ? s.getAttribute('src') : ''; })()"
                    ) { result ->
                        val version = result.replace("\"", "").trim()
                        if (version.isNotEmpty()) checkVersionUpdate(version)
                    }
                }

                override fun onReceivedError(
                    view: WebView?, request: WebResourceRequest?, error: WebResourceError?
                ) {
                    super.onReceivedError(view, request, error)
                    if (request?.isForMainFrame == true) {
                        swipeRefresh.isRefreshing = false
                        loadingOverlay.visibility = View.GONE
                        errorView.visibility = View.VISIBLE
                    }
                }

                override fun onReceivedHttpError(
                    view: WebView?, request: WebResourceRequest?, errorResponse: WebResourceResponse?
                ) {
                    super.onReceivedHttpError(view, request, errorResponse)
                    if (request?.isForMainFrame == true && (errorResponse?.statusCode ?: 0) >= 500) {
                        loadingOverlay.visibility = View.GONE
                        errorView.visibility = View.VISIBLE
                    }
                }
            }

            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView?, newProgress: Int) {
                    topProgressBar.progress = newProgress
                    topProgressBar.visibility = if (newProgress < 100) View.VISIBLE else View.GONE
                }

                override fun onJsAlert(
                    view: WebView?, url: String?, message: String?, result: JsResult?
                ): Boolean {
                    Toast.makeText(this@MainActivity, message, Toast.LENGTH_SHORT).show()
                    result?.confirm()
                    return true
                }
            }

            addJavascriptInterface(NativeBridge(this@MainActivity), "AndroidBridge")

            // GAP 5: Handle downloads (Excel export)
            setDownloadListener { url, userAgent, contentDisposition, mimeType, contentLength ->
                try {
                    val request = DownloadManager.Request(Uri.parse(url)).apply {
                        setMimeType(mimeType)
                        addRequestHeader("User-Agent", userAgent)
                        setDescription("Downloading Market Radar Export...")
                        setTitle("MarketRadar_Export.xlsx")
                        setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                        setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "MarketRadar_Export.xlsx")
                    }
                    val dm = getSystemService(DOWNLOAD_SERVICE) as DownloadManager
                    dm.enqueue(request)
                    Toast.makeText(this@MainActivity, "Downloading to Downloads folder...", Toast.LENGTH_SHORT).show()
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "Download failed: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }

        // ── 2. Thin top progress bar (like Chrome's loading bar) ───────────
        topProgressBar = ProgressBar(
            this, null, android.R.attr.progressBarStyleHorizontal
        ).apply {
            layoutParams = FrameLayout.LayoutParams(MATCH_PARENT, dp(3)).apply {
                gravity = Gravity.TOP
            }
            max = 100
            progressTintList = ColorStateList.valueOf(PURPLE)
            progressBackgroundTintList = ColorStateList.valueOf(Color.TRANSPARENT)
            visibility = View.GONE
        }

        // ── 3. Loading overlay (splash) ────────────────────────────────────
        loadingOverlay = LinearLayout(this).apply {
            layoutParams = FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT)
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.WHITE)

            // App name
            addView(TextView(this@MainActivity).apply {
                text = "Market Radar"
                textSize = 24f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(PURPLE)
                gravity = Gravity.CENTER
                layoutParams = LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply {
                    bottomMargin = dp(20)
                }
            })

            // Circular spinner
            addView(ProgressBar(this@MainActivity).apply {
                isIndeterminate = true
                indeterminateTintList = ColorStateList.valueOf(PURPLE)
                layoutParams = LinearLayout.LayoutParams(dp(48), dp(48)).apply {
                    gravity = Gravity.CENTER_HORIZONTAL
                    bottomMargin = dp(16)
                }
            })

            // Subtitle
            addView(TextView(this@MainActivity).apply {
                text = "Loading market data..."
                textSize = 14f
                setTextColor(Color.parseColor("#888888"))
                gravity = Gravity.CENTER
            })
        }

        // ── 4. Error view ──────────────────────────────────────────────────
        errorView = LinearLayout(this).apply {
            layoutParams = FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT)
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.WHITE)
            setPadding(dp(32), 0, dp(32), 0)
            visibility = View.GONE

            addView(TextView(this@MainActivity).apply {
                text = "No internet connection"
                textSize = 18f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(Color.parseColor("#333333"))
                gravity = Gravity.CENTER
                layoutParams = LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply {
                    bottomMargin = dp(8)
                }
            })

            addView(TextView(this@MainActivity).apply {
                text = "Check your connection and try again"
                textSize = 14f
                setTextColor(Color.parseColor("#888888"))
                gravity = Gravity.CENTER
                layoutParams = LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply {
                    bottomMargin = dp(28)
                }
            })

            addView(Button(this@MainActivity).apply {
                text = "Retry"
                backgroundTintList = ColorStateList.valueOf(PURPLE)
                setTextColor(Color.WHITE)
                layoutParams = LinearLayout.LayoutParams(dp(160), WRAP_CONTENT).apply {
                    gravity = Gravity.CENTER_HORIZONTAL
                }
                setOnClickListener {
                    errorView.visibility = View.GONE
                    loadingOverlay.visibility = View.VISIBLE
                    webView.reload()
                }
            })
        }

        swipeRefresh = SwipeRefreshLayout(this).apply {
            layoutParams = FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT)
            setColorSchemeColors(PURPLE)
            setOnRefreshListener {
                isManualRefresh = true
                Toast.makeText(this@MainActivity, "Checking for updates...", Toast.LENGTH_SHORT).show()
                webView.reload()
            }
            addView(webView)
        }

        container.addView(swipeRefresh)
        container.addView(topProgressBar)
        container.addView(loadingOverlay)
        container.addView(errorView)
        setContentView(container)

        // b105: Copy ML model files from assets to internal storage on install/update
        // Python reads from filesDir — assets are read-only and not directly accessible
        val mlPrefs = getSharedPreferences("market_radar", MODE_PRIVATE)
        val currentVersionCode = BuildConfig.VERSION_CODE
        val lastMLCopyVersion = mlPrefs.getInt("ml_copy_version", -1)
        if (lastMLCopyVersion != currentVersionCode) {
            listOf("ml_model.json", "temporal_model.json", "backtest_trades.csv").forEach { filename ->
                try {
                    val dest = File(filesDir, filename)
                    
                    // MA7: backtest_trades.csv is static baseline data, always overwrite.
                    // ml_model.json and temporal_model.json are TRAINED state — preserve if they exist.
                    val shouldCopy = when (filename) {
                        "backtest_trades.csv" -> true  // always refresh baseline CSV
                        else -> !dest.exists() || dest.length() == 0L  // only copy models if missing
                    }
                    
                    if (shouldCopy) {
                        assets.open(filename).use { input ->
                            dest.outputStream().use { output -> input.copyTo(output) }
                        }
                        android.util.Log.i("MainActivity", "ML: copied $filename (v$currentVersionCode)")
                    } else {
                        android.util.Log.i("MainActivity", "ML: preserved existing $filename (trained state)")
                    }
                } catch (e: Exception) {
                    android.util.Log.w("MainActivity", "ML: could not copy $filename: ${e.message}")
                }
            }
            mlPrefs.edit().putInt("ml_copy_version", currentVersionCode).apply()
        }

        // b105: Cancel any stale 11 PM alarm from previous APK (auto-schedule removed)
        MarketMLService.cancelNightlyTraining(this)

        // Restore WebView state on rotation/process restart, otherwise load fresh
        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState)
        } else {
            webView.loadUrl(APP_URL)
        }

        handleIntent(intent)

        // Register poll receiver — wakes WebView every 5 min from service
        val filter = IntentFilter("com.marketradar.POLL_TICK")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(pollReceiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(pollReceiver, filter)
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else moveTaskToBack(true)
            }
        })
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        val rawTab = intent?.getStringExtra("openTab")
        if (rawTab != null) {
            // MA14: only allow alphanumeric + underscore in tab names
            val safeTab = rawTab.filter { it.isLetterOrDigit() || it == '_' }
            if (safeTab.isNotEmpty() && safeTab == rawTab) {
                webView.post {
                    webView.evaluateJavascript("if(typeof switchTab === 'function') switchTab('$safeTab')", null)
                }
            } else {
                android.util.Log.w("MainActivity", "Rejected unsafe openTab: $rawTab")
            }
        }
    }

    private fun checkVersionUpdate(currentVersion: String) {
        val prefs = getSharedPreferences("market_radar", MODE_PRIVATE)
        val lastVersion = prefs.getString("last_version", null)
        if (lastVersion == null) {
            // First install — store silently
            prefs.edit().putString("last_version", currentVersion).apply()
        } else if (lastVersion != currentVersion) {
            Toast.makeText(this, "✓ Updated to latest version", Toast.LENGTH_SHORT).show()
            prefs.edit().putString("last_version", currentVersion).apply()
        } else if (isManualRefresh) {
            Toast.makeText(this, "✓ Already up to date", Toast.LENGTH_SHORT).show()
        }
        isManualRefresh = false
    }

    private fun hideSplashAfterDelay() {
        loadingOverlay.visibility = View.GONE
    }

    private fun dp(value: Int) = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, value.toFloat(), resources.displayMetrics
    ).toInt()

    private fun injectNativeBridge() {
        val js = """
            (function() {
                if (window._nativeBridgeInjected) return;
                window._nativeBridgeInjected = true;
                window.NativeBridge = {
                    isNative: function() { return true; },
                    startMarketService: function() { AndroidBridge.startMarketService(); },
                    stopMarketService: function() { AndroidBridge.stopMarketService(); },
                    sendNotification: function(title, body, type) { AndroidBridge.sendNotification(title, body, type); },
                    
                    // Data Push
                    setApiToken: function(t) { AndroidBridge.setApiToken(t); },
                    setOpenTrades: function(j) { AndroidBridge.setOpenTrades(j); },
                    setBaseline: function(j) { AndroidBridge.setBaseline(j); },
                    setExpiries: function(bnf, nf) { AndroidBridge.setExpiries(bnf, nf); },
                    setContext: function(j) { AndroidBridge.setContext(j); },
                    setClosedTrades: function(j) { AndroidBridge.setClosedTrades(j); },
                    
                    // Data Pull
                    getLatestPoll: function() { return AndroidBridge.getLatestPoll(); },
                    getPollHistory: function() { return AndroidBridge.getPollHistory(); },
                    getBrainResult: function() { return AndroidBridge.getBrainResult(); },
                    getServiceStatus: function() { return AndroidBridge.getServiceStatus(); },
                    getCandidates: function() { return AndroidBridge.getCandidates(); },
                    
                    // ML Methods (b105)
                    isMLModelReady: function() { return AndroidBridge.isMLModelReady(); },
                    getMLModelStatus: function() { return AndroidBridge.getMLModelStatus(); },
                    triggerMLOnlineUpdate: function(j) { AndroidBridge.triggerMLOnlineUpdate(j); },
                    triggerMLRetrain: function() { AndroidBridge.triggerMLRetrain(); },
                    
                    // Diagnostic Methods (LV.1)
                    getLogBuffer: function(f) { return AndroidBridge.getLogBuffer(f); },
                    clearLogBuffer: function() { return AndroidBridge.clearLogBuffer(); },
                    getLogCaptureMode: function() { return AndroidBridge.getLogCaptureMode(); },
                    
                    init: function() { console.log('[BRIDGE] Native Android APK v2.1 (Chaquopy+ML+LV1) active'); }
                };
                NativeBridge.init();
                console.log('[BRIDGE] Native bridge injected');
            })();
        """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        if (level >= TRIM_MEMORY_MODERATE) {
            webView.clearCache(false)
        }
    }

    override fun onPause() {
        super.onPause()
        // Do NOT call webView.onPause() — we want JS to keep running in background
    }

    override fun onResume() {
        super.onResume()
        webView.onResume() // resume if system paused it
        
        // Sync native data with WebView on resume
        webView.post {
            val syncJs = """
                (function() {
                    if (window.syncFromNative && typeof window.syncFromNative === 'function') {
                        window.syncFromNative();
                        console.log('[SYNC] Triggered UI sync from native background data');
                    }
                })();
            """.trimIndent()
            webView.evaluateJavascript(syncJs, null)
        }
    }

    private fun showVersionDialog() {
        MaterialAlertDialogBuilder(this)
            .setTitle("Market Radar")
            .setMessage("Build Version: ${BuildConfig.VERSION_NAME}\nEngine: v3 (Chaquopy)\nInfrastructure: Phase 4 (Unified)")
            .setNeutralButton("Check for Update") { _, _ -> 
                checkForUpdates(true)
            }
            .setPositiveButton("OK") { dialog, _ -> dialog.dismiss() }
            .show()
    }

    private fun checkForUpdates(isManual: Boolean) {
        val request = Request.Builder().url(UPDATE_URL).build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (isManual) {
                    runOnUiThread {
                        Toast.makeText(this@MainActivity, "Update check failed: ${e.message}", Toast.LENGTH_SHORT).show()
                    }
                }
            }

            

            override fun onResponse(call: Call, response: Response) {
                response.use {
                    if (!response.isSuccessful) return
                    val body = response.body?.string() ?: return
                    val json = JSONObject(body)
                    val cloudTag = json.optString("tag_name", "") // e.g., "v2.2.6"
                    val downloadUrl = json.optJSONArray("assets")?.optJSONObject(0)?.optString("browser_download_url")
                    val releaseNotes = json.optString("body", "")

                    val cloudVer = cloudTag.replace("v", "").trim()
                    val localVer = BuildConfig.VERSION_NAME

                    if (isNewer(localVer, cloudVer)) {
                        runOnUiThread {
                            showUpdateDialog(cloudTag, downloadUrl, releaseNotes)
                        }
                    } else if (isManual) {
                        runOnUiThread {
                            Toast.makeText(this@MainActivity, "You are on the latest version", Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            }
        })
    }

    private fun isNewer(local: String, cloud: String): Boolean {
        return try {
            // MA10: Strip any non-numeric segments (like "v" or "-beta") to prevent toInt() crashes
            val l = local.replace(Regex("[^0-9.]"), "").split(".").filter { it.isNotEmpty() }.map { it.toInt() }
            val c = cloud.replace(Regex("[^0-9.]"), "").split(".").filter { it.isNotEmpty() }.map { it.toInt() }
            
            for (i in 0 until minOf(l.size, c.size)) {
                if (c[i] > l[i]) return true
                if (c[i] < l[i]) return false
            }
            c.size > l.size
        } catch (e: Exception) {
            // Fallback for cases where parsing might still fail (should be rare now)
            cloud > local
        }
    }

    private fun showUpdateDialog(version: String, url: String?, notes: String) {
        MaterialAlertDialogBuilder(this)
            .setTitle("Update Available: $version")
            .setMessage(if (notes.isNotEmpty()) notes else "A new version of Market Radar is available for download.")
            .setPositiveButton("Download & Install") { _, _ ->
                url?.let {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(it)))
                }
            }
            .setNegativeButton("Later", null)
            .show()
    }

    override fun onDestroy() {
        unregisterReceiver(pollReceiver)
        webView.destroy()
        super.onDestroy()
    }
}
