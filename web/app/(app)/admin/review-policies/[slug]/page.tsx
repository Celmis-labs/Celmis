"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeftIcon,
  EyeIcon,
  HelpCircleIcon,
  PlusIcon,
  RotateCcwIcon,
  SaveIcon,
  Trash2Icon,
} from "lucide-react";

import {
  llmApi,
  reviewPoliciesApi,
  type AgentLLMOverride,
  type FolderRule,
  type ModelCapabilities,
  type ReviewPolicy,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import {
  AgentLLMRow, DEFAULT_AGENT_MAX_OUTPUT, agentDraftFrom, agentEntryToSave,
  agentMaxOutError, agentMaxOutLimit, storedReasoning, useAgentCapabilities,
  type AgentDraft,
} from "@/components/agent-llm-controls";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { HelpButton } from "@/components/ui/help-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip } from "@/components/ui/tooltip";

/** Local tabs splitting the long form into digestible groups. */
type PolicyTab = "general" | "prompt" | "models" | "mcp" | "agents";
const POLICY_TABS: PolicyTab[] = ["general", "prompt", "models", "mcp", "agents"];

/** Agents the orchestrator dispatches per PR — these can be switched off.
 *  Mirrors `TOGGLEABLE_AGENTS` in src/api/routers/review_policies.py. The
 *  verifier is absent on purpose: it post-processes the others' findings. */
const TOGGLEABLE_AGENTS = [
  "defect", "contract", "security", "structural",
] as const;

/** Every agent involved in a review, for the help dialog. */
const ALL_AGENTS = [...TOGGLEABLE_AGENTS, "verifier"] as const;

/** The agents whose LLM this policy can set, and the column each one's model
 *  lives in.
 *
 *  Not `REVIEW_AGENTS`: that list includes `compliance`, which the backend
 *  accepts a ceiling and a reasoning level for here but has no model column
 *  for — it inherits its model from /settings/llm. Rather than render one row
 *  with its model control missing and no way to say why, the compliance agent
 *  stays on the workspace screen, which owns all three of its settings. The
 *  five below are the five this page has always shown.
 */
/** The DB columns kept their pre-restructure names — no migration, and every
 *  stored pin keeps working. The resolver maps each column to the agent that
 *  inherited the remit: architect's column drives contract, quality's drives
 *  defect. tests_model maps to no agent and is no longer editable here. */
const POLICY_AGENT_MODEL_FIELD = {
  defect: "quality_model",
  contract: "architect_model",
  security: "security_model",
  verifier: "verifier_model",
} as const;
type PolicyAgent = keyof typeof POLICY_AGENT_MODEL_FIELD;
const POLICY_AGENTS = Object.keys(POLICY_AGENT_MODEL_FIELD) as PolicyAgent[];

/** A blank form — every box empty, which at every layer means "inherit". */
function emptyAgentDrafts(): Record<PolicyAgent, AgentDraft> {
  return Object.fromEntries(
    POLICY_AGENTS.map((agent) => [agent, agentDraftFrom(null)]),
  ) as Record<PolicyAgent, AgentDraft>;
}

/** The stored policy as five form rows.
 *
 *  Two sources, one row: the model comes from this agent's own column (where
 *  it has lived since Stage 11) and the other two from `agent_llm_overrides`,
 *  which never carries a model. The split is the server's, not the form's —
 *  the form edits one setting per box either way.
 */
function agentDraftsFrom(policy: ReviewPolicy): Record<PolicyAgent, AgentDraft> {
  const llm = policy.agent_llm_overrides ?? {};
  return Object.fromEntries(
    POLICY_AGENTS.map((agent) => [agent, agentDraftFrom({
      model: policy[POLICY_AGENT_MODEL_FIELD[agent]] ?? null,
      max_output_tokens: llm[agent]?.max_output_tokens ?? null,
      reasoning: llm[agent]?.reasoning ?? null,
    })]),
  ) as Record<PolicyAgent, AgentDraft>;
}

/** The `agent_llm_overrides` map this form is about to PUT.
 *
 *  READ `reasoningToSave` IN components/agent-llm-controls.tsx BEFORE
 *  CHANGING THIS. The map REPLACES the stored one — absent is the only
 *  spelling of "stop overriding" the inheritance chain has — so every field
 *  this function declines to emit is a field the save DELETES. That is not a
 *  hypothetical: the workspace screen shipped with a `reasoning` that was sent
 *  only once the capabilities lookup had answered, and `caps` is null in three
 *  states that say nothing about the model (in flight, errored with
 *  `retry: false`, no model to ask about). Pressing Save in any of them wiped
 *  a configured override silently. The layer this file edits WINS over that
 *  one, so the same press here would be worse.
 *
 *  No `model` key is ever emitted: the model of this layer is the
 *  `<agent>_model` column, and the router 422s an entry that carries one.
 */
function policyAgentLLMOverrides(
  agents: readonly string[],
  drafts: Record<string, AgentDraft>,
  stored: Record<string, AgentLLMOverride> | undefined,
  caps: Record<string, ModelCapabilities | null>,
): Record<string, AgentLLMOverride> {
  const out: Record<string, AgentLLMOverride> = {};
  for (const agent of agents) {
    const entry = agentEntryToSave(
      drafts[agent], stored?.[agent], caps[agent] ?? null, { withModel: false },
    );
    if (entry) out[agent] = entry;
  }
  return out;
}

export default function ReviewPolicyEditPage() {
  const params = useParams<{ slug: string }>();
  const router = useRouter();
  const slug = decodeURIComponent(params.slug);
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { confirm, dialog } = useConfirm();

  const policy = useQuery({
    queryKey: ["review-policies", "detail", slug],
    queryFn: () => reviewPoliciesApi.get(token!, slug),
    enabled: !!token,
  });

  const branches = useQuery({
    queryKey: ["review-policies", "branches", slug],
    queryFn: () => reviewPoliciesApi.branches(token!, slug),
    enabled: !!token,
  });

  const [enabled, setEnabled] = useState(true);
  const [department, setDepartment] = useState("");
  const [promptTemplate, setPromptTemplate] = useState("");
  const [targetBranches, setTargetBranches] = useState<string[]>([]);
  const [folderRules, setFolderRules] = useState<FolderRule[]>([]);
  /** Model, output ceiling and reasoning per agent, as one draft each — the
   *  same three-field shape /settings/llm edits, because it is the same three
   *  settings and this layer simply outranks that one. */
  const [agentDrafts, setAgentDrafts] = useState<Record<PolicyAgent, AgentDraft>>(
    () => emptyAgentDrafts(),
  );
  const [promptOverrides, setPromptOverrides] = useState<
    Record<"defect" | "contract" | "security" | "verifier", string>
  >({
    defect: "", contract: "", security: "", verifier: "",
  });
  const [previewAgent, setPreviewAgent] = useState<string | null>(null);
  const [disabledAgents, setDisabledAgents] = useState<string[]>([]);
  // The veto is a stage, not an agent, and it is OFF unless this repo
  // asks. Its own boolean rather than an entry in the agent deny-list:
  // squeezing a stage into that list is what made the default
  // un-invertible on the server.
  const [verifierEnabled, setVerifierEnabled] = useState(false);
  const [mcpSources, setMcpSources] = useState<Array<{
    name: string; url: string; auth_type: string;
    api_key_ref: string | null;
    allowed_tools: string[]; trigger_patterns: string[];
  }>>([]);
  const [dirty, setDirty] = useState(false);
  const [activeTab, setActiveTab] = useState<PolicyTab>("general");
  const [helpOpen, setHelpOpen] = useState(false);
  /** Free-text branch entry — the discovered list is empty when the repo has
   *  no local clone, so target branches must still be typeable by hand. */
  const [branchDraft, setBranchDraft] = useState("");

  // Warn before closing/reloading the tab while there are unsaved edits.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!policy.data) return;
    setEnabled(policy.data.enabled);
    setDepartment(policy.data.department ?? "");
    setPromptTemplate(policy.data.prompt_template);
    setTargetBranches(policy.data.target_branches);
    setFolderRules(policy.data.folder_rules);
    setAgentDrafts(agentDraftsFrom(policy.data));
    const po = policy.data.agent_prompt_overrides ?? {};
    setPromptOverrides({
      defect: po.defect ?? "",
      contract: po.contract ?? "",
      security: po.security ?? "",
      verifier: po.verifier ?? "",
    });
    setMcpSources(policy.data.mcp_sources ?? []);
    setDisabledAgents(policy.data.disabled_agents ?? []);
    // `verifier_enabled` is what THIS repo said; `_effective` is what a
    // review would do, deny-list and install default folded in. A switch
    // has to show the second — it is the answer the reader is checking.
    setVerifierEnabled(
      policy.data.verifier_enabled ?? policy.data.verifier_enabled_effective ?? false,
    );
    setDirty(false);
  }, [policy.data]);

  // The workspace layer, read here for one reason: it is what an empty box on
  // this page inherits. NOT `policy.data.agents_effective` — that walks the
  // WHOLE chain including this very policy, so on the layer that wins it would
  // answer "the workspace default is X" with X being this repo's own override,
  // and the sentence beside the box would be false exactly when it matters.
  // Shares the query key with /settings/llm, so arriving from there costs
  // nothing.
  const wsConfig = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => llmApi.getConfig(token!),
    enabled: !!token,
  });

  // The model each agent will actually call once this draft is saved: the
  // override typed here, else whatever the workspace resolved. Handed to the
  // capabilities endpoint verbatim — no vendor prefix is derived in
  // TypeScript, see the note atop components/agent-llm-controls.tsx.
  const agentModels = POLICY_AGENTS.map(
    (agent) =>
      agentDrafts[agent].model.trim()
      || wsConfig.data?.agents?.[agent]?.effective_model
      || "",
  );
  // At page level, not row level: the Save button has to refuse a ceiling
  // above what the model accepts before the row that knows the ceiling has
  // rendered.
  const agentCaps = useAgentCapabilities(agentModels);
  const capsByAgent: Record<string, ModelCapabilities | null> = Object.fromEntries(
    POLICY_AGENTS.map((agent, i) => [agent, agentCaps[i].caps]),
  );
  const maxOutErrors = POLICY_AGENTS.map(
    (agent, i) => agentMaxOutError(agentDrafts[agent].maxOut, agentCaps[i].caps),
  );
  const agentLLMBlocked = maxOutErrors.some((e) => e !== null);

  /** Rows about to save a reasoning value the operator can neither see nor
   *  edit, because their capabilities lookup gave no answer and will not.
   *  In flight is excluded — it answers in a moment, and listing it would
   *  flash a callout in and out on every page load. What is left is errored
   *  (`retry: false`, so one blip is final) and no model to ask about. */
  const preservingReasoning = POLICY_AGENTS.filter(
    (agent, i) =>
      agentCaps[i].caps === null
      && !agentCaps[i].loading
      && storedReasoning(policy.data?.agent_llm_overrides?.[agent]) != null,
  );

  const save = useMutation({
    mutationFn: () =>
      reviewPoliciesApi.upsert(token!, slug, {
        enabled,
        prompt_template: promptTemplate,
        target_branches: targetBranches,
        folder_rules: folderRules,
        department: department || null,
        // Column names predate the restructure — see POLICY_AGENT_MODEL_FIELD.
        architect_model: agentDrafts.contract.model.trim() || null,
        security_model: agentDrafts.security.model.trim() || null,
        quality_model: agentDrafts.defect.model.trim() || null,
        // Echoed, not cleared. It maps to no agent since the restructure and
        // this form has no box for it — but a PUT is a full replace, and
        // writing null would silently discard a pin the operator set before
        // the rename. The list page's toggle already echoes it for the same
        // reason; two writers of one field must not disagree about whether it
        // survives a save.
        tests_model: policy.data?.tests_model ?? null,
        verifier_model: agentDrafts.verifier.model.trim() || null,
        // Sent WHOLE and only once the policy has loaded. Before that the
        // drafts are blank, and a blank map is the server's spelling of
        // "clear every override" — so omitting the key, which the server
        // reads as "keep what is stored", is the only safe thing to send
        // from a form that has not been filled in yet.
        agent_llm_overrides: policy.data
          ? policyAgentLLMOverrides(
              POLICY_AGENTS, agentDrafts, policy.data.agent_llm_overrides, capsByAgent,
            )
          : undefined,
        agent_prompt_overrides: Object.fromEntries(
          Object.entries(promptOverrides).filter(([, v]) => v.trim()),
        ),
        mcp_sources: mcpSources,
        // Turning the veto on has to clear the OLD spelling of off as
        // well, or the deny-list keeps winning and the switch looks
        // broken to whoever just flipped it.
        disabled_agents: verifierEnabled
          ? disabledAgents.filter((a) => a !== "verifier")
          : disabledAgents,
        verifier_enabled: verifierEnabled,
      }),
    onSuccess: () => {
      toast.success(t("admin.reviewPolicies.detail.saveSuccess"));
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["review-policies"] });
    },
    onError: (e) =>
      toast.error(
        t("admin.reviewPolicies.detail.saveError", { message: (e as Error).message }),
      ),
  });

  const reset = useMutation({
    mutationFn: () => reviewPoliciesApi.reset(token!, slug),
    onSuccess: () => {
      toast.success(t("admin.reviewPolicies.detail.resetSuccess"));
      qc.invalidateQueries({ queryKey: ["review-policies"] });
      qc.invalidateQueries({ queryKey: ["review-policies", "detail", slug] });
    },
    onError: (e) =>
      toast.error(
        t("admin.reviewPolicies.detail.resetError", { message: (e as Error).message }),
      ),
  });

  const toggleBranch = (b: string) => {
    setDirty(true);
    setTargetBranches((prev) =>
      prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b],
    );
  };

  /** Accepts "main, develop" / "main develop" and appends the new names.
   *  Matching on the backend is exact, so names are kept verbatim. */
  const addBranchDraft = () => {
    const parsed = branchDraft
      .split(/[\s,]+/)
      .map((b) => b.trim())
      .filter(Boolean);
    if (parsed.length === 0) return;
    setTargetBranches((prev) => {
      const next = [...prev];
      for (const b of parsed) if (!next.includes(b)) next.push(b);
      return next;
    });
    setBranchDraft("");
    setDirty(true);
  };

  const updateFolderRule = (idx: number, patch: Partial<FolderRule>) => {
    setDirty(true);
    setFolderRules((prev) =>
      prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)),
    );
  };

  const addFolderRule = () => {
    setDirty(true);
    setFolderRules((prev) => [...prev, { pattern: "", prompt: "" }]);
  };

  const removeFolderRule = (idx: number) => {
    setDirty(true);
    setFolderRules((prev) => prev.filter((_, i) => i !== idx));
  };

  // A repo with no saved policy is NOT an error here — GET synthesizes the
  // defaults, so the form opens on them and the first save creates the row.
  // A genuine failure is different: the form would render its blank initial
  // state, and saving would replace whatever is actually stored. Guard only
  // the never-loaded case, so a failed background refetch cannot wipe a page
  // that is already showing real data.
  if (policy.error && !policy.data) {
    return (
      <PageShell width="wide">
        <Link
          href="/admin/review-policies"
          className="text-sm text-[var(--color-muted-foreground)] inline-flex items-center gap-1 hover:underline"
        >
          <ArrowLeftIcon className="h-3.5 w-3.5" />
          {t("admin.reviewPolicies.detail.backToList")}
        </Link>
        <Callout tone="danger">
          {t("common.loadError")}: {(policy.error as Error).message}
        </Callout>
      </PageShell>
    );
  }

  return (
    <PageShell width="wide">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            href="/admin/review-policies"
            className="text-sm text-[var(--color-muted-foreground)] inline-flex items-center gap-1 hover:underline"
          >
            <ArrowLeftIcon className="h-3.5 w-3.5" />
            {t("admin.reviewPolicies.detail.backToList")}
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight mt-2 truncate">
            {slug}
          </h1>
          <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
            {t("admin.reviewPolicies.detail.subtitle")}
          </p>
        </div>
        <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("admin.reviewPolicies.helpTitle")} />
      </div>

      <SectionTabs set="review" />

      <div className="flex flex-wrap gap-1 border-b border-[var(--color-border)] pb-2">
        {POLICY_TABS.map((tab) => (
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
            {t(`admin.reviewPolicies.detail.tab.${tab}`)}
          </button>
        ))}
      </div>

      {activeTab === "general" && (<>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.generalTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.generalDesc")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label htmlFor="enabled" className="font-medium">
                {t("admin.reviewPolicies.detail.enabledLabel")}
              </Label>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.enabledHint")}
              </p>
            </div>
            <Switch
              id="enabled"
              checked={enabled}
              onCheckedChange={(v) => {
                setEnabled(v);
                setDirty(true);
              }}
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="department">
              {t("admin.reviewPolicies.detail.departmentLabel")}
            </Label>
            <Input
              id="department"
              placeholder={t("admin.reviewPolicies.detail.departmentPlaceholder")}
              value={department}
              onChange={(e) => {
                setDepartment(e.target.value);
                setDirty(true);
              }}
            />
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.departmentHint")}
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-1.5">
            {t("admin.reviewPolicies.detail.branchesTitle")}
            <Tooltip label={t("admin.reviewPolicies.detail.branchesTooltip")}>
              <HelpCircleIcon className="h-3.5 w-3.5 text-[var(--color-muted-foreground)]" />
            </Tooltip>
          </CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.branchesDesc")}
            {branches.data?.default_branch && (
              <> {t("admin.reviewPolicies.detail.branchesDefaultMark")}</>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {/* Live read-out of the rule the backend actually applies
              (src/review/orchestrator.py — skip only when the list is
              non-empty and the PR base branch is not in it). */}
          <Callout tone={targetBranches.length === 0 ? "info" : "success"}>
            {targetBranches.length === 0
              ? t("admin.reviewPolicies.detail.branchesSemanticsAll")
              : t("admin.reviewPolicies.detail.branchesSemanticsFiltered", {
                  branches: targetBranches.join(", "),
                })}
          </Callout>
          <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
            {t("admin.reviewPolicies.detail.branchesExactMatchNote")}
          </p>
          {branches.isLoading && (
            <p className="text-sm">
              {t("admin.reviewPolicies.detail.branchesLoading")}
            </p>
          )}
          {branches.data && branches.data.branches.length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.branchesNotClonedBefore")}{" "}
              <code className="px-1 rounded bg-[var(--color-muted)]">
                analyzer sync {slug}
              </code>{" "}
              {t("admin.reviewPolicies.detail.branchesNotClonedAfter")}
            </p>
          )}
          <div className="flex flex-wrap gap-2 mt-2">
            {(branches.data?.branches ?? []).map((b) => {
              const checked = targetBranches.includes(b);
              return (
                <button
                  key={b}
                  type="button"
                  onClick={() => toggleBranch(b)}
                  className={`text-xs rounded border px-2 py-1 transition-colors ${
                    checked
                      ? "bg-[var(--color-primary)] text-[var(--color-primary-foreground)] border-transparent"
                      : "border-[var(--color-border)] hover:bg-[var(--color-accent)]"
                  }`}
                >
                  {b}
                  {branches.data?.default_branch === b && " ★"}
                </button>
              );
            })}
          </div>
          {/* Show any custom-typed branches not in the discovered list */}
          {targetBranches.filter(
            (b) => !(branches.data?.branches ?? []).includes(b),
          ).length > 0 && (
            <div className="mt-3">
              <p className="text-xs text-[var(--color-muted-foreground)] mb-1">
                {t("admin.reviewPolicies.detail.branchesCustomLabel")}
              </p>
              <div className="flex flex-wrap gap-2">
                {targetBranches
                  .filter((b) => !(branches.data?.branches ?? []).includes(b))
                  .map((b) => (
                    <Badge key={b} variant="outline">
                      {b}
                      <button
                        type="button"
                        onClick={() => toggleBranch(b)}
                        className="ml-1 text-[10px] opacity-70 hover:opacity-100"
                      >
                        ✕
                      </button>
                    </Badge>
                  ))}
              </div>
            </div>
          )}

          {/* Manual entry — the discovered list is empty until the repo is
              cloned, and a target branch may not exist locally yet. */}
          <div className="mt-4 space-y-1">
            <Label htmlFor="branch-add">
              {t("admin.reviewPolicies.detail.branchesAddLabel")}
            </Label>
            <div className="flex gap-2">
              <Input
                id="branch-add"
                className="flex-1"
                placeholder={t("admin.reviewPolicies.detail.branchesAddPlaceholder")}
                value={branchDraft}
                onChange={(e) => setBranchDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addBranchDraft();
                  }
                }}
              />
              <Button
                variant="outline"
                onClick={addBranchDraft}
                disabled={!branchDraft.trim()}
              >
                <PlusIcon className="h-4 w-4 mr-1" />
                {t("admin.reviewPolicies.detail.branchesAddButton")}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
      </>)}

      {activeTab === "prompt" && (<>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.promptTemplateTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.promptTemplateDesc")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Textarea
            rows={10}
            value={promptTemplate}
            onChange={(e) => {
              setPromptTemplate(e.target.value);
              setDirty(true);
            }}
            placeholder={t("admin.reviewPolicies.detail.promptTemplatePlaceholder")}
          />
          <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
            {t("admin.reviewPolicies.detail.promptTemplateCounter", {
              count: promptTemplate.length,
            })}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.folderRulesTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.folderRulesDesc")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {folderRules.length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.folderRulesEmpty")}
            </p>
          )}
          {folderRules.map((fr, idx) => (
            <div
              key={idx}
              className="rounded-lg border border-[var(--color-border)] p-3 space-y-2"
            >
              <div className="flex items-center gap-2">
                <Input
                  placeholder="src/api/**/*.py"
                  value={fr.pattern}
                  onChange={(e) =>
                    updateFolderRule(idx, { pattern: e.target.value })
                  }
                  className="flex-1"
                />
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => removeFolderRule(idx)}
                  title={t("admin.reviewPolicies.detail.removeRuleTitle")}
                >
                  <Trash2Icon className="h-4 w-4" />
                </Button>
              </div>
              <Textarea
                rows={3}
                placeholder={t("admin.reviewPolicies.detail.folderRulePromptPlaceholder")}
                value={fr.prompt}
                onChange={(e) => updateFolderRule(idx, { prompt: e.target.value })}
              />
            </div>
          ))}
          <Button variant="outline" onClick={addFolderRule}>
            <PlusIcon className="h-4 w-4 mr-1" /> {t("admin.reviewPolicies.detail.addFolderRule")}
          </Button>
        </CardContent>
      </Card>
      </>)}

      {/* ─── Per-agent LLM: model, output ceiling, reasoning ────────
          This is the layer that WINS — a repo policy beats the workspace
          `agents` entry, which beats the review profile. It used to show five
          model dropdowns and nothing else, so the screen with the most
          authority showed the least: an operator could pick a model here
          without ever learning that a ceiling and a reasoning level existed,
          that they lived on another page, or that this model refuses the
          value stored there. The row below is the same component
          /settings/llm renders, told which layer it is standing on. */}
      {activeTab === "models" && (
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.modelOverridesTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.modelOverridesDesc1")}{" "}
            (<Link className="underline" href="/settings/llm">{t("admin.reviewPolicies.detail.linkByok")}</Link>).{" "}
            {t("admin.reviewPolicies.detail.modelOverridesDesc2")}{" "}
            <Link className="underline" href="/settings/llm#review-agents">{t("admin.reviewPolicies.detail.linkLlmSetup")}</Link>.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {/* Why the ceiling is a control and not a constant, on the screen
              that can lower it below the number that failed the architect
              agent in 43% of runs. */}
          <Callout tone="info">
            {t("settings.llm.agents.budgetNote", { tokens: DEFAULT_AGENT_MAX_OUTPUT })}
          </Callout>
          {POLICY_AGENTS.map((agent, i) => (
            <AgentLLMRow
              key={agent}
              agent={agent}
              inheritsFrom="workspace"
              draft={agentDrafts[agent]}
              stored={policy.data?.agent_llm_overrides?.[agent] ?? null}
              effective={wsConfig.data?.agents?.[agent] ?? null}
              inheritedPending={!wsConfig.data}
              model={agentModels[i]}
              caps={agentCaps[i].caps}
              loading={agentCaps[i].loading}
              failed={agentCaps[i].failed}
              limit={agentMaxOutLimit(agentCaps[i].caps)}
              error={maxOutErrors[i]}
              disabled={!policy.data}
              onChange={(patch) => {
                setDirty(true);
                setAgentDrafts((prev) => ({
                  ...prev, [agent]: { ...prev[agent], ...patch },
                }));
              }}
            />
          ))}
          {/* Not a blocked Save: the value survives the press either way, and
              locking an operator out over a lookup that blipped costs more
              than it protects. This exists so nobody has to guess what a
              read-only reasoning box is about to do. */}
          {preservingReasoning.length > 0 && (
            <Callout tone="info">
              {t("settings.llm.agents.reasoningPreservedNote", {
                agents: preservingReasoning.join(", "),
              })}
            </Callout>
          )}
          <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
            {t("admin.reviewPolicies.detail.modelOverridesTip")}
          </p>
        </CardContent>
      </Card>
      )}

      {/* ─── MCP evidence sources ──────────────────────────────── */}
      {activeTab === "mcp" && (
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.mcpTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.mcpDesc1")}
            <code className="mx-1">trigger_patterns</code>
            {t("admin.reviewPolicies.detail.mcpDesc2")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {mcpSources.map((src, idx) => (
            <div key={idx} className="border border-[var(--color-border)] rounded p-3 space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <Label>{t("admin.reviewPolicies.detail.mcpNameLabel")}</Label>
                  <Input value={src.name} onChange={(e) => {
                    setDirty(true);
                    setMcpSources((s) => s.map((x, i) => i === idx ? { ...x, name: e.target.value } : x));
                  }} placeholder="sentry" />
                </div>
                <div>
                  <Label>{t("admin.reviewPolicies.detail.mcpUrlLabel")}</Label>
                  <Input value={src.url} onChange={(e) => {
                    setDirty(true);
                    setMcpSources((s) => s.map((x, i) => i === idx ? { ...x, url: e.target.value } : x));
                  }} placeholder="https://mcp.sentry.dev" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <Label>{t("admin.reviewPolicies.detail.mcpAuthTypeLabel")}</Label>
                  <Select
                    className="w-full h-11 sm:h-8 px-2"
                    value={src.auth_type}
                    onChange={(v) => {
                      setDirty(true);
                      setMcpSources((s) => s.map((x, i) => i === idx ? { ...x, auth_type: v } : x));
                    }}
                    options={[
                      { value: "none", label: t("admin.reviewPolicies.detail.mcpAuthNone") },
                      { value: "bearer", label: t("admin.reviewPolicies.detail.mcpAuthBearer") },
                      { value: "oauth", label: t("admin.reviewPolicies.detail.mcpAuthOauth") },
                    ]}
                  />
                </div>
                <div>
                  <Label>{t("admin.reviewPolicies.detail.mcpCredKeyLabel")}</Label>
                  <Input value={src.api_key_ref ?? ""} onChange={(e) => {
                    setDirty(true);
                    setMcpSources((s) => s.map((x, i) => i === idx ? { ...x, api_key_ref: e.target.value || null } : x));
                  }} placeholder={t("admin.reviewPolicies.detail.mcpCredKeyPlaceholder")} />
                </div>
              </div>
              <div>
                <Label>{t("admin.reviewPolicies.detail.mcpTriggerLabel")}</Label>
                <Input
                  value={src.trigger_patterns.join(", ")}
                  onChange={(e) => {
                    setDirty(true);
                    setMcpSources((s) => s.map((x, i) => i === idx ? {
                      ...x, trigger_patterns: e.target.value.split(",").map((v) => v.trim()).filter(Boolean),
                    } : x));
                  }}
                  placeholder="SENTRY-[A-Z0-9]+, api/users/.*"
                />
              </div>
              <div>
                <Label>{t("admin.reviewPolicies.detail.mcpAllowedToolsLabel")}</Label>
                <Input
                  value={src.allowed_tools.join(", ")}
                  onChange={(e) => {
                    setDirty(true);
                    setMcpSources((s) => s.map((x, i) => i === idx ? {
                      ...x, allowed_tools: e.target.value.split(",").map((v) => v.trim()).filter(Boolean),
                    } : x));
                  }}
                  placeholder="get_issue, list_issues"
                />
              </div>
              <div className="flex justify-end">
                <Button variant="ghost" onClick={() => {
                  setDirty(true);
                  setMcpSources((s) => s.filter((_, i) => i !== idx));
                }}>
                  <Trash2Icon className="h-3.5 w-3.5 mr-1" /> {t("admin.reviewPolicies.detail.remove")}
                </Button>
              </div>
            </div>
          ))}
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => {
              setDirty(true);
              setMcpSources((s) => [...s, {
                name: "", url: "", auth_type: "none", api_key_ref: null,
                allowed_tools: [], trigger_patterns: [],
              }]);
            }}>
              <PlusIcon className="h-4 w-4 mr-1" /> {t("admin.reviewPolicies.detail.mcpAddSource")}
            </Button>
            <Button variant="outline" onClick={() => {
              setDirty(true);
              setMcpSources((s) => [...s, {
                name: "sentry",
                url: "https://mcp.sentry.dev/sse",
                auth_type: "bearer",
                api_key_ref: "mcp:sentry",
                allowed_tools: ["get_issue", "list_issues", "search_issues"],
                trigger_patterns: ["SENTRY-[A-Z0-9]+"],
              }]);
            }}>
              {t("admin.reviewPolicies.detail.mcpSentryPreset")}
            </Button>
          </div>
        </CardContent>
      </Card>
      )}

      {/* ─── Which agents run at all ───────────────────────────── */}
      {activeTab === "agents" && (<>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.agentToggleTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.agentToggleDesc")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {TOGGLEABLE_AGENTS.map((agent) => {
            const on = !disabledAgents.includes(agent);
            return (
              <div
                key={agent}
                className="flex items-start justify-between gap-4 rounded-lg border border-[var(--color-border)] p-3"
              >
                <div className="min-w-0">
                  <Label htmlFor={`toggle-${agent}`} className="font-medium capitalize">
                    {agent}
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
                  id={`toggle-${agent}`}
                  checked={on}
                  onCheckedChange={(v) => {
                    setDirty(true);
                    setDisabledAgents((prev) =>
                      v ? prev.filter((a) => a !== agent) : [...prev, agent],
                    );
                  }}
                />
              </div>
            );
          })}
          <Callout tone="info">
            {t("admin.reviewPolicies.detail.agentToggleCostNote")}
          </Callout>
          <div className="flex items-start justify-between gap-4 rounded-lg border border-[var(--color-border)] p-3">
            <div className="min-w-0">
              <Label htmlFor="toggle-verifier" className="font-medium capitalize">
                verifier
                {!verifierEnabled && (
                  <Badge variant="destructive" className="ml-2 text-[9px]">
                    {t("admin.reviewPolicies.detail.agentOffBadge")}
                  </Badge>
                )}
              </Label>
              <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.verifierOptIn")}
              </p>
            </div>
            <Switch
              id="toggle-verifier"
              checked={verifierEnabled}
              onCheckedChange={(v) => {
                setDirty(true);
                setVerifierEnabled(v);
              }}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.agentPromptsTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.detail.agentPromptsDesc1")}{" "}
            <Link className="underline" href="/admin/agents">{t("admin.reviewPolicies.detail.linkAiAgents")}</Link>
            {" "}{t("admin.reviewPolicies.detail.agentPromptsDesc2")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {(["defect", "contract", "security"] as const).map((agent) => (
            <div key={agent} className="space-y-1">
              <div className="flex items-center justify-between">
                <Label htmlFor={`prompt-${agent}`} className="font-medium capitalize">
                  {agent}
                  {promptOverrides[agent].trim() && (
                    <Badge variant="brand" className="ml-2 text-[9px]">
                      {t("admin.reviewPolicies.detail.overrideActive")}
                    </Badge>
                  )}
                  {disabledAgents.includes(agent) && (
                    <Badge variant="destructive" className="ml-2 text-[9px]">
                      {t("admin.reviewPolicies.detail.agentOffBadge")}
                    </Badge>
                  )}
                </Label>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setPreviewAgent(agent)}
                >
                  <EyeIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.reviewPolicies.detail.preview")}
                </Button>
              </div>
              <Textarea
                id={`prompt-${agent}`}
                rows={4}
                placeholder={t("admin.reviewPolicies.detail.inheritPlaceholder")}
                value={promptOverrides[agent]}
                onChange={(e) => {
                  setDirty(true);
                  setPromptOverrides((prev) => ({ ...prev, [agent]: e.target.value }));
                }}
              />
            </div>
          ))}
        </CardContent>
      </Card>
      </>)}

      {previewAgent && (
        <PromptPreviewDrawer
          slug={slug}
          agent={previewAgent}
          onClose={() => setPreviewAgent(null)}
        />
      )}

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("admin.reviewPolicies.helpTitle")}</DialogTitle>
            <DialogDescription>{t("admin.reviewPolicies.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            {([
              ["helpEnabledTitle", "helpEnabledBody"],
              ["helpBranchesTitle", "helpBranchesBody"],
              ["helpPromptTitle", "helpPromptBody"],
              ["helpFolderRulesTitle", "helpFolderRulesBody"],
              ["helpModelsTitle", "helpModelsBody"],
              ["helpAgentPromptsTitle", "helpAgentPromptsBody"],
              ["helpAgentToggleTitle", "helpAgentToggleBody"],
              ["helpMcpTitle", "helpMcpBody"],
            ] as const).map(([titleKey, bodyKey]) => (
              <div key={titleKey}>
                <div className="mb-1 font-medium">
                  {t(`admin.reviewPolicies.${titleKey}`)}
                </div>
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t(`admin.reviewPolicies.${bodyKey}`)}
                </p>
              </div>
            ))}
            <div>
              <div className="mb-1 font-medium">
                {t("admin.reviewPolicies.helpAgentRolesTitle")}
              </div>
              <ul className="space-y-1 text-xs text-[var(--color-muted-foreground)]">
                {ALL_AGENTS.map((agent) => (
                  <li key={agent}>
                    <span className="font-medium capitalize text-[var(--color-foreground)]">
                      {agent}
                    </span>
                    {" — "}
                    {t(`admin.reviewPolicies.agentRole.${agent}`)}
                  </li>
                ))}
              </ul>
            </div>
            <Callout tone="info">{t("admin.reviewPolicies.helpNoPolicy")}</Callout>
          </div>
        </DialogContent>
      </Dialog>

      <div className="flex items-center justify-between sticky bottom-0 bg-[var(--color-background)] border-t border-[var(--color-border)] py-3">
        <Button
          variant="ghost"
          onClick={async () => {
            const ok = await confirm({
              title: t("admin.reviewPolicies.detail.resetConfirm"),
              danger: true,
            });
            if (ok) reset.mutate();
          }}
          disabled={reset.isPending}
        >
          <RotateCcwIcon className="h-4 w-4 mr-1" /> {t("admin.reviewPolicies.detail.resetButton")}
        </Button>
        <div className="flex items-center gap-2">
          {dirty && !agentLLMBlocked && (
            <span className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.unsavedChanges")}
            </span>
          )}
          {/* A ceiling above what the model accepts is a 422 the operator
              would meet after the press, on whichever tab they happen to be
              on. Refused here instead — and said out loud, because a Save
              button that is simply dead is indistinguishable from a broken
              page. */}
          {agentLLMBlocked && (
            <span className="text-xs text-red-600 dark:text-red-400">
              {t("settings.llm.agents.saveBlocked")}
            </span>
          )}
          <Button
            onClick={() => save.mutate()}
            disabled={save.isPending || !dirty || agentLLMBlocked}
          >
            <SaveIcon className="h-4 w-4 mr-1" />
            {save.isPending
              ? t("admin.reviewPolicies.detail.saving")
              : t("admin.reviewPolicies.detail.save")}
          </Button>
        </div>
      </div>
      {dialog}
    </PageShell>
  );
}


function PromptPreviewDrawer({
  slug, agent, onClose,
}: {
  slug: string;
  agent: string;
  onClose: () => void;
}) {
  const token = useToken();
  const t = useT();
  const preview = useQuery({
    queryKey: ["review-policies", "preview", slug, agent],
    queryFn: () => reviewPoliciesApi.promptPreview(token!, slug, agent),
    enabled: !!token,
  });

  return (
    <div
      className="fixed inset-0 z-30 bg-black/40 flex justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl h-full overflow-y-auto bg-[var(--color-background)] border-l border-[var(--color-border)] p-6 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold capitalize">
              {agent} {t("admin.reviewPolicies.detail.effectivePromptSuffix")}
            </h2>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.previewHint")}
            </p>
          </div>
          <Button variant="ghost" onClick={onClose}>
            {t("admin.reviewPolicies.detail.close")}
          </Button>
        </div>

        {preview.isLoading && (
          <div className="text-sm text-[var(--color-muted-foreground)]">
            {t("admin.reviewPolicies.detail.loading")}
          </div>
        )}
        {preview.error && (
          <div className="text-sm text-red-600">
            {t("admin.reviewPolicies.detail.previewFailed", {
              message: (preview.error as Error).message,
            })}
          </div>
        )}
        {preview.data && (
          <>
            <div>
              <h3 className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)] mb-1">
                system_instruction
              </h3>
              <pre className="text-xs bg-[var(--color-muted)] p-3 rounded whitespace-pre-wrap overflow-x-auto">
                {preview.data.system_prompt}
              </pre>
            </div>
            <div>
              <h3 className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)] mb-1">
                user_prompt_template
              </h3>
              <pre className="text-xs bg-[var(--color-muted)] p-3 rounded whitespace-pre-wrap overflow-x-auto">
                {preview.data.user_prompt_template}
              </pre>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
