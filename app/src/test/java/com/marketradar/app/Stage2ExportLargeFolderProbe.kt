package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.nio.file.Files

/**
 * R3 measurement (child JVM): a LARGE but admitted research folder (~200 MB, ~5,300 files: 300 x 512 KiB cohort items,
 * 6 x 7.5 MiB sealed items, 5,000 small sealed records, one real collector session). Exports to a real file with
 * readback and prints peak used heap (sampled every 2 ms) separately from zip / temp bytes.
 * Args: [mb scale factor, default 1]. Not a phone measurement.
 */
object Stage2ExportLargeFolderProbe {
    @JvmStatic fun main(args: Array<String>) {
        val dir = Files.createTempDirectory("export-large-input").toFile()
        val temp = Files.createTempDirectory("export-large-temp").toFile()
        val out = File(temp, "doc.zip")
        try {
            val half = "b".repeat(512 * 1024)
            for (i in 0 until 300) M0Durable.write(File(dir, "cohorts/c$i/item_0.json"), JSONObject().put("kind", "CHAIN_AT_OT").put("content", half).toString())
            val big = "c".repeat(7 * 1024 * 1024 + 512 * 1024)
            for (i in 0 until 6) M0Durable.write(File(dir, "cohorts/big$i/item_0.json"), JSONObject().put("kind", "CHAIN_AT_OT").put("content", big).toString())
            val small = "d".repeat(1500)
            for (i in 0 until 5000) M0Durable.write(File(dir, "env_2026-10-${10 + i % 15}_$i.json"), JSONObject().put("envelope_text", small).toString())
            ResearchCollector.setEnabled(dir, true, 1L)
            val bytes = dir.walkTopDown().filter { it.isFile }.sumOf { it.length() }
            val files = dir.walkTopDown().count { it.isFile }
            System.gc()
            // java.lang.management is not on the Android unit-test compile classpath: sample used heap every 2 ms instead
            val rt = Runtime.getRuntime()
            val base = rt.totalMemory() - rt.freeMemory()
            val peakSample = java.util.concurrent.atomic.AtomicLong(base)
            val sampling = java.util.concurrent.atomic.AtomicBoolean(true)
            val sampler = Thread { while (sampling.get()) { peakSample.accumulateAndGet(rt.totalMemory() - rt.freeMemory()) { a, b -> maxOf(a, b) }; Thread.sleep(2) } }
            sampler.isDaemon = true; sampler.start()
            val t0 = System.nanoTime()
            val r = ResearchExport.export(dir, temp, JSONObject().put("probe", "large"), 1L,
                { FileOutputStream(out) }, { FileInputStream(out) }, cancel = { false }, freeSpace = { Long.MAX_VALUE / 4 })
            val ms = (System.nanoTime() - t0) / 1_000_000
            sampling.set(false); sampler.join()
            val peak = peakSample.get()
            println("LARGE_PROBE " + JSONObject().put("max_heap", Runtime.getRuntime().maxMemory()).put("source_files", files)
                .put("source_bytes", bytes).put("status", r.optString("status")).put("zip_bytes", r.opt("zip_bytes"))
                .put("peak_temp_bytes", r.opt("peak_temp_bytes")).put("elapsed_ms", ms).put("heap_before_bytes", base)
                .put("peak_heap_used_bytes_sampled_2ms", peak).put("memory", r.opt("memory")).put("readback_ok", r.optJSONObject("readback")?.optBoolean("ok")))
        } finally { dir.deleteRecursively(); temp.deleteRecursively() }
    }
}
