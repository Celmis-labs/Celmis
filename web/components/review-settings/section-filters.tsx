"use client";

/** Filters: which findings become comments, how many, and which files and
 *  rules the review never looks at. */

import { useId } from "react";

import { globError, globLines } from "@/lib/ignore-globs";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { SEVERITY_TEXT, SeverityIcon } from "@/components/ui/status";
import { Textarea } from "@/components/ui/textarea";
import { useSettings } from "@/components/review-settings/context";
import {
  BooleanRow, Group, SectionFrame, SettingRow,
} from "@/components/review-settings/field";
import {
  MAX_INLINE_MAX, MAX_INLINE_MIN, SEVERITY_STEPS, effective, type InheritableKey,
} from "@/components/review-settings/model";

export function FiltersSection() {
  const t = useT();
  return (
    <SectionFrame
      id="filters"
      title={t("reviewSettings.section.filters")}
      description={t("reviewSettings.filters.desc")}
    >
      <Group title={t("reviewSettings.filters.commentsGroup")}>
        <SeverityRow />
        <MaxInlineRow />
        <BooleanRow
          field="apply_filters_to_rules"
          label={t("reviewSettings.filters.applyToRules")}
          description={t("reviewSettings.filters.applyToRulesHint")}
        />
      </Group>
      <Group title={t("reviewSettings.filters.scopeGroup")}>
        <LinesRow
          field="ignore_globs"
          label={t("reviewSettings.filters.ignore")}
          description={t("reviewSettings.filters.ignoreHint")}
          placeholder={"docs/**\n*.snap\nmigrations/*.py"}
          validate
        />
        <LinesRow
          field="suppressed_rules"
          label={t("reviewSettings.filters.suppressed")}
          description={t("reviewSettings.filters.suppressedHint")}
          placeholder={"quality.todo\nsec.cve-GHSA-xxxx"}
        />
      </Group>
    </SectionFrame>
  );
}

/**
 * The minimum severity as a four-step scale. A native range input, so the
 * arrow keys, Home/End and a screen reader's value announcement all work;
 * the step names sit under it and are buttons too.
 */
function SeverityRow() {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const value = String(effective(draft, "comment_min_severity", scope.kind, inh) ?? "info");
  const index = Math.max(0, SEVERITY_STEPS.indexOf(value as (typeof SEVERITY_STEPS)[number]));
  const label = (s: string) => t(`reviewSettings.severity.${s}`);
  return (
    <SettingRow
      field="comment_min_severity"
      label={t("reviewSettings.filters.minSeverity")}
      htmlFor={id}
      description={t("review.settings.thresholdHint")}
      describeInherited={(v) => label(String(v || "info"))}
      control={(
        <div className="max-w-xl space-y-2">
          <Slider
            id={id}
            min={0}
            max={SEVERITY_STEPS.length - 1}
            step={1}
            value={index}
            disabled={!canEdit}
            aria-valuetext={label(SEVERITY_STEPS[index])}
            onValueChange={(v) => setOwn("comment_min_severity", SEVERITY_STEPS[v])}
          />
          <div className="grid grid-cols-4 text-xs" aria-hidden>
            {SEVERITY_STEPS.map((step, i) => (
              <button
                key={step}
                type="button"
                tabIndex={-1}
                disabled={!canEdit}
                onClick={() => setOwn("comment_min_severity", step)}
                className={cn(
                  "rounded px-1 py-0.5 transition-colors",
                  i === 0 ? "text-left" : i === SEVERITY_STEPS.length - 1 ? "text-right" : "text-center",
                  i === index
                    ? "font-medium text-[var(--color-foreground)]"
                    : i > index
                      ? "text-[var(--color-foreground)]/70"
                      : "text-[var(--color-muted-foreground)] line-through decoration-[var(--color-muted-foreground)]/50",
                )}
              >
                <span className="inline-flex items-center gap-1">
                  <SeverityIcon severity={step} className={cn(i >= index && SEVERITY_TEXT[step])} />
                  {label(step)}
                </span>
              </button>
            ))}
          </div>
          <p className="text-xs text-[var(--color-muted-foreground)]" aria-live="polite">
            {t(`reviewSettings.severity.posts.${SEVERITY_STEPS[index]}`)}
          </p>
        </div>
      )}
    />
  );
}

function MaxInlineRow() {
  const t = useT();
  const { draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = draft.own.max_inline_comments as number | null;
  return (
    <SettingRow
      field="max_inline_comments"
      label={t("reviewSettings.filters.maxInline")}
      htmlFor={id}
      description={t("admin.reviewPolicies.detail.maxInlineHint")}
      describeInherited={(v) => String(v ?? "")}
      control={(
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={MAX_INLINE_MIN}
          max={MAX_INLINE_MAX}
          className="w-32 tabular-nums"
          placeholder={String(inh.values.max_inline_comments ?? "")}
          value={own ?? ""}
          disabled={!canEdit}
          onChange={(e) => {
            const raw = e.target.value.trim();
            if (!raw) return setOwn("max_inline_comments", null);
            const n = Math.round(Number(raw));
            if (!Number.isFinite(n)) return;
            setOwn("max_inline_comments", Math.min(MAX_INLINE_MAX, Math.max(MAX_INLINE_MIN, n)));
          }}
        />
      )}
    />
  );
}

/**
 * A list edited one entry per line. Null (inherit) shows the inherited list
 * as the placeholder; any text, an empty box included at a repository, is
 * this scope's own list — "nothing extra" is a real answer there.
 */
function LinesRow({
  field, label, description, placeholder, validate,
}: {
  field: Extract<InheritableKey, "ignore_globs" | "suppressed_rules">;
  label: string;
  description: string;
  placeholder: string;
  validate?: boolean;
}) {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const text = draft.own[field] as string | null;
  const inherited = Array.isArray(inh.values[field]) ? (inh.values[field] as string[]) : [];
  const error = validate && text ? globError(globLines(text)) : null;
  return (
    <SettingRow
      field={field}
      label={label}
      htmlFor={id}
      description={description}
      describeInherited={(v) => {
        const list = Array.isArray(v) ? (v as string[]) : [];
        return list.length ? t("reviewSettings.value.entries", { count: list.length }) : t("reviewSettings.value.none");
      }}
      control={(
        <div className="space-y-1">
          <Textarea
            id={id}
            rows={6}
            spellCheck={false}
            className="font-mono text-xs"
            value={text ?? ""}
            disabled={!canEdit}
            placeholder={text === null && inherited.length ? inherited.join("\n") : placeholder}
            aria-invalid={error ? true : undefined}
            aria-describedby={`${id}-hint`}
            onChange={(e) => setOwn(field, e.target.value)}
          />
          {error ? (
            <p id={`${id}-hint`} role="alert" className="text-xs text-[var(--color-destructive)]">
              {t(error.key, { line: error.line })}
            </p>
          ) : (
            <p id={`${id}-hint`} className="text-xs text-[var(--color-muted-foreground)]">
              {text === null && scope.kind === "repo" && inherited.length
                ? t("reviewSettings.filters.inheritsList", { count: inherited.length })
                : t("reviewSettings.filters.onePerLine")}
            </p>
          )}
        </div>
      )}
    />
  );
}
