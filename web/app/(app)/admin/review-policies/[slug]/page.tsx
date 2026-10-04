"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CopyIcon,
  EyeIcon,
  HelpCircleIcon,
  PlusIcon,
  RotateCcwIcon,
  SaveIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";

import {
  agentsApi,
  llmApi,
  reviewPoliciesApi,
  type AgentLLMOverride,
  type FolderRule,
  type ModelCapabilities,
  type ReviewPolicy,
  type RuleSeverityHint,
  type SettingSource,
} from "@/lib/api";
import { globError, globLines } from "@/lib/ignore-globs";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { useCanEditPrompts } from "@/lib/use-analytics-access";
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
import { BranchCombobox, toBranchResult } from "@/components/branch-combobox";

/** Local tabs splitting the long form into digestible groups. The active one
 *  is mirrored to `?tab=` so other pages (AI Agents) can link straight to a
 *  section. */
type PolicyTab =
  | "general" | "agents" | "rules" | "comments" | "ignore" | "models" | "mcp";
const POLICY_TABS: PolicyTab[] = [
  "general", "agents", "rules", "comments", "ignore", "models", "mcp",
];

/** Severity a custom rule may ask violations to be reported at. Mirrors
 *  `SEVERITY_HINTS` in src/review/policy_rules.py; "" = the agent decides. */
const RULE_SEVERITY_HINTS: Array<RuleSeverityHint | ""> = [
  "", "info", "warning", "error", "critical",
];

/** Server bounds, repeated so a value is refused at the keyboard. */
const MAX_RULES = 20;
const MAX_INLINE_MIN = 1;
const MAX_INLINE_MAX = 100;
const SUMMARY_INSTRUCTIONS_MAX = 4000;

/** The tab `?tab=` names, or null. */
function tabFromUrl(): PolicyTab | null {
  const wanted = new URLSearchParams(window.location.search).get("tab");
  return wanted && (POLICY_TABS as string[]).includes(wanted) ? (wanted as PolicyTab) : null;
}
const noSubscribe = () => () => {};

/** "" or an integer in [1, 100] — the only inline caps the API accepts. */
function maxInlineError(text: string): boolean {
  const v = text.trim();
  if (!v) return false;
  if (!/^\d+$/.test(v)) return true;
  const n = Number(v);
  return n < MAX_INLINE_MIN || n > MAX_INLINE_MAX;
}

/** A language code's own name ("uk" → "українська"), or the code itself
 *  where the browser has no name for it. */
function languageName(code: string): string {
  try {
    const name = new Intl.DisplayNames([code], { type: "language" }).of(code);
    return name && name !== code ? `${name} (${code})` : code;
  } catch {
    return code;
  }
}

/** "overridden here" vs "inherited" — the one badge every field uses.
 *  `from` names the layer an inherited value comes from: the workspace review
 *  defaults (/admin/review-defaults) or the install default. */
function OriginBadge({ overridden, inheritedLabel, from }: {
  overridden: boolean;
  inheritedLabel?: string;
  from?: SettingSource;
}) {
  const t = useT();
  return overridden ? (
    <Badge variant="brand" className="ml-2 text-[9px]">
      {t("admin.reviewPolicies.detail.badgeOverridden")}
    </Badge>
  ) : (
    <Badge variant="outline" className="ml-2 text-[9px] font-normal">
      {inheritedLabel
        ?? (from === "install"
          ? t("admin.reviewPolicies.detail.badgeInstallDefault")
          : t("admin.reviewPolicies.detail.badgeInherited"))}
    </Badge>
  );
}

/** A small "reset to inherited" button, shown only while overriding. */
function ResetToInherited({ onClick, disabled }: {
  onClick: () => void;
  disabled?: boolean;
}) {
  const t = useT();
  return (
    <Button type="button" variant="ghost" size="sm" onClick={onClick} disabled={disabled}>
      <RotateCcwIcon className="h-3.5 w-3.5 mr-1" />
      {t("admin.reviewPolicies.detail.resetToInherited")}
    </Button>
  );
}

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

/** The comment thresholds, most permissive first, as the select lists them.
 *  "" is "inherit", which posts every finding. Mirrors
 *  `COMMENT_SEVERITY_LEVELS` in src/api/routers/review_policies.py. */
const COMMENT_THRESHOLDS = ["", "warning", "error", "critical"] as const;

export default function ReviewPolicyEditPage() {
  const params = useParams<{ slug: string }>();
  const router = useRouter();
  const slug = decodeURIComponent(params.slug);
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  // undefined while the membership loads: drawn disabled until it is known.
  const canEdit = useCanEditPrompts() === true;
  const { confirm, dialog } = useConfirm();

  const policy = useQuery({
    queryKey: ["review-policies", "detail", slug],
    queryFn: () => reviewPoliciesApi.get(token!, slug),
    enabled: !!token,
  });

  // Only the default branch and where the list comes from: the names
  // themselves are searched on demand by the picker below.
  const branches = useQuery({
    queryKey: ["review-policies", "branches-meta", slug],
    queryFn: () => reviewPoliciesApi.branches(token!, slug, "", 1),
    enabled: !!token,
  });

  const [enabled, setEnabled] = useState(true);
  const [department, setDepartment] = useState("");
  const [promptTemplate, setPromptTemplate] = useState("");
  /** null inherits the workspace review defaults; [] = every branch. */
  const [targetBranches, setTargetBranches] = useState<string[] | null>(null);
  const [folderRules, setFolderRules] = useState<FolderRule[]>([]);
  /** Model, output ceiling and reasoning per agent, as one draft each — the
   *  same three-field shape /settings/llm edits, because it is the same three
   *  settings and this layer simply outranks that one. */
  const [agentDrafts, setAgentDrafts] = useState<Record<PolicyAgent, AgentDraft>>(
    () => emptyAgentDrafts(),
  );
  /** Per-agent system prompts for this repo, keyed by every agent the
   *  server says may carry one (`overridable_agents`). */
  const [promptOverrides, setPromptOverrides] = useState<Record<string, string>>({});
  const [previewAgent, setPreviewAgent] = useState<string | null>(null);
  /** null inherits the workspace review defaults; a list is this repo's own
   *  answer ([] = every agent runs). */
  const [disabledAgents, setDisabledAgents] = useState<string[] | null>(null);
  // The veto is a stage, not an agent, and it is OFF unless this repo
  // asks. Its own boolean rather than an entry in the agent deny-list:
  // squeezing a stage into that list is what made the default
  // un-invertible on the server.
  // Three states: null inherits the install default, true/false decides.
  const [verifierChoice, setVerifierChoice] = useState<boolean | null>(null);
  /** null inherits the workspace's globs; a string ("" included) is this
   *  repo's own list. */
  const [ignoreGlobsText, setIgnoreGlobsText] = useState<string | null>(null);
  const [commentMinSeverity, setCommentMinSeverity] = useState<string>("");
  /** null inherits the code default list; a list (even []) replaces it. */
  const [suppressedRules, setSuppressedRules] = useState<string[] | null>(null);
  const [suppressedDraft, setSuppressedDraft] = useState("");
  const [maxInlineText, setMaxInlineText] = useState("");
  /** null inherits the workspace default (then on). */
  const [summaryEnabled, setSummaryEnabled] = useState<boolean | null>(null);
  const [summaryInstructions, setSummaryInstructions] = useState("");
  const [startedCommentEnabled, setStartedCommentEnabled] = useState<boolean | null>(null);
  /** "" inherits the workspace language. */
  const [reviewLanguage, setReviewLanguage] = useState("");
  const [mcpSources, setMcpSources] = useState<Array<{
    name: string; url: string; auth_type: string;
    api_key_ref: string | null;
    allowed_tools: string[]; trigger_patterns: string[];
  }>>([]);
  const [dirty, setDirty] = useState(false);
  // `?tab=agents` opens that section. Read through useSyncExternalStore
  // rather than useSearchParams (which would force a Suspense boundary for one
  // string): the server snapshot is null, so hydration matches, and the
  // client's first render already lands on the linked tab.
  const urlTab = useSyncExternalStore(noSubscribe, tabFromUrl, () => null);
  const [chosenTab, setChosenTab] = useState<PolicyTab | null>(null);
  const activeTab: PolicyTab = chosenTab ?? urlTab ?? "general";
  const setActiveTab = (tab: PolicyTab) => {
    setChosenTab(tab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    window.history.replaceState(window.history.state, "", url.toString());
  };
  const [helpOpen, setHelpOpen] = useState(false);

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
    setTargetBranches(
      policy.data.target_branches == null ? null : [...policy.data.target_branches],
    );
    setFolderRules(policy.data.folder_rules);
    setAgentDrafts(agentDraftsFrom(policy.data));
    const po = policy.data.agent_prompt_overrides ?? {};
    setPromptOverrides(Object.fromEntries(
      (policy.data.overridable_agents ?? Object.keys(po)).map(
        (agent) => [agent, po[agent] ?? ""],
      ),
    ));
    setMcpSources(policy.data.mcp_sources ?? []);
    setDisabledAgents(
      policy.data.disabled_agents == null ? null : [...policy.data.disabled_agents],
    );
    // `verifier_enabled` is what THIS repo said; `_effective` is what a
    // review would do, deny-list and install default folded in. A switch
    // has to show the second — it is the answer the reader is checking.
    setVerifierChoice(policy.data.verifier_enabled ?? null);
    setIgnoreGlobsText(
      policy.data.ignore_globs == null ? null : policy.data.ignore_globs.join("\n"),
    );
    setCommentMinSeverity(policy.data.comment_min_severity ?? "");
    setSuppressedRules(
      policy.data.suppressed_rules == null ? null : [...policy.data.suppressed_rules],
    );
    setMaxInlineText(
      policy.data.max_inline_comments == null ? "" : String(policy.data.max_inline_comments),
    );
    setSummaryEnabled(policy.data.summary_enabled ?? null);
    setSummaryInstructions(policy.data.summary_instructions ?? "");
    setStartedCommentEnabled(policy.data.started_comment_enabled ?? null);
    setReviewLanguage(policy.data.review_language ?? "");
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

  // The workspace prompts (/admin/agents) — what an empty prompt box here
  // inherits, and the text "start from inherited" copies in.
  const wsAgents = useQuery({
    queryKey: ["agents"],
    queryFn: () => agentsApi.list(token!),
    enabled: !!token,
  });
  const wsAgentByName = Object.fromEntries(
    (wsAgents.data ?? []).map((a) => [a.name, a]),
  );
  const promptAgents = policy.data?.overridable_agents
    ?? Object.keys(promptOverrides);
  /** What each field resolves to when this repo says nothing, and from where
   *  — the workspace review defaults or the install. */
  const inherited = policy.data?.inherited ?? {};
  const inheritedFrom = (field: string): SettingSource =>
    policy.data?.inherited_sources?.[field] ?? "install";
  const inheritedList = (field: string): string[] =>
    Array.isArray(inherited[field]) ? (inherited[field] as string[]) : [];
  const inheritedBool = (field: string, fallback: boolean): boolean =>
    typeof inherited[field] === "boolean" ? (inherited[field] as boolean) : fallback;
  const branchesShown = targetBranches ?? inheritedList("target_branches");
  const disabledShown = disabledAgents ?? inheritedList("disabled_agents");
  const summaryOn = summaryEnabled ?? inheritedBool("summary_enabled", true);
  const startedOn = startedCommentEnabled ?? inheritedBool("started_comment_enabled", true);
  const ruleTargets = policy.data?.rule_target_agents ?? [];
  /** What the verifier does when this repo says nothing: the deny-list's old
   *  spelling of off still wins, then the install default. */
  const verifierInherited = disabledShown.includes("verifier")
    ? false
    : policy.data?.verifier_enabled_default ?? false;
  const verifierOn = verifierChoice ?? verifierInherited;

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
  const ignoreGlobs = globLines(ignoreGlobsText ?? "");
  const ignoreGlobsError = globError(ignoreGlobs);
  const maxInlineBad = maxInlineError(maxInlineText);
  const saveBlocked = agentLLMBlocked || ignoreGlobsError !== null || maxInlineBad;

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
        // null inherits the workspace review defaults.
        target_branches: targetBranches,
        // Optional fields only when they say something: a rule using none of
        // them is saved exactly as `{pattern, prompt}`, as it always was.
        folder_rules: folderRules.map((r) => ({
          pattern: r.pattern,
          prompt: r.prompt,
          ...(r.title?.trim() ? { title: r.title.trim() } : {}),
          ...(r.severity_hint ? { severity_hint: r.severity_hint } : {}),
          ...(r.agents && r.agents.length ? { agents: r.agents } : {}),
        })),
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
        disabled_agents: disabledAgents === null
          ? null
          : verifierChoice === true
            ? disabledAgents.filter((a) => a !== "verifier")
            : disabledAgents,
        verifier_enabled: verifierChoice,
        // Only once the policy has loaded, for the reason the overrides above
        // wait: an unloaded form would send "no globs" and "inherit" over
        // whatever is stored. Omitted keys keep the stored values.
        ...(policy.data
          ? {
              ignore_globs: ignoreGlobsText === null ? null : ignoreGlobs,
              comment_min_severity:
                (commentMinSeverity || null) as ReviewPolicy["comment_min_severity"],
              suppressed_rules: suppressedRules,
              summary_enabled: summaryEnabled,
              summary_instructions: summaryInstructions.trim() || null,
              started_comment_enabled: startedCommentEnabled,
              review_language: reviewLanguage || null,
              max_inline_comments: maxInlineText.trim()
                ? Number(maxInlineText.trim())
                : null,
            }
          : {}),
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
    setTargetBranches((prev) => {
      const cur = prev ?? inheritedList("target_branches");
      return cur.includes(b) ? cur.filter((x) => x !== b) : [...cur, b];
    });
  };

  /** A pick from the branch picker. A listed branch toggles; typed text
   *  ("main, develop" / "main develop") appends every name in it — target
   *  branches stay typeable by hand, for a branch that does not exist yet or
   *  a repository whose branches cannot be listed. Matching on the backend
   *  is exact, so names are kept verbatim. */
  const pickBranch = (raw: string) => {
    const parsed = raw.split(/[\s,]+/).map((b) => b.trim()).filter(Boolean);
    if (parsed.length === 0) return;
    if (parsed.length === 1) {
      toggleBranch(parsed[0]);
      return;
    }
    setTargetBranches((prev) => {
      const next = [...(prev ?? inheritedList("target_branches"))];
      for (const b of parsed) if (!next.includes(b)) next.push(b);
      return next;
    });
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
    setFolderRules((prev) => (
      prev.length >= MAX_RULES
        ? prev
        : [...prev, { pattern: "", prompt: "", title: "", severity_hint: null, agents: [] }]
    ));
  };

  const toggleRuleAgent = (idx: number, agent: string) => {
    setDirty(true);
    setFolderRules((prev) => prev.map((r, i) => {
      if (i !== idx) return r;
      const cur = r.agents ?? [];
      return {
        ...r,
        agents: cur.includes(agent) ? cur.filter((a) => a !== agent) : [...cur, agent],
      };
    }));
  };

  /** "a.b, c.d" or one per line → appended to the repo's own list. A token
   *  with whitespace inside cannot be a rule id and never gets this far. */
  const addSuppressed = () => {
    const parsed = suppressedDraft.split(/[\s,]+/).map((r) => r.trim()).filter(Boolean);
    if (!parsed.length) return;
    setSuppressedRules((prev) => {
      const next = [...(prev ?? policy.data?.suppressed_rules_effective ?? [])];
      for (const r of parsed) if (!next.includes(r)) next.push(r);
      return next;
    });
    setSuppressedDraft("");
    setDirty(true);
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
            {t("admin.reviewPolicies.detail.subtitle")}{" "}
            {t("admin.reviewPolicies.detail.inheritsWorkspaceDefaultsNote")}{" "}
            <Link className="underline" href="/admin/review-defaults">
              {t("admin.reviewPolicies.detail.workspaceDefaultsLink")}
            </Link>
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
          <div className="flex items-start justify-between gap-2">
            <CardTitle className="flex items-center gap-1.5">
              {t("admin.reviewPolicies.detail.branchesTitle")}
              <Tooltip label={t("admin.reviewPolicies.detail.branchesTooltip")}>
                <HelpCircleIcon className="h-3.5 w-3.5 text-[var(--color-muted-foreground)]" />
              </Tooltip>
              <OriginBadge
                overridden={targetBranches !== null}
                from={inheritedFrom("target_branches")}
              />
            </CardTitle>
            {targetBranches !== null && (
              <ResetToInherited onClick={() => { setTargetBranches(null); setDirty(true); }} />
            )}
          </div>
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
          <Callout tone={branchesShown.length === 0 ? "info" : "success"}>
            {branchesShown.length === 0
              ? t("admin.reviewPolicies.detail.branchesSemanticsAll")
              : t("admin.reviewPolicies.detail.branchesSemanticsFiltered", {
                  branches: branchesShown.join(", "),
                })}
          </Callout>
          {targetBranches === null && (
            <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
              {t("admin.reviewPolicies.detail.inheritsWorkspaceDefaultsNote")}{" "}
              <Link className="underline" href="/admin/review-defaults">
                {t("admin.reviewPolicies.detail.workspaceDefaultsLink")}
              </Link>
            </p>
          )}
          <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
            {t("admin.reviewPolicies.detail.branchesExactMatchNote")}
          </p>
          {branches.isLoading && (
            <p className="text-sm">
              {t("admin.reviewPolicies.detail.branchesLoading")}
            </p>
          )}
          {branches.data?.source === "none" && (
            <p className="text-sm text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.branchesNotClonedBefore")}{" "}
              <code className="px-1 rounded bg-[var(--color-muted)]">
                analyzer sync {slug}
              </code>{" "}
              {t("admin.reviewPolicies.detail.branchesNotClonedAfter")}
            </p>
          )}

          {/* Searchable over the repository's whole branch list (the server
              walks every provider page). It used to be a wall of chips built
              from one page — or from the single-branch clone. */}
          <div className="mt-4 space-y-1">
            <Label htmlFor="branch-add">
              {t("admin.reviewPolicies.detail.branchesPickLabel")}
            </Label>
            <BranchCombobox
              id="branch-add"
              value=""
              onChange={pickBranch}
              search={(q) => reviewPoliciesApi.branches(token!, slug, q).then(toBranchResult)}
              queryKey={["review-policies", "branches", slug]}
              selected={branchesShown}
              keepOpenOnSelect
              allowCustom
              disabled={!token}
              placeholder={t("admin.reviewPolicies.detail.branchesAddPlaceholder")}
              className="w-full sm:max-w-md"
            />
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.branchesPickHint")}
            </p>
          </div>

          {branchesShown.length > 0 && (
            <div className="mt-3">
              <p className="text-xs text-[var(--color-muted-foreground)] mb-1">
                {t("admin.reviewPolicies.detail.branchesSelectedLabel")}
              </p>
              <div className="flex flex-wrap gap-2">
                {branchesShown.map((b) => (
                  <Badge key={b} variant="outline" className="font-mono">
                    {b}
                    {branches.data?.default_branch === b && " ★"}
                    <button
                      type="button"
                      onClick={() => toggleBranch(b)}
                      aria-label={t("common.remove")}
                      className="ml-1 text-[10px] opacity-70 hover:opacity-100"
                    >
                      ✕
                    </button>
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      </>)}

      {/* ─── Comments & summary ─────────────────────────────────── */}
      {activeTab === "comments" && (<>
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.detail.commentsTitle")}</CardTitle>
          <CardDescription>{t("admin.reviewPolicies.detail.commentsDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-1">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="comment-min-severity">
                {t("review.settings.thresholdLabel")}
                <OriginBadge
                  overridden={!!commentMinSeverity}
                  from={inheritedFrom("comment_min_severity")}
                />
              </Label>
              {commentMinSeverity && (
                <ResetToInherited onClick={() => { setCommentMinSeverity(""); setDirty(true); }} />
              )}
            </div>
            <Select
              id="comment-min-severity"
              className="w-full sm:w-80"
              value={commentMinSeverity}
              onChange={(v) => {
                setCommentMinSeverity(v);
                setDirty(true);
              }}
              options={COMMENT_THRESHOLDS.map((level) => ({
                value: level,
                label: level
                  ? t(`review.settings.threshold.${level}`)
                  : t("admin.reviewPolicies.detail.inheritOption", {
                      value: t(`review.settings.threshold.${
                        inherited.comment_min_severity && inherited.comment_min_severity !== "info"
                          ? String(inherited.comment_min_severity)
                          : "all"}`),
                    }),
              }))}
            />
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("review.settings.thresholdHint")}
            </p>
          </div>

          <div className="space-y-1">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="max-inline">
                {t("admin.reviewPolicies.detail.maxInlineLabel")}
                <OriginBadge
                  overridden={!!maxInlineText.trim()}
                  from={inheritedFrom("max_inline_comments")}
                />
              </Label>
              {maxInlineText.trim() && (
                <ResetToInherited onClick={() => { setMaxInlineText(""); setDirty(true); }} />
              )}
            </div>
            <Input
              id="max-inline"
              type="number"
              inputMode="numeric"
              min={MAX_INLINE_MIN}
              max={MAX_INLINE_MAX}
              className="w-full sm:w-40"
              placeholder={String(inherited.max_inline_comments ?? "")}
              value={maxInlineText}
              aria-invalid={maxInlineBad ? true : undefined}
              aria-describedby="max-inline-hint"
              onChange={(e) => {
                setMaxInlineText(e.target.value);
                setDirty(true);
              }}
            />
            {maxInlineBad ? (
              <p id="max-inline-hint" role="alert" className="text-xs text-red-600 dark:text-red-400">
                {t("admin.reviewPolicies.detail.maxInlineInvalid")}
              </p>
            ) : (
              <p id="max-inline-hint" className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.maxInlineHint")}
              </p>
            )}
          </div>

          <div className="space-y-1">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="review-language">
                {t("admin.reviewPolicies.detail.languageLabel")}
                <OriginBadge
                  overridden={!!reviewLanguage}
                  from={inheritedFrom("review_language")}
                />
              </Label>
              {reviewLanguage && (
                <ResetToInherited onClick={() => { setReviewLanguage(""); setDirty(true); }} />
              )}
            </div>
            <Select
              id="review-language"
              className="w-full sm:w-80"
              value={reviewLanguage}
              onChange={(v) => {
                setReviewLanguage(v);
                setDirty(true);
              }}
              options={[
                {
                  value: "",
                  label: t("admin.reviewPolicies.detail.languageInherit", {
                    // The workspace's language — what "" resolves to.
                    lang: languageName(
                      wsConfig.data?.review_language
                      ?? (policy.data?.review_language ? "en" : policy.data?.review_language_effective)
                      ?? "en",
                    ),
                  }),
                },
                ...(policy.data?.review_languages ?? []).map((code) => ({
                  value: code, label: languageName(code),
                })),
              ]}
            />
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.languageHint")}
            </p>
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
              <Label htmlFor="summary-enabled" className="font-medium">
                {t("admin.reviewPolicies.detail.summaryEnabledLabel")}
                <OriginBadge
                  overridden={summaryEnabled !== null}
                  from={inheritedFrom("summary_enabled")}
                />
              </Label>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.summaryEnabledHint")}
              </p>
              {summaryEnabled !== null && (
                <ResetToInherited onClick={() => { setSummaryEnabled(null); setDirty(true); }} />
              )}
            </div>
            <Switch
              id="summary-enabled"
              checked={summaryOn}
              onCheckedChange={(v) => { setSummaryEnabled(v); setDirty(true); }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="summary-instructions">
              {t("admin.reviewPolicies.detail.summaryInstructionsLabel")}
              <OriginBadge
                overridden={!!summaryInstructions.trim()}
                from={inheritedFrom("summary_instructions")}
              />
            </Label>
            <Textarea
              id="summary-instructions"
              rows={4}
              maxLength={SUMMARY_INSTRUCTIONS_MAX}
              disabled={!summaryOn}
              placeholder={
                typeof inherited.summary_instructions === "string" && inherited.summary_instructions
                  ? inherited.summary_instructions
                  : t("admin.reviewPolicies.detail.summaryInstructionsPlaceholder")
              }
              value={summaryInstructions}
              onChange={(e) => { setSummaryInstructions(e.target.value); setDirty(true); }}
            />
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.summaryInstructionsHint", {
                count: summaryInstructions.length, max: SUMMARY_INSTRUCTIONS_MAX,
              })}
            </p>
          </div>
          <div className="flex items-start justify-between gap-4 border-t border-[var(--color-border)] pt-4">
            <div>
              <Label htmlFor="started-comment" className="font-medium">
                {t("admin.reviewPolicies.detail.startedCommentLabel")}
                <OriginBadge
                  overridden={startedCommentEnabled !== null}
                  from={inheritedFrom("started_comment_enabled")}
                />
              </Label>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.startedCommentHint")}
              </p>
              {startedCommentEnabled !== null && (
                <ResetToInherited
                  onClick={() => { setStartedCommentEnabled(null); setDirty(true); }}
                />
              )}
            </div>
            <Switch
              id="started-comment"
              checked={startedOn}
              onCheckedChange={(v) => { setStartedCommentEnabled(v); setDirty(true); }}
            />
          </div>
        </CardContent>
      </Card>
      </>)}

      {/* ─── Ignore paths ───────────────────────────────────────── */}
      {activeTab === "ignore" && (
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-2">
            <CardTitle>
              {t("admin.reviewPolicies.detail.ignoreTitle")}
              <OriginBadge
                overridden={ignoreGlobsText !== null}
                from={inheritedFrom("ignore_globs")}
              />
            </CardTitle>
            {ignoreGlobsText !== null && (
              <ResetToInherited onClick={() => { setIgnoreGlobsText(null); setDirty(true); }} />
            )}
          </div>
          <CardDescription>{t("admin.reviewPolicies.detail.ignoreDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-1">
          <Label htmlFor="ignore-globs">{t("review.settings.globsLabel")}</Label>
          <Textarea
            id="ignore-globs"
            rows={8}
            spellCheck={false}
            className="font-mono text-xs"
            placeholder={
              ignoreGlobsText === null && inheritedList("ignore_globs").length > 0
                ? inheritedList("ignore_globs").join("\n")
                : "docs/**\n*.snap\nmigrations/*.py"
            }
            value={ignoreGlobsText ?? ""}
            aria-invalid={ignoreGlobsError ? true : undefined}
            aria-describedby="ignore-globs-hint"
            onChange={(e) => {
              setIgnoreGlobsText(e.target.value);
              setDirty(true);
            }}
          />
          {ignoreGlobsError ? (
            <p id="ignore-globs-hint" role="alert" className="text-xs text-red-600 dark:text-red-400">
              {t(ignoreGlobsError.key, { line: ignoreGlobsError.line })}
            </p>
          ) : (
            <p id="ignore-globs-hint" className="text-xs text-[var(--color-muted-foreground)]">
              {t("review.settings.globsHint")}
            </p>
          )}
        </CardContent>
      </Card>
      )}

      {activeTab === "rules" && (<>
      <Callout tone="info">
        <p>{t("admin.reviewPolicies.detail.rulesLibraryHint")}</p>
        <Link
          className="mt-1 inline-flex items-center gap-1 font-medium underline"
          href={`/admin/review-rules?repo=${encodeURIComponent(slug)}`}
        >
          {t("admin.reviewPolicies.detail.rulesLibraryLink")}
          <ArrowRightIcon className="h-3.5 w-3.5" />
        </Link>
      </Callout>

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
          {folderRules.map((fr, idx) => {
            const targets = fr.agents ?? [];
            return (
              <div
                key={idx}
                className="rounded-lg border border-[var(--color-border)] p-3 space-y-3"
              >
                <div className="flex items-start gap-2">
                  <div className="grid flex-1 gap-2 sm:grid-cols-2">
                    <div className="space-y-1">
                      <Label htmlFor={`rule-title-${idx}`} className="text-xs">
                        {t("admin.reviewPolicies.detail.ruleTitleLabel")}
                      </Label>
                      <Input
                        id={`rule-title-${idx}`}
                        maxLength={200}
                        placeholder={t("admin.reviewPolicies.detail.ruleTitlePlaceholder")}
                        value={fr.title ?? ""}
                        onChange={(e) => updateFolderRule(idx, { title: e.target.value })}
                      />
                    </div>
                    <div className="space-y-1">
                      <Label htmlFor={`rule-pattern-${idx}`} className="text-xs">
                        {t("admin.reviewPolicies.detail.rulePatternLabel")}
                      </Label>
                      <Input
                        id={`rule-pattern-${idx}`}
                        className="font-mono text-xs"
                        placeholder="src/api/**/*.py"
                        value={fr.pattern}
                        onChange={(e) =>
                          updateFolderRule(idx, { pattern: e.target.value })
                        }
                      />
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => removeFolderRule(idx)}
                    title={t("admin.reviewPolicies.detail.removeRuleTitle")}
                    aria-label={t("admin.reviewPolicies.detail.removeRuleTitle")}
                  >
                    <Trash2Icon className="h-4 w-4" />
                  </Button>
                </div>
                <div className="grid gap-3 sm:grid-cols-[14rem_1fr]">
                  <div className="space-y-1">
                    <Label htmlFor={`rule-severity-${idx}`} className="text-xs">
                      {t("admin.reviewPolicies.detail.ruleSeverityLabel")}
                    </Label>
                    <Select
                      id={`rule-severity-${idx}`}
                      className="w-full"
                      value={fr.severity_hint ?? ""}
                      onChange={(v) => updateFolderRule(idx, {
                        severity_hint: (v || null) as RuleSeverityHint | null,
                      })}
                      options={RULE_SEVERITY_HINTS.map((level) => ({
                        value: level,
                        label: t(`admin.reviewPolicies.detail.ruleSeverity.${level || "none"}`),
                      }))}
                    />
                  </div>
                  <fieldset className="space-y-1">
                    <legend className="text-xs font-medium">
                      {t("admin.reviewPolicies.detail.ruleAgentsLabel")}
                    </legend>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {ruleTargets.map((agent) => {
                        const on = targets.includes(agent);
                        return (
                          <button
                            key={agent}
                            type="button"
                            aria-pressed={on}
                            onClick={() => toggleRuleAgent(idx, agent)}
                            className={`text-xs rounded border px-2 py-1 capitalize transition-colors ${
                              on
                                ? "bg-[var(--color-primary)] text-[var(--color-primary-foreground)] border-transparent"
                                : "border-[var(--color-border)] hover:bg-[var(--color-accent)]"
                            }`}
                          >
                            {agent}
                          </button>
                        );
                      })}
                      <span className="text-xs text-[var(--color-muted-foreground)]">
                        {targets.length === 0
                          ? t("admin.reviewPolicies.detail.ruleAgentsAll")
                          : t("admin.reviewPolicies.detail.ruleAgentsSome")}
                      </span>
                    </div>
                  </fieldset>
                </div>
                <Textarea
                  rows={3}
                  aria-label={t("admin.reviewPolicies.detail.rulePromptLabel")}
                  placeholder={t("admin.reviewPolicies.detail.folderRulePromptPlaceholder")}
                  value={fr.prompt}
                  onChange={(e) => updateFolderRule(idx, { prompt: e.target.value })}
                />
              </div>
            );
          })}
          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              onClick={addFolderRule}
              disabled={folderRules.length >= MAX_RULES}
            >
              <PlusIcon className="h-4 w-4 mr-1" /> {t("admin.reviewPolicies.detail.addFolderRule")}
            </Button>
            <span className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.detail.ruleLimit", { count: folderRules.length, max: MAX_RULES })}
            </span>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-2">
            <div>
              <CardTitle>
                {t("admin.reviewPolicies.detail.suppressedTitle")}
                <OriginBadge overridden={suppressedRules !== null} />
              </CardTitle>
              <CardDescription>{t("admin.reviewPolicies.detail.suppressedDesc")}</CardDescription>
            </div>
            {suppressedRules !== null && (
              <ResetToInherited onClick={() => { setSuppressedRules(null); setDirty(true); }} />
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {suppressedRules === null ? (
            <>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.suppressedInheritedNote", {
                  count: policy.data?.suppressed_rules_effective?.length ?? 0,
                })}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {(policy.data?.suppressed_rules_effective ?? []).map((rule) => (
                  <Badge key={rule} variant="outline" className="font-mono text-[10px] font-normal">
                    {rule}
                  </Badge>
                ))}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setSuppressedRules([...(policy.data?.suppressed_rules_effective ?? [])]);
                  setDirty(true);
                }}
              >
                {t("admin.reviewPolicies.detail.suppressedOverride")}
              </Button>
            </>
          ) : (
            <>
              {suppressedRules.length === 0 && (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("admin.reviewPolicies.detail.suppressedEmpty")}
                </p>
              )}
              <div className="flex flex-wrap gap-1.5">
                {suppressedRules.map((rule) => (
                  <Badge key={rule} variant="outline" className="font-mono text-[10px] font-normal">
                    {rule}
                    <button
                      type="button"
                      className="ml-1 opacity-70 hover:opacity-100"
                      aria-label={t("admin.reviewPolicies.detail.removeItem", { item: rule })}
                      onClick={() => {
                        setSuppressedRules((prev) => (prev ?? []).filter((r) => r !== rule));
                        setDirty(true);
                      }}
                    >
                      <XIcon className="h-3 w-3" />
                    </button>
                  </Badge>
                ))}
              </div>
            </>
          )}
          <div className="flex gap-2">
            <Input
              aria-label={t("admin.reviewPolicies.detail.suppressedAddLabel")}
              className="flex-1 font-mono text-xs"
              placeholder="quality.todo"
              value={suppressedDraft}
              onChange={(e) => setSuppressedDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addSuppressed();
                }
              }}
            />
            <Button variant="outline" onClick={addSuppressed} disabled={!suppressedDraft.trim()}>
              <PlusIcon className="h-4 w-4 mr-1" />
              {t("admin.reviewPolicies.detail.suppressedAdd")}
            </Button>
          </div>
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
            <Link className="underline" href="/settings/llm#review-agents">{t("admin.reviewPolicies.detail.linkLlmSetup")}</Link>.{" "}
            <Link className="underline" href="/admin/review-defaults?tab=agents">
              {t("admin.reviewPolicies.detail.workspaceDefaultsLink")}
            </Link>
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
          <div className="flex items-start justify-between gap-2">
            <div>
              <CardTitle>
                {t("admin.reviewPolicies.detail.agentToggleTitle")}
                <OriginBadge
                  overridden={disabledAgents !== null}
                  from={inheritedFrom("disabled_agents")}
                />
              </CardTitle>
              <CardDescription>
                {t("admin.reviewPolicies.detail.agentToggleDesc")}{" "}
                {t("admin.reviewPolicies.detail.agentToggleInheritNote")}{" "}
                <Link className="underline" href="/admin/review-defaults?tab=agents">
                  {t("admin.reviewPolicies.detail.workspaceDefaultsLink")}
                </Link>
              </CardDescription>
            </div>
            {disabledAgents !== null && (
              <ResetToInherited onClick={() => { setDisabledAgents(null); setDirty(true); }} />
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {TOGGLEABLE_AGENTS.map((agent) => {
            const on = !disabledShown.includes(agent);
            const modelField = POLICY_AGENT_MODEL_FIELD[agent as PolicyAgent];
            const effective = policy.data?.agents_effective?.[agent];
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
                  {/* Model and limits sit one tab over; say which are in
                      force and link straight to them. */}
                  {modelField && (
                    <button
                      type="button"
                      onClick={() => setActiveTab("models")}
                      className="mt-1 inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
                    >
                      {effective?.model
                        ? t("admin.reviewPolicies.detail.agentRunsOn", {
                            model: effective.model,
                            tokens: effective.max_output_tokens ?? "—",
                          })
                        : t("admin.reviewPolicies.detail.agentModelLink")}
                      {policy.data?.[modelField] || policy.data?.agent_llm_overrides?.[agent]
                        ? ` · ${t("admin.reviewPolicies.detail.badgeOverridden")}`
                        : ""}
                      <ArrowRightIcon className="h-3 w-3" />
                    </button>
                  )}
                </div>
                <Switch
                  id={`toggle-${agent}`}
                  checked={on}
                  onCheckedChange={(v) => {
                    setDirty(true);
                    setDisabledAgents((prev) => {
                      const cur = prev ?? inheritedList("disabled_agents");
                      return v ? cur.filter((a) => a !== agent) : [...cur, agent];
                    });
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
                {!verifierOn && (
                  <Badge variant="destructive" className="ml-2 text-[9px]">
                    {t("admin.reviewPolicies.detail.agentOffBadge")}
                  </Badge>
                )}
                <OriginBadge
                  overridden={verifierChoice !== null}
                  from={inheritedFrom("verifier_enabled")}
                />
              </Label>
              <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.detail.verifierOptIn")}
              </p>
              {verifierChoice !== null && (
                <ResetToInherited onClick={() => { setVerifierChoice(null); setDirty(true); }} />
              )}
            </div>
            <Switch
              id="toggle-verifier"
              checked={verifierOn}
              onCheckedChange={(v) => {
                setDirty(true);
                setVerifierChoice(v);
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
        <CardContent className="space-y-5">
          <Callout tone="info">{t("admin.reviewPolicies.detail.promptPrecedence")}</Callout>
          {promptAgents.map((agent) => {
            const own = (promptOverrides[agent] ?? "").trim() !== "";
            const ws = wsAgentByName[agent];
            const inheritedLabel = ws?.has_override
              ? t("admin.reviewPolicies.detail.badgeInheritedWorkspacePrompt")
              : t("admin.reviewPolicies.detail.badgeInheritedBuiltin");
            return (
              <div key={agent} className="space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Label htmlFor={`prompt-${agent}`} className="font-medium capitalize">
                    {agent}
                    <OriginBadge overridden={own} inheritedLabel={inheritedLabel} />
                    {(agent === "verifier" ? !verifierOn : disabledShown.includes(agent)) && (
                      <Badge variant="destructive" className="ml-2 text-[9px]">
                        {t("admin.reviewPolicies.detail.agentOffBadge")}
                      </Badge>
                    )}
                  </Label>
                  <div className="flex flex-wrap items-center gap-1">
                    {own ? (
                      <ResetToInherited
                        onClick={() => {
                          setDirty(true);
                          setPromptOverrides((prev) => ({ ...prev, [agent]: "" }));
                        }}
                      />
                    ) : (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={!ws}
                        onClick={() => {
                          setDirty(true);
                          setPromptOverrides((prev) => ({ ...prev, [agent]: ws?.system_prompt ?? "" }));
                        }}
                      >
                        <CopyIcon className="h-3.5 w-3.5 mr-1" />
                        {t("admin.reviewPolicies.detail.startFromInherited")}
                      </Button>
                    )}
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => setPreviewAgent(agent)}
                    >
                      <EyeIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.reviewPolicies.detail.preview")}
                    </Button>
                  </div>
                </div>
                <Textarea
                  id={`prompt-${agent}`}
                  rows={own ? 8 : 3}
                  className={own ? "font-mono text-xs" : undefined}
                  placeholder={
                    ws?.has_override
                      ? t("admin.reviewPolicies.detail.inheritsWorkspacePrompt")
                      : t("admin.reviewPolicies.detail.inheritsBuiltinPrompt")
                  }
                  value={promptOverrides[agent] ?? ""}
                  onChange={(e) => {
                    setDirty(true);
                    setPromptOverrides((prev) => ({ ...prev, [agent]: e.target.value }));
                  }}
                />
                {!own && (
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    <Link className="underline" href={`/admin/agents/${agent}`}>
                      {t("admin.reviewPolicies.detail.editWorkspacePrompt")}
                    </Link>
                  </p>
                )}
              </div>
            );
          })}
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
              ["helpCommentsTitle", "helpCommentsBody"],
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

      {!canEdit && (
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("roles.promptsReadOnly")}</p>
      )}
      <div className="clear-agent-launcher flex items-center justify-between sticky bottom-0 bg-[var(--color-background)] border-t border-[var(--color-border)] py-3">
        <Button
          variant="ghost"
          onClick={async () => {
            const ok = await confirm({
              title: t("admin.reviewPolicies.detail.resetConfirm"),
              danger: true,
            });
            if (ok) reset.mutate();
          }}
          disabled={!canEdit || reset.isPending}
        >
          <RotateCcwIcon className="h-4 w-4 mr-1" /> {t("admin.reviewPolicies.detail.resetButton")}
        </Button>
        <div className="flex items-center gap-2">
          {dirty && !saveBlocked && (
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
          {!agentLLMBlocked && ignoreGlobsError && (
            <span className="text-xs text-red-600 dark:text-red-400">
              {t("review.settings.saveBlockedGlobs")}
            </span>
          )}
          {!agentLLMBlocked && !ignoreGlobsError && maxInlineBad && (
            <span className="text-xs text-red-600 dark:text-red-400">
              {t("admin.reviewPolicies.detail.saveBlockedInline")}
            </span>
          )}
          <Button
            onClick={() => save.mutate()}
            disabled={!canEdit || save.isPending || !dirty || saveBlocked}
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
            {preview.data.prompt_source && (
              <Callout tone="info">
                {t(`admin.reviewPolicies.detail.promptSource.${preview.data.prompt_source}`)}
              </Callout>
            )}
            <div>
              <h3 className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)] mb-1">
                system_instruction
              </h3>
              <pre className="text-xs bg-[var(--color-muted)] p-3 rounded whitespace-pre-wrap overflow-x-auto">
                {preview.data.system_prompt}
              </pre>
            </div>
            {preview.data.user_prompt_template && (
            <div>
              <h3 className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)] mb-1">
                user_prompt_template
              </h3>
              <pre className="text-xs bg-[var(--color-muted)] p-3 rounded whitespace-pre-wrap overflow-x-auto">
                {preview.data.user_prompt_template}
              </pre>
            </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
