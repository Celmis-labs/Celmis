"use client";

/**
 * /access-request — "I have no team workspace yet": send a request to the
 * superadmin, see where it stands, cancel it.
 *
 * Any signed-in account, whatever way it signed in. The request names no
 * workspace and the page shows none until it is approved — then exactly the
 * ones granted. See src/api/routers/access_requests.py.
 */

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2Icon, DoorOpenIcon, HourglassIcon, XCircleIcon } from "lucide-react";

import { accessRequestsApi, WORKSPACES_CHANGED_EVENT } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { roleLabel } from "@/lib/roles";
import { MY_ACCESS_KEY, useMyAccess } from "@/components/access-request";
import { PageHeader, PageShell } from "@/components/page-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const COMMENT_MAX = 1000;

export default function AccessRequestPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const me = useMyAccess();
  const { confirm, dialog } = useConfirm();
  const [comment, setComment] = useState("");

  const refresh = () => qc.invalidateQueries({ queryKey: MY_ACCESS_KEY });
  const onError = (e: unknown) => toast.error((e as Error).message);

  const send = useMutation({
    mutationFn: () => accessRequestsApi.create(token!, comment.trim()),
    onSuccess: () => {
      setComment("");
      toast.success(t("accessRequest.sent"));
      void refresh();
    },
    onError,
  });
  const cancel = useMutation({
    mutationFn: () => accessRequestsApi.cancel(token!),
    onSuccess: () => {
      toast.success(t("accessRequest.cancelled"));
      void refresh();
    },
    onError,
  });

  const data = me.data;
  const req = data?.request ?? null;
  const pending = req?.status === "pending";
  // A new request is offered when nothing is pending: never asked, cancelled,
  // or rejected — a rejection is not final.
  const canAsk = Boolean(data?.eligible) && !pending;

  return (
    <PageShell>
      <PageHeader
        icon={<DoorOpenIcon className="h-6 w-6" />}
        title={t("accessRequest.title")}
        description={t("accessRequest.description")}
      />

      {me.isLoading && (
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("common.loading")}</p>
      )}
      {me.isError && <Callout tone="danger">{(me.error as Error).message}</Callout>}

      {data && !data.eligible && data.has_team_access && req?.status !== "approved" && (
        <Callout tone="info">{t("accessRequest.alreadyHasAccess")}</Callout>
      )}

      {req && (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2 text-base">
              {req.status === "pending" && <HourglassIcon className="h-4 w-4 text-amber-500" />}
              {req.status === "approved" && <CheckCircle2Icon className="h-4 w-4 text-emerald-500" />}
              {req.status === "rejected" && <XCircleIcon className="h-4 w-4 text-red-500" />}
              {t("accessRequest.yourRequest")}
              <Badge variant="outline">{t(`accessRequest.status.${req.status}`)}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("accessRequest.sentAt", { when: formatDateTime(req.created_at) })}
              {req.decided_at ? ` · ${t("accessRequest.decidedAt", { when: formatDateTime(req.decided_at) })}` : ""}
            </p>
            {req.comment && (
              <p className="whitespace-pre-wrap break-words rounded-md border border-[var(--color-border)] px-3 py-2 text-xs">
                {req.comment}
              </p>
            )}

            {req.status === "pending" && (
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-[var(--color-muted-foreground)]">{t("accessRequest.pendingHint")}</p>
                <Button
                  size="sm" variant="outline" disabled={cancel.isPending}
                  onClick={async () => {
                    const ok = await confirm({
                      title: t("accessRequest.cancelConfirm"),
                      confirmLabel: t("accessRequest.cancel"),
                      danger: true,
                    });
                    if (ok) cancel.mutate();
                  }}
                >
                  {t("accessRequest.cancel")}
                </Button>
              </div>
            )}

            {req.status === "approved" && (
              <div className="space-y-2">
                <p>{t("accessRequest.approvedIntro")}</p>
                <ul className="space-y-1">
                  {req.grants.map((g) => (
                    <li key={g.workspace_id} className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{g.workspace_name}</span>
                      <Badge variant="outline">{roleLabel(t, g.role)}</Badge>
                    </li>
                  ))}
                </ul>
                <Button
                  size="sm"
                  onClick={() => window.dispatchEvent(new Event(WORKSPACES_CHANGED_EVENT))}
                >
                  {t("accessRequest.refreshSwitcher")}
                </Button>
              </div>
            )}

            {req.status === "rejected" && req.decision_note && (
              <Callout tone="danger">
                <span className="font-medium">{t("accessRequest.reason")}</span>{" "}
                <span className="whitespace-pre-wrap break-words">{req.decision_note}</span>
              </Callout>
            )}
          </CardContent>
        </Card>
      )}

      {canAsk && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {req?.status === "rejected" ? t("accessRequest.askAgain") : t("accessRequest.noAccess")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-2">
              <Label htmlFor="access-comment">{t("accessRequest.commentLabel")}</Label>
              <Textarea
                id="access-comment"
                value={comment}
                maxLength={COMMENT_MAX}
                onChange={(e) => setComment(e.target.value)}
                placeholder={t("accessRequest.commentPlaceholder")}
              />
              <p className="text-right text-[11px] text-[var(--color-muted-foreground)]">
                {comment.length}/{COMMENT_MAX}
              </p>
            </div>
            <Button disabled={send.isPending || comment.length > COMMENT_MAX} onClick={() => send.mutate()}>
              {send.isPending ? t("common.saving") : t("accessRequest.send")}
            </Button>
          </CardContent>
        </Card>
      )}

      {data && !data.eligible && !data.has_team_access && !req && (
        <Callout tone="info">{t("accessRequest.notForSuperadmin")}</Callout>
      )}

      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("accessRequest.inviteHint")}{" "}
        <Link href="/dashboard" className="underline">{t("accessRequest.backToDashboard")}</Link>
      </p>
      {dialog}
    </PageShell>
  );
}
