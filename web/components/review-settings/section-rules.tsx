"use client";

/**
 * Rules: a summary of what the rules library holds for this scope, and the
 * way into it. The library is its own page (/admin/review-rules) with its
 * own filters, bulk approval and generators; it is not copied in here.
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowRightIcon, ScrollTextIcon } from "lucide-react";

import { reviewRulesApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { buttonVariants } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/components/review-settings/context";
import { Group, SectionFrame } from "@/components/review-settings/field";

export function RulesSection() {
  const t = useT();
  const token = useToken();
  const { scope, goTo } = useSettings();
  const repo = scope.kind === "repo" ? scope.slug : null;
  const rules = useQuery({
    queryKey: ["review-rules", "settings-summary", repo],
    queryFn: () => reviewRulesApi.list(token!, repo
      ? { scope: "all", repo }
      : { scope: "workspace" }),
    enabled: !!token,
  });
  const href = repo ? `/admin/review-rules?repo=${encodeURIComponent(repo)}` : "/admin/review-rules";
  const counts = rules.data?.counts;
  const legacy = rules.data?.legacy_folder_rules.length ?? 0;
  const stats: Array<{ key: string; value: number | undefined }> = [
    { key: "active", value: counts?.active },
    { key: "pending", value: counts?.pending },
    ...(repo ? [{ key: "workspaceActive", value: rules.data?.workspace_active_count }] : []),
  ];

  return (
    <SectionFrame
      id="rules"
      title={t("reviewSettings.section.rules")}
      description={repo ? t("reviewSettings.rules.descRepo") : t("reviewSettings.rules.descGlobal")}
    >
      <Group>
        <div className="px-4 py-4">
          {rules.error ? (
            <Callout tone="danger">{t("common.loadError")}: {(rules.error as Error).message}</Callout>
          ) : (
            <dl className="grid grid-cols-2 gap-4 @md:grid-cols-3">
              {stats.map((s) => (
                <div key={s.key} className="min-w-0">
                  <dt className="text-xs text-[var(--color-muted-foreground)]">{t(`reviewSettings.rules.${s.key}`)}</dt>
                  <dd className="mt-0.5 text-2xl font-semibold tabular-nums">
                    {s.value === undefined ? <Skeleton className="h-8 w-10" /> : s.value}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Link href={href} className={buttonVariants({ variant: "default", size: "sm" })}>
              <ScrollTextIcon className="h-4 w-4" aria-hidden />
              {t("reviewSettings.rules.open")}
            </Link>
            {(counts?.pending ?? 0) > 0 && (
              <Link href={href} className={buttonVariants({ variant: "outline", size: "sm" })}>
                {t("reviewSettings.rules.reviewPending", { count: counts?.pending ?? 0 })}
                <ArrowRightIcon className="h-3.5 w-3.5" aria-hidden />
              </Link>
            )}
          </div>
          <p className="mt-3 text-xs text-[var(--color-muted-foreground)]">
            {t("reviewSettings.rules.howTheyApply")}{" "}
            <button
              type="button"
              onClick={() => goTo("filters")}
              className="font-medium text-[var(--color-brand)] underline-offset-4 hover:underline"
            >
              {t("reviewSettings.filters.applyToRules")}
            </button>
          </p>
        </div>
      </Group>
      {repo && legacy > 0 && (
        <Callout tone="warning">
          {t("reviewSettings.rules.legacyNotice", { count: legacy })}{" "}
          <button
            type="button"
            onClick={() => goTo("advanced")}
            className="font-medium underline underline-offset-4"
          >
            {t("reviewSettings.section.advanced")}
          </button>
        </Callout>
      )}
    </SectionFrame>
  );
}
