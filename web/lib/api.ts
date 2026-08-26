/**
 * Typed fetch helper. Pulls JWT from NextAuth session and forwards it as
 * Bearer to the FastAPI backend.
 */

// API_BASE — how this bundle reaches FastAPI.
//
// IN THE BROWSER, A RELATIVE PATH, because the answer belongs to wherever the
// app is RUNNING and not to wherever it was BUILT. `NEXT_PUBLIC_*` is baked
// into the client bundle at build time, so an image built with an absolute URL
// works on exactly one installation — publish it and every other user's
// browser dutifully calls their own localhost and reports "Failed to fetch".
// That is not hypothetical: it is what happened when the first image built for
// the registry reached production.
//
// The default is `/backend`, which the reverse proxy in front of this app maps
// to FastAPI on the same origin. One image then serves any hostname, which is
// the whole point of publishing one.
//
// An absolute `NEXT_PUBLIC_API_BASE` still wins when it is set, for the
// development setup where Next runs on :3000 and FastAPI on :8000 with no
// proxy between them.
//
// ON THE NEXT.JS SERVER (auth.ts callbacks, route handlers) a relative path
// has nothing to be relative to, so that side keeps an absolute URL: prefer
// API_BASE_INTERNAL (typically http://api:8000) to stay inside the docker
// network, and fall back to the public one.
export const API_BASE =
  (typeof window === "undefined"
    ? process.env.API_BASE_INTERNAL ??
      process.env.NEXT_PUBLIC_API_BASE ??
      "http://localhost:8000"
    : process.env.NEXT_PUBLIC_API_BASE || "/backend");

export class ApiError extends Error {
  constructor(public status: number, public body: unknown, message: string) {
    super(message);
  }
}

/**
 * Auth plus the active-workspace hint — everything a request to this API needs
 * to be answered for the workspace the user is actually looking at.
 *
 * The switcher stores the workspace as a cookie; sending it as a header too
 * makes switching work even when API_BASE is a different origin, because
 * cookies are same-origin by default.
 *
 * Exported because file downloads cannot go through `api()` — they need the
 * raw Response to read a blob — and every one that hand-rolled its headers
 * sent only the bearer. The request then resolved to the account's DEFAULT
 * workspace: a member whose active workspace is a different one downloaded
 * either the wrong tenant's document or a 404.
 */
export function requestHeaders(
  token?: string | null,
  extra?: HeadersInit,
): Headers {
  const headers = new Headers(extra ?? {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (typeof document !== "undefined" && !headers.has("X-Workspace")) {
    const m = document.cookie.match(/(?:^|;\s*)x-workspace=([^;]+)/);
    if (m) headers.set("X-Workspace", decodeURIComponent(m[1]));
  }
  return headers;
}

export async function api<T = unknown>(
  path: string,
  opts: RequestInit & { token?: string | null; json?: unknown } = {},
): Promise<T> {
  const headers = requestHeaders(opts.token, opts.headers);
  if (opts.json !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
  const resp = await fetch(url, {
    ...opts,
    headers,
    body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body,
    cache: "no-store",
  });
  const text = await resp.text();
  let body: unknown = undefined;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!resp.ok) {
    const detail =
      (body && typeof body === "object" && "detail" in (body as Record<string, unknown>))
        ? String((body as Record<string, unknown>).detail)
        : `${resp.status} ${resp.statusText}`;
    throw new ApiError(resp.status, body, detail);
  }
  return body as T;
}

// ─── Typed wrappers ─────────────────────────────────────────────────

export interface UserOut {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
  auth_method: string;
  has_password: boolean;
  has_google: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
}

export interface ConnectionStatus {
  provider: "github" | "gitlab" | "bitbucket";
  connected: boolean;
  account_label: string;
  metadata: Record<string, unknown>;
  updated_at: string | null;
  last_used_at: string | null;
}

export interface ConnectionVerifyResult {
  ok: boolean;
  provider: string;
  username?: string | null;
  error?: string | null;
  scopes?: string[];
}

/** What a call did about a repository's code graph.
 *
 *  - `queued`            this call put a full index job in the queue
 *  - `already_queued`    an index for it was already pending/running
 *  - `already_indexed`   a graph exists, nothing to clone
 *  - `not_requested`     the caller asked for registration without an index
 *  - `queue_unavailable` the queue insert failed; the repo IS registered and
 *                        nothing is indexing it
 */
export type RepoIndexStatus =
  | "queued"
  | "already_queued"
  | "already_indexed"
  | "not_requested"
  | "queue_unavailable";

export interface RepoOut {
  slug: string;
  provider: string;
  full_name: string;
  url: string;
  indexed: boolean;
  /** null = nobody counted. Not the same as 0, which would mean an indexed
   *  repository with no symbols in it. The list endpoint does not count. */
  symbol_count: number | null;
  auto_review_enabled: boolean;
  auto_review_mode: "polling" | "webhook" | "manual";
  /** Branch to clone/index. null → whatever the provider calls default. */
  branch: string | null;
  /** True only when THIS response's call queued a new index job. */
  index_queued: boolean;
  /** Why `index_queued` is what it is. null on responses that started
   *  nothing and do not claim to — the list, the branch/auto-review toggles. */
  index_status: RepoIndexStatus | null;
  /** What the last index actually did, from the `repo_index_state` row.
   *
   *  `indexed` above is a file-exists check and cannot tell an hour-old graph
   *  from a March one, name the revision it was built from, or say that the
   *  newest attempt died — a repo with `indexed: false` AND
   *  `last_index_error` set is FAILING, not un-indexed, and those two look
   *  identical without these fields.
   *
   *  Only GET /api/repos fills them in. Optional because a server older than
   *  this field omits them entirely, and `undefined` must read as "not
   *  answered" rather than as "nothing recorded". */
  last_indexed_sha?: string | null;
  last_indexed_at?: string | null;
  last_full_rebuild_at?: string | null;
  last_index_error?: string | null;
  last_index_error_at?: string | null;
}

/** POST /api/repos body. `index` defaults to true server-side — send false
 *  only for bulk registration that must not clone one repo per row. */
export interface RepoAddRequest {
  url: string;
  auto_review?: boolean;
  branch?: string | null;
  index?: boolean;
}

export interface RepoBrowseItem {
  full_name: string;
  url: string;
  description: string;
  private: boolean;
  default_branch: string;
  already_added: boolean;
}

export interface PullRequestSummary {
  provider: string;
  repo: string;
  number: number;
  title: string;
  author: string;
  state: string;
  url: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ReviewRunOut {
  id: string;
  pr_ref: string;
  verdict: string;
  findings_count: number;
  critical: number;
  error: number;
  warning: number;
  info: number;
  cross_repo_callers: number;
  /** Deterministic cross-repo drift hits — counted apart from
   *  `findings_count`, which is the model's findings only. */
  drift_hits?: number;
  posted: boolean;
  started_at: string;
  elapsed_seconds: number | null;
  summary: string;
  /** Lifecycle state: 'queued' | 'running' | 'complete' | 'partial' |
   *  'failed' | 'skipped'. Separate from `verdict`, which doubles as the
   *  display state for queued/running/failed runs — a partial run still has
   *  a real verdict to show, and the gap travels here instead. */
  status?: string;
  /** Which agents answered and which failed. null (or absent) means the run
   *  predates the columns — "nobody wrote it down", which a consumer must
   *  not read as an all-clear; [] is "recorded, and none". */
  agents_run?: string[] | null;
  agents_failed?: string[] | null;
  /** Comment-cleanup outcome from the provider's post step. `complete:
   *  false` means an earlier run's comments may still be on the PR — worth a
   *  warning, not a shrug. Absent when the run never posted. */
  cleanup?: {
    deleted?: number;
    failed?: number;
    kept_threaded?: number;
    complete?: boolean;
  } | null;
  /** Every parameter Celmis changed behind the operator's back while this run
   *  was in flight — a ceiling clamped, a reasoning level or temperature the
   *  provider refused and the call retried without, a fallback model that
   *  took over an agent. Absent on runs recorded before the list existed;
   *  [] is "recorded, and nothing was changed". See `ParameterAdjustment`. */
  parameter_adjustments?: ParameterAdjustment[] | null;
  /** The length of the list above, for a history row that may ship the count
   *  without the rows. Either field alone is enough to show the badge. */
  adjustments_count?: number | null;
  /** What the run hid before posting, by cause — the deny-list's count per
   *  rule, the duplicates folded, the claims refused for want of evidence,
   *  the LLM veto's drops. Absent on runs recorded before it was written
   *  down, which is "nobody wrote it down", not "nothing was hidden". */
  hidden?: HiddenReport | null;
}

/** Findings a run hid before posting, by cause. Every field may be missing
 *  on a row another version of the pipeline wrote. */
export type HiddenReport = {
  by_rule?: Record<string, number>;
  duplicates?: number;
  near_duplicates?: number;
  low_confidence?: number;
  no_evidence?: number;
  coverage_claim?: number;
  veto?: number;
};

/** One parameter the runtime changed on its own while running an agent.
 *
 *  The runtime self-heals in four places and used to record each somewhere
 *  different: a ceiling above the model's maximum was clamped (one field on
 *  the LLM result), a reasoning value the provider refused was dropped and the
 *  call retried (another field, plus a process-wide memory), a temperature the
 *  model refuses was dropped the same way (an audit record and a log line),
 *  and a fallback model stepped in (the agent result). None of it reached a
 *  screen — a review quietly ran with less reasoning than it was configured
 *  for, and nobody could tell which knob to turn. This is the one shape all
 *  four now travel in, and it carries the remedy's raw material: what was
 *  asked, what went out, and the provider's own sentence for why.
 *
 *  `parameter` and `action` are open strings on purpose: a fifth self-heal
 *  must not 500 the history page, it must render as "something else was
 *  changed" until the UI learns its name.
 */
export type ParameterAdjustment = {
  /** The review stage the call belonged to. Null on a row another version
   *  of the pipeline wrote without it. */
  agent: string | null;
  /** "max_output_tokens" | "reasoning" | "temperature" | "model" */
  parameter: string;
  /** What the configuration asked for. */
  requested: string | number | null;
  /** What actually went to the provider; null when the parameter was left
   *  out of the retried request altogether. */
  sent: string | number | null;
  /** "clamped" | "dropped" | "swapped" */
  action: string;
  /** The provider's sentence, or the rule ("model ceiling is 65535"). */
  reason: string;
  /** The model the parameter was fitted to — "refused by gemini-3.7-flash",
   *  not just "refused", because a refusal is only actionable together with
   *  WHO refused it. Absent on rows written before it travelled. */
  model?: string | null;
};

// ─── Phase 2 — Projects + Chats + Q&A ──────────────────────────────

export interface ProjectRepoOut {
  repo_slug: string;
  role: string | null;
  added_at: string;
}

export interface ProjectRepoIn {
  repo_slug: string;
  role?: string | null;
}

export interface ProjectIn {
  name: string;
  description?: string | null;
  repos?: ProjectRepoIn[];
}

export interface ProjectOut {
  id: string;
  name: string;
  description: string | null;
  owner_user_id: string | null;
  created_at: string;
  updated_at: string;
  repos: ProjectRepoOut[];
  chats_count: number;
}

export interface RepoOwnerItem {
  owner: string;
  /** How many repos the capped scan saw under this owner — an ordering hint,
   *  not the provider's true total. */
  repo_count: number;
  has_registered: boolean;
}

export interface RepoDeveloperItem {
  /** Git author email, or a provider login when browsing — never both mixed. */
  identity: string;
  /** Other identities folded into this person: a second git config, a
   *  machine-local address. Empty when nothing was grouped. */
  aliases: string[];
  display_name: string;
  repos: string[];
  repo_count: number;
  /** Commits in the scanned window. Ordering only, not a total. */
  commits: number;
  /** A machine, not a colleague — `root@server`, a CI account. */
  is_robot: boolean;
}

export interface RepoDeveloperScan {
  developers: RepoDeveloperItem[];
  /** How many repositories the scan actually read, of `total` available. */
  scanned: number;
  total: number;
}

export interface AvailableRepo {
  repo_slug: string;
  vault_points: number;
  is_ready: boolean;
  in_projects: string[];
}

export interface ChatIn {
  project_id?: string | null;
  repo_slug?: string | null;
  name?: string | null;
}

export interface MessageMeta {
  type?: string | null;
  route?: string | null;
  tokens_in?: number;
  tokens_out?: number;
  vault_hits?: Array<{
    note_path: string;
    score: number;
    repo?: string;
  }>;
  files_read?: string[];
  files_read_count?: number;
  elapsed_s?: number | null;
  error?: boolean;
  /** Non-empty when the vector store (vault) was missing/unreachable — the
   *  answer was still built from grep + graph + code. Value is the reason. */
  vault_unavailable?: string | null;
  // Stage 22 — research-access boundary reporting
  blocked_repos?: string[];
  hidden_files?: string[];
  hidden_files_count?: number;
  code_included?: boolean;
  // Stage 23 — citation verification (file:line actually exists?)
  citations_total?: number;
  citations_invalid?: number;
  citations_bad?: Array<{
    label: string; target: string; repo: string | null; path: string | null;
    line: number | null; status: string; detail: string;
  }>;
}

export interface MessageOut {
  id: number;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  meta: MessageMeta | null;
}

export interface ChatOut {
  id: string;
  project_id: string | null;
  repo_slug: string | null;
  name: string | null;
  owner_user_id: string | null;
  created_at: string;
  updated_at: string;
  messages_count: number;
  messages: MessageOut[];
}

// ─── Typed helpers (high-level) ────────────────────────────────────

export const projectsApi = {
  list: (token: string) => api<ProjectOut[]>("/api/projects", { token }),
  get: (token: string, id: string) =>
    api<ProjectOut>(`/api/projects/${id}`, { token }),
  create: (token: string, payload: ProjectIn) =>
    api<ProjectOut>("/api/projects", { token, method: "POST", json: payload }),
  delete: (token: string, id: string) =>
    api<void>(`/api/projects/${id}`, { token, method: "DELETE" }),
  addRepo: (token: string, id: string, payload: ProjectRepoIn) =>
    api<ProjectRepoOut>(`/api/projects/${id}/repos`, {
      token, method: "POST", json: payload,
    }),
  removeRepo: (token: string, id: string, repoSlug: string) =>
    api<void>(`/api/projects/${id}/repos/${repoSlug}`, {
      token, method: "DELETE",
    }),
};

export const chatsApi = {
  list: (token: string, filters?: { project_id?: string; repo_slug?: string }) => {
    const qs = new URLSearchParams();
    if (filters?.project_id) qs.set("project_id", filters.project_id);
    if (filters?.repo_slug) qs.set("repo_slug", filters.repo_slug);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return api<ChatOut[]>(`/api/chats${suffix}`, { token });
  },
  get: (token: string, id: string) =>
    api<ChatOut>(`/api/chats/${id}`, { token }),
  create: (token: string, payload: ChatIn) =>
    api<ChatOut>("/api/chats", { token, method: "POST", json: payload }),
  delete: (token: string, id: string) =>
    api<void>(`/api/chats/${id}`, { token, method: "DELETE" }),
  clear: (token: string, id: string) =>
    api<{ deleted: number }>(`/api/chats/${id}/clear`, {
      token, method: "POST",
    }),
};

export const qaApi = {
  availableRepos: (token: string) =>
    api<AvailableRepo[]>("/api/qa/available-repos", { token }),
};

// SSE ask URL builder (consumption — окремий hook у lib/use-sse.ts)
export const askUrl = (chatId: string) =>
  `${API_BASE}/api/qa/chats/${chatId}/ask`;


// ─── Review policies (Stage 10) ─────────────────────────────────────

export type FolderRule = { pattern: string; prompt: string };

export type ReviewPolicy = {
  repo_slug: string;
  enabled: boolean;
  prompt_template: string;
  target_branches: string[];
  folder_rules: FolderRule[];
  department: string | null;
  created_at: string;
  updated_at: string;
  updated_by: string | null;
  // Stage 11 — per-agent model overrides (null = inherit workspace default).
  architect_model?: string | null;
  security_model?: string | null;
  quality_model?: string | null;
  tests_model?: string | null;
  verifier_model?: string | null;
  /** The OTHER two per-agent knobs for this repo, in the same shape the
   *  workspace `agents` blob uses: {defect: {max_output_tokens, reasoning}}.
   *  Keyed by the CURRENT agent names — `architect`/`quality`/`tests` are
   *  refused with 422 by the router's unknown-agent check.
   *
   *  Never a `model` key. The model of this layer is the flat `<agent>_model`
   *  field above, and the router 422s an entry that carries one — one field
   *  with two homes is how the screen and the runtime came to name different
   *  models three times in one review. Optional for an API that predates it. */
  agent_llm_overrides?: Record<string, AgentLLMOverride>;
  /** What each agent would run with if a review started now, after THIS
   *  policy, the workspace entry, the review profile and ReviewSettings have
   *  all had their say. `model` is the LiteLLM string.
   *
   *  The editor deliberately does not read this for its "inherits …" hints:
   *  it INCLUDES this policy's own stored values, so on the layer that wins it
   *  would answer "the workspace default is X" with X being this repo's own
   *  override. What an empty box inherits is the workspace entry —
   *  `LLMConfig.agents[agent].effective_*`, one layer down and never polluted
   *  by the page being edited. */
  agents_effective?: Record<string, {
    model: string;
    max_output_tokens: number | null;
    reasoning?: string | number | null;
    temperature?: number | null;
  }>;
  // Stage 12 — per-repo per-agent system_prompt overrides.
  agent_prompt_overrides?: Record<string, string>;
  // Per-repo MCP evidence sources.
  mcp_sources?: Array<{
    name: string; url: string; auth_type: string;
    api_key_ref: string | null;
    allowed_tools: string[]; trigger_patterns: string[];
  }>;
  // Agents switched off for this repo — they never run, spend no tokens
  // and produce no findings.
  disabled_agents?: string[];
  // Whether the LLM false-positive veto runs here. Three states: null is
  // "inherit the install default" (which is OFF), true/false is this repo's
  // own decision.
  verifier_enabled?: boolean | null;
  // What a review starting now would actually do — the answer above, or the
  // install default it inherits. Read-only; the PUT body drops it.
  verifier_enabled_effective?: boolean;
};

/** The PUT body. Not `Omit<ReviewPolicy, …>` alone, for two reasons that both
 *  end in a 422: the payload model is `extra="forbid"`, so `agents_effective`
 *  — a read-only projection — must not travel back; and
 *  `agent_llm_overrides` is three-state on the way IN where it is a plain map
 *  on the way out.
 *
 *  The three states, in the server's words: omitted or null keeps the stored
 *  map (which is what makes a client that cannot render these controls
 *  harmless), `{}` clears every override, and `{agent: null}` clears one. */
export type ReviewPolicyUpdate = Omit<
  ReviewPolicy,
  "repo_slug" | "created_at" | "updated_at" | "updated_by"
  | "agent_llm_overrides" | "agents_effective"
  | "verifier_enabled_effective"
> & {
  agent_llm_overrides?: Record<string, AgentLLMOverride | null> | null;
};

export type ReviewPolicyListItem = {
  repo_slug: string;
  department: string | null;
  enabled: boolean;
  target_branches: string[];
  has_custom_prompt: boolean;
  folder_rules_count: number;
  disabled_agents?: string[];
  updated_at: string;
};

export type RepoBranches = {
  repo_slug: string;
  branches: string[];
  default_branch: string | null;
};

export const reviewPoliciesApi = {
  list: (token: string, filters?: { department?: string; search?: string }) => {
    const qs = new URLSearchParams();
    if (filters?.department) qs.set("department", filters.department);
    if (filters?.search) qs.set("search", filters.search);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return api<ReviewPolicyListItem[]>(`/api/review-policies${suffix}`, { token });
  },
  get: (token: string, slug: string) =>
    api<ReviewPolicy>(`/api/review-policies/${encodeURIComponent(slug)}`, { token }),
  upsert: (
    token: string,
    slug: string,
    payload: ReviewPolicyUpdate,
  ) =>
    api<ReviewPolicy>(`/api/review-policies/${encodeURIComponent(slug)}`, {
      token,
      method: "PUT",
      json: payload,
    }),
  reset: (token: string, slug: string) =>
    api<void>(`/api/review-policies/${encodeURIComponent(slug)}`, {
      token,
      method: "DELETE",
    }),
  branches: (token: string, slug: string) =>
    api<RepoBranches>(`/api/review-policies/${encodeURIComponent(slug)}/branches`, { token }),
  promptPreview: (token: string, slug: string, agent: string) =>
    api<{ agent: string; system_prompt: string; user_prompt_template: string }>(
      `/api/review-policies/${encodeURIComponent(slug)}/prompt-preview?agent=${agent}`,
      { token },
    ),
};


// ─── Model catalog (Stage 11) ───────────────────────────────────────

export type ModelInfo = {
  id: string;
  provider: string;
  input_per_m: number;
  output_per_m: number;
  max_context: number | null;
  recommended_for: string | null;
  available: boolean;
};

export type ModelsCatalog = {
  connected_providers: string[];
  available_providers: string[];
  models: ModelInfo[];
  pricing_last_refreshed: string | null;
};

export const modelsApi = {
  available: (token: string, opts?: { allProviders?: boolean }) => {
    const qs = opts?.allProviders ? "?all_providers=true" : "";
    return api<ModelsCatalog>(`/api/models/available${qs}`, { token });
  },
  refreshPricing: (token: string) =>
    api<{ overlay_entries: number; last_refreshed: string | null }>(
      "/api/models/refresh-pricing",
      { token, method: "POST" },
    ),
};


// ─── Agents (Stage 11) ──────────────────────────────────────────────

export type AgentInfo = {
  name: string;
  display_name: string;
  role: string;
  focus: string[];
  context_used: string[];
  default_severity: string;
  verdict_impact: string;
  settings_model_field: string;
  system_prompt: string;
  user_prompt_template: string;
  has_override: boolean;
};

export const agentsApi = {
  list: (token: string) => api<AgentInfo[]>("/api/agents", { token }),
  get: (token: string, name: string) =>
    api<AgentInfo>(`/api/agents/${name}`, { token }),
  overridePrompt: (token: string, name: string, prompt: string) =>
    api<AgentInfo>(`/api/agents/${name}/prompt`, {
      token,
      method: "PUT",
      json: { system_prompt: prompt },
    }),
  resetPrompt: (token: string, name: string) =>
    api<AgentInfo>(`/api/agents/${name}/prompt`, {
      token,
      method: "DELETE",
    }),
};


// ─── Unified LLM config (Kodus-style consolidated /settings/llm) ────

export type LLMProfile = {
  provider: string;
  model: string;
  dimensions: number | null;
  /** Self-hosted (OpenAI-compatible) servers only — where the requests go.
   *  Absent for cloud providers, whose endpoint is implied by the vendor. */
  base_url?: string | null;
};
export type ProviderKeyStatus = {
  provider: string; connected: boolean; masked: string; source: "ui" | "env" | "none";
};

/** The surfaces that pick their own provider and model.
 *  "agent" is the Celmis agent's planner — a small exact call that used to
 *  borrow the chat profile and inherit whatever it was pointed at. */
export type LLMSurface = "chat" | "review" | "embeddings" | "agent";

/** The review agents an operator can point at their own model and budget.
 *
 *  Repo policy already carries per-agent MODEL overrides (architect_model, …)
 *  for one repository; this list is the workspace-wide layer under it, and it
 *  includes `compliance`, which the repo policy shape never got a column for.
 */
export const REVIEW_AGENTS = [
  "defect", "contract", "security", "verifier", "compliance",
] as const;
export type ReviewAgent = (typeof REVIEW_AGENTS)[number];

/** One entry of the workspace blob's "agents" key. Every field is optional and
 *  absent means inherit: repo policy → this → the review surface profile →
 *  the ReviewSettings default. */
export type AgentLLMOverride = {
  model?: string | null;
  max_output_tokens?: number | null;
  /** Sampling temperature. null means inherit; 0 is a value, not an absence. */
  temperature?: number | null;
  /** Two vocabularies behind one field, which is why the capabilities call
   *  exists: OpenAI takes an effort word ("low"/"medium"/"high"), Anthropic
   *  and Gemini take a thinking budget in tokens (Gemini's -1 = dynamic). The
   *  UI sends whichever shape `reasoning_kind` names for the chosen model. */
  reasoning?: string | number | null;
};

/** One entry of GET /api/llm/config's "agents" map: the workspace overrides
 *  above, plus what the agent actually ends up with once the chain has run.
 *
 *  The effective trio is why the form can show inheritance as a value instead
 *  of six empty boxes — and `effective_model` arrives as a LiteLLM string with
 *  its vendor prefix already attached, so it can be handed straight back to
 *  modelCapabilities() without this file re-deriving a prefix the server
 *  already knows. "" means the chain ran out: unknown, not a substitute. */
export type AgentSettings = AgentLLMOverride & {
  effective_model: string;
  effective_max_output_tokens: number | null;
  effective_reasoning?: string | number | null;
  effective_temperature?: number | null;
};

/** The server's bounds for a per-agent output ceiling, mirroring
 *  AGENT_TOKENS_MIN / AGENT_TOKENS_MAX in src/api/routers/llm.py. Repeated
 *  here so the form refuses out-of-range values in front of the person typing
 *  them, rather than collecting a 422 after the save. A model's OWN ceiling is
 *  narrower still and comes from modelCapabilities(). */
export const AGENT_TOKENS_MIN = 64;
export const AGENT_TOKENS_MAX = 200_000;

/** GET /api/llm/model-capabilities — what the model ACTUALLY supports,
 *  read out of litellm rather than a hand-written per-vendor table that goes
 *  stale the week after it is written.
 *
 *  `known: false` is a model litellm has no entry for — a self-hosted
 *  `openai/<name>`, a release newer than the installed litellm. Everything
 *  else is null then, and the UI says "unknown" instead of inventing a
 *  ceiling.
 */
export type ModelCapabilities = {
  model: string;
  known: boolean;
  max_output_tokens: number | null;
  supports_reasoning: boolean | null;
  reasoning_kind: "effort" | "budget" | null;
  /** The effort words the ROUTER accepts for this model, minus anything the
   *  provider has since refused — what the dropdown may offer. */
  reasoning_values: string[] | null;
  /** The words the provider answered 400 to, bare. The older spelling of the
   *  list below: a value, with no sentence and no date. Kept so a build whose
   *  server predates `provider_refusals` still strikes the word instead of
   *  silently losing it from the dropdown. */
  reasoning_values_provider_refused?: string[] | null;
  /** What the provider has refused for this model, with when it was learned
   *  — so the row can say "refused by the provider on <date>: <sentence>"
   *  rather than drop an option between two page loads and leave the operator
   *  wondering whether they imagined it. Absent until the server ships it. */
  provider_refusals?: ProviderRefusal[] | null;
  supports_function_calling: boolean | null;
  source: "litellm" | "unknown";
};

/** One thing a provider has refused for one model, measured rather than
 *  predicted: the call went out and came back 400 with this sentence.
 *
 *  `parameter` is "reasoning" for an effort word the provider will not take,
 *  "temperature" for a model that accepts only its own default (claude-sonnet-5
 *  answers 400 to anything but 1 — there the value is the one that was
 *  refused). Open strings, like `ParameterAdjustment`, so a new kind of
 *  refusal renders as a refusal before the UI learns its name.
 */
export type ProviderRefusal = {
  parameter: string;
  value: string | number | null;
  reason: string;
  /** ISO timestamp of the call that taught it, or null when unknown. */
  seen_at: string | null;
};

export type LLMConfig = {
  provider: string | null;
  model: string | null;
  temperature: number;
  max_output_tokens: number;
  system_prompt_extras: string;
  api_key_masked: string;
  api_key_connected: boolean;
  connection_last_verified: string | null;
  openrouter_enabled: boolean;
  openrouter_key_masked: string;
  openrouter_key_connected: boolean;
  // Stage 22.1 — per-surface profiles + shared provider keys
  profiles: Record<LLMSurface, LLMProfile>;
  review_engine?: "api" | "claude_code";
  review_language?: string;
  /** Model a failing review agent retries on once the primary is exhausted.
   *  Null = no fallback (the default) — the trade is liveness for
   *  comparability between runs, and it is the operator's to make. */
  review_fallback_model?: string | null;
  /** Language the generated vault documentation is written in. */
  docs_language?: string;
  /** Which engine writes it: `api` (one prompt) or `claude_code`
   *  (an agent that researches through the Celmis index). */
  docs_engine?: string;
  provider_keys: ProviderKeyStatus[];
  /** Per-agent settings for the review agents — every known agent is present,
   *  so an absent key never has to be read as either "inherit" or "no such
   *  agent". Optional only for an API that predates them. */
  agents?: Partial<Record<ReviewAgent, AgentSettings>>;
  embeddings_reindex_needed: boolean;
  /** What actually runs for embeddings when the operator pins it in the
   *  server environment. When present, the editable embeddings profile is
   *  cosmetic — the page shows this block read-only instead, so nobody
   *  edits a dropdown that does not run. */
  effective_embeddings?: EffectiveEmbeddings | null;
};

export type EffectiveEmbeddings = {
  provider: string;
  model: string;
  dimensions?: number | null;
  base_url?: string | null;
  source?: string;
};

export type LLMConfigUpdate = {
  provider?: string | null;
  model?: string | null;
  temperature?: number;
  max_output_tokens?: number;
  system_prompt_extras?: string;
  api_key?: string | null;
  openrouter_enabled?: boolean;
  openrouter_api_key?: string | null;
  // Per-surface profile updates + shared provider keys to save.
  profiles?: Partial<Record<LLMSurface, { provider?: string; model?: string; dimensions?: number; base_url?: string }>>;
  /** Review fallback model — "" clears it (empty means no fallback);
   *  omitting the key keeps whatever is stored. */
  review_fallback_model?: string;
  /** Whole "agents" map, replaced as one blob — a partial PUT would have no
   *  way to say "clear this override", since absent already means inherit. */
  agents?: Partial<Record<ReviewAgent, AgentLLMOverride>>;
  provider_keys?: Record<string, string>;
};

export type ProviderModels = {
  provider: string; generation: string[]; embedding: string[]; detail: string;
};

export type TestConnectionResult = {
  ok: boolean;
  provider: string;
  detail: string;
  latency_ms: number | null;
  models_available: number | null;
  balance_usd: number | null;
  /** Embeddings ping against a self-hosted server reports the width of the
   *  vector it actually got back — the number that must match the index. */
  vector_width?: number | null;
  /** Server-side caution that is not a failure — e.g. the returned vector
   *  width differs from the configured dimensions. */
  warning?: string | null;
};

/** GET /api/llm/local-setup-guide — how to stand up a self-hosted
 *  OpenAI-compatible server. Content is authored on the backend (English)
 *  so it can evolve without a frontend release. */
export type LocalSetupGuideOption = {
  name: string;
  command: string;
  base_url_hint: string;
  notes: string;
};

export type LocalSetupGuide = {
  options: LocalSetupGuideOption[];
  env: string[];
  reindex_warning: string;
};

export const llmApi = {
  getConfig: (token: string) => api<LLMConfig>("/api/llm/config", { token }),
  saveConfig: (token: string, payload: LLMConfigUpdate) =>
    api<LLMConfig>("/api/llm/config", { token, method: "PUT", json: payload }),
  testConnection: (
    token: string,
    body: {
      provider: string;
      /** Optional because the self-hosted provider is keyless by design;
       *  hosted providers still refuse without one (readable detail, not 422). */
      api_key?: string;
      model?: string | null;
      /** Self-hosted (OpenAI-compatible) only — which server to ping. */
      base_url?: string;
      /** "chat" (generation surfaces, review included) or "embeddings" —
       *  an embeddings test reports the vector width, which is what
       *  actually matters there. The backend accepts only these two. */
      surface?: "chat" | "embeddings";
    },
  ) =>
    api<TestConnectionResult>("/api/llm/test-connection", {
      token,
      method: "POST",
      json: body,
    }),
  localSetupGuide: (token: string) =>
    api<LocalSetupGuide>("/api/llm/local-setup-guide", { token }),
  providerModels: (token: string, provider: string) =>
    api<ProviderModels>(`/api/llm/models?provider=${encodeURIComponent(provider)}`, { token }),
  /** What this exact model supports. `model` is a LiteLLM model string —
   *  "gemini/gemini-3-flash-preview", not the bare "gemini-3-flash-preview" a
   *  profile stores, because litellm resolves vendor from the prefix. */
  modelCapabilities: (token: string, model: string) =>
    api<ModelCapabilities>(
      `/api/llm/model-capabilities?model=${encodeURIComponent(model)}`, { token }),
  reindexEmbeddings: (token: string) =>
    api<{ enqueued: number; repos: string[]; signature: string; detail: string }>(
      "/api/llm/embeddings/reindex", { token, method: "POST" }),
};


// ─── Usage summary (Stage 11) ───────────────────────────────────────

export type DailyUsage = {
  date: string;
  runs: number;
  tokens_input: number;
  tokens_output: number;
  cost_usd: number;
};

export type UsageSummary = {
  days: number;
  total_runs: number;
  completed_runs: number;
  failed_runs: number;
  tokens_input: number;
  tokens_output: number;
  cost_usd: number;
  cost_source_mix: Record<string, number>;
  daily: DailyUsage[];
};

export const usageApi = {
  summary: (token: string, days = 30) =>
    api<UsageSummary>(`/api/usage/summary?days=${days}`, { token }),
};


// ─── BYOK — multi-provider key manager ──────────────────────────────

// ─── Stage 15 — intel + notifications ───────────────────────────────

export type NotificationChannel = {
  id: string;
  name: string;
  kind: "slack" | "discord" | "google_chat" | "webhook";
  webhook_url: string;
  config: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
  created_by: string | null;
};

export type ChannelBinding = {
  id: string;
  channel_id: string;
  repo_slug: string | null;
  event: string;
  min_severity: string;
  enabled: boolean;
};

export const notificationsApi = {
  listChannels: (t: string) =>
    api<NotificationChannel[]>("/api/notifications/channels", { token: t }),
  createChannel: (t: string, p: Partial<NotificationChannel>) =>
    api<NotificationChannel>("/api/notifications/channels", {
      token: t, method: "POST", json: p,
    }),
  deleteChannel: (t: string, id: string) =>
    api<void>(`/api/notifications/channels/${id}`, { token: t, method: "DELETE" }),
  testChannel: (t: string, id: string) =>
    api<{ ok: boolean; detail: string }>(
      `/api/notifications/channels/${id}/test`, { token: t, method: "POST" }),
  listBindings: (t: string) =>
    api<ChannelBinding[]>("/api/notifications/bindings", { token: t }),
  createBinding: (t: string, p: Partial<ChannelBinding>) =>
    api<ChannelBinding>("/api/notifications/bindings", {
      token: t, method: "POST", json: p,
    }),
  deleteBinding: (t: string, id: string) =>
    api<void>(`/api/notifications/bindings/${id}`, { token: t, method: "DELETE" }),
};

export type Ownership = {
  repo_slug: string;
  computed_at: string | null;
  lookback_days: number;
  stats: { files_total?: number; distinct_authors?: number; top_owners?: Array<{ identity: string; commits: number }> };
  paths: Record<string, { primary_owner: string | null; codeowners?: string[]; top_authors?: Array<{ name: string; email: string; commits: number }> }>;
};

export type Architecture = {
  repo_slug: string;
  summary_md: string;
  model_used: string | null;
  computed_at: string | null;
};

export type Deprecation = {
  id: string;
  repo_slug: string;
  symbol: string;
  reason: string;
  replacement: string | null;
  target_removal_at: string | null;
  deprecated_at: string;
  deprecated_by: string | null;
  last_scan_at: string | null;
  consumers: Array<{ repo_slug: string; file?: string; line?: number; symbol?: string }>;
};

/** GET /api/intel/reverse-index/{slug} — {source_file → [note_paths]}.
 * `index` values are typed loosely so an older/other payload shape falls
 * back to the raw-JSON view instead of crashing the page. */
export type ReverseIndexOut = {
  repo_slug?: string;
  source_file_count?: number;
  note_count?: number;
  index?: Record<string, unknown>;
};

export const intelApi = {
  ownership: (t: string, slug: string) =>
    api<Ownership>(`/api/intel/ownership/${encodeURIComponent(slug)}`, { token: t }),
  rebuildOwnership: (t: string, slug: string, lookback = 90) =>
    api<{ snapshot_id: string; repo_slug: string }>(
      `/api/intel/ownership/${encodeURIComponent(slug)}/rebuild?lookback_days=${lookback}`,
      { token: t, method: "POST" }),
  architecture: (t: string, slug: string) =>
    api<Architecture>(`/api/intel/architecture/${encodeURIComponent(slug)}`, { token: t }),
  rebuildArchitecture: (t: string, slug: string) =>
    api<Architecture>(`/api/intel/architecture/${encodeURIComponent(slug)}/rebuild`, {
      token: t, method: "POST",
    }),
  reverseIndex: (t: string, slug: string) =>
    api<ReverseIndexOut>(
      `/api/intel/reverse-index/${encodeURIComponent(slug)}`, { token: t }),
  listDeprecations: (t: string) =>
    api<Deprecation[]>("/api/intel/deprecations", { token: t }),
  createDeprecation: (t: string, p: Partial<Deprecation>) =>
    api<Deprecation>("/api/intel/deprecations", {
      token: t, method: "POST", json: p,
    }),
  deleteDeprecation: (t: string, id: string) =>
    api<void>(`/api/intel/deprecations/${id}`, { token: t, method: "DELETE" }),
  scanDeprecation: (t: string, id: string) =>
    api<Deprecation>(`/api/intel/deprecations/${id}/scan`, {
      token: t, method: "POST",
    }),
};


// ─── Compliance checks (Stage 14) ───────────────────────────────────

export type ComplianceCheck = {
  id: string;
  name: string;
  description: string;
  scope: string;
  glob_pattern: string;
  rule: string;
  severity: "error" | "warn";
  blocking: boolean;
  enabled: boolean;
  created_by: string | null;
};

export type ComplianceCheckIn = Omit<ComplianceCheck, "id" | "created_by">;

export const complianceApi = {
  list: (token: string) => api<ComplianceCheck[]>("/api/compliance", { token }),
  create: (token: string, payload: ComplianceCheckIn) =>
    api<ComplianceCheck>("/api/compliance", { token, method: "POST", json: payload }),
  update: (token: string, id: string, payload: ComplianceCheckIn) =>
    api<ComplianceCheck>(`/api/compliance/${id}`, { token, method: "PUT", json: payload }),
  remove: (token: string, id: string) =>
    api<void>(`/api/compliance/${id}`, { token, method: "DELETE" }),
};


// ─── Teams / RBAC (Stage 14) ────────────────────────────────────────

export type Team = {
  id: string;
  name: string;
  description: string;
  member_count: number;
};

export type TeamMember = { user_id: string; role: string };
export type RepoAccess = { repo_slug: string; permission: string };
export type MyTeams = {
  teams: Team[];
  repo_permissions: Record<string, string>;
};

export const teamsApi = {
  list: (token: string) => api<Team[]>("/api/teams", { token }),
  create: (token: string, name: string, description = "") =>
    api<Team>("/api/teams", { token, method: "POST", json: { name, description } }),
  remove: (token: string, id: string) =>
    api<void>(`/api/teams/${id}`, { token, method: "DELETE" }),
  members: (token: string, id: string) =>
    api<TeamMember[]>(`/api/teams/${id}/members`, { token }),
  upsertMember: (token: string, teamId: string, userId: string, role: string) =>
    api<TeamMember>(`/api/teams/${teamId}/members/${userId}`, {
      token, method: "PUT", json: { role },
    }),
  removeMember: (token: string, teamId: string, userId: string) =>
    api<void>(`/api/teams/${teamId}/members/${userId}`, { token, method: "DELETE" }),
  repos: (token: string, id: string) =>
    api<RepoAccess[]>(`/api/teams/${id}/repos`, { token }),
  grantRepo: (token: string, teamId: string, repoSlug: string, permission: string) =>
    api<RepoAccess>(`/api/teams/${teamId}/repos/${encodeURIComponent(repoSlug)}`, {
      token, method: "PUT", json: { permission },
    }),
  revokeRepo: (token: string, teamId: string, repoSlug: string) =>
    api<void>(`/api/teams/${teamId}/repos/${encodeURIComponent(repoSlug)}`, {
      token, method: "DELETE",
    }),
  me: (token: string) => api<MyTeams>("/api/teams/me", { token }),
};


// ─── Apply-fix (Stage 14) ───────────────────────────────────────────

export type ApplyFixIn = {
  provider: "github" | "gitlab" | "bitbucket";
  repo: string;
  pr_number: number;
  head_ref: string;
  head_sha: string;
  file_path: string;
  line_start: number;
  line_end: number;
  replacement: string;
  finding_id?: string;
  commit_message?: string;
};

export type ApplyFixOut = {
  check_state?: string;
  check_reason?: string;
  ok: boolean;
  commit_sha: string | null;
  commit_url: string | null;
  branch: string | null;
  detail: string;
};

export const applyFixApi = {
  apply: (token: string, runId: string, payload: ApplyFixIn) =>
    api<ApplyFixOut>(`/api/reviews/${runId}/apply-fix`, {
      token, method: "POST", json: payload,
    }),
};

export type FindingOut = {
  id: string;
  agent: string;
  file_path: string;
  line: number;
  severity: "critical" | "error" | "warning" | "info";
  title: string;
  body: string;
  suggestion: string | null;
  rule_id: string;
  confidence: number;
};

export type FindingsPayload = {
  run_id: string;
  pr: {
    provider: string | null;
    repo: string | null;
    number: number | null;
    head_sha: string | null;
    head_ref: string | null;
  };
  findings: FindingOut[];
  /** The deterministic half, deliberately NOT merged into `findings`.
   *  A grep result with a file and a line and a model's judgement are
   *  different kinds of claim; one list behind a badge invites the reader to
   *  trust them equally. */
  drift: {
    group: string | null;
    repos_scanned: string[];
    hits: {
      value: string;
      removed_from: { file: string; line: number };
      still_in: { repo: string; file: string; line: number; excerpt: string }[];
      truncated: number;
    }[];
  } | null;
  count: number;
  total: number;
  limit: number;
  offset: number;
  legacy: boolean;
};

/** GET /api/reviews/{id} — one run, the same shape as a /history row.
 *  Fetched on demand when a row arrives with an adjustments COUNT but without
 *  the rows themselves (a lean history), so the table can still open. */
export const reviewRunsApi = {
  get: (token: string, runId: string) =>
    api<ReviewRunOut>(`/api/reviews/${encodeURIComponent(runId)}`, { token }),
};

export const findingsApi = {
  list: (token: string, runId: string, opts?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (opts?.limit) qs.set("limit", String(opts.limit));
    if (opts?.offset) qs.set("offset", String(opts.offset));
    const q = qs.toString() ? `?${qs.toString()}` : "";
    return api<FindingsPayload>(`/api/reviews/${runId}/findings${q}`, { token });
  },
  diff: (token: string, runId: string) =>
    api<{ run_id: string; diff: string; bytes: number; available: boolean }>(
      `/api/reviews/${runId}/diff`, { token }),
};


// ─── Stage 21 — audit / search / integrations health ────────────────

export type AuditRecord = {
  request_id: string;
  timestamp: string;
  mode: string;
  operation: string;
  model: string;
  repo: string | null;
  module: string | null;
  input_tokens_estimated: number;
  output_tokens_estimated: number;
  duration_ms: number;
  error: string | null;
};

export const auditApi = {
  list: (token: string, f?: {
    from_ts?: string; to_ts?: string; mode?: string;
    operation?: string; repo?: string; limit?: number; offset?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(f || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== "") qs.set(k, String(v));
    });
    const q = qs.toString() ? `?${qs.toString()}` : "";
    return api<{ records: AuditRecord[]; count: number; offset: number; limit: number }>(
      `/api/audit${q}`, { token });
  },
  exportUrl: (f?: Record<string, string>) => {
    const qs = new URLSearchParams(f || {});
    return `${API_BASE}/api/audit/export${qs.toString() ? `?${qs}` : ""}`;
  },
  facets: (token: string) =>
    api<{ modes: string[]; operations: string[]; repos: string[]; scanned: number }>(
      "/api/audit/facets", { token }),
  stats: (token: string, f?: {
    from_ts?: string; to_ts?: string; mode?: string; operation?: string; repo?: string;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(f || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== "") qs.set(k, String(v));
    });
    const q = qs.toString() ? `?${qs.toString()}` : "";
    return api<{ total_calls: number; input_tokens: number; output_tokens: number; errors: number }>(
      `/api/audit/stats${q}`, { token });
  },
};

export type SearchResult = {
  query: string;
  symbols: Array<{
    repo_slug: string; name: string; kind: string;
    file: string; line: number; language: string | null;
    web_url: string | null;
  }>;
  notes: Array<{
    note_path: string; score: number; type: string;
    module: string | null; repo: string; keywords: string[];
    path: string | null; web_url: string | null;
  }>;
  symbol_count: number;
  note_count: number;
  symbols_error?: string | null;
  notes_error?: string | null;
};

export const searchApi = {
  search: (token: string, q: string, opts?: { repo?: string; limit?: number }) => {
    const qs = new URLSearchParams({ q });
    if (opts?.repo) qs.set("repo", opts.repo);
    if (opts?.limit) qs.set("limit", String(opts.limit));
    return api<SearchResult>(`/api/search?${qs.toString()}`, { token });
  },
};

export type IntegrationCard = {
  kind: string;
  name: string;
  status: string;
  detail: string;
};

export const integrationsHealthApi = {
  get: (token: string) =>
    api<{ cards: IntegrationCard[]; count: number }>(
      "/api/health/integrations", { token }),
};


// ─── Stage 18 — sync jobs ───────────────────────────────────────────

export type SyncJob = {
  id: string;
  kind: string;
  dedup_key: string | null;
  payload: Record<string, unknown>;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "dead";
  cancel_requested?: boolean;
  attempts: number;
  max_attempts: number;
  next_run_at: string;
  locked_by: string | null;
  last_error: string | null;
  enqueued_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

// ─── Stage 19 — workspaces ──────────────────────────────────────────

export type WorkspaceSummary = {
  id: string;
  name: string;
  slug: string;
  description: string;
  role: string | null;
};

export type MyWorkspaces = {
  workspaces: WorkspaceSummary[];
  active_id: string | null;
};

// ─── OAuth clients admin ────────────────────────────────────────────

export type OAuthClientSummary = {
  client_id: string;
  name: string;
  redirect_uris: string[];
  allowed_scopes: string[];
  is_public: boolean;
  created_at: string;
  created_by: string | null;
};

export type OAuthClientRegistered = OAuthClientSummary & {
  client_secret: string | null;
};

export const oauthClientsApi = {
  list: (token: string) =>
    api<OAuthClientSummary[]>("/oauth/clients", { token }),
  register: (token: string, p: {
    name: string; redirect_uris: string[];
    allowed_scopes: string[]; public: boolean;
  }) =>
    api<OAuthClientRegistered>("/oauth/register", {
      token, method: "POST", json: p,
    }),
  remove: (token: string, client_id: string) =>
    api<void>(`/oauth/clients/${client_id}`, { token, method: "DELETE" }),
};


export type WorkspaceMember = { user_id: string; role: string; email?: string; name?: string };

export const workspacesApi = {
  me: (token: string) => api<MyWorkspaces>("/api/workspaces", { token }),
  create: (token: string, name: string, slug: string, description = "") =>
    api<WorkspaceSummary>("/api/workspaces", {
      token, method: "POST", json: { name, slug, description },
    }),
  remove: (token: string, id: string) =>
    api<void>(`/api/workspaces/${id}`, { token, method: "DELETE" }),
  members: (token: string, wsId: string) =>
    api<WorkspaceMember[]>(`/api/workspaces/${wsId}/members`, { token }),
  upsertMember: (token: string, wsId: string, userId: string, role: string) =>
    api<WorkspaceMember>(`/api/workspaces/${wsId}/members/${userId}`, {
      token, method: "PUT", json: { role },
    }),
  removeMember: (token: string, wsId: string, userId: string) =>
    api<void>(`/api/workspaces/${wsId}/members/${userId}`, {
      token, method: "DELETE",
    }),
};

// ─── Stage 22 — user directory + fine-grained research access ────────

export type UserDirectoryEntry = {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
  is_active: boolean;
};

export const usersApi = {
  list: (token: string, includeInactive = false) =>
    api<UserDirectoryEntry[]>(
      `/api/users${includeInactive ? "?include_inactive=true" : ""}`,
      { token },
    ),
  // Minimal directory (id/email/name) — readable by any signed-in user.
  directory: (token: string) =>
    api<{ id: string; email: string; name: string }[]>("/api/users/directory", { token }),
};

export type AccessVisibility = "none" | "metadata" | "code";

export type AccessRule = {
  id: string;
  workspace_id: string;
  team_id: string;
  team_name: string | null;
  repo_slug: string;
  visibility: AccessVisibility;
  allow_globs: string[];
  deny_globs: string[];
  sensitivity_tags: string[];
  note: string;
};

export type AccessRuleIn = {
  team_id: string;
  repo_slug: string;
  visibility: AccessVisibility;
  allow_globs: string[];
  deny_globs: string[];
  sensitivity_tags: string[];
  note: string;
};

export type MyAccessItem = {
  repo_slug: string;
  visibility: AccessVisibility;
  researchable: boolean;
  code_visible: boolean;
  open_default: boolean;
  allow_globs: string[];
  deny_globs: string[];
  sensitivity_tags: string[];
};

export const accessApi = {
  listRules: (token: string, filters?: { repo_slug?: string; team_id?: string }) => {
    const qs = new URLSearchParams();
    if (filters?.repo_slug) qs.set("repo_slug", filters.repo_slug);
    if (filters?.team_id) qs.set("team_id", filters.team_id);
    const q = qs.toString() ? `?${qs.toString()}` : "";
    return api<AccessRule[]>(`/api/access/rules${q}`, { token });
  },
  upsertRule: (token: string, rule: AccessRuleIn) =>
    api<AccessRule>("/api/access/rules", { token, method: "PUT", json: rule }),
  deleteRule: (token: string, ruleId: string) =>
    api<void>(`/api/access/rules/${ruleId}`, { token, method: "DELETE" }),
  my: (token: string, repoSlug?: string) =>
    api<MyAccessItem[]>(
      `/api/access/my${repoSlug ? `?repo_slug=${encodeURIComponent(repoSlug)}` : ""}`,
      { token },
    ),
};

// ─── Stage 21 — GDPR export / erasure ───────────────────────────────

export const gdprApi = {
  exportUrl: (userId: string) => `${API_BASE}/api/gdpr/export/${userId}`,
  exportData: (token: string, userId: string) =>
    api<Record<string, unknown>>(`/api/gdpr/export/${userId}`, { token }),
  erase: (token: string, userId: string) =>
    api<{ erased: boolean }>(`/api/gdpr/user/${userId}`, {
      token, method: "DELETE",
    }),
};


export const jobsApi = {
  list: (token: string, filters?: { status?: string; kind?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (filters?.status) qs.set("status", filters.status);
    if (filters?.kind) qs.set("kind", filters.kind);
    if (filters?.limit) qs.set("limit", String(filters.limit));
    const q = qs.toString() ? `?${qs.toString()}` : "";
    return api<SyncJob[]>(`/api/jobs${q}`, { token });
  },
  stats: (token: string) =>
    api<Record<string, number>>("/api/jobs/stats", { token }),
  create: (token: string, body: Partial<SyncJob> & { kind: string }) =>
    api<{ id: string | null; deduped: boolean }>("/api/jobs", {
      token, method: "POST", json: body,
    }),
  retry: (token: string, id: string) =>
    api<{ ok: boolean }>(`/api/jobs/${id}/retry`, { token, method: "POST" }),
  cancel: (token: string, id: string) =>
    api<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { token, method: "POST" }),
  remove: (token: string, id: string) =>
    api<void>(`/api/jobs/${id}`, { token, method: "DELETE" }),
};

// ─── Stage 23 — spend ledger, budgets, finding feedback, invites, auth ──

export type SpendGroupRow = {
  key: string; calls: number; tokens_in: number; tokens_out: number;
  cached_tokens_in: number; cost_usd: number;
  /** What to draw when the key is an internal id — a person's name for the
   *  by-user rows. The key stays the key, so clicking still filters by it. */
  label?: string | null;
};

export type SpendSummary = {
  days: number; calls: number; tokens_in: number; tokens_out: number;
  cached_tokens_in: number; cost_usd: number;
  cache_hit_pct: number; estimated_share_pct: number;
  subscription_calls: number;
  /** key is "subscription" or "api_key" — see spend.py */
  by_billing: SpendGroupRow[];
  by_surface: SpendGroupRow[]; by_agent: SpendGroupRow[];
  by_model: SpendGroupRow[]; by_provider: SpendGroupRow[];
  /** Which repository the tokens went to. "—" = a surface that is not
   *  per-repo (chat, embeddings), not missing data. */
  by_repo: SpendGroupRow[];
  /** Which job inside the surface — module_prd, integration_doc,
   *  automation_interpret, deps_report. The finest cut available. */
  by_operation: SpendGroupRow[];
  by_user: SpendGroupRow[];
  /** The window the API actually used, ISO. Echoed back so a page cannot
   *  disagree with the numbers under its own range label. */
  since: string;
  until: string;
};

export type SpendDaily = {
  date: string; cost_usd: number; tokens_in: number; tokens_out: number; calls: number;
};

/** Series granularity. `hour` exists for "what happened during that run this
 *  afternoon", which a daily bar cannot answer at all. */
export type SpendBucket = "hour" | "day" | "week" | "month";

/**
 * The window + filters BOTH spend reads share.
 *
 * Same shape for /summary and /daily on purpose: a question asked of the
 * totals can be asked of the series without rephrasing it, so drilling into
 * "PR review → this repo → this agent" re-reads both endpoints with one
 * object. `days` is ignored by the API whenever since/until are given.
 */
export type SpendQuery = {
  days?: number;
  since?: string;
  until?: string;
  surface?: string | null;
  repo?: string | null;
  model?: string | null;
  operation?: string | null;
  agent?: string | null;
  /** Rows per breakdown. The server caps a summary at 50 so it stays a
   *  summary; raise it when the reader is hunting for a row below that,
   *  which was otherwise unreachable by any amount of clicking. */
  limit?: number;
};

const SPEND_FILTER_KEYS = ["surface", "repo", "model", "operation", "agent"] as const;

/** `number` stays accepted so the plain `summary(token, 30)` callers keep
 *  working — the days-only case is still the common one. */
function spendParams(
  q: number | SpendQuery,
  extra?: Record<string, string>,
): string {
  const query: SpendQuery = typeof q === "number" ? { days: q } : q;
  const p = new URLSearchParams();
  p.set("days", String(query.days ?? 30));
  if (query.since) p.set("since", query.since);
  if (query.until) p.set("until", query.until);
  if (query.limit) p.set("limit", String(query.limit));
  for (const k of SPEND_FILTER_KEYS) {
    const v = query[k];
    if (v) p.set(k, v);
  }
  for (const [k, v] of Object.entries(extra ?? {})) p.set(k, v);
  return p.toString();
}

export type Budget = {
  workspace_id: string; cap_usd: number; spent_usd: number; used_pct: number;
  hard_stop: boolean; alert_pct: number; enabled: boolean;
  over_cap: boolean; over_alert: boolean;
};

export const spendApi = {
  summary: (token: string, q: number | SpendQuery = 30) =>
    api<SpendSummary>(`/api/spend/summary?${spendParams(q)}`, { token }),
  daily: (token: string, q: number | SpendQuery = 30, bucket: SpendBucket = "day") =>
    api<SpendDaily[]>(`/api/spend/daily?${spendParams(q, { bucket })}`, { token }),
  getBudget: (token: string) => api<Budget>("/api/spend/budget", { token }),
  saveBudget: (
    token: string,
    body: { monthly_usd_cap: number; alert_pct: number; hard_stop: boolean },
  ) => api<Budget>("/api/spend/budget", { token, method: "PUT", json: body }),
};

export type FindingFeedback = {
  finding_key: string; state: "accepted" | "dismissed"; reason: string;
  agent: string | null; severity: string | null; user_id: string | null;
};

export type AgentFeedbackStat = {
  agent: string; accepted: number; dismissed: number; dismissal_rate_pct: number;
};

export const feedbackApi = {
  forRun: (token: string, runId: string) =>
    api<FindingFeedback[]>(`/api/feedback/run/${runId}`, { token }),
  set: (token: string, runId: string, body: {
    finding_key: string; state: "accepted" | "dismissed"; reason?: string;
    agent?: string | null; severity?: string | null; repo_slug?: string | null;
  }) => api<FindingFeedback>(`/api/feedback/run/${runId}`, {
    token, method: "PUT", json: body,
  }),
  clear: (token: string, runId: string, key: string) =>
    api<void>(`/api/feedback/run/${runId}/${key}`, { token, method: "DELETE" }),
  stats: (token: string) => api<AgentFeedbackStat[]>("/api/feedback/stats", { token }),
};

export type Invite = {
  id: string; workspace_id: string; email: string | null; role: string;
  max_uses: number; used_count: number; expires_at: string;
  revoked: boolean; created_by: string | null;
};

export type InviteCreated = Invite & {
  token?: string | null; invite_url?: string | null; added_directly?: boolean;
};

export type InvitePreview = {
  workspace_id: string; workspace_name: string; role: string;
  email_bound: boolean; valid: boolean; detail: string;
};

export const invitesApi = {
  list: (token: string) => api<Invite[]>("/api/invites", { token }),
  create: (token: string, body: {
    email?: string | null; role: string; ttl_days?: number; max_uses?: number;
    /** Minutes until the invite dies. Wins over ttl_days when both are sent. */
    ttl_minutes?: number;
    /** Link invites only — the server ignores it for an emailed invite. */
    never_expires?: boolean;
  }) => api<InviteCreated>("/api/invites", { token, method: "POST", json: body }),
  revoke: (token: string, id: string) =>
    api<void>(`/api/invites/${id}`, { token, method: "DELETE" }),
  preview: (inviteToken: string) =>
    api<InvitePreview>(`/api/invites/preview/${inviteToken}`),
  accept: (token: string, inviteToken: string) =>
    api<{ ok: boolean; workspace_id: string; workspace_slug: string; role: string }>(
      "/api/invites/accept", { token, method: "POST", json: { token: inviteToken } }),
};

export type ResetLink = { url: string; expires_at: string; email: string };

export const authApi = {
  forgotPassword: (email: string) =>
    api<{ ok: boolean; detail: string }>(
      "/api/auth/forgot-password", { method: "POST", json: { email } }),
  // Returns 204 — resetting a password deliberately does not start a session.
  resetPassword: (token: string, password: string) =>
    api<void>("/api/auth/reset-password", {
      method: "POST", json: { token, password },
    }),
  // Admin-only: the mailer-less delivery path. The admin passes the link on.
  createResetLink: (token: string, userId: string) =>
    api<ResetLink>(`/api/users/${userId}/reset-link`, { token, method: "POST" }),
  // Workspace-admin variant — scoped to members of the given workspace.
  createWsResetLink: (token: string, wsId: string, userId: string) =>
    api<{ url: string; expires_at: string }>(
      `/api/workspaces/${wsId}/members/${userId}/reset-link`,
      { token, method: "POST" },
    ),
  // Self-service: permanently delete the caller's own account (204).
  deleteAccount: (token: string) =>
    api<void>("/api/auth/me", { token, method: "DELETE" }),
};

// ─── Embedded Claude Code agent ─────────────────────────────────────

export type ClaudeConnectionStatus = {
  personal: boolean;
  workspace: boolean;
  workspace_saved_by?: string | null;
};

/** How a session runs — see src/agent/modes.py for what each one changes. */
export type AgentMode = "standard" | "workflow";

/**
 * Model ALIASES, not pinned ids — the CLI resolves each to the current model
 * ("--model … an alias for the latest model"). A pinned list goes stale the
 * day a new model ships. Mirrors MODEL_ALIASES in src/agent/modes.py; the API
 * also accepts any full claude-* id for a model newer than this list.
 */
export const AGENT_MODELS = ["", "opus", "sonnet", "haiku", "fable"] as const;

export type AgentSession = {
  id: string;
  mode?: AgentMode;
  model?: string;
  /** Every repo the session covers; first entry equals repo_slug. */
  repo_slugs?: string[];
  /** Set when the session was started from a project rather than a bare repo.
   *  Not a live link — the project can be renamed or deleted afterwards. */
  project_id?: string | null;
  repo_slug: string;
  title: string;
  prompt: string;
  /** `paused` is not an ending: no process is running, the branch is already
   *  pushed and the transcript is stored, and sending a message resumes the
   *  conversation where it left off. */
  status: "queued" | "running" | "paused" | "done" | "error" | "cancelled";
  /** How many times this conversation has been picked back up. */
  resume_count?: number;
  /** When the stored transcript is swept and resuming stops working. */
  resumable_until?: string | null;
  result: {
    branch?: string;
    compare_url?: string;
    pr_url?: string;
    commit?: string;
    summary?: string;
    turns?: number;
    duration_ms?: number;
    cost_usd?: number | null;
    push_error?: string;
  };
  error: string | null;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
};

export const claudeApi = {
  connection: (token: string) =>
    api<ClaudeConnectionStatus>("/api/claude/connection", { token }),
  connect: (token: string, oauthToken: string, scope: "personal" | "workspace") =>
    api<ClaudeConnectionStatus>("/api/claude/connection", {
      token, method: "PUT", json: { token: oauthToken, scope },
    }),
  disconnect: (token: string, scope: "personal" | "workspace") =>
    api<void>(`/api/claude/connection/${scope}`, { token, method: "DELETE" }),

  sessions: (token: string) =>
    api<AgentSession[]>("/api/agent-sessions?limit=100", { token }),

  /** Another turn in a live session. 409 means it no longer takes any. */
  sendMessage: (token: string, id: string, text: string) =>
    api<{ ok: boolean }>(`/api/agent-sessions/${id}/messages`, {
      token, method: "POST", json: { text },
    }),

  /** End the conversation cleanly: push what was done and open the PR.
   *  Distinct from `stop`, which interrupts a turn in flight. */
  finish: (token: string, id: string) =>
    api<{ ok: boolean }>(`/api/agent-sessions/${id}/finish`, {
      token, method: "POST",
    }),

  /** Put a text file where the running agent can Read it. FormData, so no
   *  Content-Type is set — the browser has to add its own boundary. */
  /** Attach a file BEFORE the session exists.
   *
   *  Returns the staging id, which the create call then carries. The id is
   *  minted by the server on the first upload and reused for the rest, so a
   *  set of files lands in one place. */
  stageAttachment: (token: string, file: File, stagingId?: string | null) => {
    const form = new FormData();
    form.append("file", file);
    if (stagingId) form.append("staging_id", stagingId);
    return fetch(`${API_BASE}/api/agent-sessions/staged-attachments`, {
      method: "POST",
      headers: requestHeaders(token),
      body: form,
    }).then(async (r) => {
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
      return r.json() as Promise<{ staging_id: string; name: string; bytes: number }>;
    });
  },

  attach: (token: string, id: string, file: File) => {
    const body = new FormData();
    body.append("file", file);
    return api<{ ok: boolean; path: string; bytes: number }>(
      `/api/agent-sessions/${id}/attachments`, { token, method: "POST", body },
    );
  },
  createSession: (
    token: string,
    repo_slug: string,
    prompt: string,
    opts?: {
      title?: string; mode?: AgentMode; model?: string; repo_slugs?: string[];
      project_id?: string | null;
      /** Files attached before the session existed — see `stageAttachment`. */
      staging_id?: string | null;
    },
  ) =>
    api<AgentSession>("/api/agent-sessions", {
      token,
      method: "POST",
      json: {
        repo_slug,
        prompt,
        title: opts?.title ?? "",
        mode: opts?.mode ?? "standard",
        model: opts?.model ?? "",
        repo_slugs: opts?.repo_slugs ?? [],
        project_id: opts?.project_id ?? null,
        staging_id: opts?.staging_id ?? null,
      },
    }),
  session: (token: string, id: string) =>
    api<AgentSession>(`/api/agent-sessions/${id}`, { token }),
  stop: (token: string, id: string) =>
    api<{ ok: boolean }>(`/api/agent-sessions/${id}/stop`, { token, method: "POST" }),
  streamUrl: (id: string, after = 0) =>
    `${API_BASE}/api/agent-sessions/${id}/stream?after=${after}`,
};

// ─── Monitoring alerts ──────────────────────────────────────────────

export type IncomingAlert = {
  id: string;
  source: string;
  title: string;
  body: string;
  severity: "info" | "warning" | "error" | "critical";
  status: "new" | "acked" | "fixed";
  repo_hint: string | null;
  session_id: string | null;
  created_at: string;
};

// ─── Ops metrics (resource history) ─────────────────────────────────

export type ResourceSampleOut = {
  ts: string; cpu_pct: number; rss_mb: number; sys_mem_pct: number;
  load1: number; reviews_running: number; jobs_running: number;
  jobs_pending: number; agent_sessions_running: number;
  llm_calls: number; llm_tokens_in: number; llm_tokens_out: number;
  http_requests: number;
};

export type OpsMetrics = {
  samples: ResourceSampleOut[];
  aggregates: Record<string, { avg: number; max: number; total: number }>;
  window_hours: number;
};

export type LogRecordOut = {
  ts: string; level: string; logger: string; message: string;
  module: string; exc?: string;
};

export type LogTail = {
  records: LogRecordOut[];
  stats: {
    buffered: number; capacity: number;
    by_level: Record<string, number>;
    oldest: string | null; newest: string | null;
  };
};

export type PushConfig = { enabled: boolean; public_key: string; devices: number };

export const pushApi = {
  config: (token: string) => api<PushConfig>("/api/push/config", { token }),
  subscribe: (token: string, body: unknown) =>
    api<{ ok: boolean }>("/api/push/subscribe", { token, method: "POST", json: body }),
  unsubscribe: (token: string, body: unknown) =>
    api<{ ok: boolean }>("/api/push/subscribe", { token, method: "DELETE", json: body }),
  test: (token: string) =>
    api<{ sent: number; expired: number; failed: number }>("/api/push/test", {
      token, method: "POST",
    }),
};

export const opsApi = {
  metrics: (token: string, hours = 24) =>
    api<OpsMetrics>(`/api/ops/metrics?hours=${hours}`, { token }),
  csvUrl: (hours = 24) => `${API_BASE}/api/ops/metrics.csv?hours=${hours}`,
  logs: (token: string, f?: { limit?: number; level?: string; contains?: string }) => {
    const qs = new URLSearchParams();
    if (f?.limit) qs.set("limit", String(f.limit));
    if (f?.level) qs.set("level", f.level);
    if (f?.contains) qs.set("contains", f.contains);
    const q = qs.toString() ? `?${qs}` : "";
    return api<LogTail>(`/api/ops/logs${q}`, { token });
  },
  diag: (token: string) => api<Record<string, unknown>>("/api/ops/diag", { token }),
};

// ─── Vector store ───────────────────────────────────────────────────

export type VectorStoreConfig = {
  type: "local" | "qdrant" | "pinecone" | "weaviate";
  url: string;
  api_key_set: boolean;
  source: "ui" | "env" | "default";
  supported: string[];
  planned: string[];
};

export const vectorStoreApi = {
  get: (token: string) => api<VectorStoreConfig>("/api/vector-store", { token }),
  save: (token: string, cfg: { type: string; url?: string; api_key?: string }) =>
    api<VectorStoreConfig>("/api/vector-store", { token, method: "PUT", json: cfg }),
  clear: (token: string) => api<void>("/api/vector-store", { token, method: "DELETE" }),
  test: (token: string, cfg: { type: string; url?: string; api_key?: string }) =>
    api<{ ok: boolean; detail: string; collections: string[] }>(
      "/api/vector-store/test", { token, method: "POST", json: cfg }),
};

// ─── Dependency audit ───────────────────────────────────────────────

/** A supply-chain hygiene finding, as the auditor writes it into the summary.
 *
 *  Deterministic by construction — a lock file that disagrees with its
 *  manifest, a package that runs a script at install, a dependency fetched
 *  from outside the registry. Kept apart from CVEs and from anything a model
 *  produced, because this is the class of finding a reader can check.
 *
 *  `line` and `excerpt` are the evidence itself and are null/empty where the
 *  source genuinely has none: npm's lock records `hasInstallScript` as a
 *  boolean and never the script body. */
export type HygieneItem = {
  repo?: string;
  kind: string;
  severity: string;
  ecosystem: string;
  package: string;
  detail: string;
  location: string;
  subproject?: string;
  line?: number | null;
  excerpt?: string;
};

export type DepAuditRun = {
  id: string;
  status: "queued" | "running" | "done" | "error";
  summary: {
    hygiene?: {
      total?: number;
      by_kind?: Record<string, number>;
      items?: HygieneItem[];
    };
    repos_total?: number;
    repos_scanned?: number;
    repos_skipped?: string[];
    skip_reasons?: Record<string, string>;
    packages?: number;
    unique_packages?: number;
    outdated?: number;
    vulnerable?: number;
    by_severity?: Record<string, number>;
    ai_report?: string;
    ai_report_engine?: string;
    vuln_check_errors?: number;
    ai_report_error?: string;
    // Live progress while status is queued/running (rewritten per phase).
    phase?: string;
    repos_done?: number;
    current?: string;
    done?: number;
    total?: number;
  };
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type DepVuln = {
  id: string;
  cve: string | null;
  severity: string;
  summary: string;
  fixed_in: string | null;
  url: string;
};

export type DepFinding = {
  id: string;
  repo_slug: string;
  ecosystem: string;
  package: string;
  current_version: string;
  latest_version: string | null;
  outdated: "none" | "patch" | "minor" | "major";
  is_dev: boolean;
  vulns: DepVuln[];
  severity: "none" | "low" | "medium" | "high" | "critical";
  recommendation: "update_now" | "update_safe" | "plan_major" | "ok";
};

/** Fetch a file with credentials attached, then hand it to the browser.
 *
 *  Every export endpoint sits behind `get_current_user`, which reads the
 *  `Authorization` header and nothing else — no cookie fallback, and the Next
 *  proxy is page-routing only (its matcher excludes /api), so it injects
 *  nothing either. A plain `<a href download>` is a browser navigation: it
 *  sends cookies and no Authorization header, so it downloaded a file
 *  containing `{"detail":"Missing or invalid Authorization header"}` — under a
 *  button labelled "Download SBOM", which is the one artefact procurement asks
 *  for by name.
 *
 *  `requestHeaders`, not a hand-rolled bearer: it also carries the X-Workspace
 *  hint, without which the export resolves to the account's DEFAULT workspace
 *  and a member looking at another one silently gets the wrong audit.
 */
export async function downloadWithAuth(
  url: string, fallbackName: string, token?: string | null,
): Promise<void> {
  const resp = await fetch(url, { headers: requestHeaders(token) });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
    } catch { /* not JSON — show what came back */ }
    throw new Error(detail || `${resp.status} ${resp.statusText}`);
  }
  // The server names the file in Content-Disposition; honour it so the SBOM
  // arrives as celmis-sbom-<run>.cdx.json rather than the endpoint's path.
  const disposition = resp.headers.get("content-disposition") ?? "";
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = match ? decodeURIComponent(match[1]) : fallbackName;

  const blob = await resp.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  a.click();
  URL.revokeObjectURL(href);
}

/** Rewrite specific vault notes, optionally with a different engine.
 *
 *  A vault build is dozens of documents and many minutes, so improving one
 *  weak PRD had to mean running the whole thing again. */
export const docsApi = {
  /** Documentation for a SET of repositories.
   *
   *  `missing_only` is the condition that makes this worth asking for: without
   *  it the same request regenerates everything, which is hours of model time
   *  and almost never what was meant. */
  generateBulk: (token: string, body: {
    repo_slugs?: string[]; owner?: string; missing_only?: boolean;
    language?: string; engine?: string;
  }) =>
    api<{
      queued: { repo: string; job_id: string }[];
      skipped: { repo: string; reason: string }[];
      language: string;
      engine: string;
    }>("/api/docs/generate", { token, method: "POST", json: body }),
  /** Every repository's documentation in one archive. */
  exportAllUrl: (owner?: string) =>
    `${API_BASE}/api/docs/export-all${owner ? `?owner=${encodeURIComponent(owner)}` : ""}`,
  regenerate: (token: string, slug: string, body: {
    note_paths: string[]; engine?: string; language?: string;
  }) =>
    api<{
      ok: boolean;
      /** null when an identical job was already pending — see `queued`. */
      job_id: string | null;
      engine: string;
      /** false when the queue deduped it and nothing new was started. */
      queued?: boolean;
      detail: string;
    }>(
      `/api/docs/${encodeURIComponent(slug)}/regenerate`,
      { token, method: "POST", json: body },
    ),
};

export const depsApi = {
  /** The CycloneDX file itself — what procurement asks for by name. */
  sbomUrl: (runId: string, repo?: string) =>
    `${API_BASE}/api/deps/${runId}/sbom${repo ? `?repo=${encodeURIComponent(repo)}` : ""}`,
  /** The archive an audit is filed with: SBOMs, findings, timeline, hashes. */
  evidenceUrl: (runId: string) => `${API_BASE}/api/deps/${runId}/evidence`,
  delta: (token: string, runId: string) =>
    api<{
      first_run: boolean;
      headline: string;
      counts: { appeared: number; resolved: number; unchanged: number };
      appeared: { id: string; repo: string; package: string; severity: string }[];
      resolved: { id: string; repo: string; package: string; severity: string }[];
    }>(`/api/deps/${runId}/delta`, { token }),

  startAudit: (
    token: string,
    scope?: {
      repo_slugs?: string[];
      owner?: string;
      report_engine?: "none" | "api" | "claude_code";
      /** Close a run that stopped reporting progress and start a fresh one. */
      force?: boolean;
    },
  ) =>
    api<DepAuditRun>("/api/deps/audit", { token, method: "POST", json: scope ?? {} }),
  /** Stop a queued/running audit (cooperative — the worker exits at its next
   * checkpoint; the run flips to `error: "cancelled by user"` immediately). */
  cancel: (token: string, runId: string) =>
    api<DepAuditRun>(`/api/deps/${runId}/cancel`, { token, method: "POST" }),
  report: (token: string, runId: string, engine: "api" | "claude_code", temperature = 0.2) =>
    api<{ report: string; engine: string }>(`/api/deps/${runId}/report`, {
      token, method: "POST", json: { engine, temperature },
    }),
  latest: (token: string) =>
    api<DepAuditRun | null>("/api/deps/latest", { token }),
  findings: (token: string, runId: string, only?: "vulnerable" | "outdated") =>
    api<DepFinding[]>(
      `/api/deps/${runId}/findings${only ? `?only=${only}` : ""}`, { token }),
};

export const alertsApi = {
  list: (token: string) => api<IncomingAlert[]>("/api/alerts", { token }),
  patch: (token: string, id: string, patch: { status?: string; session_id?: string }) =>
    api<IncomingAlert>(`/api/alerts/${id}`, { token, method: "PATCH", json: patch }),
  ingestToken: (token: string) =>
    api<{ ingest_path: string } | null>("/api/alerts/ingest-token", { token }),
  createIngestToken: (token: string) =>
    api<{ ingest_path: string }>("/api/alerts/ingest-token", { token, method: "POST" }),
};
