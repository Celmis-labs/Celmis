"use client";

/**
 * The requester's side of an access request (src/api/routers/access_requests.py):
 * the dashboard banner and the decision toast. The page itself is
 * app/(app)/access-request/page.tsx.
 *
 * Nothing here names a workspace until a request is approved — the API does
 * not send one, and this file has nothing to name it with.
 */

import { useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowRightIcon, DoorOpenIcon, HourglassIcon } from "lucide-react";

import { accessRequestsApi, WORKSPACES_CHANGED_EVENT, type MyAccess } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { roleLabel } from "@/lib/roles";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

export const MY_ACCESS_KEY = ["access-request", "me"] as const;

/** While a request is pending the answer is re-read every minute, so an
 *  approval reaches the switcher during the same visit. */
const PENDING_POLL_MS = 60_000;

export function useMyAccess() {
  const token = useToken();
  return useQuery<MyAccess>({
    queryKey: MY_ACCESS_KEY,
    queryFn: () => accessRequestsApi.me(token!),
    enabled: !!token,
    refetchInterval: (q) =>
      q.state.data?.request?.status === "pending" ? PENDING_POLL_MS : false,
  });
}

/** Dashboard banner for an account whose only workspace is its personal one. */
export function AccessRequestBanner() {
  const t = useT();
  const me = useMyAccess();
  const data = me.data;
  if (!data || !data.eligible) return null;
  const pending = data.request?.status === "pending";
  return (
    <Card className="border-[var(--color-brand)]/40 bg-[var(--color-brand-muted)]">
      <CardContent className="flex flex-col gap-3 pt-6 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-2 text-sm">
          {pending ? (
            <HourglassIcon className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-brand)]" />
          ) : (
            <DoorOpenIcon className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-brand)]" />
          )}
          <span>{pending ? t("accessRequest.banner.pending") : t("accessRequest.banner.none")}</span>
        </div>
        <Link href="/access-request" className="inline-flex shrink-0">
          <Button size="sm" variant={pending ? "outline" : "default"}>
            {pending ? t("accessRequest.banner.view") : t("accessRequest.banner.cta")}
            <ArrowRightIcon className="ml-1 h-3.5 w-3.5" />
          </Button>
        </Link>
      </CardContent>
    </Card>
  );
}

const SEEN_KEY = "celmis:access-decision-seen";

/**
 * A toast, once per decision, on the visit after a superadmin decided — and
 * on approval a nudge for the workspace switcher to re-read its list. Which
 * decision was already shown is a per-browser convenience (localStorage);
 * losing it costs one repeated toast, the decision itself lives on the
 * server and on /access-request.
 */
export function AccessDecisionNotifier() {
  const t = useT();
  const router = useRouter();
  const me = useMyAccess();
  const req = me.data?.request;
  const decided = req && (req.status === "approved" || req.status === "rejected") ? req : null;

  useEffect(() => {
    if (!decided) return;
    const marker = `${decided.id}:${decided.status}`;
    let seen: string | null = null;
    try { seen = localStorage.getItem(SEEN_KEY); } catch { /* private mode */ }
    if (seen === marker) return;
    try { localStorage.setItem(SEEN_KEY, marker); } catch { /* private mode */ }
    if (decided.status === "approved") {
      window.dispatchEvent(new Event(WORKSPACES_CHANGED_EVENT));
      const names = decided.grants
        .map((g) => `${g.workspace_name} (${roleLabel(t, g.role)})`)
        .join(", ");
      toast.success(t("accessRequest.toast.approved", { workspaces: names }), {
        action: { label: t("accessRequest.toast.open"), onClick: () => router.push("/access-request") },
      });
    } else {
      toast.error(t("accessRequest.toast.rejected"), {
        action: { label: t("accessRequest.toast.open"), onClick: () => router.push("/access-request") },
      });
    }
  }, [decided, t, router]);

  return null;
}
