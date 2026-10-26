"use client";

/**
 * Connect a coding agent to the packs: a guide a person who has never done it
 * can follow to the end on the first try.
 *
 * The steps are the ones that went wrong when it was done for real: where the
 * token comes from (it depends on the install), that the plugin's marketplace
 * has to be added before Discover can find it, that installing does not ask for
 * the address and token (`/plugin configure` does, in a masked field), that a
 * restart is needed, and what each failure on the way means. The page ends with
 * those failures as symptom, meaning, remedy.
 *
 * Nothing here issues or shows a secret. A token is made on the MCP settings
 * page (or issued by the superadmin, or asked for with the request text below);
 * the plugin asks for it and the address shown here, the manual route names a
 * variable the person fills without the token reaching the screen, and what an
 * agent can read is what the person who owns the token can read.
 */

import { useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { useQuery } from "@tanstack/react-query";
import {
  BookMarkedIcon, ChevronDownIcon, CircleCheckIcon, DownloadIcon, KeyRoundIcon, LifeBuoyIcon,
  PlugIcon, SettingsIcon, TerminalSquareIcon, WorkflowIcon,
} from "lucide-react";

import { codebaseApi, mcpTokensApi, publicMcpUrl, workspacesApi } from "@/lib/api";
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
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

type Route = "plugin" | "manual";

/** The tools an agent calls, in the order it is meant to. */
const AGENT_STEPS = ["brief", "skills", "find", "read_symbol"] as const;

/** What to ask the agent to prove the connection, cheapest first. */
const CHECK_PROMPTS = ["repos", "skills", "brief"] as const;
const PROMPT_TEXT: Record<(typeof CHECK_PROMPTS)[number], string> = {
  repos: "call celmis repos",
  skills: "call celmis skills",
  brief: "call celmis brief for: <my task>",
};

/** The things that go wrong when a token is typed where it should not be. */
const DONTS = ["shell", "multi", "both", "chat"] as const;

/** Failures people met, in the order they meet them. Each has what is seen, what
 *  it means and what to do (`codebase.connect.trouble.<id>.symptom|meaning|fix`). */
const TROUBLES = [
  "substitution", "discover", "noserver", "saved", "failed",
  "forbidden", "shared", "notfound", "unreachable",
] as const;

/** Claude Code's own commands. Shown as they are typed: not translated. The
 *  plugin is `celmis-code` in the marketplace `celmis`
 *  (packaging/claude-plugin/celmis-code, .claude-plugin/marketplace.json). */
const CMD = {
  addMarketplace: "/plugin marketplace add Celmis-labs/Celmis",
  install: "/plugin install celmis-code@celmis",
  configure: "/plugin configure celmis-code@celmis",
  cliAddMarketplace: "claude plugin marketplace add Celmis-labs/Celmis",
  cliInstall: "claude plugin install celmis-code@celmis",
  cliConfigure: "claude plugin configure celmis-code@celmis",
} as const;

/** The manual route keeps the token in a shell variable. `read -s` asks for it
 *  without echoing it, so it is neither on screen nor in the shell history, and
 *  `export` lets the next command in the same window see it. A box of its own: it
 *  is run alone, and a line pasted together with another is how one ends up in the
 *  wrong place. */
const READ_TOKEN = "read -rs CELMIS_MCP_TOKEN && export CELMIS_MCP_TOKEN";

/** The server address is known only in the browser (the page's own origin), so the
 *  server render and the first client render must agree on a placeholder: reading
 *  `window` during render made them differ, React rebuilt the tree from the root
 *  and the theme class on <html> was lost. A store that never changes gives each
 *  side its own snapshot without a mismatch. */
const noopSubscribe = () => () => {};

/** Text with `code` pieces: what sits between a pair of backticks is a command, shown
 *  as one. The sentences of the troubleshooting are translated, the commands in them
 *  are not. */
function Inline({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, i) => i % 2 === 1 ? (
        <code key={i} className="rounded bg-[var(--color-muted)] px-1 py-0.5 font-mono text-[0.95em] text-[var(--color-foreground)] [overflow-wrap:anywhere]">
          {part}
        </code>
      ) : part)}
    </>
  );
}

export default function CodebaseConnectPage() {
  const t = useT();
  const token = useToken();
  const { data: session, status: sessionStatus } = useSession();
  const [route, setRoute] = useState<Route>("plugin");

  const packs = useQuery({
    queryKey: ["codebase", "packs"],
    queryFn: () => codebaseApi.list(token!),
    enabled: !!token,
  });

  // Who may make a token depends on the install: the same answer, and the same
  // cache entry, as the MCP settings page. Until the server has answered nothing
  // is claimed either way, and when it cannot be asked (`mine.isError`) the step
  // says so and offers both ways instead of deciding for the person.
  const mine = useQuery({
    queryKey: ["mcp-tokens-me"],
    queryFn: () => mcpTokensApi.mine(token!),
    enabled: !!token,
  });
  const selfService = mine.data?.self_service_enabled === true;
  const isSuperadmin = Boolean(session?.isSuperadmin);

  // The workspace the request text asks for: the one the badge in the header shows.
  const workspaces = useQuery({
    queryKey: ["workspaces", "me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const workspaceName = workspaces.data?.workspaces.find(
    (w) => w.id === workspaces.data?.active_id,
  )?.name;
  const request = t("codebase.connect.step1.request", {
    workspace: workspaceName ?? t("codebase.connect.step1.workspaceFallback"),
  });

  // Nothing is shown until what it depends on has answered: the session (who is the
  // superadmin), who issues tokens, and, for the request text, the workspace's name.
  // A request with a placeholder instead of the name is one that gets copied.
  const waiting = sessionStatus === "loading" || mine.isPending
    || (!selfService && !isSuperadmin && workspaces.isPending);

  // The address an agent reaches this server at. Same derivation as the MCP
  // settings page, so the two always agree.
  const mcpUrl = useSyncExternalStore(noopSubscribe, () => publicMcpUrl("/mcp/dev/"), () => "/mcp/dev/");
  const origin = useSyncExternalStore(noopSubscribe, () => publicMcpUrl(""), () => "");

  // The plugin asks Claude Code for two values, not the shell: the address (shown
  // below, the same one this page's origin gives) and the token from step 1, which
  // Claude Code keeps in the system keychain. Nothing here is exported or contains
  // a token; `/plugin configure` changes either later.
  const pluginAddress = origin || "<server address>";

  // The variable is named, never filled in: the shell expands it before Claude Code
  // runs, so the value is what gets saved, and the token was never typed here.
  const manualCommand = `claude mcp add --transport http celmis \\
  ${mcpUrl} \\
  --header "Authorization: Bearer \${CELMIS_MCP_TOKEN}"`;

  const visible = packs.data?.packs.length;

  // The plugin route has one step more than the manual one (the address and token
  // are asked for separately), so the numbers after step 2 follow the route.
  const plugin = route === "plugin";
  const checkN = plugin ? 4 : 3;

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

      <Callout tone="info" className="flex max-w-4xl items-start gap-2">
        <BookMarkedIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
        <span>
          {packs.isSuccess
            ? t("codebase.connect.visible", { count: visible ?? 0 })
            : t("codebase.connect.visibleUnknown")}{" "}
          {t("codebase.connect.sameAsYou")}
        </span>
      </Callout>

      <div className="flex max-w-4xl flex-col gap-2 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <p className="min-w-0 text-xs leading-relaxed text-[var(--color-muted-foreground)]">
          {t("codebase.connect.route.hint")}
        </p>
        <SegmentedControl<Route>
          className="shrink-0"
          value={route} onValueChange={setRoute}
          label={t("codebase.connect.title")} semantics="tabs"
          segments={[
            { value: "plugin", label: t("codebase.connect.route.plugin") },
            { value: "manual", label: t("codebase.connect.route.manual") },
          ]}
        />
      </div>

      <div className="max-w-4xl space-y-4">
        <Step n={1} icon={<KeyRoundIcon aria-hidden className="size-4" />}
              title={t("codebase.connect.step1.title")}
              desc={waiting ? "" : selfService
                ? t("codebase.connect.step1.desc")
                : isSuperadmin
                  ? t("codebase.connect.step1.descSuper")
                  : mine.isError
                    ? t("codebase.connect.step1.descUnknown")
                    : t("codebase.connect.step1.descAdmin")}>
          {waiting ? (
            <Skeleton className="h-9 w-48" />
          ) : (
            <>
              {(selfService || (mine.isError && !isSuperadmin)) && (
                <Link href="/settings/mcp" className={cn(buttonVariants({ variant: "outline" }))}>
                  <KeyRoundIcon />{t("codebase.connect.step1.link")}
                </Link>
              )}
              {!selfService && !isSuperadmin && <CodeBlock text={request} wrap />}
            </>
          )}
          {isSuperadmin && (
            <Callout tone="info" className="space-y-2">
              <p>{t("codebase.connect.step1.superadmin")}</p>
              <Link href="/admin/mcp-tokens"
                    className={cn(buttonVariants({ variant: "outline", size: "sm" }))}>
                <KeyRoundIcon />{t("codebase.connect.step1.superadminLink")}
              </Link>
            </Callout>
          )}
        </Step>

        {plugin ? (
          <>
            <Step n={2} icon={<DownloadIcon aria-hidden className="size-4" />}
                  title={t("codebase.connect.step2.title")} desc={t("codebase.connect.step2.desc")}>
              {/* Side by side only where there is room: two columns beside the side
                  panel cut a command short, and a command cut short is retyped wrong. */}
              <div className="grid gap-3 xl:grid-cols-2">
                <Variant title={t("codebase.connect.plugin.inside")}
                         note={t("codebase.connect.plugin.insideNote")}>
                  <CodeBlock text={CMD.addMarketplace} wrap />
                  <CodeBlock text={CMD.install} wrap />
                </Variant>
                <Variant title={t("codebase.connect.plugin.terminal")}
                         note={t("codebase.connect.plugin.terminalNote")}>
                  <CodeBlock text={CMD.cliAddMarketplace} wrap />
                  <CodeBlock text={CMD.cliInstall} wrap />
                </Variant>
              </div>
              <Callout tone="info">{t("codebase.connect.plugin.discover")}</Callout>
            </Step>

            <Step n={3} icon={<SettingsIcon aria-hidden className="size-4" />}
                  title={t("codebase.connect.enter.title")} desc={t("codebase.connect.enter.desc")}>
              <CodeBlock text={CMD.configure} />
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.connect.plugin.address")}
              </p>
              <CodeBlock text={pluginAddress} />
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.connect.enter.token")}
              </p>
              <Callout tone="warning" className="space-y-1.5">
                <p className="font-semibold">{t("codebase.connect.enter.dont.title")}</p>
                <ul className="list-disc space-y-1 pl-4">
                  {DONTS.map((d) => (
                    <li key={d}>{t(`codebase.connect.dont.${d}`)}</li>
                  ))}
                </ul>
                <Link href="/settings/mcp" className="inline-block font-medium underline">
                  {t("codebase.connect.step1.link")}
                </Link>
              </Callout>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.connect.enter.verify")}
              </p>
              <CodeBlock text={CMD.cliConfigure} />
            </Step>
          </>
        ) : (
          <Step n={2} icon={<TerminalSquareIcon aria-hidden className="size-4" />}
                title={t("codebase.connect.manual.title")} desc={t("codebase.connect.manual.desc")}>
            <Callout tone="warning">{t("codebase.connect.manual.note")}</Callout>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("codebase.connect.manual.prepare")}
            </p>
            <CodeBlock text={READ_TOKEN} />
            <CodeBlock text={manualCommand} />
          </Step>
        )}

        <Step n={checkN} icon={<CircleCheckIcon aria-hidden className="size-4" />}
              title={t("codebase.connect.check.title")} desc={t("codebase.connect.check.desc")}>
          <ul className="list-disc space-y-1 pl-4 text-sm">
            <li>{t("codebase.connect.check.restart")}</li>
            <li>{t("codebase.connect.check.mcp")}</li>
          </ul>
          <p className="text-sm">{t("codebase.connect.check.ask")}</p>
          <Pairs items={CHECK_PROMPTS.map((id) => ({
            code: PROMPT_TEXT[id], text: t(`codebase.connect.check.prompt.${id}`),
          }))} />
        </Step>

        <Step n={checkN + 1} icon={<WorkflowIcon aria-hidden className="size-4" />}
              title={t("codebase.connect.step3.title")} desc={t("codebase.connect.step3.desc")}>
          <Pairs numbered items={AGENT_STEPS.map((tool) => ({
            code: tool, text: t(`codebase.connect.tool.${tool}`),
          }))} />
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("codebase.connect.tryIt")}{" "}
            <Link href="/codebase/search" className="underline">{t("codebase.connect.tryItLink")}</Link>
          </p>
        </Step>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <LifeBuoyIcon aria-hidden className="size-4" />{t("codebase.connect.trouble.title")}
            </CardTitle>
            <CardDescription>{t("codebase.connect.trouble.desc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {TROUBLES.map((id) => (
              <details key={id} className="group rounded-lg border border-[var(--color-border)]">
                <summary className="flex cursor-pointer list-none items-start gap-2 px-3 py-2 text-sm font-medium [&::-webkit-details-marker]:hidden">
                  <span className="min-w-0 break-words">{t(`codebase.connect.trouble.${id}.symptom`)}</span>
                  {/* the one sign that the row opens */}
                  <ChevronDownIcon aria-hidden className="ml-auto mt-0.5 size-4 shrink-0 text-[var(--color-muted-foreground)] transition-transform group-open:rotate-180" />
                </summary>
                <dl className="space-y-2 border-t border-[var(--color-border)] px-3 py-2 text-xs leading-relaxed">
                  <div>
                    <dt className="font-semibold">{t("codebase.connect.trouble.meaning")}</dt>
                    <dd className="text-[var(--color-muted-foreground)]">
                      <Inline text={t(`codebase.connect.trouble.${id}.meaning`)} />
                    </dd>
                  </div>
                  <div>
                    <dt className="font-semibold">{t("codebase.connect.trouble.fix")}</dt>
                    <dd className="text-[var(--color-muted-foreground)]">
                      <Inline text={t(`codebase.connect.trouble.${id}.fix`)} />
                      {id === "substitution" && (
                        <>{" "}
                          <Link href="/settings/mcp" className="font-medium underline">
                            {t("codebase.connect.step1.link")}
                          </Link>
                        </>
                      )}
                    </dd>
                  </div>
                </dl>
              </details>
            ))}
          </CardContent>
        </Card>
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
    <Card className={className}>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <span aria-hidden className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[var(--color-primary)] text-xs font-semibold text-[var(--color-primary-foreground)]">
            {n}
          </span>
          {icon}{title}
        </CardTitle>
        {desc && <CardDescription>{desc}</CardDescription>}
      </CardHeader>
      <CardContent className="space-y-3">{children}</CardContent>
    </Card>
  );
}

/** One way of doing a step, side by side with the other ways. */
function Variant({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-2 rounded-lg border border-[var(--color-border)] p-3">
      <h4 className="text-sm font-semibold">{title}</h4>
      <p className="text-xs leading-relaxed text-[var(--color-muted-foreground)]">{note}</p>
      {children}
    </div>
  );
}

/** A command or tool name with what it does; `numbered` shows the order. */
function Pairs({ items, numbered = false }: {
  items: { code: string; text: string }[]; numbered?: boolean;
}) {
  return (
    <ol className="grid gap-2 sm:grid-cols-2">
      {items.map((item, i) => (
        <li key={item.code} className="flex min-w-0 gap-3 rounded-lg border border-[var(--color-border)] p-3">
          {numbered && (
            <span aria-hidden className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[var(--color-primary-soft)] text-xs font-semibold text-[var(--color-primary-soft-foreground)]">
              {i + 1}
            </span>
          )}
          <div className="min-w-0 space-y-0.5">
            <code className="break-words text-xs font-semibold">{item.code}</code>
            <p className="text-xs text-[var(--color-muted-foreground)]">{item.text}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}
