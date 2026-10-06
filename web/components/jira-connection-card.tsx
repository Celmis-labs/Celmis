"use client";

/**
 * The Jira connection: the site, the Atlassian account and an API token, saved
 * only after the server verified them against the site (PUT /api/connections/
 * jira). The token can be copied server-side from the Bitbucket connection, so
 * it never travels through the browser twice. Below the form, an admin can
 * preview a task the way a review reads it — the curated fields and the exact
 * text handed to the model.
 */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import {
  CheckCircle2Icon, ExternalLinkIcon, RefreshCwIcon, TrashIcon, XCircleIcon,
} from "lucide-react";

import {
  api, type ConnectionStatus, type ConnectionVerifyResult, type TaskPreview,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const KEY_RE = /^[A-Z][A-Z0-9_]{1,9}-\d{1,9}$/;

export function JiraConnectionCard({
  current,
  bitbucket,
  onUpdated,
}: {
  current: ConnectionStatus | undefined;
  /** The Bitbucket connection, to offer reusing its email and token. */
  bitbucket: ConnectionStatus | undefined;
  onUpdated: () => void;
}) {
  const token = useToken();
  const t = useT();
  const { confirm, dialog } = useConfirm();
  const connected = Boolean(current?.connected);
  const [formError, setFormError] = useState<string | null>(null);
  const [reuse, setReuse] = useState(false);
  const canReuse = Boolean(bitbucket?.connected && bitbucket.metadata?.atlassian_email);
  const siteUrl = String(current?.metadata?.jira_base_url ?? "");

  const upsert = useMutation({
    mutationFn: async (form: { token: string; email: string; base_url: string; reuse: boolean }) =>
      api<ConnectionVerifyResult>("/api/connections/jira", {
        method: "PUT",
        token,
        json: {
          provider: "jira",
          token: form.reuse ? "" : form.token,
          email: form.reuse ? undefined : form.email,
          base_url: form.base_url,
          reuse_bitbucket: form.reuse,
        },
      }),
    onSuccess: (res) => {
      setFormError(res.ok ? null : (res.error ?? t("connections.verificationFailed")));
      if (res.ok) {
        toast.success(
          res.username
            ? t("connections.connectedAs", { name: "Jira", username: res.username })
            : t("connections.connectedToast", { name: "Jira" }),
        );
        onUpdated();
      }
    },
    onError: (err: Error) => setFormError(err.message),
  });

  const remove = useMutation({
    mutationFn: async () => api<void>("/api/connections/jira", { method: "DELETE", token }),
    onSuccess: () => {
      toast.success(t("connections.disconnectedToast", { name: "Jira" }));
      onUpdated();
    },
  });

  const verify = useMutation({
    mutationFn: async () =>
      api<ConnectionVerifyResult>("/api/connections/jira/verify", { method: "POST", token }),
    onSuccess: (res) =>
      res.ok
        ? toast.success(t("connections.tokenStillWorks", {
            name: "Jira", status: res.username ?? t("connections.ok"),
          }))
        : toast.error(t("connections.providerError", {
            name: "Jira", error: res.error ?? t("connections.verifyFailed"),
          })),
  });

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const baseUrl = String(fd.get("base_url") || "").trim();
    // The server decides (https, an Atlassian Cloud site or an allowed host);
    // this only catches the obvious slip before a token is sent anywhere.
    if (!/^https:\/\/[^/?#\s]+/i.test(baseUrl)) {
      setFormError(t("connections.jira.siteInvalid"));
      return;
    }
    const tok = String(fd.get("token") || "").trim();
    const email = String(fd.get("email") || "").trim();
    if (!reuse && (!tok || !email)) return;
    setFormError(null);
    upsert.mutate({ token: tok, email, base_url: baseUrl, reuse });
  };

  return (
    <Card>
      <CardHeader className="flex-row items-start gap-4">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-600 font-bold text-white">
          J
        </div>
        <div className="flex-1">
          <CardTitle className="flex items-center gap-2">
            Jira
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
                  account: (current?.metadata?.username as string | undefined) || "—",
                  updated: formatDateTime(current?.updated_at),
                })
              : t("connections.jira.desc")}
            {connected && siteUrl && (
              <span className="block font-mono text-xs">
                {t("connections.jira.site", { url: siteUrl })}
              </span>
            )}
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
                  title: t("connections.disconnectConfirm", { name: "Jira" }),
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
        <Callout tone="info">{t("connections.jira.llmNotice")}</Callout>

        <div className="flex justify-end">
          <a
            href="https://id.atlassian.com/manage-profile/security/api-tokens"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm text-[var(--color-brand)] hover:underline"
          >
            {t("connections.openTokenPage")} <ExternalLinkIcon className="h-3 w-3" />
          </a>
        </div>

        <form method="post" onSubmit={onSubmit} className="grid gap-3">
          <div>
            <Label htmlFor="jira-url">{t("connections.jira.siteLabel")}</Label>
            <Input
              id="jira-url"
              name="base_url"
              type="url"
              inputMode="url"
              placeholder="https://your-company.atlassian.net"
              defaultValue={siteUrl}
              autoComplete="off"
              aria-describedby="jira-url-help"
              aria-invalid={formError ? true : undefined}
              required
            />
            <p id="jira-url-help" className="mt-1 text-xs text-[var(--color-muted-foreground)]">
              {t("connections.jira.siteHelp")}
            </p>
          </div>
          {canReuse && (
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={reuse}
                onChange={(e) => setReuse(e.target.checked)}
              />
              <span>
                {t("connections.jira.reuse")}
                <span className="block text-xs text-[var(--color-muted-foreground)]">
                  {t("connections.jira.reuseHelp")}
                </span>
              </span>
            </label>
          )}
          {!reuse && (
            <>
              <div>
                <Label htmlFor="jira-email">{t("connections.jira.emailLabel")}</Label>
                <Input
                  id="jira-email"
                  name="email"
                  type="email"
                  placeholder={t("connections.emailPlaceholder")}
                  defaultValue={(current?.metadata?.atlassian_email as string) || ""}
                  autoComplete="off"
                  required
                />
              </div>
              <div>
                <Label htmlFor="jira-token">{t("connections.jira.tokenLabel")}</Label>
                <Input
                  id="jira-token"
                  name="token"
                  type="password"
                  placeholder={connected ? t("connections.replaceTokenPlaceholder") : "ATATT…"}
                  autoComplete="off"
                  aria-describedby="jira-token-help"
                  required
                />
                <p id="jira-token-help" className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                  {t("connections.jira.tokenHelp")}
                </p>
              </div>
            </>
          )}
          {formError && (
            <p role="alert" className="text-sm text-[var(--color-destructive)]">
              {formError}
            </p>
          )}
          <div className="flex justify-end">
            <Button type="submit" disabled={upsert.isPending}>
              {upsert.isPending
                ? t("connections.verifying")
                : connected ? t("connections.replaceToken") : t("connections.saveAndVerify")}
            </Button>
          </div>
        </form>

        {connected && <TaskPreviewPanel />}
      </CardContent>
      {dialog}
    </Card>
  );
}

/** One task, as a review would read it. Read fresh — the cache is bypassed. */
function TaskPreviewPanel() {
  const token = useToken();
  const t = useT();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const preview = useMutation({
    mutationFn: async (issueKey: string) =>
      api<TaskPreview>(`/api/task-context/issue/${encodeURIComponent(issueKey)}`, { token }),
    onSuccess: () => setError(null),
    onError: (err: Error) => setError(err.message),
  });
  const data = preview.data;
  return (
    <section className="space-y-3 border-t border-[var(--color-border)] pt-4" aria-labelledby="jira-preview-title">
      <div>
        <h3 id="jira-preview-title" className="text-sm font-semibold">{t("connections.jira.previewTitle")}</h3>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("connections.jira.previewHint")}</p>
      </div>
      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const k = key.trim().toUpperCase();
          if (!KEY_RE.test(k)) {
            setError(t("connections.jira.keyInvalid"));
            return;
          }
          setError(null);
          preview.mutate(k);
        }}
      >
        <div className="grow">
          <Label htmlFor="jira-preview-key">{t("connections.jira.previewKey")}</Label>
          <Input
            id="jira-preview-key"
            value={key}
            placeholder="PROJ-6066"
            className="max-w-xs font-mono uppercase"
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setKey(e.target.value)}
          />
        </div>
        <Button type="submit" variant="outline" disabled={preview.isPending}>
          {preview.isPending ? t("connections.jira.previewing") : t("connections.jira.previewButton")}
        </Button>
      </form>
      {error && <p role="alert" className="text-sm text-[var(--color-destructive)]">{error}</p>}
      {data && (
        <div className="space-y-3 text-sm">
          <div>
            <a href={data.url} target="_blank" rel="noreferrer" className="font-mono text-[var(--color-brand)] hover:underline">
              {data.key}
            </a>{" "}
            <span className="font-medium">{data.summary}</span>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {[data.issue_type, data.status, data.priority].filter(Boolean).join(" · ")}
            </p>
          </div>
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
              {t("connections.jira.criteria")}
              {data.criteria.length > 0 && data.criteria_source !== "none" && (
                <span className="ml-1 font-normal normal-case">
                  ({t(`connections.jira.criteriaFrom.${data.criteria_source}`)})
                </span>
              )}
            </h4>
            {data.criteria.length === 0 ? (
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("connections.jira.criteriaNone")}</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {data.criteria.map((c) => (
                  <li key={c.id} className="flex gap-2">
                    <span className="font-mono text-xs text-[var(--color-muted-foreground)]">{c.id}</span>
                    <span>{c.text}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {data.truncated && (
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("connections.jira.truncated")}</p>
          )}
          <details>
            <summary className="cursor-pointer text-xs font-medium">{t("connections.jira.sentToModel")}</summary>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-[var(--color-secondary)] p-3 text-xs">
              {data.llm_text}
            </pre>
          </details>
        </div>
      )}
    </section>
  );
}
