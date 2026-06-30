# 🛡️ Market Radar — Native Android Runtime

Market Radar is a high-performance options trading companion with a native Android runtime, Kotlin bridge, and canonical Python brain (`brain.py`).

## Runtime highlights
- **Native Background Polling**: Stable 5-min tick cycle with `PARTIAL_WAKE_LOCK`.
- **Canonical Python Brain**: 6,400+ lines of Python analysis running locally via Chaquopy.
- **Institutional Positioning**: Automatic 2:00 PM and 3:15 PM positioning snapshots.
- **Supabase Integration**: Real-time cloud sync for trade history and ML performance.
- **Smart Notifications**: Deduplicated, context-aware alerts for FII activity and regime shifts.

## 🛠️ Build Requirements
To build the production APK, you must provide the following environment variables or add them to a non-committed `gradle.properties`:

- `SUPABASE_URL`: Your Supabase project URL.
- `SUPABASE_ANON_KEY`: Your Supabase Anonymous API Key.

### Security Note
Cleartext traffic is disabled. All API calls (Upstox, Supabase, GitHub, hosted PWA) MUST use HTTPS.

## 📦 APK Installation
1. Ensure `SUPABASE_ANON_KEY` is set in your environment.
2. Run `./gradlew assembleDebug` (or use Android Studio).
3. The APK will be generated at `app/build/outputs/apk/debug/app-debug.apk`.

## 🧠 Brain Engine
The core logic resides in `app/src/main/python/brain.py`. It processes live market polls, trades, and institutional data to generate probabilistic "Verdicts" and candidate strategies. This file is now the canonical brain source of truth for the product.

---
*Maintained by Vivek Ashokan • Stabilized by Antigravity AI*
