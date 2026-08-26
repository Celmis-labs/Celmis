"use client";

/**
 * /admin/audit — the LLM audit trail.
 *
 * Reads the append-only JSONL through /api/audit with filters, paging, and a
 * CSV export carrying the same filters.
 *
 * This page used to refuse everyone who was not a global admin, and that
 * refusal was right at the time: `AuditRecord` carried mode/model/operation/
 * repo and nothing that named a tenant, so one workspace's log WAS every
 * workspace's log. Opening it to an owner would have shown them another
 * tenant's repository names, models, and file lists. The fix was upstream
 * rather than here — `AuditRecord` learned which workspace it belongs to, and
 * /api/audit now scopes the read the way the job queue does:
 *
 *   global admin      every record, every tenant
 *   everyone else     records whose workspace_id is their active workspace
 *
 * Records written before the field existed have no workspace, and neither
 * does work no tenant asked for (the embedder is built from settings alone).
 * Those stay global-admin-only, and this page SAYS so rather than leaving a
 * silent omission — a log that quietly drops rows is worse than one that
 * refuses, because it looks complete. The server counts them
 * (`hidden_unattributed`) so the line can name a number.
 *
 * On the gate: the API's bar is `get_current_user` — any member of the
 * workspace, on the grounds that every endpoint here is a read of the
 * caller's own activity. This page is deliberately one notch stricter and
 * draws for owner/admin only, because an audit row says which files went to
 * which model and what the call cost: the workspace's books rather than its
 * status page, which is why the write-side hook gates it. If the API's bar is
 * the final word, the `canManage === false` block below is the one deletion
 * that aligns them — nothing else on the page assumes the stricter rule.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import { DownloadIcon, ScrollTextIcon } from "lucide-react";

import { auditApi, downloadWithAuth, type AuditRecord } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/** The API now returns the tenant on every record it can name. Widened here
 *  rather than in lib/api.ts, which is being edited elsewhere; the field is
 *  optional so an older backend still type-checks, and `null` is a real
 *  answer — "this call belongs to no workspace" — not a missing one. */
type ScopedAuditRecord = AuditRecord & { workspace_id?: string | null };

export default function AuditPage() {
  const t = useT();
  const token = useToken();
  const { data: session } = useSession();
  // Every hook runs unconditionally, above every branch. A hook below an
  // early return has taken this app down before (React #310).
  const canManage = useCanManageWorkspace();
  const isGlobalAdmin = Boolean(session?.isAdmin);
  const [mode, setMode] = useState("");
  const [operation, setOperation] = useState("");
  const [repo, setRepo] = useState("");
  const [fromTs, setFromTs] = useState("");
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState(false);
  const limit = 50;

  const q = useQuery({
    queryKey: ["audit", mode, operation, repo, fromTs, offset],
    queryFn: () => auditApi.list(token!, {
      mode: mode || undefined,
      operation: operation || undefined,
      repo: repo || undefined,
      from_ts: fromTs || undefined,
      limit, offset,
    }) as Promise<{
      records: ScopedAuditRecord[]; count: number; offset: number; limit: number;
      /** The server's own answer to "is this list the whole installation?",
       *  which beats inferring it from the session: the read is scoped by the
       *  caller's ACTIVE workspace, and a global admin looking at one is
       *  still reading a scoped list. */
      scoped?: boolean;
    }>,
    enabled: !!token,
  });
  const facets = useQuery({
    queryKey: ["audit", "facets"],
    queryFn: () => auditApi.facets(token!),
    enabled: !!token,
  });
  const stats = useQuery({
    queryKey: ["audit", "stats", mode, operation, repo, fromTs],
    queryFn: () => auditApi.stats(token!, {
      mode: mode || undefined,
      operation: operation || undefined,
      repo: repo || undefined,
      from_ts: fromTs || undefined,
    }) as Promise<{
      total_calls: number; input_tokens: number; output_tokens: number;
      errors: number;
      /** How many records matched the filter but carry no tenant. The server
       *  counts them precisely so this page can name a number instead of
       *  gesturing at "some records"; 0 for a global admin, who is hiding
       *  from nobody. */
      hidden_unattributed?: number;
      scoped?: boolean;
    }>,
    enabled: !!token,
  });

  const facetOptions = (values: string[]) => [
    { value: "", label: t("admin.audit.allOption") },
    ...values.map((v) => ({ value: v, label: v })),
  ];

  /** CSV, through fetch rather than a plain <a href>.
   *
   *  The link had no way to authenticate: /api/audit/export sits behind
   *  `get_current_user`, which reads the Authorization header and nothing
   *  else, so a browser navigation downloaded a file containing
   *  {"detail":"Missing or invalid Authorization header"} under a button
   *  labelled CSV. Now that the export is scoped it also needs the
   *  X-Workspace hint, which a navigation cannot carry either when API_BASE
   *  is a different origin than the app — without it the request resolves to
   *  the account's best-ranked workspace and a member reading another one
   *  exports somebody else's audit. `downloadWithAuth` sends both. */
  const exportCsv = async () => {
    setExporting(true);
    try {
      await downloadWithAuth(
        auditApi.exportUrl({
          ...(mode && { mode }), ...(operation && { operation }),
          ...(repo && { repo }), ...(fromTs && { from_ts: fromTs }),
        }),
        "audit_export.csv",
        token,
      );
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setExporting(false);
    }
  };

  // Whether this list is scoped, and by how much it falls short of the whole
  // installation. Both come from the server; the session is only the fallback
  // for the moment before either query has answered.
  const scoped = q.data?.scoped ?? stats.data?.scoped ?? !isGlobalAdmin;
  const hiddenCount = stats.data?.hidden_unattributed ?? 0;

  // `undefined` means the membership is still loading — draw the page rather
  // than flash a refusal at somebody who turns out to be allowed. The API
  // decides for real; this only chooses what to paint.
  if (canManage === false) {
    return (
      <div className="mx-auto w-full p-8 max-w-6xl space-y-6">
        <Card>
          <CardContent className="py-8 text-center text-sm text-[var(--color-muted-foreground)] space-y-2">
            <p>{t("admin.audit.ownerOnly")}</p>
            <p className="text-xs">{t("admin.audit.ownerOnlyWhy")}</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<ScrollTextIcon className="h-6 w-6" />}
        title={t("admin.audit.title")}
        description={t("admin.audit.description")}
        tabs={<SectionTabs set="monitoring" />}
      />

      {/* Said before the numbers, not after them: the totals below are this
          workspace's totals, and a reader who takes them for the
          installation's has been misled by a page that looked complete.
          `scoped` is the server's own answer rather than a guess from the
          session — a global admin reading one workspace is still reading a
          scoped list — and `hidden_unattributed` turns "some records are not
          shown" into a number. */}
      {scoped && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("admin.audit.scopeNotice")}
          {hiddenCount > 0 && <> {t("admin.audit.hiddenCount", { count: hiddenCount })}</>}
        </p>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle>{t("admin.audit.filtersTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2 sm:grid-cols-5 items-end">
          <div>
            <Label>{t("admin.audit.modeLabel")}</Label>
            <Select
              className="w-full"
              value={mode}
              onChange={(v) => { setMode(v); setOffset(0); }}
              options={facetOptions(facets.data?.modes ?? [])}
            />
          </div>
          <div>
            <Label>{t("admin.audit.operationLabel")}</Label>
            <Select
              className="w-full"
              value={operation}
              onChange={(v) => { setOperation(v); setOffset(0); }}
              options={facetOptions(facets.data?.operations ?? [])}
            />
          </div>
          <div>
            <Label>{t("admin.audit.repoLabel")}</Label>
            <Select
              className="w-full"
              value={repo}
              onChange={(v) => { setRepo(v); setOffset(0); }}
              options={facetOptions(facets.data?.repos ?? [])}
            />
          </div>
          <div>
            <Label>{t("admin.audit.fromLabel")}</Label>
            <Input value={fromTs} onChange={(e) => { setFromTs(e.target.value); setOffset(0); }}
                   placeholder="2026-07-01" />
          </div>
          <Button variant="outline" className="w-full" onClick={exportCsv}
                  disabled={!token || exporting}>
            <DownloadIcon className="h-4 w-4 mr-1" /> CSV
          </Button>
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label={t("admin.audit.statsTotal")} value={String(stats.data?.total_calls ?? "—")} />
        <Stat label={t("admin.audit.statsTokensIn")} value={String(stats.data?.input_tokens ?? "—")} />
        <Stat label={t("admin.audit.statsTokensOut")} value={String(stats.data?.output_tokens ?? "—")} />
        <Stat
          label={t("admin.audit.statsErrors")}
          value={String(stats.data?.errors ?? "—")}
          accent={(stats.data?.errors ?? 0) > 0}
        />
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardDescription>
            {q.data
              ? t("admin.audit.recordsCount", { count: q.data.count, offset })
              : q.isError
                ? (
                  <span className="text-red-600">
                    {t("admin.audit.error")}: {(q.error as Error).message}
                  </span>
                )
                : t("admin.audit.loading")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-1">
          <Table>
            <THead>
              <TR className="hover:bg-transparent">
                <TH>{t("admin.audit.colTime")}</TH>
                <TH>{t("admin.audit.colMode")}</TH>
                <TH>{t("admin.audit.colOperation")}</TH>
                <TH>{t("admin.audit.colModel")}</TH>
                <TH>{t("admin.audit.colRepo")}</TH>
                {/* Only a global admin is shown records from more than one
                    tenant, so only a global admin needs the column that says
                    which one a record came from. */}
                {isGlobalAdmin && <TH>{t("admin.audit.colWorkspace")}</TH>}
                <TH className="text-right">{t("admin.audit.colTokens")}</TH>
                <TH className="text-right">{t("admin.audit.colDuration")}</TH>
              </TR>
            </THead>
            <TBody>
              {(q.data?.records ?? []).map((r, i) => (
                <TR key={`${r.request_id}-${i}`}>
                  <TD className="whitespace-nowrap text-[var(--color-muted-foreground)]">
                    {formatDateTime(r.timestamp)}
                  </TD>
                  <TD><Badge variant="outline">{r.mode}</Badge></TD>
                  <TD>
                    {r.operation}
                    {r.error && <div className="mt-0.5 text-red-600">{r.error}</div>}
                  </TD>
                  <TD className="text-[var(--color-muted-foreground)]">{r.model}</TD>
                  <TD>{r.repo ? <code>{r.repo}</code> : "—"}</TD>
                  {isGlobalAdmin && (
                    <TD>
                      <Badge variant="outline" className="font-mono text-[9px]"
                             title={t("admin.audit.workspaceTitle")}>
                        {r.workspace_id || t("admin.audit.noWorkspace")}
                      </Badge>
                    </TD>
                  )}
                  <TD className="whitespace-nowrap text-right font-mono">
                    {r.input_tokens_estimated}→{r.output_tokens_estimated}
                  </TD>
                  <TD className="whitespace-nowrap text-right font-mono">
                    {r.duration_ms}ms
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
          <div className="flex justify-between pt-2">
            <Button variant="ghost" size="sm" disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - limit))}>
              {t("admin.audit.prev")}
            </Button>
            <Button variant="ghost" size="sm"
                    disabled={(q.data?.count ?? 0) < limit}
                    onClick={() => setOffset(offset + limit)}>
              {t("admin.audit.next")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </PageShell>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] p-3">
      <div className="text-xs text-[var(--color-muted-foreground)]">{label}</div>
      <div className={`text-2xl font-semibold ${accent ? "text-red-500" : ""}`}>{value}</div>
    </div>
  );
}
