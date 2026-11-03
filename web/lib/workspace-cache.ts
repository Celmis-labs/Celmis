import type { QueryClient } from "@tanstack/react-query";

/** Every cached list that names workspaces. Creating or deleting one must refresh all
 *  of them: the admin pickers (users, access requests, MCP tokens) keep their list for
 *  30 seconds, and used to keep offering a workspace that was just deleted. */
export const WORKSPACE_LIST_KEYS: ReadonlyArray<readonly string[]> = [
  ["workspaces"],          // ["workspaces", "me"], per-workspace members, onboarding
  ["workspaces-me"],
  ["admin-workspaces"],    // users and access-requests pages
  ["mcp-token-workspaces"],
  ["mcp-token-repos"],
  ["mcp-shares"],
  ["mcp-tokens"],
];

export function invalidateWorkspaceLists(qc: Pick<QueryClient, "invalidateQueries">): void {
  for (const queryKey of WORKSPACE_LIST_KEYS) void qc.invalidateQueries({ queryKey: [...queryKey] });
}
