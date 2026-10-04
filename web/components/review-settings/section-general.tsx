"use client";

/** General: whether and when a pull request is reviewed, and what the
 *  review does on the provider besides commenting. */

import { useId, useState } from "react";
import { CheckIcon, MinusIcon, XIcon } from "lucide-react";

import { reviewPoliciesApi } from "@/lib/api";
import {
  matchBranch, parsePatternInput, patternErrorKey, splitPatterns,
} from "@/lib/branch-patterns";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { cn } from "@/lib/utils";
import { BranchCombobox, toBranchResult } from "@/components/branch-combobox";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useSettings } from "@/components/review-settings/context";
import {
  BooleanRow, Group, SectionFrame, SettingRow,
} from "@/components/review-settings/field";
import { effective, isSet } from "@/components/review-settings/model";

/** A language code's own name ("uk" → "українська (uk)"). */
export function languageName(code: string): string {
  try {
    const name = new Intl.DisplayNames([code], { type: "language" }).of(code);
    return name && name !== code ? `${name} (${code})` : code;
  } catch {
    return code;
  }
}

export function GeneralSection() {
  const t = useT();
  const { scope, draft, patch, canEdit, setOwn, inh, meta } = useSettings();
  const enabledId = useId();
  const languageId = useId();
  const language = String(effective(draft, "review_language", scope.kind, inh) ?? "en");

  return (
    <SectionFrame
      id="general"
      title={t("reviewSettings.section.general")}
      description={scope.kind === "repo"
        ? t("reviewSettings.general.descRepo")
        : t("reviewSettings.general.descGlobal")}
    >
      {scope.kind === "repo" && (
        <Group title={t("reviewSettings.general.repoGroup")}>
          <SettingRow
            label={t("reviewSettings.general.enabled")}
            description={t("reviewSettings.general.enabledHint")}
            htmlFor={enabledId}
            inline
            control={(
              <Switch
                id={enabledId}
                checked={draft.enabled}
                disabled={!canEdit}
                onCheckedChange={(v) => patch({ enabled: v })}
              />
            )}
          />
          <SettingRow
            label={t("reviewSettings.general.department")}
            description={t("reviewSettings.general.departmentHint")}
            htmlFor={`${enabledId}-dept`}
            control={(
              <Input
                id={`${enabledId}-dept`}
                className="w-full @md:w-80"
                maxLength={128}
                value={draft.department}
                disabled={!canEdit}
                placeholder={t("reviewSettings.general.departmentPlaceholder")}
                onChange={(e) => patch({ department: e.target.value })}
              />
            )}
          />
        </Group>
      )}

      <Group title={t("reviewSettings.general.whenGroup")}>
        <TargetBranchesRow />
        <BooleanRow
          field="run_on_drafts"
          label={t("reviewSettings.general.runOnDrafts")}
          description={t("reviewSettings.general.runOnDraftsHint")}
        />
      </Group>

      <Group
        title={t("reviewSettings.general.actionsGroup")}
        description={t("reviewSettings.general.actionsGroupHint")}
      >
        <BooleanRow
          field="approve_when_clean"
          label={t("reviewSettings.general.approveWhenClean")}
          description={t("reviewSettings.general.approveWhenCleanHint")}
        />
        <BooleanRow
          field="request_changes_on_critical"
          label={t("reviewSettings.general.requestChanges")}
          description={t("reviewSettings.general.requestChangesHint")}
        />
        <BooleanRow
          field="status_feedback"
          label={t("reviewSettings.general.statusFeedback")}
          description={t("reviewSettings.general.statusFeedbackHint")}
        />
        <BooleanRow
          field="committable_suggestions"
          label={t("reviewSettings.general.committable")}
          description={t("reviewSettings.general.committableHint")}
        />
        <BooleanRow
          field="started_comment_enabled"
          label={t("reviewSettings.general.startedComment")}
          description={t("reviewSettings.general.startedCommentHint")}
        />
        <SettingRow
          field="review_language"
          label={t("reviewSettings.general.language")}
          description={t("reviewSettings.general.languageHint")}
          htmlFor={languageId}
          describeInherited={(v) => languageName(String(v || "en"))}
          control={(
            <Select
              id={languageId}
              className="w-full @md:w-80"
              value={language}
              disabled={!canEdit}
              onChange={(v) => setOwn("review_language", v)}
              options={[...new Set(["en", ...meta.languages])].map((code) => ({
                value: code, label: languageName(code),
              }))}
            />
          )}
        />
      </Group>
    </SectionFrame>
  );
}

/**
 * Target branches: names, globs and `!` exclusions (the gate's rule, in
 * lib/branch-patterns.ts). A repository can pick names from its provider;
 * anything can be typed — a branch that does not exist yet, a pattern.
 * Under the list, a tester answers "would a PR into X be reviewed?".
 */
function TargetBranchesRow() {
  const t = useT();
  const token = useToken();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const inputId = useId();
  const [text, setText] = useState("");
  const [probe, setProbe] = useState("");
  const set = isSet(draft, "target_branches", scope.kind);
  const shown = (effective(draft, "target_branches", scope.kind, inh) as string[] | null) ?? [];
  const { includes, excludes } = splitPatterns(shown);
  const typed = parsePatternInput(text);
  const typedError = typed.map(patternErrorKey).find(Boolean) ?? null;

  const write = (next: string[]) => setOwn("target_branches", next);
  const add = (entries: string[]) => {
    if (!entries.length || entries.some((e) => patternErrorKey(e))) return;
    write([...shown, ...entries.filter((e) => !shown.includes(e))]);
    setText("");
  };
  const remove = (entry: string) => write(shown.filter((e) => e !== entry));

  const result = probe.trim() ? matchBranch(probe.trim(), shown) : null;

  const chip = (entry: string, negated: boolean) => (
    <li key={entry}>
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-md border py-0.5 pl-2 pr-0.5 font-mono text-xs",
          negated
            ? "border-[var(--color-destructive)]/30 bg-[var(--color-destructive)]/5 text-[var(--color-destructive)]"
            : "border-[var(--color-border)] bg-[var(--color-secondary)]",
        )}
      >
        {entry}
        <button
          type="button"
          disabled={!canEdit}
          onClick={() => remove(entry)}
          aria-label={t("reviewSettings.branches.remove", { entry })}
          className="grid size-5 place-items-center rounded opacity-70 hover:bg-[var(--color-accent)] hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none"
        >
          <XIcon className="h-3 w-3" aria-hidden />
        </button>
      </span>
    </li>
  );

  return (
    <SettingRow
      field="target_branches"
      label={t("reviewSettings.branches.label")}
      htmlFor={inputId}
      description={t("reviewSettings.branches.hint")}
      describeInherited={(v) => {
        const list = Array.isArray(v) ? (v as string[]) : [];
        return list.length ? list.join(", ") : t("reviewSettings.branches.every");
      }}
      control={(
        <div className="space-y-3">
          <p className="text-xs" aria-live="polite">
            {shown.length === 0
              ? t("reviewSettings.branches.summaryAll")
              : includes.length === 0
                ? t("reviewSettings.branches.summaryAllExcept", { excludes: excludes.join(", ") })
                : excludes.length === 0
                  ? t("reviewSettings.branches.summaryOnly", { includes: includes.join(", ") })
                  : t("reviewSettings.branches.summaryOnlyExcept", {
                    includes: includes.join(", "), excludes: excludes.join(", "),
                  })}
            {scope.kind === "repo" && set && shown.length === 0 && (
              <> {t("reviewSettings.branches.overrideAll")}</>
            )}
          </p>
          {shown.length > 0 && (
            <div className="grid gap-2 @md:grid-cols-2">
              <div>
                <p className="mb-1 text-[11px] font-medium text-[var(--color-muted-foreground)]">
                  {t("reviewSettings.branches.includes")}
                </p>
                {includes.length ? (
                  <ul className="flex flex-wrap gap-1.5">{includes.map((e) => chip(e, false))}</ul>
                ) : (
                  <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.branches.anyBranch")}</p>
                )}
              </div>
              <div>
                <p className="mb-1 text-[11px] font-medium text-[var(--color-muted-foreground)]">
                  {t("reviewSettings.branches.excludes")}
                </p>
                {excludes.length ? (
                  <ul className="flex flex-wrap gap-1.5">
                    {excludes.map((body) => chip(`!${body}`, true))}
                  </ul>
                ) : (
                  <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.branches.none")}</p>
                )}
              </div>
            </div>
          )}
          <div className="flex flex-col gap-2 @md:flex-row">
            <Input
              id={inputId}
              className="flex-1 font-mono text-xs"
              placeholder="main,release/*,!legacy"
              value={text}
              disabled={!canEdit}
              aria-invalid={typedError ? true : undefined}
              aria-describedby={`${inputId}-help`}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  add(typed);
                }
              }}
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!canEdit || !typed.length || !!typedError}
              onClick={() => add(typed)}
            >
              {t("reviewSettings.branches.add")}
            </Button>
          </div>
          {scope.kind === "repo" && (
            <div className="space-y-1">
              <label htmlFor={`${inputId}-pick`} className="text-xs text-[var(--color-muted-foreground)]">
                {t("reviewSettings.branches.pick")}
              </label>
              <BranchCombobox
                id={`${inputId}-pick`}
                value=""
                onChange={(raw) => add(parsePatternInput(raw))}
                search={(q) => reviewPoliciesApi.branches(token!, scope.slug, q).then(toBranchResult)}
                queryKey={["review-policies", "branches", scope.slug]}
                selected={includes}
                keepOpenOnSelect
                allowCustom
                disabled={!token || !canEdit}
                placeholder={t("reviewSettings.branches.pickPlaceholder")}
                className="w-full @md:max-w-md"
              />
            </div>
          )}
          <p id={`${inputId}-help`} className={cn(
            "text-xs",
            typedError ? "text-[var(--color-destructive)]" : "text-[var(--color-muted-foreground)]",
          )} role={typedError ? "alert" : undefined}>
            {typedError ? t(typedError) : t("reviewSettings.branches.syntax")}
          </p>
          <div className="flex flex-col gap-2 rounded-md bg-[var(--color-muted)]/50 p-2.5 @md:flex-row @md:items-center">
            <label htmlFor={`${inputId}-probe`} className="shrink-0 text-xs font-medium">
              {t("reviewSettings.branches.test")}
            </label>
            <Input
              id={`${inputId}-probe`}
              className="h-9 flex-1 font-mono text-xs @md:max-w-56"
              placeholder="release/2.3"
              value={probe}
              onChange={(e) => setProbe(e.target.value)}
            />
            <p className="flex min-h-5 items-center gap-1.5 text-xs" aria-live="polite">
              {result && (result.targeted ? (
                <>
                  <CheckIcon className="h-3.5 w-3.5 text-[var(--color-success)]" aria-hidden />
                  {result.pattern
                    ? t("reviewSettings.branches.reviewedVia", { pattern: result.pattern })
                    : t("reviewSettings.branches.reviewed")}
                </>
              ) : (
                <>
                  {result.reason === "excluded"
                    ? <XIcon className="h-3.5 w-3.5 text-[var(--color-destructive)]" aria-hidden />
                    : <MinusIcon className="h-3.5 w-3.5 text-[var(--color-muted-foreground)]" aria-hidden />}
                  {result.reason === "excluded"
                    ? t("reviewSettings.branches.skippedExcluded", { pattern: result.pattern ?? "" })
                    : t("reviewSettings.branches.skippedUnmatched")}
                </>
              ))}
            </p>
          </div>
        </div>
      )}
    />
  );
}
