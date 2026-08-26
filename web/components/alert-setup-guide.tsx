"use client";

/**
 * How to wire monitoring into Celmis, and Celmis into a chat room.
 *
 * These are two independent halves that look like one feature and are not:
 * inbound alerts land in a list on this page, outbound notifications are
 * configured on Settings → Notification channels, and neither triggers the
 * other. That is the single thing people get wrong, so it is stated first.
 *
 * The provider recipes are menu paths, which no amount of reading our own code
 * would reveal — they are the part of the setup that happens in someone else's
 * product, and the part a user would otherwise go and search for.
 */

import Link from "next/link";
import { ArrowRightIcon, BellRingIcon, MessageSquareIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";

/** i18n key stem → number of numbered steps under it. */
const OUTBOUND: Array<{ key: string; steps: number }> = [
  { key: "slack", steps: 5 },
  { key: "discord", steps: 4 },
  { key: "googlechat", steps: 4 },
];

const INBOUND: Array<{ key: string; steps: number }> = [
  { key: "grafana", steps: 4 },
  { key: "curl", steps: 3 },
];

function Recipe({ stem, steps }: { stem: string; steps: number }) {
  const t = useT();
  return (
    <div className="rounded-md border border-[var(--color-border)] p-3">
      <p className="text-sm font-medium">{t(`${stem}.title`)}</p>
      <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-xs text-[var(--color-muted-foreground)]">
        {Array.from({ length: steps }, (_, i) => (
          <li key={i} className="wrap-anywhere">{t(`${stem}.s${i + 1}`)}</li>
        ))}
      </ol>
    </div>
  );
}

export function AlertSetupGuide() {
  const t = useT();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <BellRingIcon className="h-4 w-4" /> {t("alertGuide.title")}
        </CardTitle>
        <CardDescription>{t("alertGuide.subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Stated before either recipe, because it is what makes a correct
            setup look broken: two halves, wired to different things. */}
        <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          {t("alertGuide.twoHalves")}
        </p>

        <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
            {t("alertGuide.inboundTitle")}
          </h3>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("alertGuide.inboundIntro")}
          </p>
          <div className="grid gap-2 sm:grid-cols-2">
            {INBOUND.map((r) => (
              <Recipe key={r.key} stem={`alertGuide.in.${r.key}`} steps={r.steps} />
            ))}
          </div>
        </section>

        <section className="space-y-2">
          <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
            <MessageSquareIcon className="h-3.5 w-3.5" />
            {t("alertGuide.outboundTitle")}
          </h3>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("alertGuide.outboundIntro")}
          </p>
          <div className="grid gap-2 sm:grid-cols-2">
            {OUTBOUND.map((r) => (
              <Recipe key={r.key} stem={`alertGuide.out.${r.key}`} steps={r.steps} />
            ))}
          </div>
          <p className="rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-3 py-2 text-xs text-[var(--color-muted-foreground)]">
            {t("alertGuide.bindingWarning")}
          </p>
          <Link href="/admin/notifications">
            <Button size="sm" variant="outline">
              {t("alertGuide.openChannels")} <ArrowRightIcon className="ml-1 h-3 w-3" />
            </Button>
          </Link>
        </section>
      </CardContent>
    </Card>
  );
}
