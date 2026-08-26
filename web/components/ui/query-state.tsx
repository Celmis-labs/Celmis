"use client";

import type { UseQueryResult } from "@tanstack/react-query";
import type { LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/lib/i18n";

/**
 * Standard loading / error / empty / data switch for a useQuery result.
 *
 * - loading → skeleton rows (3 by default)
 * - error   → danger Callout with the message and a Retry button
 * - data is an empty array AND `empty` given → EmptyState
 * - otherwise → children(data)
 */
export function QueryState<T>({
  query,
  empty,
  skeleton = 3,
  children,
}: {
  query: UseQueryResult<T>;
  empty?: { icon?: LucideIcon; title: string; description?: string; action?: React.ReactNode };
  skeleton?: number;
  children: (data: T) => React.ReactNode;
}) {
  const t = useT();

  if (query.isError) {
    return (
      <Callout tone="danger">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>
            {t("common.loadError")}
            {query.error instanceof Error && query.error.message ? `: ${query.error.message}` : ""}
          </span>
          <Button size="sm" variant="outline" onClick={() => query.refetch()}>
            {t("common.retry")}
          </Button>
        </div>
      </Callout>
    );
  }

  // isPending also covers token-gated queries (`enabled: !!token`) that have
  // not started yet — skeletons are the right thing to show there too.
  if (query.isPending || query.data === undefined) {
    return (
      <div className="space-y-2">
        {Array.from({ length: skeleton }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  const data = query.data;
  if (empty && Array.isArray(data) && data.length === 0) {
    return (
      <EmptyState
        icon={empty.icon}
        title={empty.title}
        description={empty.description}
        action={empty.action}
      />
    );
  }

  return <>{children(data)}</>;
}
