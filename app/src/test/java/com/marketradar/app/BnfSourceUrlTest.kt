package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Test

class BnfSourceUrlTest {
    @Test fun capturedLiteralInstrumentSeparatorAndSpacesStillYieldSourceTableIdentity() {
        val original = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=2026-12-29"
        val metadata = BnfSourceUrl.parse(original)
        assertEquals("upstox", metadata.source)
        assertEquals("/v2/option/chain", metadata.endpoint)
        assertEquals("NSE_INDEX|Nifty Bank", metadata.params.getString("instrument_key"))
        assertEquals("2026-12-29", metadata.params.getString("expiry_date"))
    }

    @Test fun encodedQueryAndInternalCaptureKeepExistingMetadata() {
        val encoded = BnfSourceUrl.parse("https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX%7CNifty+Bank&expiry_date=2026-12-29")
        assertEquals("NSE_INDEX|Nifty Bank", encoded.params.getString("instrument_key"))
        val internal = BnfSourceUrl.parse("marketradar://capture/coverage")
        assertEquals("market_radar", internal.source)
        assertEquals("/coverage", internal.endpoint)
    }
}
