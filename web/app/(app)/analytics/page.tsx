"use client";

/**
 * /analytics — the route. AGPL; the dashboard itself is enterprise
 * (web/ee/analytics, LICENSE_EE) and so is the API behind it (src/ee).
 *
 * Which one is drawn follows /api/capabilities and its contract: only an
 * explicit `review_analytics: false` — the API did not mount /api/analytics
 * because no licence grants it — shows the "Enterprise" note. No answer, an
 * error, or a document without the key renders the dashboard, which then
 * makes its own request and handles its own refusal. While the document is
 * still loading a skeleton holds the place, so a community install does not
 * flash a dashboard that is about to be replaced.
 */

import { BarChart3Icon, SparklesIcon } from "lucide-react";

import { AnalyticsView } from "@/ee/analytics/analytics-view";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/lib/i18n";
import { featureOff, useCapabilities } from "@/lib/use-capabilities";

export default function AnalyticsPage() {
  const t = useT();
  const caps = useCapabilities();

  if (caps.isPending) {
    return (
      <PageShell width="wide">
        <Skeleton className="h-24" />
      </PageShell>
    );
  }

  if (featureOff(caps.data, "review_analytics")) {
    return (
      <PageShell width="wide">
        <PageHeader
          icon={<BarChart3Icon className="h-6 w-6" />}
          title={t("analytics.title")}
          description={t("analytics.subtitle")}
          tabs={<SectionTabs set="review" />}
        />
        <Card>
          <CardContent>
            <EmptyState
              icon={SparklesIcon}
              title={t("enterprise.analytics.title")}
              description={t("enterprise.analytics.desc")}
            />
          </CardContent>
        </Card>
      </PageShell>
    );
  }

  return <AnalyticsView />;
}
