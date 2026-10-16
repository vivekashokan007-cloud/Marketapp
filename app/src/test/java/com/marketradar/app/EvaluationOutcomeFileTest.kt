package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

class EvaluationOutcomeFileTest {
    private val date = "2026-10-16"
    private fun row(id: Int) = JSONObject().put("snapshot_id", id).put("candidate_id", "nf-$id")
        .put("session_date", date).put("role", "primary").put("detail", """escaped \"[{}] ☺""")

    @Test fun validatesEveryRowBeforeUploadAndCollapsesOnlyIdenticalRetries() {
        val dir = Files.createTempDirectory("eval-stream").toFile()
        try {
            val src = File(dir, "raw.json").apply { writeText("[${row(1)},${row(2)},${row(1)}]") }
            val validated = EvaluationOutcomeFile.validate(date, src, File(dir, "checked.json"))
            assertEquals(2, validated.count)
            assertEquals(1, validated.duplicateCount)
            val uploaded = mutableListOf<Int>()
            EvaluationOutcomeFile.batches(validated.file, maxRows = 1) { batch ->
                assertEquals(1, batch.length()); uploaded.add(batch.getJSONObject(0).getInt("snapshot_id"))
            }
            assertEquals(listOf(1, 2), uploaded)
            assertTrue(src.readText().contains("escaped"))
            src.writeText("[${row(1)},${row(1).put("different", true)}]")
            assertThrows(IllegalStateException::class.java) {
                EvaluationOutcomeFile.validate(date, src, File(dir, "checked.json"))
            }
            // Failed validation does not replace the last validated generation or original evidence.
            assertEquals(2, validated.count)
            assertTrue(src.readText().contains("different"))
            var oldRows = 0; EvaluationOutcomeFile.forEach(validated.file) { oldRows++ }; assertEquals(2, oldRows)
        } finally { dir.deleteRecursively() }
    }

    @Test fun lateMalformedInputAndInvalidIdentityNeverPublishAValidatedFile() {
        val dir = Files.createTempDirectory("eval-malformed").toFile()
        try {
            for (content in listOf("[${row(1)},", "[${row(1)},]", "[${row(1)}] garbage", "[${row(1).put("session_date", "2026-10-15")}]") ) {
                val src = File(dir, "raw.json").apply { writeText(content) }
                val target = File(dir, "checked.json")
                assertThrows(Exception::class.java) { EvaluationOutcomeFile.validate(date, src, target) }
                assertFalse(target.exists()); assertEquals(content, src.readText())
            }
        } finally { dir.deleteRecursively() }
    }

    @Test fun failedUploadCanReplayTheSameValidatedInputWithoutLosingRows() {
        val dir = Files.createTempDirectory("eval-replay").toFile()
        try {
            val src = File(dir, "raw.json").apply { writeText("[${row(1)},${row(2)},${row(3)}]") }
            val checked = EvaluationOutcomeFile.validate(date, src, File(dir, "checked.json"))
            var attempts = 0
            assertThrows(IllegalStateException::class.java) {
                EvaluationOutcomeFile.batches(checked.file, maxRows = 1) { if (++attempts == 2) error("HTTP failure") }
            }
            var replayed = 0
            EvaluationOutcomeFile.batches(checked.file, maxRows = 1) { replayed += it.length() }
            assertEquals(3, replayed)
        } finally { dir.deleteRecursively() }
    }

    @Test fun completedBatchIsDurableBeforeBudgetContinuationAndSaveFailureDoesNotAdvance() {
        var now = 0L
        val budget = EvaluationBudget(45, { now })
        var checkpoint = 40
        now = 46
        assertThrows(IllegalStateException::class.java) {
            budget.commitBatch { checkpoint = 44 }
        }
        assertEquals(44, checkpoint)
        assertThrows(IllegalArgumentException::class.java) {
            budget.commitBatch { throw IllegalArgumentException("disk full") }
        }
        assertEquals(44, checkpoint)
    }

    @Test fun outcomeFileLargerThanPhoneHeapCompletesInA64MiBJavaProcess() {
        val classes = listOf(EvaluationOutcomeMemoryProbe::class.java, EvaluationOutcomeFile::class.java,
            JSONObject::class.java, kotlin.Unit::class.java).map { File(it.protectionDomain.codeSource.location.toURI()).path }.distinct()
        val process = ProcessBuilder(File(System.getProperty("java.home"), "bin/java").path,
            "-Xmx64m", "-cp", classes.joinToString(File.pathSeparator), EvaluationOutcomeMemoryProbe::class.java.name)
            .redirectErrorStream(true).start()
        val output = process.inputStream.bufferedReader().readText()
        assertEquals(output, 0, process.waitFor())
        assertTrue(output, output.contains("LARGE_OUTCOME_RECOVERY_OK"))
    }
}

/** Executed under a smaller heap than the real phone; never allocates the complete test file. */
object EvaluationOutcomeMemoryProbe {
    @JvmStatic fun main(args: Array<String>) {
        val dir = Files.createTempDirectory("eval-low-heap").toFile()
        try {
            val raw = File(dir, "raw.json")
            val padding = "x".repeat(256 * 1024)
            val rows = 1030
            raw.bufferedWriter().use { writer ->
                writer.write("[")
                repeat(rows) { i ->
                    if (i > 0) writer.write(",")
                    writer.write(JSONObject().put("snapshot_id", i+1).put("candidate_id", "nf-$i")
                        .put("session_date", "2026-10-16").put("role", "primary").put("retained_evidence", padding).toString())
                }
                writer.write("]")
            }
            check(raw.length() > 256L * 1024 * 1024)
            val verified = EvaluationOutcomeFile.validate("2026-10-16", raw, File(dir, "verified.json"))
            var uploaded = 0
            EvaluationOutcomeFile.batches(verified.file) { uploaded += it.length(); check(it.length() <= 32) }
            check(uploaded == rows && verified.count == rows)
            println("LARGE_OUTCOME_RECOVERY_OK rows=$rows bytes=${raw.length()} maxHeap=${Runtime.getRuntime().maxMemory()}")
        } finally { dir.deleteRecursively() }
    }
}
