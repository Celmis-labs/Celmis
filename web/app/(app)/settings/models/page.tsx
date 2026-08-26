"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CpuIcon, ExternalLinkIcon, RefreshCwIcon, KeyIcon } from "lucide-react";

import { modelsApi, type ModelInfo } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function ModelsPage() {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [showAll, setShowAll] = useState(false);

  const catalog = useQuery({
    queryKey: ["models", "available", showAll ? "all" : "connected"],
    queryFn: () =>
      modelsApi.available(token!, { allProviders: showAll }),
    enabled: !!token,
  });

  const refresh = useMutation({
    mutationFn: () => modelsApi.refreshPricing(token!),
    onSuccess: (r) => {
      toast.success(t("settings.models.overlayRefreshed", { count: r.overlay_entries }));
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e) =>
      toast.error(t("settings.models.refreshFailed", { error: (e as Error).message })),
  });

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    const all = catalog.data?.models ?? [];
    if (!q) return all;
    return all.filter((m) =>
      m.id.toLowerCase().includes(q) ||
      m.provider.toLowerCase().includes(q) ||
      (m.recommended_for ?? "").toLowerCase().includes(q),
    );
  }, [catalog.data, search]);

  const byProvider = useMemo(() => {
    const groups: Record<string, ModelInfo[]> = {};
    for (const m of filtered) {
      (groups[m.provider] ??= []).push(m);
    }
    return groups;
  }, [filtered]);

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<CpuIcon className="h-6 w-6" />}
        title={t("settings.models.title")}
        description={t("settings.models.description")}
        tabs={<SectionTabs set="settings" />}
      />

      <Card>
        <CardHeader>
          <CardTitle>{t("settings.models.connectedProvidersTitle")}</CardTitle>
          <CardDescription>
            {t("settings.models.connectedProvidersDescBefore")}{" "}
            <Link href="/settings/llm" className="underline">{t("settings.models.connectionsLink")}</Link>{" "}
            {t("settings.models.connectedProvidersDescAfter")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {catalog.isLoading && <p className="text-sm">{t("settings.models.loading")}</p>}
          {catalog.data && (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2">
                {(["openai", "anthropic", "google", "openrouter", "groq"] as const).map((p) => {
                  const connected = catalog.data!.connected_providers.includes(p) ||
                    (p === "google" && catalog.data!.connected_providers.includes("gemini"));
                  return (
                    <Badge
                      key={p}
                      variant={connected ? "success" : "outline"}
                      className="text-xs"
                    >
                      {connected ? "✓" : "○"} {p}
                    </Badge>
                  );
                })}
              </div>
              <div className="flex gap-2 items-center">
                <Link href="/settings/llm">
                  <Button variant="outline" size="sm">
                    <KeyIcon className="h-3.5 w-3.5 mr-1" /> {t("settings.models.manageKeys")}
                  </Button>
                </Link>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => refresh.mutate()}
                  disabled={refresh.isPending}
                >
                  <RefreshCwIcon className="h-3.5 w-3.5 mr-1" />
                  {refresh.isPending ? t("settings.models.refreshing") : t("settings.models.refreshPricing")}
                </Button>
                {catalog.data.pricing_last_refreshed && (
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    {t("settings.models.lastRefreshed", { date: new Date(catalog.data.pricing_last_refreshed).toLocaleString() })}
                  </span>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>{t("settings.models.catalogTitle")}</CardTitle>
              <CardDescription>
                {catalog.data
                  ? t("settings.models.catalogSummary", { models: filtered.length, providers: Object.keys(byProvider).length })
                  : t("settings.models.loadingCatalog")}
              </CardDescription>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={showAll}
                onChange={(e) => setShowAll(e.target.checked)}
              />
              {t("settings.models.showAllProviders")}
            </label>
          </div>
          <Input
            placeholder={t("settings.models.searchPlaceholder")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </CardHeader>
        <CardContent>
          {/* The catalog runs to hundreds of models — keep it in its own
              scroller so the page stays a page. Search + "show all" live in
              the header above, so they never scroll out of reach. */}
          {filtered.length > 0 && (
            <div className="max-h-[60vh] overflow-y-auto rounded-md border border-[var(--color-border)] p-2">
              {Object.entries(byProvider).map(([provider, models]) => (
                <div key={provider} className="mb-6 last:mb-0">
                  <div className="sticky top-0 z-10 -mx-2 flex items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-card)] px-2 py-1.5">
                    <h3 className="font-semibold uppercase text-xs tracking-wide">
                      {provider}
                    </h3>
                    <Badge variant="outline" className="text-[10px]">
                      {models.length}
                    </Badge>
                  </div>
                  <div className="grid gap-1.5 pt-2">
                    {models.map((m) => (
                      <div
                        key={m.id}
                        className={`flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-[var(--color-border)] px-3 py-1.5 text-sm ${
                          !m.available ? "opacity-60" : ""
                        }`}
                      >
                        <code className="min-w-0 flex-1 truncate font-mono text-xs">
                          {m.id}
                        </code>
                        <span className="text-xs text-[var(--color-muted-foreground)] whitespace-nowrap">
                          {t("settings.models.pricePerM", { input: m.input_per_m.toFixed(2), output: m.output_per_m.toFixed(2) })}
                        </span>
                        {m.max_context && (
                          <span className="text-xs text-[var(--color-muted-foreground)] whitespace-nowrap">
                            {t("settings.models.contextK", { k: (m.max_context / 1000).toFixed(0) })}
                          </span>
                        )}
                        {m.recommended_for && (
                          <Badge variant="outline" className="text-[9px]">
                            {m.recommended_for}
                          </Badge>
                        )}
                        {!m.available && (
                          <Badge variant="outline" className="text-[9px]">{t("settings.models.noKey")}</Badge>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          {catalog.data && filtered.length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">
              {t("settings.models.noModelsMatch", { query: search })}
            </p>
          )}
        </CardContent>
      </Card>
    </PageShell>
  );
}
