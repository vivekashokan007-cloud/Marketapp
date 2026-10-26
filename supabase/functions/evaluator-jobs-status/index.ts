// CDX-GROK-CONTAINMENT-PREP-1 — PROPOSED containment C1 (review branch; DO NOT DEPLOY without an explicit decision).
// Fixed deny handler for the evaluator Edge Function "evaluator-jobs-status".
// * Every method (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS, ...) and every path answers 403.
// * No imports, no environment / secret access, no database client, no request-body read, no CORS grant
//   (no Access-Control-Allow-* headers, so browser preflights fail closed).
// * Self-contained so the deployed bundle is reproducible from this file alone.
// The previous deployed handler (live v1/v2) is preserved as review-only audit evidence under
// docs/security/evaluator_containment_20261026/live_baseline/ and is NOT an automatic recovery target.
const FUNCTION_NAME = "evaluator-jobs-status";
const CONTAINMENT_REF = "CDX-GROK-CONTAINMENT-PREP-1";

export function containedResponse(req: Request): Response {
  const headers = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  };
  if (req.method === "HEAD") return new Response(null, { status: 403, headers });
  return new Response(
    JSON.stringify({ ok: false, error: "evaluator_contained", function: FUNCTION_NAME, ref: CONTAINMENT_REF }),
    { status: 403, headers },
  );
}

Deno.serve((req: Request) => containedResponse(req));
