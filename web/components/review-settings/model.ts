/**
 * The code review settings form, as data: what each scope stores, what it
 * inherits, what a save sends. No React here — the page and its sections
 * read this, and the guard tests lift its functions and run them.
 *
 * Two scopes, one shape. "Global" is the workspace review defaults
 * (GET/PUT /api/review-defaults: every value null = the install default) plus
 * the workspace agent prompts (/api/agents). A repository is its review
 * policy (GET/PUT/DELETE /api/review-policies/<slug>: every value null =
 * inherit the workspace, then the install). Every inheritable field is held
 * the same way at both: `own` (null = inherit) beside `inherited` and the
 * layer it comes from, so a field renders and resets identically wherever it
 * is edited.
 *
 *   repository (non-null) > workspace default (non-null) > install > built-in
 */

import type {
  AgentInfo,
  AgentLLMOverride,
  FolderRule,
  ModelCapabilities,
  ReviewPolicy,
  ReviewPolicyUpdate,
  SettingSource,
  WorkspaceReviewDefaults,
  WorkspaceReviewDefaultsUpdate,
} from "@/lib/api";
import { agentDraftFrom, agentEntryToSave, type AgentDraft } from "@/components/agent-llm-controls";
import { globLines } from "@/lib/ignore-globs";
import type { SectionId } from "@/lib/review-settings-routes";

export type Scope = { kind: "workspace" } | { kind: "repo"; slug: string };

export const SEVERITY_STEPS = ["info", "warning", "error", "critical"] as const;
export type Severity = (typeof SEVERITY_STEPS)[number];

/** Every setting a workspace may default and a repository may override —
 *  `INHERITABLE_FIELDS` in src/review/review_defaults.py plus the review
 *  language, which lives in the workspace LLM config. */
export const INHERITABLE_KEYS = [
  "disabled_agents",
  "verifier_enabled",
  "comment_min_severity",
  "max_inline_comments",
  "summary_enabled",
  "summary_instructions",
  "started_comment_enabled",
  "ignore_globs",
  "target_branches",
  "suppressed_rules",
  "enabled_agents",
  "run_on_drafts",
  "approve_when_clean",
  "request_changes_on_critical",
  "status_feedback",
  "committable_suggestions",
  "apply_filters_to_rules",
  "summary_target",
  "summary_on_new_commits",
  "summary_existing_description",
  "base_instruction",
  "message_started",
  "message_finished_header",
  "completed_comment",
  "commands_guide_enabled",
  "review_cadence",
  "auto_pause_pushes",
  "auto_pause_window_minutes",
  "ignored_title_keywords",
  "review_scope",
  "commands_enabled",
  "chat_enabled",
  "command_permission",
  "memories_enabled",
  "knowledge_approval",
  "memory_trusted_commenters",
  "learning_suppression",
  "learning_excluded_reviewers",
  "issues_auto_resolve",
  "issues_resolve_llm_verify",
  "issues_resolve_max_llm",
  "issues_announce_resolved",
  "task_context_enabled",
  "task_project_keys",
  "task_acceptance_field",
  "task_include_comments",
  "business_logic_auto",
  "requirements_check_mode",
  "task_urls_enabled",
  "review_language",
] as const;
export type InheritableKey = (typeof INHERITABLE_KEYS)[number];

/** The section each setting is edited in. "agents" is the per-agent model and
 *  limits block the overview counts as one overridden field. */
export const FIELD_SECTION: Record<InheritableKey | "agents", SectionId> = {
  target_branches: "general",
  run_on_drafts: "general",
  approve_when_clean: "general",
  request_changes_on_critical: "general",
  status_feedback: "general",
  committable_suggestions: "general",
  started_comment_enabled: "general",
  issues_auto_resolve: "general",
  issues_resolve_llm_verify: "general",
  issues_resolve_max_llm: "general",
  issues_announce_resolved: "general",
  review_language: "general",
  disabled_agents: "categories",
  enabled_agents: "categories",
  verifier_enabled: "categories",
  agents: "categories",
  comment_min_severity: "filters",
  max_inline_comments: "filters",
  apply_filters_to_rules: "filters",
  ignore_globs: "filters",
  suppressed_rules: "filters",
  base_instruction: "prompts",
  summary_enabled: "summary",
  summary_target: "summary",
  summary_on_new_commits: "summary",
  summary_existing_description: "summary",
  summary_instructions: "summary",
  completed_comment: "summary",
  commands_guide_enabled: "summary",
  review_cadence: "general",
  auto_pause_pushes: "general",
  auto_pause_window_minutes: "general",
  ignored_title_keywords: "general",
  review_scope: "general",
  commands_enabled: "commands",
  chat_enabled: "commands",
  command_permission: "commands",
  message_started: "messages",
  message_finished_header: "messages",
  memories_enabled: "learning",
  knowledge_approval: "learning",
  memory_trusted_commenters: "learning",
  learning_suppression: "learning",
  learning_excluded_reviewers: "learning",
  task_context_enabled: "categories",
  task_project_keys: "categories",
  task_acceptance_field: "categories",
  task_include_comments: "categories",
  business_logic_auto: "categories",
  requirements_check_mode: "categories",
  task_urls_enabled: "categories",
};

const LIST_KEYS = new Set<InheritableKey>([
  "disabled_agents", "enabled_agents", "target_branches", "ignored_title_keywords",
  "task_project_keys",
]);
/** Edited as one entry per line; held as the text while typing. */
const LINE_KEYS = new Set<InheritableKey>([
  "ignore_globs", "suppressed_rules", "memory_trusted_commenters",
  "learning_excluded_reviewers",
]);
/** Blank is "inherit" at every layer, never "say nothing". */
export const TEXT_KEYS = new Set<InheritableKey>([
  "summary_instructions", "base_instruction", "message_started", "message_finished_header",
  "task_acceptance_field",
]);

/** Server bounds, repeated so a value is refused at the keyboard. */
export const BASE_INSTRUCTION_MAX = 2000;
export const MESSAGE_MAX = 2000;
export const SUMMARY_INSTRUCTIONS_MAX = 4000;
export const PROMPT_TEMPLATE_MAX = 20_000;
export const MAX_INLINE_MIN = 1;
export const MAX_INLINE_MAX = 100;
/** Auto-pause bounds and keyword limits — src/review/review_defaults.py
 *  `INT_FIELDS`, `TITLE_KEYWORDS_MAX`, `TITLE_KEYWORD_MAX_CHARS`. */
export const AUTO_PAUSE_PUSHES_MIN = 2;
export const AUTO_PAUSE_PUSHES_MAX = 20;
export const AUTO_PAUSE_WINDOW_MIN = 1;
export const AUTO_PAUSE_WINDOW_MAX = 240;
export const TITLE_KEYWORDS_MAX = 50;
export const TITLE_KEYWORD_MAX_CHARS = 100;
/** `INT_FIELDS["issues_resolve_max_llm"]` on the server. */
export const ISSUES_MAX_LLM_MIN = 0;
export const ISSUES_MAX_LLM_MAX = 50;
/** `AgentPromptIn.system_prompt` min_length on the server. */
export const WORKSPACE_PROMPT_MIN = 10;
/** Team guidelines per agent and layer — src/review/prompt_guidelines.py
 *  `GUIDELINES_MAX_CHARS` (Kodus's cap). Longer is a 422. */
export const GUIDELINES_MAX = 2000;

/** What the form holds per field: the stored value, except the two line
 *  lists, which are the text being typed (null = inherit, "" = an empty list
 *  of this scope's own). */
export type OwnValues = Record<InheritableKey, unknown>;

/** A repository's per-agent model column (the names predate the agent
 *  restructure: contract's model is `architect_model`, defect's
 *  `quality_model` — src/review/settings.py LEGACY_AGENT_NAMES). */
export const POLICY_MODEL_FIELD = {
  defect: "quality_model",
  contract: "architect_model",
  security: "security_model",
  performance: "performance_model",
  business_logic: "business_logic_model",
  verifier: "verifier_model",
} as const;
export type PolicyLLMAgent = keyof typeof POLICY_MODEL_FIELD;
export const POLICY_LLM_AGENTS = Object.keys(POLICY_MODEL_FIELD) as PolicyLLMAgent[];

export type McpSource = NonNullable<ReviewPolicy["mcp_sources"]>[number];

/** A workspace agent prompt as edited: the full text, or "go back to the
 *  built-in" (the API has no endpoint for the built-in text of an agent that
 *  is overridden, so a reset is a flag until Save). */
export type WorkspacePromptDraft = { text: string; reset: boolean };

export type Draft = {
  own: OwnValues;
  /** Repository only. */
  enabled: boolean;
  department: string;
  promptTemplate: string;
  folderRules: FolderRule[];
  mcpSources: McpSource[];
  /** Repository: this repo's override per agent ("" = inherit).
   *  Global: unused (see `workspacePrompts`). */
  agentPrompts: Record<string, string>;
  workspacePrompts: Record<string, WorkspacePromptDraft>;
  /** Team guidelines per agent — ADDED to the agent's prompt. Repository:
   *  this repo's ("" = inherit the workspace's). Global: the workspace's
   *  ("" = none). */
  agentGuidelines: Record<string, string>;
  /** Repository only: agents whose guidelines here add to the workspace's
   *  instead of replacing them. */
  guidelinesExtend: string[];
  /** Per-agent model / output ceiling / reasoning / temperature. */
  agentLLM: Record<string, AgentDraft>;
};

/** What a field inherits at this scope, and from where. */
export type Inheritance = {
  values: Record<string, unknown>;
  sources: Partial<Record<string, SettingSource>>;
};

// ─── reading ─────────────────────────────────────────────────────────

function ownFrom(source: Record<string, unknown>): OwnValues {
  const out = {} as OwnValues;
  for (const key of INHERITABLE_KEYS) {
    const value = source[key];
    if (LINE_KEYS.has(key)) {
      out[key] = Array.isArray(value) ? value.join("\n") : null;
    } else if (LIST_KEYS.has(key)) {
      out[key] = Array.isArray(value) ? [...value] : null;
    } else if (TEXT_KEYS.has(key)) {
      out[key] = typeof value === "string" && value.trim() ? value : null;
    } else {
      out[key] = value ?? null;
    }
  }
  return out;
}

export function emptyDraft(): Draft {
  return {
    own: ownFrom({}),
    enabled: true,
    department: "",
    promptTemplate: "",
    folderRules: [],
    mcpSources: [],
    agentPrompts: {},
    workspacePrompts: {},
    agentGuidelines: {},
    guidelinesExtend: [],
    agentLLM: {},
  };
}

export function draftFromDefaults(
  d: WorkspaceReviewDefaults,
  agents: readonly AgentInfo[] | undefined,
  llmAgents: Record<string, AgentLLMOverride | null | undefined> | undefined,
  llmAgentNames: readonly string[],
): Draft {
  return {
    ...emptyDraft(),
    own: ownFrom(d as unknown as Record<string, unknown>),
    workspacePrompts: Object.fromEntries(
      (agents ?? []).map((a) => [a.name, { text: a.system_prompt, reset: false }]),
    ),
    agentGuidelines: Object.fromEntries(
      (agents ?? []).map((a) => [a.name, a.guidelines ?? ""]),
    ),
    agentLLM: Object.fromEntries(
      llmAgentNames.map((a) => [a, agentDraftFrom(llmAgents?.[a] ?? null)]),
    ),
  };
}

export function draftFromPolicy(p: ReviewPolicy): Draft {
  const llm = p.agent_llm_overrides ?? {};
  const prompts = p.agent_prompt_overrides ?? {};
  const guidelines = p.agent_prompt_guidelines ?? {};
  return {
    ...emptyDraft(),
    own: ownFrom(p as unknown as Record<string, unknown>),
    enabled: p.enabled,
    department: p.department ?? "",
    promptTemplate: p.prompt_template ?? "",
    folderRules: [...(p.folder_rules ?? [])],
    mcpSources: [...(p.mcp_sources ?? [])],
    agentPrompts: Object.fromEntries(
      (p.overridable_agents ?? Object.keys(prompts)).map((a) => [a, prompts[a] ?? ""]),
    ),
    agentGuidelines: Object.fromEntries(
      (p.overridable_agents ?? Object.keys(guidelines)).map((a) => [a, guidelines[a] ?? ""]),
    ),
    guidelinesExtend: [...(p.agent_guidelines_extend ?? [])],
    agentLLM: Object.fromEntries(POLICY_LLM_AGENTS.map((agent) => [agent, agentDraftFrom({
      model: (p[POLICY_MODEL_FIELD[agent] as keyof ReviewPolicy] as string | null | undefined) ?? null,
      max_output_tokens: llm[agent]?.max_output_tokens ?? null,
      reasoning: llm[agent]?.reasoning ?? null,
      temperature: llm[agent]?.temperature ?? null,
    })])),
  };
}

export function inheritanceForDefaults(d: WorkspaceReviewDefaults): Inheritance {
  const values = { review_language: "en", ...(d.install ?? {}) } as Record<string, unknown>;
  return {
    values,
    sources: Object.fromEntries(INHERITABLE_KEYS.map((k) => [k, "install" as const])),
  };
}

export function inheritanceForPolicy(p: ReviewPolicy): Inheritance {
  return { values: { ...(p.inherited ?? {}) }, sources: { ...(p.inherited_sources ?? {}) } };
}

// ─── the canonical value of a field ─────────────────────────────────

/** A field's value as the API stores it, or null for "inherit". At Global an
 *  empty branch or glob list IS the default (the server stores it as null),
 *  so it is spelled that way here too. */
export function canonical(key: InheritableKey, value: unknown, scope: Scope["kind"]): unknown {
  if (value === null || value === undefined) return null;
  if (LINE_KEYS.has(key)) {
    const lines = globLines(String(value));
    if (scope === "workspace" && lines.length === 0) return null;
    return lines;
  }
  if (key === "ignored_title_keywords") {
    // Case-insensitive duplicates fold into the first spelling, as on the server.
    const seen = new Set<string>();
    const list = (value as string[]).map((v) => v.trim()).filter((v) => {
      const folded = v.toLowerCase();
      if (!v || seen.has(folded)) return false;
      seen.add(folded);
      return true;
    });
    if (scope === "workspace" && list.length === 0) return null;
    return list;
  }
  if (LIST_KEYS.has(key)) {
    const list = [...new Set((value as string[]).map((v) => v.trim()).filter(Boolean))];
    if (scope === "workspace" && (key === "target_branches" || key === "task_project_keys")
      && list.length === 0) return null;
    return list;
  }
  if (TEXT_KEYS.has(key)) {
    const text = String(value);
    return text.trim() ? text : null;
  }
  if (key === "review_language") return value ? String(value) : null;
  if (key === "comment_min_severity") return value ? String(value) : null;
  return value;
}

export function canonicalOwn(own: OwnValues, scope: Scope["kind"]): Record<InheritableKey, unknown> {
  const out = {} as Record<InheritableKey, unknown>;
  for (const key of INHERITABLE_KEYS) out[key] = canonical(key, own[key], scope);
  return out;
}

/** Is this field set at this scope (overridden / a workspace default)? */
export function isSet(draft: Draft, key: InheritableKey, scope: Scope["kind"]): boolean {
  return canonical(key, draft.own[key], scope) !== null;
}

/** What a review would use for this field once the draft is saved. */
export function effective(
  draft: Draft, key: InheritableKey, scope: Scope["kind"], inh: Inheritance,
): unknown {
  const own = canonical(key, draft.own[key], scope);
  return own !== null ? own : inh.values[key] ?? null;
}

export function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

// ─── agents taking part ──────────────────────────────────────────────

/** agent → does it take part, from the EFFECTIVE lists and the built-in
 *  participation map (false = opt-in). A name in the off list wins. The
 *  rule of `agent_participation` in src/review/review_defaults.py. */
export function participation(
  disabled: readonly string[], enabled: readonly string[], defaults: Record<string, boolean>,
): Record<string, boolean> {
  const off = new Set(disabled);
  const on = new Set(enabled);
  return Object.fromEntries(Object.entries(defaults).map(([agent, byDefault]) => [
    agent, !off.has(agent) && (byDefault || on.has(agent)),
  ]));
}

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  const x = new Set(a);
  const y = new Set(b);
  return x.size === y.size && [...x].every((v) => y.has(v));
}

/** The two lists after switching `agent` to `on`. Opt-in agents are named in
 *  `enabled_agents`, the rest in `disabled_agents`. A list that ends up equal
 *  to what this scope inherits goes back to inheriting (null), so flipping a
 *  switch twice leaves no override behind. */
export function switchAgent(
  draft: Draft, inh: Inheritance, defaults: Record<string, boolean>, agent: string, on: boolean,
): Pick<OwnValues, "disabled_agents" | "enabled_agents"> {
  const inhDisabled = (inh.values.disabled_agents as string[] | null) ?? [];
  const inhEnabled = (inh.values.enabled_agents as string[] | null) ?? [];
  let disabled = [...((draft.own.disabled_agents as string[] | null) ?? inhDisabled)];
  let enabled = [...((draft.own.enabled_agents as string[] | null) ?? inhEnabled)];
  if (defaults[agent] === false) {
    enabled = enabled.filter((a) => a !== agent);
    if (on) {
      enabled.push(agent);
      disabled = disabled.filter((a) => a !== agent);
    }
  } else {
    disabled = disabled.filter((a) => a !== agent);
    if (!on) disabled.push(agent);
  }
  return {
    disabled_agents: sameSet(disabled, inhDisabled) ? null : disabled,
    enabled_agents: sameSet(enabled, inhEnabled) ? null : enabled,
  };
}

export function effectiveParticipation(
  draft: Draft, inh: Inheritance, defaults: Record<string, boolean>,
): Record<string, boolean> {
  return participation(
    (draft.own.disabled_agents as string[] | null) ?? (inh.values.disabled_agents as string[] | null) ?? [],
    (draft.own.enabled_agents as string[] | null) ?? (inh.values.enabled_agents as string[] | null) ?? [],
    defaults,
  );
}

export function inheritedParticipation(
  inh: Inheritance, defaults: Record<string, boolean>,
): Record<string, boolean> {
  return participation(
    (inh.values.disabled_agents as string[] | null) ?? [],
    (inh.values.enabled_agents as string[] | null) ?? [],
    defaults,
  );
}

// ─── the per-agent model rows ────────────────────────────────────────

/** The `agent_llm_overrides` map a repository save sends.
 *
 *  READ `reasoningToSave` IN components/agent-llm-controls.tsx BEFORE
 *  CHANGING THIS. The map REPLACES the stored one — absent is the only
 *  spelling of "stop overriding" the inheritance chain has — so every field
 *  this declines to emit is a field the save DELETES. No `model` key is ever
 *  emitted: a repository's model is its `<agent>_model` column, and the
 *  router 422s an entry that carries one.
 */
export function policyAgentLLMOverrides(
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

// ─── saving ──────────────────────────────────────────────────────────

/** PUT /api/review-defaults. A PATCH on the server; every field is sent so
 *  the save is exactly the screen. `agents` only when a model row was
 *  touched and the LLM config is loaded: a blank map clears every override. */
export function defaultsPayload(
  draft: Draft,
  agents?: { names: readonly string[]; stored: Record<string, AgentLLMOverride | null | undefined>;
    caps: Record<string, ModelCapabilities | null> } | null,
): WorkspaceReviewDefaultsUpdate {
  const c = canonicalOwn(draft.own, "workspace");
  const payload: WorkspaceReviewDefaultsUpdate = {
    disabled_agents: c.disabled_agents as string[] | null,
    enabled_agents: c.enabled_agents as string[] | null,
    verifier_enabled: c.verifier_enabled as boolean | null,
    comment_min_severity: c.comment_min_severity as WorkspaceReviewDefaults["comment_min_severity"],
    max_inline_comments: c.max_inline_comments as number | null,
    summary_enabled: c.summary_enabled as boolean | null,
    summary_instructions: c.summary_instructions as string | null,
    started_comment_enabled: c.started_comment_enabled as boolean | null,
    ignore_globs: c.ignore_globs as string[] | null,
    target_branches: c.target_branches as string[] | null,
    suppressed_rules: c.suppressed_rules as string[] | null,
    review_language: c.review_language as string | null,
    run_on_drafts: c.run_on_drafts as boolean | null,
    approve_when_clean: c.approve_when_clean as boolean | null,
    request_changes_on_critical: c.request_changes_on_critical as boolean | null,
    status_feedback: c.status_feedback as boolean | null,
    issues_auto_resolve: c.issues_auto_resolve as boolean | null,
    issues_resolve_llm_verify: c.issues_resolve_llm_verify as boolean | null,
    issues_resolve_max_llm: c.issues_resolve_max_llm as number | null,
    issues_announce_resolved: c.issues_announce_resolved as boolean | null,
    committable_suggestions: c.committable_suggestions as boolean | null,
    apply_filters_to_rules: c.apply_filters_to_rules as boolean | null,
    summary_target: c.summary_target as WorkspaceReviewDefaults["summary_target"],
    summary_on_new_commits: c.summary_on_new_commits as WorkspaceReviewDefaults["summary_on_new_commits"],
    summary_existing_description:
      c.summary_existing_description as WorkspaceReviewDefaults["summary_existing_description"],
    base_instruction: c.base_instruction as string | null,
    message_started: c.message_started as string | null,
    message_finished_header: c.message_finished_header as string | null,
    completed_comment: c.completed_comment as WorkspaceReviewDefaults["completed_comment"],
    commands_guide_enabled: c.commands_guide_enabled as boolean | null,
    review_cadence: c.review_cadence as WorkspaceReviewDefaults["review_cadence"],
    review_scope: c.review_scope as WorkspaceReviewDefaults["review_scope"],
    auto_pause_pushes: c.auto_pause_pushes as number | null,
    auto_pause_window_minutes: c.auto_pause_window_minutes as number | null,
    ignored_title_keywords: c.ignored_title_keywords as string[] | null,
    commands_enabled: c.commands_enabled as boolean | null,
    chat_enabled: c.chat_enabled as boolean | null,
    command_permission: c.command_permission as WorkspaceReviewDefaults["command_permission"],
    memories_enabled: c.memories_enabled as boolean | null,
    knowledge_approval: c.knowledge_approval as boolean | null,
    memory_trusted_commenters: c.memory_trusted_commenters as string[] | null,
    learning_suppression: c.learning_suppression as WorkspaceReviewDefaults["learning_suppression"],
    learning_excluded_reviewers: c.learning_excluded_reviewers as string[] | null,
    task_context_enabled: c.task_context_enabled as boolean | null,
    task_project_keys: c.task_project_keys as string[] | null,
    task_acceptance_field: c.task_acceptance_field as string | null,
    task_include_comments: c.task_include_comments as number | null,
    business_logic_auto: c.business_logic_auto as WorkspaceReviewDefaults["business_logic_auto"],
    requirements_check_mode: c.requirements_check_mode as WorkspaceReviewDefaults["requirements_check_mode"],
    task_urls_enabled: c.task_urls_enabled as boolean | null,
  };
  if (agents) {
    const map: Record<string, AgentLLMOverride> = {};
    for (const agent of agents.names) {
      const entry = agentEntryToSave(
        draft.agentLLM[agent] ?? agentDraftFrom(null), agents.stored[agent], agents.caps[agent] ?? null,
      );
      if (entry) map[agent] = entry;
    }
    payload.agents = map;
  }
  return payload;
}

/** PUT /api/review-policies/<slug> — a full replace of the older fields, so
 *  every one is sent; what this form cannot show is echoed back as loaded
 *  (legacy folder rules, `tests_model`, LLM entries of agents without a row
 *  here), never dropped. */
export function policyPayload(
  draft: Draft,
  policy: ReviewPolicy,
  caps: Record<string, ModelCapabilities | null>,
): ReviewPolicyUpdate {
  const c = canonicalOwn(draft.own, "repo");
  const verifierOn = c.verifier_enabled === true;
  const disabled = c.disabled_agents as string[] | null;
  const llm = draft.agentLLM;
  const model = (agent: PolicyLLMAgent) => (llm[agent]?.model ?? "").trim() || null;
  const stored = policy.agent_llm_overrides ?? {};
  const overrides = policyAgentLLMOverrides(POLICY_LLM_AGENTS, llm, stored, caps);
  // Entries for agents this form has no row for (compliance: no model
  // column) ride along untouched; the map replaces the stored one.
  for (const [agent, entry] of Object.entries(stored)) {
    if (!(POLICY_LLM_AGENTS as readonly string[]).includes(agent)) overrides[agent] = entry;
  }
  return {
    enabled: draft.enabled,
    prompt_template: draft.promptTemplate,
    department: draft.department.trim() || null,
    folder_rules: draft.folderRules.map((r) => ({
      pattern: r.pattern,
      prompt: r.prompt,
      ...(r.title?.trim() ? { title: r.title.trim() } : {}),
      ...(r.severity_hint ? { severity_hint: r.severity_hint } : {}),
      ...(r.agents && r.agents.length ? { agents: r.agents } : {}),
    })),
    architect_model: model("contract"),
    security_model: model("security"),
    quality_model: model("defect"),
    tests_model: policy.tests_model ?? null,
    verifier_model: model("verifier"),
    performance_model: model("performance"),
    business_logic_model: model("business_logic"),
    agent_llm_overrides: overrides,
    agent_prompt_overrides: Object.fromEntries(
      Object.entries(draft.agentPrompts).filter(([, v]) => v.trim()),
    ),
    agent_prompt_guidelines: Object.fromEntries(
      Object.entries(draft.agentGuidelines)
        .map(([k, v]) => [k, v.trim()] as const)
        .filter(([, v]) => v),
    ),
    agent_guidelines_extend: draft.guidelinesExtend,
    mcp_sources: draft.mcpSources,
    target_branches: c.target_branches as string[] | null,
    // Switching the veto on clears the deny-list's old spelling of off too,
    // or that keeps winning and the switch looks broken.
    disabled_agents: disabled === null || !verifierOn
      ? disabled : disabled.filter((a) => a !== "verifier"),
    enabled_agents: c.enabled_agents as string[] | null,
    verifier_enabled: c.verifier_enabled as boolean | null,
    ignore_globs: c.ignore_globs as string[] | null,
    comment_min_severity: c.comment_min_severity as ReviewPolicy["comment_min_severity"],
    suppressed_rules: c.suppressed_rules as string[] | null,
    max_inline_comments: c.max_inline_comments as number | null,
    summary_enabled: c.summary_enabled as boolean | null,
    summary_instructions: c.summary_instructions as string | null,
    started_comment_enabled: c.started_comment_enabled as boolean | null,
    review_language: c.review_language as string | null,
    run_on_drafts: c.run_on_drafts as boolean | null,
    approve_when_clean: c.approve_when_clean as boolean | null,
    request_changes_on_critical: c.request_changes_on_critical as boolean | null,
    status_feedback: c.status_feedback as boolean | null,
    issues_auto_resolve: c.issues_auto_resolve as boolean | null,
    issues_resolve_llm_verify: c.issues_resolve_llm_verify as boolean | null,
    issues_resolve_max_llm: c.issues_resolve_max_llm as number | null,
    issues_announce_resolved: c.issues_announce_resolved as boolean | null,
    committable_suggestions: c.committable_suggestions as boolean | null,
    apply_filters_to_rules: c.apply_filters_to_rules as boolean | null,
    summary_target: c.summary_target as ReviewPolicy["summary_target"],
    summary_on_new_commits: c.summary_on_new_commits as ReviewPolicy["summary_on_new_commits"],
    summary_existing_description:
      c.summary_existing_description as ReviewPolicy["summary_existing_description"],
    base_instruction: c.base_instruction as string | null,
    message_started: c.message_started as string | null,
    message_finished_header: c.message_finished_header as string | null,
    completed_comment: c.completed_comment as ReviewPolicy["completed_comment"],
    commands_guide_enabled: c.commands_guide_enabled as boolean | null,
    review_cadence: c.review_cadence as ReviewPolicy["review_cadence"],
    review_scope: c.review_scope as ReviewPolicy["review_scope"],
    auto_pause_pushes: c.auto_pause_pushes as number | null,
    auto_pause_window_minutes: c.auto_pause_window_minutes as number | null,
    ignored_title_keywords: c.ignored_title_keywords as string[] | null,
    commands_enabled: c.commands_enabled as boolean | null,
    chat_enabled: c.chat_enabled as boolean | null,
    command_permission: c.command_permission as ReviewPolicy["command_permission"],
    memories_enabled: c.memories_enabled as boolean | null,
    knowledge_approval: c.knowledge_approval as boolean | null,
    memory_trusted_commenters: c.memory_trusted_commenters as string[] | null,
    learning_suppression: c.learning_suppression as ReviewPolicy["learning_suppression"],
    learning_excluded_reviewers: c.learning_excluded_reviewers as string[] | null,
    task_context_enabled: c.task_context_enabled as boolean | null,
    task_project_keys: c.task_project_keys as string[] | null,
    task_acceptance_field: c.task_acceptance_field as string | null,
    task_include_comments: c.task_include_comments as number | null,
    business_logic_auto: c.business_logic_auto as ReviewPolicy["business_logic_auto"],
    requirements_check_mode: c.requirements_check_mode as ReviewPolicy["requirements_check_mode"],
    task_urls_enabled: c.task_urls_enabled as boolean | null,
  };
}

// ─── what changed ────────────────────────────────────────────────────

/** Every unsaved change, as the section it is in — the nav marks those
 *  sections and the header counts them. */
export function changedSections(
  draft: Draft, original: Draft, scope: Scope["kind"],
): Map<SectionId, number> {
  const out = new Map<SectionId, number>();
  const bump = (s: SectionId) => out.set(s, (out.get(s) ?? 0) + 1);
  const a = canonicalOwn(draft.own, scope);
  const b = canonicalOwn(original.own, scope);
  for (const key of INHERITABLE_KEYS) if (!sameValue(a[key], b[key])) bump(FIELD_SECTION[key]);
  if (!sameValue(draft.agentLLM, original.agentLLM)) bump("categories");
  for (const agent of new Set([...Object.keys(draft.agentPrompts), ...Object.keys(original.agentPrompts)])) {
    if ((draft.agentPrompts[agent] ?? "").trim() !== (original.agentPrompts[agent] ?? "").trim()) {
      bump("prompts");
    }
  }
  for (const agent of Object.keys(draft.workspacePrompts)) {
    if (!sameValue(draft.workspacePrompts[agent], original.workspacePrompts[agent])) bump("prompts");
  }
  for (const agent of new Set([...Object.keys(draft.agentGuidelines), ...Object.keys(original.agentGuidelines)])) {
    if ((draft.agentGuidelines[agent] ?? "").trim() !== (original.agentGuidelines[agent] ?? "").trim()) {
      bump("prompts");
    }
  }
  if (!sameValue([...draft.guidelinesExtend].sort(), [...original.guidelinesExtend].sort())) bump("prompts");
  if (draft.promptTemplate !== original.promptTemplate) bump("prompts");
  if (draft.enabled !== original.enabled) bump("general");
  if (draft.department.trim() !== original.department.trim()) bump("general");
  if (!sameValue(draft.mcpSources, original.mcpSources)) bump("advanced");
  return out;
}

/** How many settings of each section this scope sets — the badges beside
 *  the sections in the navigation. */
export function overriddenBySection(draft: Draft, scope: Scope["kind"]): Map<SectionId, number> {
  const out = new Map<SectionId, number>();
  const c = canonicalOwn(draft.own, scope);
  for (const key of INHERITABLE_KEYS) {
    if (c[key] !== null) out.set(FIELD_SECTION[key], (out.get(FIELD_SECTION[key]) ?? 0) + 1);
  }
  return out;
}

/** The same count for a repository that is not open, from the overview's
 *  `overridden_fields` — no policy fetch per row. */
export function sectionsOfFields(fields: readonly string[]): Map<SectionId, number> {
  const out = new Map<SectionId, number>();
  for (const f of fields) {
    const s = FIELD_SECTION[f as InheritableKey | "agents"];
    if (s) out.set(s, (out.get(s) ?? 0) + 1);
  }
  return out;
}
