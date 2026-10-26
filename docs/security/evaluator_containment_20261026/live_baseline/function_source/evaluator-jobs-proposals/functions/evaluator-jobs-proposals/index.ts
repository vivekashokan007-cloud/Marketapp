import { adminClient } from "../_shared/client.ts";
import { json, okOptions } from "../_shared/cors.ts";

function jobIdFromRequest(req: Request) {
  const url = new URL(req.url);
  return String(url.searchParams.get("job_id") || "").trim();
}

Deno.serve(async (req) => {
  const options = okOptions(req);
  if (options) return options;
  if (req.method !== "GET") return json({ ok: false, error: "Method not allowed" }, { status: 405 });

  try {
    const jobId = jobIdFromRequest(req);
    if (!jobId) throw new Error("job_id is required");
    const supabase = adminClient();
    const { data, error } = await supabase
      .from("evaluator_proposals")
      .select("id,job_id,branch_key,title,summary,rationale,proposal_payload,status,confidence,priority,created_at,updated_at")
      .eq("job_id", jobId)
      .order("priority", { ascending: true })
      .order("created_at", { ascending: false });
    if (error) throw error;
    return json({
      ok: true,
      job_id: jobId,
      proposals: data || [],
    });
  } catch (error) {
    return json({ ok: false, error: error.message || String(error) }, { status: 400 });
  }
});
