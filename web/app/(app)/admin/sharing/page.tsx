"use client";

/**
 * /admin/sharing — what this workspace lets another workspace read.
 *
 * AGPL. For the owner's administrators: the shares this workspace has made, who
 * holds a token for each, who read it, and the ways to make it smaller or end it
 * (narrow, block a person, revoke). Those are free in every build, because a build
 * that could not end a share would be less safe, not more.
 *
 * Making a share and giving more of one is the licensed feature `sharing`
 * (web/ee/sharing, LICENSE_EE; `POST /api/sharing-grants`). The dialogs are
 * imported statically, as the analytics dashboards are, and drawn only when
 * `/api/capabilities` says `repo_sharing_grant` is available. In a build without
 * the licence the page says so, once, and everything else keeps working.
 *
 * Nobody in the receiving workspace gets access from a share by itself: their
 * superadmin names it on a person's developer token (/admin/mcp-tokens).
 */

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon, ShieldOffIcon, Share2Icon } from "lucide-react";

import { CreateShareDialog, ExtendShareDialog } from "@/ee/sharing/create-share-dialog";
import { sharingApi, type ShareDetail, type ShareSummary } from "@/lib/api";
import { dayOf, minuteOf, revokeReasonKey } from "@/lib/sharing";
import { useT } from "@/lib/i18n";
import { featureOff, useCapabilities } from "@/lib/use-capabilities";
import { useToken } from "@/lib/use-token";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { ShareDetailPanel, ShareStatusBadge } from "@/components/sharing/share-detail";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

export default function SharingPage() {
  const t = useT();
  // Owner or admin of the active workspace (`require_workspace_admin` on the list):
  // anybody else is told so instead of being handed a page that is one 403.
  // `undefined` = the membership is still loading.
  const canManage = useCanManageWorkspace();

  if (canManage === undefined || canManage === false) {
    return (
      <PageShell width="wide">
        <PageHeader
          icon={<Share2Icon className="h-6 w-6" />}
          title={t("sharing.title")}
          description={t("sharing.intro")}
          tabs={<SectionTabs set="team" />}
        />
        {canManage === undefined ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <Card>
            <CardContent>
              <EmptyState
                icon={ShieldOffIcon}
                title={t("sharing.forbiddenTitle")}
                description={t("sharing.forbiddenDesc")}
              />
            </CardContent>
          </Card>
        )}
      </PageShell>
    );
  }
  return <SharingView />;
}

function SharingView() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const caps = useCapabilities();
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [extending, setExtending] = useState<ShareDetail | null>(null);

  // The licensed routes are drawn only when the document says they are mounted;
  // the note about the licence only when it says they are not. No document at all
  // draws neither: a button that may 404 is not offered, a note that may be wrong
  // is not written.
  const canGrant = caps.data?.features?.repo_sharing_grant?.available === true;
  const needsLicence = featureOff(caps.data, "repo_sharing_grant");

  const list = useQuery({
    queryKey: ["sharing-shares"],
    queryFn: () => sharingApi.list(token!),
    enabled: !!token,
  });
  const shares = list.data ?? [];

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<Share2Icon className="h-6 w-6" />}
        title={t("sharing.title")}
        description={t("sharing.intro")}
        tabs={<SectionTabs set="team" />}
        actions={canGrant ? (
          <Button size="sm" onClick={() => setCreating(true)}>
            <PlusIcon className="h-4 w-4" /> {t("sharing.create")}
          </Button>
        ) : undefined}
      />

      {needsLicence && <Callout tone="info">{t("sharing.licenceNeeded")}</Callout>}

      <Card>
        <CardHeader className="pb-3"><CardTitle>{t("sharing.listTitle")}</CardTitle></CardHeader>
        <CardContent>
          {list.isLoading && <Skeleton className="h-24 w-full" />}
          {list.isError && (
            <Callout tone="danger">
              {t("common.loadError")}
              <Button size="sm" variant="ghost" className="ml-2" onClick={() => list.refetch()}>
                {t("common.retry")}
              </Button>
            </Callout>
          )}
          {list.isSuccess && shares.length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("sharing.empty")}</p>
          )}
          {shares.length > 0 && (
            <SharesTable shares={shares} selected={selected}
                         onSelect={(id) => setSelected(id === selected ? null : id)} />
          )}
        </CardContent>
      </Card>

      {selected && (
        <ShareDetailPanel
          key={selected}
          id={selected}
          canGrant={canGrant}
          onExtend={setExtending}
          onClose={() => setSelected(null)}
        />
      )}

      {canGrant && (
        <CreateShareDialog
          open={creating}
          onOpenChange={setCreating}
          onDone={(share) => setSelected(share.share_id)}
        />
      )}
      {canGrant && extending && (
        <ExtendShareDialog
          share={extending}
          open
          onOpenChange={(open) => { if (!open) setExtending(null); }}
          onDone={(updated) => {
            // The open panel shows the share as it was before it was extended.
            qc.setQueryData(["sharing-share", updated.share_id], updated);
            setExtending(null);
          }}
        />
      )}
    </PageShell>
  );
}

function SharesTable({
  shares, selected, onSelect,
}: { shares: ShareSummary[]; selected: string | null; onSelect: (id: string) => void }) {
  const t = useT();
  const reasonOf = (s: ShareSummary) => (s.status === "revoked" ? revokeReasonKey(s.revoke_reason) : null);
  const details = (s: ShareSummary) => (
    <Button size="sm" variant={s.share_id === selected ? "default" : "outline"}
            aria-pressed={s.share_id === selected} onClick={() => onSelect(s.share_id)}>
      {t("sharing.details")}
    </Button>
  );
  const askBadge = (s: ShareSummary) => (
    <Badge variant={s.allow_ask ? "warning" : "outline"}>
      {s.allow_ask ? t("sharing.askOn") : t("sharing.askOff")}
    </Badge>
  );
  return (
    <>
      <div className="hidden md:block">
        <Table>
          <THead>
            <TR>
              <TH>{t("sharing.col.repo")}</TH>
              <TH>{t("sharing.col.workspace")}</TH>
              <TH>{t("sharing.col.expires")}</TH>
              <TH>{t("sharing.col.people")}</TH>
              <TH>{t("sharing.col.ask")}</TH>
              <TH>{t("sharing.col.deny")}</TH>
              <TH>{t("sharing.col.status")}</TH>
              <TH>{t("sharing.col.lastUse")}</TH>
              <TH />
            </TR>
          </THead>
          <TBody>
            {shares.map((s) => {
              const reason = reasonOf(s);
              return (
                <TR key={s.share_id} className={s.share_id === selected ? "bg-[var(--color-accent)]/60" : undefined}>
                  <TD className="max-w-[16rem]">
                    <code className="break-all text-xs">{s.full_name || s.slug}</code>
                  </TD>
                  <TD>{s.grantee_name || s.grantee_workspace_id}</TD>
                  <TD className="whitespace-nowrap">{dayOf(s.expires_at)}</TD>
                  <TD className="whitespace-nowrap">
                    {t("sharing.people", { count: s.holders, max: s.max_people })}
                  </TD>
                  <TD>{askBadge(s)}</TD>
                  <TD>{s.deny_count}</TD>
                  <TD>
                    <ShareStatusBadge status={s.status} />
                    {reason && (
                      <div className="mt-1 text-xs text-[var(--color-muted-foreground)]">{t(reason)}</div>
                    )}
                  </TD>
                  <TD className="whitespace-nowrap text-xs">
                    {s.last_used_at ? minuteOf(s.last_used_at) : t("sharing.neverRead")}
                  </TD>
                  <TD className="text-right">{details(s)}</TD>
                </TR>
              );
            })}
          </TBody>
        </Table>
      </div>

      {/* On a phone each share is a card: nine columns do not fit. */}
      <ul className="space-y-2 md:hidden">
        {shares.map((s) => {
          const reason = reasonOf(s);
          return (
            <li key={s.share_id}
                className={"space-y-2 rounded-md border border-[var(--color-border)] p-3 text-sm "
                  + (s.share_id === selected ? "bg-[var(--color-accent)]/60" : "")}>
              <div className="flex items-start justify-between gap-2">
                <code className="min-w-0 break-all text-xs">{s.full_name || s.slug}</code>
                <ShareStatusBadge status={s.status} />
              </div>
              {reason && <div className="text-xs text-[var(--color-muted-foreground)]">{t(reason)}</div>}
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.workspace")}</dt>
                <dd className="break-words">{s.grantee_name || s.grantee_workspace_id}</dd>
                <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.expires")}</dt>
                <dd>{dayOf(s.expires_at)}</dd>
                <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.people")}</dt>
                <dd>{t("sharing.people", { count: s.holders, max: s.max_people })}</dd>
                <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.ask")}</dt>
                <dd>{askBadge(s)}</dd>
                <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.deny")}</dt>
                <dd>{s.deny_count}</dd>
                <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.lastUse")}</dt>
                <dd>{s.last_used_at ? minuteOf(s.last_used_at) : t("sharing.neverRead")}</dd>
              </dl>
              <div>{details(s)}</div>
            </li>
          );
        })}
      </ul>
    </>
  );
}
