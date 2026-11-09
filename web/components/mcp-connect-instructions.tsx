"use client";

/**
 * How to connect an MCP client by signing in through the browser, one short
 * section per client. Shared by Settings > MCP and the Codebase Connect page;
 * `url` is the address each of them already computes for its own endpoint.
 */

import { CodeBlock } from "@/components/codebase/copy";
import { useT } from "@/lib/i18n";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="text-sm font-medium">{title}</h3>
      {children}
    </section>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-[var(--color-muted-foreground)]">{children}</p>;
}

export function McpConnectInstructions({ url }: { url: string }) {
  const t = useT();
  const generic = JSON.stringify({ mcpServers: { celmis: { url } } }, null, 2);
  return (
    <div className="space-y-4" data-testid="mcp-connect-instructions">
      <Section title={t("oauth.apps.client.cc.title")}>
        <CodeBlock wrap text={`claude mcp add --transport http celmis ${url}`} />
        <Note>{t("oauth.apps.client.cc.then")}</Note>
        <Note>{t("oauth.apps.client.cc.scope")}</Note>
      </Section>
      <Section title={t("oauth.apps.client.plugin.title")}>
        <Note>{t("oauth.apps.client.plugin.body")}</Note>
      </Section>
      <Section title={t("oauth.apps.client.ca.title")}>
        <CodeBlock wrap text={url} />
        <Note>{t("oauth.apps.client.ca.body")}</Note>
      </Section>
      <Section title={t("oauth.apps.client.other.title")}>
        <Note>{t("oauth.apps.client.other.body")}</Note>
        <CodeBlock wrap text={generic} />
        <Note>{t("oauth.apps.client.other.file")}</Note>
      </Section>
      <Note>{t("oauth.apps.tokenFallback")}</Note>
      <Note>{t("oauth.apps.client.access")}</Note>
    </div>
  );
}
