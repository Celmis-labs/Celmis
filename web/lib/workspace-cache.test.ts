import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { invalidateWorkspaceLists } from "@/lib/workspace-cache";

describe("invalidateWorkspaceLists", () => {
  it("marks every workspace list stale, and nothing else", () => {
    const qc = new QueryClient();
    const keys = [
      ["workspaces", "me"], ["workspaces-me"], ["admin-workspaces"],
      ["mcp-token-workspaces"], ["mcp-shares", "w1"], ["repos", "list"],
    ];
    for (const k of keys) qc.setQueryData(k, [1]);
    invalidateWorkspaceLists(qc);
    const stale = (k: string[]) => qc.getQueryState(k)?.isInvalidated;
    for (const k of keys.slice(0, 5)) expect(stale(k), k.join("/")).toBe(true);
    expect(stale(["repos", "list"])).toBe(false);
  });
});
