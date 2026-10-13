package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

/** b503 round 4 (Codex review of 64e7424): C3 part A (full typed sampled witness) and part B (common manifest contract). */
class B503ReviewRound4Test {
    private val day = "2026-10-13"
    private val F = GeneratedCohort.T.FLOAT8

    private fun build(nf: Int, bnf: Int, fp: Boolean = true): CaptureEngine.BuiltChannels {
        val r = JSONObject().put("brain_version", "2.6.72").put("watchlist", JSONArray())
        if (fp) r.put("elephant_fact_pack", B503Harness.richFactPack("${day}T09:50:00+0530", nf, bnf))
        return B503Harness().engine.build(B503Harness.request(result = r, attempt = "r4"), CaptureDelivery.LATE,
            CaptureCoordinator.Computed(B503Harness.pythonSnapshot("${day}T09:50:42+0530"), 1, 1), 0, 0)
    }

    private fun shape(b: CaptureEngine.BuiltChannels): Pair<JSONObject, List<JSONObject>> {
        val snap = JSONObject(b.channels.getString("snapshot"))
        val s = JSONObject().put("poll_ts_utc", "2026-10-13T04:20:42Z").put("session_date", snap.getString("session_date"))
            .put("recommendation_id", snap.opt("recommendation_id"))
            .put("capture_provenance", snap.getJSONObject("poll_summary_json").getJSONObject("capture_provenance"))
            .put("generated_cohort_manifest", snap.getJSONObject("poll_summary_json").getJSONObject("generated_cohort_manifest"))
        val rows = b.channels.optString("generated").takeIf { it.isNotEmpty() }?.let { JSONObject(it).getJSONArray("rows") } ?: JSONArray()
        // what the exporter hands the audit: the uploaded JSON re-parsed from text, plus the UTC lineage key
        val g = (0 until rows.length()).map { i -> JSONObject(rows.getJSONObject(i).toString()).also {
            it.put("snapshot_poll_ts_utc", Instant.ofEpochSecond(Pc2ParityAudit.parseInstant(it.getString("snapshot_poll_ts"))!!.epochSecond).toString()) } }
        return s to g
    }

    private fun audit(s: JSONObject, g: List<JSONObject>) = CaptureCohortAudit.audit(day, listOf(s), g)
    private fun copy(g: List<JSONObject>) = g.map { JSONObject(it.toString()) }
    private fun m(s: JSONObject) = s.getJSONObject("generated_cohort_manifest")

    // ---- coverage ----

    @Test fun coverage_everyCaptureWrittenColumn_exactly_typed() {
        val schema = mapOf( // public.ml_generated_candidates as given by Codex (read-only), minus server id/created_at
            "TIMESTAMPTZ" to "snapshot_poll_ts", "DATE" to "session_date",
            "TEXT" to "recommendation_id quality_tag decision_source candidate_id lane index_key strategy_type trade_mode risk_reward expiry execution_gate entry_action ml_action ml_regime app_version brain_version",
            "INTEGER" to "rank watchlist_rank width generated_count watchlist_count",
            "BOOLEAN" to "was_surfaced capital_blocked is_credit execution_ready direction_safe ml_unsure ml_ood_flag",
            "FLOAT8" to "net_premium max_profit max_loss premium_edge ev_per_1k est_cost sigma_otm iv_richness credit_width_ratio brain_score p_ml ml_edge signal_independence_score")
            .flatMap { (t, cols) -> cols.split(' ').map { it to t } }.toMap()
        assertEquals(43, schema.size)
        assertEquals(schema, GeneratedCohort.COLUMNS.associate { it.first to it.second.name })
        // both row builders write exactly these keys (normal and synthetic rejected rows)
        val (_, g) = shape(build(2, 1))
        g.forEach { r -> assertEquals(schema.keys, r.keys().asSequence().toSet() - "snapshot_poll_ts_utc") }
        val rej = GeneratedCandidateRows.build(JSONObject().put("poll_timestamp", "${day}T09:50:00+0530").put("session_date", day).put("candidates", JSONArray())
            .put("rejected_candidates", JSONArray().put(JSONObject().put("lane", "NF_intraday").put("index", "NF").put("netPremium", 3.5))),
            JSONObject().put("poll_ts", "${day}T09:50:00+0530").put("session_date", day))
        assertEquals(1, rej.length())
        assertEquals(schema.keys, rej.getJSONObject(0).keys().asSequence().toSet())
    }

    @Test fun manifest_isV2_frozenInSnapshot_bounded() {
        val (s, g) = shape(build(30, 30))
        val man = m(s)
        assertEquals("generated_cohort_manifest_v2", man.getString("schema")); assertEquals("generated_row_witness_v2", man.getString("contract"))
        assertEquals(GeneratedCohort.COLUMN_CONTRACT, man.getString("columns"))
        assertEquals(50, man.getInt("row_count")); assertEquals(50, g.size)
        assertEquals(43, man.getJSONObject("col_sha").length()); assertEquals(0, man.getJSONArray("type_errors").length())
        assertTrue("bounded payload ${man.toString().length}", man.toString().length < 16_000)
        assertTrue(audit(s, g).toJson().toString(), audit(s, g).verified)
        assertTrue(audit(s, g).toJson().getString("certifies").contains("SAMPLED content completeness"))
    }

    @Test fun v1_partialWitness_cannotPass() {
        val (s, g) = shape(build(1, 1))
        m(s).put("schema", "generated_cohort_manifest_v1")
        assertEquals(1, audit(s, g).count("COHORT_WRONG_VERSION")); assertFalse(audit(s, g).verified)
    }

    // ---- A: per-field independent mutation of every covered column ----

    private fun changed(t: GeneratedCohort.T, v: Any?): Any = when (t) {
        GeneratedCohort.T.TIMESTAMPTZ -> Instant.ofEpochSecond(Pc2ParityAudit.parseInstant(v as String)!!.epochSecond + 1).toString()
        GeneratedCohort.T.DATE -> "2026-10-12"
        GeneratedCohort.T.TEXT -> if (v == null || v == JSONObject.NULL) "X" else "$v~"
        GeneratedCohort.T.INTEGER -> if (v is Number) v.toInt() + 1 else 7
        GeneratedCohort.T.BOOLEAN -> if (v is Boolean) !v else true
        GeneratedCohort.T.FLOAT8 -> if (v is Number) Math.nextUp(v.toDouble()).let { if (it.isInfinite()) Math.nextDown(v.toDouble()) else it } else 1.5
    }

    @Test fun A_everyCoveredColumn_independentMutation_isDetected_andNamed() {
        val (s, g) = shape(build(3, 2))
        assertTrue(audit(s, g).verified)
        for ((col, t) in GeneratedCohort.COLUMNS) for (row in g.indices) {
            val mg = copy(g); mg[row].put(col, changed(t, mg[row].opt(col)))
            val r = audit(s, mg)
            assertFalse("$col row $row undetected", r.verified)
            when (col) {
                "candidate_id", "lane", "rank" -> { assertEquals(col, 1, r.count("COHORT_ROWS_MISSING")); assertEquals(col, 1, r.count("COHORT_EXTRA_ROWS")) }
                "index_key" -> assertEquals(col, 1, r.count("COHORT_WRONG_INDEX"))
                else -> {
                    assertEquals(col, 1, r.count("COHORT_CONTENT_CONFLICT"))
                    val d = r.findings.single { it.category == "COHORT_FIELD_CONFLICT" }.detail
                    assertEquals("$col -> $d", "columns differing from the capture-time witness: [$col]", d)
                }
            }
        }
    }

    @Test fun A_codexNamedFields_economicProbabilityFeeBoolTextRankCountLineageVersion() {
        val (s, g) = shape(build(2, 2))
        listOf("max_loss" to 999999.75, "net_premium" to 0.0, "p_ml" to 0.51, "est_cost" to 41.0, "execution_ready" to false,
            "ml_regime" to "MUTATED_REGIME", "watchlist_rank" to 9, "generated_count" to 99, "recommendation_id" to "other",
            "app_version" to "2.6.71", "brain_version" to "x", "session_date" to "2026-10-14").forEach { (c, v) ->
            assertFalse(c, audit(s, copy(g).also { it[0].put(c, v) }).verified)
        }
    }

    // ---- A: representation variants vs real changes ----

    @Test fun A_canonical_equivalentRepresentationsAreEqual() {
        val c = GeneratedCohort::canonical
        listOf<Any>(1, 1L, 1.0, "1", "1.0", "1e0", "1.000", "0.1e1", java.math.BigDecimal("1.0000")).forEach { assertEquals("$it", c(F, 1.0), c(F, it)) }
        assertEquals(c(F, 1000.25), c(F, JSONObject("{\"v\":1.00025e3}").get("v")))
        assertEquals(c(F, 1000.25), c(F, JSONObject("{\"v\":1000.250}").get("v")))
        assertEquals(c(F, 0.30000000000000004), c(F, 0.1 + 0.2))
        assertEquals(c(GeneratedCohort.T.INTEGER, 3), c(GeneratedCohort.T.INTEGER, 3.0))
        assertEquals(c(GeneratedCohort.T.INTEGER, 3), c(GeneratedCohort.T.INTEGER, "3"))
        val ts = GeneratedCohort.T.TIMESTAMPTZ
        listOf("2026-10-13T04:20:00Z", "2026-10-13 04:20:00+00", "2026-10-13T09:50:00+0530", "2026-10-13T09:50:00+05:30", "2026-10-13T04:20:00.000000+00:00")
            .forEach { assertEquals(it, c(ts, "2026-10-13T09:50:00+0530"), c(ts, it)) }
        assertNotEquals(c(ts, "2026-10-13T04:20:00Z"), c(ts, "2026-10-13T04:20:00.000001Z"))
        assertEquals(c(GeneratedCohort.T.TEXT, "2.5"), c(GeneratedCohort.T.TEXT, 2.5))
        assertEquals(c(GeneratedCohort.T.BOOLEAN, true), c(GeneratedCohort.T.BOOLEAN, "true"))
    }

    @Test fun A_representationVariantsInStoredRows_stillVerify() {
        val (s, g) = shape(build(2, 1))
        fun lit(t: String) = JSONObject("{\"v\":$t}").get("v")
        val v = copy(g).onEach { r ->
            r.put("max_loss", lit("1.00025E3")).put("iv_richness", lit("1.000")).put("max_profit", "100000") // float8out text form
                .put("rank", (r.get("rank") as Number).toDouble()).put("generated_count", lit("3.0e0"))
                .put("snapshot_poll_ts", "2026-10-13 04:20:00+00") // PG text form of the same instant
        }
        assertNotEquals(g.toString(), v.toString())
        assertTrue(audit(s, v).toJson().toString(), audit(s, v).verified)
    }

    @Test fun A_realChanges_smallRepresentable_nullVsValue_negative_extreme() {
        val c = GeneratedCohort::canonical
        assertNotEquals(c(F, 1000.25), c(F, Math.nextUp(1000.25)))
        assertNotEquals(c(F, 0.0), c(F, -0.0))
        assertNotEquals(c(F, -0.1), c(F, 0.1))
        assertNotEquals(c(F, 0.0), c(F, null)); assertEquals("N", c(F, JSONObject.NULL))
        assertNotEquals(c(GeneratedCohort.T.TEXT, ""), c(GeneratedCohort.T.TEXT, null))
        assertNotEquals(c(GeneratedCohort.T.BOOLEAN, false), c(GeneratedCohort.T.BOOLEAN, null))
        assertEquals(c(F, Double.MAX_VALUE), c(F, "1.7976931348623157E308")); assertEquals(c(F, Double.MIN_VALUE), c(F, "4.9E-324"))
        assertNotEquals(c(F, Double.MAX_VALUE), c(F, Math.nextDown(Double.MAX_VALUE)))
        val (s, g) = shape(build(3, 0))
        assertFalse(audit(s, copy(g).also { it[1].put("watchlist_rank", 1) }).verified)     // null -> value
        assertFalse(audit(s, copy(g).also { it[0].put("watchlist_rank", JSONObject.NULL) }).verified) // value -> null
        assertFalse(audit(s, copy(g).also { it[0].put("premium_edge", 0.1) }).verified)       // sign
        assertFalse(audit(s, copy(g).also { it[0].put("ev_per_1k", Math.nextDown(Double.MAX_VALUE)) }).verified)
        assertFalse(audit(s, copy(g).also { it[2].put("sigma_otm", 0.0) }).verified)          // -0.0 -> 0.0
    }

    @Test fun A_nonFiniteAndTypeErrors_neverVerify() {
        val c = GeneratedCohort::canonical
        listOf("NaN", "Infinity", "-Infinity").forEach { assertEquals("!nonfinite", c(F, it)) }
        assertEquals("!nonfinite", c(F, Double.NaN))
        assertTrue(c(F, "abc").startsWith("!type")); assertTrue(c(F, true).startsWith("!type"))
        assertTrue(c(GeneratedCohort.T.INTEGER, 2.5).startsWith("!type")); assertTrue(c(GeneratedCohort.T.INTEGER, 3_000_000_000L).startsWith("!type"))
        assertTrue(c(GeneratedCohort.T.BOOLEAN, "yes").startsWith("!type")); assertTrue(c(GeneratedCohort.T.BOOLEAN, 1).startsWith("!type"))
        assertTrue(c(GeneratedCohort.T.DATE, "2026-13-40").startsWith("!type")); assertTrue(c(GeneratedCohort.T.TIMESTAMPTZ, "soon").startsWith("!type"))
        // stored invalid value
        val (s, g) = shape(build(1, 0))
        val bad = audit(s, copy(g).also { it[0].put("max_loss", "NaN") })
        assertFalse(bad.verified); assertEquals(1, bad.count("COHORT_INVALID_STORED_VALUE"))
        // capture-time type error is frozen into the manifest and makes it uncertifiable
        val row = JSONObject(g[0].toString()).also { it.remove("snapshot_poll_ts_utc"); it.put("width", 2.5) }
        val man = GeneratedCohort.expected(JSONArray().put(row), day, s.opt("recommendation_id"))
        assertEquals(1, man.getJSONArray("type_errors").length())
        assertTrue(GeneratedCohort.validate(man).contains("capture_value_type_errors"))
    }

    @Test fun A_missingExportedColumn_isInputIncomplete() {
        val (s, g) = shape(build(1, 1))
        val r = audit(s, copy(g).onEach { it.remove("ml_edge") })
        assertFalse(r.verified); assertEquals(1, r.count("INPUT_INCOMPLETE"))
    }

    // ---- B: common manifest contract ----

    private fun recompute(man: JSONObject) {
        val e = man.getJSONArray("rows").let { a -> (0 until a.length()).map { a.getJSONObject(it) } }
        man.put("cohort_sha256", GeneratedCohort.cohortSha(e))
    }

    @Test fun B_notExpected_contradictions_allRejected() {
        val (zs, _) = shape(build(0, 0)); val zero = m(zs)
        assertEquals("NOT_EXPECTED", zero.getString("status")); assertTrue(GeneratedCohort.validate(zero).isEmpty())
        assertTrue("legitimate zero cohort", audit(zs, emptyList()).verified)
        val (ns, _) = shape(build(0, 0, fp = false))
        assertEquals("no_fact_pack", m(ns).getString("reason")); assertTrue(audit(ns, emptyList()).verified)
        val entry = JSONObject().put("k", "S:NF-0|S:NF_intraday|I:1").put("i", "NF").put("w", "a".repeat(64))
        val cases = mapOf<String, (JSONObject) -> Unit>(
            "row_count" to { it.put("row_count", 1) },
            "entries" to { it.put("rows", JSONArray().put(entry)) },
            "entries_with_sha" to { it.put("rows", JSONArray().put(entry)); recompute(it) },
            "per_index_nf" to { it.getJSONObject("per_index").put("NF", 1) },
            "per_index_bnf" to { it.getJSONObject("per_index").put("BNF", 2) },
            "digest" to { it.put("cohort_sha256", "0".repeat(64)) },
            "col_sha" to { it.getJSONObject("col_sha").put("max_loss", "0".repeat(32)) },
            "reason" to { it.put("reason", "warmup_guess") },
            "reason_missing" to { it.remove("reason") },
            "lineage" to { it.put("snapshot_poll_ts", JSONObject.NULL) },
            "lineage_garbage" to { it.put("snapshot_poll_ts", "later") },
            "schema" to { it.put("contract", "generated_row_witness_v1") },
            "cap" to { it.put("sample_cap", 49) },
            "cap_type" to { it.put("sample_cap", "50") },
        )
        cases.forEach { (name, mut) ->
            val s = JSONObject(zs.toString()); mut(m(s))
            assertFalse(name, audit(s, emptyList()).verified)
        }
        // Codex: status flipped to NOT_EXPECTED over a 1-row EXPECTED manifest
        val (es, _) = shape(build(1, 0)); m(es).put("status", "NOT_EXPECTED").put("reason", "zero_candidates")
        assertEquals(1, audit(es, emptyList()).count("INVALID_COHORT_MANIFEST"))
    }

    @Test fun B_expected_contradictions_allRejected() {
        val (base, g) = shape(build(2, 2))
        val cases = mapOf<String, (JSONObject) -> Unit>(
            "cap" to { it.put("sample_cap", 60) },
            "dup_identity" to { val a = it.getJSONArray("rows"); a.put(1, JSONObject(a.getJSONObject(0).toString())); it.put("row_count", 4); recompute(it) },
            "index_not_allowed" to { val a = it.getJSONArray("rows"); a.getJSONObject(0).put("i", "NIFTY"); recompute(it) },
            "per_index_extra_key" to { it.getJSONObject("per_index").put("NIFTY", 0) },
            "per_index_vs_rows" to { it.getJSONObject("per_index").put("NF", 3).put("BNF", 1) },
            "count" to { it.put("row_count", 3) },
            "digest" to { it.put("cohort_sha256", "f".repeat(64)) },
            "entry_drop_with_sha" to { val a = it.getJSONArray("rows"); a.remove(0); recompute(it) },
            "bad_witness_type" to { it.getJSONArray("rows").getJSONObject(0).put("w", 5) },
            "row_count_type" to { it.put("row_count", "4") },
            "rows_type" to { it.put("rows", "[]") },
            "status_unknown" to { it.put("status", "MAYBE") },
            "reason_on_expected" to { it.put("reason", "zero_candidates") },
            "lineage_null" to { it.put("snapshot_poll_ts", JSONObject.NULL) },
            "columns_contract" to { it.put("columns", "max_loss:FLOAT8") },
            "col_sha_missing_col" to { it.getJSONObject("col_sha").remove("p_ml") },
            "over_cap" to { val a = it.getJSONArray("rows"); repeat(48) { n -> a.put(JSONObject().put("k", "S:x$n").put("i", "NF").put("w", "b".repeat(64))) } },
        )
        cases.forEach { (name, mut) ->
            val s = JSONObject(base.toString()); mut(m(s))
            val r = audit(s, g)
            assertFalse(name, r.verified); assertTrue(name + r.toJson(), r.count("INVALID_COHORT_MANIFEST") == 1)
        }
        for (k in listOf("schema", "contract", "columns", "status", "sample_cap", "population", "session_date", "row_count", "rows", "per_index", "cohort_sha256", "col_sha", "type_errors")) {
            val s = JSONObject(base.toString()); m(s).remove(k)
            assertFalse("missing $k", audit(s, g).verified)
        }
    }

    @Test fun B_buildFailed_alwaysFails_evenIfStructurallyValid() {
        val man = GeneratedCohort.buildFailed("fact_pack_unparseable", "${day}T09:50:00+0530", day, null)
        assertTrue(GeneratedCohort.validate(man).isEmpty())
        val (s, _) = shape(build(0, 0)); s.put("generated_cohort_manifest", man.put("recommendation_id", m(s).opt("recommendation_id")))
        val r = audit(s, emptyList()); assertFalse(r.verified); assertEquals(1, r.count("GENERATED_BUILD_FAILED"))
    }

    @Test fun preserved_missingExtraOrphanDuplicateWrongIndex_andGatesClosed() {
        val (s, g) = shape(build(3, 2))
        assertEquals(1, audit(s, g.drop(1)).count("COHORT_ROWS_MISSING"))
        assertEquals(1, audit(s, g + JSONObject(g[0].toString()).put("candidate_id", "NF-9")).count("COHORT_EXTRA_ROWS"))
        assertEquals(1, audit(s, g + JSONObject(g[0].toString())).count("COHORT_DUPLICATE_ROWS"))
        assertEquals(1, audit(s, g + JSONObject(g[0].toString()).put("snapshot_poll_ts_utc", "2026-10-13T04:40:00Z")).count("ORPHAN_GENERATED_ROWS"))
        val r = CaptureReadiness.report(Pc2ParityAudit.audit(null, emptyList(), emptyList(), emptyList()), audit(s, g))
        assertEquals("CLOSED", r.getString("c3_gate")); assertEquals("CLOSED", r.getString("source_cap_gate")); assertFalse(r.getBoolean("full_population_verified"))
    }
}
