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

function normalizePriority(payload: Record<string, unknown>, fallback: unknown): number {
  const raw = payload.priority ?? fallback;
  if (typeof raw === "number" && Number.isFinite(raw)) return Math.trunc(raw);
  const text = String(raw || "").trim();
  return /^-?\d+$/.test(text) ? Number.parseInt(text, 10) : 100;
}

function branchKeyFor(payload: Record<string, unknown>, proposal: Record<string, unknown>, rowId: string) {
  return String(payload.index_key || payload.index || proposal.branch_key || rowId).trim() || rowId;
}

function buildApprovedRow(rowId: string, proposal: Record<string, unknown>) {
  const payload = asObject(proposal.proposal_payload);
  const branchKey = branchKeyFor(payload, proposal, rowId);
  const priority = normalizePriority(payload, proposal.priority);
  const proposalPayload = {
    ...payload,
    proposal_id: String(payload.proposal_id || rowId).trim() || rowId,
    index: String(payload.index || payload.index_key || branchKey),
    index_key: String(payload.index_key || payload.index || branchKey),
    category: String(payload.category || proposal.title || "proposal"),
    priority,
  };
  return {
    proposal_id: rowId,
    branch_key: branchKey,
    title: String(proposal.title || "proposal"),
    summary: String(proposal.summary || ""),
    rationale: String(proposal.rationale || ""),
    proposal_payload: proposalPayload,
    approved_at: new Date().toISOString(),
  };
}

function safeClientError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error || "");
  if (message === "proposal_id is required" || message === "Proposal not found.") {
    return message;
  }
  return "Proposal review failed.";
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
      .select("id,job_id,branch_key,title,summary,rationale,proposal_payload,priority,status")
      .eq("id", proposalId)
      .single();
    if (proposalError) throw proposalError;
    if (!proposal) throw new Error("Proposal not found.");

    const { error: updateError } = await supabase
      .from("evaluator_proposals")
      .update({ status })
      .eq("id", proposalId);
    if (updateError) throw updateError;

    if (status === "approved") {
      const approvedRow = buildApprovedRow(proposalId, proposal);
      const { error: approvedError } = await supabase
        .from("approved_branch_proposals")
        .upsert(approvedRow, { onConflict: "branch_key" });
      if (approvedError) throw approvedError;

      return json({
        ok: true,
        proposal_id: proposalId,
        status,
        synced_live: false,
        synced_approved: true,
        approved_row: approvedRow,
        message: "Proposal approved and stored in approved_branch_proposals.",
      });
    }

    const branchKey = String(proposal.branch_key || "").trim();
    if (branchKey) {
      const { error: deleteError } = await supabase
        .from("approved_branch_proposals")
        .delete()
        .eq("branch_key", branchKey);
      if (deleteError) throw deleteError;
    }

    return json({
      ok: true,
      proposal_id: proposalId,
      status,
      synced_live: false,
      synced_approved: false,
      message: "Proposal rejected and removed from approved_branch_proposals if present.",
    });
  } catch (error) {
    console.error("[evaluator-proposals-review] failed", error);
    return json({ ok: false, error: safeClientError(error) }, { status: 400 });
  }
});
