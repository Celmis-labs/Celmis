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
  BooleanRow, ChoiceRow, Group, SectionFrame, SettingRow,
} from "@/components/review-settings/field";
import {
  AUTO_PAUSE_PUSHES_MAX, AUTO_PAUSE_PUSHES_MIN, AUTO_PAUSE_WINDOW_MAX,
  AUTO_PAUSE_WINDOW_MIN, ISSUES_MAX_LLM_MAX, ISSUES_MAX_LLM_MIN,
  TITLE_KEYWORDS_MAX, TITLE_KEYWORD_MAX_CHARS, effective, isSet, type InheritableKey,
} from "@/components/review-settings/model";

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
        <TitleKeywordsRow />
        <CadenceRows />
        <ScopeRow />
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

      <Group
        title={t("reviewSettings.general.issuesGroup")}
        description={t("reviewSettings.general.issuesGroupHint")}
      >
        <BooleanRow
          field="issues_auto_resolve"
          label={t("reviewSettings.general.issuesAutoResolve")}
          description={t("reviewSettings.general.issuesAutoResolveHint")}
        />
        <BooleanRow
          field="issues_resolve_llm_verify"
          label={t("reviewSettings.general.issuesLlmVerify")}
          description={t("reviewSettings.general.issuesLlmVerifyHint")}
        />
        <IssuesMaxLlmRow />
        <BooleanRow
          field="issues_announce_resolved"
          label={t("reviewSettings.general.issuesAnnounce")}
          description={t("reviewSettings.general.issuesAnnounceHint")}
        />
      </Group>
    </SectionFrame>
  );
}

/** How many model calls one recheck of a branch may spend on the issues
 *  backlog. 0 keeps only the checks that need no model. */
function IssuesMaxLlmRow() {
  const t = useT();
  const { draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = draft.own.issues_resolve_max_llm as number | null;
  return (
    <SettingRow
      field="issues_resolve_max_llm"
      label={t("reviewSettings.general.issuesMaxLlm")}
      htmlFor={id}
      description={t("reviewSettings.general.issuesMaxLlmHint")}
      describeInherited={(v) => String(v ?? "")}
      control={(
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={ISSUES_MAX_LLM_MIN}
          max={ISSUES_MAX_LLM_MAX}
          className="w-32 tabular-nums"
          placeholder={String(inh.values.issues_resolve_max_llm ?? "")}
          value={own ?? ""}
          disabled={!canEdit}
          onChange={(e) => {
            const raw = e.target.value.trim();
            if (!raw) return setOwn("issues_resolve_max_llm", null);
            const n = Math.round(Number(raw));
            if (!Number.isFinite(n)) return;
            setOwn(
              "issues_resolve_max_llm",
              Math.min(ISSUES_MAX_LLM_MAX, Math.max(ISSUES_MAX_LLM_MIN, n)),
            );
          }}
        />
      )}
    />
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

// The order a reader compares the cadences in; the server's list decides
//  which exist (`setting_choices`), this only sorts.
const CADENCE_ORDER = ["automatic", "auto_pause", "manual"];

//
// Review cadence: every push, every push until a PR gets noisy, or only when
// asked. The two auto-pause numbers show only while the cadence in force is
// auto_pause — they mean nothing otherwise.
// /
function CadenceRows() {
  const t = useT();
  const { scope, draft, inh, meta } = useSettings();
  const cadence = String(effective(draft, "review_cadence", scope.kind, inh) ?? "automatic");
  const served = meta.choices.review_cadence ?? CADENCE_ORDER;
  const options = [
    ...CADENCE_ORDER.filter((v) => served.includes(v)),
    ...served.filter((v) => !CADENCE_ORDER.includes(v)),
  ].map((v) => ({
    value: v,
    title: t(`reviewSettings.cadence.choice.${v}`),
    body: t(`reviewSettings.cadence.choice.${v}Body`),
  }));
  return (
    <>
      <ChoiceRow
        field="review_cadence"
        label={t("reviewSettings.cadence.label")}
        description={t("reviewSettings.cadence.hint")}
        options={options}
        columns={3}
      />
      {cadence === "auto_pause" && (
        <>
          <NumberRow
            field="auto_pause_pushes"
            label={t("reviewSettings.cadence.pushes")}
            description={t("reviewSettings.cadence.pushesHint")}
            min={AUTO_PAUSE_PUSHES_MIN}
            max={AUTO_PAUSE_PUSHES_MAX}
          />
          <NumberRow
            field="auto_pause_window_minutes"
            label={t("reviewSettings.cadence.window")}
            description={t("reviewSettings.cadence.windowHint")}
            min={AUTO_PAUSE_WINDOW_MIN}
            max={AUTO_PAUSE_WINDOW_MAX}
          />
        </>
      )}
    </>
  );
}

// What a review reads: only the commits since the last reviewed one, or the
// whole pull request each time. The server's list decides which exist.
const SCOPE_ORDER = ["incremental", "full"];

function ScopeRow() {
  const t = useT();
  const { meta } = useSettings();
  const served = meta.choices.review_scope ?? SCOPE_ORDER;
  const options = [
    ...SCOPE_ORDER.filter((v) => served.includes(v)),
    ...served.filter((v) => !SCOPE_ORDER.includes(v)),
  ].map((v) => ({
    value: v,
    title: t(`reviewSettings.scope.choice.${v}`),
    body: t(`reviewSettings.scope.choice.${v}Body`),
  }));
  return (
    <ChoiceRow
      field="review_scope"
      label={t("reviewSettings.scope.label")}
      description={t("reviewSettings.scope.hint")}
      options={options}
      columns={2}
    />
  );
}

// A whole-number setting in the server's range. What is typed is kept as text
// (a minimum of 2 must not turn the first "1" of "10" into 2); the value
// reaches the draft while it is in range and is clamped when the box is left.
// An empty box is "inherit".
function NumberRow({
  field, label, description, min, max,
}: {
  field: Extract<InheritableKey, "auto_pause_pushes" | "auto_pause_window_minutes">;
  label: string;
  description: string;
  min: number;
  max: number;
}) {
  const { draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = draft.own[field] as number | null;
  const [typing, setTyping] = useState<string | null>(null);
  const commit = (raw: string, clamp: boolean) => {
    const text = raw.trim();
    if (!text) return setOwn(field, null);
    const n = Math.round(Number(text));
    if (!Number.isFinite(n)) return;
    if (clamp) return setOwn(field, Math.min(max, Math.max(min, n)));
    if (n >= min && n <= max) setOwn(field, n);
  };
  return (
    <SettingRow
      field={field}
      label={label}
      htmlFor={id}
      description={description}
      describeInherited={(v) => String(v ?? "")}
      control={(
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={min}
          max={max}
          className="w-32 tabular-nums"
          placeholder={String(inh.values[field] ?? "")}
          value={typing ?? own ?? ""}
          disabled={!canEdit}
          onChange={(e) => {
            setTyping(e.target.value);
            commit(e.target.value, false);
          }}
          onBlur={() => {
            if (typing !== null) commit(typing, true);
            setTyping(null);
          }}
        />
      )}
    />
  );
}

//
// Ignored title keywords: a pull request whose title contains one of them
// (any case) is not reviewed by an automatic trigger — "WIP", "[skip review]",
// "Revert". A person's request still reviews it. Added with Enter or the
// button; several at once with commas.
// /
function TitleKeywordsRow() {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const inputId = useId();
  const [text, setText] = useState("");
  const set = isSet(draft, "ignored_title_keywords", scope.kind);
  const shown = (effective(draft, "ignored_title_keywords", scope.kind, inh) as string[] | null) ?? [];
  const typed = text.split(",").map((v) => v.trim()).filter(Boolean);
  const tooLong = typed.some((v) => v.length > TITLE_KEYWORD_MAX_CHARS);
  const full = shown.length >= TITLE_KEYWORDS_MAX;

  const write = (next: string[]) => setOwn("ignored_title_keywords", next);
  const add = () => {
    if (!typed.length || tooLong) return;
    const known = new Set(shown.map((v) => v.toLowerCase()));
    const fresh = typed.filter((v) => {
      const folded = v.toLowerCase();
      if (known.has(folded)) return false;
      known.add(folded);
      return true;
    });
    write([...shown, ...fresh].slice(0, TITLE_KEYWORDS_MAX));
    setText("");
  };
  const remove = (entry: string) => write(shown.filter((e) => e !== entry));

  return (
    <SettingRow
      field="ignored_title_keywords"
      label={t("reviewSettings.titleKeywords.label")}
      htmlFor={inputId}
      description={t("reviewSettings.titleKeywords.hint")}
      describeInherited={(v) => {
        const list = Array.isArray(v) ? (v as string[]) : [];
        return list.length ? list.join(", ") : t("reviewSettings.titleKeywords.none");
      }}
      control={(
        <div className="space-y-3">
          <p className="text-xs" aria-live="polite">
            {shown.length === 0
              ? t("reviewSettings.titleKeywords.summaryNone")
              : t("reviewSettings.titleKeywords.summary", { keywords: shown.join(", ") })}
            {scope.kind === "repo" && set && shown.length === 0 && (
              <> {t("reviewSettings.titleKeywords.overrideNone")}</>
            )}
          </p>
          {shown.length > 0 && (
            <ul className="flex flex-wrap gap-1.5">
              {shown.map((entry) => (
                <li key={entry}>
                  <span className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-secondary)] py-0.5 pl-2 pr-0.5 font-mono text-xs">
                    {entry}
                    <button
                      type="button"
                      disabled={!canEdit}
                      onClick={() => remove(entry)}
                      aria-label={t("reviewSettings.titleKeywords.remove", { entry })}
                      className="grid size-5 place-items-center rounded opacity-70 hover:bg-[var(--color-accent)] hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none"
                    >
                      <XIcon className="h-3 w-3" aria-hidden />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          )}
          <div className="flex flex-col gap-2 @md:flex-row">
            <Input
              id={inputId}
              className="flex-1 font-mono text-xs"
              placeholder="WIP, [skip review], Revert"
              value={text}
              maxLength={TITLE_KEYWORD_MAX_CHARS * 5}
              disabled={!canEdit || full}
              aria-invalid={tooLong ? true : undefined}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  add();
                }
              }}
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!canEdit || !typed.length || tooLong || full}
              onClick={add}
            >
              {t("reviewSettings.titleKeywords.add")}
            </Button>
          </div>
          {(tooLong || full) && (
            <p className="text-xs text-[var(--color-destructive)]" role="alert">
              {tooLong
                ? t("reviewSettings.titleKeywords.tooLong", { max: TITLE_KEYWORD_MAX_CHARS })
                : t("reviewSettings.titleKeywords.full", { max: TITLE_KEYWORDS_MAX })}
            </p>
          )}
        </div>
      )}
    />
  );
}
