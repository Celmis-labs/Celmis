"use client";

/**
 * The review webhook, per repository: is it there, and a button to put it there.
 *
 * A repository registered with auto-review reviewed nothing until somebody
 * pasted a webhook into the provider by hand — a step the product documented
 * and never performed, so /pull-requests stayed empty with no reason given.
 * The server now installs the hook itself (POST /api/repos/{slug}/webhook);
 * this shows the outcome and, when the server cannot do it (no public URL,
 * a token without webhook permission), the manual steps instead.
 *
 * The status comes from the repository list, which makes no provider call.
 * The secret is never shown here: it lives on the Reviews page, where it can
 * be generated and is displayed exactly once.
 */

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, CopyIcon, Loader2Icon, WebhookIcon } from "lucide-react";
import { toast } from "sonner";

import { api, ApiError, type RepoOut, type RepoWebhook } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";

/** Where each provider hides the webhook form. */
const WHERE: Record<string, string> = {
  github: "Settings → Webhooks → Add webhook",
  gitlab: "Settings → Webhooks → Add new webhook",
  bitbucket: "Repository settings → Webhooks → Add webhook",
};

/** What each receiver acts on — mirrors src/review/webhook_install.py EVENTS. */
const EVENTS: Record<string, string[]> = {
  github: ["pull_request", "push"],
  gitlab: ["Merge request events"],
  bitbucket: [
    "pullrequest:created", "pullrequest:updated",
    "pullrequest:fulfilled", "pullrequest:rejected",
  ],
};

function CopyLine({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-2 py-1.5 text-xs">
        {value}
      </code>
      <Button
        type="button" size="sm" variant="outline" className="h-8 shrink-0"
        title={label} aria-label={label}
        onClick={() => {
          void navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? <CheckIcon className="h-3.5 w-3.5" /> : <CopyIcon className="h-3.5 w-3.5" />}
      </Button>
    </div>
  );
}

function StatusBadge({ hook }: { hook: RepoWebhook | null | undefined }) {
  const t = useT();
  if (hook?.status === "installed") {
    return <Badge variant="success" className="px-1.5 py-0">{t("repoWebhook.installed")}</Badge>;
  }
  if (hook?.status === "failed") {
    return <Badge variant="destructive" className="px-1.5 py-0">{t("repoWebhook.failed")}</Badge>;
  }
  return <Badge variant="outline" className="px-1.5 py-0">{t("repoWebhook.none")}</Badge>;
}

export function RepoWebhookControl({ repo }: { repo: RepoOut }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  //: The outcome of the last press, or the 409 explanation. Shown in the dialog.
  const [last, setLast] = useState<RepoWebhook | null>(null);
  const hook = last ?? repo.webhook ?? null;
  const installed = hook?.status === "installed";

  const install = useMutation({
    mutationFn: () =>
      api<RepoWebhook>(`/api/repos/${repo.slug}/webhook`, { method: "POST", token }),
    onSuccess: (st) => {
      setLast(st);
      void qc.invalidateQueries({ queryKey: ["repos"] });
      if (st.status === "installed") {
        toast.success(st.action === "updated"
          ? t("repoWebhook.repaired") : t("repoWebhook.created"));
      } else {
        toast.error(st.message ?? t("repoWebhook.failed"));
        setOpen(true);
      }
    },
    onError: (e: Error) => {
      // 409 = no usable PUBLIC_BASE_URL; 403 = not a workspace admin. Either
      // way the manual route is what is left, so open it with the reason.
      const status = e instanceof ApiError ? e.status : 0;
      setLast({
        provider: repo.provider, status: "skipped", url: null,
        events: EVENTS[repo.provider] ?? [], hook_id: null, action: null,
        reason: status === 409 ? "no_public_url" : status === 403 ? "not_admin" : "error",
        message: e.message, hint: null, last_delivery: null, full_name: null,
        updated_at: null,
      });
      setOpen(true);
    },
  });

  const events = hook?.events?.length ? hook.events : (EVENTS[repo.provider] ?? []);

  return (
    <>
      <StatusBadge hook={hook} />
      <Button
        type="button" size="sm" variant="outline"
        disabled={install.isPending}
        onClick={() => install.mutate()}
        title={installed ? t("repoWebhook.repairTitle") : t("repoWebhook.installTitle")}
      >
        {install.isPending
          ? <Loader2Icon className="h-3.5 w-3.5 animate-spin" />
          : <WebhookIcon className="h-3.5 w-3.5" />}
        {installed ? t("repoWebhook.repair") : t("repoWebhook.install")}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <button
            type="button"
            className="inline-flex min-h-9 items-center hover:underline"
          >
            {t("repoWebhook.manual")}
          </button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("repoWebhook.manualTitle", { repo: repo.full_name })}</DialogTitle>
            <DialogDescription>{t("repoWebhook.manualIntro")}</DialogDescription>
          </DialogHeader>
          {hook && hook.status !== "installed" && (hook.message || hook.hint) && (
            <div className="space-y-1 rounded-md border border-[var(--color-warning)]/40 bg-[var(--color-warning)]/10 p-3 text-xs">
              {hook.message && <p>{hook.message}</p>}
              {hook.hint && <p className="text-[var(--color-muted-foreground)]">{hook.hint}</p>}
            </div>
          )}
          <ol className="list-decimal space-y-3 pl-5 text-sm">
            <li>{t("repoWebhook.stepOpen", { where: WHERE[repo.provider] ?? "" })}</li>
            <li className="space-y-1">
              <div>{t("repoWebhook.stepUrl")}</div>
              {hook?.url ? (
                <CopyLine value={hook.url} label={t("repoWebhook.copyUrl")} />
              ) : (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("repoWebhook.urlOnReviews")}
                </p>
              )}
            </li>
            <li>{t("repoWebhook.stepEvents", { events: events.join(", ") })}</li>
            <li>
              {t("repoWebhook.stepSecret")}{" "}
              <Link href="/reviews" className="underline">{t("repoWebhook.secretLink")}</Link>
            </li>
          </ol>
          {hook?.last_delivery && (
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("repoWebhook.lastDelivery", {
                value: [hook.last_delivery.code, hook.last_delivery.status, hook.last_delivery.message]
                  .filter((v) => v !== null && v !== undefined && v !== "").join(" · "),
              })}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** For the empty /pull-requests and /issues pages: nothing has been reviewed,
 *  and the usual reason is that no provider is delivering events yet. */
export function NoReviewsYetHint() {
  const t = useT();
  return (
    <p className="mx-auto mt-3 max-w-md text-xs text-[var(--color-muted-foreground)]">
      {t("repoWebhook.emptyHint")}{" "}
      <Link href="/repositories" className="underline">{t("repoWebhook.emptyInstall")}</Link>
      {" · "}
      <Link href="/reviews" className="underline">{t("repoWebhook.emptyRun")}</Link>
    </p>
  );
}
