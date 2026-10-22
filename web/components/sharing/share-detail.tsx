"use client";

/**
 * One share the active workspace gives: who holds a token for it, who is switched
 * off, what it hides, and who read it. Everything here is free in every build —
 * seeing, narrowing, blocking a person and ending a share; the licensed half (a
 * later end, more people, switching a person back on) is reached through the
 * `onExtend` callback and the `canGrant` flag the page derives from
 * `/api/capabilities`.
 *
 * The people listed belong to ANOTHER workspace. The owner sees their names and
 * emails because they are reading the owner's code, and the panel says so beside
 * the list rather than leaving it to be discovered.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { XIcon } from "lucide-react";

import {
  ApiError, sharingApi, type ShareActivity, type ShareDetail, type SharePerson, type ShareStatus,
} from "@/lib/api";
import { blockConfirmKey, daysLeft, minuteOf, refusalKey, revokeReasonKey } from "@/lib/sharing";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { NarrowDialog } from "@/components/sharing/narrow-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

/** The one place a share's status is given a colour. */
export function ShareStatusBadge({ status }: { status: ShareStatus }) {
  const t = useT();
  const variant = status === "active" ? "success" : status === "revoked" ? "destructive" : "outline";
  const label = status === "active" ? t("sharing.status.active")
    : status === "revoked" ? t("sharing.status.revoked") : t("sharing.status.expired");
  return <Badge variant={variant}>{label}</Badge>;
}

/** The person's name, or their email when there is no name, with the other as
 *  the second line. An id alone (a person the account store no longer knows) is
 *  shown as it is. */
export function PersonCell({ person }: { person: SharePerson }) {
  const label = person.name || person.email || person.user_id;
  return (
    <div className="min-w-0">
      <div className="truncate font-medium">{label}</div>
      {person.name && person.email && (
        <div className="truncate text-xs text-[var(--color-muted-foreground)]">{person.email}</div>
      )}
    </div>
  );
}

export function ShareDetailPanel({
  id, canGrant, onExtend, onClose,
}: {
  id: string;
  /** The licensed routes are mounted: a later end, more people, unblocking. */
  canGrant: boolean;
  onExtend: (share: ShareDetail) => void;
  onClose: () => void;
}) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [narrowing, setNarrowing] = useState(false);

  const detail = useQuery({
    queryKey: ["sharing-share", id],
    queryFn: () => sharingApi.get(token!, id),
    enabled: !!token,
  });
  const activity = useQuery({
    queryKey: ["sharing-activity", id],
    queryFn: () => sharingApi.activity(token!, id),
    enabled: !!token,
  });

  const changed = (updated: ShareDetail) => {
    qc.setQueryData(["sharing-share", id], updated);
    qc.invalidateQueries({ queryKey: ["sharing-shares"] });
    qc.invalidateQueries({ queryKey: ["sharing-activity", id] });
  };
  const fail = (e: Error) => {
    const key = e instanceof ApiError ? refusalKey(e.status, e.message, canGrant) : null;
    toast.error(key ? t(key) : e.message);
  };

  const revoke = useMutation({
    mutationFn: () => sharingApi.revoke(token!, id),
    onSuccess: (updated) => { toast.success(t("sharing.revoked")); changed(updated); },
    onError: fail,
  });
  const block = useMutation({
    mutationFn: (userId: string) => sharingApi.narrow(token!, id, { block: [userId] }),
    onSuccess: (updated) => { toast.success(t("sharing.blockedDone")); changed(updated); },
    onError: fail,
  });
  const unblock = useMutation({
    mutationFn: (userId: string) => sharingApi.extend(token!, id, { unblock: [userId] }),
    onSuccess: (updated) => { toast.success(t("sharing.unblockedDone")); changed(updated); },
    onError: fail,
  });

  const blockButton = (p: SharePerson) => (
    <Button size="sm" variant="outline" disabled={block.isPending}
            onClick={async () => {
              const ok = await confirm({
                title: t("sharing.block"),
                description: t(blockConfirmKey(canGrant), { name: p.name || p.email || p.user_id }),
                confirmLabel: t("sharing.block"),
                danger: true,
              });
              if (ok) block.mutate(p.user_id);
            }}>
      {t("sharing.block")}
    </Button>
  );

  const share = detail.data;
  const live = share?.status === "active";
  const repo = share ? (share.full_name || share.slug) : "";
  const workspace = share ? (share.grantee_name || share.grantee_workspace_id) : "";

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start gap-2">
          <CardTitle className="min-w-0 flex-1 break-words">
            {share ? t("sharing.detail.heading", { repo, workspace }) : t("common.loading")}
          </CardTitle>
          <Button size="sm" variant="ghost" onClick={onClose} aria-label={t("sharing.detail.close")}>
            <XIcon className="h-4 w-4" />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {detail.isLoading && <Skeleton className="h-32 w-full" />}
        {detail.isError && (
          <Callout tone="danger">
            {t("common.loadError")}
            <Button size="sm" variant="ghost" className="ml-2" onClick={() => detail.refetch()}>
              {t("common.retry")}
            </Button>
          </Callout>
        )}

        {share && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
              <ShareStatusBadge status={share.status} />
              <span>{t("sharing.detail.ends", { date: share.expires_at.slice(0, 10) })}</span>
              {live && <span>{t("sharing.detail.daysLeft", { count: daysLeft(share.expires_at) })}</span>}
              {share.status === "revoked" && revokeReasonKey(share.revoke_reason) && (
                <span>{t(revokeReasonKey(share.revoke_reason)!)}</span>
              )}
            </div>

            {live && (
              <div className="flex flex-wrap gap-2">
                {canGrant && (
                  <Button size="sm" variant="outline" onClick={() => onExtend(share)}>
                    {t("sharing.extend")}
                  </Button>
                )}
                <Button size="sm" variant="outline" onClick={() => setNarrowing(true)}>
                  {t("sharing.narrow")}
                </Button>
                <Button size="sm" variant="outline" disabled={revoke.isPending}
                        onClick={async () => {
                          const ok = await confirm({
                            title: t("sharing.revoke"),
                            description: t("sharing.revokeConfirm", { repo, workspace }),
                            confirmLabel: t("sharing.revoke"),
                            danger: true,
                          });
                          if (ok) revoke.mutate();
                        }}>
                  {t("sharing.revoke")}
                </Button>
              </div>
            )}

            <Callout tone="warning">{t("sharing.detail.otherWorkspace", { workspace })}</Callout>

            <section className="space-y-2">
              <h3 className="text-sm font-semibold">{t("sharing.detail.holders")}</h3>
              {share.people.length === 0 ? (
                <p className="text-sm text-[var(--color-muted-foreground)]">
                  {t("sharing.detail.noHolders")}
                </p>
              ) : (
                <>
                  <div className="hidden md:block">
                    <Table>
                      <THead>
                        <TR>
                          <TH>{t("sharing.activity.who")}</TH>
                          <TH>{t("sharing.holder.tokens")}</TH>
                          <TH>{t("sharing.col.lastUse")}</TH>
                          <TH />
                        </TR>
                      </THead>
                      <TBody>
                        {share.people.map((p) => (
                          <TR key={p.user_id}>
                            <TD><PersonCell person={p} /></TD>
                            <TD>{p.tokens}</TD>
                            <TD className="whitespace-nowrap">
                              {p.last_used_at ? minuteOf(p.last_used_at) : t("sharing.neverRead")}
                            </TD>
                            <TD className="text-right">{live && blockButton(p)}</TD>
                          </TR>
                        ))}
                      </TBody>
                    </Table>
                  </div>
                  {/* On a phone each person is a card: four columns do not fit. */}
                  <ul className="space-y-2 md:hidden">
                    {share.people.map((p) => (
                      <li key={p.user_id}
                          className="space-y-2 rounded-md border border-[var(--color-border)] p-3 text-sm">
                        <PersonCell person={p} />
                        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                          <dt className="text-[var(--color-muted-foreground)]">{t("sharing.holder.tokens")}</dt>
                          <dd>{p.tokens}</dd>
                          <dt className="text-[var(--color-muted-foreground)]">{t("sharing.col.lastUse")}</dt>
                          <dd>{p.last_used_at ? minuteOf(p.last_used_at) : t("sharing.neverRead")}</dd>
                        </dl>
                        {live && <div>{blockButton(p)}</div>}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>

            {share.blocked.length > 0 && (
              <section className="space-y-2">
                <h3 className="text-sm font-semibold">{t("sharing.detail.blocked")}</h3>
                <ul className="space-y-1">
                  {share.blocked.map((p) => (
                    <li key={p.user_id} className="flex items-center gap-2 text-sm">
                      <div className="min-w-0 flex-1"><PersonCell person={p} /></div>
                      {live && canGrant && (
                        <Button size="sm" variant="outline" disabled={unblock.isPending}
                                onClick={() => unblock.mutate(p.user_id)}>
                          {t("sharing.unblock")}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
                {live && !canGrant && (
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("sharing.detail.unblockNeedsLicence")}
                  </p>
                )}
              </section>
            )}

            <section className="space-y-2">
              <h3 className="text-sm font-semibold">{t("sharing.col.deny")}</h3>
              {share.deny_globs.length === 0 ? (
                <p className="text-sm text-[var(--color-muted-foreground)]">
                  {t("sharing.detail.noDeny")}
                </p>
              ) : (
                <div className="flex flex-wrap gap-1">
                  {share.deny_globs.map((g) => (
                    <code key={g} className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">{g}</code>
                  ))}
                </div>
              )}
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("sharing.dialog.denyHint")}</p>
            </section>

            <section className="space-y-2">
              <h3 className="text-sm font-semibold">{t("sharing.detail.activity")}</h3>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("sharing.detail.activityNote")}
              </p>
              <ActivityTable rows={activity.data} loading={activity.isLoading} />
            </section>

            <NarrowDialog share={share} canGrant={canGrant} open={narrowing} onOpenChange={setNarrowing} onDone={changed} />
          </>
        )}
        {dialog}
      </CardContent>
    </Card>
  );
}

function ActivityTable({ rows, loading }: { rows: ShareActivity[] | undefined; loading: boolean }) {
  const t = useT();
  if (loading) return <Skeleton className="h-16 w-full" />;
  if (!rows || rows.length === 0) {
    return <p className="text-sm text-[var(--color-muted-foreground)]">{t("sharing.activity.empty")}</p>;
  }
  const badge = (r: ShareActivity) => (
    <Badge variant={r.status === "ok" ? "success" : r.status === "denied" ? "warning" : "destructive"}>
      {r.status}
    </Badge>
  );
  return (
    <div className="max-h-80 overflow-y-auto rounded-md border border-[var(--color-border)]">
      <div className="hidden md:block">
        <Table>
          <THead>
            <TR>
              <TH>{t("sharing.activity.when")}</TH>
              <TH>{t("sharing.activity.who")}</TH>
              <TH>{t("sharing.activity.tool")}</TH>
              <TH>{t("sharing.col.status")}</TH>
            </TR>
          </THead>
          <TBody>
            {rows.map((r, i) => (
              <TR key={`${r.ts}-${i}`}>
                <TD className="whitespace-nowrap text-xs">{minuteOf(r.ts)}</TD>
                <TD><PersonCell person={r} /></TD>
                <TD><code className="text-xs">{r.tool}</code></TD>
                <TD>{badge(r)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>
      <ul className="divide-y divide-[var(--color-border)] md:hidden">
        {rows.map((r, i) => (
          <li key={`${r.ts}-${i}`} className="space-y-1 p-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs">{minuteOf(r.ts)}</span>
              {badge(r)}
            </div>
            <PersonCell person={r} />
            <code className="text-xs">{r.tool}</code>
          </li>
        ))}
      </ul>
    </div>
  );
}
