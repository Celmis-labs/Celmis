"use client";

/**
 * /admin/users — people and their workspace roles. Superadmin only.
 *
 * Memberships were always per workspace; this page shows them per PERSON, so
 * one account can be made admin of several workspaces and editor of several
 * others in one place. Every change goes through the same API rule and audit
 * row as the workspace members page (`change_membership`,
 * src/api/memberships.py) — nothing here is decided by the page itself.
 *
 * "No team access" lists the accounts whose only workspace is their own
 * personal one — however they sign in — newest first, so the superadmin can
 * add them somewhere without waiting for an access request.
 * `?filter=no-team-access` opens the page with it on.
 */

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PlusIcon, SearchIcon, Trash2Icon, UserCogIcon } from "lucide-react";

import { adminUsersApi, type AdminUser } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { roleOptions } from "@/lib/roles";
import { formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

export default function AdminUsersPage() {
  // useSearchParams needs a Suspense boundary above it to prerender.
  return (
    <Suspense fallback={null}>
      <AdminUsersInner />
    </Suspense>
  );
}

function methodLabel(t: (k: string) => string, m: string): string {
  return ["password", "google", "oidc"].includes(m) ? t(`admin.accessRequests.method.${m}`) : m;
}

function AdminUsersInner() {
  const t = useT();
  const token = useToken();
  const { data: session } = useSession();
  const params = useSearchParams();
  const [q, setQ] = useState("");
  const [noTeam, setNoTeam] = useState(params.get("filter") === "no-team-access");
  const [selected, setSelected] = useState<AdminUser | null>(null);

  const users = useQuery({
    queryKey: ["admin-users", q, noTeam],
    queryFn: () => adminUsersApi.search(token!, q.trim(), noTeam),
    enabled: !!token && Boolean(session?.isSuperadmin),
  });

  if (session && !session.isSuperadmin) {
    return (
      <div className="mx-auto w-full max-w-4xl p-8">
        <Callout tone="warning">{t("admin.users.superadminOnly")}</Callout>
      </div>
    );
  }

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<UserCogIcon className="h-6 w-6" />}
        title={t("admin.users.title")}
        description={t("admin.users.description")}
        tabs={<SectionTabs set="admin" />}
      />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t("admin.users.peopleHeading")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="relative">
              <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-muted-foreground)]" />
              <Input
                className="pl-8" value={q} onChange={(e) => setQ(e.target.value)}
                placeholder={t("admin.users.searchPlaceholder")}
                aria-label={t("admin.users.searchPlaceholder")}
              />
            </div>
            <label className="flex items-center gap-2 text-xs">
              <Switch checked={noTeam} onCheckedChange={setNoTeam} aria-label={t("admin.users.noTeamAccess")} />
              <span>{t("admin.users.noTeamAccess")}</span>
            </label>
            {noTeam && (
              <p className="text-[11px] text-[var(--color-muted-foreground)]">{t("admin.users.noTeamAccessHint")}</p>
            )}
            {users.isLoading && (
              <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.users.loading")}</p>
            )}
            {users.isError && (
              <Callout tone="danger">{(users.error as Error).message}</Callout>
            )}
            <ul className="divide-y divide-[var(--color-border)]">
              {(users.data ?? []).map((u) => (
                <li key={u.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(u)}
                    aria-pressed={selected?.id === u.id}
                    className={`flex w-full items-center justify-between gap-2 px-2 py-2 text-left text-sm hover:bg-[var(--color-accent)] ${selected?.id === u.id ? "bg-[var(--color-accent)]" : ""}`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{u.email}</span>
                      {u.name ? (
                        <span className="block truncate text-xs text-[var(--color-muted-foreground)]">{u.name}</span>
                      ) : null}
                      {noTeam && u.created_at ? (
                        <span className="block truncate text-[11px] text-[var(--color-muted-foreground)]">
                          {t("admin.users.joined", { when: formatDateTime(u.created_at) })}
                        </span>
                      ) : null}
                    </span>
                    <span className="flex shrink-0 items-center gap-1">
                      {noTeam && (u.sign_in_methods ?? []).map((m) => (
                        <Badge key={m} variant="outline">{methodLabel(t, m)}</Badge>
                      ))}
                      {u.is_admin && <Badge variant="outline">{t("admin.users.globalAdmin")}</Badge>}
                      {!u.is_active && <Badge variant="outline">{t("admin.users.inactive")}</Badge>}
                      <Badge variant="outline">{t("admin.users.workspacesCount", { n: String(u.memberships) })}</Badge>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {users.data?.length === 0 && (
              <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.users.noResults")}</p>
            )}
          </CardContent>
        </Card>

        {selected ? (
          <MembershipsCard key={selected.id} user={selected} />
        ) : (
          <Card>
            <CardContent className="py-10 text-center text-sm text-[var(--color-muted-foreground)]">
              {t("admin.users.pickSomeone")}
            </CardContent>
          </Card>
        )}
      </div>
    </PageShell>
  );
}

function MembershipsCard({ user }: { user: AdminUser }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [addWs, setAddWs] = useState("");
  const [addRole, setAddRole] = useState("member");

  const memberships = useQuery({
    queryKey: ["admin-users", user.id, "memberships"],
    queryFn: () => adminUsersApi.memberships(token!, user.id),
    enabled: !!token,
  });
  const workspaces = useQuery({
    queryKey: ["admin-workspaces"],
    queryFn: () => adminUsersApi.workspaces(token!),
    enabled: !!token,
  });

  const refresh = (rows: unknown) => {
    qc.setQueryData(["admin-users", user.id, "memberships"], rows);
    qc.invalidateQueries({ queryKey: ["admin-users"], exact: false });
  };
  const onError = (e: unknown) =>
    toast.error(t("admin.users.error", { message: (e as Error).message }));

  const setRole = useMutation({
    mutationFn: ({ wsId, role }: { wsId: string; role: string }) =>
      adminUsersApi.setRole(token!, user.id, wsId, role),
    onSuccess: (rows) => { refresh(rows); toast.success(t("admin.users.saved")); },
    onError,
  });
  const remove = useMutation({
    mutationFn: (wsId: string) => adminUsersApi.remove(token!, user.id, wsId),
    onSuccess: (rows) => { refresh(rows); toast.success(t("admin.users.removed")); },
    onError,
  });

  const mine = new Set((memberships.data ?? []).map((m) => m.workspace_id));
  const addable = (workspaces.data ?? []).filter((w) => !mine.has(w.id));
  const options = roleOptions(t);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{user.email}</CardTitle>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.users.membershipsHint")}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {memberships.isLoading && (
          <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.users.loading")}</p>
        )}
        <ul className="space-y-2">
          {(memberships.data ?? []).map((m) => (
            <li key={m.workspace_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="min-w-0">
                <span className="font-medium">{m.workspace_name}</span>{" "}
                <code className="text-xs text-[var(--color-muted-foreground)]">{m.workspace_slug}</code>
              </span>
              <span className="flex items-center gap-1">
                <Select
                  className="h-8 w-auto text-xs"
                  value={m.role}
                  disabled={setRole.isPending}
                  onChange={(v) => { if (v !== m.role) setRole.mutate({ wsId: m.workspace_id, role: v }); }}
                  options={options}
                />
                <Button
                  variant="ghost" size="icon"
                  aria-label={t("admin.users.removeAria", { ws: m.workspace_name })}
                  disabled={remove.isPending}
                  onClick={async () => {
                    const ok = await confirm({
                      title: t("admin.users.removeConfirm", { email: user.email, ws: m.workspace_name }),
                      confirmLabel: t("common.remove"),
                      danger: true,
                    });
                    if (ok) remove.mutate(m.workspace_id);
                  }}
                >
                  <Trash2Icon className="h-3.5 w-3.5" />
                </Button>
              </span>
            </li>
          ))}
        </ul>
        {memberships.data?.length === 0 && (
          <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.users.noMemberships")}</p>
        )}

        <div className="grid grid-cols-1 gap-2 border-t border-[var(--color-border)] pt-3 sm:grid-cols-[1fr_auto_auto] sm:items-end">
          <Select
            value={addWs} onChange={(v) => setAddWs(v)}
            options={addable.map((w) => ({ value: w.id, label: `${w.name} (${w.slug})` }))}
            placeholder={t("admin.users.pickWorkspace")}
          />
          <Select value={addRole} onChange={(v) => setAddRole(v)} options={options} />
          <Button
            disabled={!addWs || setRole.isPending || !user.is_active}
            onClick={() => setRole.mutate(
              { wsId: addWs, role: addRole },
              { onSuccess: () => setAddWs("") },
            )}
          >
            <PlusIcon className="mr-1 h-4 w-4" /> {t("admin.users.addMembership")}
          </Button>
        </div>
      </CardContent>
      {dialog}
    </Card>
  );
}
