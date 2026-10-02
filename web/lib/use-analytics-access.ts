"use client";

import { useQuery } from "@tanstack/react-query";
import { useSession } from "next-auth/react";

import { workspacesApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";

/** Workspace roles that may open /analytics — the API's
 *  `ANALYTICS_ROLES` in src/api/deps.py, spelled the same way. */
const ANALYTICS_ROLES = new Set(["owner", "admin", "editor"]);

/**
 * May the signed-in person read review analytics for the active workspace?
 *
 * A global admin, or owner / admin / editor OF THE ACTIVE WORKSPACE. Members
 * and viewers may not: the page reports cost and how often the team merged
 * what the reviewer flagged, which is a lead's view.
 *
 * Same shape as `useCanManageWorkspace` (lib/use-workspace-role.ts) and the
 * same query key, so the two share one membership fetch. `undefined` while it
 * loads, so a caller can avoid flashing a refusal — or hiding a tab — at
 * somebody who turns out to be allowed. `require_analytics_access` enforces
 * the real rule; this only decides what to draw.
 */
export function useCanViewAnalytics(): boolean | undefined {
  const { data: session } = useSession();
  const token = useToken();
  const me = useQuery({
    queryKey: ["workspaces-me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });

  if (session?.isAdmin) return true;
  if (me.isLoading || !me.data) return undefined;
  const role = me.data.workspaces.find((w) => w.id === me.data.active_id)?.role;
  return role ? ANALYTICS_ROLES.has(role) : false;
}
