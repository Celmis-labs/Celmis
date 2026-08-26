"use client";

/**
 * /capabilities — the standing "what can this product do" reference.
 *
 * The 19 scenario cards used to live at the bottom of the onboarding wizard,
 * where they were only ever seen once. Now they are a page of their own
 * (linked from onboarding and mapped under the Dashboard section), so the
 * playbook stays discoverable after setup is finished.
 */

import { CompassIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { ScenarioCards } from "@/components/scenario-cards";

export default function CapabilitiesPage() {
  const t = useT();
  return (
    <PageShell>
      <PageHeader
        icon={<CompassIcon className="h-6 w-6" />}
        title={t("capabilities.title")}
        description={t("capabilities.subtitle")}
        tabs={<SectionTabs set="dashboard" />}
      />
      <ScenarioCards />
    </PageShell>
  );
}
