"use client";

/**
 * How to actually connect auto-review.
 *
 * The feature had two undiscoverable values — the URL to POST to and the
 * shared secret — and no screen said either. So it did not look unconfigured,
 * it looked broken: every delivery, correct ones included, came back 500
 * "Webhook secret not configured", and the only place that said so was a
 * server log.
 *
 * The other half of why this page exists: the alternative, polling, cannot
 * work here. It reads the token owner's GitHub notification INBOX, which
 * fine-grained tokens cannot access at all (403), and which never reports
 * your own pull requests. That is stated plainly below rather than left for
 * somebody to rediscover.
 *
 * Everything shown comes from the server: the URL is built from the request
 * the browser made, so it is right on a bare IP and behind a domain without
 * anybody configuring a base URL.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, CopyIcon, KeyIcon, RefreshCwIcon, TriangleAlertIcon } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { InlineHelp } from "@/components/ui/inline-help";

type Setup = {
  provider: string;
  url: string;
  configured: boolean;
  events: string[];
  header: string;
  scheme: string;
};

const LABEL: Record<string, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  bitbucket: "Bitbucket",
};

/** Where each provider hides the webhook form. Saves a search every time. */
const WHERE: Record<string, string> = {
  github: "Settings → Webhooks → Add webhook",
  gitlab: "Settings → Webhooks → Add new webhook",
  bitbucket: "Repository settings → Webhooks → Add webhook",
};

/** What the secret field is called there — it differs, and guessing wastes a try. */
const FIELD: Record<string, string> = {
  github: "Secret",
  gitlab: "Secret token",
  bitbucket: "Secret",
};

function Copyable({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-2 py-1.5 text-xs">
        {value}
      </code>
      <Button
        size="sm" variant="outline" className="h-8 shrink-0"
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

export function WebhookSetup() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  //: The secret is returned in full exactly once, so it is held here until the
  //: page is left. Nothing can read it back out of the store afterwards.
  const [fresh, setFresh] = useState<Record<string, string>>({});

  const setups = useQuery({
    queryKey: ["webhook-setup"],
    queryFn: () => api<Setup[]>("/api/webhooks", { token }),
    enabled: !!token,
  });

  const rotate = useMutation({
    mutationFn: (provider: string) =>
      api<{ provider: string; secret: string; url: string }>(
        `/api/webhooks/${provider}/secret`, { method: "POST", token }),
    onSuccess: (data) => {
      setFresh((cur) => ({ ...cur, [data.provider]: data.secret }));
      void qc.invalidateQueries({ queryKey: ["webhook-setup"] });
      toast.success(t("webhooks.generated", { provider: LABEL[data.provider] }));
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyIcon className="h-4 w-4" /> {t("webhooks.title")}
        </CardTitle>
        <CardDescription>{t("webhooks.subtitle")}</CardDescription>
        {/* The question that otherwise costs an afternoon: people turn on
            "auto-review", nothing happens, and nothing anywhere says why. */}
        <InlineHelp className="mt-1" question={t("webhooks.whyNotPolling")}>
          {t("webhooks.whyNotPollingBody")}
        </InlineHelp>
      </CardHeader>
      <CardContent className="space-y-5">
        {(setups.data ?? []).map((s) => {
          const secret = fresh[s.provider];
          return (
            <div key={s.provider} className="rounded-lg border border-[var(--color-border)] p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 font-medium">
                  {LABEL[s.provider]}
                  {s.configured ? (
                    <Badge variant="success" className="text-[10px]">
                      {t("webhooks.configured")}
                    </Badge>
                  ) : (
                    <Badge variant="outline" className="text-[10px]">
                      {t("webhooks.notConfigured")}
                    </Badge>
                  )}
                </div>
                <Button
                  size="sm" variant={s.configured ? "outline" : "default"}
                  disabled={rotate.isPending}
                  onClick={() => rotate.mutate(s.provider)}
                >
                  <RefreshCwIcon className={`mr-1 h-3.5 w-3.5 ${rotate.isPending ? "animate-spin" : ""}`} />
                  {s.configured ? t("webhooks.rotate") : t("webhooks.generate")}
                </Button>
              </div>

              <ol className="space-y-3 text-sm">
                <li>
                  <div className="mb-1 text-[var(--color-muted-foreground)]">
                    1. {t("webhooks.stepOpen", { where: WHERE[s.provider] })}
                  </div>
                </li>
                <li>
                  <div className="mb-1 text-[var(--color-muted-foreground)]">
                    2. {t("webhooks.stepUrl")}
                  </div>
                  <Copyable value={s.url} label={t("webhooks.copyUrl")} />
                </li>
                <li>
                  <div className="mb-1 text-[var(--color-muted-foreground)]">
                    3. {t("webhooks.stepSecret", { field: FIELD[s.provider] })}
                  </div>
                  {secret ? (
                    <>
                      <Copyable value={secret} label={t("webhooks.copySecret")} />
                      {/* Shown once, and saying so is the difference between
                          copying it now and generating a second one later. */}
                      <p className="mt-1 flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                        <TriangleAlertIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        {t("webhooks.shownOnce")}
                      </p>
                    </>
                  ) : (
                    <p className="text-xs text-[var(--color-muted-foreground)]">
                      {s.configured ? t("webhooks.secretHidden") : t("webhooks.secretMissing")}
                    </p>
                  )}
                </li>
                <li>
                  <div className="text-[var(--color-muted-foreground)]">
                    4. {t("webhooks.stepEvents", { events: s.events.join(", ") })}
                  </div>
                </li>
              </ol>

              <InlineHelp className="mt-3" question={t("webhooks.howVerified")}>
                {t("webhooks.howVerifiedBody", { header: s.header, scheme: s.scheme })}
              </InlineHelp>
            </div>
          );
        })}
        {setups.isPending && (
          <p className="text-sm text-[var(--color-muted-foreground)]">{t("common.loading")}</p>
        )}
      </CardContent>
    </Card>
  );
}
