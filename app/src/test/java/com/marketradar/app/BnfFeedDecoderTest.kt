package com.marketradar.app

import com.upstox.marketdatafeederv3udapi.rpc.proto.MarketDataFeedV3
import org.junit.Assert.*
import org.junit.Test
import java.util.Base64

class BnfFeedDecoderTest {
    @Test fun decodePreservesExchangeClockDepthGreeksAndRawUnknownFields() {
        val market = MarketDataFeedV3.MarketFullFeed.newBuilder()
            .setLtpc(MarketDataFeedV3.LTPC.newBuilder().setLtp(500.0).setLtt(1795743000123))
            .setMarketLevel(MarketDataFeedV3.MarketLevel.newBuilder().addBidAskQuote(
                MarketDataFeedV3.Quote.newBuilder().setBidP(499.0).setAskP(501.0).setBidQ(30).setAskQ(60)))
            .setOptionGreeks(MarketDataFeedV3.OptionGreeks.newBuilder().setDelta(0.5))
            .setIv(0.17).setOi(12345.0)
        val msg = MarketDataFeedV3.FeedResponse.newBuilder().setType(MarketDataFeedV3.Type.live_feed)
            .setCurrentTs(1795743000456).putFeeds("NSE_FO|12345", MarketDataFeedV3.Feed.newBuilder()
                .setFullFeed(MarketDataFeedV3.FullFeed.newBuilder().setMarketFF(market)).build()).build()
        // Unknown future field 99=7 must survive in original bytes.
        val raw = msg.toByteArray() + byteArrayOf(0x98.toByte(), 0x06, 0x07)
        val result = BnfFeedDecoder.frame(raw, 1795743000789)
        assertEquals("DECODED", result.getString("decode_status"))
        assertArrayEquals(raw, Base64.getDecoder().decode(result.getString("raw_protobuf_base64")))
        val fields = result.getJSONObject("decoded").getJSONObject("feeds").getJSONObject("NSE_FO|12345")
            .getJSONObject("fullFeed").getJSONObject("marketFF")
        assertEquals("1795743000123", fields.getJSONObject("ltpc").getString("ltt"))
        assertEquals(499.0, fields.getJSONObject("marketLevel").getJSONArray("bidAskQuote").getJSONObject(0).getDouble("bidP"), 0.0)
        assertEquals(0.17, fields.getDouble("iv"), 0.0)
    }
    @Test fun malformedFrameRemainsRawAndExplicitlyFailed() {
        val raw = byteArrayOf(0x12, 0x7f)
        val r = BnfFeedDecoder.frame(raw, 123)
        assertEquals("DECODE_FAILED", r.getString("decode_status"))
        assertArrayEquals(raw, Base64.getDecoder().decode(r.getString("raw_protobuf_base64")))
    }
}
