"use client";

import { useQuery } from "@tanstack/react-query";

import { unruledApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Callout } from "@/components/ui/callout";

/**
 * "N repos are visible to admins only."
 *
 * A repository with no access rule and no team grant is seen by the owner, the
 * admins and the superadmin and nobody else. This says how many there are so
 * nobody finds out from a member who cannot see their code. Shown to workspace
 * admins only: the endpoint refuses everyone else, and a refusal renders
 * nothing.
 */
export function UnruledBanner() {
  const t = useT();
  const token = useToken();
  const q = useQuery({
    queryKey: ["access", "unruled"],
    queryFn: () => unruledApi.list(token!),
    enabled: !!token,
    retry: false,
  });
  if (!q.data || q.data.count === 0) return null;
  return (
    <Callout tone="warning">
      <div>{t("admin.unruled.banner", { count: String(q.data.count) })}</div>
      <div className="mt-1 text-xs opacity-80">
        {q.data.repos.slice(0, 6).join(", ")}{q.data.count > 6 ? " …" : ""}
      </div>
    </Callout>
  );
}
