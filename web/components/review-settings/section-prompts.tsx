"use client";

/**
 * Prompts: what every agent is told (the base instruction — Kodus's
 * "Writing guidelines"), and per agent two kinds of customisation:
 *
 *   Guidelines — the default. Text ADDED to the agent's prompt in a
 *   delimited "Team guidelines" block (src/review/prompt_guidelines.py):
 *   the repository's, else the workspace's (both with "Also keep the
 *   workspace guidelines"). At most GUIDELINES_MAX characters, like Kodus.
 *
 *   Replace the built-in prompt — the advanced mode, folded away with a
 *   warning: repository replacement → workspace replacement → built-in.
 *   Guidelines are still added to a replaced prompt.
 *
 * At Global both are the workspace's (/api/agents — the editor role may
 * change them even where the other defaults are the admin's). At a
 * repository both are this repository's; empty inherits.
 */

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangleIcon, CopyIcon, EyeIcon, GitForkIcon, RotateCcwIcon,
} from "lucide-react";

import {
  reviewPoliciesApi, type AgentInfo, type PromptPart, type PromptPreview,
} from "@/lib/api";
import { agentLabel } from "@/lib/review-categories";
import { settingsHref } from "@/lib/review-settings-routes";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { OverriddenPill } from "@/components/ui/status";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useSettings } from "@/components/review-settings/context";
import {
  CharCount, Group, MarkdownEditor, SectionFrame, SettingRow, TextRow,
} from "@/components/review-settings/field";
import {
  BASE_INSTRUCTION_MAX, GUIDELINES_MAX, PROMPT_TEMPLATE_MAX, WORKSPACE_PROMPT_MIN,
} from "@/components/review-settings/model";

export function PromptsSection() {
  const t = useT();
  const { scope } = useSettings();
  return (
    <SectionFrame
      id="prompts"
      title={t("reviewSettings.section.prompts")}
      description={t("reviewSettings.prompts.desc")}
    >
      <Group title={t("reviewSettings.prompts.sharedGroup")}>
        <TextRow
          field="base_instruction"
          label={t("reviewSettings.prompts.base")}
          description={t("reviewSettings.prompts.baseHint")}
          max={BASE_INSTRUCTION_MAX}
          placeholder={t("reviewSettings.prompts.basePlaceholder")}
        />
        {scope.kind === "repo" && <RepoInstructionsRow />}
      </Group>
      {scope.kind === "repo" ? <RepoAgentPrompts /> : <WorkspaceAgentPrompts />}
    </SectionFrame>
  );
}

function RepoInstructionsRow() {
  const t = useT();
  const { draft, original, patch, canEdit } = useSettings();
  const id = useId();
  const set = draft.promptTemplate.trim() !== "";
  return (
    <SettingRow
      label={t("reviewSettings.prompts.repoInstructions")}
      htmlFor={id}
      description={t("reviewSettings.prompts.repoInstructionsHint")}
      set={set}
      onReset={original.promptTemplate || set ? () => patch({ promptTemplate: "" }) : undefined}
      control={(
        <MarkdownEditor
          id={id}
          value={draft.promptTemplate}
          max={PROMPT_TEMPLATE_MAX}
          rows={6}
          disabled={!canEdit}
          placeholder={t("admin.reviewPolicies.detail.promptTemplatePlaceholder")}
          onChange={(v) => patch({ promptTemplate: v })}
        />
      )}
    />
  );
}

/** Scroll the agent a link named into view once, after the first paint. */
function useFocusAgent(agent: string, ref: React.RefObject<HTMLElement | null>) {
  const { focusAgent } = useSettings();
  useEffect(() => {
    if (focusAgent === agent && ref.current) {
      ref.current.scrollIntoView({ block: "center" });
      ref.current.querySelector<HTMLElement>("textarea, button")?.focus({ preventScroll: true });
    }
    // Only on arrival: the link is a one-time instruction.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

// ─── the shared pieces ───────────────────────────────────────────────

/** The guidelines box: what is ADDED to the agent's prompt. The help text
 *  is what the built-in prompt already covers (Kodus shows the category's
 *  default description the same way), so a team writes only what it adds. */
function GuidelinesEditor({
  id, value, onChange, disabled, hint, placeholder,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  hint?: string;
  placeholder: string;
}) {
  const t = useT();
  const over = value.trim().length > GUIDELINES_MAX;
  return (
    <div className="mt-2 space-y-1.5">
      <label htmlFor={id} className="text-xs font-medium">
        {t("reviewSettings.prompts.guidelines")}
      </label>
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("reviewSettings.prompts.guidelinesHint")}
      </p>
      {hint && (
        <details className="text-xs text-[var(--color-muted-foreground)]">
          <summary className="cursor-pointer select-none">
            {t("reviewSettings.prompts.guidelinesCovers")}
          </summary>
          <p className="mt-1 whitespace-pre-line rounded-md bg-[var(--color-muted)] p-2">{hint}</p>
        </details>
      )}
      <Textarea
        id={id}
        rows={value.trim() ? 6 : 3}
        spellCheck={false}
        className="text-xs leading-relaxed"
        value={value}
        disabled={disabled}
        aria-invalid={over ? true : undefined}
        aria-describedby={`${id}-count`}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      <div id={`${id}-count`} className="flex flex-wrap items-center justify-between gap-2">
        <span className={cn("text-xs", over ? "text-[var(--color-destructive)]" : "text-[var(--color-muted-foreground)]")}>
          {over
            ? t("reviewSettings.save.blockedGuidelines", { max: GUIDELINES_MAX })
            : t("reviewSettings.prompts.guidelinesAdded")}
        </span>
        <CharCount count={value.trim().length} max={GUIDELINES_MAX} />
      </div>
    </div>
  );
}

/** "Advanced: replace the built-in prompt" — folded, with the warning
 *  first. Open on arrival when this scope already replaces the prompt. */
function AdvancedReplace({ active, children }: { active: boolean; children: React.ReactNode }) {
  const t = useT();
  return (
    <details className="mt-3 rounded-md border border-[var(--color-border)] px-3 py-2" open={active || undefined}>
      <summary className="flex cursor-pointer select-none flex-wrap items-center gap-2 text-xs font-medium">
        {t("reviewSettings.prompts.advancedReplace")}
        {active && (
          <Badge variant="outline" className="text-[10px] text-[var(--color-warning)]">
            {t("reviewSettings.prompts.replaces")}
          </Badge>
        )}
      </summary>
      <Callout tone="warning" className="mt-2 flex gap-2">
        <AlertTriangleIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>{t("reviewSettings.prompts.replaceWarning")}</span>
      </Callout>
      {children}
    </details>
  );
}

// ─── repository scope ────────────────────────────────────────────────

function RepoAgentPrompts() {
  const t = useT();
  const { meta, draft, wsAgents } = useSettings();
  const ws = Object.fromEntries((wsAgents ?? []).map((a) => [a.name, a]));
  const agents = meta.overridableAgents.length ? meta.overridableAgents : Object.keys(draft.agentPrompts);
  return (
    <Group
      title={t("reviewSettings.prompts.agentsGroup")}
      description={t("reviewSettings.prompts.agentsRepoHint")}
    >
      {agents.map((agent) => <RepoAgentPrompt key={agent} agent={agent} ws={ws[agent]} />)}
    </Group>
  );
}

function RepoAgentPrompt({ agent, ws }: { agent: string; ws?: AgentInfo }) {
  const t = useT();
  const { scope, draft, patch, canEdit } = useSettings();
  const id = useId();
  const replaceId = useId();
  const extendId = useId();
  const ref = useRef<HTMLDivElement>(null);
  useFocusAgent(agent, ref);
  const [preview, setPreview] = useState(false);

  const guidelines = draft.agentGuidelines[agent] ?? "";
  const ownGuidelines = guidelines.trim() !== "";
  const wsGuidelines = (ws?.guidelines ?? "").trim();
  const extend = draft.guidelinesExtend.includes(agent);
  const writeGuidelines = (v: string) => patch({ agentGuidelines: { ...draft.agentGuidelines, [agent]: v } });
  const writeExtend = (on: boolean) => patch({
    guidelinesExtend: on
      ? [...draft.guidelinesExtend.filter((a) => a !== agent), agent]
      : draft.guidelinesExtend.filter((a) => a !== agent),
  });

  const value = draft.agentPrompts[agent] ?? "";
  const ownReplace = value.trim() !== "";
  const write = (v: string) => patch({ agentPrompts: { ...draft.agentPrompts, [agent]: v } });

  return (
    <div ref={ref} className="px-4 py-3.5" id={`prompt-${agent}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-h-8 flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{agentLabel(agent)}</span>
          {ownGuidelines ? (
            <OverriddenPill label={t("reviewSettings.prompts.custom")} />
          ) : (
            <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
              {wsGuidelines ? t("reviewSettings.prompts.inherited") : t("reviewSettings.prompts.notSet")}
            </Badge>
          )}
          {ownReplace && (
            <Badge variant="outline" className="text-[10px] text-[var(--color-warning)]">
              {t("reviewSettings.prompts.replaces")}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {ownGuidelines && (
            <Button type="button" variant="ghost" size="sm" disabled={!canEdit} onClick={() => writeGuidelines("")}>
              <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
              {t("reviewSettings.prompts.reset")}
            </Button>
          )}
          <Button type="button" variant="ghost" size="sm" onClick={() => setPreview(true)}>
            <EyeIcon className="h-3.5 w-3.5" aria-hidden />
            {t("reviewSettings.prompts.preview")}
          </Button>
        </div>
      </div>
      <GuidelinesEditor
        id={id}
        value={guidelines}
        onChange={writeGuidelines}
        disabled={!canEdit}
        hint={ws?.guidelines_hint}
        placeholder={wsGuidelines
          ? t("reviewSettings.prompts.inheritsWorkspaceGuidelines", { text: wsGuidelines })
          : t("reviewSettings.prompts.guidelinesPlaceholder")}
      />
      <div className="mt-2 flex items-start gap-2">
        <Switch
          id={extendId}
          checked={extend}
          disabled={!canEdit}
          onCheckedChange={writeExtend}
        />
        <label htmlFor={extendId} className="text-xs">
          <span className="font-medium">{t("reviewSettings.prompts.extendWorkspace")}</span>
          <span className="block text-[var(--color-muted-foreground)]">
            {t("reviewSettings.prompts.extendWorkspaceHint")}
          </span>
        </label>
      </div>
      <AdvancedReplace active={ownReplace}>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <label htmlFor={replaceId} className="text-xs font-medium">
            {ownReplace
              ? t("reviewSettings.prompts.custom")
              : ws?.has_override
                ? t("reviewSettings.prompts.inheritsWorkspace")
                : t("reviewSettings.prompts.inheritsBuiltin")}
          </label>
          {ownReplace ? (
            <Button type="button" variant="ghost" size="sm" disabled={!canEdit} onClick={() => write("")}>
              <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
              {t("reviewSettings.prompts.useInherited")}
            </Button>
          ) : (
            <Button
              type="button" variant="ghost" size="sm"
              disabled={!canEdit || !ws}
              onClick={() => write(ws?.system_prompt ?? "")}
            >
              <CopyIcon className="h-3.5 w-3.5" aria-hidden />
              {t("reviewSettings.prompts.startFromInherited")}
            </Button>
          )}
        </div>
        <Textarea
          id={replaceId}
          rows={ownReplace ? 10 : 2}
          spellCheck={false}
          className={cn("mt-2 text-xs", ownReplace && "font-mono leading-relaxed")}
          value={value}
          disabled={!canEdit}
          placeholder={ws?.has_override
            ? t("admin.reviewPolicies.detail.inheritsWorkspacePrompt")
            : t("admin.reviewPolicies.detail.inheritsBuiltinPrompt")}
          onChange={(e) => write(e.target.value)}
        />
      </AdvancedReplace>
      {preview && scope.kind === "repo" && (
        <RepoPromptPreview slug={scope.slug} agent={agent} onClose={() => setPreview(false)} />
      )}
    </div>
  );
}

function RepoPromptPreview({ slug, agent, onClose }: { slug: string; agent: string; onClose: () => void }) {
  const t = useT();
  const token = useToken();
  const q = useQuery({
    queryKey: ["review-policies", "preview", slug, agent],
    queryFn: () => reviewPoliciesApi.promptPreview(token!, slug, agent),
    enabled: !!token,
  });
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{t("reviewSettings.prompts.previewTitle", { agent: agentLabel(agent) })}</DialogTitle>
          <DialogDescription>{t("reviewSettings.prompts.previewRepoHint")}</DialogDescription>
        </DialogHeader>
        <ComposedPreview loading={q.isLoading} error={q.error as Error | null} data={q.data} />
      </DialogContent>
    </Dialog>
  );
}

/** The composed prompt: the agent's own prompt folded (it is long and the
 *  team did not write it), every block appended to it shown, and the team's
 *  guidelines highlighted — the part this page edits. */
function ComposedPreview({
  loading, error, data,
}: { loading: boolean; error: Error | null; data?: PromptPreview }) {
  const t = useT();
  if (loading) return <Skeleton className="h-64 w-full" />;
  if (error) {
    return (
      <Callout tone="danger">
        {t("admin.reviewPolicies.detail.previewFailed", { message: error.message })}
      </Callout>
    );
  }
  if (!data) return null;
  const parts = data.parts ?? [];
  return (
    <div className="max-h-[70dvh] space-y-3 overflow-auto">
      {data.prompt_source && (
        <Callout tone="info">
          {t(`admin.reviewPolicies.detail.promptSource.${data.prompt_source}`)}
        </Callout>
      )}
      {parts.length ? (
        <div className="space-y-2">
          {parts.map((part, i) => <PreviewPart key={`${part.kind}-${i}`} part={part} />)}
        </div>
      ) : (
        <PromptBlock title={t("reviewSettings.prompts.systemPrompt")} text={data.system_prompt} />
      )}
      {data.user_prompt_template && (
        <details>
          <summary className="cursor-pointer select-none text-xs font-medium text-[var(--color-muted-foreground)]">
            {t("reviewSettings.prompts.userTemplate")}
          </summary>
          <PromptBlock title="" text={data.user_prompt_template} />
        </details>
      )}
    </div>
  );
}

const PART_KINDS = [
  "guidelines", "base_instruction", "workspace_rules", "rules", "language", "output_format", "rider",
] as const;

function PreviewPart({ part }: { part: PromptPart }) {
  const t = useT();
  if (part.kind === "base") {
    const source = (["repo", "workspace"] as const).find((s) => s === part.source) ?? "builtin";
    return (
      <details className="rounded-md border border-[var(--color-border)]">
        <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-[var(--color-muted-foreground)]">
          {t(`reviewSettings.prompts.previewBase.${source}`)}
          {" · "}
          {t("reviewSettings.prompts.previewChars", { count: part.text.length })}
        </summary>
        <pre className="max-h-[40dvh] overflow-auto whitespace-pre-wrap border-t border-[var(--color-border)] bg-[var(--color-muted)] p-3 font-mono text-xs leading-relaxed">
          {part.text}
        </pre>
      </details>
    );
  }
  const kind = PART_KINDS.find((k) => k === part.kind) ?? "rules";
  const team = kind === "guidelines";
  return (
    <div
      className={cn(
        "rounded-md border p-3",
        team
          ? "border-[var(--color-primary)]/50 bg-[var(--color-primary)]/5"
          : "border-[var(--color-border)]",
      )}
    >
      <h3 className={cn("mb-1 text-xs font-medium", team ? "text-[var(--color-primary)]" : "text-[var(--color-muted-foreground)]")}>
        {t("reviewSettings.prompts.previewAdded")}: {t(`reviewSettings.prompts.previewPart.${kind}`)}
      </h3>
      <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed">{part.text}</pre>
    </div>
  );
}

function PromptBlock({ title, text }: { title: string; text: string }) {
  return (
    <div>
      {title && <h3 className="mb-1 text-xs font-medium text-[var(--color-muted-foreground)]">{title}</h3>}
      <pre className="max-h-[50dvh] overflow-auto whitespace-pre-wrap rounded-md bg-[var(--color-muted)] p-3 font-mono text-xs leading-relaxed">
        {text}
      </pre>
    </div>
  );
}

// ─── workspace scope ─────────────────────────────────────────────────

function WorkspaceAgentPrompts() {
  const t = useT();
  const token = useToken();
  const { wsAgents, canEditPrompts, canEdit } = useSettings();
  // Which repositories carry their own prompt or guidelines for each agent:
  // a workspace value edited here never reaches them, and this is where
  // people look.
  const overrides = useQuery({
    queryKey: ["review-policies", "overrides-summary"],
    queryFn: () => reviewPoliciesApi.overridesSummary(token!),
    enabled: !!token,
  });
  return (
    <Group
      title={t("reviewSettings.prompts.agentsGroup")}
      description={t("reviewSettings.prompts.agentsGlobalHint")}
    >
      {canEditPrompts && !canEdit && (
        <div className="px-4 py-2.5">
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.access.editorPrompts")}</p>
        </div>
      )}
      {!wsAgents && (
        <div className="space-y-2 px-4 py-3.5">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-24 w-full" />
        </div>
      )}
      {(wsAgents ?? []).map((a) => (
        <WorkspaceAgentPrompt
          key={a.name}
          agent={a}
          repos={overrides.data?.prompt_overrides?.[a.name] ?? []}
          guidelineRepos={overrides.data?.guideline_overrides?.[a.name] ?? []}
        />
      ))}
    </Group>
  );
}

function RepoLinks({ agent, repos }: { agent: string; repos: Array<{ repo_slug: string }> }) {
  if (!repos.length) return null;
  return (
    <ul className="mt-1 flex flex-wrap gap-1.5 text-xs">
      {repos.map((r) => (
        <li key={r.repo_slug}>
          <Link
            className="font-mono text-[var(--color-primary)] underline-offset-4 hover:underline"
            href={settingsHref({ repo: r.repo_slug, section: "prompts", agent })}
          >
            {r.repo_slug}
          </Link>
        </li>
      ))}
    </ul>
  );
}

function WorkspaceAgentPrompt({
  agent, repos, guidelineRepos,
}: {
  agent: AgentInfo;
  repos: Array<{ repo_slug: string }>;
  guidelineRepos: Array<{ repo_slug: string }>;
}) {
  const t = useT();
  const token = useToken();
  const { draft, patch, canEditPrompts } = useSettings();
  const id = useId();
  const replaceId = useId();
  const ref = useRef<HTMLDivElement>(null);
  useFocusAgent(agent.name, ref);
  const [preview, setPreview] = useState(false);

  const guidelines = draft.agentGuidelines[agent.name] ?? "";
  const ownGuidelines = guidelines.trim() !== "";
  const writeGuidelines = (v: string) => patch({ agentGuidelines: { ...draft.agentGuidelines, [agent.name]: v } });

  const d = draft.workspacePrompts[agent.name] ?? { text: agent.system_prompt, reset: false };
  const custom = agent.has_override && !d.reset;
  const changed = d.text !== agent.system_prompt;
  const write = (next: Partial<typeof d>) => patch({
    workspacePrompts: { ...draft.workspacePrompts, [agent.name]: { ...d, ...next } },
  });
  const tooShort = !d.reset && changed && d.text.trim().length < WORKSPACE_PROMPT_MIN;

  const q = useQuery({
    queryKey: ["review-policies", "preview", "(workspace)", agent.name],
    queryFn: () => reviewPoliciesApi.workspacePromptPreview(token!, agent.name),
    enabled: !!token && preview,
  });

  return (
    <div ref={ref} className="px-4 py-3.5" id={`prompt-${agent.name}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-h-8 flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{agent.display_name}</span>
          {ownGuidelines ? (
            <OverriddenPill label={t("reviewSettings.prompts.custom")} />
          ) : (
            <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
              {t("reviewSettings.prompts.notSet")}
            </Badge>
          )}
          {d.reset ? (
            <Badge variant="outline" className="text-[10px]">{t("reviewSettings.prompts.resetPending")}</Badge>
          ) : (custom || changed) && (
            <Badge variant="outline" className="text-[10px] text-[var(--color-warning)]">
              {t("reviewSettings.prompts.replaces")}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {ownGuidelines && (
            <Button type="button" variant="ghost" size="sm" disabled={!canEditPrompts} onClick={() => writeGuidelines("")}>
              <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
              {t("reviewSettings.prompts.reset")}
            </Button>
          )}
          <Button type="button" variant="ghost" size="sm" onClick={() => setPreview(true)}>
            <EyeIcon className="h-3.5 w-3.5" aria-hidden />
            {t("reviewSettings.prompts.preview")}
          </Button>
        </div>
      </div>
      <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">{agent.role}</p>
      <GuidelinesEditor
        id={id}
        value={guidelines}
        onChange={writeGuidelines}
        disabled={!canEditPrompts}
        hint={agent.guidelines_hint}
        placeholder={t("reviewSettings.prompts.guidelinesPlaceholder")}
      />
      {guidelineRepos.length > 0 && (
        <div className="mt-2">
          <span className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)]">
            <GitForkIcon className="h-3 w-3" aria-hidden />
            {t("reviewSettings.prompts.guidelinesIn", { count: guidelineRepos.length })}
          </span>
          <RepoLinks agent={agent.name} repos={guidelineRepos} />
        </div>
      )}
      <AdvancedReplace active={custom || changed || d.reset}>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <label htmlFor={replaceId} className="text-xs font-medium">
            {t("reviewSettings.prompts.systemPrompt")}
          </label>
          <div className="flex flex-wrap items-center gap-1">
            {(agent.has_override || changed) && (
              <Button
                type="button" variant="ghost" size="sm"
                disabled={!canEditPrompts || d.reset}
                onClick={() => write(agent.has_override
                  ? { reset: true, text: agent.system_prompt }
                  : { text: agent.system_prompt })}
              >
                <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
                {t("reviewSettings.prompts.resetDefault")}
              </Button>
            )}
            {d.reset && (
              <Button type="button" variant="ghost" size="sm" onClick={() => write({ reset: false })}>
                {t("reviewSettings.prompts.keepCustom")}
              </Button>
            )}
          </div>
        </div>
        <Textarea
          id={replaceId}
          rows={8}
          spellCheck={false}
          className="mt-2 font-mono text-xs leading-relaxed"
          value={d.text}
          disabled={!canEditPrompts || d.reset}
          aria-invalid={tooShort ? true : undefined}
          aria-describedby={`${replaceId}-count`}
          onChange={(e) => write({ text: e.target.value })}
        />
        <div id={`${replaceId}-count`} className="mt-1 flex flex-wrap items-center justify-between gap-2">
          <span className={cn("text-xs", tooShort ? "text-[var(--color-destructive)]" : "text-[var(--color-muted-foreground)]")}>
            {d.reset
              ? t("reviewSettings.prompts.resetPendingHint")
              : tooShort
                ? t("reviewSettings.prompts.tooShort", { min: WORKSPACE_PROMPT_MIN })
                : t("reviewSettings.prompts.appliesEverywhere")}
          </span>
          <CharCount count={d.text.length} max={100_000} />
        </div>
        {repos.length > 0 && (
          <div className="mt-2">
            <span className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)]">
              <GitForkIcon className="h-3 w-3" aria-hidden />
              {t("reviewSettings.prompts.overriddenIn", { count: repos.length })}
            </span>
            <ul className="mt-1 flex flex-wrap gap-1.5 text-xs">
              {repos.map((r) => (
                <li key={r.repo_slug}>
                  <Link
                    className="font-mono text-[var(--color-primary)] underline-offset-4 hover:underline"
                    href={settingsHref({ repo: r.repo_slug, section: "prompts", agent: agent.name })}
                  >
                    {r.repo_slug}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}
      </AdvancedReplace>
      <Dialog open={preview} onOpenChange={setPreview}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{t("reviewSettings.prompts.previewTitle", { agent: agent.display_name })}</DialogTitle>
            <DialogDescription>{t("reviewSettings.prompts.previewWorkspaceHint")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <div>
              <h3 className="mb-1 text-xs font-medium text-[var(--color-muted-foreground)]">
                {t("admin.agents.detail.whatItChecks")}
              </h3>
              <ul className="list-inside list-disc space-y-0.5 text-xs">
                {agent.focus.map((f) => <li key={f}>{f}</li>)}
              </ul>
            </div>
            <div>
              <h3 className="mb-1 text-xs font-medium text-[var(--color-muted-foreground)]">
                {t("admin.agents.detail.contextProvided")}
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {agent.context_used.map((c) => (
                  <Badge key={c} variant="outline" className="text-[10px] font-normal">{c}</Badge>
                ))}
              </div>
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.agents.detail.verdictImpact")} {agent.verdict_impact}
              {" · "}
              {t("admin.agents.detail.defaultSeverity")} <code>{agent.default_severity}</code>
            </p>
            <ComposedPreview loading={q.isLoading} error={q.error as Error | null} data={q.data} />
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
