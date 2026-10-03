package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Test
import java.io.File

/**
 * Writes the SQL fixtures used by `tools/pc2_db_suite/run.sh`, from the CURRENT
 * Kotlin builder, so the PostgreSQL checks can never drift from the code that
 * produces the bytes. A no-op unless PC2_FIXTURE_OUT is set; in ordinary CI it
 * passes instantly.
 *
 *   ingest_vectors.sql  cross-implementation vectors, ingested as anon
 *   session_owner.sql   a realistic IST session as the phone writes it: chain
 *                       evidence 11 s before each poll, snapshots, legacy rows
 *   session_anon.sql    the same session's compact batches, through the
 *                       ingestion function as anon
 */
class Pc2DbFixtureTest {

    private val template = JSONObject(
        """{"constant":"DOW_THRESHOLD","slice_key":"dow_pct|UNKNOWN|UNKNOWN|UNKNOWN","hard_passed":false,""" +
            """"promotion_id":null,"stability_bar":0.1,"support_count":0,"variable_name":"dow_pct",""" +
            """"authority_kind":"parameter_threshold","diversity_pass":false,"observed_value":0.13,""" +
            """"stability_pass":false,"authority_state":"SHADOW",""" +
            """"fallback_reason":"stability_or_history_not_ready|censor_guard_not_clear","neutrality_pass":false,""" +
            """"neutrality_tick":0.01,"stability_ratio":null,"context_variable":"dow_pct","diversity_status":"NO_HISTORY",""" +
            """"neutrality_delta":null,"percentile_passed":false,"censor_guard_flags":["no_history"],""" +
            """"hard_threshold_value":0.5,"input_contract_reasons":[],"authority_policy_version":"pc2_authority_policy_v2"}"""
    )

    private fun dollar(tag: String, text: String): String {
        val quote = "\$$tag\$"
        require(!text.contains(quote)) { "fixture text contains its own quote tag" }
        return "$quote$text$quote"
    }

    @Test
    fun writeFixturesWhenRequested() {
        val out = System.getenv("PC2_FIXTURE_OUT")?.takeIf { it.isNotBlank() }?.let(::File) ?: return
        val key = requireNotNull(System.getenv("PC2_FIXTURE_KEY")?.takeIf { Pc2IngestCredential.isValidKey(it) }) {
            "PC2_FIXTURE_KEY must be a 64-hex device key"
        }
        out.mkdirs()
        File(out, "ingest_vectors.sql").writeText(vectors(key))
        val (owner, anon) = session(key)
        File(out, "session_owner.sql").writeText(owner)
        File(out, "session_anon.sql").writeText(anon)
    }

    /** Version-derivation edge cases and every string class that once diverged. */
    private fun vectors(key: String): String {
        val versions: List<Any?> = listOf(
            null, "", "   ", " \t\n\r\u000B\u000C ", "pc2_authority_policy_v2", "\t pc2_v3 \n",
            " pc2_v4", "pc2 v5", JSONObject.NULL, 7, JSONObject().put("x", 1)
        )
        val strings = listOf(
            "n/a", "</script>", "\u0085 nel", "  ls", "café", "😀", "tab\there",
            "quote\" back\\slash", "ctrl\u0001x", "pipe|sep"
        )
        val sql = StringBuilder()
        versions.forEachIndexed { vi, version ->
            val policy = JSONObject().put("authority_diagnostics_version", "pc2_authority_diagnostics_v1").put("v", vi)
            if (version != null) policy.put("version", version)
            val decisions = JSONArray()
            repeat(6) { i ->
                decisions.put(JSONObject().put("variable_name", "vix_$i").put("observed_value", 0.1 + i / 10.0)
                    .put("reason", strings[(vi + i) % strings.size]).put("bucket", i % 2))
            }
            val built = requireNotNull(Pc2CompactBatch.build(
                JSONObject().put("session_date", "2026-09-30").put("poll_ts", "2026-09-30T11:%02d:00+0530".format(vi))
                    .put("context_json", JSONObject().put("snapshot_pc2_authority_decisions", decisions)
                        .put("snapshot_pc2_authority_policy", policy).put("snapshot_brain_version", "2.6.63"))
            ))
            val body = Pc2IngestCredential.ingestRequestBody(key, built.envelope())
            val tag = "v$vi"
            sql.append("select 'vector $vi derived=' || ${dollar(tag, built.policyRow.getString("policy_version"))}")
                .append(" || ' inserted=' || (public.pc2_ingest_compact_batch(${dollar(tag, key)}, ")
                .append("${dollar(tag, body.getJSONObject("p_policy").toString())}::jsonb, ")
                .append("${dollar(tag, body.getJSONObject("p_batch").toString())}::jsonb) ->> 'batch_inserted');\n")
        }
        return sql.toString()
    }

    /**
     * 13 polls on 2026-10-01 IST: one capped at 128 decisions, one whose
     * snapshot dropped the array (legacy is then the source), one carrying
     * characters that once diverged between platforms, and one with no PC2
     * evidence at all. Chain evidence precedes every poll by 11 s.
     */
    private fun session(key: String): Pair<String, String> {
        val policy = JSONObject().put("version", "pc2_authority_policy_v2")
            .put("authority_diagnostics_version", "pc2_authority_diagnostics_v1")
        val owner = StringBuilder()
        val anon = StringBuilder()
        for (p in 0 until 13) {
            val minutes = 15 + 5 * p
            val hh = 9 + minutes / 60
            val mm = minutes % 60
            val clientTs = "2026-10-01T%02d:%02d:00+0530".format(hh, mm)
            val dbTs = "2026-10-01 %02d:%02d:00+05:30".format(hh, mm)
            val tag = "s$p"
            owner.append("insert into public.ml_option_chain_snapshots (poll_ts, index_key, strike, option_type) values ")
                .append("('$dbTs'::timestamptz - interval '11 seconds', 'NF', 25000, 'CE'), ")
                .append("('$dbTs'::timestamptz - interval '11 seconds', 'NF', 25000, 'PE');\n")
            if (p == 12) {
                // A genuine poll with no PC2 evidence: snapshot only.
                owner.append("insert into public.ml_brain_snapshots (poll_ts, session_date, context_json) values ")
                    .append("('$dbTs', '2026-10-01', '{\"snapshot_brain_version\":\"2.6.64\"}'::jsonb);\n")
                continue
            }
            val n = if (p == 3) 128 else 20 + p
            val decisions = JSONArray()
            repeat(n) { i ->
                decisions.put(JSONObject(template.toString())
                    .put("observed_value", 0.13 + (i % 9) / 100.0)
                    .put("variable_name", "dow_pct_${i % 9}")
                    .put("fallback_reason", if (p == 5) "n/a </x>   café" else template.getString("fallback_reason")))
            }
            val built = requireNotNull(Pc2CompactBatch.build(
                JSONObject().put("session_date", "2026-10-01").put("poll_ts", clientTs)
                    .put("context_json", JSONObject().put("snapshot_pc2_authority_decisions", decisions)
                        .put("snapshot_pc2_authority_policy", policy).put("snapshot_brain_version", "2.6.64"))
            ))
            val context = JSONObject()
                .put("snapshot_pc2_authority_compact_ref", built.snapshotRef)
                .put("snapshot_pc2_authority_policy", policy)
                .put("snapshot_brain_version", "2.6.64")
            if (p != 7) context.put("snapshot_pc2_authority_decisions", decisions)
            owner.append("insert into public.ml_brain_snapshots (poll_ts, session_date, context_json) values ")
                .append("('$dbTs', '2026-10-01', ${dollar(tag, context.toString())}::jsonb);\n")
            for (i in 0 until n) {
                owner.append("insert into public.ml_pc2_authority_decisions (poll_ts, decision_index, decision_json) values ")
                    .append("('$dbTs', $i, ${dollar(tag, decisions.getJSONObject(i).toString())}::jsonb);\n")
            }
            val body = Pc2IngestCredential.ingestRequestBody(key, built.envelope())
            anon.append("select public.pc2_ingest_compact_batch(${dollar(tag, key)}, ")
                .append("${dollar(tag, body.getJSONObject("p_policy").toString())}::jsonb, ")
                .append("${dollar(tag, body.getJSONObject("p_batch").toString())}::jsonb) ->> 'batch_inserted';\n")
        }
        return owner.toString() to anon.toString()
    }
}
