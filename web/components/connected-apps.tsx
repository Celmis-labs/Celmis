"use client";

/**
 * "Connect with your browser" and the list of apps that did.
 *
 * An MCP client given only the address signs in through the browser (Settings >
 * MCP is where a person reads how and, afterwards, where they take an app back).
 * Everyone sees their own connections; an owner or admin of a workspace also
 * sees the ones made in it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PlugZapIcon } from "lucide-react";
import { useSyncExternalStore } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { McpConnectInstructions } from "@/components/mcp-connect-instructions";
import { oauthConnectionsApi, publicMcpUrl } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";

const noopSubscribe = () => () => {};

export function ConnectedApps() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  // The address is only known in the browser (see the note in settings/mcp).
  const url = useSyncExternalStore(noopSubscribe, () => publicMcpUrl("/mcp/"), () => "/mcp/");

  const list = useQuery({
    queryKey: ["oauth-connections"],
    queryFn: () => oauthConnectionsApi.list(token!),
    enabled: !!token,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => oauthConnectionsApi.revoke(token!, id),
    onSuccess: () => {
      toast.success(t("oauth.apps.revoked"));
      qc.invalidateQueries({ queryKey: ["oauth-connections"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <PlugZapIcon className="h-4 w-4" /> {t("oauth.apps.connectTitle")}
        </CardTitle>
        <CardDescription>{t("oauth.apps.connectBody")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <McpConnectInstructions url={url} />

        <div className="border-t border-[var(--color-border)] pt-3">
          <div className="mb-1 text-sm font-medium">{t("oauth.apps.title")}</div>
          <p className="mb-2 text-xs text-[var(--color-muted-foreground)]">{t("oauth.apps.desc")}</p>
          {(list.data ?? []).length === 0 && !list.isLoading && (
            <div className="text-sm text-[var(--color-muted-foreground)]">{t("oauth.apps.empty")}</div>
          )}
          <ul className="space-y-2" data-testid="connected-apps">
            {(list.data ?? []).map((c) => (
              <li
                key={c.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--color-border)] p-2 text-sm"
              >
                <div className="min-w-0 space-y-1">
                  <div className="font-medium">
                    {c.client_name}
                    <span className="ml-2 text-xs font-normal text-[var(--color-muted-foreground)]">
                      {c.redirect_host}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {c.scopes.map((s) => (
                      <Badge key={s} variant="outline" className="font-mono text-[10px]">{s}</Badge>
                    ))}
                  </div>
                  <div className="text-xs text-[var(--color-muted-foreground)]">
                    {[
                      c.workspace_name ? t("oauth.apps.workspace", { name: c.workspace_name }) : null,
                      c.last_used_at
                        ? t("oauth.apps.lastUsed", { date: new Date(c.last_used_at).toLocaleString() })
                        : null,
                      c.person ? t("oauth.apps.by", { person: c.person }) : null,
                    ].filter(Boolean).join(" · ")}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(c.id)}
                >
                  {t("oauth.apps.revoke")}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}
