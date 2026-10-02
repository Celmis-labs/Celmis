"use client";

/**
 * Which edition this API is running, and the licence behind it — for the
 * operator, on /admin/health.
 *
 * Read from /api/capabilities with the session token: the customer and the
 * expiry are only in the signed-in document (a customer name is not for the
 * anonymous first paint). The token itself never leaves the API.
 */
import { useState } from "react";
import { BadgeCheckIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDate } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useCapabilities } from "@/lib/use-capabilities";

/** Licence feature name → i18n key; unknown names are shown as they are. */
const FEATURE_KEY: Record<string, string> = {
  sso: "edition.feature.sso",
  analytics: "edition.feature.analytics",
};

export function EditionCard() {
  const t = useT();
  const caps = useCapabilities();
  // Read once per mount: an expiry is a date, not a countdown.
  const [now] = useState(() => Date.now());

  const data = caps.data;
  const enterprise = data?.edition === "enterprise";
  const lic = data?.license;
  const expired = Boolean(lic?.expires_at && Date.parse(lic.expires_at) <= now);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <BadgeCheckIcon className="h-4 w-4" aria-hidden />
          {t("edition.title")}
          {data && (
            <Badge variant="outline">
              {enterprise
                ? t("edition.enterprise")
                : data.edition === "community"
                  ? t("edition.community")
                  : data.edition}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-sm">
        {!data ? (
          caps.isPending ? null : (
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("edition.unknown")}
            </p>
          )
        ) : enterprise && lic ? (
          <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-[max-content_1fr]">
            <dt className="text-[var(--color-muted-foreground)]">{t("edition.customer")}</dt>
            <dd>{lic.customer ?? "—"}</dd>
            <dt className="text-[var(--color-muted-foreground)]">{t("edition.expires")}</dt>
            <dd className={expired ? "font-semibold text-red-600" : undefined}>
              {formatDate(lic.expires_at)}
              {expired && ` — ${t("edition.expired")}`}
            </dd>
            <dt className="text-[var(--color-muted-foreground)]">{t("edition.features")}</dt>
            <dd>
              {lic.features.length
                ? lic.features.map((f) => (FEATURE_KEY[f] ? t(FEATURE_KEY[f]) : f)).join(", ")
                : "—"}
            </dd>
          </dl>
        ) : (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("edition.communityHint")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
