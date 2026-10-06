"use client";

/**
 * What a pull request was meant to do: the Jira task(s) the last review read
 * and, under each, the acceptance criteria as a checklist with how the change
 * stands against every one. Shown above the review timeline of an expanded
 * pull-request row; a PR whose review read no task shows nothing at all, so
 * a team without Jira never sees an empty card.
 *
 * The texts come from Jira, so they are rendered as plain text (never as
 * markup) and the task link only when the server vouched for an https URL.
 */

import { useQuery } from "@tanstack/react-query";
import { CheckIcon, CircleHelpIcon, ExternalLinkIcon, TriangleAlertIcon, XIcon } from "lucide-react";

import { pullRequestsApi, type RequirementVerdict } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Badge } from "@/components/ui/badge";

const VERDICT_TONE: Record<RequirementVerdict, "success" | "warning" | "destructive" | "default"> = {
  met: "success",
  no_gap: "success",
  partial: "warning",
  missing: "destructive",
  contradicts: "destructive",
  unclear: "default",
};

function VerdictIcon({ verdict }: { verdict: RequirementVerdict }) {
  const cls = "h-3.5 w-3.5";
  if (verdict === "met" || verdict === "no_gap") return <CheckIcon aria-hidden className={cls} />;
  if (verdict === "partial") return <TriangleAlertIcon aria-hidden className={cls} />;
  if (verdict === "missing" || verdict === "contradicts") return <XIcon aria-hidden className={cls} />;
  return <CircleHelpIcon aria-hidden className={cls} />;
}

export function PullRequestRequirements({ prId }: { prId: string }) {
  const token = useToken();
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["pr-requirements", prId],
    queryFn: () => pullRequestsApi.requirements(token!, prId),
    enabled: !!token,
    retry: false,
  });
  const data = query.data;
  if (!data || data.tasks.length === 0) return null;
  return (
    <section
      aria-label={t("prs.requirements.title")}
      className="mb-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2.5"
    >
      <h3 className="text-sm font-semibold">{t("prs.requirements.title")}</h3>
      <div className="mt-2 flex flex-col gap-3">
        {data.tasks.map((task) => {
          const rows = data.requirements.filter((r) => r.key === task.key);
          return (
            <div key={task.key} className="text-sm">
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {task.url ? (
                  <a href={task.url} target="_blank" rel="noreferrer"
                    className="inline-flex items-center gap-1 font-mono text-xs font-medium hover:underline">
                    {task.key} <ExternalLinkIcon aria-hidden className="h-3 w-3" />
                  </a>
                ) : <span className="font-mono text-xs font-medium">{task.key}</span>}
                <span>{task.summary}</span>
                {task.status && <Badge variant="default">{task.status}</Badge>}
              </p>
              {rows.length === 0 ? (
                <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                  {t("prs.requirements.none")}
                </p>
              ) : (
                <ul className="mt-1.5 flex flex-col gap-1.5">
                  {rows.map((r) => (
                    <li key={r.id} className="flex flex-wrap items-start gap-x-2 gap-y-0.5">
                      <Badge variant={VERDICT_TONE[r.verdict] ?? "default"}>
                        <VerdictIcon verdict={r.verdict} />
                        {t(`prs.requirements.verdict.${r.verdict}`)}
                      </Badge>
                      <span className="font-mono text-xs text-[var(--color-muted-foreground)]">{r.id}</span>
                      <span className="min-w-0 flex-1 basis-60">{r.text}</span>
                      {r.evidence && (
                        <code className="font-mono text-xs text-[var(--color-muted-foreground)]">
                          {r.evidence}
                        </code>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
