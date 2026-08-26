"use client";

/**
 * /admin/gdpr — data export + right-to-erasure (Stage 21 parity).
 *
 * Exposes GET /api/gdpr/export/{user_id} and DELETE /api/gdpr/user/{user_id}
 * which previously had no UI.
 *
 * Stays GLOBAL admin, and the refusal now says why. Both endpoints are keyed
 * on a USER, not on a workspace, and a user belongs to as many workspaces as
 * they please: the export walks their chats, runs, credentials and
 * memberships with no workspace filter, and the erasure deactivates the
 * account and revokes every token everywhere. "Export/erasure of a
 * workspace's own data" would be a fair rule for a workspace owner — but
 * that is not what these two do, and handing them over would let one
 * tenant's owner read and switch off a person another tenant depends on.
 * Reasoning in src/api/routers/gdpr.py.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import { DatabaseIcon, DownloadIcon, Trash2Icon } from "lucide-react";

import { gdprApi, usersApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

export default function GdprPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { data: session } = useSession();
  const { confirm, dialog } = useConfirm();
  const [userId, setUserId] = useState("");
  const [search, setSearch] = useState("");
  const [preview, setPreview] = useState<string>("");

  const users = useQuery({
    queryKey: ["users", "directory", "gdpr"],
    queryFn: () => usersApi.list(token!, true),
    // The directory is global-admin-only. Asking for it behind a card that
    // already says "global admin required" only buys a 403 in the console.
    enabled: !!token && Boolean(session?.isAdmin),
  });

  const doExport = useMutation({
    mutationFn: () => gdprApi.exportData(token!, userId),
    onSuccess: (data) => {
      const json = JSON.stringify(data, null, 2);
      setPreview(json);
      const blob = new Blob([json], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `gdpr-export-${userId}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(t("admin.gdpr.exportSuccess"));
    },
    onError: (e) =>
      toast.error(t("admin.gdpr.error", { message: (e as Error).message })),
  });

  const doErase = useMutation({
    mutationFn: () => gdprApi.erase(token!, userId),
    onSuccess: () => {
      toast.success(t("admin.gdpr.eraseSuccess"));
      qc.invalidateQueries({ queryKey: ["users", "directory", "gdpr"] });
      setUserId("");
    },
    onError: (e) =>
      toast.error(t("admin.gdpr.error", { message: (e as Error).message })),
  });

  const needle = search.trim().toLowerCase();
  const filtered = (users.data ?? []).filter(
    (u) => !needle || `${u.name}${u.email}`.toLowerCase().includes(needle),
  );

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<DatabaseIcon className="h-6 w-6" />}
        title={t("admin.gdpr.title")}
        description={t("admin.gdpr.description")}
        tabs={<SectionTabs set="admin" />}
      />

      {session && !session.isAdmin ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-[var(--color-muted-foreground)] space-y-2">
            <p>{t("admin.gdpr.adminRequired")}</p>
            <p className="text-xs">{t("admin.gdpr.adminRequiredWhy")}</p>
          </CardContent>
        </Card>
      ) : (
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.gdpr.selectUserTitle")}</CardTitle>
          <CardDescription>{t("admin.gdpr.selectUserDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {users.isLoading && (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.gdpr.usersLoading")}</p>
          )}
          {users.isError && (
            <p className="text-sm text-red-600">
              {t("admin.gdpr.usersLoadError", { message: (users.error as Error).message })}
            </p>
          )}
          <div>
            <Label>{t("admin.gdpr.userLabel")}</Label>
            <Input
              className="mb-2"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("admin.gdpr.searchPlaceholder")}
            />
            <Select
              className="w-full"
              value={userId}
              onChange={(v) => setUserId(v)}
              placeholder={t("admin.gdpr.selectPlaceholder")}
              options={filtered.map((u) => ({
                value: u.id,
                label: `${u.name ? u.name + " — " : ""}${u.email}${u.is_admin ? t("admin.gdpr.optionAdmin") : ""}${!u.is_active ? t("admin.gdpr.optionInactive") : ""}`,
              }))}
            />
            {!users.isLoading && !users.isError && filtered.length === 0 && (
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">{t("admin.gdpr.noMatches")}</p>
            )}
          </div>
          <div className="flex gap-2">
            <Button onClick={() => doExport.mutate()} disabled={!userId || doExport.isPending}>
              <DownloadIcon className="h-4 w-4 mr-1" />
              {doExport.isPending ? "…" : t("admin.gdpr.exportButton")}
            </Button>
            <Button
              variant="destructive"
              onClick={async () => {
                const ok = await confirm({
                  title: t("admin.gdpr.eraseConfirm"),
                  danger: true,
                });
                if (ok) doErase.mutate();
              }}
              disabled={!userId || doErase.isPending}
            >
              <Trash2Icon className="h-4 w-4 mr-1" />
              {doErase.isPending ? "…" : t("admin.gdpr.eraseButton")}
            </Button>
          </div>
        </CardContent>
      </Card>
      )}

      {preview && (
        <Card>
          <CardHeader><CardTitle className="text-base">{t("admin.gdpr.lastExportTitle")}</CardTitle></CardHeader>
          <CardContent>
            <pre className="max-h-96 overflow-auto rounded bg-[var(--color-muted)] p-3 text-[11px] font-mono">
              {preview}
            </pre>
          </CardContent>
        </Card>
      )}
      {dialog}
    </PageShell>
  );
}
