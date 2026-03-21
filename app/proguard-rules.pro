# Add project specific ProGuard rules here.
# Keep JavaScript interface methods
-keepclassmembers class com.marketradar.app.NativeBridge {
    @android.webkit.JavascriptInterface <methods>;
}
