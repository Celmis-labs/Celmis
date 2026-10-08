"use client";

/**
 * How to point Claude Code or Cursor at this Celmis install.
 *
 * The gap this closes: the only way to get an MCP token was
 * `analyzer mcp issue-token` on the server, which needs MCP_JWT_SECRET and a
 * shell. Connecting an editor to Celmis is something a developer does on their
 * own laptop, so "ask an administrator to SSH in" was not an instruction — it
 * was a description of a gap with a page around it.
 *
 * The page gives the config to paste and the token to paste into it, in that
 * order, because the config is the part somebody has to understand and the
 * token is the part they have to copy.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckIcon, CopyIcon, KeyIcon, PlugIcon, TerminalIcon } from "lucide-react";

import { api, mcpTokensApi, publicMcpUrl, type McpTokenRow } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { InlineHelp } from "@/components/ui/inline-help";

type McpToken = {
  token: string;
  expires_in: number;
  scopes: string[];
  url: string;
  workspace_id: string;
};

/** Copy-to-clipboard that says whether it worked.
 *
 *  navigator.clipboard is unavailable over plain HTTP on some browsers, and a
 *  button that silently does nothing on the one page whose whole job is
 *  "copy this" is worse than no button. */
/** The write scopes a token may ask for — the server's `WRITE_SCOPES`. */
const WRITE_SCOPES = ["write:config", "write:repos", "write:reviews"];

function CopyButton({ text, label }: { text: string; label: string }) {
  const t = useT();
  const [done, setDone] = useState(false);
  return (
    <Button
      size="sm"
      variant="outline"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          toast.error(t("mcp.copyFailed"));
        }
      }}
    >
      {done ? <CheckIcon className="mr-1 h-3.5 w-3.5" />
            : <CopyIcon className="mr-1 h-3.5 w-3.5" />}
      {done ? t("mcp.copied") : label}
    </Button>
  );
}

function Snippet({ children }: { children: string }) {
  return (
    <pre className="mt-2 overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-3 text-[11px] leading-relaxed">
      <code>{children}</code>
    </pre>
  );
}

export default function McpPage() {
  const t = useT();
  const token = useToken();
  const [issued, setIssued] = useState<McpToken | null>(null);
  // Write scopes are opt-in: nothing ticked is the read-only token it always
  // was. The server refuses the ones the account's role cannot use.
  const [writeScopes, setWriteScopes] = useState<string[]>([]);
  const toggleScope = (scope: string) =>
    setWriteScopes((cur) => cur.includes(scope) ? cur.filter((x) => x !== scope) : [...cur, scope]);
  const qc = useQueryClient();

  // Tokens are issued by the superadmin unless the install turned self-service
  // on. Until the server has answered, nothing is offered: a button that then
  // answers 403 is the worse way to find out.
  const mine = useQuery({
    queryKey: ["mcp-tokens-me"],
    queryFn: () => mcpTokensApi.mine(token!),
    enabled: !!token,
  });
  const selfService = mine.data?.self_service_enabled === true;
  const revokeMine = useMutation({
    mutationFn: (id: string) => mcpTokensApi.revokeMine(token!, id),
    onSuccess: () => {
      toast.success(t("mcp.revoked"));
      qc.invalidateQueries({ queryKey: ["mcp-tokens-me"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const issue = useMutation({
    mutationFn: () =>
      api<McpToken>("/api/mcp/token", {
        method: "POST", token,
        json: writeScopes.length ? { scopes: writeScopes } : {},
      }),
    onSuccess: (r) => { setIssued(r); toast.success(t("mcp.issued")); },
    onError: (e) => toast.error((e as Error).message),
  });

  // Shown before a token exists too, so the shape is readable without having
  // to generate anything first — the config is the part to understand.
  // The developer endpoint is the one a person connects an editor to: read-only
  // tools over the repositories their token names.
  const url = issued?.url && issued.url !== "/mcp/" && issued.url !== "/mcp/dev/"
    ? issued.url
    : (typeof window !== "undefined" ? publicMcpUrl("/mcp/dev/") : "/mcp/dev/");
  const secret = issued?.token ?? "${CELMIS_MCP_TOKEN}";

  const claudeCode = `claude mcp add --transport http celmis \\
  ${url} \\
  --header "Authorization: Bearer ${secret}"`;

  // The header name is a variable, not a literal, and that is deliberate.
  // tests/security/test_download_workspace_header.py bans the literal
  // `Authorization: ` + backtick-Bearer anywhere in web/, because a request
  // that hand-rolls its own bearer skips requestHeaders() and loses the
  // X-Workspace hint — which silently resolves it to the account's DEFAULT
  // workspace. That guard cannot tell a real fetch from a config snippet
  // printed on screen, and it should not try: a blunt rule that catches an
  // extra string is worth more than a clever one somebody can slip past.
  const AUTH_HEADER = "Authorization";
  const jsonConfig = JSON.stringify({
    mcpServers: {
      celmis: {
        type: "http",
        url,
        headers: { [AUTH_HEADER]: `Bearer ${secret}` },
      },
    },
  }, null, 2);

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<PlugIcon className="h-6 w-6" />}
        title={t("mcp.title")}
        description={t("mcp.subtitle")}
        tabs={<SectionTabs set="settings" />}
      />

      {/* The person's own tokens: metadata and revoke, never a value. */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <KeyIcon className="h-4 w-4" /> {t("mcp.myTokensTitle")}
          </CardTitle>
          <CardDescription>
            {selfService ? t("mcp.selfServiceOn") : t("mcp.issuedByAdmin")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {(mine.data?.tokens ?? []).length === 0 && !mine.isLoading && (
            <div className="text-sm text-[var(--color-muted-foreground)]">{t("mcp.noTokens")}</div>
          )}
          {(mine.data?.tokens ?? []).map((row: McpTokenRow) => (
            <div key={row.id} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--color-border)] p-2 text-xs">
              <Badge variant={row.status === "active" ? "success" : "outline"}>{row.status}</Badge>
              <span>{row.matched_repos.length > 0 ? row.matched_repos.length : row.repos.length} {t("mcp.reposCount")}</span>
              <span className="text-[var(--color-muted-foreground)]">
                {t("mcp.expires", { date: row.expires_at.slice(0, 10) })}
              </span>
              {row.status === "active" && (
                <Button size="sm" variant="outline" className="ml-auto"
                        disabled={revokeMine.isPending}
                        onClick={() => revokeMine.mutate(row.id)}>
                  {t("mcp.revoke")}
                </Button>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Step 1 — the token, because everything below needs it. Only when the
          install lets people issue their own. */}
      {selfService && (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <KeyIcon className="h-4 w-4" /> {t("mcp.step1Title")}
          </CardTitle>
          <CardDescription>{t("mcp.step1Body")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <fieldset className="space-y-1.5">
            <legend className="text-xs font-medium">{t("mcp.writeScopes")}</legend>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {WRITE_SCOPES.map((scope) => (
                <label key={scope} className="flex items-center gap-1.5 text-xs">
                  <input type="checkbox" checked={writeScopes.includes(scope)}
                         onChange={() => toggleScope(scope)} />
                  <code className="text-[11px]">{scope}</code>
                </label>
              ))}
            </div>
            <p className="text-[11px] text-[var(--color-muted-foreground)]">
              {t("mcp.writeScopesHint")}
            </p>
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={issue.isPending} onClick={() => issue.mutate()}>
              {issue.isPending ? t("mcp.issuing") : t("mcp.issue")}
            </Button>
            {issued && (
              <>
                {issued.scopes.some((x) => x.startsWith("write:"))
                  ? (
                    <Badge variant="warning" className="text-[10px]">
                      {t("mcp.canWrite")}
                    </Badge>
                  )
                  : (
                    <Badge variant="success" className="text-[10px]">
                      {t("mcp.readOnly")}
                    </Badge>
                  )}
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {issued.scopes.join(" · ")}
                </span>
              </>
            )}
          </div>

          {issued && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <code className="flex-1 overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-2 text-[11px]">
                  {issued.token}
                </code>
                <CopyButton text={issued.token} label={t("mcp.copyToken")} />
              </div>
              {/* Said once, plainly: this is the only time it is on screen. */}
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("mcp.tokenOnceHint", {
                  days: String(Math.round(issued.expires_in / 86400)),
                })}
              </p>
            </>
          )}

          <InlineHelp question={t("mcp.whatCanItDo")}>
            {t("mcp.whatCanItDoBody")}
          </InlineHelp>
        </CardContent>
      </Card>
      )}

      {/* Step 2 — Claude Code, the one-liner. */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <TerminalIcon className="h-4 w-4" /> {t("mcp.step2Title")}
          </CardTitle>
          <CardDescription>{t("mcp.step2Body")}</CardDescription>
        </CardHeader>
        <CardContent>
          <Snippet>{claudeCode}</Snippet>
          <div className="mt-2">
            <CopyButton text={claudeCode} label={t("mcp.copyCommand")} />
          </div>
        </CardContent>
      </Card>

      {/* Step 3 — everything else, which is a JSON file. */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("mcp.step3Title")}</CardTitle>
          <CardDescription>{t("mcp.step3Body")}</CardDescription>
        </CardHeader>
        <CardContent>
          <Snippet>{jsonConfig}</Snippet>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <CopyButton text={jsonConfig} label={t("mcp.copyConfig")} />
          </div>
          <InlineHelp className="mt-3" question={t("mcp.whereFile")}>
            {t("mcp.whereFileBody")}
          </InlineHelp>
        </CardContent>
      </Card>

      {/* Step 4 — how to know it worked, which every setup guide forgets. */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("mcp.verifyTitle")}</CardTitle>
          <CardDescription>{t("mcp.verifyBody")}</CardDescription>
        </CardHeader>
        <CardContent>
          <Snippet>{t("mcp.verifyExample")}</Snippet>
          <InlineHelp className="mt-3" question={t("mcp.troubleTitle")}>
            {t("mcp.troubleBody")}
          </InlineHelp>
        </CardContent>
      </Card>
    </PageShell>
  );
}
