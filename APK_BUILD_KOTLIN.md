# Market Radar APK — Pure Kotlin Build
# NO Node.js, NO Capacitor, NO npm. Just Android Studio.

## WHAT THIS DOES
Wraps the existing PWA (hosted at vivekashokan007-cloud.github.io/MarketVivi) in a native Android WebView with:
1. Foreground service — app stays alive when backgrounded
2. Wake lock — CPU stays active for 5-min polling
3. Native notification channels (urgent/important/routine)
4. JavaScript bridge — web app can trigger native features

## STEP 1: Create New Project in Android Studio
1. File → New → New Project
2. Select "Empty Activity" (Compose is fine, we'll replace it)
3. Name: `Market Radar`
4. Package: `com.marketradar.app`
5. Language: **Kotlin**
6. Minimum SDK: API 26 (Android 8.0)
7. Click Finish

## STEP 2: Update build.gradle (Module: app)
Open `app/build.gradle.kts` (or `build.gradle`) and make sure these are present:

```kotlin
android {
    namespace = "com.marketradar.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.marketradar.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "2.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.webkit:webkit:1.10.0")
}
```

## STEP 3: AndroidManifest.xml
Replace `app/src/main/AndroidManifest.xml` with:

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools">

    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" />
    <uses-permission android:name="android.permission.WAKE_LOCK" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

    <application
        android:allowBackup="true"
        android:icon="@mipmap/ic_launcher"
        android:label="Market Radar"
        android:theme="@style/Theme.MarketRadar"
        android:usesCleartextTraffic="true"
        tools:targetApi="34">

        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:configChanges="orientation|screenSize|keyboard|keyboardHidden"
            android:launchMode="singleTask">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>

        <service
            android:name=".MarketRadarService"
            android:foregroundServiceType="dataSync"
            android:exported="false" />

    </application>
</manifest>
```

## STEP 4: Create Theme
Create/edit `app/src/main/res/values/themes.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="Theme.MarketRadar" parent="Theme.AppCompat.Light.NoActionBar">
        <item name="android:statusBarColor">#FFFFFF</item>
        <item name="android:navigationBarColor">#FFFFFF</item>
        <item name="android:windowLightStatusBar">true</item>
    </style>
</resources>
```

## STEP 5: MainActivity.kt
Replace `app/src/main/java/com/marketradar/app/MainActivity.kt` with:

```kotlin
package com.marketradar.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.webkit.*
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val APP_URL = "https://vivekashokan007-cloud.github.io/MarketVivi/"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Request notification permission (Android 13+)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001)
            }
        }

        // Create WebView
        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true          // localStorage works
            settings.databaseEnabled = true
            settings.cacheMode = WebSettings.LOAD_DEFAULT
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            settings.userAgentString = settings.userAgentString + " MarketRadarApp/2.1"
            settings.setSupportZoom(false)
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true

            // Handle all URLs inside the WebView (don't open Chrome)
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                    return false // Keep everything in WebView
                }
                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    // Inject native bridge after page loads
                    injectNativeBridge()
                }
            }

            // Handle JS alerts/confirms
            webChromeClient = object : WebChromeClient() {
                override fun onJsAlert(view: WebView?, url: String?, message: String?, result: JsResult?): Boolean {
                    Toast.makeText(this@MainActivity, message, Toast.LENGTH_SHORT).show()
                    result?.confirm()
                    return true
                }
            }

            // Add JavaScript interface for native calls
            addJavascriptInterface(NativeBridge(this@MainActivity), "AndroidBridge")
        }

        setContentView(webView)
        webView.loadUrl(APP_URL)
    }

    private fun injectNativeBridge() {
        // Inject JS that connects web app to native Android
        val js = """
            (function() {
                if (window._nativeBridgeInjected) return;
                window._nativeBridgeInjected = true;
                
                window.NativeBridge = {
                    isNative: function() { return true; },
                    startMarketService: function() { AndroidBridge.startService(); },
                    stopMarketService: function() { AndroidBridge.stopService(); },
                    sendNotification: function(title, body, type) { AndroidBridge.sendNotification(title, body, type); },
                    init: function() { console.log('[BRIDGE] Native Android APK detected'); }
                };
                
                // Auto-init
                NativeBridge.init();
                console.log('[BRIDGE] Native bridge injected');
            })();
        """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            // Don't close app on back — minimize instead
            moveTaskToBack(true)
        }
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }
}
```

## STEP 6: NativeBridge.kt
Create NEW file `app/src/main/java/com/marketradar/app/NativeBridge.kt`:

```kotlin
package com.marketradar.app

import android.content.Context
import android.content.Intent
import android.os.Build
import android.webkit.JavascriptInterface

class NativeBridge(private val context: Context) {

    @JavascriptInterface
    fun startService() {
        val intent = Intent(context, MarketRadarService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(intent)
        } else {
            context.startService(intent)
        }
    }

    @JavascriptInterface
    fun stopService() {
        val intent = Intent(context, MarketRadarService::class.java).apply {
            action = "STOP"
        }
        context.startService(intent)
    }

    @JavascriptInterface
    fun sendNotification(title: String, body: String, type: String) {
        NotificationHelper.send(context, title, body, type)
    }
}
```

## STEP 7: MarketRadarService.kt
Create NEW file `app/src/main/java/com/marketradar/app/MarketRadarService.kt`:

```kotlin
package com.marketradar.app

import android.app.*
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

class MarketRadarService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    companion object {
        const val CHANNEL_ID = "market_radar_service"
        const val NOTIFICATION_ID = 1001
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        acquireWakeLock()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "STOP") {
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }

        val notificationIntent = packageManager.getLaunchIntentForPackage(packageName)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, notificationIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val stopIntent = Intent(this, MarketRadarService::class.java).apply { action = "STOP" }
        val stopPending = PendingIntent.getService(
            this, 1, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Market Radar Active")
            .setContentText("Watching market • Polls every 5 min")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pendingIntent)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stopPending)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()

        startForeground(NOTIFICATION_ID, notification)
        return START_STICKY
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Market Radar Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps Market Radar alive during market hours"
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(channel)
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "MarketRadar::PollWakeLock"
        ).apply {
            acquire(7 * 60 * 60 * 1000L) // 7 hours max
        }
    }

    override fun onDestroy() {
        wakeLock?.let { if (it.isHeld) it.release() }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
```

## STEP 8: NotificationHelper.kt
Create NEW file `app/src/main/java/com/marketradar/app/NotificationHelper.kt`:

```kotlin
package com.marketradar.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

object NotificationHelper {

    private const val CHANNEL_URGENT = "trade_urgent"
    private const val CHANNEL_IMPORTANT = "trade_important"
    private const val CHANNEL_ROUTINE = "trade_routine"
    private var notifId = 2000

    fun createChannels(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = context.getSystemService(NotificationManager::class.java) ?: return

            val urgent = NotificationChannel(CHANNEL_URGENT, "Urgent Alerts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Exit signals, stop loss, target alerts"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 300, 200, 300)
            }

            val important = NotificationChannel(CHANNEL_IMPORTANT, "Important Alerts", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Market moves, force alignment changes"
                enableVibration(true)
            }

            val routine = NotificationChannel(CHANNEL_ROUTINE, "Market Updates", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Periodic market status"
            }

            manager.createNotificationChannel(urgent)
            manager.createNotificationChannel(important)
            manager.createNotificationChannel(routine)
        }
    }

    fun send(context: Context, title: String, body: String, type: String) {
        createChannels(context)

        val channelId = when (type) {
            "urgent" -> CHANNEL_URGENT
            "important", "entry" -> CHANNEL_IMPORTANT
            else -> CHANNEL_ROUTINE
        }
        val priority = when (type) {
            "urgent" -> NotificationCompat.PRIORITY_HIGH
            "important", "entry" -> NotificationCompat.PRIORITY_DEFAULT
            else -> NotificationCompat.PRIORITY_LOW
        }

        val intent = context.packageManager.getLaunchIntentForPackage(context.packageName)
        val pending = PendingIntent.getActivity(
            context, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val builder = NotificationCompat.Builder(context, channelId)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pending)
            .setPriority(priority)
            .setAutoCancel(true)

        if (type == "urgent") {
            builder.setStyle(NotificationCompat.BigTextStyle().bigText(body))
        }

        val manager = context.getSystemService(NotificationManager::class.java)
        manager?.notify(notifId++, builder.build())
    }
}
```

## STEP 9: Delete Compose files (if created)
If Android Studio created Compose files, delete them:
- Delete any `ui/theme/` folder
- Delete any reference to `setContent { }` in MainActivity (we replaced it)

## STEP 10: Build APK
1. In Android Studio: Build → Build Bundle(s) / APK(s) → Build APK(s)
2. Wait for build to finish
3. APK at: `app/build/outputs/apk/debug/app-debug.apk`
4. Copy to phone → Install

## FILE STRUCTURE SUMMARY
```
app/src/main/
├── AndroidManifest.xml
├── java/com/marketradar/app/
│   ├── MainActivity.kt          (WebView + bridge injection)
│   ├── NativeBridge.kt          (JS ↔ Kotlin interface)
│   ├── MarketRadarService.kt    (foreground service + wake lock)
│   └── NotificationHelper.kt    (3 notification channels)
└── res/values/
    └── themes.xml
```

## HOW IT WORKS
1. App opens → WebView loads https://vivekashokan007-cloud.github.io/MarketVivi/
2. Page loads → injectNativeBridge() adds window.NativeBridge to JS
3. Web app's startWatchLoop() calls NativeBridge.startMarketService()
4. Android starts foreground service → "Market Radar Active" notification
5. Service holds wake lock → CPU stays alive → 5-min setInterval works
6. Web app's sendNotification() calls NativeBridge.sendNotification()
7. Native Android notifications fire with proper channels + vibration
8. Back button minimizes (doesn't close). Service keeps running.

## UPDATING THE WEB APP
The APK loads from GitHub Pages. When you push new web files to GitHub:
- The APK automatically shows the latest version on next open
- NO APK rebuild needed for web-only changes
- Only rebuild APK if native code (Kotlin) changes

## TROUBLESHOOTING
- "WebView blank" → Check INTERNET permission in manifest
- "Service not starting" → Check FOREGROUND_SERVICE permission
- "Notifications not showing" → Check POST_NOTIFICATIONS permission + channels
- "localStorage lost" → settings.domStorageEnabled must be true
- "Build failed" → Sync Gradle (File → Sync Project with Gradle Files)
