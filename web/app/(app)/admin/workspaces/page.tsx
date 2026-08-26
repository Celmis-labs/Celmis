"use client";

/**
 * /admin/workspaces — workspace hierarchy + membership (Stage 22).
 *
 * Hierarchy: workspace → members (people) and workspace → teams → repos.
 * Members are managed here; team→repo research access lives in /admin/access;
 * team→repo review permission lives in /admin/teams (both scoped to the
 * active workspace via the sidebar switcher).
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import Link from "next/link";
import {
  BuildingIcon, CopyIcon, PlusIcon, Trash2Icon, UsersIcon, ShieldHalfIcon, LayersIcon, MailIcon, LinkIcon,
  KeyRoundIcon,
} from "lucide-react";

import {
  workspacesApi, usersApi, invitesApi, authApi,
  type WorkspaceSummary,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { copyText } from "@/lib/copy";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

const WS_ROLES = ["owner", "admin", "member", "viewer"];
const WS_ROLE_OPTIONS = WS_ROLES.map((r) => ({ value: r, label: r }));

/**
 * Copies a short-lived password-reset link to the clipboard.
 *
 * There is no mail server in a local-first install, so an admin is the
 * delivery channel — the same "copy this link and pass it on" idiom as the
 * workspace invite below.
 */
function ResetLinkButton({ wsId, userId, email }: { wsId: string; userId: string; email: string }) {
  const token = useToken();
  const t = useT();
  const issue = useMutation({
    mutationFn: () => authApi.createWsResetLink(token!, wsId, userId),
    onSuccess: async (r) => {
      const url = `${window.location.origin}${r.url}`;
      // Same non-secure-origin trap as the invite link: writeText is absent
      // over plain HTTP, and the throw used to leave the admin with a link
      // they never received and no sign anything went wrong.
      if (await copyText(url)) {
        toast.success(t("admin.workspaces.resetLinkCopied", { email }));
      } else {
        toast.error(t("common.copyFailed"));
      }
    },
    onError: (e) => toast.error((e as Error).message),
  });
  return (
    <Button
      variant="ghost" size="icon" title={t("admin.workspaces.resetLinkTitle")}
      disabled={issue.isPending} onClick={() => issue.mutate()}
    >
      <KeyRoundIcon className="h-3.5 w-3.5" />
    </Button>
  );
}

export default function WorkspacesPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");

  const me = useQuery({
    queryKey: ["workspaces", "me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });

  const create = useMutation({
    mutationFn: () => {
      const slug = name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
      return workspacesApi.create(token!, name.trim(), slug, desc.trim());
    },
    onSuccess: () => {
      setName(""); setDesc("");
      qc.invalidateQueries({ queryKey: ["workspaces", "me"] });
      toast.success(t("admin.workspaces.workspaceCreated"));
    },
    onError: (e) => toast.error(t("admin.workspaces.error", { message: (e as Error).message })),
  });

  return (
    <PageShell width="wide">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
          <BuildingIcon className="h-6 w-6" /> {t("admin.workspaces.heading")}
        </h1>
        <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
          {t("admin.workspaces.descHierarchyLabel")}
          <b>{t("admin.workspaces.descPeopleBranch")}</b>
          {t("admin.workspaces.descMiddle1")}
          <b>{t("admin.workspaces.descTeamsBranch")}</b>
          {t("admin.workspaces.descMiddle2")}
          <Link href="/admin/teams" className="underline">/admin/teams</Link>
          {t("admin.workspaces.descMiddle3")}
          <Link href="/admin/access" className="underline">/admin/access</Link>
          {t("admin.workspaces.descTail")}
        </p>
      </div>

      <SectionTabs set="team" />

      <Card>
        <CardHeader><CardTitle>{t("admin.workspaces.createTitle")}</CardTitle></CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] items-end">
          <div>
            <Label>{t("admin.workspaces.nameLabel")}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("admin.workspaces.namePlaceholder")} />
          </div>
          <div>
            <Label>{t("admin.workspaces.descLabel")}</Label>
            <Input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t("admin.workspaces.descPlaceholder")} />
          </div>
          <Button onClick={() => create.mutate()} disabled={create.isPending || !name.trim()}>
            <PlusIcon className="h-4 w-4 mr-1" />
            {create.isPending ? "…" : t("admin.workspaces.createButton")}
          </Button>
        </CardContent>
      </Card>

      {me.isLoading && (
        <div className="text-sm text-[var(--color-muted-foreground)]">{t("admin.workspaces.loading")}</div>
      )}
      {(me.data?.workspaces ?? []).map((w) => (
        <WorkspaceCard key={w.id} ws={w} activeId={me.data?.active_id ?? null} />
      ))}
    </PageShell>
  );
}


function WorkspaceCard({ ws, activeId }: { ws: WorkspaceSummary; activeId: string | null }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [expanded, setExpanded] = useState(false);
  const [pickUser, setPickUser] = useState("");
  const [pickRole, setPickRole] = useState("member");

  const members = useQuery({
    queryKey: ["workspaces", ws.id, "members"],
    queryFn: () => workspacesApi.members(token!, ws.id),
    enabled: !!token && expanded,
  });
  const users = useQuery({
    queryKey: ["users", "directory"],
    queryFn: () => usersApi.directory(token!),
    enabled: !!token && expanded,
  });

  const emailById = (id: string) =>
    (users.data ?? []).find((u) => u.id === id)?.email ?? id;

  const addMember = useMutation({
    mutationFn: () => workspacesApi.upsertMember(token!, ws.id, pickUser, pickRole),
    onSuccess: () => {
      setPickUser("");
      qc.invalidateQueries({ queryKey: ["workspaces", ws.id, "members"] });
      toast.success(t("admin.workspaces.memberAdded"));
    },
    onError: (e) => toast.error(t("admin.workspaces.error", { message: (e as Error).message })),
  });
  const removeMember = useMutation({
    mutationFn: (uid: string) => workspacesApi.removeMember(token!, ws.id, uid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspaces", ws.id, "members"] }),
    onError: (e) => toast.error(t("admin.workspaces.error", { message: (e as Error).message })),
  });
  const del = useMutation({
    mutationFn: () => workspacesApi.remove(token!, ws.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspaces", "me"] });
      toast.success(t("admin.workspaces.workspaceDeleted"));
    },
    onError: (e) => toast.error(t("admin.workspaces.error", { message: (e as Error).message })),
  });

  const memberIds = new Set((members.data ?? []).map((m) => m.user_id));
  const candidates = (users.data ?? []).filter((u) => !memberIds.has(u.id));

  return (
    <Card>
      <CardHeader className="cursor-pointer" onClick={() => setExpanded((v) => !v)}>
        <div className="flex justify-between items-center gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              {ws.name}
              {ws.id === activeId && <Badge variant="outline">{t("admin.workspaces.activeBadge")}</Badge>}
            </CardTitle>
            <CardDescription className="mt-1">
              <code>{ws.slug}</code>{ws.role ? t("admin.workspaces.yourRole", { role: ws.role }) : ""}
            </CardDescription>
          </div>
          {ws.slug !== "default" && (
            <Button
              variant="ghost" size="icon"
              onClick={async (e) => {
                e.stopPropagation();
                const ok = await confirm({
                  title: t("admin.workspaces.deleteConfirm", { name: ws.name }),
                  confirmLabel: t("common.delete"),
                  danger: true,
                });
                if (ok) del.mutate();
              }}
              aria-label={t("admin.workspaces.deleteAriaLabel")}
            >
              <Trash2Icon className="h-4 w-4 text-red-600" />
            </Button>
          )}
        </div>
      </CardHeader>
      {expanded && (
        <CardContent className="space-y-5">
          <div>
            <h3 className="text-sm font-medium mb-2 flex items-center gap-1.5">
              <UsersIcon className="h-4 w-4" /> {t("admin.workspaces.membersHeading")}
            </h3>
            <div className="space-y-1">
              {(members.data ?? []).map((m) => (
                <div key={m.user_id} className="flex justify-between items-center text-sm">
                  <span>
                    {m.email || emailById(m.user_id)}
                    {m.name ? (
                      <span className="ml-1 text-xs text-[var(--color-muted-foreground)]">{m.name}</span>
                    ) : null}
                    {" · "}<Badge variant="outline">{m.role}</Badge>
                  </span>
                  <div className="flex items-center">
                    <ResetLinkButton wsId={ws.id} userId={m.user_id} email={m.email || emailById(m.user_id)} />
                    <Button
                      variant="ghost" size="icon"
                      onClick={async () => {
                        const ok = await confirm({
                          title: t("admin.workspaces.removeMemberConfirm", {
                            email: m.email || emailById(m.user_id),
                          }),
                          confirmLabel: t("common.remove"),
                          danger: true,
                        });
                        if (ok) removeMember.mutate(m.user_id);
                      }}
                    >
                      <Trash2Icon className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              ))}
              {members.data?.length === 0 && (
                <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.workspaces.noMembers")}</p>
              )}
            </div>
            <div className="grid grid-cols-[1fr_auto_auto] gap-2 mt-2 items-end">
              <Select
                value={pickUser} onChange={(v) => setPickUser(v)}
                options={candidates.map((u) => ({
                  value: u.id,
                  label: u.name ? `${u.name} — ${u.email}` : u.email,
                }))}
                placeholder={t("admin.workspaces.selectUser")}
              />
              <Select
                value={pickRole} onChange={(v) => setPickRole(v)}
                options={WS_ROLE_OPTIONS}
              />
              <Button onClick={() => addMember.mutate()} disabled={!pickUser || addMember.isPending}>
                {t("admin.workspaces.addButton")}
              </Button>
            </div>
          </div>

          <InviteSection wsId={ws.id} />

          <div className="flex flex-wrap gap-2 pt-1">
            <Link href="/admin/teams" className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-accent)]">
              <LayersIcon className="h-3.5 w-3.5" /> {t("admin.workspaces.teamsLink")}
            </Link>
            <Link href="/admin/access" className="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-accent)]">
              <ShieldHalfIcon className="h-3.5 w-3.5" /> {t("admin.workspaces.accessLink")}
            </Link>
          </div>
        </CardContent>
      )}
      {dialog}
    </Card>
  );
}


function InviteSection({ wsId }: { wsId: string }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [lastLink, setLastLink] = useState<string | null>(null);
  // Links only — see the mutation. Five minutes is the default because the
  // common case is reading a link out loud on a call.
  const [expiry, setExpiry] = useState<"5m" | "never">("5m");

  const invites = useQuery({
    queryKey: ["invites", wsId],
    queryFn: () => invitesApi.list(token!),
    enabled: !!token,
  });

  const create = useMutation({
    mutationFn: (mode: "email" | "link") =>
      invitesApi.create(token!, {
        email: mode === "email" ? email.trim() : null,
        role,
        // The expiry choice governs links only. An emailed invite that dies in
        // five minutes expires before it is read, and one that never dies is a
        // standing credential in a mailbox — neither is a useful offer.
        ...(mode === "link"
          ? expiry === "never"
            ? { never_expires: true }
            : { ttl_minutes: 5 }
          : { ttl_days: 14 }),
        max_uses: mode === "link" ? 25 : 1,
      }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["invites", wsId] });
      if (r.added_directly) {
        toast.success(t("invite.addedDirectly", { email: r.email ?? "" }));
      } else if (r.invite_url) {
        const url = `${window.location.origin}${r.invite_url}`;
        setLastLink(url);
        // Best effort: the link is on screen and selectable either way, so a
        // failure here is not worth an error toast at creation time.
        void copyText(url).then((ok) => {
          toast.success(t(ok ? "invite.linkCopied" : "invite.linkReady"));
        });
      }
      setEmail("");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => invitesApi.revoke(token!, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["invites", wsId] }),
    onError: (e) => toast.error(t("admin.workspaces.error", { message: (e as Error).message })),
  });

  const active = (invites.data ?? []).filter((i) => !i.revoked);

  return (
    <div>
      <h3 className="text-sm font-medium mb-2 flex items-center gap-1.5">
        <MailIcon className="h-4 w-4" /> {t("invite.sectionTitle")}
      </h3>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto_auto_auto] sm:items-end">
        <Input value={email} placeholder={t("invite.emailPlaceholder")}
               onChange={(e) => setEmail(e.target.value)} />
        <Select
          value={role} onChange={(v) => setRole(v)}
          options={WS_ROLE_OPTIONS}
        />
        <Button size="sm" disabled={!email.trim() || create.isPending}
                onClick={() => create.mutate("email")}>
          {t("invite.byEmail")}
        </Button>
        <Button size="sm" variant="outline" disabled={create.isPending}
                onClick={() => create.mutate("link")}>
          <LinkIcon className="h-3.5 w-3.5 mr-1" /> {t("invite.byLink")}
        </Button>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Label htmlFor={`invite-expiry-${wsId}`} className="text-xs">
          {t("invite.expiryLabel")}
        </Label>
        <Select
          id={`invite-expiry-${wsId}`}
          value={expiry}
          onChange={(v) => setExpiry(v as "5m" | "never")}
          options={[
            { value: "5m", label: t("invite.expiry5m") },
            { value: "never", label: t("invite.expiryNever") },
          ]}
        />
      </div>

      {lastLink && (
        <div className="mt-2 flex items-center gap-2">
          <p className="min-w-0 flex-1 break-all rounded border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-2 py-1.5 font-mono text-[11px]">
            {lastLink}
          </p>
          <Button size="sm" variant="ghost" title={t("invite.copyLink")} onClick={() => {
            void copyText(lastLink).then((ok) => {
              // Never claim success blind: on a non-secure origin both paths
              // can refuse, and a green toast over an empty clipboard is worse
              // than no toast at all — the link box above stays selectable.
              if (ok) toast.success(t("invite.linkCopied"));
              else toast.error(t("common.copyFailed"));
            });
          }}>
            <CopyIcon className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      {active.length > 0 && (
        <div className="mt-2 space-y-1">
          {active.map((i) => (
            <div key={i.id} className="flex items-center justify-between text-xs">
              <span>
                {i.email ?? t("invite.openLink")} · <Badge variant="outline">{i.role}</Badge>
                <span className="ml-1 text-[10px] text-[var(--color-muted-foreground)]">
                  {i.used_count}/{i.max_uses}
                </span>
              </span>
              <Button
                variant="ghost" size="icon"
                onClick={async () => {
                  const ok = await confirm({
                    title: t("admin.workspaces.revokeInviteConfirm"),
                    description: i.email ?? undefined,
                    confirmLabel: t("common.remove"),
                    danger: true,
                  });
                  if (ok) revoke.mutate(i.id);
                }}
              >
                <Trash2Icon className="h-3.5 w-3.5" />
              </Button>
            </div>
          ))}
        </div>
      )}
      {dialog}
    </div>
  );
}
