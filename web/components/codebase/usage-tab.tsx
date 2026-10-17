"use client";

/**
 * What agents did with a pack in the last 30 days — maintainers only.
 *
 * The numbers are the API's `usage` object and nothing more: how many calls
 * returned something from the pack, when the last one was, and per skill how
 * often its body was opened. There is no person in it, on purpose: this tells a
 * maintainer whether the pack is worth keeping up, not who read it.
 */

import { GaugeIcon } from "lucide-react";

import type { PackDetail } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { CodeBlock } from "@/components/codebase/copy";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

export function UsageTab({ pack }: { pack: PackDetail }) {
  const t = useT();
  const usage = pack.usage ?? { calls_30d: 0, last_used_at: null, skills: [] };

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-2 pt-5 sm:pt-6">
          <div className="flex items-center gap-3">
            <GaugeIcon aria-hidden className="size-8 text-[var(--color-muted-foreground)]" />
            <div>
              <div className="text-3xl font-semibold tabular-nums">{usage.calls_30d}</div>
              <div className="text-sm text-[var(--color-muted-foreground)]">{t("codebase.usage.label")}</div>
            </div>
          </div>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {usage.last_used_at
              ? t("codebase.usage.lastUsed", { date: formatDateTime(usage.last_used_at) })
              : t("codebase.usage.neverUsed")}
          </p>
          <p className="max-w-2xl text-xs leading-relaxed text-[var(--color-muted-foreground)]">
            {t("codebase.usage.desc")}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("codebase.usage.skillsTitle")}</CardTitle>
          <CardDescription>{t("codebase.usage.skillsDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {usage.skills.length > 0 ? (
            <Table>
              <THead>
                <TR>
                  <TH>{t("codebase.usage.skill")}</TH>
                  <TH className="text-right">{t("codebase.usage.opens")}</TH>
                  <TH>{t("codebase.usage.lastOpened")}</TH>
                </TR>
              </THead>
              <TBody>
                {usage.skills.map((s) => (
                  <TR key={s.skill}>
                    <TD className="font-mono text-xs">{s.skill}</TD>
                    <TD className="text-right tabular-nums">{s.opens_30d}</TD>
                    <TD className="whitespace-nowrap">
                      {s.last_used_at ? formatDateTime(s.last_used_at) : t("codebase.usage.never")}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : (
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("codebase.usage.noSkills")}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("codebase.usage.verifyTitle")}</CardTitle>
          <CardDescription>{t("codebase.usage.verifyDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <CodeBlock
            text={`celmis-pack status ./skills/${pack.slug}\ncelmis-pack verify ./skills/${pack.slug}`}
          />
        </CardContent>
      </Card>
    </div>
  );
}
