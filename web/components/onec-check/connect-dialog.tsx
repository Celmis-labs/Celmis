"use client";

/** "Connect to MCP": how the same check is called from an editor or agent. */

import { useSyncExternalStore } from "react";
import { PlugIcon } from "lucide-react";
import Link from "next/link";

import { CodeBlock } from "@/components/codebase/copy";
import { McpConnectInstructions } from "@/components/mcp-connect-instructions";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { publicMcpUrl } from "@/lib/api";
import { useT } from "@/lib/i18n";

const noopSubscribe = () => () => {};

export function ConnectMcpDialog() {
  const t = useT();
  const url = useSyncExternalStore(noopSubscribe, () => publicMcpUrl("/mcp/dev/"), () => "/mcp/dev/");
  const codex = `[mcp_servers.celmis]\nurl = "${url}"\nbearer_token_env_var = "CELMIS_MCP_TOKEN"`;
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          <PlugIcon className="mr-1.5 h-4 w-4" /> {t("onecCheck.connect")}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("onecCheck.connectTitle")}</DialogTitle>
          <DialogDescription>{t("onecCheck.connectIntro")}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <McpConnectInstructions url={url} />
          <section className="space-y-1.5">
            <h3 className="text-sm font-medium">Codex</h3>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.connectCodex")}</p>
            <CodeBlock text={codex} />
          </section>
          <section className="space-y-1.5">
            <h3 className="text-sm font-medium">{t("onecCheck.connectCall")}</h3>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.connectStep1")}</p>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.connectStep2")}</p>
            <CodeBlock wrap text={'inspect_external(file="<handle>", repo="<configuration>")'} />
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("onecCheck.connectStep3")}</p>
          </section>
          <p className="text-xs">
            <Link href="/settings/mcp" className="underline">{t("onecCheck.connectMore")}</Link>
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
