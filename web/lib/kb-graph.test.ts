import { afterEach, describe, expect, it, vi } from "vitest";

import { kbApi, type KbGraph } from "@/lib/api";
import { filtersActive, GRAPH_H, GRAPH_W, layout } from "@/lib/kb-graph";

const node = (path: string) => ({ path, title: path, type: "process", card: true, status: "draft" });

describe("layout", () => {
  const graph: KbGraph = {
    nodes: ["a", "b", "c", "d"].map(node),
    edges: [{ source: "a", target: "b", kind: "link" }, { source: "b", target: "c", kind: "related" }],
    dangling: {}, omitted: 0,
  };

  it("places every node inside the drawing, the same way twice", () => {
    const one = layout(graph);
    const two = layout(graph);
    expect([...one.keys()]).toEqual(["a", "b", "c", "d"]);
    for (const [k, p] of one) {
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.x).toBeLessThanOrEqual(GRAPH_W);
      expect(p.y).toBeGreaterThanOrEqual(0);
      expect(p.y).toBeLessThanOrEqual(GRAPH_H);
      expect(two.get(k)).toEqual(p);
    }
  });

  it("draws linked notes closer than unlinked ones", () => {
    const p = layout(graph);
    const d = (x: string, y: string) => Math.hypot(p.get(x)!.x - p.get(y)!.x, p.get(x)!.y - p.get(y)!.y);
    expect(d("a", "b")).toBeLessThan(d("a", "d"));
  });

  it("survives an empty graph and an edge to a note it does not have", () => {
    expect(layout({ nodes: [], edges: [], dangling: {}, omitted: 0 }).size).toBe(0);
    const p = layout({ nodes: [node("a")], edges: [{ source: "a", target: "zz", kind: "link" }],
      dangling: {}, omitted: 0 });
    expect(Number.isFinite(p.get("a")!.x)).toBe(true);
  });
});

describe("filtersActive", () => {
  it("is false for empty values and true for any real filter", () => {
    expect(filtersActive({})).toBe(false);
    expect(filtersActive({ type: "", status: undefined, cards_only: false })).toBe(false);
    expect(filtersActive({ object: "Документ.Заказ" })).toBe(true);
    expect(filtersActive({ cards_only: true })).toBe(true);
  });
});

describe("kbApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends only the filters that are set, and the upload as multipart", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url: String(url), init });
      return new Response(JSON.stringify({ repo: "r", binding: {}, notes: [], types: [] }),
        { status: 200, headers: { "content-type": "application/json" } });
    });
    await kbApi.list("t", "my repo", { status: "verified", type: "", object: "Документ.Заказ" });
    const u = new URL(calls[0].url, "http://x");
    expect(u.pathname.endsWith("/api/kb/my%20repo")).toBe(true);
    expect(Object.fromEntries(u.searchParams)).toEqual({ status: "verified", object: "Документ.Заказ" });

    await kbApi.upload("t", "r", [new File(["# a"], "a.md")], { code_repo: "erp", replace: false });
    const body = calls[1].init.body as FormData;
    expect(calls[1].init.method).toBe("POST");
    expect(body.getAll("files")).toHaveLength(1);
    expect(body.get("code_repo")).toBe("erp");
    expect(body.get("replace")).toBe("false");
  });
});
