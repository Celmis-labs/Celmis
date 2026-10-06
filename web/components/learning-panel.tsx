"use client";

/**
 * The Learning view of /memories — what the reviews learned from the team.
 *
 * Reads `/api/learning/*`: the counts of what people said and what was fixed
 * afterwards, the findings dismissed in the most different reviews, and the
 * recorded signals, any of which an editor can forget. The people behind the
 * signals are shown to workspace admins only (the server leaves the field
 * out for everybody else).
 */

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { BrainIcon, HistoryIcon, Trash2Icon } from "lucide-react";

import {
  learningApi,
  type LearningSignal,
  type LearningSignalKind,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { relativeTime } from "@/lib/review-stages";
import { useToken } from "@/lib/use-token";
import { Button, buttonVariants } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { SkeletonRows } from "@/components/ui/skeleton";

const KINDS: LearningSignalKind[] = ["dismissed", "accepted", "implemented", "ignored", "resolved"];
type Filter = "all" | LearningSignalKind;
const PAGE = 25;

export function LearningPanel({ repo, canEdit }: { repo: string | null; canEdit: boolean }) {
  const { t, locale } = useI18n();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [filter, setFilter] = useState<Filter>("all");
  const [limit, setLimit] = useState(PAGE);

  const summary = useQuery({
    queryKey: ["learning-summary", repo ?? ""],
    queryFn: () => learningApi.summary(token!, repo),
    enabled: !!token,
  });
  const list = useQuery({
    queryKey: ["learning-signals", repo ?? "", filter, limit],
    queryFn: () => learningApi.signals(token!, {
      repo, signal: filter === "all" ? null : filter, limit,
    }),
    enabled: !!token,
  });

  const forget = useMutation({
    mutationFn: (id: string) => learningApi.forget(token!, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["learning-summary"] });
      qc.invalidateQueries({ queryKey: ["learning-signals"] });
      toast.success(t("learning.forgotten"));
    },
    onError: (e) => toast.error(t("learning.loadFailed", { message: (e as Error).message })),
  });

  const askForget = async (s: LearningSignal) => {
    const ok = await confirm({
      title: t("learning.forgetConfirm"),
      description: t("learning.forgetBody"),
      confirmLabel: t("learning.forget"),
      danger: true,
    });
    if (ok) forget.mutate(s.id);
  };

  const data = summary.data;
  const mode = data?.mode ?? "shadow";
  const rate = data?.implementation;
  const settingsHref = repo
    ? `/review-settings?repo=${encodeURIComponent(repo)}&section=learning`
    : "/review-settings?section=learning";

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <BrainIcon aria-hidden className="h-4 w-4" /> {t("memories.view.learning")}
          </CardTitle>
          <CardDescription>{t("learning.desc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {summary.isLoading && <SkeletonRows rows={2} />}
          {summary.error && (
            <Callout tone="danger">
              {t("learning.loadFailed", { message: (summary.error as Error).message })}
            </Callout>
          )}
          {data && (
            <>
              <Callout tone={mode === "on" ? "success" : "info"}>
                {t(`learning.mode.${mode === "on" || mode === "off" ? mode : "shadow"}`)}{" "}
                <Link href={settingsHref} className="font-medium underline underline-offset-4">
                  {t("learning.mode.change")}
                </Link>
              </Callout>
              <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                {KINDS.map((k) => (
                  <div
                    key={k}
                    className="rounded-lg border border-[var(--color-border)] px-3 py-2"
                  >
                    <dt className="text-xs text-[var(--color-muted-foreground)]">
                      {t(`learning.signal.${k}`)}
                    </dt>
                    <dd className="text-2xl font-semibold tabular-nums">
                      {data.signals[k] ?? 0}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("learning.windowNote", { days: data.window_days })}
                {" · "}
                {rate && rate.rate !== null
                  ? t("learning.rate", {
                    rate: Math.round(rate.rate * 100),
                    implemented: rate.implemented,
                    total: rate.total,
                  })
                  : t("learning.rateNone")}
              </p>
            </>
          )}
        </CardContent>
      </Card>

      {data && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <CardTitle className="text-base">{t("learning.topTitle")}</CardTitle>
                <CardDescription>{t("learning.topDesc")}</CardDescription>
              </div>
              {canEdit && repo && (
                <Link
                  href={`/admin/review-rules?repo=${encodeURIComponent(repo)}`}
                  className={buttonVariants({ variant: "outline", size: "sm" })}
                >
                  <HistoryIcon /> {t("learning.rulesLink")}
                </Link>
              )}
            </div>
          </CardHeader>
          <CardContent>
            {data.top_dismissed.length === 0 ? (
              <p className="text-sm text-[var(--color-muted-foreground)]">{t("learning.topEmpty")}</p>
            ) : (
              <ul className="divide-y divide-[var(--color-border)]">
                {data.top_dismissed.map((g) => (
                  <li key={g.fingerprint} className="flex items-baseline justify-between gap-3 py-2 text-sm">
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{g.title}</span>
                      <span className="block truncate font-mono text-xs text-[var(--color-muted-foreground)]">
                        {g.file_path}{!repo && g.repo_slug ? ` · ${g.repo_slug}` : ""}
                      </span>
                    </span>
                    <span className="shrink-0 tabular-nums text-xs text-[var(--color-muted-foreground)]">
                      {t("learning.topCount", { count: g.dismissals })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle className="text-base">{t("learning.signalsTitle")}</CardTitle>
              <CardDescription>{t("learning.signalsDesc")}</CardDescription>
            </div>
            <SegmentedControl
              semantics="tabs"
              size="sm"
              label={t("learning.signalsTitle")}
              value={filter}
              onValueChange={(f) => { setFilter(f); setLimit(PAGE); }}
              segments={[
                { value: "all" as Filter, label: t("learning.filter.all") },
                ...KINDS.map((k) => ({ value: k as Filter, label: t(`learning.signal.${k}`) })),
              ]}
            />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {list.isLoading && <SkeletonRows rows={4} />}
          {list.error && (
            <Callout tone="danger">
              {t("learning.loadFailed", { message: (list.error as Error).message })}
            </Callout>
          )}
          {list.data && list.data.signals.length === 0 && (
            <EmptyState
              icon={BrainIcon}
              title={t("learning.empty")}
              description={t("learning.emptyDesc")}
            />
          )}
          {list.data?.signals.map((s) => (
            <div
              key={s.id}
              className="flex items-start justify-between gap-3 rounded-lg border border-[var(--color-border)] px-3 py-2 text-sm"
            >
              <div className="min-w-0 space-y-0.5">
                <p className="truncate font-medium">{s.title}</p>
                <p className="truncate font-mono text-xs text-[var(--color-muted-foreground)]">
                  {s.file_path}
                </p>
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t(`learning.signal.${s.signal}`)}
                  {" · "}{t(`learning.source.${s.source}`)}
                  {s.pr_number
                    ? ` · ${t("learning.pr", { repo: s.pr_repo, number: s.pr_number })}`
                    : ""}
                  {s.actor ? ` · ${s.actor}` : ""}
                  {" · "}{relativeTime(s.created_at, locale)}
                </p>
              </div>
              {canEdit && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => askForget(s)}
                  disabled={forget.isPending}
                  aria-label={t("learning.forget")}
                  className="shrink-0 text-[var(--color-destructive)] hover:bg-[var(--color-destructive-soft)] hover:text-[var(--color-destructive)]"
                >
                  <Trash2Icon />
                </Button>
              )}
            </div>
          ))}
          {list.data && list.data.total > list.data.signals.length && (
            <Button variant="outline" size="sm" className="self-center" onClick={() => setLimit(limit + PAGE)}>
              {t("learning.more")}
            </Button>
          )}
        </CardContent>
      </Card>
      {dialog}
    </>
  );
}
