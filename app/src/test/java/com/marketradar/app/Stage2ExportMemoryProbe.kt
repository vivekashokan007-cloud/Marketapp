package com.marketradar.app

import java.io.File
import java.io.OutputStream
import java.nio.file.Files
import org.json.JSONObject

/** Child-JVM resource probe. All inputs fit the cohort byte cap and exporter limits; no phone/server is touched. */
object Stage2ExportMemoryProbe {
    @JvmStatic fun main(args: Array<String>) {
        val dir = Files.createTempDirectory("export-heap-input").toFile()
        val temp = Files.createTempDirectory("export-heap-temp").toFile()
        try {
            val content = "a".repeat(512 * 1024)
            for (i in 0 until 110) {
                M0Durable.write(File(dir, "cohorts/c$i/item_0.json"), JSONObject()
                    .put("kind", "CHAIN_AT_OT").put("source_ref", "fixture").put("content", content).toString())
            }
            val bytes = dir.walkTopDown().filter { it.isFile }.sumOf { it.length() }
            check(bytes < ResearchCohorts.MAX_TOTAL_BYTES)
            println("source_bytes=$bytes cohort_cap=${ResearchCohorts.MAX_TOTAL_BYTES} max_heap=${Runtime.getRuntime().maxMemory()}")
            val result = ResearchExport.export(dir, temp, JSONObject(), 1L,
                { OutputStream.nullOutputStream() }, null, cancel = { false },
                freeSpace = { Long.MAX_VALUE / 4 })
            println(result.toString())
        } finally { dir.deleteRecursively(); temp.deleteRecursively() }
    }
}
