"use client";

/** Superadmin: project-scoped MCP tokens (create, copy once, list, revoke). */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { API_BASE, projectsApi, type ProjectMcpTokenIssued } from "@/lib/api";
import { useToken } from "@/lib/use-token";
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

/** The public base of this deployment, with the trailing slash the MCP mount needs. */
function mcpUrl(): string {
  const base = typeof window !== "undefined" && API_BASE.startsWith("/")
    ? window.location.origin : API_BASE;
  return `${base.replace(/\/$/, "")}/mcp/`;
}

async function copy(text: string, done: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(done);
  } catch {
    toast.error("Copy failed");
  }
}

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

  const command = issued
    ? `claude mcp add --transport http project-search ${mcpUrl()} --header "Authorization: Bearer ${issued.token}"`
    : "";

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
              onClick={() => copy(issued.token, t("projects.mcp.copied"))}>
              {t("projects.mcp.copy")}
            </Button>
            <p className="text-xs font-medium">{t("projects.mcp.command")}</p>
            <code className="block break-all text-xs">{command}</code>
            <Button size="sm" variant="outline"
              onClick={() => copy(command, t("projects.mcp.copied"))}>
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
