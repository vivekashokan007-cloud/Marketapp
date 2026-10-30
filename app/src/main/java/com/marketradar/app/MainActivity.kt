package com.marketradar.app

import android.Manifest
import android.content.ComponentCallbacks2
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
import android.view.ViewGroup
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
import com.marketradar.app.util.LogBuffer
import okhttp3.*
import java.io.IOException
import org.json.JSONObject

class MainActivity : AppCompatActivity() {

    // Stage 2 CI-2: SAF "create document" for the read-only research export.
    @Volatile private var researchExportIdentity: String? = null
    private val researchExportLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.CreateDocument("application/zip")
    ) { uri -> onResearchExportUri(uri) }

    fun launchResearchExport(pwaIdentityJson: String?) {
        if (ResearchExport.running) return
        researchExportIdentity = pwaIdentityJson
        val name = "MarketRadar_research_m0_${ResearchCollector.sessionOf(System.currentTimeMillis())}_${System.currentTimeMillis()}.zip"
        try { researchExportLauncher.launch(name) } catch (t: Throwable) {
            deliverResearchExport(JSONObject().put("status", "FAILED_IO").put("stage", "picker").put("error", t.javaClass.simpleName))
        }
    }

    private fun researchBuildIdentity(): JSONObject {
        val id = JSONObject().put("app_id", BuildConfig.APPLICATION_ID).put("version_name", BuildConfig.VERSION_NAME)
            .put("version_code", BuildConfig.VERSION_CODE).put("brain_version_expected", BuildConfig.VERSION_NAME)
            .put("marketapp_source_head", BuildConfig.SOURCE_HEAD).put("build_type", BuildConfig.BUILD_TYPE)
        try {
            val pi = if (Build.VERSION.SDK_INT >= 28) packageManager.getPackageInfo(packageName, PackageManager.GET_SIGNING_CERTIFICATES)
                     else @Suppress("DEPRECATION") packageManager.getPackageInfo(packageName, PackageManager.GET_SIGNATURES)
            val sigs = if (Build.VERSION.SDK_INT >= 28) pi.signingInfo?.apkContentsSigners else @Suppress("DEPRECATION") pi.signatures
            id.put("signer_sha256", org.json.JSONArray((sigs ?: emptyArray()).map { M0Durable.sha256(it.toByteArray()) }))
        } catch (t: Throwable) { id.put("signer_sha256", "UNAVAILABLE:" + t.javaClass.simpleName) }
        val pwa = try { JSONObject(researchExportIdentity ?: "") } catch (_: Exception) { JSONObject().put("status", "NOT_PROVIDED") }
        return id.put("pwa", pwa)
    }

    private fun onResearchExportUri(uri: Uri?) {
        if (uri == null) { deliverResearchExport(JSONObject().put("status", "CANCELLED").put("partial_output", "NONE_WRITTEN").put("stage", "picker")); return }
        if (ResearchExport.running) return
        ResearchExport.running = true; ResearchExport.cancelRequested = false
        val identity = researchBuildIdentity()
        Thread({
            val r = try {
                ResearchExport.export(File(filesDir, ResearchCollector.DIR), File(cacheDir, "research_export_tmp"), identity,
                    System.currentTimeMillis(),
                    openSink = { contentResolver.openOutputStream(uri, "wt") ?: throw IOException("SINK_NULL") },
                    openReadback = { contentResolver.openInputStream(uri) ?: throw IOException("READBACK_NULL") })
            } catch (t: Throwable) { JSONObject().put("status", "FAILED_IO").put("error", t.javaClass.simpleName) }
            r.put("document", uri.toString())
            ResearchExport.lastResult = r
            ResearchExport.running = false
            deliverResearchExport(r)
        }, "m0-research-export").start()
    }

    private fun deliverResearchExport(r: JSONObject) {
        ResearchExport.lastResult = r
        runOnUiThread {
            try { webView.evaluateJavascript("window.m0OnResearchExportResult && window.m0OnResearchExportResult(" + JSONObject.quote(r.toString()) + ")", null) }
            catch (_: Throwable) {}
        }
    }

    companion object {
        private const val PREFS_NAME = "market_radar"
        private const val PREF_LAST_WEB_URL = "last_web_url"
    }

    private lateinit var webView: WebView
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var loadingOverlay: View
    private lateinit var errorView: View
    private lateinit var topProgressBar: ProgressBar

    private val APP_URL = "https://vivekashokan007-cloud.github.io/MarketVivi/"
    private val UPDATE_URL = "https://api.github.com/repos/vivekashokan007-cloud/Marketapp/releases/latest"
    private val PURPLE = Color.parseColor("#7B2FC4")
    private var lastNativeSyncElapsedMs = 0L
    private var isManualRefresh = false
    private var lastLoadFailedMainFrame = false
    private var webViewRendererGone = false
    private val client = OkHttpClient()
    /** b512: true only between onCreate's WebView setup and onDestroy; posted callbacks check it with instance identity. */
    @Volatile private var uiAlive = false
    private var instanceSeq = 0
    private var createKind = LifecycleDiagnostics.CreateKind.FRESH_PROCESS_LAUNCH
    private var droppedUiCallbacks = 0
    /** b512 R2: retains a launch/onNewIntent tab until the PWA acknowledges it (Codex R1). */
    private val launchNav = LaunchNavigation(object : LaunchNavigation.Host {
        override fun evaluate(script: String, onResult: (String?) -> Unit) {
            postToLiveWebView("open_tab") { view -> view.evaluateJavascript(script) { r -> onResult(r) } }
        }
        override fun postDelayed(delayMs: Long, block: () -> Unit) {
            if (::webView.isInitialized) webView.postDelayed({ if (uiUsable()) block() }, delayMs)
        }
        override fun log(message: String) { LogBuffer.add('I', "MainActivity", "$message seq=$instanceSeq") }
    })

    /**
     * b512: every native->WebView callback goes through here. The target view is captured now and the block only runs
     * if this Activity is still alive AND that same view is still the current one, so a tick broadcast or update
     * response that lands after onDestroy (seen 21 Oct 10:00:49 and 10:25:52 IST) cannot touch a destroyed WebView.
     */
    private fun postToLiveWebView(what: String, block: (WebView) -> Unit) {
        if (!::webView.isInitialized || !uiAlive) { dropUiCallback(what); return }
        val target = webView
        target.post {
            if (uiAlive && !isDestroyed && !webViewRendererGone && webView === target) block(target) else dropUiCallback(what)
        }
    }

    private fun dropUiCallback(what: String) {
        droppedUiCallbacks++
        if (droppedUiCallbacks <= 5) LogBuffer.add('I', "MainActivity",
            "UI_CALLBACK_DROPPED_DEAD_VIEW: what=$what seq=$instanceSeq count=$droppedUiCallbacks pid=${android.os.Process.myPid()}")
    }

    private fun uiUsable() = uiAlive && !isFinishing && !isDestroyed

    private val pollReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val source = when (intent?.action) {
                PositionTickService.ACTION_POSITION_MARK_TICK -> "POSITION_MARK_TICK"
                PositionTickService.ACTION_PAPER_CLOSE_QUOTE_READY -> "PAPER_CLOSE_QUOTE_READY"
                else -> "POLL_TICK"
            }
            Log.d("MainActivity", "BROADCAST_RECEIVED: $source")
            if (!::webView.isInitialized) return

            val data = intent?.getStringExtra("data")

            postToLiveWebView("broadcast_$source") { webView ->
                if (data != null) {
                    // Legacy data-bearing tick path; current service ticks usually trigger native pull sync.
                    val escaped = data
                        .replace("\\", "\\\\")
                        .replace("'", "\\'")
                        .replace("\n", "")
                        .replace("\r", "")
                    Log.d("MainActivity", "EVALUATE_JS_CALLED: syncFromNative with data ($source)")
                    webView.evaluateJavascript(
                        "(function(){ if(typeof syncFromNative==='function') syncFromNative('$escaped'); else console.warn('[APK] syncFromNative not found'); })()",
                        null
                    )
                    lastNativeSyncElapsedMs = android.os.SystemClock.elapsedRealtime()
                } else {
                    // Brain failed or no data, but still wake the UI to refresh poll badge
                    Log.d("MainActivity", "EVALUATE_JS_CALLED: syncFromNative (no data, $source wake)")
                    webView.evaluateJavascript(
                        "(function(){ if(typeof syncFromNative==='function') syncFromNative(null); })()",
                        null
                    )
                    lastNativeSyncElapsedMs = android.os.SystemClock.elapsedRealtime()
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        LifecycleDiagnostics.onActivityCreate(savedInstanceState != null, MarketRadarApp.PROCESS_START_UUID).let { (seq, kind) ->
            instanceSeq = seq; createKind = kind
        }
        LogBuffer.add(
            'I',
            "MainActivity",
            "ACTIVITY_CREATE: restored=${savedInstanceState != null} kind=$createKind seq=$instanceSeq taskId=$taskId pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}"
        )
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
            LogBuffer.add(
                'I',
                "MainActivity",
                "WEBVIEW_CREATE: hostPid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID} rendererPid=unavailable_public_api"
            )
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

                override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                    super.onPageStarted(view, url, favicon)
                    errorView.visibility = View.GONE
                    lastLoadFailedMainFrame = false
                    injectNativeBridge()
                }

                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    swipeRefresh.isRefreshing = false
                    hideSplashAfterDelay()
                    errorView.visibility = View.GONE
                    lastLoadFailedMainFrame = false
                    persistCurrentWebUrl(url)
                    injectNativeBridge()
                    launchNav.onPageFinished()

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
                        lastLoadFailedMainFrame = true
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
                        lastLoadFailedMainFrame = true
                        loadingOverlay.visibility = View.GONE
                        errorView.visibility = View.VISIBLE
                    }
                }

                override fun onRenderProcessGone(
                    view: WebView?,
                    detail: RenderProcessGoneDetail?
                ): Boolean {
                    val didCrash = detail?.didCrash() ?: false
                    val priority = detail?.rendererPriorityAtExit() ?: -1
                    LogBuffer.add(
                        'E',
                        "MainActivity",
                        "WEBVIEW_RENDERER_GONE: didCrash=$didCrash priority=$priority activityFinishing=$isFinishing pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}"
                    )
                    persistCurrentWebUrl(view?.url)
                    webViewRendererGone = true
                    uiAlive = false
                    launchNav.onUiLost()
                    LifecycleDiagnostics.markRendererRecovery()
                    view?.let { deadView ->
                        (deadView.parent as? ViewGroup)?.removeView(deadView)
                        deadView.destroy()
                    }
                    if (!isFinishing && !isDestroyed) {
                        recreate()
                    }
                    return true
                }
            }

            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView?, newProgress: Int) {
                    topProgressBar.progress = newProgress
                    topProgressBar.visibility = if (newProgress < 100) View.VISIBLE else View.GONE
                }

                override fun onConsoleMessage(consoleMessage: ConsoleMessage?): Boolean {
                    val msg = consoleMessage ?: return super.onConsoleMessage(consoleMessage)
                    val level = when (msg.messageLevel()) {
                        ConsoleMessage.MessageLevel.ERROR -> 'E'
                        ConsoleMessage.MessageLevel.WARNING -> 'W'
                        ConsoleMessage.MessageLevel.DEBUG -> 'D'
                        else -> 'I'
                    }
                    LogBuffer.add(
                        level,
                        "WebViewJS",
                        "${msg.sourceId()}:${msg.lineNumber()} ${msg.message()}"
                    )
                    return super.onConsoleMessage(consoleMessage)
                }

                override fun onJsAlert(
                    view: WebView?, url: String?, message: String?, result: JsResult?
                ): Boolean {
                    Toast.makeText(this@MainActivity, message, Toast.LENGTH_SHORT).show()
                    result?.confirm()
                    return true
                }

                override fun onJsConfirm(
                    view: WebView?, url: String?, message: String?, result: JsResult?
                ): Boolean {
                    MaterialAlertDialogBuilder(this@MainActivity)
                        .setMessage(message ?: "")
                        .setPositiveButton("OK") { _, _ -> result?.confirm() }
                        .setNegativeButton("Cancel") { _, _ -> result?.cancel() }
                        .setOnCancelListener { result?.cancel() }
                        .show()
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
        val mlPrefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        val currentVersionCode = BuildConfig.VERSION_CODE
        val lastMLCopyVersion = mlPrefs.getInt("ml_copy_version", -1)
        if (lastMLCopyVersion != currentVersionCode) {
            listOf("ml_model.json", "temporal_model.json", "backtest_trades.csv", "teacher_table_stage2a.json").forEach { filename ->
                try {
                    val dest = File(filesDir, filename)
                    
                    // MA7: backtest_trades.csv is static baseline data, always overwrite.
                    // ml_model.json and temporal_model.json are TRAINED state — preserve if they exist.
                    val shouldCopy = when (filename) {
                        "backtest_trades.csv", "teacher_table_stage2a.json" -> true  // always refresh measured baselines
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
        val webStateRestored = savedInstanceState != null && webView.restoreState(savedInstanceState) != null
        if (!webStateRestored) {
            webView.loadUrl(restoredWebUrl())
        }
        LogBuffer.add('I', "MainActivity", "WEBVIEW_STATE: restored=$webStateRestored url=${webView.url.orEmpty()}")

        // b512: on a re-creation from saved state the launch intent was already handled; re-applying its openTab
        // would override the screen the user was on. New intents still arrive through onNewIntent.
        if (savedInstanceState == null) handleIntent(intent)
        else LogBuffer.add('I', "MainActivity", "ACTIVITY_INTENT_NOT_REAPPLIED: kind=$createKind seq=$instanceSeq")

        // Refresh on both the five-minute Brain cycle and each accepted 60-second
        // P1 position mark. P1 does not make execution decisions; this only lets
        // the WebView render its verified Paper valuation without a five-minute lag.
        val filter = IntentFilter("com.marketradar.POLL_TICK").apply {
            addAction(PositionTickService.ACTION_POSITION_MARK_TICK)
            addAction(PositionTickService.ACTION_PAPER_CLOSE_QUOTE_READY)
        }
        // M1: in-app broadcasts only. All senders (MarketWatchService POLL_TICK,
        // PositionTickService mark/close-quote) are same-app, so NOT_EXPORTED still
        // receives them. Other apps can no longer inject `data` into the WebView.
        uiAlive = true
        launchNav.onUiReady()   // b512 R2: deliver a cold-launch openTab now that the view is live (retried until the PWA acks)
        ContextCompat.registerReceiver(this, pollReceiver, filter, ContextCompat.RECEIVER_NOT_EXPORTED)

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
                launchNav.request(safeTab, "intent")   // only the tab name is retained; never any trade/broker action
            } else {
                android.util.Log.w("MainActivity", "Rejected unsafe openTab: $rawTab")
            }
        }
    }

    private fun checkVersionUpdate(currentVersion: String) {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
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

    private fun persistCurrentWebUrl(url: String?) {
        val safeUrl = url?.trim().orEmpty()
        if (safeUrl.startsWith(APP_URL)) {
            getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                .edit()
                .putString(PREF_LAST_WEB_URL, safeUrl)
                .apply()
        }
    }

    private fun restoredWebUrl(): String {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        val savedUrl = prefs.getString(PREF_LAST_WEB_URL, null)?.trim().orEmpty()
        return if (savedUrl.startsWith(APP_URL)) savedUrl else APP_URL
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
                    stopMarketService: function() { AndroidBridge.userStopMarketService(); },
                    sendNotification: function(title, body, type) { AndroidBridge.sendNotification(title, body, type); },
                    
                    // Data Push
                    setApiToken: function(t) { AndroidBridge.setApiToken(t); },
                    setOpenTrades: function(j) { AndroidBridge.setOpenTrades(j); },
                    setBaseline: function(j) { AndroidBridge.setBaseline(j); },
                    setMorningInput: function(j) { return AndroidBridge.setMorningInput(j); },
                    setEveningClose: function(j) { return AndroidBridge.setEveningClose(j); },
                    setExpiries: function(bnf, nf) { AndroidBridge.setExpiries(bnf, nf); },
                    setContext: function(j) { AndroidBridge.setContext(j); },
                    setGlobalDirection: function(j) { AndroidBridge.setGlobalDirection(j); },
                    setTradeMode: function(mode) { AndroidBridge.setTradeMode(mode || "intraday"); },
                    setTradeModeDefault: function(mode) { AndroidBridge.setTradeModeDefault(mode || "intraday"); },
                    requestImmediatePoll: function() { AndroidBridge.requestImmediatePoll(); },
                    setClosedTrades: function(j) { AndroidBridge.setClosedTrades(j); },
                    recordClosedTrade: function(j) { return AndroidBridge.recordClosedTrade(j); },
                    
                    // Data Pull
                    getLatestPoll: function() { return AndroidBridge.getLatestPoll(); },
                    getPollHistory: function() { return AndroidBridge.getPollHistory(); },
                    getBrainResult: function() { return AndroidBridge.getBrainResult(); },
                    getServiceStatus: function() { return AndroidBridge.getServiceStatus(); },
                    getCandidates: function() { return AndroidBridge.getCandidates(); },
                    getSignalAccuracyStats: function() { return AndroidBridge.getSignalAccuracyStats(); },
                    getOpenTrades: function() { return AndroidBridge.getOpenTrades(); },
                    getPositionMarkStates: function() { return AndroidBridge.getPositionMarkStates(); },
                    requestPaperCloseQuote: function(tradeId) { return AndroidBridge.requestPaperCloseQuote(String(tradeId || '')); },
                    getPaperCloseQuote: function(requestId) { return AndroidBridge.getPaperCloseQuote(String(requestId || '')); },
                    getClosedTrades: function(limit) { return AndroidBridge.getClosedTrades(limit || 200); },
                    getPremiumHistory: function(days) { return AndroidBridge.getPremiumHistory(days || 5); },
                    getMorningSnapshot: function(date) { return AndroidBridge.getMorningSnapshot(date || ""); },
                    getYesterdayHistory: function(days) { return AndroidBridge.getYesterdayHistory(days || 5); },
                    getChainSnapshot: function(date, session) { return AndroidBridge.getChainSnapshot(date || "", session || "2pm"); },
                    getBaseline: function() { return AndroidBridge.getBaseline(); },
                    getConfig: function(key) { return AndroidBridge.getConfig(key || ""); },
                    getAllConfig: function() { return AndroidBridge.getAllConfig(); },
                    getBnfChain: function() { return AndroidBridge.getBnfChain(); },
                    getNfChain: function() { return AndroidBridge.getNfChain(); },
                    getBnfBreadth: function() { return AndroidBridge.getBnfBreadth(); },
                    getNf50Breadth: function() { return AndroidBridge.getNf50Breadth(); },
                    getTradeMode: function() { return AndroidBridge.getTradeMode(); },
                    getTradeModeExplicit: function() { return AndroidBridge.getTradeModeExplicit(); },
                    getGlobalDirection: function() { return AndroidBridge.getGlobalDirection(); },
                    getOvernightInputState: function() { return AndroidBridge.getOvernightInputState(); },
                    getPaperBuyingResearchStatus: function() { return AndroidBridge.getPaperBuyingResearchStatus(); },
                    exportPaperBuyingResearchJournal: function() { return AndroidBridge.exportPaperBuyingResearchJournal(); },
                    getRecentSignals: function(limit) { return AndroidBridge.getRecentSignals(limit || 50); },
                    getMLDecisions: function(limit) { return AndroidBridge.getMLDecisions(limit || 50); },
                    getMLEvaluationOutcomes: function(limit) { return AndroidBridge.getMLEvaluationOutcomes(limit || 1000); },
                    getMLEvaluationLaneSummary: function(limit) { return AndroidBridge.getMLEvaluationLaneSummary(limit || 1000); },
                    getMLTeacherResearchReport: function() { return AndroidBridge.getMLTeacherResearchReport(); },
                    getMLBrainSnapshots: function(limit) { return AndroidBridge.getMLBrainSnapshots(limit || 200); },
                    getTeacherTruthConfig: function() { return AndroidBridge.getTeacherTruthConfig(); },
                    getExecutionInfraStatus: function() { return AndroidBridge.getExecutionInfraStatus(); },
                    computeLiveFriction: function(j) { return AndroidBridge.computeLiveFriction(j || "{}"); },
                    getOrderProxyUrl: function() { return AndroidBridge.getOrderProxyUrl(); },
                    getSandboxTokenReady: function() { return AndroidBridge.getSandboxTokenReady(); },
                    getNotificationTransportMode: function() { return AndroidBridge.getNotificationTransportMode(); },
                    getStage2AGuardMode: function() { return AndroidBridge.getStage2AGuardMode(); },
                    getCachedEvaluationJob: function() { return AndroidBridge.getCachedEvaluationJob(); },
                    getApprovedBranchProposals: function() { return AndroidBridge.getApprovedBranchProposals(); },
                    refreshApprovedBranchProposals: function() { return AndroidBridge.refreshApprovedBranchProposals(); },

                    // File export methods
                    saveExportFile: function(fileName, mimeType, base64Data) { return AndroidBridge.saveExportFile(fileName, mimeType, base64Data); },
                    beginExportFile: function(fileName, mimeType) { return AndroidBridge.beginExportFile(fileName, mimeType); },
                    appendExportFileChunk: function(sessionId, base64Chunk) { return AndroidBridge.appendExportFileChunk(sessionId, base64Chunk); },
                    finishExportFile: function(sessionId) { return AndroidBridge.finishExportFile(sessionId); },
                    
                    // ML Methods (b105)
                    isMLModelReady: function() { return AndroidBridge.isMLModelReady(); },
                    getMLModelStatus: function() { return AndroidBridge.getMLModelStatus(); },
                    dailyReviewRun: function(d, p, t) { return AndroidBridge.dailyReviewRun(String(d || ''), String(p || '[]'), String(t || '')); },
                    dailyReviewLatest: function(d) { return AndroidBridge.dailyReviewLatest(String(d || '')); },
                    triggerMLOnlineUpdate: function(j) { AndroidBridge.triggerMLOnlineUpdate(j); },
                    triggerMLRetrain: function() { return AndroidBridge.triggerMLRetrain(); },
                    triggerDayEvaluation: function() { return AndroidBridge.triggerDayEvaluation(); },
                    triggerDayEvaluationForce: function() { return AndroidBridge.triggerDayEvaluationForce(); },
                    triggerEvaluationJob: function(j) { return AndroidBridge.triggerEvaluationJob(j); },
                    approveBranchProposal: function(rowId) { return AndroidBridge.approveBranchProposal(rowId); },
                    rejectBranchProposal: function(rowId) { return AndroidBridge.rejectBranchProposal(rowId); },
                    setExecutionSandboxEnabled: function(enabled) { return AndroidBridge.setExecutionSandboxEnabled(!!enabled); },
                    setSandboxToken: function(token) { return AndroidBridge.setSandboxToken(token || ""); },
                    runSandboxOrderDebugAction: function(j) { return AndroidBridge.runSandboxOrderDebugAction(j || "{}"); },
                    setOrderProxyUrl: function(url) { return AndroidBridge.setOrderProxyUrl(url || ""); },
                    setNotificationTransportMode: function(mode) { return AndroidBridge.setNotificationTransportMode(mode || "live"); },
                    setStage2AGuardMode: function(mode) { return AndroidBridge.setStage2AGuardMode(mode || "shadow"); },
                    reportJsCrash: function(payloadJson) { return AndroidBridge.reportJsCrash(payloadJson || "{}"); },
                    
                    // Diagnostic Methods (LV.1)
                    getLogBuffer: function(f) { return AndroidBridge.getLogBuffer(f); },
                    clearLogBuffer: function() { return AndroidBridge.clearLogBuffer(); },
                    getLogCaptureMode: function() { return AndroidBridge.getLogCaptureMode(); },

                    // Implied-EV v1.2 (b506, Codex option A): explicit forwarders for EVERY @JavascriptInterface ev*
                    // method on AndroidBridge. Arguments pass through unchanged and in order; return values unchanged.
                    // Native keeps all authority (Paper-only, Off/Observe, Active blocking, evRuleBAck fail-closed).
                    evGetMode: function() { return AndroidBridge.evGetMode(); },
                    evSetMode: function(mode) { return AndroidBridge.evSetMode(mode); },
                    evSetModeChecked: function(mode) { return AndroidBridge.evSetModeChecked(mode); },
                    evGetStatus: function() { return AndroidBridge.evGetStatus(); },
                    evPrepareAcceptance: function(sessionDate, decisionId) { return AndroidBridge.evPrepareAcceptance(sessionDate, decisionId); },
                    evGetAcceptance: function(requestId) { return AndroidBridge.evGetAcceptance(requestId); },
                    evConfirmAcceptance: function(decisionId, reservationId) { return AndroidBridge.evConfirmAcceptance(decisionId, reservationId); },
                    evCancelAcceptance: function(decisionId, reservationId) { return AndroidBridge.evCancelAcceptance(decisionId, reservationId); },
                    evMarkTradePersisted: function(decisionId, tradeId, tradeUid) { return AndroidBridge.evMarkTradePersisted(decisionId, tradeId, tradeUid); },
                    evAcceptanceRecovery: function() { return AndroidBridge.evAcceptanceRecovery(); },
                    evRuleBPending: function() { return AndroidBridge.evRuleBPending(); },
                    evRuleBJournal: function(tradeId, patchJson) { return AndroidBridge.evRuleBJournal(tradeId, patchJson); },
                    evRuleBRemoteFailed: function(tradeId, detail) { return AndroidBridge.evRuleBRemoteFailed(tradeId, detail); },
                    evRuleBAckState: function(tradeId, state) { return AndroidBridge.evRuleBAckState(tradeId, state); },
                    evRuleBAck: function(tradeId) { return AndroidBridge.evRuleBAck(tradeId); },
                    evNoteManualClose: function(tradeId, decisionId) { return AndroidBridge.evNoteManualClose(tradeId, decisionId); },
                    evManualCloseRequest: function(tradeId, decisionId, patchJson) { return AndroidBridge.evManualCloseRequest(tradeId, decisionId, patchJson); },
                    evManualIntentAbandon: function(tradeId) { return AndroidBridge.evManualIntentAbandon(tradeId); },
                    evManualCloseJournal: function(tradeId, patchJson) { return AndroidBridge.evManualCloseJournal(tradeId, patchJson); },
                    evManualCloseResult: function(tradeId, result) { return AndroidBridge.evManualCloseResult(tradeId, result); },
                    evAcceptanceReconciledClosed: function(decisionId) { return AndroidBridge.evAcceptanceReconciledClosed(decisionId); },
                    evAcceptanceQuarantine: function() { return AndroidBridge.evAcceptanceQuarantine(); },

                    // M0 full-day research collector (collection only). An APK whose AndroidBridge lacks the method
                    // returns an explicit unavailable state instead of throwing.
                    getResearchCollectorStatus: function() {
                        if (typeof AndroidBridge.getResearchCollectorStatus !== 'function') return JSON.stringify({ available: false, label: 'UNAVAILABLE', reason: 'NATIVE_METHOD_ABSENT' });
                        return AndroidBridge.getResearchCollectorStatus();
                    },
                    setResearchCollectorEnabled: function(on) {
                        if (typeof AndroidBridge.setResearchCollectorEnabled !== 'function') return JSON.stringify({ available: false, label: 'UNAVAILABLE', reason: 'NATIVE_METHOD_ABSENT' });
                        return AndroidBridge.setResearchCollectorEnabled(on === true);
                    },
                    // Stage 2 CI-2: read-only research export (SAF picker) + result/cancel.
                    exportResearchEvidence: function(pwaIdentityJson) {
                        if (typeof AndroidBridge.exportResearchEvidence !== 'function') return JSON.stringify({ status: 'UNAVAILABLE', reason: 'NATIVE_METHOD_ABSENT' });
                        return AndroidBridge.exportResearchEvidence(typeof pwaIdentityJson === 'string' ? pwaIdentityJson : '');
                    },
                    getResearchExportResult: function() {
                        if (typeof AndroidBridge.getResearchExportResult !== 'function') return JSON.stringify({ status: 'UNAVAILABLE', reason: 'NATIVE_METHOD_ABSENT' });
                        return AndroidBridge.getResearchExportResult();
                    },
                    cancelResearchExport: function() {
                        if (typeof AndroidBridge.cancelResearchExport !== 'function') return JSON.stringify({ status: 'UNAVAILABLE', reason: 'NATIVE_METHOD_ABSENT' });
                        return AndroidBridge.cancelResearchExport();
                    },
                    
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
        LogBuffer.add('I', "MainActivity", "ACTIVITY_SAVE_STATE: url=${webView.url.orEmpty()} pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}")
    }

    override fun onPause() {
        super.onPause()
        persistCurrentWebUrl(webView.url)
        LogBuffer.add('I', "MainActivity", "ACTIVITY_PAUSE: url=${webView.url.orEmpty()} pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}")
    }

    override fun onResume() {
        super.onResume()
        LogBuffer.add('I', "MainActivity", "ACTIVITY_RESUME: url=${webView.url.orEmpty()} pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}")
        webView.onResume() // resume if system paused it
        webView.resumeTimers()
        
        // Sync native data with WebView on resume
        postToLiveWebView("resume_sync") { webView ->
            val neverLoaded = webView.url == null && webView.originalUrl == null
            if (lastLoadFailedMainFrame || neverLoaded) {
                lastLoadFailedMainFrame = false
                webView.loadUrl(restoredWebUrl())
                return@postToLiveWebView
            }
            val nowElapsed = android.os.SystemClock.elapsedRealtime()
            if (nowElapsed - lastNativeSyncElapsedMs < 5_000L) {
                return@postToLiveWebView
            }
            injectNativeBridge()
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

    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        LogBuffer.add('I', "MainActivity", "TRIM_MEMORY: level=$level pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}")
        if (level >= ComponentCallbacks2.TRIM_MEMORY_BACKGROUND) {
            EvaluationLocalCache.releaseMemory()
        }
        if (!::webView.isInitialized) return
        if (level >= ComponentCallbacks2.TRIM_MEMORY_MODERATE) {
            webView.clearCache(false)
            LogBuffer.add('I', "MainActivity", "TRIM_MEMORY_ACTION: level=$level cleared_webview_cache")
        }
    }

    override fun onStop() {
        LogBuffer.add(
            'I',
            "MainActivity",
            "ACTIVITY_STOP: changingConfig=$isChangingConfigurations finishing=$isFinishing pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}"
        )
        if (::webView.isInitialized) {
            webView.onPause()
            webView.pauseTimers()
            LogBuffer.add('I', "MainActivity", "WEBVIEW_PAUSED_ON_STOP")
        }
        super.onStop()
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
                        if (!uiUsable()) return@runOnUiThread
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
                            if (!uiUsable()) return@runOnUiThread
                            showUpdateDialog(cloudTag, downloadUrl, releaseNotes)
                        }
                    } else if (isManual) {
                        runOnUiThread {
                            if (!uiUsable()) return@runOnUiThread
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
        uiAlive = false
        launchNav.destroy()
        val alwaysFinish = runCatching {
            android.provider.Settings.Global.getInt(contentResolver, android.provider.Settings.Global.ALWAYS_FINISH_ACTIVITIES, 0)
        }.getOrDefault(-1)
        val trimAgoS = if (LifecycleDiagnostics.lastTrimElapsedMs > 0)
            (android.os.SystemClock.elapsedRealtime() - LifecycleDiagnostics.lastTrimElapsedMs) / 1000 else -1
        val lowMem = runCatching {
            android.app.ActivityManager.MemoryInfo().also { (getSystemService(ACTIVITY_SERVICE) as android.app.ActivityManager).getMemoryInfo(it) }.lowMemory
        }.getOrNull()
        LogBuffer.add(
            'I',
            "MainActivity",
            "ACTIVITY_DESTROY: changingConfig=$isChangingConfigurations finishing=$isFinishing seq=$instanceSeq kind=$createKind " +
                "rendererGone=$webViewRendererGone alwaysFinish=$alwaysFinish lastTrim=${LifecycleDiagnostics.lastTrimLevel} " +
                "lastTrimAgoS=$trimAgoS lowMemory=$lowMem droppedUiCallbacks=$droppedUiCallbacks " +
                "pid=${android.os.Process.myPid()} startUuid=${MarketRadarApp.PROCESS_START_UUID}"
        )
        runCatching { unregisterReceiver(pollReceiver) }
        client.dispatcher.cancelAll() // pending update check must not call back into this instance
        if (::webView.isInitialized) {
            persistCurrentWebUrl(webView.url)
            if (!webViewRendererGone) {
                webView.stopLoading()
                webView.removeJavascriptInterface("AndroidBridge")
                (webView.parent as? ViewGroup)?.removeView(webView)
                webView.destroy()
            }
        }
        super.onDestroy()
    }
}
