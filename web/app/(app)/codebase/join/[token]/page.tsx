"use client";

/**
 * Join a pack from an invite link.
 *
 * The link's token travels in the path, which is how the maintainer shares it,
 * and in a POST body to the API; it is never put in a query string. The
 * server answers one way for a link that is wrong, expired, revoked, used up
 * or meant for somebody else, so this page shows one message for all of them
 * and cannot be used to find out which it was.
 *
 * Joining is the one thing here that can change which workspace is active: the
 * pack may live in a workspace other than the one open now. That is handled
 * the way the workspace switcher does it — set the cookie, drop the agent's
 * session, and load the page fresh.
 */

import { use, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { BookMarkedIcon, CheckCircle2Icon, LinkIcon } from "lucide-react";
import { toast } from "sonner";

import { codebaseApi } from "@/lib/api";
import { forgetAgentSession } from "@/lib/agent-session";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { PageHeader, PageShell } from "@/components/page-shell";
import { Button, buttonVariants } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonRows } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export default function CodebaseJoinPage({ params }: { params: Promise<{ token: string }> }) {
  const { token: inviteToken } = use(params);
  const t = useT();
  const token = useToken();
  const [joined, setJoined] = useState(false);

  const preview = useQuery({
    queryKey: ["codebase", "invite", inviteToken],
    queryFn: () => codebaseApi.previewInvite(token!, inviteToken),
    enabled: !!token,
    // A wrong link stays wrong; asking again only delays the message.
    retry: false,
  });

  const join = useMutation({
    mutationFn: () => codebaseApi.redeemInvite(token!, inviteToken),
    onSuccess: (r) => {
      setJoined(true);
      document.cookie = `x-workspace=${r.workspace_slug}; path=/; max-age=31536000; SameSite=Lax`;
      forgetAgentSession();
      toast.success(t("codebase.join.joined"));
      // A full load on purpose: nothing from the previous workspace may carry over.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign(`/codebase/packs/${encodeURIComponent(r.pack_id)}`);
    },
    // Redeeming can fail the same way previewing does (the link was used up
    // in between); one sentence either way, never the server's wording.
    onError: () => toast.error(t("codebase.join.invalid")),
  });

  const p = preview.data;

  return (
    <PageShell>
      <PageHeader
        tour="page-codebase-join"
        icon={<LinkIcon className="h-6 w-6" />}
        title={t("codebase.join.title")}
        description={t("codebase.join.subtitle")}
      />

      {(preview.isPending || !token) && !preview.isError && <SkeletonRows rows={3} />}

      {preview.isError && (
        <EmptyState
          icon={LinkIcon}
          title={t("codebase.join.invalid")}
          description={t("codebase.join.invalidDesc")}
          action={
            <Link href="/codebase" className={cn(buttonVariants({ variant: "outline" }))}>
              {t("codebase.join.toCatalogue")}
            </Link>
          }
        />
      )}

      {p && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <BookMarkedIcon aria-hidden className="size-5" /> {p.pack_title}
            </CardTitle>
            {p.pack_description && <CardDescription>{p.pack_description}</CardDescription>}
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm">
              {t("codebase.join.workspace", { workspace: p.workspace_name })}
            </p>
            {p.already_member ? (
              <Callout tone="success" className="flex items-start gap-2">
                <CheckCircle2Icon aria-hidden className="mt-0.5 size-4 shrink-0" />
                <span>{t("codebase.join.already")}</span>
              </Callout>
            ) : (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("codebase.join.grants")}
              </p>
            )}
            <Button
              loading={join.isPending} disabled={joined || !token}
              onClick={() => join.mutate()}
            >
              {p.already_member ? t("codebase.join.open") : t("codebase.join.button")}
            </Button>
          </CardContent>
        </Card>
      )}
    </PageShell>
  );
}
