"use client";

/**
 * Search the packs, and see the brief an agent would get for a task.
 *
 * Both run over exactly the packs this person may see, the same ones an agent
 * connected with their token sees. The brief is not a preview of something
 * else: it is the text the `brief` tool returns, so a pack that reads badly
 * here reads badly to the agent, and this is where to find out.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ClipboardListIcon, PlugIcon, SearchIcon, SparklesIcon } from "lucide-react";

import { codebaseApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { CodeBlock } from "@/components/codebase/copy";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SkeletonRows } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

/** `MAX_TASK` in the brief route: the longest task the API accepts. */
const TASK_MAX = 8000;
const QUERY_MAX = 300;
const MIN_QUERY = 2;

export default function CodebaseSearchPage() {
  const t = useT();
  const token = useToken();
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [task, setTask] = useState("");

  // `?q=` from the catalogue's search box pre-fills and runs the search; read
  // from the location (no Suspense boundary) and applied a tick later.
  useEffect(() => {
    const first = new URLSearchParams(window.location.search).get("q");
    if (!first?.trim()) return;
    const timer = setTimeout(() => {
      setInput(first);
      setQuery(first.trim());
    }, 0);
    return () => clearTimeout(timer);
  }, []);

  const hits = useQuery({
    queryKey: ["codebase", "search", query],
    queryFn: () => codebaseApi.search(token!, query),
    enabled: !!token && query.length >= MIN_QUERY,
  });

  const brief = useMutation({ mutationFn: () => codebaseApi.brief(token!, task.trim()) });

  return (
    <PageShell width="wide">
      <PageHeader
        tour="page-codebase-search"
        icon={<SearchIcon className="h-6 w-6" />}
        title={t("codebase.search.title")}
        badge={<WorkspaceBadge />}
        description={t("codebase.search.subtitle")}
        tabs={<SectionTabs set="codebase" />}
      />

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="self-start">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <SearchIcon aria-hidden className="size-4" /> {t("codebase.search.packsTitle")}
            </CardTitle>
            <CardDescription>{t("codebase.search.packsDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <form
              role="search"
              className="flex gap-2"
              onSubmit={(e) => { e.preventDefault(); setQuery(input.trim()); }}
            >
              <Input
                value={input} maxLength={QUERY_MAX} autoFocus
                onChange={(e) => setInput(e.target.value)}
                placeholder={t("codebase.search.placeholder")}
                aria-label={t("codebase.search.placeholder")}
              />
              <Button type="submit" disabled={input.trim().length < MIN_QUERY}>
                <SearchIcon />{t("codebase.searchButton")}
              </Button>
            </form>

            {query.length < MIN_QUERY && (
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.search.hint")}</p>
            )}
            {hits.isPending && query.length >= MIN_QUERY && !hits.isError && <SkeletonRows rows={3} />}
            {hits.isError && <Callout tone="danger">{(hits.error as Error).message}</Callout>}
            {hits.data?.truncated && (
              <Callout tone="warning">{t("codebase.search.truncated")}</Callout>
            )}
            {hits.data && hits.data.hits.length === 0 && (
              <EmptyState
                icon={SearchIcon}
                title={t("codebase.search.noneTitle")}
                description={t("codebase.search.noneDesc", { query })}
                className="py-8"
              />
            )}
            {hits.data && hits.data.hits.length > 0 && (
              <>
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("codebase.search.count", { count: hits.data.hits.length })}
                </p>
                <ul className="divide-y divide-[var(--color-border)] rounded-lg border border-[var(--color-border)]">
                  {hits.data.hits.map((hit) => (
                    <li key={hit.item_id}>
                      <Link
                        href={`/codebase/packs/${hit.pack_id}?item=${encodeURIComponent(hit.item_id)}`}
                        className="block space-y-1 px-3 py-2.5 transition-colors hover:bg-[var(--color-accent)]/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)]"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium">{hit.name}</span>
                          <Badge variant={hit.kind === "skill" ? "brand" : "outline"}>
                            {t(`codebase.kind.${hit.kind}`)}
                          </Badge>
                          <span className="text-xs text-[var(--color-muted-foreground)]">{hit.pack_title}</span>
                        </div>
                        <div className="break-all font-mono text-[11px] text-[var(--color-muted-foreground)]">
                          {hit.pack_slug}/{hit.path}
                        </div>
                        {hit.snippet && (
                          <p className="line-clamp-3 whitespace-pre-line text-xs leading-relaxed">{hit.snippet}</p>
                        )}
                      </Link>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </CardContent>
        </Card>

        <Card className="self-start" data-tour="codebase-brief">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <ClipboardListIcon aria-hidden className="size-4" /> {t("codebase.brief.title")}
            </CardTitle>
            <CardDescription>{t("codebase.brief.desc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <form
              className="space-y-3"
              onSubmit={(e) => { e.preventDefault(); if (task.trim() && token) brief.mutate(); }}
            >
              <div className="space-y-1.5">
                <Label htmlFor="codebase-task">{t("codebase.brief.taskLabel")}</Label>
                <Textarea
                  id="codebase-task" rows={6} maxLength={TASK_MAX} value={task}
                  onChange={(e) => setTask(e.target.value)}
                  placeholder={t("codebase.brief.placeholder")}
                />
                <div className="text-right text-[11px] text-[var(--color-muted-foreground)]">
                  {task.length} / {TASK_MAX}
                </div>
              </div>
              <Button type="submit" loading={brief.isPending} disabled={!task.trim() || !token}>
                <SparklesIcon />{t("codebase.brief.build")}
              </Button>
            </form>

            {brief.isError && <Callout tone="danger">{(brief.error as Error).message}</Callout>}
            {brief.data && (
              <div className="space-y-3">
                {brief.data.truncated && <Callout tone="warning">{t("codebase.search.truncated")}</Callout>}
                <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.brief.resultNote")}</p>
                <CodeBlock text={brief.data.text} wrap label={t("codebase.brief.copy")} />
                {brief.data.items.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-xs font-medium">{t("codebase.brief.sources")}</div>
                    <ul className="space-y-0.5 text-xs">
                      {brief.data.items.map((item) => (
                        <li key={item.item_id} className="break-all">
                          <code>{item.pack_slug}/{item.path}</code>{" "}
                          <span className="text-[var(--color-muted-foreground)]">
                            · {t(`codebase.kind.${item.kind}`)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
            <p className="flex items-start gap-1.5 text-xs text-[var(--color-muted-foreground)]">
              <PlugIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
              <span>
                {t("codebase.brief.connectHint")}{" "}
                <Link href="/codebase/connect" className="underline">{t("codebase.brief.connectLink")}</Link>
              </span>
            </p>
          </CardContent>
        </Card>
      </div>
    </PageShell>
  );
}
