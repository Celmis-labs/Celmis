"use client";

/**
 * /admin/access-requests — the superadmin decides who gets into which team
 * workspace. Superadmin only: not workspace admins, not other global admins.
 *
 * Approve with one or more (workspace, role) pairs — any role, the superadmin
 * grants anything — applied all or nothing by the API through the one
 * membership writer, with an audit row each (via="access_request"). Reject
 * with a reason the requester will read. Decided requests stay listed as
 * history. See src/api/routers/access_requests.py.
 */

import { useState } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckIcon, InboxIcon, PlusIcon, Trash2Icon, XIcon } from "lucide-react";

import {
  accessRequestsApi, adminUsersApi,
  type AccessRequestStatus, type AdminAccessRequest, type AdminWorkspace,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { roleLabel, roleOptions } from "@/lib/roles";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

type Filter = AccessRequestStatus | "all";
const FILTERS: Filter[] = ["all", "pending", "approved", "rejected", "cancelled"];

/** A personal workspace (slug `u-{user id}`): the API refuses to grant one,
 *  so the picker does not offer them. */
const PERSONAL_SLUG = /^u-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|master-admin)$/i;

export default function AdminAccessRequestsPage() {
  const t = useT();
  const token = useToken();
  const { data: session } = useSession();
  const [filter, setFilter] = useState<Filter>("all");
  const allowed = Boolean(session?.isSuperadmin);

  const list = useQuery({
    queryKey: ["admin-access-requests", filter],
    queryFn: () => accessRequestsApi.list(token!, filter),
    enabled: !!token && allowed,
  });
  const workspaces = useQuery({
    queryKey: ["admin-workspaces"],
    queryFn: () => adminUsersApi.workspaces(token!),
    enabled: !!token && allowed,
  });

  if (session && !allowed) {
    return (
      <div className="mx-auto w-full max-w-4xl p-8">
        <Callout tone="warning">{t("admin.accessRequests.superadminOnly")}</Callout>
      </div>
    );
  }

  const teamWorkspaces = (workspaces.data ?? []).filter((w) => !PERSONAL_SLUG.test(w.slug));

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<InboxIcon className="h-6 w-6" />}
        title={t("admin.accessRequests.title")}
        description={t("admin.accessRequests.description")}
        tabs={<SectionTabs set="admin" />}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="ar-filter" className="text-xs">{t("admin.accessRequests.filter")}</Label>
            <Select
              id="ar-filter"
              className="h-8 w-auto text-xs"
              value={filter}
              onChange={(v) => setFilter(v as Filter)}
              options={FILTERS.map((f) => ({
                value: f,
                label: f === "all" ? t("admin.accessRequests.filterAll") : t(`accessRequest.status.${f}`),
              }))}
            />
          </div>
        }
      />

      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("admin.accessRequests.usersHint")}{" "}
        <Link href="/admin/users?filter=no-team-access" className="underline">
          {t("admin.users.noTeamAccess")}
        </Link>
      </p>

      {list.isLoading && (
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("common.loading")}</p>
      )}
      {list.isError && <Callout tone="danger">{(list.error as Error).message}</Callout>}
      {list.data?.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-[var(--color-muted-foreground)]">
            {t("admin.accessRequests.empty")}
          </CardContent>
        </Card>
      )}

      <div className="space-y-3">
        {(list.data ?? []).map((r) => (
          <RequestCard key={r.id} req={r} workspaces={teamWorkspaces} />
        ))}
      </div>
    </PageShell>
  );
}

function methodLabel(t: (k: string) => string, m: string): string {
  return ["password", "google", "oidc"].includes(m) ? t(`admin.accessRequests.method.${m}`) : m;
}

function RequestCard({ req, workspaces }: { req: AdminAccessRequest; workspaces: AdminWorkspace[] }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [grants, setGrants] = useState<{ workspace_id: string; role: string }[]>([
    { workspace_id: "", role: "member" },
  ]);
  const [reason, setReason] = useState("");

  const done = () => {
    void qc.invalidateQueries({ queryKey: ["admin-access-requests"], exact: false });
    void qc.invalidateQueries({ queryKey: ["admin-users"], exact: false });
  };
  const onError = (e: unknown) =>
    toast.error(t("admin.accessRequests.error", { message: (e as Error).message }));

  const approve = useMutation({
    mutationFn: () => accessRequestsApi.approve(token!, req.id, grants),
    onSuccess: () => { toast.success(t("admin.accessRequests.approved")); done(); },
    onError,
  });
  const reject = useMutation({
    mutationFn: () => accessRequestsApi.reject(token!, req.id, reason.trim()),
    onSuccess: () => { toast.success(t("admin.accessRequests.rejected")); done(); },
    onError,
  });

  const chosen = grants.map((g) => g.workspace_id);
  const complete = grants.length > 0 && chosen.every(Boolean)
    && new Set(chosen).size === chosen.length;
  const busy = approve.isPending || reject.isPending;
  const roles = roleOptions(t);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          <span className="min-w-0 break-all">{req.name || req.email}</span>
          {req.name && (
            <span className="text-xs font-normal text-[var(--color-muted-foreground)]">{req.email}</span>
          )}
          <Badge variant="outline">{t(`accessRequest.status.${req.status}`)}</Badge>
          {req.sign_in_methods.map((m) => (
            <Badge key={m} variant="outline">{methodLabel(t, m)}</Badge>
          ))}
          {!req.user_active && <Badge variant="outline">{t("admin.users.inactive")}</Badge>}
        </CardTitle>
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("accessRequest.sentAt", { when: formatDateTime(req.created_at) })}
          {req.decided_at
            ? ` · ${t("admin.accessRequests.decidedBy", { who: req.decided_by ?? "—", when: formatDateTime(req.decided_at) })}`
            : ""}
        </p>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {req.comment ? (
          <p className="whitespace-pre-wrap break-words rounded-md border border-[var(--color-border)] px-3 py-2 text-xs">
            {req.comment}
          </p>
        ) : (
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.accessRequests.noComment")}</p>
        )}

        {req.status === "approved" && (
          <ul className="space-y-1">
            {req.grants.map((g) => (
              <li key={g.workspace_id} className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium">{g.workspace_name}</span>
                <Badge variant="outline">{roleLabel(t, g.role)}</Badge>
              </li>
            ))}
          </ul>
        )}
        {req.status === "rejected" && req.decision_note && (
          <Callout tone="danger">
            <span className="font-medium">{t("accessRequest.reason")}</span>{" "}
            <span className="whitespace-pre-wrap break-words">{req.decision_note}</span>
          </Callout>
        )}

        {req.status === "pending" && (
          <div className="grid gap-4 border-t border-[var(--color-border)] pt-3 lg:grid-cols-2">
            <div className="space-y-2">
              <p className="text-xs font-medium">{t("admin.accessRequests.grantHeading")}</p>
              {grants.map((g, i) => (
                <div key={i} className="grid grid-cols-[1fr_auto_auto] items-center gap-2">
                  <Select
                    value={g.workspace_id}
                    onChange={(v) => setGrants((gs) => gs.map((x, j) => (j === i ? { ...x, workspace_id: v } : x)))}
                    options={workspaces
                      .filter((w) => w.id === g.workspace_id || !chosen.includes(w.id))
                      .map((w) => ({ value: w.id, label: `${w.name} (${w.slug})` }))}
                    placeholder={t("admin.users.pickWorkspace")}
                  />
                  <Select
                    className="w-auto"
                    value={g.role}
                    onChange={(v) => setGrants((gs) => gs.map((x, j) => (j === i ? { ...x, role: v } : x)))}
                    options={roles}
                  />
                  <Button
                    variant="ghost" size="icon"
                    aria-label={t("admin.accessRequests.removeGrant")}
                    disabled={grants.length === 1}
                    onClick={() => setGrants((gs) => gs.filter((_, j) => j !== i))}
                  >
                    <Trash2Icon className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm" variant="outline"
                  disabled={grants.length >= workspaces.length}
                  onClick={() => setGrants((gs) => [...gs, { workspace_id: "", role: "member" }])}
                >
                  <PlusIcon className="mr-1 h-3.5 w-3.5" /> {t("admin.accessRequests.addGrant")}
                </Button>
                <Button size="sm" disabled={!complete || busy || !req.user_active}
                        onClick={() => approve.mutate()}>
                  <CheckIcon className="mr-1 h-3.5 w-3.5" /> {t("admin.accessRequests.approve")}
                </Button>
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`reject-${req.id}`} className="text-xs font-medium">
                {t("admin.accessRequests.rejectHeading")}
              </Label>
              <Textarea
                id={`reject-${req.id}`}
                value={reason}
                maxLength={1000}
                onChange={(e) => setReason(e.target.value)}
                placeholder={t("admin.accessRequests.reasonPlaceholder")}
              />
              <Button size="sm" variant="outline" disabled={!reason.trim() || busy}
                      onClick={() => reject.mutate()}>
                <XIcon className="mr-1 h-3.5 w-3.5" /> {t("admin.accessRequests.reject")}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
