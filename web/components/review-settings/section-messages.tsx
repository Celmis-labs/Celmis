"use client";

/**
 * Messages: the two comments a review posts on its own behalf — "review
 * started" and the header of the finished review. Templates with
 * placeholders; the chips insert one at the cursor and the preview fills
 * them from a sample pull request, so what is shown is what gets posted.
 */

import { useId, useRef } from "react";

import {
  SAMPLE_VALUES, renderTemplate, templateError,
} from "@/lib/message-templates";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Markdown } from "@/components/markdown";
import { Callout } from "@/components/ui/callout";
import { Textarea } from "@/components/ui/textarea";
import { useSettings } from "@/components/review-settings/context";
import {
  CharCount, Group, SectionFrame, SettingRow,
} from "@/components/review-settings/field";
import { MESSAGE_MAX, effective, type InheritableKey } from "@/components/review-settings/model";

export function MessagesSection() {
  const t = useT();
  const { scope, draft, inh, goTo } = useSettings();
  const startedOn = Boolean(effective(draft, "started_comment_enabled", scope.kind, inh));
  return (
    <SectionFrame
      id="messages"
      title={t("reviewSettings.section.messages")}
      description={t("reviewSettings.messages.desc")}
    >
      {!startedOn && (
        <Callout tone="info">
          {t("reviewSettings.messages.startedOff")}{" "}
          <button type="button" className="font-medium underline underline-offset-4" onClick={() => goTo("general")}>
            {t("reviewSettings.section.general")}
          </button>
        </Callout>
      )}
      <Group>
        <TemplateRow
          field="message_started"
          label={t("reviewSettings.messages.started")}
          description={t("reviewSettings.messages.startedHint")}
          example={"Reviewing `{commit}` with {agents}…"}
        />
        <TemplateRow
          field="message_finished_header"
          label={t("reviewSettings.messages.finished")}
          description={t("reviewSettings.messages.finishedHint")}
          example={"## Review of #{pr_number} ({files} files)"}
        />
      </Group>
    </SectionFrame>
  );
}

/** Whether every template the draft holds would be accepted. */
export function messagesBlocked(own: Record<string, unknown>, placeholders: string[]): boolean {
  return (["message_started", "message_finished_header"] as const).some((f) => {
    const v = own[f];
    return typeof v === "string" && v.trim() !== "" && templateError(v, placeholders) !== null;
  });
}

function TemplateRow({
  field, label, description, example,
}: {
  field: Extract<InheritableKey, "message_started" | "message_finished_header">;
  label: string;
  description: string;
  example: string;
}) {
  const t = useT();
  const { draft, inh, setOwn, canEdit, meta } = useSettings();
  const id = useId();
  const ref = useRef<HTMLTextAreaElement>(null);
  const own = (draft.own[field] as string | null) ?? "";
  const inherited = typeof inh.values[field] === "string" ? (inh.values[field] as string) : "";
  const shown = own || inherited;
  const error = own ? templateError(own, meta.placeholders) : null;

  const insert = (name: string) => {
    const el = ref.current;
    const token = `{${name}}`;
    const start = el?.selectionStart ?? own.length;
    const end = el?.selectionEnd ?? own.length;
    const next = own.slice(0, start) + token + own.slice(end);
    setOwn(field, next);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(start + token.length, start + token.length);
    });
  };

  return (
    <SettingRow
      field={field}
      label={label}
      htmlFor={id}
      description={description}
      control={(
        <div className="grid gap-3 @2xl:grid-cols-2">
          <div className="space-y-2">
            <Textarea
              ref={ref}
              id={id}
              rows={4}
              spellCheck={false}
              maxLength={MESSAGE_MAX}
              className="font-mono text-xs"
              value={own}
              disabled={!canEdit}
              placeholder={inherited || example}
              aria-invalid={error ? true : undefined}
              aria-describedby={`${id}-status`}
              onChange={(e) => setOwn(field, e.target.value.trim() ? e.target.value : null)}
            />
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={t("reviewSettings.messages.placeholders")}>
              {meta.placeholders.map((p) => (
                <button
                  key={p}
                  type="button"
                  disabled={!canEdit}
                  onClick={() => insert(p)}
                  aria-label={t("reviewSettings.messages.insert", { name: p })}
                  className="rounded-md border border-[var(--color-border)] bg-[var(--color-secondary)] px-1.5 py-0.5 font-mono text-[11px] transition-colors hover:border-[var(--color-brand)] hover:text-[var(--color-brand)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none disabled:opacity-50"
                >
                  {`{${p}}`}
                </button>
              ))}
              <span className="ml-auto"><CharCount count={own.length} max={MESSAGE_MAX} /></span>
            </div>
            <p
              id={`${id}-status`}
              role={error ? "alert" : undefined}
              className={cn("text-xs", error ? "text-[var(--color-destructive)]" : "text-[var(--color-muted-foreground)]")}
            >
              {error
                ? t(error.key, "field" in error ? { field: error.field } : undefined)
                : own
                  ? t("reviewSettings.messages.braces")
                  : t("reviewSettings.messages.builtinUsed")}
            </p>
          </div>
          <div className="min-w-0 rounded-md border border-dashed border-[var(--color-border)] p-3">
            <p className="mb-1.5 text-[11px] font-medium text-[var(--color-muted-foreground)]">
              {t("reviewSettings.messages.preview")}
            </p>
            {shown ? (
              <Markdown text={renderTemplate(shown, SAMPLE_VALUES)} className="text-sm" />
            ) : (
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.messages.builtinPreview")}</p>
            )}
          </div>
        </div>
      )}
    />
  );
}
