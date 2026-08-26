"use client";

/**
 * The three artefacts a buyer asks for, where a buyer can find them.
 *
 * They existed, and reaching them took five steps: Repositories → the
 * Dependencies sub-tab → run an audit → scroll a long page to the bottom →
 * past the Word/Markdown/Print row, below a divider. Nothing in the
 * navigation, the capability reference or any label outside those two buttons
 * used the word SBOM at all. So the most commercial thing the product does —
 * the part a procurement department asks for by name rather than a developer
 * discovering it — was reachable only by accident.
 *
 * It sits at the TOP and says what each file is, because "SBOM" and "evidence
 * pack" are names for people who already know they need them. Someone who
 * doesn't should be able to learn it here rather than by downloading a zip to
 * see what falls out.
 *
 * The third is the vault, and it was the one we were not presenting as an
 * artefact at all. An SBOM says what is in the product; the evidence pack says
 * what we knew and when; the vault says how the thing works — which is the
 * technical documentation the CRA asks for alongside the inventory. It is also
 * the only one of the three that outlives the subscription: it can be handed
 * to a customer and it keeps working afterwards.
 */

import { useState } from "react";
import { toast } from "sonner";
import {
  BookTextIcon, FileBoxIcon, PackageSearchIcon, ShieldCheckIcon,
} from "lucide-react";
import Link from "next/link";

import { depsApi, downloadWithAuth } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { InlineHelp } from "@/components/ui/inline-help";

export function ComplianceArtifacts({
  runId, token, repoCount,
}: {
  runId: string | undefined;
  token: string | null | undefined;
  repoCount?: number;
}) {
  const t = useT();
  const [busy, setBusy] = useState<"sbom" | "evidence" | null>(null);

  const download = async (kind: "sbom" | "evidence") => {
    if (!runId) return;
    setBusy(kind);
    try {
      await downloadWithAuth(
        kind === "sbom" ? depsApi.sbomUrl(runId) : depsApi.evidenceUrl(runId),
        kind === "sbom"
          ? `celmis-sbom-${runId}.zip`
          : `celmis-evidence-${runId}.zip`,
        token,
      );
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card className="border-[var(--color-brand)]/40 print:hidden">
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          <FileBoxIcon className="h-4 w-4 text-[var(--color-brand)]" />
          {t("deps.artifactsTitle")}
          <Badge variant="brand" className="text-[10px]">
            {t("deps.artifactsBadge")}
          </Badge>
        </CardTitle>
        <CardDescription>{t("deps.artifactsDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          {/* Each one names what the file IS, not what the button does. */}
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <div className="mb-1 flex items-center gap-2 text-sm font-medium">
              <PackageSearchIcon className="h-3.5 w-3.5" />
              {t("deps.downloadSbom")}
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("deps.artifactsSbomWhat")}
            </p>
            <Button size="sm" variant="outline" className="mt-2 h-11 sm:h-8"
                    disabled={!runId || busy !== null}
                    onClick={() => download("sbom")}>
              {busy === "sbom" ? t("deps.artifactsPreparing") : t("deps.artifactsGetSbom")}
            </Button>
          </div>

          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <div className="mb-1 flex items-center gap-2 text-sm font-medium">
              <ShieldCheckIcon className="h-3.5 w-3.5" />
              {t("deps.downloadEvidence")}
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("deps.artifactsEvidenceWhat")}
            </p>
            <Button size="sm" variant="outline" className="mt-2 h-11 sm:h-8"
                    disabled={!runId || busy !== null}
                    onClick={() => download("evidence")}>
              {busy === "evidence" ? t("deps.artifactsPreparing") : t("deps.artifactsGetEvidence")}
            </Button>
          </div>
        </div>

        {/* The third artefact of the same class, and the one we were not
            presenting as one. An SBOM says what is in the product; the
            evidence pack says what we knew and when; the vault says how the
            thing works — which is the technical documentation the CRA asks
            for alongside the other two.
            It is also the only one that outlives us: a customer can be handed
            the vault, and it keeps working after a subscription ends. */}
        <div className="rounded-lg border border-[var(--color-border)] p-3">
          <div className="mb-1 flex items-center gap-2 text-sm font-medium">
            <BookTextIcon className="h-3.5 w-3.5" />
            {t("deps.artifactsVault")}
          </div>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("deps.artifactsVaultWhat")}
          </p>
          <Link href="/docs">
            <Button size="sm" variant="outline" className="mt-2 h-11 sm:h-8">
              {t("deps.artifactsOpenVault")}
            </Button>
          </Link>
        </div>

        {!runId && (
          // Both buttons need a finished run. Saying so beats two disabled
          // buttons with no explanation.
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("deps.artifactsNeedRun")}
          </p>
        )}

        <InlineHelp question={t("deps.artifactsWhyAsk")}>
          {t("deps.artifactsWhyAskBody", { repos: String(repoCount ?? 0) })}
        </InlineHelp>
      </CardContent>
    </Card>
  );
}
