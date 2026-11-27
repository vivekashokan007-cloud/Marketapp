package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class PollNotificationTest {
    @Test fun bnfOnlySavedPollCanUpdateNotificationWithoutADeletedNiftyField() {
        val poll = JSONObject().put("bnf", 63767.0).put("vix", 16.7)
        assertEquals("BNF: 63767.0 | VIX: 16.7", PollNotification.body(poll))
    }
}
