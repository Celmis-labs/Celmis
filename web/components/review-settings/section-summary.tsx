"use client";

/** PR summary: whether the overview is written, where it goes, what happens
 *  to it on the next push and to a description already there, and how the
 *  comment that closes a review looks. */

import { useT } from "@/lib/i18n";
import { useSettings } from "@/components/review-settings/context";
import {
  BooleanRow, ChoiceRow, Group, SectionFrame, TextRow,
} from "@/components/review-settings/field";
import { SUMMARY_INSTRUCTIONS_MAX, effective } from "@/components/review-settings/model";

/** The order a reader compares them in; the server's list decides which
 *  exist (`setting_choices`), this only sorts. */
const ORDER: Record<string, string[]> = {
  summary_target: ["comment", "description"],
  summary_on_new_commits: ["nothing", "append", "replace"],
  summary_existing_description: ["append", "complement", "replace"],
  completed_comment: ["completed", "classic"],
};

export function SummarySection() {
  const t = useT();
  const { scope, draft, inh, meta } = useSettings();
  const on = Boolean(effective(draft, "summary_enabled", scope.kind, inh));
  const target = String(effective(draft, "summary_target", scope.kind, inh) ?? "comment");
  const completed = String(
    effective(draft, "completed_comment", scope.kind, inh) ?? "completed") === "completed";
  const options = (field: keyof typeof ORDER) => {
    const served = meta.choices[field] ?? ORDER[field];
    return [...ORDER[field].filter((v) => served.includes(v)),
      ...served.filter((v) => !ORDER[field].includes(v))].map((v) => ({
      value: v,
      title: t(`reviewSettings.summary.${field}.${v}`),
      body: t(`reviewSettings.summary.${field}.${v}Body`),
    }));
  };
  return (
    <SectionFrame
      id="summary"
      title={t("reviewSettings.section.summary")}
      description={t("reviewSettings.summary.desc")}
    >
      <Group>
        <BooleanRow
          field="summary_enabled"
          label={t("reviewSettings.summary.enabled")}
          description={t("admin.reviewPolicies.detail.summaryEnabledHint")}
        />
        <ChoiceRow
          field="summary_target"
          label={t("reviewSettings.summary.target")}
          options={options("summary_target")}
          columns={2}
          disabled={!on}
        />
        <ChoiceRow
          field="summary_on_new_commits"
          label={t("reviewSettings.summary.onNewCommits")}
          description={t("reviewSettings.summary.onNewCommitsHint")}
          options={options("summary_on_new_commits")}
          disabled={!on}
        />
        <ChoiceRow
          field="summary_existing_description"
          label={t("reviewSettings.summary.existing")}
          description={target === "description"
            ? t("reviewSettings.summary.existingHint")
            : t("reviewSettings.summary.existingOnlyDescription")}
          options={options("summary_existing_description")}
          disabled={!on || target !== "description"}
        />
        <ChoiceRow
          field="completed_comment"
          label={t("reviewSettings.summary.completed")}
          description={t("reviewSettings.summary.completedHint")}
          options={options("completed_comment")}
          columns={2}
        />
        <BooleanRow
          field="commands_guide_enabled"
          label={t("reviewSettings.summary.commandsGuide")}
          description={completed
            ? t("reviewSettings.summary.commandsGuideHint")
            : t("reviewSettings.summary.commandsGuideOnlyCompleted")}
          disabled={!completed}
        />
        <TextRow
          field="summary_instructions"
          label={t("reviewSettings.summary.instructions")}
          description={t("reviewSettings.summary.instructionsHint")}
          max={SUMMARY_INSTRUCTIONS_MAX}
          rows={5}
          placeholder={t("admin.reviewPolicies.detail.summaryInstructionsPlaceholder")}
        />
      </Group>
    </SectionFrame>
  );
}
