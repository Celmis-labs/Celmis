"use client";

/**
 * Prompts: what every agent is told (the base instruction), and each
 * agent's own system prompt.
 *
 *   repository prompt → workspace prompt → built-in prompt
 *
 * At Global the agent prompts are the workspace's (/api/agents — the editor
 * role may change them even where the other defaults are the admin's). At a
 * repository each agent's box is this repository's override; empty inherits.
 */

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CopyIcon, EyeIcon, GitForkIcon, RotateCcwIcon } from "lucide-react";

import { reviewPoliciesApi, type AgentInfo } from "@/lib/api";
import { agentLabel } from "@/lib/review-categories";
import { settingsHref } from "@/lib/review-settings-routes";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useSettings } from "@/components/review-settings/context";
import {
  CharCount, Group, MarkdownEditor, SectionFrame, SettingRow, TextRow,
} from "@/components/review-settings/field";
import {
  BASE_INSTRUCTION_MAX, PROMPT_TEMPLATE_MAX, WORKSPACE_PROMPT_MIN,
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
  const ref = useRef<HTMLDivElement>(null);
  useFocusAgent(agent, ref);
  const [preview, setPreview] = useState(false);
  const value = draft.agentPrompts[agent] ?? "";
  const own = value.trim() !== "";
  const write = (v: string) => patch({ agentPrompts: { ...draft.agentPrompts, [agent]: v } });
  return (
    <div ref={ref} className="px-4 py-3.5" id={`prompt-${agent}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-h-8 flex-wrap items-center gap-2">
          <label htmlFor={id} className="text-sm font-medium">{agentLabel(agent)}</label>
          {own ? (
            <Badge variant="warning" className="text-[10px]">{t("reviewSettings.prompts.custom")}</Badge>
          ) : (
            <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
              {ws?.has_override
                ? t("reviewSettings.prompts.inheritsWorkspace")
                : t("reviewSettings.prompts.inheritsBuiltin")}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {own ? (
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
          <Button type="button" variant="ghost" size="sm" onClick={() => setPreview(true)}>
            <EyeIcon className="h-3.5 w-3.5" aria-hidden />
            {t("reviewSettings.prompts.preview")}
          </Button>
        </div>
      </div>
      <Textarea
        id={id}
        rows={own ? 10 : 2}
        spellCheck={false}
        className={cn("mt-2 text-xs", own && "font-mono leading-relaxed")}
        value={value}
        disabled={!canEdit}
        placeholder={ws?.has_override
          ? t("admin.reviewPolicies.detail.inheritsWorkspacePrompt")
          : t("admin.reviewPolicies.detail.inheritsBuiltinPrompt")}
        onChange={(e) => write(e.target.value)}
      />
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
        {q.isLoading && <Skeleton className="h-64 w-full" />}
        {q.error && (
          <Callout tone="danger">
            {t("admin.reviewPolicies.detail.previewFailed", { message: (q.error as Error).message })}
          </Callout>
        )}
        {q.data && (
          <div className="space-y-3">
            {q.data.prompt_source && (
              <Callout tone="info">
                {t(`admin.reviewPolicies.detail.promptSource.${q.data.prompt_source}`)}
              </Callout>
            )}
            <PromptBlock title={t("reviewSettings.prompts.systemPrompt")} text={q.data.system_prompt} />
            {q.data.user_prompt_template && (
              <PromptBlock title={t("reviewSettings.prompts.userTemplate")} text={q.data.user_prompt_template} />
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function PromptBlock({ title, text }: { title: string; text: string }) {
  return (
    <div>
      <h3 className="mb-1 text-xs font-medium text-[var(--color-muted-foreground)]">{title}</h3>
      <pre className="max-h-[50dvh] overflow-auto whitespace-pre-wrap rounded-md bg-[var(--color-muted)] p-3 font-mono text-xs leading-relaxed">
        {text}
      </pre>
    </div>
  );
}

function WorkspaceAgentPrompts() {
  const t = useT();
  const token = useToken();
  const { wsAgents, canEditPrompts, canEdit } = useSettings();
  // Which repositories carry their own prompt for each agent: a workspace
  // prompt edited here never reaches them, and this is where people look.
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
        />
      ))}
    </Group>
  );
}

function WorkspaceAgentPrompt({ agent, repos }: { agent: AgentInfo; repos: Array<{ repo_slug: string }> }) {
  const t = useT();
  const { draft, patch, canEditPrompts } = useSettings();
  const id = useId();
  const ref = useRef<HTMLDivElement>(null);
  useFocusAgent(agent.name, ref);
  const [preview, setPreview] = useState(false);
  const d = draft.workspacePrompts[agent.name] ?? { text: agent.system_prompt, reset: false };
  const custom = agent.has_override && !d.reset;
  const changed = d.text !== agent.system_prompt;
  const write = (next: Partial<typeof d>) => patch({
    workspacePrompts: { ...draft.workspacePrompts, [agent.name]: { ...d, ...next } },
  });
  const tooShort = !d.reset && changed && d.text.trim().length < WORKSPACE_PROMPT_MIN;
  return (
    <div ref={ref} className="px-4 py-3.5" id={`prompt-${agent.name}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-h-8 flex-wrap items-center gap-2">
          <label htmlFor={id} className="text-sm font-medium">{agent.display_name}</label>
          {d.reset ? (
            <Badge variant="outline" className="text-[10px]">{t("reviewSettings.prompts.resetPending")}</Badge>
          ) : custom || changed ? (
            <Badge variant="brand" className="text-[10px]">{t("reviewSettings.prompts.custom")}</Badge>
          ) : (
            <Badge variant="outline" className="text-[10px] font-normal text-[var(--color-muted-foreground)]">
              {t("reviewSettings.prompts.default")}
            </Badge>
          )}
          {repos.length > 0 && (
            <span className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)]">
              <GitForkIcon className="h-3 w-3" aria-hidden />
              {t("reviewSettings.prompts.overriddenIn", { count: repos.length })}
            </span>
          )}
        </div>
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
          <Button type="button" variant="ghost" size="sm" onClick={() => setPreview(true)}>
            <EyeIcon className="h-3.5 w-3.5" aria-hidden />
            {t("reviewSettings.prompts.preview")}
          </Button>
        </div>
      </div>
      <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">{agent.role}</p>
      <Textarea
        id={id}
        rows={8}
        spellCheck={false}
        className="mt-2 font-mono text-xs leading-relaxed"
        value={d.text}
        disabled={!canEditPrompts || d.reset}
        aria-invalid={tooShort ? true : undefined}
        aria-describedby={`${id}-count`}
        onChange={(e) => write({ text: e.target.value })}
      />
      <div id={`${id}-count`} className="mt-1 flex flex-wrap items-center justify-between gap-2">
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
        <ul className="mt-2 flex flex-wrap gap-1.5 text-xs">
          {repos.map((r) => (
            <li key={r.repo_slug}>
              <Link
                className="font-mono text-[var(--color-brand)] underline-offset-4 hover:underline"
                href={settingsHref({ repo: r.repo_slug, section: "prompts", agent: agent.name })}
              >
                {r.repo_slug}
              </Link>
            </li>
          ))}
        </ul>
      )}
      <Dialog open={preview} onOpenChange={setPreview}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{t("reviewSettings.prompts.previewTitle", { agent: agent.display_name })}</DialogTitle>
            <DialogDescription>{agent.role}</DialogDescription>
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
            <PromptBlock title={t("reviewSettings.prompts.systemPrompt")} text={d.text} />
            <PromptBlock title={t("reviewSettings.prompts.userTemplate")} text={agent.user_prompt_template} />
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
