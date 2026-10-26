"use client";

/** Superadmin: project-scoped MCP tokens (create, copy once, list, revoke). */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { projectsApi, publicMcpUrl, type ProjectMcpTokenIssued } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { copyText } from "@/lib/copy";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

const TTLS = [
  { value: "3600", key: "projects.mcp.hour1" },
  { value: "86400", key: "projects.mcp.day1" },
  { value: "604800", key: "projects.mcp.day7" },
  { value: "2592000", key: "projects.mcp.day30" },
  { value: "7776000", key: "projects.mcp.day90" },
] as const;

/** The public MCP URL of this deployment, API prefix included. */
const mcpUrl = () => publicMcpUrl();

/** `copyText`, not `navigator.clipboard`: the token is copied from here and nowhere
 *  else, and this product is often served over plain HTTP, where the Clipboard API
 *  does not exist (lib/copy.ts). */
async function copy(text: string, done: string, failed: string) {
  if (await copyText(text)) toast.success(done);
  else toast.error(failed);
}

/** The token is shown once, in a field of its own, and never typed into a command: a
 *  token in a command stays on screen and in the shell history. The command names a
 *  shell variable instead and READ_TOKEN fills it (`read -s` takes the value without
 *  echoing it). The same two lines as the manual route of the connect guide
 *  (/codebase/connect), and the guide's sentence says how to use them. */
const READ_TOKEN = "read -rs CELMIS_MCP_TOKEN && export CELMIS_MCP_TOKEN";

/** One line on purpose: a command pasted in several lines is how a line ends up in the
 *  wrong place. `\${...}` is the shell's, not this file's: it is written, never filled in. */
const connectCommand = () =>
  `claude mcp add --transport http project-search ${mcpUrl()} --header "Authorization: Bearer \${CELMIS_MCP_TOKEN}"`;

export function ProjectMcpTokens({ projectId }: { projectId: string }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [ttl, setTtl] = useState("2592000");
  const [issued, setIssued] = useState<ProjectMcpTokenIssued | null>(null);
  const key = ["projects", projectId, "mcp-tokens"];

  const list = useQuery({
    queryKey: key,
    queryFn: () => projectsApi.mcpTokens(token!, projectId),
    enabled: !!token,
  });
  const create = useMutation({
    mutationFn: () => projectsApi.createMcpToken(token!, projectId, {
      label: label.trim(), ttl_seconds: Number(ttl),
    }),
    onSuccess: (row) => {
      setIssued(row);
      setLabel("");
      void qc.invalidateQueries({ queryKey: key });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => projectsApi.revokeMcpToken(token!, projectId, id),
    onSuccess: () => {
      toast.success(t("projects.mcp.revoked"));
      void qc.invalidateQueries({ queryKey: key });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const command = issued ? connectCommand() : "";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("projects.mcp.title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">{t("projects.mcp.hint")}</p>
        <div className="space-y-2">
          <div>
            <Label htmlFor="pmcp-label">{t("projects.mcp.label")}</Label>
            <Input id="pmcp-label" value={label} maxLength={80}
              onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div>
            <Label htmlFor="pmcp-ttl">{t("projects.mcp.ttl")}</Label>
            <Select value={ttl} onChange={setTtl}
              options={TTLS.map((o) => ({ value: o.value, label: t(o.key) }))} />
          </div>
          <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending || !token}>
            {t("projects.mcp.create")}
          </Button>
        </div>

        {issued && (
          <div className="space-y-2 rounded border border-[var(--color-border)] p-2">
            <p className="text-xs font-medium">{t("projects.mcp.created")}</p>
            <code className="block break-all text-xs">{issued.token}</code>
            <Button size="sm" variant="outline"
              onClick={() => copy(issued.token, t("projects.mcp.copied"), t("projects.mcp.copyFailed"))}>
              {t("projects.mcp.copy")}
            </Button>
            <p className="text-xs font-medium">{t("projects.mcp.command")}</p>
            <p className="text-xs text-muted-foreground">{t("codebase.connect.manual.prepare")}</p>
            <code className="block break-all text-xs">{READ_TOKEN}</code>
            <Button size="sm" variant="outline"
              onClick={() => copy(READ_TOKEN, t("projects.mcp.copied"), t("projects.mcp.copyFailed"))}>
              {t("projects.mcp.copy")}
            </Button>
            <code className="block break-all text-xs">{command}</code>
            <Button size="sm" variant="outline"
              onClick={() => copy(command, t("projects.mcp.copied"), t("projects.mcp.copyFailed"))}>
              {t("projects.mcp.copy")}
            </Button>
          </div>
        )}

        <div className="space-y-2">
          {(list.data ?? []).length === 0 && (
            <p className="text-xs text-muted-foreground">{t("projects.mcp.none")}</p>
          )}
          {(list.data ?? []).map((row) => {
            const status = row.revoked_at ? "revoked"
              : new Date(row.expires_at) <= new Date() ? "expired" : "active";
            return (
              <div key={row.id}
                className="flex items-center justify-between gap-2 rounded border px-2 py-1">
                <div className="min-w-0 text-xs">
                  <div className="truncate font-medium">{row.label || row.id.slice(0, 8)}</div>
                  <div className="text-muted-foreground">
                    {t("projects.mcp.expires")} {formatDateTime(row.expires_at)} ·{" "}
                    {row.last_used_at
                      ? `${t("projects.mcp.lastUsed")} ${formatDateTime(row.last_used_at)}`
                      : t("projects.mcp.never")}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <Badge variant={status === "active" ? "success" : "outline"}
                    className="text-[10px]">
                    {t(`projects.mcp.status.${status}`)}
                  </Badge>
                  {status === "active" && (
                    <Button size="sm" variant="ghost" onClick={() => revoke.mutate(row.id)}>
                      {t("projects.mcp.revoke")}
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
