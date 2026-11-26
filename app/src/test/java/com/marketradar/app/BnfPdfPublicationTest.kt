package com.marketradar.app

import android.app.Application
import com.tom_roush.pdfbox.android.PDFBoxResourceLoader
import com.tom_roush.pdfbox.pdmodel.PDDocument
import com.tom_roush.pdfbox.text.PDFTextStripper
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = Application::class)
class BnfPdfPublicationTest {
    @Test fun actualOfficialPdfPassesAndroidExtractorAndAllFourteenChecks() {
        PDFBoxResourceLoader.init(RuntimeEnvironment.getApplication())
        val pdf = javaClass.getResourceAsStream("/bnf_reference/official_oct2026.pdf")!!.use { it.readBytes() }
        val text = PDDocument.load(pdf).use { document -> PDFTextStripper().apply { sortByPosition = true }.getText(document) }
        val csv = javaClass.getResourceAsStream("/bnf_reference/members.csv")!!.bufferedReader().use { it.readText() }
        val result = BnfWeightPublication.parse(text, BnfReferenceCapture.parseMembers(csv), "2026-11-26")
        assertEquals("VALIDATED", result.getString("status"))
        assertEquals(14, result.getJSONArray("members").length())
        assertEquals("2026-10-30", result.getString("as_of_date"))
    }
}
