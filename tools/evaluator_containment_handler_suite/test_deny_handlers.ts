// CDX-GROK-CONTAINMENT-PREP-1 — local Deno tests of the five proposed deny handlers.
// Handlers call Deno.serve at module scope (required by the Edge runtime), so this suite does
// NOT import them (that would bind port 8000). It loads each source, strips the Deno.serve
// line, and evaluates the exported containedResponse in an isolated module via data: URL.
// No network, no secrets, no production. Run:
//   deno test -A tools/evaluator_containment_handler_suite/test_deny_handlers.ts
import { assertEquals, assert } from "https://deno.land/std@0.224.0/assert/mod.ts";

const ROOT = new URL("../../supabase/functions/", import.meta.url);
const SLUGS = [
  "evaluator-jobs-create",
  "evaluator-jobs-run",
  "evaluator-jobs-status",
  "evaluator-jobs-proposals",
  "evaluator-proposals-review",
] as const;
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] as const;

async function loadContainedResponse(slug: string): Promise<(req: Request) => Response> {
  const src = await Deno.readTextFile(new URL(`${slug}/index.ts`, ROOT));
  assert(src.includes("Deno.serve((req: Request) => containedResponse(req));"), `${slug} must call Deno.serve`);
  const stripped = src.replace(/Deno\.serve\(\(req: Request\) => containedResponse\(req\)\);\s*$/m, "");
  const mod = await import(`data:application/typescript,${encodeURIComponent(stripped)}`);
  assert(typeof mod.containedResponse === "function", slug);
  return mod.containedResponse;
}

for (const slug of SLUGS) {
  Deno.test(`deny handler ${slug} rejects every reachable method with 403 and no CORS grant`, async () => {
    const containedResponse = await loadContainedResponse(slug);
    for (const method of METHODS) {
      const req = new Request(`http://local.test/${slug}?job_id=x`, {
        method,
        headers: {
          "authorization": "Bearer anything",
          "apikey": "anything",
          "content-type": "application/json",
          "origin": "https://evil.example",
          "access-control-request-method": "POST",
        },
        body: method === "GET" || method === "HEAD" ? undefined : JSON.stringify({ job_id: "x", status: "approved" }),
      });
      const resp = containedResponse(req);
      assertEquals(resp.status, 403, `${slug} ${method}`);
      assertEquals(resp.headers.get("access-control-allow-origin"), null);
      assertEquals(resp.headers.get("access-control-allow-methods"), null);
      assertEquals(resp.headers.get("access-control-allow-headers"), null);
      assertEquals(resp.headers.get("cache-control"), "no-store");
      if (method === "HEAD") {
        assertEquals(await resp.text(), "");
      } else {
        const body = await resp.json();
        assertEquals(body.ok, false);
        assertEquals(body.error, "evaluator_contained");
        assertEquals(body.function, slug);
        assertEquals(body.ref, "CDX-GROK-CONTAINMENT-PREP-1");
      }
    }
  });
}

Deno.test("deny handlers do not import shared client / cors / contracts modules", async () => {
  for (const slug of SLUGS) {
    const src = await Deno.readTextFile(new URL(`${slug}/index.ts`, ROOT));
    assert(!src.includes("_shared/client"), slug);
    assert(!src.includes("_shared/cors"), slug);
    assert(!src.includes("_shared/contracts"), slug);
    assert(!src.includes("SUPABASE_SERVICE_ROLE_KEY"), slug);
    assert(!src.includes("createClient"), slug);
    assert(!src.includes("Deno.env"), slug);
    assert(src.includes("Deno.serve((req: Request) => containedResponse(req));"), slug);
  }
});
