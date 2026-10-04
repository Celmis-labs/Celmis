"use client";

/**
 * The building blocks every section is made of: a section frame, a group of
 * rows, and one row per setting that says whether this scope sets it or
 * inherits it, from where, and what resetting gives back.
 *
 * Inherited vs overridden is the whole point of this page, so it is decided
 * once, here, from the model — a row never works it out for itself.
 */

import { useId, useState } from "react";
import { RotateCcwIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Markdown } from "@/components/markdown";
import { Badge } from "@/components/ui/badge";
import { OptionCard, OptionCardGroup } from "@/components/ui/option-card";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { OverriddenPill } from "@/components/ui/status";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useSettings } from "@/components/review-settings/context";
import {
  canonical, effective, isSet, type InheritableKey,
} from "@/components/review-settings/model";

// ─── frame ───────────────────────────────────────────────────────────

export function SectionFrame({
  id, title, description, children,
}: {
  id: string;
  title: string;
  description: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={`${id}-title`} className="space-y-6">
      <header className="space-y-1">
        <h2 id={`${id}-title`} className="text-lg font-semibold tracking-tight">{title}</h2>
        <p className="max-w-[70ch] text-sm text-[var(--color-muted-foreground)]">{description}</p>
      </header>
      {children}
    </section>
  );
}

/** A titled group of rows: one bordered surface, rows divided by hairlines.
 *  role="group" named by its title, so a screen reader announces the group
 *  with each control inside it. */
export function Group({
  title, description, children, className, action,
}: {
  title?: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  action?: React.ReactNode;
}) {
  const titleId = useId();
  return (
    <div
      role="group"
      aria-labelledby={title ? titleId : undefined}
      className={cn("min-w-0 space-y-2", className)}
    >
      {(title || action) && (
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div className="min-w-0">
            {title && <h3 id={titleId} className="text-sm font-medium">{title}</h3>}
            {description && (
              <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">{description}</p>
            )}
          </div>
          {action}
        </div>
      )}
      <div className="divide-y divide-[var(--color-border)] rounded-[var(--radius)] border border-[var(--color-border)] bg-[var(--color-card)]">
        {children}
      </div>
    </div>
  );
}

// ─── origin ──────────────────────────────────────────────────────────

/** "Overridden" / "Inherited · Workspace" at a repository; "Workspace
 *  default" / "Built-in" at Global. */
export function OriginBadge({ field, set: forced }: { field?: InheritableKey; set?: boolean }) {
  const t = useT();
  const { scope, draft, inh } = useSettings();
  const set = forced ?? (field ? isSet(draft, field, scope.kind) : false);
  if (scope.kind === "workspace") {
    return set ? (
      <OverriddenPill label={t("reviewSettings.origin.workspaceSet")} />
    ) : (
      <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
        {t("reviewSettings.origin.builtin")}
      </Badge>
    );
  }
  if (set) {
    return <OverriddenPill label={t("reviewSettings.origin.overridden")} />;
  }
  const from = field ? inh.sources[field] : "workspace";
  return (
    <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
      {from === "workspace"
        ? t("reviewSettings.origin.inheritedWorkspace")
        : t("reviewSettings.origin.inheritedBuiltin")}
    </Badge>
  );
}

/** The reset icon beside an overridden row. Named for what it does at this
 *  scope — "use the workspace value" is not "use the built-in". */
export function ResetButton({
  onClick, label, disabled,
}: {
  onClick: () => void;
  label?: string;
  disabled?: boolean;
}) {
  const t = useT();
  const { scope } = useSettings();
  const name = label ?? (scope.kind === "workspace"
    ? t("reviewSettings.reset.toBuiltin")
    : t("reviewSettings.reset.toInherited"));
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={name}
      title={name}
      className="inline-grid size-8 shrink-0 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none disabled:opacity-40"
    >
      <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
    </button>
  );
}

// ─── one setting ─────────────────────────────────────────────────────

/**
 * One setting: label, origin, reset, description, control. `inline` puts the
 * control on the right (switches); otherwise it sits under the text.
 * `describeInherited` turns the inherited value into words for the line that
 * says what a reset would give back.
 */
export function SettingRow({
  field, label, description, control, inline = false, describeInherited, htmlFor, extra,
  set: forcedSet, onReset,
}: {
  field?: InheritableKey;
  label: string;
  description?: React.ReactNode;
  control: React.ReactNode;
  inline?: boolean;
  describeInherited?: (value: unknown) => string;
  htmlFor?: string;
  extra?: React.ReactNode;
  /** For rows that are not one inheritable field (a whole agent list). */
  set?: boolean;
  onReset?: () => void;
}) {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const set = forcedSet ?? (field ? isSet(draft, field, scope.kind) : false);
  const reset = onReset ?? (field ? () => setOwn(field, null) : undefined);
  const inherited = field ? inh.values[field] : undefined;
  const labelId = useId();
  return (
    <div className="px-4 py-3.5" data-field={field}>
      <div className={cn("flex gap-4", inline ? "items-start justify-between" : "flex-col gap-2")}>
        <div className="min-w-0 flex-1">
          <div className="flex min-h-8 flex-wrap items-center gap-x-2 gap-y-1">
            {htmlFor ? (
              <label id={labelId} htmlFor={htmlFor} className="text-sm font-medium">{label}</label>
            ) : (
              <span id={labelId} className="text-sm font-medium">{label}</span>
            )}
            {(field || forcedSet !== undefined) && <OriginBadge field={field} set={forcedSet} />}
            {set && reset && canEdit && <ResetButton onClick={reset} />}
          </div>
          {description && (
            <p className="mt-0.5 max-w-[70ch] text-xs text-[var(--color-muted-foreground)]">{description}</p>
          )}
          {set && describeInherited && field && (
            <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
              {scope.kind === "workspace"
                ? t("reviewSettings.origin.builtinIs", { value: describeInherited(inherited) })
                : inh.sources[field] === "workspace"
                  ? t("reviewSettings.origin.workspaceIs", { value: describeInherited(inherited) })
                  : t("reviewSettings.origin.builtinIs", { value: describeInherited(inherited) })}
            </p>
          )}
        </div>
        {inline ? <div className="shrink-0 pt-1.5">{control}</div> : null}
      </div>
      {!inline && <div className="mt-2">{control}</div>}
      {extra}
    </div>
  );
}

/** A boolean setting as a switch row. Flipping always writes this scope's
 *  own value; the reset icon is how it goes back to inheriting. */
export function BooleanRow({
  field, label, description, disabled,
}: {
  field: InheritableKey;
  label: string;
  description?: React.ReactNode;
  disabled?: boolean;
}) {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const on = Boolean(effective(draft, field, scope.kind, inh));
  return (
    <SettingRow
      field={field}
      label={label}
      description={description}
      htmlFor={id}
      inline
      describeInherited={(v) => (v ? t("reviewSettings.value.on") : t("reviewSettings.value.off"))}
      control={(
        <Switch
          id={id}
          checked={on}
          disabled={!canEdit || disabled}
          onCheckedChange={(v) => setOwn(field, v)}
        />
      )}
    />
  );
}

// ─── choice cards ────────────────────────────────────────────────────

/**
 * One of a closed set, as selectable cards — native radios underneath, so
 * arrow keys move between them and a screen reader says "3 of 3".
 */
export function ChoiceCards({
  name, value, options, onChange, disabled, columns = 3, label,
}: {
  name: string;
  value: string;
  options: { value: string; title: string; body: string; badge?: string }[];
  onChange: (v: string) => void;
  disabled?: boolean;
  columns?: 2 | 3;
  label: string;
}) {
  return (
    <OptionCardGroup label={label} columns={columns}>
      {options.map((o) => (
        <OptionCard
          key={o.value}
          type="radio"
          name={name}
          value={o.value}
          checked={o.value === value}
          disabled={disabled}
          onCheckedChange={() => onChange(o.value)}
          title={o.title}
          description={o.body}
          meta={o.badge ? (
            <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
              {o.badge}
            </Badge>
          ) : undefined}
        />
      ))}
    </OptionCardGroup>
  );
}

/** A closed-choice setting as a row of cards. */
export function ChoiceRow({
  field, label, description, options, disabled, columns,
}: {
  field: InheritableKey;
  label: string;
  description?: React.ReactNode;
  options: { value: string; title: string; body: string }[];
  disabled?: boolean;
  columns?: 2 | 3;
}) {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const current = String(effective(draft, field, scope.kind, inh) ?? options[0]?.value ?? "");
  const builtin = String(inh.values[field] ?? "");
  return (
    <SettingRow
      field={field}
      label={label}
      description={description}
      describeInherited={(v) => options.find((o) => o.value === v)?.title ?? String(v ?? "")}
      control={(
        <div id={id}>
          <ChoiceCards
            name={`${field}-${id}`}
            value={current}
            label={label}
            columns={columns}
            disabled={!canEdit || disabled}
            onChange={(v) => setOwn(field, v)}
            options={options.map((o) => ({
              ...o,
              badge: o.value === builtin ? t("reviewSettings.value.inheritedChoice") : undefined,
            }))}
          />
        </div>
      )}
    />
  );
}

// ─── text ────────────────────────────────────────────────────────────

/** "123 / 2000", turning to a warning colour near the limit. Announced
 *  politely only once it is close, so typing is not narrated. */
export function CharCount({ count, max }: { count: number; max: number }) {
  const near = count > max * 0.9;
  return (
    <span
      aria-live={near ? "polite" : "off"}
      className={cn(
        "tabular-nums text-xs",
        count > max
          ? "text-[var(--color-destructive)]"
          : near
            ? "text-[var(--color-warning)]"
            : "text-[var(--color-muted-foreground)]",
      )}
    >
      {count.toLocaleString()} / {max.toLocaleString()}
    </span>
  );
}

/**
 * Markdown text with Write / Preview, and a character count. The preview
 * renders what the agents are handed; it is not a second editor.
 */
export function MarkdownEditor({
  id, value, onChange, max, placeholder, disabled, rows = 8, describedBy,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  max: number;
  placeholder?: string;
  disabled?: boolean;
  rows?: number;
  describedBy?: string;
}) {
  const t = useT();
  const [mode, setMode] = useState<"write" | "preview">("write");
  return (
    <div className="rounded-md border border-[var(--color-input)] focus-within:ring-2 focus-within:ring-[var(--color-ring)]">
      <div className="flex items-center justify-between gap-2 border-b border-[var(--color-border)] px-2 py-1">
        <SegmentedControl
          label={t("reviewSettings.editor.mode")}
          semantics="tabs"
          size="sm"
          value={mode}
          onValueChange={setMode}
          segments={[
            { value: "write", label: t("reviewSettings.editor.write") },
            { value: "preview", label: t("reviewSettings.editor.preview") },
          ]}
        />
        <CharCount count={value.length} max={max} />
      </div>
      {mode === "write" ? (
        <Textarea
          id={id}
          aria-describedby={describedBy}
          rows={rows}
          value={value}
          maxLength={max}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className="rounded-none border-0 font-mono text-xs leading-relaxed focus-visible:ring-0 focus-visible:ring-offset-0"
        />
      ) : (
        <div id={`${id}-preview`} className="min-h-32 px-3 py-2">
          {value.trim() ? (
            <Markdown text={value} />
          ) : (
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.editor.empty")}</p>
          )}
        </div>
      )}
    </div>
  );
}

/** A free-text setting (blank = inherit) as a markdown editor row. */
export function TextRow({
  field, label, description, max, placeholder, rows,
}: {
  field: InheritableKey;
  label: string;
  description?: React.ReactNode;
  max: number;
  placeholder?: string;
  rows?: number;
}) {
  const t = useT();
  const { scope, draft, inh, setOwn, canEdit } = useSettings();
  const id = useId();
  const own = (draft.own[field] as string | null) ?? "";
  const inherited = typeof inh.values[field] === "string" ? (inh.values[field] as string) : "";
  return (
    <SettingRow
      field={field}
      label={label}
      htmlFor={id}
      description={description}
      control={(
        <MarkdownEditor
          id={id}
          value={own}
          max={max}
          rows={rows}
          disabled={!canEdit}
          placeholder={inherited
            ? (scope.kind === "repo"
              ? t("reviewSettings.text.inheritsPlaceholder", { text: inherited.slice(0, 160) })
              : inherited)
            : placeholder}
          onChange={(v) => setOwn(field, v.trim() ? v : null)}
        />
      )}
    />
  );
}

/** A value read for "Workspace default: …" lines. */
export function useDescribe() {
  const t = useT();
  return {
    bool: (v: unknown) => (v ? t("reviewSettings.value.on") : t("reviewSettings.value.off")),
    list: (v: unknown, empty: string) => {
      const list = Array.isArray(v) ? (v as string[]) : [];
      return list.length ? list.join(", ") : empty;
    },
  };
}

/** Whether a field's draft differs from what was loaded — for a row that
 *  wants to say "unsaved". */
export function useFieldChanged(field: InheritableKey): boolean {
  const { scope, draft, original } = useSettings();
  return JSON.stringify(canonical(field, draft.own[field], scope.kind))
    !== JSON.stringify(canonical(field, original.own[field], scope.kind));
}
