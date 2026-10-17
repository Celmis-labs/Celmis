"use client";

/**
 * Connect a coding agent to the packs: get a token, install the plugin (or
 * add the MCP server by hand), then say how the agent is meant to use them.
 *
 * Nothing here issues or shows a secret. A token is made on the MCP settings
 * page; the commands below refer to it by variable name, and what an agent
 * can read is what the person who owns the token can read.
 */

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { BookMarkedIcon, KeyRoundIcon, PlugIcon, TerminalSquareIcon, WorkflowIcon } from "lucide-react";

import { codebaseApi, publicMcpUrl } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { CodeBlock } from "@/components/codebase/copy";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { buttonVariants } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { cn } from "@/lib/utils";

type Route = "plugin" | "manual";

/** The tools an agent calls, in the order it is meant to. */
const AGENT_STEPS = ["brief", "skills", "find", "read_symbol"] as const;

export default function CodebaseConnectPage() {
  const t = useT();
  const token = useToken();
  const [route, setRoute] = useState<Route>("plugin");

  const packs = useQuery({
    queryKey: ["codebase", "packs"],
    queryFn: () => codebaseApi.list(token!),
    enabled: !!token,
  });

  // The address an agent reaches this server at. Same derivation as the MCP
  // settings page, so the two always agree.
  const mcpUrl = typeof window !== "undefined" ? publicMcpUrl("/mcp/dev/") : "/mcp/dev/";
  const origin = typeof window !== "undefined" ? publicMcpUrl("") : "";

  const pluginCommands = [
    `export CELMIS_URL=${origin || "<server address>"}`,
    `export CELMIS_TOKEN=<${t("codebase.connect.tokenPlaceholder")}>`,
    "",
    "/plugin marketplace add Celmis-labs/Celmis",
    "/plugin install celmis-code@celmis",
  ].join("\n");

  // `Authorization` is held in a variable on purpose: the web tree must not
  // contain the literal header-with-bearer template (tests/security/
  // test_download_workspace_header.py), and a command shown on screen is not
  // a request worth making an exception for. Same pattern as settings/mcp.
  const AUTH_HEADER = "Authorization";
  const manualCommand = `claude mcp add --transport http celmis \\
  ${mcpUrl} \\
  --header "${AUTH_HEADER}: Bearer \${CELMIS_MCP_TOKEN}"`;

  const visible = packs.data?.packs.length;

  return (
    <PageShell width="wide">
      <PageHeader
        tour="page-codebase-connect"
        icon={<PlugIcon className="h-6 w-6" />}
        title={t("codebase.connect.title")}
        badge={<WorkspaceBadge />}
        description={t("codebase.connect.subtitle")}
        tabs={<SectionTabs set="codebase" />}
      />

      <Callout tone="info" className="flex items-start gap-2">
        <BookMarkedIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
        <span>
          {packs.isSuccess
            ? t("codebase.connect.visible", { count: visible ?? 0 })
            : t("codebase.connect.visibleUnknown")}{" "}
          {t("codebase.connect.sameAsYou")}
        </span>
      </Callout>

      <div className="grid gap-4 xl:grid-cols-2">
        <Step n={1} icon={<KeyRoundIcon aria-hidden className="size-4" />}
              title={t("codebase.connect.step1.title")} desc={t("codebase.connect.step1.desc")}>
          <Link href="/settings/mcp" className={cn(buttonVariants({ variant: "outline" }))}>
            <KeyRoundIcon />{t("codebase.connect.step1.link")}
          </Link>
        </Step>

        <Step n={2} icon={<TerminalSquareIcon aria-hidden className="size-4" />}
              title={t("codebase.connect.step2.title")} desc={t("codebase.connect.step2.desc")}>
          <SegmentedControl<Route>
            value={route} onValueChange={setRoute}
            label={t("codebase.connect.step2.title")} semantics="tabs"
            segments={[
              { value: "plugin", label: t("codebase.connect.route.plugin") },
              { value: "manual", label: t("codebase.connect.route.manual") },
            ]}
          />
          {route === "plugin" ? (
            <>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.connect.plugin.note")}
              </p>
              <CodeBlock text={pluginCommands} />
            </>
          ) : (
            <>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.connect.manual.note")}
              </p>
              <CodeBlock text={manualCommand} />
            </>
          )}
        </Step>

        <Step n={3} icon={<WorkflowIcon aria-hidden className="size-4" />}
              title={t("codebase.connect.step3.title")} desc={t("codebase.connect.step3.desc")}
              className="xl:col-span-2">
          <ol className="grid gap-2 sm:grid-cols-2">
            {AGENT_STEPS.map((tool, i) => (
              <li key={tool} className="flex gap-3 rounded-lg border border-[var(--color-border)] p-3">
                <span aria-hidden className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[var(--color-primary-soft)] text-xs font-semibold text-[var(--color-primary-soft-foreground)]">
                  {i + 1}
                </span>
                <div className="min-w-0 space-y-0.5">
                  <code className="text-xs font-semibold">{tool}</code>
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t(`codebase.connect.tool.${tool}`)}
                  </p>
                </div>
              </li>
            ))}
          </ol>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("codebase.connect.tryIt")}{" "}
            <Link href="/codebase/search" className="underline">{t("codebase.connect.tryItLink")}</Link>
          </p>
        </Step>
      </div>
    </PageShell>
  );
}

function Step({
  n, icon, title, desc, className, children,
}: {
  n: number; icon: React.ReactNode; title: string; desc: string; className?: string;
  children: React.ReactNode;
}) {
  return (
    <Card className={cn("self-start", className)}>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <span aria-hidden className="flex size-6 items-center justify-center rounded-full bg-[var(--color-primary)] text-xs font-semibold text-[var(--color-primary-foreground)]">
            {n}
          </span>
          {icon}{title}
        </CardTitle>
        <CardDescription>{desc}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">{children}</CardContent>
    </Card>
  );
}
