"use client";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { CheckCircle2Icon, ExternalLinkIcon, XCircleIcon, TrashIcon, RefreshCwIcon } from "lucide-react";
import { api, type ConnectionStatus, type ConnectionVerifyResult } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";

type Provider = "github" | "gitlab" | "bitbucket";

// `name`, `color` and `tokenUrl` are literal (brand / technical) values.
// `scopes` and `steps` hold i18n keys resolved through t() at render time.
// LLM provider keys are managed in /settings/llm — only git providers here.
const PROVIDER_INFO: Record<Provider, {
  name: string;
  color: string;
  tokenUrl: string;
  scopes: string;
  steps: string[];
}> = {
  github: {
    name: "GitHub",
    color: "bg-zinc-900 text-white",
    tokenUrl: "https://github.com/settings/tokens?type=beta",
    scopes: "connections.github.scopes",
    steps: [
      "connections.github.step1",
      "connections.github.step2",
      "connections.github.step3",
      "connections.github.step4",
      "connections.github.step5",
    ],
  },
  gitlab: {
    name: "GitLab",
    color: "bg-orange-600 text-white",
    tokenUrl: "https://gitlab.com/-/user_settings/personal_access_tokens",
    scopes: "connections.gitlab.scopes",
    steps: [
      "connections.gitlab.step1",
      "connections.gitlab.step2",
      "connections.gitlab.step3",
      "connections.gitlab.step4",
    ],
  },
  bitbucket: {
    name: "Bitbucket",
    color: "bg-blue-600 text-white",
    tokenUrl: "https://id.atlassian.com/manage-profile/security/api-tokens",
    scopes: "connections.bitbucket.scopes",
    steps: [
      "connections.bitbucket.step1",
      "connections.bitbucket.step2",
      "connections.bitbucket.step3",
      "connections.bitbucket.step4",
      "connections.bitbucket.step5",
    ],
  },
};

export default function ConnectionsPage() {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const conns = useQuery({
    queryKey: ["connections"],
    queryFn: () => api<ConnectionStatus[]>("/api/connections", { token }),
    enabled: !!token,
  });

  const byProvider = (p: Provider): ConnectionStatus | undefined =>
    conns.data?.find((c) => c.provider === p);

  const gitProviders: Provider[] = ["github", "gitlab", "bitbucket"];

  return (
    <PageShell width="wide">
      <PageHeader
        title={t("connections.title")}
        description={t("connections.intro")}
        tabs={<SectionTabs set="settings" />}
      />

      <div>
        <h2 className="text-sm font-semibold text-[var(--color-muted-foreground)] uppercase tracking-wide mb-3">
          {t("connections.gitProviders")}
        </h2>
        <div className="grid gap-4">
          {gitProviders.map((p) => (
            <ProviderCard
              key={p}
              provider={p}
              current={byProvider(p)}
              onUpdated={() => qc.invalidateQueries({ queryKey: ["connections"] })}
            />
          ))}
        </div>
      </div>

      {/* LLM provider keys live in /settings/llm — workspace-shared and
          admin-managed, so they don't belong next to personal git tokens. */}
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("connections.llmMoved")}{" "}
        <Link href="/settings/llm" className="font-medium text-[var(--color-brand)] underline">
          {t("connections.llmMovedLink")}
        </Link>
      </p>
    </PageShell>
  );
}

function ProviderCard({
  provider,
  current,
  onUpdated,
}: {
  provider: Provider;
  current: ConnectionStatus | undefined;
  onUpdated: () => void;
}) {
  const token = useToken();
  const t = useT();
  const { confirm, dialog } = useConfirm();
  const info = PROVIDER_INFO[provider];
  const [showInstructions, setShowInstructions] = useState(false);
  const connected = Boolean(current?.connected);

  const upsert = useMutation({
    mutationFn: async (form: { token: string; email?: string; workspace?: string }) =>
      api<ConnectionVerifyResult>(`/api/connections/${provider}`, {
        method: "PUT",
        token,
        json: { provider, ...form },
      }),
    onSuccess: (res) => {
      if (res.ok) {
        toast.success(
          res.username
            ? t("connections.connectedAs", { name: info.name, username: res.username })
            : t("connections.connectedToast", { name: info.name }),
        );
        onUpdated();
      } else {
        toast.error(
          t("connections.providerError", {
            name: info.name,
            error: res.error ?? t("connections.verificationFailed"),
          }),
        );
      }
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: async () =>
      api<void>(`/api/connections/${provider}`, { method: "DELETE", token }),
    onSuccess: () => {
      toast.success(t("connections.disconnectedToast", { name: info.name }));
      onUpdated();
    },
  });

  const verify = useMutation({
    mutationFn: async () =>
      api<ConnectionVerifyResult>(`/api/connections/${provider}/verify`, {
        method: "POST",
        token,
      }),
    onSuccess: (res) =>
      res.ok
        ? toast.success(
            t("connections.tokenStillWorks", {
              name: info.name,
              status: res.username ?? t("connections.ok"),
            }),
          )
        : toast.error(
            t("connections.providerError", {
              name: info.name,
              error: res.error ?? t("connections.verifyFailed"),
            }),
          ),
  });

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const tok = String(fd.get("token") || "").trim();
    if (!tok) return;
    upsert.mutate({
      token: tok,
      email: provider === "bitbucket" ? String(fd.get("email") || "").trim() : undefined,
      workspace: provider === "bitbucket" ? String(fd.get("workspace") || "").trim() : undefined,
    });
  };

  return (
    <Card>
      <CardHeader className="flex-row items-start gap-4">
        <div className={`flex h-10 w-10 items-center justify-center rounded-lg ${info.color} font-bold`}>
          {info.name.slice(0, 1)}
        </div>
        <div className="flex-1">
          <CardTitle className="flex items-center gap-2">
            {info.name}
            {connected ? (
              <Badge variant="success" className="gap-1">
                <CheckCircle2Icon className="h-3 w-3" /> {t("connections.connected")}
              </Badge>
            ) : (
              <Badge variant="outline" className="gap-1">
                <XCircleIcon className="h-3 w-3" /> {t("connections.notConnected")}
              </Badge>
            )}
          </CardTitle>
          <CardDescription>
            {connected
              ? t("connections.savedAs", {
                  account:
                    (current?.metadata?.username as string | undefined) ||
                    current?.account_label ||
                    "—",
                  updated: formatDateTime(current?.updated_at),
                })
              : t("connections.requiredScope", { scopes: t(info.scopes) })}
          </CardDescription>
        </div>
        {connected && (
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => verify.mutate()} disabled={verify.isPending}>
              <RefreshCwIcon className="h-3 w-3" />
              {verify.isPending ? t("connections.verifying") : t("connections.verify")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={async () => {
                const ok = await confirm({
                  title: t("connections.disconnectConfirm", { name: info.name }),
                  confirmLabel: t("common.remove"),
                  danger: true,
                });
                if (ok) remove.mutate();
              }}
            >
              <TrashIcon className="h-3 w-3" />
              {t("connections.remove")}
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <Button
            type="button"
            variant="link"
            size="sm"
            className="px-0"
            onClick={() => setShowInstructions((s) => !s)}
          >
            {showInstructions ? t("connections.hideInstructions") : t("connections.showInstructions")}
          </Button>
          <a
            href={info.tokenUrl}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-[var(--color-brand)] hover:underline inline-flex items-center gap-1"
          >
            {t("connections.openTokenPage")} <ExternalLinkIcon className="h-3 w-3" />
          </a>
        </div>

        {showInstructions && (
          <div className="text-sm space-y-2 text-[var(--color-muted-foreground)] bg-[var(--color-secondary)] rounded-md p-4">
            <ol className="list-decimal pl-5 space-y-1">
              {info.steps.map((s) => (
                <li key={s}>{t(s)}</li>
              ))}
            </ol>
            {provider === "github" && (
              <p className="pt-1 border-t border-[var(--color-border)]">
                {t("connections.github.classic")}
              </p>
            )}
          </div>
        )}

        <form method="post" onSubmit={onSubmit} className="grid gap-3">
          {provider === "bitbucket" && (
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label htmlFor={`${provider}-email`}>{t("connections.atlassianEmail")}</Label>
                <Input
                  id={`${provider}-email`}
                  name="email"
                  type="email"
                  placeholder={t("connections.emailPlaceholder")}
                  defaultValue={(current?.metadata?.atlassian_email as string) || ""}
                  required
                />
              </div>
              <div>
                <Label htmlFor={`${provider}-ws`}>{t("connections.workspaceSlug")}</Label>
                <Input
                  id={`${provider}-ws`}
                  name="workspace"
                  type="text"
                  placeholder={t("connections.workspacePlaceholder")}
                  defaultValue={(current?.metadata?.bitbucket_workspace as string) || ""}
                  required
                />
              </div>
            </div>
          )}
          <div>
            <Label htmlFor={`${provider}-token`}>{t("connections.tokenLabel")}</Label>
            <Input
              id={`${provider}-token`}
              name="token"
              type="password"
              placeholder={connected ? t("connections.replaceTokenPlaceholder") : "ghp_… / glpat_… / ATATT…"}
              autoComplete="off"
              required
            />
          </div>
          <div className="flex justify-end">
            <Button type="submit" disabled={upsert.isPending}>
              {upsert.isPending
                ? t("connections.verifying")
                : connected
                  ? t("connections.replaceToken")
                  : t("connections.saveAndVerify")}
            </Button>
          </div>
        </form>
      </CardContent>
      {dialog}
    </Card>
  );
}
