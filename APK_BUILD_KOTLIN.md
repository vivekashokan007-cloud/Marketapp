# 📱 Market Radar v2.2 — Native Kotlin & Python Build
## NO Node.js, NO Capacitor. Just Android Studio + Chaquopy.

### 🛡️ v2.2 Stabilization Milestone (April 2026)
This build includes 7 critical stability fixes:
1. **SwipeRefreshLayout Leak Fix**: Removed redundant UI initializations.
2. **Foreground ML Receiver**: Properly registered `MLAlarmReceiver` for background training.
3. **Supabase Security**: Moved all credentials to `BuildConfig` via environment variables.
4. **HTTPS Enforcement**: Removed `usesCleartextTraffic` to allow only secure API calls.
5. **Silent Catch Blocks**: All empty `catch` blocks now have debug logging.
6. **Python Call Timeouts**: Synchronous `brain.py` and training calls now have 10s-30s timeouts.
7. **Institutional Snapshots**: Automatic 2:00 PM and 3:15 PM positioning captures.

---

## 🛠️ PREREQUISITES
1. **Android Studio Koala (or later)**.
2. **Environment Variables**:
   ```powershell
   # Windows PowerShell
   $env:SUPABASE_ANON_KEY = "your-anon-key-here"
   $env:SUPABASE_URL = "your-supabase-url-here"
   ```
   Or add to `gradle.properties` (NOT COMMITTED):
   ```properties
   SUPABASE_ANON_KEY=your-key
   SUPABASE_URL=your-url
   ```

## 🏗️ BUILD STEPS
1. **Gradle Sync**: In Android Studio, click "Sync Project with Gradle Files".
2. **Chaquopy**: The plugin will automatically download Python 3.11 and requirements.
3. **Build APK**: Build → Build APK(s).
4. **Path**: `app/build/outputs/apk/debug/app-debug.apk`.

## 📂 CORE FILES
- `MainActivity.kt`: WebView hosting + Native bridge for data sync.
- `MarketWatchService.kt`: The main background engine. Polling, Python `analyze`, and snapshots.
- `MarketMLService.kt`: Nightly GBT + Neural Net training and online updates.
- `SupabaseClient.kt`: Generic REST helpers for cloud persistence.
- `brain.py`: The 3,000+ line Python synthesis engine.

---
*Maintained by Vivek Ashokan • Developed with Antigravity AI*
