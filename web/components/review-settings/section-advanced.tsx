"use client";

/**
 * Advanced (repository only): MCP evidence sources, and the folder rules a
 * policy carried before the rules library existed — still applied, shown
 * read-only, with the way to move them into the library.
 */

import Link from "next/link";
import { useId } from "react";
import { PlusIcon, Trash2Icon } from "lucide-react";

import { agentLabel } from "@/lib/review-categories";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useSettings } from "@/components/review-settings/context";
import { Group, SectionFrame } from "@/components/review-settings/field";
import type { McpSource } from "@/components/review-settings/model";

const SENTRY: McpSource = {
  name: "sentry",
  url: "https://mcp.sentry.dev/sse",
  auth_type: "bearer",
  api_key_ref: "mcp:sentry",
  allowed_tools: ["get_issue", "list_issues", "search_issues"],
  trigger_patterns: ["SENTRY-[A-Z0-9]+"],
};

const BLANK: McpSource = {
  name: "", url: "", auth_type: "none", api_key_ref: null, allowed_tools: [], trigger_patterns: [],
};

function csv(text: string): string[] {
  return text.split(",").map((v) => v.trim()).filter(Boolean);
}

export function AdvancedSection() {
  const t = useT();
  const { scope, draft } = useSettings();
  if (scope.kind !== "repo") return null;
  return (
    <SectionFrame
      id="advanced"
      title={t("reviewSettings.section.advanced")}
      description={t("reviewSettings.advanced.desc")}
    >
      <McpSources />
      <Group
        title={t("reviewSettings.advanced.legacyGroup")}
        description={t("reviewSettings.advanced.legacyHint")}
      >
        {draft.folderRules.length === 0 ? (
          <p className="px-4 py-3.5 text-xs text-[var(--color-muted-foreground)]">
            {t("reviewSettings.advanced.legacyNone")}
          </p>
        ) : (
          <>
            <div className="px-4 py-3">
              <Callout tone="warning">
                {t("reviewSettings.advanced.legacyMigrate")}{" "}
                <Link
                  className="font-medium underline underline-offset-4"
                  href={`/admin/review-rules?repo=${encodeURIComponent(scope.slug)}`}
                >
                  {t("reviewSettings.rules.open")}
                </Link>
              </Callout>
            </div>
            {draft.folderRules.map((r, i) => (
              <div key={`${r.pattern}-${i}`} className="space-y-1 px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{r.title || r.pattern}</span>
                  <code className="rounded bg-[var(--color-muted)] px-1 text-xs">{r.pattern}</code>
                  {r.severity_hint && <Badge variant="outline" className="text-[10px]">{r.severity_hint}</Badge>}
                  {(r.agents ?? []).map((a) => (
                    <Badge key={a} variant="outline" className="text-[10px] font-normal">{agentLabel(a)}</Badge>
                  ))}
                </div>
                <p className="whitespace-pre-wrap text-xs text-[var(--color-muted-foreground)]">{r.prompt}</p>
              </div>
            ))}
          </>
        )}
      </Group>
    </SectionFrame>
  );
}

function McpSources() {
  const t = useT();
  const { draft, patch, canEdit } = useSettings();
  const id = useId();
  const sources = draft.mcpSources;
  const update = (i: number, p: Partial<McpSource>) =>
    patch({ mcpSources: sources.map((s, j) => (j === i ? { ...s, ...p } : s)) });
  return (
    <Group
      title={t("reviewSettings.advanced.mcpGroup")}
      description={t("reviewSettings.advanced.mcpHint")}
      action={(
        <div className="flex flex-wrap gap-2">
          <Button
            type="button" variant="outline" size="sm" disabled={!canEdit}
            onClick={() => patch({ mcpSources: [...sources, { ...BLANK }] })}
          >
            <PlusIcon className="h-3.5 w-3.5" aria-hidden />
            {t("admin.reviewPolicies.detail.mcpAddSource")}
          </Button>
          <Button
            type="button" variant="ghost" size="sm"
            disabled={!canEdit || sources.some((s) => s.name === SENTRY.name)}
            onClick={() => patch({ mcpSources: [...sources, { ...SENTRY }] })}
          >
            {t("admin.reviewPolicies.detail.mcpSentryPreset")}
          </Button>
        </div>
      )}
    >
      {sources.length === 0 && (
        <p className="px-4 py-3.5 text-xs text-[var(--color-muted-foreground)]">
          {t("reviewSettings.advanced.mcpNone")}
        </p>
      )}
      {sources.map((src, i) => {
        const f = `${id}-${i}`;
        const incomplete = !src.name.trim() || !src.url.trim();
        return (
          <div key={i} className="space-y-3 px-4 py-3.5">
            <div className="grid gap-3 @lg:grid-cols-2">
              <Field id={`${f}-name`} label={t("admin.reviewPolicies.detail.mcpNameLabel")}>
                <Input id={`${f}-name`} value={src.name} disabled={!canEdit} placeholder="sentry"
                  onChange={(e) => update(i, { name: e.target.value })} />
              </Field>
              <Field id={`${f}-url`} label={t("admin.reviewPolicies.detail.mcpUrlLabel")}>
                <Input id={`${f}-url`} value={src.url} disabled={!canEdit} placeholder="https://mcp.sentry.dev/sse"
                  onChange={(e) => update(i, { url: e.target.value })} />
              </Field>
              <Field id={`${f}-auth`} label={t("admin.reviewPolicies.detail.mcpAuthTypeLabel")}>
                <Select
                  id={`${f}-auth`}
                  className="w-full"
                  value={src.auth_type}
                  disabled={!canEdit}
                  onChange={(v) => update(i, { auth_type: v })}
                  options={[
                    { value: "none", label: t("admin.reviewPolicies.detail.mcpAuthNone") },
                    { value: "bearer", label: t("admin.reviewPolicies.detail.mcpAuthBearer") },
                    { value: "oauth", label: t("admin.reviewPolicies.detail.mcpAuthOauth") },
                  ]}
                />
              </Field>
              <Field id={`${f}-key`} label={t("admin.reviewPolicies.detail.mcpCredKeyLabel")}>
                <Input id={`${f}-key`} value={src.api_key_ref ?? ""} disabled={!canEdit}
                  placeholder={t("admin.reviewPolicies.detail.mcpCredKeyPlaceholder")}
                  onChange={(e) => update(i, { api_key_ref: e.target.value || null })} />
              </Field>
              <Field id={`${f}-trig`} label={t("admin.reviewPolicies.detail.mcpTriggerLabel")}>
                <Input key={src.trigger_patterns.join(",")} id={`${f}-trig`} className="font-mono text-xs" disabled={!canEdit}
                  defaultValue={src.trigger_patterns.join(", ")} placeholder="SENTRY-[A-Z0-9]+"
                  onBlur={(e) => update(i, { trigger_patterns: csv(e.target.value) })} />
              </Field>
              <Field id={`${f}-tools`} label={t("admin.reviewPolicies.detail.mcpAllowedToolsLabel")}>
                <Input key={src.allowed_tools.join(",")} id={`${f}-tools`} className="font-mono text-xs" disabled={!canEdit}
                  defaultValue={src.allowed_tools.join(", ")} placeholder="get_issue,list_issues"
                  onBlur={(e) => update(i, { allowed_tools: csv(e.target.value) })} />
              </Field>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {incomplete ? t("reviewSettings.advanced.mcpIncomplete") : ""}
              </p>
              <Button
                type="button" variant="ghost" size="sm" disabled={!canEdit}
                onClick={() => patch({ mcpSources: sources.filter((_, j) => j !== i) })}
              >
                <Trash2Icon className="h-3.5 w-3.5" aria-hidden />
                {t("admin.reviewPolicies.detail.remove")}
              </Button>
            </div>
          </div>
        );
      })}
    </Group>
  );
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-1">
      <label htmlFor={id} className="text-xs font-medium">{label}</label>
      {children}
    </div>
  );
}
