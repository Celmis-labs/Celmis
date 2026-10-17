"""The map of the app's screens, as data: the one place that says where a setting lives.

The Celmis agent answers "how do I add custom prompts for repository X" by
OPENING the page with that repository selected and spotlighting the box to
fill, not by describing it. To do that it needs three things that used to be
scattered or missing:

  1. a stable name for every destination (a page, or one section of the
     review settings page) and the route that opens it;
  2. a stable name for every control on those pages that a person may be sent
     to, tied to the `data-tour="…"` attribute that marks it in `web/`;
  3. an accurate one-line description of what each control does, its default
     and what it inherits from. These are the product knowledge behind
     "what do the advanced settings give me" — the agent quotes them instead
     of guessing, and `knowledge/ui_map.py` renders them for the planner.

WHY ONE CATALOGUE. A model that is allowed to write a route or a selector
invents some, and an invented target starts a tour that highlights nothing,
which is worse than no tour. So the model only ever names a destination and a
list of focus keys from this file; the route, the query string, the anchors
and the opener to click are looked up here. `tests/automation/
test_the_catalogue_matches_the_screens.py` reads `web/` and fails when an
anchor is not in the source, when a review setting has no entry, or when an
entry says a different section than the page does — so moving a control means
moving its `data-tour` attribute and nothing else.

LAYERING. Every review setting resolves repository > workspace default >
installation > built-in (`src/review/review_defaults.py`); an empty value at a
layer means "inherit". The descriptions below say what the built-in value is,
because that is what a person gets when they do nothing.

Roles. Which role may CHANGE a destination is the same rule the actions use
(`PROMPT_EDITOR_ROLES`, `WORKSPACE_ADMIN_ROLES` in `src/users/roles.py`):
repository settings and prompts need an editor or above, workspace defaults an
admin or above. Everybody can still open a page and read it, so a person below
the bar gets the link and the reason instead of a tour that ends at a disabled
field.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

from src.users.roles import PACK_AUTHOR_ROLES, PROMPT_EDITOR_ROLES, WORKSPACE_ADMIN_ROLES

SETTINGS_ROUTE = "/review-settings"

#: Agents whose prompt can be customised (`GET /api/agents`).
PROMPT_AGENTS: tuple[str, ...] = (
    "defect", "contract", "security", "performance", "business_logic",
    "verifier",
)
#: Agents with a per-agent switch / model row.
SWITCH_AGENTS: tuple[str, ...] = (
    "defect", "contract", "security", "performance", "business_logic",
    "verifier", "compliance",
)

MAX_FOCUS = 6
MAX_STEP_TEXT = 220


@dataclass(frozen=True)
class Destination:
    """A place the agent can open.

    `section` is the review-settings section (the `section=` query value);
    `scoped` says the page edits a repository as well as the workspace, so a
    `repo` can be passed. `edit_repo` / `edit_workspace` are the roles that may
    change things there, at repository and at workspace scope.
    """

    id: str
    route: str
    title: str
    what: str
    anchor: str
    section: str | None = None
    scoped: bool = False
    repo_only: bool = False
    edit_repo: frozenset[str] = PROMPT_EDITOR_ROLES
    edit_workspace: frozenset[str] = WORKSPACE_ADMIN_ROLES


@dataclass(frozen=True)
class UiField:
    """One control a tour may point at.

    `anchor` is the `data-tour` id; `{agent}` marks a per-agent control, which
    the model names as `key:agent`. `opener` is the id of the control to press
    when this one is folded away. `advanced` marks options that most teams
    never touch and that the knowledge answer should say so about.
    """

    key: str
    destination: str
    anchor: str
    what: str
    advanced: bool = False
    opener: str | None = None
    #: Settings with a value of their own carry the built-in one here.
    default: str = ""

    @property
    def per_agent(self) -> bool:
        return "{agent}" in self.anchor


def _sections() -> tuple[Destination, ...]:
    def s(section: str, title: str, what: str, **kw: Any) -> Destination:
        return Destination(
            id=f"review_settings.{section}", route=SETTINGS_ROUTE,
            section=section, title=title, what=what, scoped=True,
            anchor=f"settings-section:{section}", **kw)

    return (
        s("general", "Review settings > General",
          "When a pull request is reviewed and what the review does on it: "
          "target branches, drafts, cadence, approve/request changes, "
          "language, issue auto-resolve."),
        s("categories", "Review settings > Review categories",
          "Which review agents take part, the verifier, and each agent's "
          "model, reasoning level and output limit."),
        s("filters", "Review settings > Filters",
          "Which findings become inline comments: minimum severity, comment "
          "cap, ignored files (globs) and silenced rules."),
        s("prompts", "Review settings > Custom prompts",
          "What every agent is told (base instruction) and, per agent, "
          "guidelines added to its prompt or a full replacement prompt.",
          edit_workspace=PROMPT_EDITOR_ROLES),
        s("summary", "Review settings > Summary",
          "The pull request overview a review writes: whether, where, what "
          "it covers and what a later push does to it."),
        s("rules", "Review settings > Rules",
          "How many review rules apply here and where to manage them."),
        s("messages", "Review settings > Messages",
          "The wording of the comments a review posts on its own behalf."),
        s("commands", "Review settings > Commands",
          "Whether the bot answers commands and questions written in a "
          "pull request comment, and who may use them."),
        s("learning", "Review settings > Learning",
          "Team memories given to the reviewers, their approval, and how "
          "dismissed findings are learned from."),
        s("advanced", "Review settings > Advanced",
          "Per repository only: external evidence sources (MCP) and legacy "
          "folder rules.", repo_only=True),
    )


DESTINATIONS: dict[str, Destination] = {d.id: d for d in (
    *_sections(),
    Destination("llm", "/settings/llm", "Settings > LLM keys & models",
                "Provider API keys, the base PR Review model and reasoning, "
                "per-agent models, embeddings, Chat and the agent itself.",
                anchor="llm-review-card"),
    Destination("connections", "/connections", "Settings > Git connections",
                "One token per git provider (GitHub, GitLab, Bitbucket) and "
                "per tracker, saved once for the workspace.",
                anchor="page-connections"),
    Destination("memories", "/memories", "Code review > Memories",
                "Facts and conventions the team taught the reviewers (\"we "
                "never use library X\", \"our API returns 404 for a missing "
                "id\"), per repository or directory; add, approve pending, "
                "disable. Use for something to REMEMBER, not for how to "
                "write reviews (that is the prompts).",
                anchor="page-memories", edit_workspace=PROMPT_EDITOR_ROLES),
    Destination("codebase", "/codebase", "Codebase > Packs",
                "Knowledge packs for coding agents: skills, API examples, "
                "checklists and notes the workspace curated, each shown to "
                "everybody, to chosen teams or to named people. Create a "
                "pack and upload a folder or a zip, share it, roll back a "
                "version. Use for something an AGENT should follow or read "
                "while it codes, not for a fact the reviewers should know "
                "(that is a memory).",
                anchor="page-codebase", edit_workspace=PACK_AUTHOR_ROLES),
    Destination("review_rules", "/admin/review-rules",
                "Code review > Review rules",
                "The rules library: add a rule, generate from the code, "
                "import, approve proposals.",
                anchor="page-review-rules", edit_workspace=PROMPT_EDITOR_ROLES),
    Destination("alerts", "/alerts", "Monitoring > Alerts",
                "The webhook URL that turns a monitoring alert (Grafana or "
                "any sender) into a Celmis alert.", anchor="page-alerts"),
    Destination("notifications", "/admin/notifications",
                "Monitoring > Notifications",
                "Channels (Slack, Discord, Google Chat, webhook) and the "
                "bindings that say which events go to which channel.",
                anchor="page-notifications"),
)}


def _f(key: str, dest: str, what: str, *, anchor: str | None = None,
       default: str = "", advanced: bool = False,
       opener: str | None = None) -> UiField:
    return UiField(key=key, destination=dest, anchor=anchor or f"setting:{key}",
                   what=what, default=default, advanced=advanced, opener=opener)


_G, _C, _F, _P = ("review_settings.general", "review_settings.categories",
                  "review_settings.filters", "review_settings.prompts")
_S, _M, _CM, _L = ("review_settings.summary", "review_settings.messages",
                   "review_settings.commands", "review_settings.learning")

FIELDS: tuple[UiField, ...] = (
    # ── General ───────────────────────────────────────────────────────
    _f("target_branches", _G, "Branches whose pull requests are reviewed "
       "(patterns allowed). Empty reviews pull requests into every branch.",
       default="every branch"),
    _f("run_on_drafts", _G, "Review draft pull requests too. Off waits until "
       "the pull request is marked ready.", default="off"),
    _f("review_cadence", _G, "When a review starts by itself: automatic (every "
       "push), auto_pause (stop after several quick pushes until asked) or "
       "manual (only on command).", default="automatic"),
    _f("auto_pause_pushes", _G, "With cadence auto_pause: how many pushes "
       "inside the window pause automatic review.", default="3"),
    _f("auto_pause_window_minutes", _G, "With cadence auto_pause: the window, "
       "in minutes, those pushes are counted in.", default="15"),
    _f("ignored_title_keywords", _G, "Pull requests whose title contains one "
       "of these words are skipped (e.g. WIP).", default="none"),
    _f("review_scope", _G, "What a review reads: incremental (a push is "
       "reviewed from the last reviewed commit) or full (the whole pull "
       "request every time).", default="incremental"),
    _f("approve_when_clean", _G, "Submit an approving review when no finding "
       "reaches the comment threshold.", default="off"),
    _f("request_changes_on_critical", _G, "Submit a blocking review when a "
       "critical finding is posted.", default="off"),
    _f("status_feedback", _G, "Show the review as a status check on the head "
       "commit (running, passed, failed, skipped).", default="on"),
    _f("committable_suggestions", _G, "Post fixes as suggestion blocks the "
       "author applies in one click, where the provider supports it.",
       default="off"),
    _f("started_comment_enabled", _G, "Post a short comment when the review "
       "begins and edit it into the result when it ends.", default="on"),
    _f("review_language", _G, "The language comments and the summary are "
       "written in; code and identifiers stay as they are.",
       default="English"),
    _f("issues_auto_resolve", _G, "Close an issue when the target branch no "
       "longer has the code it was raised on.", default="on"),
    _f("issues_resolve_llm_verify", _G, "When the flagged line is gone, ask "
       "the model whether the problem is really fixed.", default="on",
       advanced=True),
    _f("issues_resolve_max_llm", _G, "The most model calls one recheck of a "
       "branch may use (0 to 50; 0 keeps only checks that need no model).",
       default="8", advanced=True),
    _f("issues_announce_resolved", _G, "List earlier issues found fixed in "
       "the summary comment.", default="on"),
    # ── Review categories ─────────────────────────────────────────────
    _f("disabled_agents", _C, "Switch an agent off (its switch on the "
       "Review categories card). A name here beats an enable.",
       anchor="review-agent:{agent}", default="none off"),
    _f("enabled_agents", _C, "Switch an opt-in agent (business logic, "
       "compliance) on; opt-in agents run only when named here.",
       anchor="review-agent:{agent}", default="none"),
    _f("verifier_enabled", _C, "The verifier re-reads each finding against "
       "the code and drops the ones it cannot confirm: fewer false "
       "positives, one more model call.", default="off"),
    _f("task_context_enabled", _C, "Read the linked tracker task so the "
       "review knows the intent of the change.", default="on"),
    _f("business_logic_auto", _C, "Run the business-logic agent by itself: "
       "off, or when_task_found (a linked task exists).", default="off"),
    _f("requirements_check_mode", _C, "Check the change against the task's "
       "requirements: off, findings (gaps become findings) or checklist "
       "(a pass/fail list in the summary).", default="checklist"),
    _f("task_urls_enabled", _C, "Also follow task links written in the pull "
       "request description.", default="off", advanced=True),
    _f("task_project_keys", _C, "Tracker project keys (e.g. SHOP) the task "
       "finder may match; empty accepts any.", default="any", advanced=True),
    _f("task_acceptance_field", _C, "The tracker custom field "
       "(customfield_NNNNN) that holds acceptance criteria.",
       default="description only", advanced=True),
    _f("task_include_comments", _C, "How many of the task's latest comments "
       "the reviewers also read.", default="0", advanced=True),
    # ── Filters ───────────────────────────────────────────────────────
    _f("comment_min_severity", _F, "The severity threshold: findings below "
       "it are not posted. Steps: info, warning, error, critical.",
       default="info (everything)"),
    _f("max_inline_comments", _F, "At most this many inline comments per "
       "review; the most severe are kept.", default="20"),
    _f("apply_filters_to_rules", _F, "Apply the threshold and the cap to rule "
       "violations as well. Off posts every violation of your rules.",
       default="on"),
    _f("ignore_globs", _F, "Files matching these globs are never read or "
       "sent to a model (e.g. dist/**, *.lock). One per line.",
       default="none"),
    _f("suppressed_rules", _F, "Rule ids whose findings are dropped before "
       "posting, e.g. quality.todo.", default="none"),
    # ── Prompts ───────────────────────────────────────────────────────
    _f("base_instruction", _P, "An instruction every agent is given on top "
       "of its own prompt (tone, house conventions). Up to 2,000 "
       "characters; empty inherits.", default="none"),
    UiField("repo_instructions", _P, "prompt-repo-instructions",
            "Repository only: free instructions for this repository's "
            "reviews, kept apart from the per-agent guidelines (up to 20,000 "
            "characters).", advanced=True),
    UiField("prompt_agent", _P, "prompt-agent:{agent}",
            "One agent's prompt card: its guidelines, the extend switch, "
            "the replace box and the preview."),
    UiField("prompt_guidelines", _P, "prompt-guidelines:{agent}",
            "THE place for custom prompts: text ADDED to this agent's "
            "built-in prompt in a 'Team guidelines' block (up to 2,000 "
            "characters). It refines focus and wording; it cannot change "
            "scope, severity or output rules. Write only what the built-in "
            "prompt does not already cover (shown above the box)."),
    UiField("prompt_extend", _P, "prompt-extend:{agent}",
            "Repository only, 'Also keep the workspace guidelines': this "
            "repository's guidelines are added to the workspace's instead of "
            "replacing them.", advanced=True),
    UiField("prompt_replace", _P, "prompt-replace:{agent}",
            "Advanced: replace the agent's whole built-in system prompt "
            "(folded, with a warning). The built-in severity calibration and "
            "scope rules are lost; guidelines are still appended. Most teams "
            "should use guidelines.", advanced=True),
    UiField("prompt_preview", _P, "prompt-preview:{agent}",
            "Preview the exact prompt this agent will get: built-in, "
            "workspace and repository parts, each labelled."),
    # ── Summary ───────────────────────────────────────────────────────
    _f("summary_enabled", _S, "Write the pull request overview at all.",
       default="on"),
    _f("summary_target", _S, "Where the summary goes: a comment, or the pull "
       "request description.", default="comment"),
    _f("summary_on_new_commits", _S, "What a later review does with the "
       "summary already posted: replace, append or nothing.",
       default="replace"),
    _f("summary_existing_description", _S, "With target description: how the "
       "summary meets a description that is already there (append, "
       "complement, replace).", default="append", advanced=True),
    _f("summary_instructions", _S, "What to put in the summary and how "
       "(a risk section, a QA checklist). Markdown.", default="none"),
    _f("completed_comment", _S, "Layout of the closing comment: completed or "
       "classic.", default="completed", advanced=True),
    _f("commands_guide_enabled", _S, "Add a short guide to the bot's commands "
       "to the closing comment.", default="on"),
    # ── Messages ──────────────────────────────────────────────────────
    _f("message_started", _M, "Text of the comment posted when a review "
       "begins. Placeholders: {commit}, {agents}, {files}, {pr_number}.",
       default="built-in text"),
    _f("message_finished_header", _M, "The first lines of the comment with "
       "the result. Same placeholders.", default="built-in text"),
    # ── Commands ──────────────────────────────────────────────────────
    _f("commands_enabled", _CM, "React to start-review, review, help and the "
       "other commands written in a pull request comment.", default="on"),
    _f("chat_enabled", _CM, "Answer a comment that addresses the bot with a "
       "question about the change.", default="on"),
    _f("command_permission", _CM, "Who may command the bot: repo_access "
       "(anyone who can comment on a private repository), participants "
       "(author and reviewers) or anyone.", default="repo_access"),
    # ── Learning ──────────────────────────────────────────────────────
    _f("memories_enabled", _L, "Tell the reviewers the team's active "
       "memories. Off: none reaches a prompt; existing ones are kept.",
       default="on"),
    _f("knowledge_approval", _L, "A new memory from someone without standing "
       "waits for an editor to approve it. Off: every memory is active at "
       "once.", default="on"),
    _f("memory_trusted_commenters", _L, "Logins or e-mails whose memories are "
       "active without approval, one per line.", default="none",
       advanced=True),
    _f("learning_suppression", _L, "Leave out a finding that enough people "
       "already dismissed here: shadow (record only), off or on. Critical "
       "and rule-proven findings are never hidden.", default="shadow",
       advanced=True),
    _f("learning_excluded_reviewers", _L, "Logins or e-mails whose verdicts "
       "and reactions are not learned from (bots, test accounts).",
       default="none", advanced=True),
    # ── Advanced (repository only) ────────────────────────────────────
    UiField("mcp_sources", "review_settings.advanced", "advanced-mcp",
            "External evidence sources over MCP (name, URL, auth, "
            "credentials key, trigger patterns, allowed tools) the reviewers "
            "may query for proof.", advanced=True),
    UiField("folder_rules", "review_settings.advanced", "advanced-folder-rules",
            "Legacy per-folder rules, kept for old setups; migrate them to "
            "the rules library.", advanced=True),
    UiField("rules_overview", "review_settings.rules", "rules-overview",
            "How many rules apply here (workspace and repository) and the "
            "link to the library."),
    # ── Per-agent model controls (Review categories) ──────────────────
    UiField("agent_model_toggle", _C, "review-agent-toggle:{agent}",
            'The "Model & limits" button of one agent: unfolds its model '
            "row."),
    UiField("agent_model", _C, "review-agent-model:{agent}",
            "The model this agent runs on; empty inherits the PR Review "
            "base model.", opener="review-agent-toggle:{agent}"),
    UiField("agent_reasoning", _C, "review-agent-reasoning:{agent}",
            "How hard this agent thinks (reasoning level); empty inherits.",
            opener="review-agent-toggle:{agent}"),
    UiField("agent_test", _C, "review-agent-test:{agent}",
            'The "Test" button of one agent: runs a tiny call against the '
            "chosen model to prove it answers.",
            opener="review-agent-toggle:{agent}"),
    UiField("agent_max_output", _C, "review-agent-max-output:{agent}",
            "The output token cap of this agent; empty inherits.",
            opener="review-agent-toggle:{agent}", advanced=True),
    # ── LLM settings page ─────────────────────────────────────────────
    UiField("llm_provider_keys", "llm", "llm-provider-keys",
            "API keys for each LLM provider, saved encrypted per workspace."),
    UiField("llm_review_model", "llm", "llm-review-card",
            "The PR Review base card: provider, model and fallback every "
            "agent inherits."),
    UiField("llm_base_reasoning", "llm", "llm-base-reasoning",
            "The base reasoning level of the PR Review card."),
    UiField("llm_base_test", "llm", "llm-base-test",
            'The "Test" button of the PR Review base card.'),
    UiField("llm_apply_all", "llm", "llm-apply-all",
            'The "Apply to all agents" button under the base reasoning '
            "level."),
    UiField("llm_review_agents", "llm", "llm-review-agents",
            "Every review agent with its own model and limits."),
    UiField("llm_embeddings", "llm", "llm-embeddings-card",
            "The embeddings model that indexes code for search."),
    UiField("llm_chat", "llm", "llm-chat-card",
            "The model behind Ask the code."),
    UiField("llm_agent", "llm", "llm-agent-card",
            "The model behind this Celmis agent."),
    # ── Other pages ───────────────────────────────────────────────────
    UiField("connection_provider", "connections", "connection-provider:{agent}",
            "The card of one git provider (github, gitlab or bitbucket): "
            "paste the token here."),
    UiField("connection_scopes", "connections", "connection-scopes:{agent}",
            "The list of permissions (scopes) the token of one git provider "
            "needs, with the ones a Verify found missing highlighted."),
    UiField("memories_add", "memories", "memories-add",
            "The Add memory button: a short fact about how the team works."),
    UiField("codebase_new_pack", "codebase", "codebase-new-pack",
            "The New pack button: name a knowledge pack for coding agents "
            "and choose who sees it."),
    UiField("review_rules_list", "review_rules", "page-review-rules",
            "The review rules library."),
    UiField("alerts_webhook", "alerts", "alerts-webhook",
            "The ingest webhook URL (and its secret) to paste into Grafana "
            "or any sender."),
    UiField("notifications_channel", "notifications", "notifications-channel",
            "Create a channel: name, kind (Slack, Discord, Google Chat, "
            "webhook) and its webhook URL."),
    UiField("notifications_binding", "notifications", "notifications-binding",
            "Bind a channel to events so the right people hear about them."),
)

FIELDS_BY_KEY: dict[str, UiField] = {f.key: f for f in FIELDS}

#: Review settings that are the same control as another entry: the agent
#: switch list covers `enabled_agents` and `disabled_agents` alike.
SHARED_ANCHOR_KEYS = frozenset({"disabled_agents", "enabled_agents"})

#: Names the model tends to use for a field, in the words people type, mapped
#: to the key. Kept small: the key itself is the primary vocabulary.
FOCUS_ALIASES: dict[str, str] = {
    "prompts": "prompt_guidelines", "custom_prompt": "prompt_guidelines",
    "guidelines": "prompt_guidelines", "agent_guidelines": "prompt_guidelines",
    "replace_prompt": "prompt_replace", "system_prompt": "prompt_replace",
    "severity": "comment_min_severity", "ignore": "ignore_globs",
    "ignore_files": "ignore_globs", "verifier": "verifier_enabled",
    "model": "agent_model", "reasoning": "agent_reasoning",
}


def known_anchors() -> set[str]:
    """Every `data-tour` base id (before any `:`) the catalogue relies on."""
    out = {d.anchor.split(":")[0] for d in DESTINATIONS.values()}
    for f in FIELDS:
        out.add(f.anchor.split(":")[0])
        if f.opener:
            out.add(f.opener.split(":")[0])
    return out


# ── routes ───────────────────────────────────────────────────────────────

def route_for(dest: Destination, repo: str | None = None,
              agent: str | None = None) -> str:
    """The in-app path that opens `dest` (with the repository selected).

    The review settings page reads `?repo=<slug>&section=<id>&agent=<name>`;
    General is the default section so it is left out, as the page's own
    links do (`settingsHref` in web/lib/review-settings-routes.ts).
    """
    q: dict[str, str] = {}
    if dest.scoped and repo:
        q["repo"] = repo
    if dest.section and dest.section != "general":
        q["section"] = dest.section
    if dest.section and agent and dest.section in ("prompts", "categories"):
        q["agent"] = agent
    return f"{dest.route}?{urlencode(q)}" if q else dest.route


def required_roles(dest: Destination, repo: str | None) -> frozenset[str]:
    return dest.edit_repo if (dest.scoped and repo) else dest.edit_workspace


# ── focus keys ───────────────────────────────────────────────────────────

def _norm_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9_:]+", "_", raw.strip().lower()).strip("_")


def resolve_focus(dest: Destination, raw: object, *,
                  default_agent: str | None = None,
                  repo_scope: bool = False) -> dict[str, str] | None:
    """`{"target","opener"?,"what"}` for a focus key of `dest`, else None.

    A focus key is a `UiField.key` of the destination, written `key` or
    `key:agent` for per-agent controls. Anything else is dropped — the same
    rule as `tour.resolve_target`.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    base, sep, agent = _norm_key(raw).partition(":")
    base = FOCUS_ALIASES.get(base, base)
    spec = FIELDS_BY_KEY.get(base)
    if spec is None or spec.destination != dest.id:
        return None
    if spec.per_agent:
        agents = (SWITCH_AGENTS if spec.key in SHARED_ANCHOR_KEYS
                  or spec.key.startswith("agent_") else
                  ("github", "gitlab", "bitbucket")
                  if spec.key == "connection_provider" else PROMPT_AGENTS)
        # A bare per-agent key ("prompt_guidelines") points at the agent an
        # earlier focus named, else at the first card: the page lists every
        # agent in the same order, so the person lands on the first one and
        # the sentence says the rest work the same way.
        agent = agent or (default_agent if default_agent in agents else "") or agents[0]
        if agent not in agents:
            return None
    elif sep and agent:
        return None
    else:
        agent = ""
    if spec.key in ("repo_instructions", "prompt_extend") and not repo_scope:
        return None
    fmt = {"agent": agent}
    out = {"target": spec.anchor.format(**fmt), "what": spec.what,
           "key": spec.key}
    if spec.opener:
        out["opener"] = spec.opener.format(**fmt)
    return out


# ── destination and repository matching ─────────────────────────────────

def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", text).strip()


def resolve_destination(raw: object) -> Destination | None:
    """A destination by id.

    A bare section name ("prompts") works, and so do the near-misses a small
    model produces for an id it was shown a moment ago ("review_filters",
    "settings.prompts", "review-settings/prompts"): each is the same screen,
    and refusing it would turn a correct choice into an error.
    """
    if not isinstance(raw, str):
        return None
    key = re.sub(r"[-/\s]+", "_", raw.strip().lower()).replace("_settings_", "_settings.")
    for cand in (key, key.removeprefix("review_"), key.removeprefix("settings."),
                 key.removeprefix("review_settings_")):
        if cand in DESTINATIONS:
            return DESTINATIONS[cand]
        if f"review_settings.{cand}" in DESTINATIONS:
            return DESTINATIONS[f"review_settings.{cand}"]
    return None


@dataclass(frozen=True)
class RepoMatch:
    status: str  # "found" | "ambiguous" | "none"
    slug: str = ""
    candidates: tuple[str, ...] = ()


def match_repo(query: object, repos: list[dict[str, Any]]) -> RepoMatch:
    """Resolve the repository a person named against the ones they may read.

    `repos` is `list_repos(...)["repos"]`: dicts with `repo` (the slug the
    settings page takes) and `full_name`. Order of trust: an exact slug or
    full name, then the bare name after the owner, then a unique substring
    or near-spelling. Two repositories that are equally close are NOT
    guessed between — the caller asks, because opening the wrong
    repository's settings is a quiet mistake.
    """
    q = _fold(str(query or ""))
    if not q or not repos:
        return RepoMatch("none")
    rows = [(r.get("repo", ""), _fold(r.get("repo", "")),
             _fold(r.get("full_name", "") or r.get("repo", "")))
            for r in repos if r.get("repo")]
    for slug, s, full in rows:
        if q in (s, full):
            return RepoMatch("found", slug)
    bare = [(slug, full.split(" ")[-1]) for slug, _s, full in rows]
    hits = [slug for slug, name in bare if q.replace(" ", "") == name.replace(" ", "")]
    if len(hits) == 1:
        return RepoMatch("found", hits[0])
    if len(hits) > 1:
        return RepoMatch("ambiguous", candidates=tuple(sorted(hits))[:5])
    subs = [slug for slug, s, full in rows if q in s or q in full]
    if len(subs) == 1:
        return RepoMatch("found", subs[0])
    if len(subs) > 1:
        return RepoMatch("ambiguous", candidates=tuple(sorted(subs))[:5])
    scored = sorted(
        ((max(SequenceMatcher(None, q, s).ratio(),
              SequenceMatcher(None, q, full.split(" ")[-1]).ratio()), slug)
         for slug, s, full in rows), reverse=True)
    best = scored[0]
    if best[0] < 0.72:
        return RepoMatch("none")
    close = [slug for score, slug in scored if score >= best[0] - 0.05]
    if len(close) > 1:
        return RepoMatch("ambiguous", candidates=tuple(sorted(close))[:5])
    return RepoMatch("found", best[1])


# ── the client action ───────────────────────────────────────────────────

def build_ui_action(
    *, destination: object, repo: object = None, focus: object = None,
    repos: list[dict[str, Any]] | None = None,
    caller: dict[str, Any] | None = None, workspace_id: str = "",
    language: str = "",
) -> dict[str, Any]:
    """What `show_in_ui` returns: a validated client action, or a refusal.

    Always a dict with `ok`. On success: `route`, `steps` (each
    `{target, text, route[, opener]}`, the shape the browser's tour already
    takes; `language` is the question's, and a model-written text in another
    script falls back to the catalogue's), `title`, `repo`, `workspace_id` and `mode`: "tour" when the
    caller may change what is shown, "link" when their role cannot, with
    `needs` naming the roles. On failure: `error` (one of unknown_destination,
    repo_required, repo_not_found, repo_ambiguous, repo_not_applicable) and,
    for an ambiguous repository, `candidates`, so the agent can ask which.
    """
    dest = resolve_destination(destination)
    if dest is None:
        return {"ok": False, "error": "unknown_destination",
                "known": sorted(DESTINATIONS)}
    slug = ""
    wanted = str(repo).strip() if isinstance(repo, str) else ""
    if wanted and not dest.scoped:
        wanted = ""  # a page that is not per repository: the name is moot
    if wanted:
        m = match_repo(wanted, repos or [])
        if m.status == "ambiguous":
            return {"ok": False, "error": "repo_ambiguous",
                    "candidates": list(m.candidates)}
        if m.status == "none":
            return {"ok": False, "error": "repo_not_found", "asked": wanted}
        slug = m.slug
    elif dest.repo_only:
        return {"ok": False, "error": "repo_required"}

    items = focus if isinstance(focus, list) else []
    agent_hint = None
    steps: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        key, text = (item.get("field"), item.get("text")) if isinstance(
            item, dict) else (item, "")
        r = resolve_focus(dest, key, default_agent=agent_hint,
                          repo_scope=bool(slug))
        if r is None or r["target"] in seen:
            continue
        seen.add(r["target"])
        if FIELDS_BY_KEY[r["key"]].per_agent:
            agent_hint = agent_hint or r["target"].partition(":")[2]
        # A popover the model wrote in the wrong language (Latin for a
        # Ukrainian question, Russian for a Ukrainian one) is replaced by the
        # catalogue's own sentence, which is English: one language the
        # person can at least be expected to find, never a wrong guess.
        written = _tidy(text)
        if written and not text_in_language(written, language):
            written = ""
        step = {"target": r["target"],
                "text": written or _tidy(r["what"])}
        if "opener" in r:
            step["opener"] = r["opener"]
        steps.append(step)
        if len(steps) == MAX_FOCUS:
            break
    if not steps:
        steps.append({"target": dest.anchor, "text": _tidy(dest.what)})
    route = route_for(dest, slug or None, agent_hint)
    for s in steps:
        s["route"] = route

    action: dict[str, Any] = {
        "ok": True, "destination": dest.id, "title": dest.title,
        "repo": slug, "workspace_id": workspace_id, "route": route,
        "steps": steps, "mode": "tour",
    }
    needs = required_roles(dest, slug or None)
    role = (caller or {}).get("role")
    if (caller and not caller.get("is_admin") and not caller.get("is_superadmin")
            and role not in needs):
        action["mode"] = "link"
        action["needs"] = sorted(needs)
        action["role"] = role or ""
    return action


def _tidy(text: object) -> str:
    out = re.sub(r"\s+", " ", re.sub(r"[`*_<>\[\]]", "", str(text or ""))).strip()
    return out[:MAX_STEP_TEXT]


# ── what the planner is told ────────────────────────────────────────────

def destination_menu() -> str:
    """One line per destination with the focus keys it accepts.

    The planner's whole vocabulary for `show_in_ui`: it can only name what is
    printed here, and `build_ui_action` drops anything else. Per-agent keys
    are printed as `key:<agent>`.
    """
    lines = []
    for d in DESTINATIONS.values():
        keys = [f.key + (":<agent>" if f.per_agent else "")
                for f in FIELDS if f.destination == d.id]
        scope = ("repo only" if d.repo_only else
                 "takes a repo" if d.scoped else "workspace-wide")
        lines.append(f"  {d.id} ({scope}) — {d.what}"
                     + (f" Focus keys: {', '.join(keys)}." if keys else ""))
    return "\n".join(lines)


def spec() -> dict[str, Any]:
    """The `CATALOGUE` entry of the `show_in_ui` verb (see chat.py)."""
    return {
        "summary": (
            "OPEN the page and spotlight the exact fields in the app, with "
            "the right repository selected. Choose this whenever the person "
            "wants to SET UP, CHANGE or FIND something — custom prompts, "
            "base instruction, agent guidelines, ignore files, severity "
            "threshold, verifier, review rules, memories, notifications, "
            "alerts, LLM model or reasoning, git connections — in any "
            "language. Combine with `help` when they also ask what an "
            "option does."),
        "reads": True,
        "arguments": {
            "destination": "one of the destination ids listed under 'UI "
                           "destinations' in the instructions",
            "repo": "the repository EXACTLY as the person wrote it (a name "
                    "or part of one, never invented); omit when none was "
                    "named and it is not needed",
            "focus": "list of {\"field\": <focus key of that destination>, "
                     "\"text\": <one short sentence, in the person's "
                     "language, saying what to write or choose there>}, in "
                     "the order to fill them, at most 5",
        },
    }


def routing_prompt() -> str:
    """The rules and examples that make the planner prefer `show_in_ui`."""
    return f"""
SETUP AND WHERE-IS QUESTIONS — the show_in_ui action.

When a person wants to configure, change, add or find something in the app,
SHOW them instead of describing: return a `show_in_ui` step. The browser opens
the page with the repository selected and spotlights the fields, one popover
per field. The note stays ONE short sentence ("Opening the prompts of acme/shop
— write your rules in the highlighted box.") in the person's language; do not
repeat the steps in text.

Rules:
- Pick the destination from the list below and only focus keys listed for it.
  Never write a route, a selector or a key that is not listed.
- Put the fields in the order to fill them. Each `text` says WHAT to write or
  choose there, under 120 characters, in the language of the REQUEST (a text
  in another language is discarded and the English description shown).
- "Custom prompts" / "my own rules for the reviewer" / "base instruction" /
  "guidelines for the security agent" → review_settings.prompts with
  base_instruction first, then prompt_guidelines:<agent> for the agent named
  (when none is named, prompt_guidelines alone). "Replace the whole prompt" →
  prompt_replace:<agent>. Ignore files → review_settings.filters, ignore_globs.
  Severity threshold → comment_min_severity. Verifier → review_settings.
  categories, verifier_enabled. Model or reasoning of ONE agent →
  review_settings.categories with agent_model:<agent> /
  agent_reasoning:<agent>; the base model for all → llm.
- `repo`: the repository the person named, as they wrote it. Several
  repositories, or "all", or a workspace-wide setting → omit `repo` (the page
  then edits the workspace defaults every repository inherits). If they
  clearly mean "this repo" and earlier turns name it, use that name. If a
  repository is needed (Advanced section) and none was named, return no steps
  and ask which one.
- If the person ALSO asks what something does or how options differ, add a
  `help` step after `show_in_ui` and answer in full in the note (from the
  product knowledge below); the spotlight then illustrates the answer.
- A fact or convention for the reviewers to remember ("we never use moment.js",
  "this service returns 404 on purpose") → memories, focus memories_add;
  instructions about HOW reviews are written or what to look for → prompts.
- "How / where do I add, set up, change, connect, turn on or off X" and "I
  need / I want X" are show_in_ui even when they feel like questions: the
  answer is to take the person there. Slack/Discord/Google Chat channels →
  notifications; GitHub, GitLab or Bitbucket tokens → connections
  (connection_provider:<provider>); an API key of an LLM provider → llm
  (llm_provider_keys); a team memory → memories (memories_add).
- An explicit order that gives the value ("set the severity threshold to
  warning for acme/web") stays update_review_setting, which is shown for
  approval; if you doubt, prefer show_in_ui — it changes nothing.
- A pure question with no screen to open (what is Celmis, how does layering
  work) is `help` or `explain`, not `show_in_ui`.
- Whether the person may change the setting is checked for you; if they
  cannot, they are given the link and the reason. Do not refuse in advance,
  whatever their role line says.
- Matching the repository name against the real list is done for you after
  you answer (typos, bare names, "owner/name"). Never ask "which repository"
  when the person wrote any name; copy it into `repo`. Ask only when none was
  given and the page needs one.

Examples (language follows the request):
Request: "I need to add custom prompts for repo shop-api"
{{"language": "en", "note": "Opening the prompts of shop-api. Write your team rules in the highlighted boxes.", "steps": [{{"action": "show_in_ui", "arguments": {{"destination": "review_settings.prompts", "repo": "shop-api", "focus": [{{"field": "base_instruction", "text": "Rules every agent should follow in this repository."}}, {{"field": "prompt_guidelines:defect", "text": "Extra guidelines for the bug-finding agent; each agent has its own box."}}]}}}}]}}
Request: "де вимкнути перевірку для lock-файлів в acme/web?"
{{"language": "uk", "note": "Відкриваю фільтри acme/web: додайте шаблон у підсвічене поле.", "steps": [{{"action": "show_in_ui", "arguments": {{"destination": "review_settings.filters", "repo": "acme/web", "focus": [{{"field": "ignore_globs", "text": "Додайте по шаблону в рядок, напр. *.lock або dist/**."}}]}}}}]}}
Request: "add a Slack channel for notifications"
{{"language": "en", "note": "Opening Notifications. Fill in the channel here, then bind it to events.", "steps": [{{"action": "show_in_ui", "arguments": {{"destination": "notifications", "focus": [{{"field": "notifications_channel", "text": "Name, kind (Slack) and the incoming-webhook URL."}}, {{"field": "notifications_binding", "text": "Then choose which events go to this channel."}}]}}}}]}}
Request: "як підключити GitHub токен?"
{{"language": "uk", "note": "Відкриваю підключення git: вставте токен у картку GitHub.", "steps": [{{"action": "show_in_ui", "arguments": {{"destination": "connections", "focus": [{{"field": "connection_provider:github", "text": "Вставте сюди fine-grained токен із правами Contents та Pull requests (read/write)."}}]}}}}]}}
Request: "how do I turn on the verifier for billing?"
{{"language": "en", "note": "Opening the review categories of billing; switch the verifier on.", "steps": [{{"action": "show_in_ui", "arguments": {{"destination": "review_settings.categories", "repo": "billing", "focus": [{{"field": "verifier_enabled", "text": "Turn this on to re-check every finding against the code."}}]}}}}]}}
Request: "where do I add a memory for the team?" / "where do I put my OpenAI API key?"
→ show_in_ui with destination memories (focus memories_add) / llm (focus llm_provider_keys).
Request: "what do the advanced settings give when configuring prompts?"
{{"language": "en", "note": "<the full answer from the product knowledge: guidelines vs replace, extend switch, preview, layering repository > workspace > built-in>", "steps": [{{"action": "help", "arguments": {{}}}}, {{"action": "show_in_ui", "arguments": {{"destination": "review_settings.prompts", "focus": [{{"field": "prompt_replace:defect", "text": "Advanced: replaces the whole built-in prompt."}}]}}}}]}}

UI destinations:
{destination_menu()}

"""


# ── the safety net for a routing miss ───────────────────────────────────

_ASK_WHERE = re.compile(
    r"(куди|\bде\b|як (?:мені )?(?:додати|налаштувати|вказати|вставити|"
    r"змінити|підключити|задати|прописати)|хочу (?:додати|налаштувати|"
    r"вказати)|потрібно (?:додати|налаштувати)|треба (?:додати|налаштувати)|"
    r"\bгде\b|куда|как (?:мне )?(?:добавить|настроить|указать|вставить|"
    r"изменить|подключить|задать|прописать)|хочу (?:добавить|настроить|"
    r"указать)|нужно (?:добавить|настроить)|надо (?:добавить|настроить)|"
    r"\bwhere\b|\bhow (?:do|can|should|to)\b.*\b(?:add|set|configure|change|"
    r"enter|paste|put|connect|define|ignore|turn)|\b(?:i|we) (?:need|want|"
    r"would like) to (?:add|set|configure|change|connect|enter|ignore)|"
    r"\bneed to (?:add|set|configure|connect)|show me where)", re.I)

#: A question that starts as a request for an explanation belongs to `help`
#: however many setting names it mentions.
_ASK_EXPLAIN = re.compile(
    r"^\W*(?:що таке|що це|що означає|що робить|чому|навіщо|поясни|розкажи|"
    r"що за|что такое|что это|что значит|что делает|почему|зачем|объясни|"
    r"расскажи|what is|what's|what are|what does|what do|why|explain|tell me "
    r"about|how does|як працює|как работает)", re.I)

#: (destination, focus keys, terms, terms needed). A term is matched as a
#: lower-cased substring; the Cyrillic ones are stems, so every case of the
#: noun matches. Two terms are asked for where a single word is common
#: ("token" alone could be a model's token limit).
_TOPICS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], int], ...] = (
    ("review_settings.prompts", ("base_instruction", "prompt_guidelines"),
     ("prompt", "промпт", "guideline", "instruction", "інструкці",
      "инструкци", "настанов"), 1),
    ("review_settings.filters", ("ignore_globs",),
     ("ignore", "ігнор", "игнор", "lock file", "lockfile", "lock-файл",
      "glob", "exclude", "виключ", "исключ"), 1),
    ("review_settings.filters", ("comment_min_severity",),
     ("severity", "серйозн", "серьезн", "серьёзн", "поріг", "порог"), 1),
    ("review_settings.filters", ("max_inline_comments",),
     ("max comments", "maximum comments", "кількість коментар",
      "максимум коментар", "количество комментар", "максимум комментар"), 1),
    ("review_settings.categories", ("verifier_enabled",),
     ("verifier", "верифікатор", "верификатор"), 1),
    ("review_settings.general", ("run_on_drafts",),
     ("draft", "чернетк", "черновик"), 1),
    ("review_settings.general", ("review_cadence", "auto_pause_pushes"),
     ("cadence", "auto-pause", "autopause", "auto pause", "автопауз",
      "авто-пауз"), 1),
    ("review_settings.advanced", ("mcp_sources",), ("mcp",), 1),
    ("connections", ("connection_provider",),
     ("token", "токен", "gitlab", "github", "bitbucket", "підключ", "подключ"),
     2),
    ("llm", ("llm_provider_keys",),
     ("api key", "api-key", "api-ключ", "ключ api", "provider key",
      "ключ провайдер", "openai", "anthropic", "embeddings"), 1),
    ("notifications", ("notifications_channel",),
     ("slack", "telegram", "notification", "сповіщен", "оповіщен",
      "уведомлен"), 1),
    ("alerts", ("alerts_webhook",), ("alert", "алерт"), 1),
    ("memories", ("memories_add",),
     ("memory", "memories", "пам'ят", "пам’ят", "памят"), 1),
)

_PROVIDERS = ("github", "gitlab", "bitbucket")


def suggest_destination(question: object) -> dict[str, Any] | None:
    """The `show_in_ui` arguments a setup question clearly asks for, or None.

    A deterministic net under the planner: a model that answers "where do I
    paste the GitLab token" with `help` has not been wrong about the facts,
    only about the verb. Two things must both hold. The sentence asks WHERE or
    HOW TO SET something (`_ASK_WHERE`) and does not open as a request to
    explain one (`_ASK_EXPLAIN`); and exactly one destination is named by
    enough of its words. A tie between destinations, a single weak word, or no
    cue returns None, because a spotlight on the wrong page is worse than the
    help text it replaces.
    """
    text = " ".join(str(question or "").casefold().split())
    if not text or _ASK_EXPLAIN.match(text) or not _ASK_WHERE.search(text):
        return None
    scored: dict[str, tuple[int, tuple[str, ...], list[str]]] = {}
    for dest, focus, terms, need in _TOPICS:
        hits = [t for t in terms if t in text]
        if len(hits) < need:
            continue
        # The most specific topic of a destination wins within it.
        if dest not in scored or len(hits) > scored[dest][0]:
            scored[dest] = (len(hits), focus, hits)
    if not scored:
        return None
    best = max(v[0] for v in scored.values())
    top = [d for d, v in scored.items() if v[0] == best]
    if len(top) != 1:
        return None
    dest = top[0]
    focus = list(scored[dest][1])
    if dest == "connections":
        named = [p for p in _PROVIDERS if p in text]
        if len(named) == 1:
            focus = [f"connection_provider:{named[0]}"]
    return {"destination": dest, "focus": focus}


def find_repo_in_text(question: object, repos: list[dict[str, Any]]) -> str:
    """The repository a sentence names, for the safety net (which has no model).

    Every word is tried with `match_repo`, but a word has to earn it: three
    letters for a slug or a piece of one, six and a close spelling for a typo,
    so that "the", "reviewer" or "settings" never turn into a repository. One
    distinct repository named: its slug. Nothing named: "". A word that fits
    several (the "shop" of acme/shop-api and acme/shop-web) is returned as
    typed, so `build_ui_action` reports it as ambiguous and the agent asks
    which, rather than guessing or silently opening workspace defaults.
    """
    if not repos:
        return ""
    names = [_fold(r.get(k, "")) for r in repos for k in ("repo", "full_name")]
    found: list[str] = []
    vague = ""
    for raw in re.findall(r"[\w./-]+", str(question or "")):
        word = raw.strip("./-")
        if len(word) < 3:
            continue
        w = _fold(word)
        literal = any(w in n for n in names)
        if not literal and len(w) < 6:
            continue
        m = match_repo(word, repos)
        if m.status == "found" and (literal or max(
                SequenceMatcher(None, w, n.split(" ")[-1]).ratio()
                for n in names) >= 0.8):
            if m.slug not in found:
                found.append(m.slug)
        elif m.status == "ambiguous" and literal and len(w) >= 4:
            vague = vague or word
    if len(found) == 1 and not vague:
        return found[0]
    return "" if found else vague


# ── the language the step texts are written in ──────────────────────────

_UK_ONLY = set("іїєґ")
_RU_ONLY = set("ыэъё")


def detect_language(text: object) -> str:
    """`uk`, `ru`, `cyr` (Cyrillic without a telling letter), `en` or ``.

    A script check, not a classifier: Cyrillic against Latin by letter count,
    then Ukrainian by і ї є ґ and Russian by ы э ъ ё. That is enough to say
    whether a sentence the model wrote is in the language it was asked in,
    which is all the popovers need.
    """
    s = str(text or "").casefold()
    cyr = sum("а" <= ch <= "я" or ch in "іїєґёў" for ch in s)
    lat = sum("a" <= ch <= "z" for ch in s)
    if not cyr and not lat:
        return ""
    if cyr <= lat:
        return "en"
    if any(ch in _UK_ONLY for ch in s):
        return "uk"
    if any(ch in _RU_ONLY for ch in s):
        return "ru"
    return "cyr"


def text_in_language(text: str, language: str) -> bool:
    """True when `text` plausibly is in `language` (see `detect_language`).

    Unknown or unsupported languages accept anything: only the cases the
    script can settle are rejected.
    """
    want = (language or "").lower()[:2]
    if want not in {"uk", "ru", "en"}:
        return True
    got = detect_language(text)
    if not got:
        return True
    if want == "en":
        return got == "en"
    if got == "en":
        return False
    return got in {want, "cyr"}
