import { adminClient } from "../_shared/client.ts";
import { normalizeRequest } from "../_shared/contracts.ts";
import { json, okOptions } from "../_shared/cors.ts";

function deriveMonthKey(dateFrom: string, dateTo: string) {
  const candidate = (dateTo || dateFrom || "").trim();
  const match = candidate.match(/^(\d{4}-\d{2})/);
  if (!match) {
    throw new Error("Could not derive month_key from date range");
  }
  return match[1];
}

Deno.serve(async (req) => {
  const options = okOptions(req);
  if (options) return options;
  if (req.method !== "POST") return json({ ok: false, error: "Method not allowed" }, { status: 405 });

  try {
    const payload = normalizeRequest(await req.json());
    const supabase = adminClient();
    const row = {
      month_key: deriveMonthKey(payload.date_from, payload.date_to),
      requested_by: payload.requested_by,
      runner_type: "supabase_edge",
      model_name: payload.model_name,
      date_from: payload.date_from,
      date_to: payload.date_to,
      index_scope: payload.index_scope,
      request_payload: payload,
      status: "queued",
      summary: {
        mode: payload.mode,
        advisory_only: true,
      },
    };
    const { data, error } = await supabase
      .from("evaluator_jobs")
      .insert(row)
      .select("job_id,status,request_payload,proposal_count,created_at,updated_at")
      .single();
    if (error) throw error;
    return json({
      ok: true,
      ...data,
      message: "Evaluator job queued in advisory-only mode.",
    });
  } catch (error) {
    return json({ ok: false, error: error.message || String(error) }, { status: 400 });
  }
});
