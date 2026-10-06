"use client";

/**
 * /admin/mcp-tokens — the superadmin issues, lists and revokes MCP tokens.
 *
 * A token is for ONE person and a named list of repositories (exact slugs
 * and/or globs). The list is kept on the server and looked up on every call,
 * so changing it or revoking the token applies at once and nothing is
 * reissued. The token itself is on screen once, in a dialog, and never again.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import { KeyRoundIcon, PlusIcon } from "lucide-react";

import {
  adminUsersApi, mcpTokensApi, type McpCallRow, type McpTokenIssued, type McpTokenRow,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

/** The same matching the server does (case-sensitive fnmatch): `*` any run,
 *  `?` one character. Used only for the live preview. */
function globMatches(pattern: string, name: string): boolean {
  const re = pattern
    .replace(/[.+^${}()|\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  try {
    return new RegExp(`^${re}$`).test(name);
  } catch {
    return false;
  }
}

const splitList = (text: string) =>
  text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);

function statusVariant(status: string) {
  return status === "active" ? "success" : status === "revoked" ? "destructive" : "outline";
}

export default function McpTokensPage() {
  const t = useT();
  const { data: session } = useSession();
  const allowed = Boolean(session?.isSuperadmin);
  const [tab, setTab] = useState<"tokens" | "calls">("tokens");

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<KeyRoundIcon className="h-6 w-6" />}
        title={t("mcpTokens.title")}
        description={t("mcpTokens.description")}
        tabs={<SectionTabs set="admin" />}
      />
      {session && !allowed ? (
        <Callout tone="warning">{t("mcpTokens.superadminOnly")}</Callout>
      ) : (
        <>
          <div className="flex gap-2">
            <Button size="sm" variant={tab === "tokens" ? "default" : "outline"}
                    onClick={() => setTab("tokens")}>{t("mcpTokens.tabTokens")}</Button>
            <Button size="sm" variant={tab === "calls" ? "default" : "outline"}
                    onClick={() => setTab("calls")}>{t("mcpTokens.tabCalls")}</Button>
          </div>
          {tab === "tokens" ? <TokensTab /> : <CallsTab />}
        </>
      )}
    </PageShell>
  );
}

function TokensTab() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [issued, setIssued] = useState<McpTokenIssued | null>(null);

  const list = useQuery({
    queryKey: ["mcp-tokens"],
    queryFn: () => mcpTokensApi.list(token!),
    enabled: !!token,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => mcpTokensApi.revoke(token!, id),
    onSuccess: () => {
      toast.success(t("mcpTokens.revoked"));
      qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <>
      <IssueForm onIssued={(r) => {
        setIssued(r);
        qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
      }} />

      <Dialog open={!!issued} onOpenChange={(open) => { if (!open) setIssued(null); }}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("mcpTokens.issuedTitle")}</DialogTitle>
            <DialogDescription>{t("mcpTokens.issuedDescription")}</DialogDescription>
          </DialogHeader>
          <Callout tone="warning">{t("mcpTokens.shownOnce")}</Callout>
          <code className="block break-all rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-2 text-[11px]">
            {issued?.token}
          </code>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={async () => {
              try {
                await navigator.clipboard.writeText(issued?.token ?? "");
                toast.success(t("mcpTokens.copied"));
              } catch {
                toast.error(t("mcpTokens.copyFailed"));
              }
            }}>{t("mcpTokens.copy")}</Button>
          </div>
          <div className="text-xs text-[var(--color-muted-foreground)]">
            {t("mcpTokens.snippetHint")}
          </div>
          <pre className="overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-3 text-[11px]">
            <code>{JSON.stringify(issued?.mcp_json ?? {}, null, 2)}</code>
          </pre>
          <div className="text-xs">
            {t("mcpTokens.matchedHint", { count: String(issued?.matched_repos.length ?? 0) })}
          </div>
          <div className="flex justify-end">
            <Button size="sm" variant="outline" onClick={() => setIssued(null)}>
              {t("mcpTokens.close")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Card>
        <CardHeader><CardTitle>{t("mcpTokens.listTitle")}</CardTitle></CardHeader>
        <CardContent className="space-y-2">
          {list.isLoading && <div className="text-sm">{t("mcpTokens.loading")}</div>}
          {!list.isLoading && (list.data ?? []).length === 0 && (
            <div className="text-sm text-[var(--color-muted-foreground)]">
              {t("mcpTokens.empty")}
            </div>
          )}
          {(list.data ?? []).map((row) => (
            <TokenRow key={row.id} row={row}
              onRevoke={async () => {
                const ok = await confirm({
                  title: t("mcpTokens.revokeTitle"),
                  description: t("mcpTokens.revokeBody", { user: row.user_email || row.user_id }),
                  confirmLabel: t("mcpTokens.revoke"),
                  danger: true,
                });
                if (ok) revoke.mutate(row.id);
              }} />
          ))}
        </CardContent>
      </Card>
      {dialog}
    </>
  );
}

function TokenRow({ row, onRevoke }: { row: McpTokenRow; onRevoke: () => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(row.repos.join("\n"));
  const save = useMutation({
    mutationFn: () => mcpTokensApi.patch(token!, row.id, { repos: splitList(text) }),
    onSuccess: () => {
      toast.success(t("mcpTokens.saved"));
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <div className="rounded-md border border-[var(--color-border)] p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{row.user_email || row.user_id}</span>
        <Badge variant={statusVariant(row.status)}>{t(`mcpTokens.status.${row.status}`)}</Badge>
        <Badge variant="outline">{row.kind}</Badge>
        {row.allow_write
          ? <Badge variant="warning">{t("mcpTokens.canWrite")}</Badge>
          : <Badge variant="success">{t("mcpTokens.readOnly")}</Badge>}
        <span className="ml-auto text-xs text-[var(--color-muted-foreground)]">
          {t("mcpTokens.expires", { date: row.expires_at.slice(0, 10) })}
          {" · "}
          {row.last_used_at
            ? t("mcpTokens.lastUsed", { date: row.last_used_at.slice(0, 16).replace("T", " ") })
            : t("mcpTokens.neverUsed")}
        </span>
      </div>
      {editing ? (
        <div className="mt-2 space-y-2">
          <textarea className="w-full rounded-md border border-[var(--color-border)] bg-transparent p-2 font-mono text-xs"
                    rows={4} value={text} onChange={(e) => setText(e.target.value)} />
          <div className="flex gap-2">
            <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
              {t("mcpTokens.save")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
              {t("mcpTokens.cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {row.repos.map((r) => (
            <code key={r} className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">{r}</code>
          ))}
          <span className="text-xs text-[var(--color-muted-foreground)]">
            {t("mcpTokens.matches", { count: String(row.matched_repos.length) })}
          </span>
        </div>
      )}
      {row.status === "active" && !editing && (
        <div className="mt-2 flex gap-2">
          <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
            {t("mcpTokens.editRepos")}
          </Button>
          <Button size="sm" variant="outline" onClick={onRevoke}>{t("mcpTokens.revoke")}</Button>
        </div>
      )}
    </div>
  );
}

function IssueForm({ onIssued }: { onIssued: (r: McpTokenIssued) => void }) {
  const t = useT();
  const token = useToken();
  const [q, setQ] = useState("");
  const [userRef, setUserRef] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [globs, setGlobs] = useState("");
  const [days, setDays] = useState("30");
  const [write, setWrite] = useState(false);

  const users = useQuery({
    queryKey: ["mcp-token-users", q],
    queryFn: () => adminUsersApi.search(token!, q.trim()),
    enabled: !!token,
  });
  const workspaces = useQuery({
    queryKey: ["mcp-token-workspaces"],
    queryFn: () => adminUsersApi.workspaces(token!),
    enabled: !!token,
  });
  const repos = useQuery({
    queryKey: ["mcp-token-repos", workspace],
    queryFn: () => mcpTokensApi.repos(token!, workspace),
    enabled: !!token && !!workspace,
  });

  const patterns = useMemo(() => [...picked, ...splitList(globs)], [picked, globs]);
  const matched = useMemo(
    () => (repos.data ?? []).filter((r) =>
      patterns.some((p) => globMatches(p, r.slug) || (r.full_name && globMatches(p, r.full_name)))),
    [repos.data, patterns],
  );
  const toggle = (slug: string) =>
    setPicked((cur) => cur.includes(slug) ? cur.filter((x) => x !== slug) : [...cur, slug]);

  const issue = useMutation({
    mutationFn: () => mcpTokensApi.issue(token!, {
      user_ref: userRef, workspace_id: workspace, repos: patterns,
      allow_write: write, expires_in_days: Number(days) || 30,
      profile: write ? "full" : "dev",
    }),
    onSuccess: (r) => {
      onIssued(r);
      setPicked([]); setGlobs(""); setWrite(false);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const ready = !!userRef && !!workspace && patterns.length > 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <PlusIcon className="h-4 w-4" /> {t("mcpTokens.issueTitle")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="mcp-user-q">{t("mcpTokens.person")}</Label>
            <Input id="mcp-user-q" value={q} onChange={(e) => setQ(e.target.value)}
                   placeholder={t("mcpTokens.personSearch")} />
            <Select className="w-full" value={userRef} onChange={setUserRef}
                    placeholder={t("mcpTokens.personPick")}
                    options={(users.data ?? []).filter((u) => u.is_active)
                      .map((u) => ({ value: u.id, label: u.email, hint: u.name }))} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="mcp-ws">{t("mcpTokens.workspace")}</Label>
            <Select id="mcp-ws" className="w-full" value={workspace}
                    onChange={(v) => { setWorkspace(v); setPicked([]); }}
                    placeholder={t("mcpTokens.workspacePick")}
                    options={(workspaces.data ?? []).map((w) => ({ value: w.id, label: w.name }))} />
          </div>
        </div>

        <div className="space-y-1">
          <Label>{t("mcpTokens.repos")}</Label>
          <div className="max-h-44 space-y-1 overflow-y-auto rounded-md border border-[var(--color-border)] p-2">
            {!workspace && (
              <div className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.pickWorkspaceFirst")}</div>
            )}
            {(repos.data ?? []).map((r) => (
              <label key={r.slug} className="flex cursor-pointer items-center gap-2 text-sm">
                <input type="checkbox" checked={picked.includes(r.slug)} onChange={() => toggle(r.slug)} />
                <code className="text-xs">{r.full_name || r.slug}</code>
              </label>
            ))}
          </div>
          <Input value={globs} onChange={(e) => setGlobs(e.target.value)}
                 placeholder={t("mcpTokens.globsPlaceholder")} />
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("mcpTokens.preview", { count: String(matched.length) })}
            {matched.length > 0 && ": " + matched.slice(0, 8).map((r) => r.slug).join(", ")
              + (matched.length > 8 ? " …" : "")}
          </p>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="mcp-days">{t("mcpTokens.days")}</Label>
            <Input id="mcp-days" type="number" min={1} value={days}
                   onChange={(e) => setDays(e.target.value)} />
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.daysHint")}</p>
          </div>
          <div className="space-y-1">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={write} onChange={(e) => setWrite(e.target.checked)} />
              {t("mcpTokens.allowWrite")}
            </label>
            {write && <Callout tone="warning">{t("mcpTokens.writeWarning")}</Callout>}
          </div>
        </div>

        <Button disabled={!ready || issue.isPending} onClick={() => issue.mutate()}>
          {issue.isPending ? t("mcpTokens.issuing") : t("mcpTokens.issue")}
        </Button>
      </CardContent>
    </Card>
  );
}

function CallsTab() {
  const t = useT();
  const token = useToken();
  const [status, setStatus] = useState("");
  const [tool, setTool] = useState("");
  const calls = useQuery({
    queryKey: ["mcp-calls", status, tool],
    queryFn: () => mcpTokensApi.calls(token!, { status: status || undefined, tool: tool || undefined }),
    enabled: !!token,
  });
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>{t("mcpTokens.callsTitle")}</CardTitle>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.callsHint")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          <Select value={status} onChange={setStatus} placeholder={t("mcpTokens.anyStatus")}
                  options={[{ value: "", label: t("mcpTokens.anyStatus") },
                            { value: "ok", label: "ok" },
                            { value: "denied", label: "denied" },
                            { value: "error", label: "error" }]} />
          <Input className="w-48" value={tool} onChange={(e) => setTool(e.target.value)}
                 placeholder={t("mcpTokens.toolFilter")} />
        </div>
        {calls.isLoading && <div className="text-sm">{t("mcpTokens.loading")}</div>}
        {!calls.isLoading && (calls.data ?? []).length === 0 && (
          <div className="text-sm text-[var(--color-muted-foreground)]">{t("mcpTokens.noCalls")}</div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <tbody>
              {(calls.data ?? []).map((c: McpCallRow, i) => (
                <tr key={`${c.ts}-${i}`} className="border-t border-[var(--color-border)]">
                  <td className="py-1 pr-3 whitespace-nowrap">{c.ts.slice(0, 19).replace("T", " ")}</td>
                  <td className="pr-3">{c.user_id}</td>
                  <td className="pr-3"><code>{c.tool}</code></td>
                  <td className="pr-3">{c.repos.join(", ")}</td>
                  <td className="pr-3">
                    <Badge variant={c.status === "ok" ? "success" : c.status === "denied" ? "warning" : "destructive"}>
                      {c.status}
                    </Badge>
                  </td>
                  <td className="pr-3 text-right">{c.result_bytes} B</td>
                  <td className="text-right">{c.duration_ms} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
