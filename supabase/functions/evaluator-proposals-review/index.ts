import { adminClient } from "../_shared/client.ts";
import { json, okOptions } from "../_shared/cors.ts";

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function normalizeStatus(raw: unknown): "approved" | "rejected" {
  const value = String(raw || "").trim().toLowerCase();
  return value === "approved" ? "approved" : "rejected";
}

function proposalIdFor(payload: Record<string, unknown>, rowId: string) {
  return String(payload.proposal_id || rowId).trim() || rowId;
}

function buildLiveRow(rowId: string, proposal: Record<string, unknown>, status: "approved" | "rejected") {
  const payload = asObject(proposal.proposal_payload);
  const approved = status === "approved";
  const proposalId = proposalIdFor(payload, rowId);
  return {
    proposal_id: proposalId,
    status,
    index_key: String(payload.index_key || payload.index || "ALL"),
    category: String(payload.category || proposal.title || "proposal"),
    priority: payload.priority ?? proposal.priority ?? 100,
    validation_notes: String(payload.validation_notes || ""),
    approved_by: approved ? "supabase_evaluator_review" : null,
    approved_at: approved ? new Date().toISOString() : null,
    proposal_json: {
      ...payload,
      proposal_id: proposalId,
      status,
      index: payload.index || payload.index_key || "ALL",
      index_key: payload.index_key || payload.index || "ALL",
      category: payload.category || proposal.title || "proposal",
      priority: payload.priority ?? proposal.priority ?? 100,
    },
  };
}

Deno.serve(async (req) => {
  const options = okOptions(req);
  if (options) return options;
  if (req.method !== "POST") return json({ ok: false, error: "Method not allowed" }, { status: 405 });

  try {
    const body = await req.json();
    const proposalId = String(body?.proposal_id || "").trim();
    const status = normalizeStatus(body?.status);
    if (!proposalId) throw new Error("proposal_id is required");

    const supabase = adminClient();
    const { data: proposal, error: proposalError } = await supabase
      .from("evaluator_proposals")
      .select("id,job_id,title,summary,rationale,proposal_payload,priority,status")
      .eq("id", proposalId)
      .single();
    if (proposalError) throw proposalError;

    const { error: updateError } = await supabase
      .from("evaluator_proposals")
      .update({ status })
      .eq("id", proposalId);
    if (updateError) throw updateError;

    const liveRow = buildLiveRow(proposalId, proposal, status);
    const { error: liveError } = await supabase
      .from("ai_branch_proposals")
      .upsert(liveRow, { onConflict: "proposal_id" });
    if (liveError) throw liveError;

    return json({
      ok: true,
      proposal_id: proposalId,
      status,
      synced_live: true,
      live_row: liveRow,
      message: status === "approved"
        ? "Proposal approved and synced to ai_branch_proposals."
        : "Proposal rejected and synced to ai_branch_proposals.",
    });
  } catch (error) {
    return json({ ok: false, error: error.message || String(error) }, { status: 400 });
  }
});
