"use client";

import { useQuery } from "@tanstack/react-query";
import { useSession } from "next-auth/react";

import { workspacesApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";

/**
 * May the signed-in person change this workspace's settings?
 *
 * Owner or admin OF THE ACTIVE WORKSPACE — or a global admin, who can reach
 * every workspace by construction. Everyone owns their personal workspace, so
 * in the common case this is true for the person's own things and false for
 * somebody else's.
 *
 * It exists because the same three lines had been written inline on the LLM
 * settings page and NOT on the pages beside it, which is how the budget cap
 * and the reindex button ended up gated on global admin: a workspace owner
 * could choose the models but not cap what they cost.
 *
 * `undefined` while the membership is still loading, so a caller can avoid
 * flashing a refusal at somebody who turns out to be allowed. The API
 * enforces the real rule either way; this only decides what to draw.
 */
export function useCanManageWorkspace(): boolean | undefined {
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
  return role === "owner" || role === "admin";
}
