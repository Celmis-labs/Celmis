"use client";

/**
 * /productivity — the route. AGPL; the page itself is enterprise
 * (web/ee/analytics, LICENSE_EE) and so is the API behind it (src/ee), under
 * the same licence feature as /analytics.
 *
 * Which one is drawn follows /api/capabilities and its contract: only an
 * explicit `productivity: false` — the API did not mount
 * /api/analytics/productivity because no licence grants it — shows the
 * "Enterprise" note. No answer, an error, or a document without the key
 * renders the page, which then makes its own request and handles its own
 * refusal. While the document is still loading a skeleton holds the place.
 */

import { GaugeIcon, SparklesIcon } from "lucide-react";

import { ProductivityView } from "@/ee/analytics/productivity-view";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/lib/i18n";
import { featureOff, useCapabilities } from "@/lib/use-capabilities";

export default function ProductivityPage() {
  const t = useT();
  const caps = useCapabilities();

  if (caps.isPending) {
    return (
      <PageShell width="wide">
        <Skeleton className="h-24" />
      </PageShell>
    );
  }

  if (featureOff(caps.data, "productivity")) {
    return (
      <PageShell width="wide">
        <PageHeader
          icon={<GaugeIcon className="h-6 w-6" />}
          title={t("productivity.title")}
          description={t("productivity.subtitle")}
          tabs={<SectionTabs set="review" />}
        />
        <Card>
          <CardContent>
            <EmptyState
              icon={SparklesIcon}
              title={t("enterprise.productivity.title")}
              description={t("enterprise.productivity.desc")}
            />
          </CardContent>
        </Card>
      </PageShell>
    );
  }

  return <ProductivityView />;
}
