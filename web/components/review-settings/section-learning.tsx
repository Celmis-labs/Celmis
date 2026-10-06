"use client";

/** Learning: whether reviews are told the team's memories, whether a new
 *  memory waits for approval, who may add one without that wait, and whether
 *  findings the team already dismissed are left out. The memories and what
 *  was learned live on their own page. */

import Link from "next/link";
import { useId } from "react";

import { useT } from "@/lib/i18n";
import { useCanEditPrompts } from "@/lib/use-analytics-access";
import { Textarea } from "@/components/ui/textarea";
import { useSettings } from "@/components/review-settings/context";
import {
  BooleanRow, ChoiceRow, Group, SectionFrame, SettingRow,
} from "@/components/review-settings/field";

const SUPPRESSION_MODES = ["shadow", "on", "off"] as const;

export function LearningSection() {
  const t = useT();
  const { scope } = useSettings();
  // /memories answers 403 below editor, so the link is drawn for those who can use it.
  const canOpenMemories = useCanEditPrompts() === true;
  const memoriesHref = scope.kind === "repo"
    ? `/memories?repo=${encodeURIComponent(scope.slug)}`
    : "/memories";
  const learningHref = `${memoriesHref}${memoriesHref.includes("?") ? "&" : "?"}view=learning`;
  return (
    <SectionFrame
      id="learning"
      title={t("reviewSettings.section.learning")}
      description={t("reviewSettings.learning.desc")}
    >
      <Group
        title={t("reviewSettings.learning.memoriesGroup")}
        description={t("reviewSettings.learning.memoriesGroupHint")}
      >
        <BooleanRow
          field="memories_enabled"
          label={t("reviewSettings.learning.memoriesEnabled")}
          description={t("reviewSettings.learning.memoriesEnabledHint")}
        />
        {canOpenMemories && (
          <p className="px-1 text-sm">
            <Link
              href={memoriesHref}
              className="font-medium text-[var(--color-primary)] underline-offset-4 hover:underline"
            >
              {t("reviewSettings.learning.openMemories")}
            </Link>
          </p>
        )}
      </Group>
      <Group
        title={t("reviewSettings.learning.approvalGroup")}
        description={t("reviewSettings.learning.approvalGroupHint")}
      >
        <BooleanRow
          field="knowledge_approval"
          label={t("reviewSettings.learning.knowledgeApproval")}
          description={t("reviewSettings.learning.knowledgeApprovalHint")}
        />
        <LinesRow
          field="memory_trusted_commenters"
          label={t("reviewSettings.learning.trusted")}
          hint={t("reviewSettings.learning.trustedHint")}
          placeholder={t("reviewSettings.learning.trustedPlaceholder")}
        />
      </Group>
      <Group
        title={t("reviewSettings.learning.feedbackGroup")}
        description={t("reviewSettings.learning.feedbackGroupHint")}
      >
        <ChoiceRow
          field="learning_suppression"
          label={t("reviewSettings.learning.suppression")}
          description={t("reviewSettings.learning.suppressionHint")}
          options={SUPPRESSION_MODES.map((v) => ({
            value: v,
            title: t(`reviewSettings.learning.suppression.${v}`),
            body: t(`reviewSettings.learning.suppression.${v}Body`),
          }))}
          columns={3}
        />
        <LinesRow
          field="learning_excluded_reviewers"
          label={t("reviewSettings.learning.excluded")}
          hint={t("reviewSettings.learning.excludedHint")}
          placeholder={t("reviewSettings.learning.excludedPlaceholder")}
        />
        {canOpenMemories && (
          <p className="px-1 text-sm">
            <Link
              href={learningHref}
              className="font-medium text-[var(--color-primary)] underline-offset-4 hover:underline"
            >
              {t("reviewSettings.learning.openLearning")}
            </Link>
          </p>
        )}
      </Group>
    </SectionFrame>
  );
}

type LinesField = "memory_trusted_commenters" | "learning_excluded_reviewers";

/** A list of identities (login or e-mail), one per line. */
function LinesRow({
  field, label, hint, placeholder,
}: { field: LinesField; label: string; hint: string; placeholder: string }) {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const text = draft.own[field] as string | null;
  const inherited = Array.isArray(inh.values[field]) ? (inh.values[field] as string[]) : [];
  return (
    <SettingRow
      field={field}
      label={label}
      htmlFor={id}
      description={hint}
      describeInherited={(v) => {
        const list = Array.isArray(v) ? (v as string[]) : [];
        return list.length
          ? t("reviewSettings.value.entries", { count: list.length })
          : t("reviewSettings.value.none");
      }}
      control={(
        <div className="space-y-1">
          <Textarea
            id={id}
            rows={4}
            spellCheck={false}
            className="font-mono text-xs"
            value={text ?? ""}
            disabled={!canEdit}
            placeholder={text === null && inherited.length
              ? inherited.join("\n")
              : placeholder}
            aria-describedby={`${id}-hint`}
            onChange={(e) => setOwn(field, e.target.value)}
          />
          <p id={`${id}-hint`} className="text-xs text-[var(--color-muted-foreground)]">
            {text === null && scope.kind === "repo" && inherited.length
              ? t("reviewSettings.filters.inheritsList", { count: inherited.length })
              : t("reviewSettings.filters.onePerLine")}
          </p>
        </div>
      )}
    />
  );
}
