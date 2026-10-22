"use client";

/**
 * /admin/mcp-tokens — the superadmin issues, lists and revokes MCP tokens.
 *
 * A token is for ONE person and a named list of repositories (exact slugs
 * and/or globs). The list is kept on the server and looked up on every call,
 * so changing it or revoking the token applies at once and nothing is
 * reissued. The token itself is on screen once, in a dialog, and never again.
 *
 * A token can also carry SHARED repositories: ones another workspace has chosen to
 * share with the token's workspace (/admin/sharing on the owner's side). They are
 * ticked one by one, never matched by a pattern, and a star never reaches them.
 * Only the superadmin sees any of this — the receiving workspace's other
 * administrators do not, by design — and a share ticked here is read through the
 * owner's rules on every call, so ending the share on the owner's side ends the
 * reading here at once.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import { KeyRoundIcon, PlusIcon } from "lucide-react";

import {
  adminUsersApi, mcpTokensApi, sharingApi, workspacesApi,
  type McpCallRow, type McpShare, type McpTokenIssued, type McpTokenRow,
} from "@/lib/api";
import {
  MAX_SHARES_PER_TOKEN, canTick, dayOf, hasPlace, heldBy, sharesToSave, wasShortened,
} from "@/lib/sharing";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { PersonCell } from "@/components/sharing/share-detail";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
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
  const [tab, setTab] = useState<"tokens" | "shared" | "calls">("tokens");

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
            <Button size="sm" variant={tab === "shared" ? "default" : "outline"}
                    onClick={() => setTab("shared")}>{t("mcpTokens.shared.tab")}</Button>
            <Button size="sm" variant={tab === "calls" ? "default" : "outline"}
                    onClick={() => setTab("calls")}>{t("mcpTokens.tabCalls")}</Button>
          </div>
          {tab === "tokens" ? <TokensTab /> : tab === "shared" ? <SharedTab /> : <CallsTab />}
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
  // The days the person asked for: a token that ends sooner was cut short by a share.
  const [askedDays, setAskedDays] = useState(30);

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
      <IssueForm tokens={list.data ?? []} onIssued={(r, days) => {
        setIssued(r);
        setAskedDays(days);
        qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
      }} />

      <Dialog open={!!issued} onOpenChange={(open) => { if (!open) setIssued(null); }}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("mcpTokens.issuedTitle")}</DialogTitle>
            <DialogDescription>{t("mcpTokens.issuedDescription")}</DialogDescription>
          </DialogHeader>
          <Callout tone="warning">{t("mcpTokens.shownOnce")}</Callout>
          {issued && wasShortened(issued.expires_at, askedDays, (issued.shares ?? []).length > 0) && (
            <Callout tone="info">
              {t("mcpTokens.shared.shortened")}{" "}
              {t("mcpTokens.expires", { date: dayOf(issued.expires_at) })}
            </Callout>
          )}
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
              held={heldBy(list.data ?? [], row.user_id, row.workspace_id)}
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

/** Can this token carry shared repositories at all? Only a read-only developer
 *  token (`kind=pat`, `profile=dev`); the server ignores the list on any other. */
const carriesShares = (row: McpTokenRow) =>
  row.kind === "pat" && row.profile === "dev" && !row.allow_write;

function TokenRow({
  row, held, onRevoke,
}: { row: McpTokenRow; held: Set<string>; onRevoke: () => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(row.repos.join("\n"));
  const listed = row.shares ?? [];
  const [picked, setPicked] = useState<string[]>(listed.map((s) => s.share_id));
  const canShare = carriesShares(row);

  // The shares of the token's workspace, asked for only while the list is being
  // edited: a page of many tokens does not ask once per row.
  const offered = useQuery({
    queryKey: ["mcp-shares", row.workspace_id],
    queryFn: () => mcpTokensApi.shares(token!, row.workspace_id),
    enabled: !!token && editing && canShare,
  });
  // What can be ticked: the live shares of the workspace, and every share the token
  // already lists (an ended one stays visible so it can be removed).
  const choices: Array<{ id: string; slug: string; owner: string; until: string; status: string;
                         share?: McpShare }> = [
    ...listed.map((s) => ({
      id: s.share_id, slug: s.slug, owner: s.owner_name, until: s.expires_at, status: s.status,
      share: (offered.data ?? []).find((o) => o.share_id === s.share_id),
    })),
    ...(offered.data ?? []).filter((o) => !listed.some((s) => s.share_id === o.share_id)).map((o) => ({
      id: o.share_id, slug: o.slug, owner: o.owner_name, until: o.expires_at, status: o.status, share: o,
    })),
  ];

  const save = useMutation({
    mutationFn: () => {
      const shares = sharesToSave(listed.map((s) => s.share_id), picked);
      return mcpTokensApi.patch(token!, row.id, {
        repos: splitList(text),
        ...(shares ? { shares } : {}),
      });
    },
    onSuccess: () => {
      toast.success(t("mcpTokens.saved"));
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
      qc.invalidateQueries({ queryKey: ["mcp-shares"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const toggle = (id: string) =>
    setPicked((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  return (
    <div className="rounded-md border border-[var(--color-border)] p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{row.user_email || row.user_id}</span>
        <Badge variant={statusVariant(row.status)}>{t(`mcpTokens.status.${row.status}`)}</Badge>
        <Badge variant="outline">{row.kind}</Badge>
        {row.allow_write
          ? <Badge variant="warning">{t("mcpTokens.canWrite")}</Badge>
          : <Badge variant="success">{t("mcpTokens.readOnly")}</Badge>}
        {listed.length > 0 && (
          <Badge variant="info">{t("mcpTokens.shared.chip", { count: listed.length })}</Badge>
        )}
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
          {canShare && choices.length > 0 && (
            <div className="space-y-1">
              <div className="text-xs font-medium">{t("mcpTokens.shared.title")}</div>
              <div className="max-h-44 space-y-1 overflow-y-auto rounded-md border border-[var(--color-border)] p-2">
                {choices.map((c) => {
                  const ticked = picked.includes(c.id);
                  // A share that has ended can only be removed; a live one is judged by
                  // its places, and a person who already holds it keeps theirs.
                  const open = c.status === "active" && (c.share
                    ? canTick(c.share, picked, held.has(c.id)) : true);
                  return (
                    <label key={c.id} className="flex cursor-pointer flex-wrap items-center gap-2 text-sm">
                      <Checkbox checked={ticked} disabled={!ticked && !open}
                                onChange={() => toggle(c.id)} />
                      <code className="text-xs">{c.slug}</code>
                      <span className="text-xs text-[var(--color-muted-foreground)]">
                        {t("mcpTokens.shared.from", { name: c.owner })}
                        {" · "}
                        {t("mcpTokens.shared.until", { date: dayOf(c.until) })}
                      </span>
                      {c.status !== "active" && (
                        <Badge variant="outline">{t(`mcpTokens.status.${c.status}`)}</Badge>
                      )}
                    </label>
                  );
                })}
              </div>
              {picked.length >= MAX_SHARES_PER_TOKEN && (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("mcpTokens.shared.tooMany", { max: MAX_SHARES_PER_TOKEN })}
                </p>
              )}
            </div>
          )}
          <div className="flex gap-2">
            <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
              {t("mcpTokens.save")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => {
              setEditing(false);
              setText(row.repos.join("\n"));
              setPicked(listed.map((s) => s.share_id));
            }}>
              {t("mcpTokens.cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {row.repos.map((r) => (
            <code key={r} className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">{r}</code>
          ))}
          {listed.map((s) => (
            <span key={s.share_id} className="inline-flex items-center gap-1">
              <code className="rounded bg-[var(--color-info-soft)] px-1.5 py-0.5 text-[11px]">
                {s.slug}@{s.owner_name}
              </code>
              {s.status !== "active" ? (
                <Badge variant="outline">{t(`mcpTokens.status.${s.status}`)}</Badge>
              ) : s.expires_at < row.expires_at ? (
                // The token outlives the share: after this date it no longer reads it.
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {t("mcpTokens.shared.until", { date: dayOf(s.expires_at) })}
                </span>
              ) : null}
            </span>
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

function IssueForm({
  tokens, onIssued,
}: {
  /** The tokens already issued: a person who holds a share keeps their place in it. */
  tokens: McpTokenRow[];
  onIssued: (r: McpTokenIssued, askedDays: number) => void;
}) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [userRef, setUserRef] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [globs, setGlobs] = useState("");
  const [days, setDays] = useState("30");
  const [write, setWrite] = useState(false);
  // Shares are ticked one by one and nothing is ticked to begin with: they belong
  // to no preset and no pattern.
  const [sharePicked, setSharePicked] = useState<string[]>([]);

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
  const offered = useQuery({
    queryKey: ["mcp-shares", workspace],
    queryFn: () => mcpTokensApi.shares(token!, workspace),
    enabled: !!token && !!workspace,
  });
  // Shared repositories are read-only, so the section goes with the write switch.
  const shares = offered.data ?? [];
  const showShares = !write && shares.length > 0;
  const held = useMemo(() => heldBy(tokens, userRef, workspace), [tokens, userRef, workspace]);

  const patterns = useMemo(() => [...picked, ...splitList(globs)], [picked, globs]);
  const matched = useMemo(
    () => (repos.data ?? []).filter((r) =>
      patterns.some((p) => globMatches(p, r.slug) || (r.full_name && globMatches(p, r.full_name)))),
    [repos.data, patterns],
  );
  const toggle = (slug: string) =>
    setPicked((cur) => cur.includes(slug) ? cur.filter((x) => x !== slug) : [...cur, slug]);
  const toggleShare = (id: string) =>
    setSharePicked((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);

  // Only what is ticked AND still offered goes out: a share that ended while the form
  // was open is not sent, and the server would refuse it with the same sentence anyway.
  const wantedShares = showShares
    ? sharePicked.filter((id) => shares.some((s) => s.share_id === id)) : [];

  const issue = useMutation({
    mutationFn: () => mcpTokensApi.issue(token!, {
      user_ref: userRef, workspace_id: workspace, repos: patterns,
      ...(wantedShares.length ? { shares: wantedShares } : {}),
      allow_write: write, expires_in_days: Number(days) || 30,
      profile: write ? "full" : "dev",
    }),
    onSuccess: (r) => {
      onIssued(r, Number(days) || 30);
      setPicked([]); setGlobs(""); setWrite(false); setSharePicked([]);
      qc.invalidateQueries({ queryKey: ["mcp-shares"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const ready = !!userRef && !!workspace && (patterns.length > 0 || wantedShares.length > 0);

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
                    onChange={(v) => { setWorkspace(v); setPicked([]); setSharePicked([]); }}
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

        {showShares && (
          <div className="space-y-2">
            <Label>{t("mcpTokens.shared.title")}</Label>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.shared.hint")}</p>
            <div className="max-h-56 space-y-2 overflow-y-auto rounded-md border border-[var(--color-border)] p-2">
              {shares.map((s) => {
                const ticked = sharePicked.includes(s.share_id);
                const open = canTick(s, sharePicked, held.has(s.share_id));
                return (
                  <label key={s.share_id} className="flex cursor-pointer items-start gap-2 text-sm">
                    <Checkbox className="mt-0.5" checked={ticked} disabled={!open}
                              onChange={() => toggleShare(s.share_id)} />
                    <span className="min-w-0 space-y-0.5">
                      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <code className="text-xs">{s.full_name || s.slug}</code>
                        <span className="text-xs text-[var(--color-muted-foreground)]">
                          {t("mcpTokens.shared.from", { name: s.owner_name })}
                          {" · "}
                          {t("mcpTokens.shared.until", { date: dayOf(s.expires_at) })}
                        </span>
                      </span>
                      <span className="flex flex-wrap items-center gap-1">
                        <Badge variant={s.allow_ask ? "warning" : "outline"}>
                          {s.allow_ask ? t("mcpTokens.shared.askOn") : t("mcpTokens.shared.askOff")}
                        </Badge>
                        <Badge variant="outline">
                          {t("mcpTokens.shared.places", { count: s.holders, max: s.max_people })}
                        </Badge>
                        {!hasPlace(s, held.has(s.share_id)) && (
                          <Badge variant="destructive">{t("mcpTokens.shared.full")}</Badge>
                        )}
                        {s.deny_count > 0 && (
                          <Badge variant="outline">
                            {t("mcpTokens.shared.hidden", { count: s.deny_count })}
                          </Badge>
                        )}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
            <Callout tone="info">{t("mcpTokens.shared.untrusted")}</Callout>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.shared.audit")}</p>
            {patterns.includes("*") && <Callout tone="warning">{t("mcpTokens.shared.warnStar")}</Callout>}
          </div>
        )}
        {write && shares.length > 0 && (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.shared.readOnlyOnly")}</p>
        )}

        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="mcp-days">{t("mcpTokens.days")}</Label>
            <Input id="mcp-days" type="number" min={1} value={days}
                   onChange={(e) => setDays(e.target.value)} />
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.daysHint")}</p>
          </div>
          <div className="space-y-1">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={write}
                     onChange={(e) => { setWrite(e.target.checked); if (e.target.checked) setSharePicked([]); }} />
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

/**
 * The repositories other workspaces share with a workspace, as its superadmin sees
 * them: who gave them, until when, whether questions are allowed, how many of the
 * owner's places are taken, and who holds them. Ending one here is the superadmin's
 * way to stop a share on the platform's side; the owner's own page is
 * /admin/sharing. The receiving workspace's other administrators never see this
 * tab (the page is the superadmin's) and are not told about shares at all.
 */
function SharedTab() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const me = useQuery({
    queryKey: ["workspaces-me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const workspaces = useQuery({
    queryKey: ["mcp-token-workspaces"],
    queryFn: () => adminUsersApi.workspaces(token!),
    enabled: !!token,
  });
  const [chosen, setChosen] = useState("");
  // The workspace the superadmin is in unless they pick another one.
  const workspace = chosen || me.data?.active_id || "";
  const workspaceName = (workspaces.data ?? []).find((w) => w.id === workspace)?.name ?? workspace;
  const shares = useQuery({
    queryKey: ["mcp-shares", workspace],
    queryFn: () => mcpTokensApi.shares(token!, workspace),
    enabled: !!token && !!workspace,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => sharingApi.revoke(token!, id),
    onSuccess: () => {
      toast.success(t("sharing.revoked"));
      qc.invalidateQueries({ queryKey: ["mcp-shares"] });
      qc.invalidateQueries({ queryKey: ["mcp-tokens"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>{t("mcpTokens.shared.title")}</CardTitle>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.shared.hint")}</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="max-w-xs space-y-1">
          <Label htmlFor="mcp-shared-ws">{t("mcpTokens.workspace")}</Label>
          <Select id="mcp-shared-ws" className="w-full" value={workspace} onChange={setChosen}
                  placeholder={t("mcpTokens.workspacePick")}
                  options={(workspaces.data ?? []).map((w) => ({ value: w.id, label: w.name }))} />
        </div>
        <Callout tone="info">{t("mcpTokens.shared.untrusted")}</Callout>
        {shares.isLoading && <div className="text-sm">{t("mcpTokens.loading")}</div>}
        {shares.isSuccess && (shares.data ?? []).length === 0 && (
          <div className="text-sm text-[var(--color-muted-foreground)]">{t("mcpTokens.shared.none")}</div>
        )}
        {(shares.data ?? []).map((s) => (
          <SharedRow key={s.share_id} share={s} disabled={revoke.isPending}
                     onRevoke={async () => {
                       const ok = await confirm({
                         title: t("sharing.revoke"),
                         description: t("sharing.revokeConfirm", {
                           repo: s.full_name || s.slug, workspace: workspaceName,
                         }),
                         confirmLabel: t("sharing.revoke"),
                         danger: true,
                       });
                       if (ok) revoke.mutate(s.share_id);
                     }} />
        ))}
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("mcpTokens.shared.audit")}</p>
      </CardContent>
      {dialog}
    </Card>
  );
}

function SharedRow({
  share, disabled, onRevoke,
}: { share: McpShare; disabled: boolean; onRevoke: () => void }) {
  const t = useT();
  const token = useToken();
  const [showPeople, setShowPeople] = useState(false);
  // The people who hold it, by name: asked for only when the row is opened.
  const people = useQuery({
    queryKey: ["sharing-share", share.share_id],
    queryFn: () => sharingApi.get(token!, share.share_id),
    enabled: !!token && showPeople,
  });
  return (
    <div className="rounded-md border border-[var(--color-border)] p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <code className="text-xs">{share.full_name || share.slug}</code>
        <span className="text-xs text-[var(--color-muted-foreground)]">
          {t("mcpTokens.shared.from", { name: share.owner_name })}
          {" · "}
          {t("mcpTokens.shared.until", { date: dayOf(share.expires_at) })}
        </span>
        <Badge variant={share.allow_ask ? "warning" : "outline"}>
          {share.allow_ask ? t("mcpTokens.shared.askOn") : t("mcpTokens.shared.askOff")}
        </Badge>
        <Badge variant="outline">
          {t("mcpTokens.shared.places", { count: share.holders, max: share.max_people })}
        </Badge>
        {share.deny_count > 0 && (
          <Badge variant="outline">{t("mcpTokens.shared.hidden", { count: share.deny_count })}</Badge>
        )}
        <Badge variant="success">{t("sharing.status.active")}</Badge>
        <span className="ml-auto flex gap-2">
          <Button size="sm" variant="outline" aria-expanded={showPeople}
                  onClick={() => setShowPeople((v) => !v)}>
            {t("sharing.detail.holders")}
          </Button>
          <Button size="sm" variant="outline" disabled={disabled} onClick={onRevoke}>
            {t("sharing.revoke")}
          </Button>
        </span>
      </div>
      {showPeople && (
        <div className="mt-2 space-y-1">
          {people.isLoading && <div className="text-xs">{t("mcpTokens.loading")}</div>}
          {people.isError && (
            <div className="text-xs text-[var(--color-destructive)]">{t("common.loadError")}</div>
          )}
          {people.data && people.data.people.length === 0 && (
            <div className="text-xs text-[var(--color-muted-foreground)]">{t("sharing.detail.noHolders")}</div>
          )}
          {people.data?.people.map((p) => (
            <div key={p.user_id} className="flex items-center gap-3">
              <div className="min-w-0 flex-1"><PersonCell person={p} /></div>
              <span className="text-xs text-[var(--color-muted-foreground)]">
                {t("sharing.holder.tokens")}: {p.tokens}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
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
                  <td className="pr-3">
                    {c.repos.join(", ")}
                    {(c.shares ?? []).length > 0 && (
                      <span className="ml-1 inline-flex flex-wrap items-center gap-1">
                        <Badge variant="info">{t("mcpTokens.calls.shared")}</Badge>
                        {(c.shares ?? []).join(", ")}
                      </span>
                    )}
                  </td>
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
