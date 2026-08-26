"use client";

import { useQuery } from "@tanstack/react-query";
import { BuildingIcon } from "lucide-react";

import { workspacesApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** Small badge with the active workspace name — for workspace-scoped pages. */
export function WorkspaceBadge({ className }: { className?: string }) {
  const token = useToken();
  const me = useQuery({
    queryKey: ["workspaces", "me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const active = me.data?.workspaces.find((w) => w.id === me.data?.active_id);
  if (!active) return null;
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1 font-normal text-xs text-[var(--color-muted-foreground)]",
        className,
      )}
    >
      <BuildingIcon className="h-3 w-3" />
      <span className="truncate">{active.name}</span>
    </Badge>
  );
}
