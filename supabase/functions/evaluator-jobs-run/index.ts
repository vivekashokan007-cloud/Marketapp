import { adminClient } from "../_shared/client.ts";
import { json, okOptions } from "../_shared/cors.ts";

function summarizeWindow(job: Record<string, unknown>) {
  return {
    date_from: job.date_from,
    date_to: job.date_to,
    advisory_only: true,
    source: "supabase_edge_stub",
  };
}

function buildBriefArtifact(jobId: string, job: Record<string, unknown>) {
  const requestPayload = (job.request_payload && typeof job.request_payload === "object")
    ? job.request_payload as Record<string, unknown>
    : {};
  return {
    job_id: jobId,
    artifact_kind: "prompt_bundle_stub",
    prompt_version: "supabase_scaffold_v1",
    input_payload: {
      schema_version: "brief_v1",
      job: {
        job_id: jobId,
        date_from: job.date_from,
        date_to: job.date_to,
        index_scope: Array.isArray(job.index_scope) ? job.index_scope : ["BNF", "NF"],
        mode: requestPayload.mode || "manual_advisory",
        model_name: job.model_name,
      },
      inputs: {
        session_dates: [],
        snapshot_count: 0,
        decision_count: 0,
        teacher_row_count: 0,
      },
      market_context: {
        regime_summary: [],
        vix_summary: [],
        lane_mix: [],
      },
      teacher_findings: {
        chosen_summary: {},
        alternative_summary: {},
        worth_it_summary: {},
      },
      proposal_task: {
        objective: "propose branch/filter/ranking changes only",
        guardrails: [
          "no live trade authority",
          "no auto approval",
          "no runtime mutation",
        ],
      },
      stub: {
        note: "Stub brief artifact. Replace with real session research bundle later.",
        request_payload: requestPayload,
      },
    },
    snapshot_count: 0,
    decision_count: 0,
  };
}

function buildVerdictArtifact(jobId: string, job: Record<string, unknown>) {
  return {
    job_id: jobId,
    verdict_kind: "gemini_response_stub",
    model_name: job.model_name,
    raw_text: "Stub run only. No Gemini call executed.",
    normalized_payload: {
      schema_version: "verdict_v1",
      job_id: jobId,
      advisory_only: true,
      summary: {
        proposal_count: 0,
        high_confidence_count: 0,
        notes: [
          "Stub evaluator run only.",
          "No Gemini call executed.",
        ],
      },
      proposals: [],
    },
    token_usage: {},
  };
}

Deno.serve(async (req) => {
  const options = okOptions(req);
  if (options) return options;
  if (req.method !== "POST") return json({ ok: false, error: "Method not allowed" }, { status: 405 });

  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
    const jobId = String(body?.job_id || "").trim();
    if (!jobId) throw new Error("job_id is required");

    const supabase = adminClient();
    const { data: job, error: jobError } = await supabase
      .from("evaluator_jobs")
      .select("*")
      .eq("job_id", jobId)
      .single();
    if (jobError) throw jobError;

    await supabase
      .from("evaluator_jobs")
      .update({
        status: "running",
        started_at: new Date().toISOString(),
      })
      .eq("job_id", jobId);

    const briefArtifact = buildBriefArtifact(jobId, job);
    const verdictArtifact = buildVerdictArtifact(jobId, job);

    await supabase.from("evaluator_brief_artifacts").insert(briefArtifact);
    await supabase.from("evaluator_verdict_artifacts").insert(verdictArtifact);

    const { error: updateError } = await supabase
      .from("evaluator_jobs")
      .update({
        status: "completed",
        completed_at: new Date().toISOString(),
        proposal_count: 0,
        summary: summarizeWindow(job),
        error: null,
      })
      .eq("job_id", jobId);
    if (updateError) throw updateError;

    return json({
      ok: true,
      job_id: jobId,
      status: "completed",
      proposal_count: 0,
      message: "Stub evaluator run completed without proposals.",
    });
  } catch (error) {
    const message = error.message || String(error);
    try {
      const jobId = String(body?.job_id || "").trim();
      if (jobId) {
        const supabase = adminClient();
        await supabase
          .from("evaluator_jobs")
          .update({
            status: "failed",
            error: message,
            completed_at: new Date().toISOString(),
          })
          .eq("job_id", jobId);
      }
    } catch (_) {}
    return json({ ok: false, error: message }, { status: 400 });
  }
});
