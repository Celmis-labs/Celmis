"use client";

/**
 * Workspace review defaults — what a repository's review does when its own
 * policy (/admin/review-policies/<slug>) says nothing.
 *
 *   repo policy (non-null) > these defaults > install default > built-in
 *
 * Every control here has three states: a value is this workspace's default,
 * "inherit" hands the field to the install default. A repository that set
 * the field itself keeps its own value; the counts beside each section say
 * how many do, because a default changed here does nothing for them.
 *
 * The per-agent model / output ceiling / reasoning rows edit the workspace
 * `agents` block of the LLM config — the same block /settings/llm edits, not
 * a copy of it — through PUT /api/review-defaults, which validates it with
 * the same function /api/llm/config uses.
 */

import Link from "next/link";
import { useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RotateCcwIcon, SaveIcon, SlidersHorizontalIcon } from "lucide-react";

import {
  REVIEW_AGENTS,
  llmApi,
  reviewDefaultsApi,
  type AgentLLMOverride,
  type ModelCapabilities,
  type ReviewAgent,
  type WorkspaceReviewDefaults,
  type WorkspaceReviewDefaultsUpdate,
} from "@/lib/api";
import { agentLabel } from "@/lib/review-categories";
import { globError, globLines } from "@/lib/ignore-globs";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import {
  AgentLLMRow, DEFAULT_AGENT_MAX_OUTPUT, agentDraftFrom, agentEntryToSave,
  agentMaxOutError, agentMaxOutLimit, useAgentCapabilities, type AgentDraft,
} from "@/components/agent-llm-controls";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

type DefaultsTab = "agents" | "comments" | "ignore";
const DEFAULTS_TABS: DefaultsTab[] = ["agents", "comments", "ignore"];

const MAX_INLINE_MIN = 1;
const MAX_INLINE_MAX = 100;
const SUMMARY_INSTRUCTIONS_MAX = 4000;
/** The thresholds offered, "" = inherit (the install default posts all). */
const THRESHOLDS = ["", "warning", "error", "critical"] as const;

function tabFromUrl(): DefaultsTab | null {
  const wanted = new URLSearchParams(window.location.search).get("tab");
  return wanted && (DEFAULTS_TABS as string[]).includes(wanted) ? (wanted as DefaultsTab) : null;
}
const noSubscribe = () => () => {};

/** The 2.3.0 settings this page has no controls for yet (the settings
 *  redesign brings them). Carried from the GET and sent back as they were,
 *  so a save from this page never resets one it cannot show. */
type CarriedSettings = Pick<WorkspaceReviewDefaults,
  | "run_on_drafts" | "approve_when_clean" | "request_changes_on_critical"
  | "status_feedback" | "committable_suggestions" | "apply_filters_to_rules"
  | "summary_target" | "summary_on_new_commits" | "summary_existing_description"
  | "base_instruction" | "message_started" | "message_finished_header"
>;

/** The editable state of the page — null everywhere means "inherit". */
type Draft = {
  disabledAgents: string[] | null;
  /** Opt-in agents switched on (those whose built-in is off). */
  enabledAgents: string[] | null;
  carried: CarriedSettings;
  verifier: boolean | null;
  threshold: string;
  maxInline: string;
  summary: boolean | null;
  summaryInstructions: string;
  started: boolean | null;
  language: string;
  ignoreGlobs: string;
  targetBranches: string[] | null;
  suppressedRules: string | null;
};

function draftFrom(d: WorkspaceReviewDefaults): Draft {
  return {
    disabledAgents: d.disabled_agents == null ? null : [...d.disabled_agents],
    enabledAgents: d.enabled_agents == null ? null : [...d.enabled_agents],
    carried: {
      run_on_drafts: d.run_on_drafts,
      approve_when_clean: d.approve_when_clean,
      request_changes_on_critical: d.request_changes_on_critical,
      status_feedback: d.status_feedback,
      committable_suggestions: d.committable_suggestions,
      apply_filters_to_rules: d.apply_filters_to_rules,
      summary_target: d.summary_target,
      summary_on_new_commits: d.summary_on_new_commits,
      summary_existing_description: d.summary_existing_description,
      base_instruction: d.base_instruction,
      message_started: d.message_started,
      message_finished_header: d.message_finished_header,
    },
    verifier: d.verifier_enabled,
    threshold: d.comment_min_severity ?? "",
    maxInline: d.max_inline_comments == null ? "" : String(d.max_inline_comments),
    summary: d.summary_enabled,
    summaryInstructions: d.summary_instructions ?? "",
    started: d.started_comment_enabled,
    language: d.review_language ?? "",
    ignoreGlobs: (d.ignore_globs ?? []).join("\n"),
    targetBranches: d.target_branches == null ? null : [...d.target_branches],
    suppressedRules: d.suppressed_rules == null ? null : d.suppressed_rules.join("\n"),
  };
}

/** "" or an integer in [1, 100] — the only caps the API accepts. */
function maxInlineBad(text: string): boolean {
  const v = text.trim();
  if (!v) return false;
  if (!/^\d+$/.test(v)) return true;
  const n = Number(v);
  return n < MAX_INLINE_MIN || n > MAX_INLINE_MAX;
}

function languageName(code: string): string {
  try {
    const name = new Intl.DisplayNames([code], { type: "language" }).of(code);
    return name && name !== code ? `${name} (${code})` : code;
  } catch {
    return code;
  }
}

/** "set for the workspace" vs "install default". */
function LayerBadge({ set }: { set: boolean }) {
  const t = useT();
  return set ? (
    <Badge variant="brand" className="ml-2 text-[9px]">
      {t("admin.reviewDefaults.badgeWorkspace")}
    </Badge>
  ) : (
    <Badge variant="outline" className="ml-2 text-[9px] font-normal">
      {t("admin.reviewPolicies.detail.badgeInstallDefault")}
    </Badge>
  );
}

function ResetButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  const t = useT();
  return (
    <Button type="button" variant="ghost" size="sm" onClick={onClick} disabled={disabled}>
      <RotateCcwIcon className="h-3.5 w-3.5 mr-1" />
      {t("admin.reviewDefaults.resetToInstall")}
    </Button>
  );
}

/** "N repositories override this here" — only when some do. */
function OverrideCount({ count }: { count: number | undefined }) {
  const t = useT();
  if (!count) return null;
  return (
    <p className="text-xs text-[var(--color-muted-foreground)]">
      {t("admin.reviewDefaults.repoOverrides", { count })}{" "}
      <Link className="underline" href="/admin/review-policies">
        {t("nav.reviewPolicies")}
      </Link>
    </p>
  );
}

export default function ReviewDefaultsPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();

  const defaults = useQuery({
    queryKey: ["review-defaults"],
    queryFn: () => reviewDefaultsApi.get(token!),
    enabled: !!token,
  });
  // The per-agent rows read the workspace LLM config: stored overrides plus
  // what each agent runs with — the same answer /settings/llm shows.
  const llmConfig = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => llmApi.getConfig(token!),
    enabled: !!token,
  });

  const urlTab = useSyncExternalStore(noSubscribe, tabFromUrl, () => null);
  const [chosenTab, setChosenTab] = useState<DefaultsTab | null>(null);
  const activeTab: DefaultsTab = chosenTab ?? urlTab ?? "agents";
  const setActiveTab = (tab: DefaultsTab) => {
    setChosenTab(tab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    window.history.replaceState(window.history.state, "", url.toString());
  };

  // Synced during render ("adjust state when a prop changes"), keyed by the
  // serialised data so a refetch that changed nothing never wipes typing.
  const dataKey = defaults.data ? JSON.stringify(defaults.data) : "";
  const [syncedKey, setSyncedKey] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  if (defaults.data && syncedKey !== dataKey) {
    setSyncedKey(dataKey);
    setDraft(draftFrom(defaults.data));
  }
  const agentsKey = JSON.stringify(llmConfig.data?.agents ?? {});
  const [syncedAgentsKey, setSyncedAgentsKey] = useState<string | null>(null);
  const [agentDrafts, setAgentDrafts] = useState<Record<ReviewAgent, AgentDraft>>(
    () => Object.fromEntries(REVIEW_AGENTS.map((a) => [a, agentDraftFrom(null)])) as
      Record<ReviewAgent, AgentDraft>,
  );
  if (llmConfig.data && syncedAgentsKey !== agentsKey) {
    setSyncedAgentsKey(agentsKey);
    setAgentDrafts(Object.fromEntries(REVIEW_AGENTS.map(
      (a) => [a, agentDraftFrom(llmConfig.data?.agents?.[a] ?? null)],
    )) as Record<ReviewAgent, AgentDraft>);
  }
  const [dirty, setDirty] = useState(false);
  const [agentsDirty, setAgentsDirty] = useState(false);
  const [branchDraft, setBranchDraft] = useState("");

  const agentModels = REVIEW_AGENTS.map(
    (agent) => agentDrafts[agent].model.trim()
      || llmConfig.data?.agents?.[agent]?.effective_model || "",
  );
  const agentCaps = useAgentCapabilities(agentModels);
  const capsByAgent: Record<string, ModelCapabilities | null> = Object.fromEntries(
    REVIEW_AGENTS.map((agent, i) => [agent, agentCaps[i].caps]),
  );
  const maxOutErrors = REVIEW_AGENTS.map(
    (agent, i) => agentMaxOutError(agentDrafts[agent].maxOut, agentCaps[i].caps),
  );

  const data = defaults.data;
  const canEdit = data?.can_edit === true;
  const install = data?.install ?? {};
  const update = (patch: Partial<Draft>) => {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));
    setDirty(true);
  };

  const globs = globLines(draft?.ignoreGlobs ?? "");
  const globsError = globError(globs);
  const inlineBad = maxInlineBad(draft?.maxInline ?? "");
  const blocked = maxOutErrors.some((e) => e !== null) || globsError !== null || inlineBad;

  const save = useMutation({
    mutationFn: () => {
      const d = draft!;
      const payload: WorkspaceReviewDefaultsUpdate = {
        disabled_agents: d.disabledAgents,
        enabled_agents: d.enabledAgents,
        ...d.carried,
        verifier_enabled: d.verifier,
        comment_min_severity: (d.threshold || null) as WorkspaceReviewDefaults["comment_min_severity"],
        max_inline_comments: d.maxInline.trim() ? Number(d.maxInline.trim()) : null,
        summary_enabled: d.summary,
        summary_instructions: d.summaryInstructions.trim() || null,
        started_comment_enabled: d.started,
        review_language: d.language || null,
        ignore_globs: globs.length ? globs : null,
        target_branches: d.targetBranches,
        suppressed_rules: d.suppressedRules === null ? null : globLines(d.suppressedRules),
      };
      // Sent WHOLE, and only once the LLM config has loaded and a row was
      // touched: a blank map is the server's spelling of "clear every
      // override", so an unloaded form must say nothing about it.
      if (llmConfig.data && agentsDirty) {
        const agents: Record<string, AgentLLMOverride> = {};
        REVIEW_AGENTS.forEach((agent) => {
          const entry = agentEntryToSave(
            agentDrafts[agent], llmConfig.data?.agents?.[agent], capsByAgent[agent],
          );
          if (entry) agents[agent] = entry;
        });
        payload.agents = agents;
      }
      return reviewDefaultsApi.save(token!, payload);
    },
    onSuccess: () => {
      toast.success(t("admin.reviewDefaults.saved"));
      setDirty(false);
      setAgentsDirty(false);
      void qc.invalidateQueries({ queryKey: ["review-defaults"] });
      void qc.invalidateQueries({ queryKey: ["review-policies"] });
      void qc.invalidateQueries({ queryKey: ["llm-config"] });
    },
    onError: (e) => toast.error(t("admin.reviewDefaults.saveError", { message: (e as Error).message })),
  });

  const disabledShown = draft?.disabledAgents ?? (install.disabled_agents as string[] | undefined) ?? [];
  const enabledShown = draft?.enabledAgents ?? (install.enabled_agents as string[] | undefined) ?? [];
  // An opt-in agent (built-in off) is on only when named in enabled_agents;
  // a name in disabled_agents still wins — the server's rule, in
  // src/review/review_defaults.py.
  const optIn = (agent: string) => data?.agent_participation_defaults?.[agent] === false;
  const agentsSet = draft !== null && (draft.disabledAgents !== null || draft.enabledAgents !== null);
  const finders = (data?.toggleable_agents ?? []).filter((a) => a !== "verifier");
  const verifierOn = draft?.verifier ?? Boolean(install.verifier_enabled);
  const summaryOn = draft?.summary ?? true;
  const startedOn = draft?.started ?? true;
  const branchesShown = draft?.targetBranches ?? [];

  const addBranches = () => {
    const parsed = branchDraft.split(/[\s,]+/).map((b) => b.trim()).filter(Boolean);
    if (!parsed.length || !draft) return;
    const next = [...(draft.targetBranches ?? [])];
    for (const b of parsed) if (!next.includes(b)) next.push(b);
    update({ targetBranches: next });
    setBranchDraft("");
  };

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<SlidersHorizontalIcon className="h-6 w-6" />}
        title={t("admin.reviewDefaults.title")}
        description={
          <>
            {t("admin.reviewDefaults.description")}{" "}
            <Link className="underline" href="/admin/review-policies">
              {t("admin.reviewDefaults.repoPoliciesLink")}
            </Link>
            .
          </>
        }
        tabs={<SectionTabs set="review" />}
      />

      <Callout tone="info">{t("admin.reviewDefaults.precedence")}</Callout>
      {defaults.error && !data && (
        <Callout tone="danger">
          {t("common.loadError")}: {(defaults.error as Error).message}
        </Callout>
      )}
      {data && !canEdit && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("admin.reviewDefaults.readOnly")}
        </p>
      )}

      <div className="flex flex-wrap gap-1 border-b border-[var(--color-border)] pb-2">
        {DEFAULTS_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setActiveTab(tab)}
            className={`rounded-md px-3 py-1.5 text-xs transition-colors ${
              activeTab === tab
                ? "bg-[var(--color-brand-muted)] font-medium text-[var(--color-brand)]"
                : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]"
            }`}
          >
            {t(`admin.reviewDefaults.tab.${tab}`)}
          </button>
        ))}
      </div>

      {draft && activeTab === "agents" && (<>
        <Card>
          <CardHeader>
            <div className="flex items-start justify-between gap-2">
              <div>
                <CardTitle>
                  {t("admin.reviewPolicies.detail.agentToggleTitle")}
                  <LayerBadge set={agentsSet} />
                </CardTitle>
                <CardDescription>{t("admin.reviewDefaults.agentsDesc")}</CardDescription>
              </div>
              {agentsSet && (
                <ResetButton
                  onClick={() => update({ disabledAgents: null, enabledAgents: null })}
                  disabled={!canEdit}
                />
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <OverrideCount count={data?.repo_overrides?.disabled_agents} />
            {finders.map((agent) => {
              const on = !disabledShown.includes(agent)
                && (!optIn(agent) || enabledShown.includes(agent));
              return (
                <div
                  key={agent}
                  className="flex items-start justify-between gap-4 rounded-lg border border-[var(--color-border)] p-3"
                >
                  <div className="min-w-0">
                    <Label htmlFor={`ws-toggle-${agent}`} className="font-medium capitalize">
                      {agentLabel(agent)}
                      {!on && (
                        <Badge variant="destructive" className="ml-2 text-[9px]">
                          {t("admin.reviewPolicies.detail.agentOffBadge")}
                        </Badge>
                      )}
                    </Label>
                    <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">
                      {t(`admin.reviewPolicies.agentRole.${agent}`)}
                    </p>
                  </div>
                  <Switch
                    id={`ws-toggle-${agent}`}
                    checked={on}
                    disabled={!canEdit}
                    onCheckedChange={(v) => {
                      const cur = draft.disabledAgents ?? [];
                      if (optIn(agent)) {
                        const en = (draft.enabledAgents ?? []).filter((a) => a !== agent);
                        update({
                          enabledAgents: v ? [...en, agent] : en,
                          ...(v && cur.includes(agent)
                            ? { disabledAgents: cur.filter((a) => a !== agent) }
                            : {}),
                        });
                        return;
                      }
                      update({
                        disabledAgents: v ? cur.filter((a) => a !== agent) : [...cur, agent],
                      });
                    }}
                  />
                </div>
              );
            })}
            <Callout tone="info">{t("admin.reviewPolicies.detail.agentToggleCostNote")}</Callout>
            <div className="flex items-start justify-between gap-4 rounded-lg border border-[var(--color-border)] p-3">
              <div className="min-w-0">
                <Label htmlFor="ws-toggle-verifier" className="font-medium capitalize">
                  verifier
                  <LayerBadge set={draft.verifier !== null} />
                </Label>
                <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">
                  {t("admin.reviewPolicies.detail.verifierOptIn")}
                </p>
                <OverrideCount count={data?.repo_overrides?.verifier_enabled} />
                {draft.verifier !== null && (
                  <ResetButton onClick={() => update({ verifier: null })} disabled={!canEdit} />
                )}
              </div>
              <Switch
                id="ws-toggle-verifier"
                checked={verifierOn}
                disabled={!canEdit}
                onCheckedChange={(v) => update({ verifier: v })}
              />
            </div>
          </CardContent>
        </Card>

        <Card id="agent-models">
          <CardHeader>
            <CardTitle>{t("admin.reviewDefaults.modelsTitle")}</CardTitle>
            <CardDescription>
              {t("admin.reviewDefaults.modelsDesc")}{" "}
              <Link className="underline" href="/settings/llm#review-agents">
                {t("admin.reviewPolicies.detail.linkLlmSetup")}
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Callout tone="info">
              {t("settings.llm.agents.budgetNote", { tokens: DEFAULT_AGENT_MAX_OUTPUT })}
            </Callout>
            <OverrideCount count={data?.repo_overrides?.agents} />
            {REVIEW_AGENTS.map((agent, i) => (
              <AgentLLMRow
                key={agent}
                agent={agent}
                draft={agentDrafts[agent]}
                stored={llmConfig.data?.agents?.[agent] ?? null}
                effective={llmConfig.data?.agents?.[agent] ?? null}
                inheritedPending={!llmConfig.data}
                model={agentModels[i]}
                caps={agentCaps[i].caps}
                loading={agentCaps[i].loading}
                failed={agentCaps[i].failed}
                limit={agentMaxOutLimit(agentCaps[i].caps)}
                error={maxOutErrors[i]}
                disabled={!canEdit || !llmConfig.data}
                onChange={(patch) => {
                  setAgentsDirty(true);
                  setDirty(true);
                  setAgentDrafts((prev) => ({ ...prev, [agent]: { ...prev[agent], ...patch } }));
                }}
              />
            ))}
          </CardContent>
        </Card>
      </>)}

      {draft && activeTab === "comments" && (<>
        <Card>
          <CardHeader>
            <CardTitle>{t("admin.reviewPolicies.detail.commentsTitle")}</CardTitle>
            <CardDescription>{t("admin.reviewDefaults.commentsDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-1">
              <Label htmlFor="ws-threshold">
                {t("review.settings.thresholdLabel")}
                <LayerBadge set={!!draft.threshold} />
              </Label>
              <Select
                id="ws-threshold"
                className="w-full sm:w-80"
                value={draft.threshold}
                disabled={!canEdit}
                onChange={(v) => update({ threshold: v })}
                options={THRESHOLDS.map((level) => ({
                  value: level,
                  label: t(`review.settings.threshold.${level || "all"}`),
                }))}
              />
              <OverrideCount count={data?.repo_overrides?.comment_min_severity} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ws-max-inline">
                {t("admin.reviewPolicies.detail.maxInlineLabel")}
                <LayerBadge set={!!draft.maxInline.trim()} />
              </Label>
              <Input
                id="ws-max-inline"
                type="number"
                inputMode="numeric"
                min={MAX_INLINE_MIN}
                max={MAX_INLINE_MAX}
                className="w-full sm:w-40"
                placeholder={String(install.max_inline_comments ?? "")}
                value={draft.maxInline}
                disabled={!canEdit}
                aria-invalid={inlineBad ? true : undefined}
                onChange={(e) => update({ maxInline: e.target.value })}
              />
              {inlineBad && (
                <p role="alert" className="text-xs text-red-600 dark:text-red-400">
                  {t("admin.reviewPolicies.detail.maxInlineInvalid")}
                </p>
              )}
              <OverrideCount count={data?.repo_overrides?.max_inline_comments} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ws-language">
                {t("admin.reviewPolicies.detail.languageLabel")}
                <LayerBadge set={!!draft.language} />
              </Label>
              <Select
                id="ws-language"
                className="w-full sm:w-80"
                value={draft.language}
                disabled={!canEdit}
                onChange={(v) => update({ language: v })}
                options={[
                  { value: "", label: languageName("en") },
                  ...(data?.review_languages ?? []).filter((c) => c !== "en").map((code) => ({
                    value: code, label: languageName(code),
                  })),
                ]}
              />
              <OverrideCount count={data?.repo_overrides?.review_language} />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("admin.reviewPolicies.detail.summaryTitle")}</CardTitle>
            <CardDescription>{t("admin.reviewPolicies.detail.summaryDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div>
                <Label htmlFor="ws-summary" className="font-medium">
                  {t("admin.reviewPolicies.detail.summaryEnabledLabel")}
                  <LayerBadge set={draft.summary !== null} />
                </Label>
                <OverrideCount count={data?.repo_overrides?.summary_enabled} />
                {draft.summary !== null && (
                  <ResetButton onClick={() => update({ summary: null })} disabled={!canEdit} />
                )}
              </div>
              <Switch
                id="ws-summary"
                checked={summaryOn}
                disabled={!canEdit}
                onCheckedChange={(v) => update({ summary: v })}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ws-summary-instructions">
                {t("admin.reviewPolicies.detail.summaryInstructionsLabel")}
                <LayerBadge set={!!draft.summaryInstructions.trim()} />
              </Label>
              <Textarea
                id="ws-summary-instructions"
                rows={4}
                maxLength={SUMMARY_INSTRUCTIONS_MAX}
                disabled={!canEdit || !summaryOn}
                placeholder={t("admin.reviewPolicies.detail.summaryInstructionsPlaceholder")}
                value={draft.summaryInstructions}
                onChange={(e) => update({ summaryInstructions: e.target.value })}
              />
              <OverrideCount count={data?.repo_overrides?.summary_instructions} />
            </div>
            <div className="flex items-start justify-between gap-4 border-t border-[var(--color-border)] pt-4">
              <div>
                <Label htmlFor="ws-started" className="font-medium">
                  {t("admin.reviewPolicies.detail.startedCommentLabel")}
                  <LayerBadge set={draft.started !== null} />
                </Label>
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("admin.reviewPolicies.detail.startedCommentHint")}
                </p>
                <OverrideCount count={data?.repo_overrides?.started_comment_enabled} />
                {draft.started !== null && (
                  <ResetButton onClick={() => update({ started: null })} disabled={!canEdit} />
                )}
              </div>
              <Switch
                id="ws-started"
                checked={startedOn}
                disabled={!canEdit}
                onCheckedChange={(v) => update({ started: v })}
              />
            </div>
          </CardContent>
        </Card>
      </>)}

      {draft && activeTab === "ignore" && (<>
        <Card>
          <CardHeader>
            <CardTitle>
              {t("admin.reviewPolicies.detail.ignoreTitle")}
              <LayerBadge set={globs.length > 0} />
            </CardTitle>
            <CardDescription>{t("admin.reviewDefaults.ignoreDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-1">
            <Label htmlFor="ws-ignore-globs">{t("review.settings.globsLabel")}</Label>
            <Textarea
              id="ws-ignore-globs"
              rows={8}
              spellCheck={false}
              className="font-mono text-xs"
              placeholder={"docs/**\n*.snap\nmigrations/*.py"}
              value={draft.ignoreGlobs}
              disabled={!canEdit}
              aria-invalid={globsError ? true : undefined}
              onChange={(e) => update({ ignoreGlobs: e.target.value })}
            />
            {globsError ? (
              <p role="alert" className="text-xs text-red-600 dark:text-red-400">
                {t(globsError.key, { line: globsError.line })}
              </p>
            ) : (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("review.settings.globsHint")}
              </p>
            )}
            <OverrideCount count={data?.repo_overrides?.ignore_globs} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-start justify-between gap-2">
              <div>
                <CardTitle>
                  {t("admin.reviewPolicies.detail.branchesTitle")}
                  <LayerBadge set={draft.targetBranches !== null} />
                </CardTitle>
                <CardDescription>{t("admin.reviewDefaults.branchesDesc")}</CardDescription>
              </div>
              {draft.targetBranches !== null && (
                <ResetButton onClick={() => update({ targetBranches: null })} disabled={!canEdit} />
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <Callout tone={branchesShown.length === 0 ? "info" : "success"}>
              {branchesShown.length === 0
                ? t("admin.reviewDefaults.branchesAll")
                : t("admin.reviewPolicies.detail.branchesSemanticsFiltered", {
                    branches: branchesShown.join(", "),
                  })}
            </Callout>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.branchesExactMatchNote")}
            </p>
            <div className="flex flex-wrap gap-2">
              {branchesShown.map((b) => (
                <Badge key={b} variant="outline">
                  {b}
                  <button
                    type="button"
                    disabled={!canEdit}
                    aria-label={t("admin.reviewPolicies.detail.removeItem", { item: b })}
                    onClick={() => {
                      const next = branchesShown.filter((x) => x !== b);
                      update({ targetBranches: next.length ? next : null });
                    }}
                    className="ml-1 text-[10px] opacity-70 hover:opacity-100"
                  >
                    ✕
                  </button>
                </Badge>
              ))}
            </div>
            <div className="flex gap-2">
              <Input
                aria-label={t("admin.reviewPolicies.detail.branchesAddLabel")}
                className="flex-1"
                placeholder={t("admin.reviewPolicies.detail.branchesAddPlaceholder")}
                value={branchDraft}
                disabled={!canEdit}
                onChange={(e) => setBranchDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addBranches();
                  }
                }}
              />
              <Button variant="outline" onClick={addBranches} disabled={!canEdit || !branchDraft.trim()}>
                {t("admin.reviewPolicies.detail.branchesAddButton")}
              </Button>
            </div>
            <OverrideCount count={data?.repo_overrides?.target_branches} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-start justify-between gap-2">
              <div>
                <CardTitle>
                  {t("admin.reviewPolicies.detail.suppressedTitle")}
                  <LayerBadge set={draft.suppressedRules !== null} />
                </CardTitle>
                <CardDescription>{t("admin.reviewPolicies.detail.suppressedDesc")}</CardDescription>
              </div>
              {draft.suppressedRules !== null && (
                <ResetButton onClick={() => update({ suppressedRules: null })} disabled={!canEdit} />
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-2">
            <Textarea
              aria-label={t("admin.reviewPolicies.detail.suppressedTitle")}
              rows={6}
              spellCheck={false}
              className="font-mono text-xs"
              disabled={!canEdit}
              placeholder={((install.suppressed_rules as string[] | undefined) ?? []).join("\n")}
              value={draft.suppressedRules ?? ""}
              onChange={(e) => update({ suppressedRules: e.target.value })}
            />
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewDefaults.suppressedHint")}
            </p>
            <OverrideCount count={data?.repo_overrides?.suppressed_rules} />
          </CardContent>
        </Card>
      </>)}

      <div className="clear-agent-launcher flex items-center justify-end gap-2 sticky bottom-0 bg-[var(--color-background)] border-t border-[var(--color-border)] py-3">
        {dirty && !blocked && (
          <span className="text-xs text-[var(--color-muted-foreground)]">
            {t("admin.reviewPolicies.detail.unsavedChanges")}
          </span>
        )}
        {blocked && (
          <span className="text-xs text-red-600 dark:text-red-400">
            {t("admin.reviewDefaults.saveBlocked")}
          </span>
        )}
        <Button
          onClick={() => save.mutate()}
          disabled={!canEdit || !draft || !dirty || blocked || save.isPending}
        >
          <SaveIcon className="h-4 w-4 mr-1" />
          {save.isPending
            ? t("admin.reviewPolicies.detail.saving")
            : t("admin.reviewPolicies.detail.save")}
        </Button>
      </div>
    </PageShell>
  );
}
