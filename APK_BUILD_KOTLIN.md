# 📱 Market Radar — Native Kotlin & Python Build
## Android Studio + Chaquopy native runtime.

### Current branch notes
Use this document for the native build flow only. For the exact shipped version values, use:
1. `app/build.gradle.kts` for Android `versionName` / `versionCode`
2. `app/src/main/python/brain.py` for `BRAIN_VERSION`
3. `MarketVivi-git/index.html` for the visible web label

Current runtime expectations:
1. Supabase credentials are injected through Gradle / environment into `BuildConfig`.
2. Cleartext traffic is disabled; all endpoints must remain HTTPS.
3. ML evaluation uses canonical persisted outcomes plus derived teacher-report views.

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
