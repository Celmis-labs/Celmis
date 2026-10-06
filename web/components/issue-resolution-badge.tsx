"use client";

/**
 * How an issue was closed, said where the status is shown: "Resolved
 * automatically in PR #42" (a link to that PR or its commit) with the
 * resolver's note as the tooltip. A person's own decision gets no badge —
 * the status select already says it.
 */

import { ExternalLinkIcon } from "lucide-react";

import type { ReviewIssue } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";

export function IssueResolutionBadge({ issue: i }: { issue: ReviewIssue }) {
  const t = useT();
  if (i.status !== "fixed" || i.resolution_kind !== "auto") return null;
  const sha = (i.fixed_in_sha ?? "").slice(0, 7);
  const label = i.fixed_by_pr_number
    ? t("issues.resolvedByPr", { pr: i.fixed_by_pr_number })
    : sha
      ? t("issues.resolvedInSha", { sha })
      : t("issues.resolvedAuto");
  const tip = [i.fixed_by_pr_title, i.resolution_note].filter(Boolean).join(" — ");
  const badge = (
    <Badge variant="success" className="w-fit gap-1" title={tip || undefined}>
      {label}
      {i.fixed_by_pr_url && <ExternalLinkIcon aria-hidden className="h-3 w-3" />}
    </Badge>
  );
  return i.fixed_by_pr_url ? (
    <a href={i.fixed_by_pr_url} target="_blank" rel="noreferrer" className="w-fit rounded">
      {badge}
    </a>
  ) : badge;
}
