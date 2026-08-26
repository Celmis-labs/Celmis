"use client";

/**
 * Cross-repo drift, shown as the finding it is.
 *
 * The detector produced a DriftReport, rendered it to markdown, and handed it
 * to a language model — the architect agent then, the contract agent since the
 * remit was split. That was its only exit, so the one deterministic check in
 * the product reached the user through a model's summary of it. They read what
 * the model chose to mention, not the fact.
 *
 * That is backwards for this particular finding, because it is the one that
 * needs no defending: a value, the file it was removed from, and every sibling
 * repository that still hardcodes it, with the line and the line's text. A
 * reader agrees or disagrees in five seconds.
 *
 * It sits ABOVE the model's findings and outside their list. Not styling —
 * twenty percent false positives is where developers stop reading a tool at
 * all, and mixing a grep result into that list is how the proven part
 * inherits the inferred part's reputation.
 */

import { ArrowRightIcon, GitCompareIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { InlineHelp } from "@/components/ui/inline-help";

type Drift = {
  group: string | null;
  repos_scanned: string[];
  hits: {
    value: string;
    removed_from: { file: string; line: number };
    still_in: { repo: string; file: string; line: number; excerpt: string }[];
    truncated: number;
  }[];
};

export function DriftPanel({ drift }: { drift: Drift | null }) {
  const t = useT();
  // Nothing found is not the same as not run, but neither is worth a card on a
  // page about one pull request — the reviews list already says the review
  // happened.
  if (!drift || drift.hits.length === 0) return null;

  return (
    <Card className="border-[var(--color-brand)]/40">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <GitCompareIcon className="h-4 w-4 text-[var(--color-brand)]" />
          {t("drift.title")}
          <Badge variant="success" className="text-[10px]">
            {t("drift.proven")}
          </Badge>
        </CardTitle>
        <CardDescription>
          {t("drift.subtitle", {
            count: String(drift.hits.length),
            repos: String(drift.repos_scanned.length),
          })}
        </CardDescription>
        <InlineHelp className="mt-1" question={t("drift.whyProven")}>
          {t("drift.whyProvenBody")}
        </InlineHelp>
      </CardHeader>
      <CardContent className="space-y-4">
        {drift.hits.map((hit) => (
          <div
            key={`${hit.value}-${hit.removed_from.file}-${hit.removed_from.line}`}
            className="rounded-lg border border-[var(--color-border)] p-3"
          >
            {/* The value first: it is the thing the reader is looking for in
                their own head before they read anything else. */}
            <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
              <code className="rounded bg-[var(--color-muted)]/40 px-1.5 py-0.5 text-xs">
                {hit.value}
              </code>
              <span className="text-xs text-[var(--color-muted-foreground)]">
                {t("drift.removedFrom")}
              </span>
              <code className="text-xs">
                {hit.removed_from.file}:{hit.removed_from.line}
              </code>
            </div>

            <div className="mb-1.5 flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
              <ArrowRightIcon className="h-3 w-3" />
              {t("drift.stillIn", { count: String(hit.still_in.length) })}
            </div>

            {/* One row per site, and every row carries the line itself. A file
                name alone would make this another claim to be taken on
                trust. */}
            <div className="space-y-1.5">
              {hit.still_in.map((m) => (
                <div
                  key={`${m.repo}-${m.file}-${m.line}`}
                  className="rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/20 p-2"
                >
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
                    <span className="font-medium">{m.repo}</span>
                    <code className="text-[var(--color-muted-foreground)]">
                      {m.file}:{m.line}
                    </code>
                  </div>
                  <code className="mt-1 block overflow-x-auto whitespace-pre text-[11px] text-[var(--color-muted-foreground)]">
                    {m.excerpt}
                  </code>
                </div>
              ))}
              {hit.truncated > 0 && (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("drift.more", { count: String(hit.truncated) })}
                </p>
              )}
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
