export type EvaluationJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface EvaluationJobRequest {
  date_from: string;
  date_to: string;
  index_scope?: string[];
  mode?: string;
  requested_by?: string;
  model_name?: string;
}

export function normalizeIndexScope(raw: unknown): string[] {
  const arr = Array.isArray(raw) ? raw : [];
  const cleaned = arr
    .map((v) => String(v || "").trim().toUpperCase())
    .filter((v) => v === "BNF" || v === "NF");
  return cleaned.length ? Array.from(new Set(cleaned)) : ["BNF", "NF"];
}

export function normalizeRequest(payload: Partial<EvaluationJobRequest>): EvaluationJobRequest {
  const dateFrom = String(payload.date_from || "").trim();
  const dateTo = String(payload.date_to || "").trim();
  if (!dateFrom || !dateTo) {
    throw new Error("date_from and date_to are required");
  }
  return {
    date_from: dateFrom,
    date_to: dateTo,
    index_scope: normalizeIndexScope(payload.index_scope),
    mode: String(payload.mode || "manual_advisory"),
    requested_by: String(payload.requested_by || "market_radar_app"),
    model_name: String(payload.model_name || "gemini-2.5-pro"),
  };
}
