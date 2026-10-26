import { afterEach, describe, expect, it, vi } from "vitest";

import * as bare from "@/app/.well-known/route";
import { GET, HEAD, OPTIONS } from "@/app/.well-known/[...path]/route";

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });

function req(path: string, headers: Record<string, string> = {}) {
  const url = new URL(`http://web:3000${path}`);
  // The route handler reads `headers` and `nextUrl.protocol` only.
  return { headers: new Headers(headers), nextUrl: url } as never;
}

const stubUpstream = (res: Response | Error) => {
  const fetchMock = vi.fn(async () => {
    if (res instanceof Error) throw res;
    return res;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
};

afterEach(() => vi.unstubAllGlobals());

describe("/.well-known on the web host", () => {
  it("forwards an OAuth document to the API with the address the caller used, and relays its JSON", async () => {
    const doc = { resource: "https://celmis.example.com/backend/mcp/dev" };
    const fetchMock = stubUpstream(new Response(JSON.stringify(doc), {
      status: 200, headers: { "content-type": "application/json" },
    }));
    const res = await GET(
      req("/.well-known/oauth-protected-resource/backend/mcp/dev", {
        host: "web:3000", "x-forwarded-host": "celmis.example.com", "x-forwarded-proto": "https",
        "x-forwarded-for": "203.0.113.7",
      }),
      ctx("oauth-protected-resource", "backend", "mcp", "dev"),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/json");
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.json()).toEqual(doc);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url.endsWith("/.well-known/oauth-protected-resource/mcp/dev")).toBe(true);
    const sent = init.headers as Record<string, string>;
    expect(sent["x-forwarded-host"]).toBe("celmis.example.com");
    expect(sent["x-forwarded-proto"]).toBe("https");
    expect(sent["x-forwarded-prefix"]).toBe("/backend");
  });

  it("answers anything else with a JSON 404 and never reaches the API", async () => {
    const fetchMock = stubUpstream(new Response("{}", { headers: { "content-type": "application/json" } }));
    const res = await GET(req("/.well-known/security.txt"), ctx("security.txt"));
    expect(res.status).toBe(404);
    expect(res.headers.get("content-type")).toBe("application/json");
    expect(res.headers.get("location")).toBeNull();
    const body = await res.json();
    expect(body.error).toBe("not_found");
    expect(typeof body.error_description).toBe("string");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never passes an HTML answer on: a client would try to parse it as JSON", async () => {
    stubUpstream(new Response("<!doctype html><title>Sign in</title>", {
      status: 200, headers: { "content-type": "text/html" },
    }));
    const res = await GET(req("/.well-known/oauth-authorization-server"), ctx("oauth-authorization-server"));
    expect(res.headers.get("content-type")).toBe("application/json");
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(JSON.stringify(await res.json())).not.toContain("<");
  });

  it("answers 502 in JSON when the API does not answer", async () => {
    stubUpstream(new Error("connect ECONNREFUSED"));
    const res = await GET(req("/.well-known/oauth-authorization-server"), ctx("oauth-authorization-server"));
    expect(res.status).toBe(502);
    expect(res.headers.get("content-type")).toBe("application/json");
    expect((await res.json()).error).toBe("temporarily_unavailable");
  });

  it("answers HEAD without a body and OPTIONS as a CORS preflight", async () => {
    stubUpstream(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    const head = await HEAD(req("/.well-known/oauth-authorization-server"), ctx("oauth-authorization-server"));
    expect(head.status).toBe(200);
    expect(await head.text()).toBe("");
    const options = OPTIONS();
    expect(options.status).toBe(204);
    expect(options.headers.get("access-control-allow-origin")).toBe("*");
  });
});

describe("/.well-known with nothing after it", () => {
  it("answers a JSON 404, not Next's HTML page, and never reaches the API", async () => {
    const fetchMock = stubUpstream(new Response("{}", { headers: { "content-type": "application/json" } }));
    const res = await bare.GET(req("/.well-known"));
    expect(res.status).toBe(404);
    expect(res.headers.get("content-type")).toBe("application/json");
    expect(res.headers.get("location")).toBeNull();
    expect((await res.json()).error).toBe("not_found");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("answers HEAD without a body and OPTIONS as a CORS preflight", async () => {
    const head = await bare.HEAD(req("/.well-known"));
    expect(head.status).toBe(404);
    expect(head.headers.get("content-type")).toBe("application/json");
    expect(await head.text()).toBe("");
    expect(bare.OPTIONS().status).toBe(204);
  });
});
