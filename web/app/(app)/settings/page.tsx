"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { signOut, useSession } from "next-auth/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ActivityIcon, TrashIcon, ZapIcon } from "lucide-react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { authApi, spendApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { PushCard } from "@/components/push-card";

/** Two decimals turn $0.0012 into "$0.00", which reads as free. */
function formatCost(usd: number): string {
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

const RANGE_OPTIONS = [
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
];

export default function SettingsPage() {
  const t = useT();
  const { data } = useSession();
  const token = useToken();
  const [days, setDays] = useState(30);

  // Той самий API, що й Адміністрування → Витрати — числа збігаються.
  const usage = useQuery({
    queryKey: ["spend", "summary", days],
    queryFn: () => spendApi.summary(token!, days),
    enabled: !!token,
    staleTime: 60_000,
  });
  const dailyQ = useQuery({
    queryKey: ["spend", "daily", days],
    queryFn: () => spendApi.daily(token!, days),
    enabled: !!token,
    staleTime: 60_000,
  });

  const dailyMax = useMemo(
    () => Math.max(1, ...((dailyQ.data ?? []).map((d) => d.cost_usd))),
    [dailyQ.data],
  );

  return (
    <PageShell width="wide">
      <PageHeader
        title={t("settings.title")}
        description={t("settings.subtitle")}
        tabs={<SectionTabs set="settings" />}
      />

      <PushCard />

      <Card>
        <CardHeader>
          <CardTitle>{t("settings.accountTitle")}</CardTitle>
          <CardDescription>
            {t("settings.signedInAs", { email: data?.user?.email ?? "" })}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm">
          <Row label={t("settings.name")}>{data?.user?.name || "—"}</Row>
          <Row label={t("settings.role")}>
            {data?.isAdmin ? (
              <Badge variant="brand">{t("settings.roleAdmin")}</Badge>
            ) : (
              <Badge>{t("settings.roleUser")}</Badge>
            )}
          </Row>
          <Row label={t("settings.tokenExpires")}>
            {data?.celmisExpiresAt
              ? new Date(data.celmisExpiresAt).toLocaleString()
              : "—"}
          </Row>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2">
              <ActivityIcon className="h-4 w-4" /> {t("settings.usageTitle")}
            </CardTitle>
            <CardDescription>
              {t("settings.usageDescription")}{" "}
              {/* The only way into LLM settings — 44px tall on a phone, with
                  -my-2 so the description line keeps its height. */}
              <Link
                className="inline-flex min-h-11 -my-2 items-center underline sm:my-0 sm:min-h-0"
                href="/settings/llm"
              >
                {t("settings.configureLlm")}
              </Link>
            </CardDescription>
          </div>
          {/* Three targets side by side: full height and a wider gap on a
              phone so 7d/30d/90d can't be confused for one another. */}
          <div className="flex gap-2 sm:gap-1">
            {RANGE_OPTIONS.map((r) => (
              <button
                key={r.days}
                onClick={() => setDays(r.days)}
                className={`min-h-11 px-2 py-1 text-xs rounded border sm:min-h-0 ${
                  days === r.days
                    ? "bg-[var(--color-foreground)] text-[var(--color-background)] border-transparent"
                    : "border-[var(--color-border)] text-[var(--color-muted-foreground)]"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {usage.isLoading && (
            <div className="text-xs text-[var(--color-muted-foreground)]">
              {t("settings.loadingUsage")}
            </div>
          )}
          {usage.error && (
            <div className="text-xs text-red-600">
              {t("settings.loadUsageFailed", {
                message: (usage.error as Error).message,
              })}
            </div>
          )}
          {usage.data && (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <Metric
                  label={t("settings.metricCalls")}
                  value={usage.data.calls.toString()}
                />
                <Metric
                  label={t("settings.metricTokensIn")}
                  value={formatCompact(usage.data.tokens_in + usage.data.cached_tokens_in)}
                />
                <Metric
                  label={t("settings.metricTokensOut")}
                  value={formatCompact(usage.data.tokens_out)}
                />
                <Metric
                  label={t("settings.metricCost")}
                  value={formatCost(usage.data.cost_usd)}
                  hint={[
                    // "$0.00 for 193K tokens" reads as an accounting bug. It
                    // is not: agent sessions run on a Claude subscription, so
                    // they are billed by seat and not per token. Say so.
                    usage.data.subscription_calls > 0
                      ? t("settings.subscriptionCalls", { count: usage.data.subscription_calls })
                      : "",
                    usage.data.cache_hit_pct > 0
                      ? t("settings.cacheHit", { pct: usage.data.cache_hit_pct.toFixed(0) })
                      : "",
                  ].filter(Boolean).join(" · ") || undefined}
                />
              </div>

              {/* One "cost" line answers no question anyone has. A Claude
                  subscription is a flat monthly seat; a BYOK provider key is
                  metered per token and is the only half that can grow the
                  bill. Split, and label which is which. */}
              {(usage.data.by_billing ?? []).length > 0 && (
                <div className="grid gap-3 sm:grid-cols-2">
                  {(usage.data.by_billing ?? []).map((row) => {
                    const isSub = row.key === "subscription";
                    return (
                      <div
                        key={row.key}
                        className="min-w-0 rounded-lg border border-[var(--color-border)] p-3"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium">
                            {t(isSub ? "settings.billingSubscription" : "settings.billingApiKey")}
                          </span>
                          <Badge variant={isSub ? "outline" : "brand"}>
                            {isSub ? t("settings.billingSeat") : formatCost(row.cost_usd)}
                          </Badge>
                        </div>
                        <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                          {t(isSub ? "settings.billingSubscriptionHint" : "settings.billingApiKeyHint")}
                        </p>
                        <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                          <div>
                            <div className="text-[var(--color-muted-foreground)]">
                              {t("settings.metricCalls")}
                            </div>
                            <div className="font-medium">{row.calls}</div>
                          </div>
                          <div>
                            <div className="text-[var(--color-muted-foreground)]">
                              {t("settings.metricTokensIn")}
                            </div>
                            <div className="font-medium">
                              {formatCompact(row.tokens_in + row.cached_tokens_in)}
                            </div>
                          </div>
                          <div>
                            <div className="text-[var(--color-muted-foreground)]">
                              {t("settings.metricTokensOut")}
                            </div>
                            <div className="font-medium">{formatCompact(row.tokens_out)}</div>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              {(dailyQ.data ?? []).length > 0 ? (
                <div>
                  <div className="text-xs text-[var(--color-muted-foreground)] mb-1">
                    {t("settings.dailyCost")}
                  </div>
                  <div className="flex items-end gap-[2px] h-16">
                    {(dailyQ.data ?? []).map((d) => (
                      <div
                        key={d.date}
                        title={t("settings.barTitle", {
                          date: d.date,
                          cost: d.cost_usd.toFixed(4),
                          runs: d.calls,
                        })}
                        className="flex-1 bg-[var(--color-brand,#4f46e5)] rounded-t"
                        style={{
                          height: `${Math.max(2, (d.cost_usd / dailyMax) * 100)}%`,
                          opacity: 0.75,
                        }}
                      />
                    ))}
                  </div>
                </div>
              ) : (
                <div className="text-xs text-[var(--color-muted-foreground)] flex items-center gap-2">
                  <ZapIcon className="h-3.5 w-3.5" />
                  {t("settings.noReviews")}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <DeleteAccountCard />
    </PageShell>
  );
}

function DeleteAccountCard() {
  const t = useT();
  const token = useToken();
  const [confirming, setConfirming] = useState(false);
  const del = useMutation({
    mutationFn: () => authApi.deleteAccount(token!),
    onSuccess: () => {
      toast.success(t("settings.deleteAccountDone"));
      void signOut({ callbackUrl: "/login" });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card className="border-red-500/40">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-red-500">
          <TrashIcon className="h-4 w-4" /> {t("settings.dangerZoneTitle")}
        </CardTitle>
        <CardDescription>{t("settings.deleteAccountDescription")}</CardDescription>
      </CardHeader>
      <CardContent>
        {!confirming ? (
          <Button variant="outline" className="border-red-500/50 text-red-500 hover:bg-red-500/10"
            onClick={() => setConfirming(true)}>
            {t("settings.deleteAccount")}
          </Button>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm text-[var(--color-muted-foreground)]">
              {t("settings.deleteAccountConfirm")}
            </span>
            <Button variant="outline" size="sm" onClick={() => setConfirming(false)} disabled={del.isPending}>
              {t("common.cancel")}
            </Button>
            <Button size="sm" className="bg-red-600 hover:bg-red-700 text-white"
              onClick={() => del.mutate()} disabled={del.isPending}>
              {del.isPending ? t("settings.deletingAccount") : t("settings.deleteAccountYes")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between py-2 border-b border-[var(--color-border)] last:border-0">
      <span className="text-[var(--color-muted-foreground)]">{label}</span>
      <span>{children}</span>
    </div>
  );
}

function Metric({
  label, value, hint,
}: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded border border-[var(--color-border)] p-3">
      <div className="text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
        {label}
      </div>
      <div className="text-lg font-semibold">{value}</div>
      {hint && (
        <div className="text-[10px] text-[var(--color-muted-foreground)] mt-0.5">
          {hint}
        </div>
      )}
    </div>
  );
}

function formatCompact(n: number): string {
  if (n < 1_000) return n.toString();
  if (n < 1_000_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}
