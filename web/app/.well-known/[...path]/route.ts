import { type NextRequest } from "next/server";

import { API_BASE } from "@/lib/api";
import { API_PREFIX, resolveWellKnown } from "@/lib/well-known";

/**
 * `/.well-known/*` on the web host: the API's OAuth discovery documents, or a
 * JSON 404. See lib/well-known.ts for why this host answers at all.
 *
 * A runtime handler rather than a `rewrites()` entry on purpose: a rewrite is
 * resolved when the image is BUILT, and one published image has to find the
 * API of whatever installation it runs in (`API_BASE_INTERNAL`).
 *
 * The API prints its own public address into these documents, and behind the
 * proxy it cannot see it. So the address the caller used (host, scheme) and
 * the prefix go along as the headers a proxy would send; the API checks the
 * host against the ones it serves and decides.
 */
export const dynamic = "force-dynamic";

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, OPTIONS",
  "access-control-allow-headers": "content-type, mcp-protocol-version",
};

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...CORS, ...extra },
  });
}

async function answer(req: NextRequest, path: string[], head: boolean): Promise<Response> {
  const target = resolveWellKnown(path);
  if (!target.ok) {
    return head
      ? new Response(null, { status: 404, headers: { "content-type": "application/json", "cache-control": "no-store", ...CORS } })
      : json(404, { error: target.error, error_description: target.description });
  }

  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host") ?? "";
  const proto = req.headers.get("x-forwarded-proto") ?? req.nextUrl.protocol.replace(":", "");
  const headers: Record<string, string> = {
    accept: "application/json",
    "x-forwarded-host": host,
    "x-forwarded-proto": proto,
    "x-forwarded-prefix": API_PREFIX,
  };
  const forwardedFor = req.headers.get("x-forwarded-for");
  if (forwardedFor) headers["x-forwarded-for"] = forwardedFor;

  let upstream: Response;
  try {
    upstream = await fetch(`${API_BASE.replace(/\/+$/, "")}${target.upstreamPath}`, {
      headers, cache: "no-store", signal: AbortSignal.timeout(8000),
    });
  } catch {
    return json(502, {
      error: "temporarily_unavailable",
      error_description: "The Celmis API did not answer. Try again in a moment.",
    });
  }
  const type = upstream.headers.get("content-type") ?? "";
  if (!type.includes("json")) {
    // Never pass HTML (or anything else a JSON parser would choke on) through.
    return json(upstream.ok ? 502 : upstream.status, {
      error: upstream.status === 404 ? "not_found" : "temporarily_unavailable",
      error_description: upstream.status === 404 ? "Not found." : "The Celmis API gave an answer that is not a metadata document.",
    });
  }
  const body = await upstream.text();
  return new Response(head ? null : body, {
    status: upstream.status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...CORS },
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, { params }: Ctx): Promise<Response> {
  return answer(req, (await params).path, false);
}

export async function HEAD(req: NextRequest, { params }: Ctx): Promise<Response> {
  return answer(req, (await params).path, true);
}

export function OPTIONS(): Response {
  return new Response(null, { status: 204, headers: { ...CORS, "access-control-max-age": "86400" } });
}
