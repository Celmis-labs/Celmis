"""Workspace review defaults: the layer between a repo policy and the install.

Every review setting a repository can override is resolved the same way,
by the review that runs and by the page that shows it:

    repo policy (non-null) > workspace default (non-null) > install > built-in

"install" is env / `ReviewSettings` (REVIEW_MAX_INLINE_COMMENTS,
REVIEW_VERIFIER_ENABLED, the suppressed-rule set) or, where no env knob
exists, the built-in value (every agent runs, summary on, every branch).

Per-agent model / output ceiling / reasoning are not here: they already have
a workspace layer in the LLM config blob and their own resolver
(`src.review.settings.resolve_agent_llm`). The review language likewise lives
in that blob (`review_language`).

2.3.0 added the Kodus-parity settings (`V23_FIELDS`: drafts, approval,
request-changes, status feedback, committable suggestions, summary placement,
base instruction, message templates, rule filtering, opt-in agents). They
resolve exactly like the older ones; their built-ins live in
`BUILTIN_DEFAULTS`, and a review reads them with the orchestrator's
`_policy_setting(policy, name)`.

Pure functions plus two blocking loaders; no FastAPI, so the orchestrator can
import it without the API's import graph.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

logger = logging.getLogger(__name__)

Source = Literal["repo", "workspace", "install"]

#: The settings a workspace may default and a repository may override, in the
#: order the API reports them. NULL / None at a layer always means "inherit".
INHERITABLE_FIELDS: tuple[str, ...] = (
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
    # ── 2.3.0 (Kodus-parity review settings, migration f1a2b3c4d5e6) ──
    # Opt-in agents (see AGENT_PARTICIPATION_DEFAULTS below).
    "enabled_agents",
    # Gates and provider actions. Stored and resolved here; what each one
    # DOES is the business of the stage that reads it (the drafts gate is
    # the orchestrator's `pr.is_draft` check and the webhook's; the rest land
    # with the provider-action and pipeline work).
    "run_on_drafts",
    "approve_when_clean",
    "request_changes_on_critical",
    "status_feedback",
    "committable_suggestions",
    "apply_filters_to_rules",
    # Where the PR summary goes and what happens to it on the next push.
    "summary_target",
    "summary_on_new_commits",
    "summary_existing_description",
    # Free text: an instruction every agent is given, and the two comments a
    # review posts on its own behalf. NULL / blank = the built-in.
    "base_instruction",
    "message_started",
    "message_finished_header",
)

#: The fields 2.3.0 added (migration f1a2b3c4d5e6) — one column of the same
#: name on both tables, one key of the same name in the policy dict.
V23_FIELDS: tuple[str, ...] = INHERITABLE_FIELDS[INHERITABLE_FIELDS.index("enabled_agents"):]

#: Fields whose value is a list (copied, never shared, between layers).
_LIST_FIELDS = frozenset({
    "disabled_agents", "ignore_globs", "target_branches", "suppressed_rules",
    "enabled_agents",
})

#: Free-text fields. Blank is "inherit" at every layer, never "say nothing":
#: a cleared textarea must not silence a workspace's instruction for one repo
#: while the page shows that repo as inheriting it.
TEXT_FIELDS = frozenset({
    "summary_instructions", "base_instruction", "message_started",
    "message_finished_header",
})

#: The longest base instruction / message template a layer may store. The
#: base instruction reaches EVERY agent's prompt on every review, so its cost
#: is paid once per agent per PR; 2000 characters is a page of guidance, not a
#: second system prompt.
TEXT_SETTING_MAX = 2000

#: The closed-vocabulary settings and what each may say, built-in FIRST:
#:   summary_target               — post the summary as a PR comment, or
#:                                  write it into the PR description;
#:   summary_on_new_commits       — on a later push: replace the summary,
#:                                  append the new review's to it, or leave
#:                                  it alone ("nothing");
#:   summary_existing_description — writing into a description the author
#:                                  already wrote: append below it,
#:                                  complement it (fill in what it lacks), or
#:                                  replace it.
SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    "summary_target": ("comment", "description"),
    "summary_on_new_commits": ("replace", "append", "nothing"),
    "summary_existing_description": ("append", "complement", "replace"),
}

#: Placeholders a message template may use: `{commit}` the short head sha,
#: `{agents}` the agents taking part (comma-separated), `{files}` how many
#: files the review reads, `{pr_number}` the PR / MR number. Anything else in
#: braces is refused on save — a typo would otherwise be posted literally on
#: every PR. `{{` / `}}` write a literal brace.
MESSAGE_PLACEHOLDERS: tuple[str, ...] = ("commit", "agents", "files", "pr_number")
MESSAGE_FIELDS = frozenset({"message_started", "message_finished_header"})

#: Which agents take part when nothing is configured — the explicit default
#: map, one entry per agent `disabled_agents` / `enabled_agents` may name.
#:
#: True ("default on") agents run unless the effective `disabled_agents`
#: names them. False ("opt-in") agents run only when the effective
#: `enabled_agents` names them AND the effective `disabled_agents` does not —
#: an off switch beats an on switch.
#:
#: Why a map and not a built-in `disabled_agents = ["business_logic"]`: a
#: stored list REPLACES the layers under it, so every list saved before an
#: opt-in agent existed (["cve"], …) would switch that agent ON the day it
#: shipped, and every later opt-in agent would need a data migration
#: appending itself to every stored row. With the map a stored list keeps
#: meaning exactly what it meant when it was written, the built-in
#: `disabled_agents` stays [], and a new opt-in agent is one line here.
AGENT_PARTICIPATION_DEFAULTS: dict[str, bool] = {
    "defect": True,
    "contract": True,
    "security": True,
    # The 2.3.0 LLM finders. Named here (and in REVIEW_AGENTS) before their
    # classes join the roster, so settings for them can be saved today; a
    # name the roster does not dispatch is simply never run.
    "performance": True,
    "business_logic": False,
    "structural": True,
    "cve": True,
}

#: The 2.3.0 LLM finders, for the per-agent prompt-override whitelist, which
#: is otherwise derived from the orchestrator's roster (and so would refuse
#: them until their classes are dispatched).
ADDED_LLM_FINDERS: tuple[str, ...] = ("performance", "business_logic")

#: The built-in value of every setting with no env / ReviewSettings knob —
#: the ONE place each is stated. `install_defaults` reads it, and so does the
#: orchestrator's `_policy_setting`, so a review and the page that shows
#: "inherit → X" cannot disagree about X.
BUILTIN_DEFAULTS: dict[str, Any] = {
    "disabled_agents": [],
    "enabled_agents": [],
    "comment_min_severity": "info",
    "summary_enabled": True,
    "summary_instructions": None,
    "started_comment_enabled": True,
    "ignore_globs": [],
    "target_branches": [],
    "run_on_drafts": False,
    "approve_when_clean": False,
    "request_changes_on_critical": False,
    "status_feedback": True,
    "committable_suggestions": False,
    "apply_filters_to_rules": True,
    "summary_target": "comment",
    "summary_on_new_commits": "replace",
    "summary_existing_description": "append",
    "base_instruction": None,
    "message_started": None,
    "message_finished_header": None,
}


def builtin_default(name: str) -> Any:
    """The built-in value of `name` (a fresh copy for a list)."""
    value = BUILTIN_DEFAULTS.get(name)
    return list(value) if isinstance(value, list) else value


def effective_disabled_agents(
    disabled: list[str] | None, enabled: list[str] | None,
) -> set[str]:
    """Every agent that must not run, given the EFFECTIVE lists: the ones
    named off, plus every opt-in agent the enabled list does not name. A name
    in both lists stays off."""
    off = {str(a).strip().lower() for a in (disabled or []) if str(a).strip()}
    on = {str(a).strip().lower() for a in (enabled or []) if str(a).strip()}
    off |= {a for a, default in AGENT_PARTICIPATION_DEFAULTS.items()
            if not default and a not in on}
    return off


def agent_participation(
    disabled: list[str] | None, enabled: list[str] | None,
) -> dict[str, bool]:
    """agent → does it take part, for every agent of the default map."""
    off = effective_disabled_agents(disabled, enabled)
    return {a: a not in off for a in AGENT_PARTICIPATION_DEFAULTS}


def message_template_error(text: str) -> str | None:
    """Why `text` cannot be a message template, or None when it can.

    Parsed with the formatter `render_message_template` uses, so what is
    accepted here is exactly what renders there."""
    import string

    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        return f"{exc} — write {{{{ or }}}} for a literal brace"
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if field not in MESSAGE_PLACEHOLDERS or spec or conversion:
            return (
                f"unknown placeholder {{{field}}} — the placeholders are "
                + ", ".join(f"{{{p}}}" for p in MESSAGE_PLACEHOLDERS)
            )
    return None


def render_message_template(text: str, values: dict[str, Any]) -> str:
    """`text` with its placeholders filled; one without a value renders empty.

    For the stage that posts the message. A template that would not pass
    `message_template_error` (a row written by hand) is returned as written
    rather than raising inside a review."""
    import string

    if message_template_error(text) is not None:
        return text
    filled = {k: "" if values.get(k) is None else str(values[k])
              for k in MESSAGE_PLACEHOLDERS}
    return string.Formatter().vformat(text, (), filled)


def defaults_from_row(row: Any) -> dict[str, Any] | None:
    """A `WorkspaceReviewDefaults` row (or any object with its attributes)
    as the dict the resolver reads; None stays None."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for name in INHERITABLE_FIELDS:
        value = getattr(row, name, None)
        out[name] = list(value) if (name in _LIST_FIELDS and value is not None) else value
    return out


def install_defaults(settings: Any | None = None) -> dict[str, Any]:
    """What every field resolves to when neither a repo nor the workspace
    says anything. `settings` is a `ReviewSettings`; read when omitted."""
    if settings is None:
        from src.review.settings import get_review_settings
        settings = get_review_settings()
    out = {name: builtin_default(name) for name in BUILTIN_DEFAULTS}
    out.update({
        "verifier_enabled": bool(getattr(settings, "verifier_enabled", False)),
        "max_inline_comments": int(getattr(settings, "max_inline_comments", 20)),
        "suppressed_rules": sorted(getattr(settings, "suppressed_rules", ()) or ()),
    })
    return {name: out[name] for name in INHERITABLE_FIELDS}


def _layer_value(layer: Any, name: str) -> Any:
    if layer is None:
        return None
    value = layer.get(name) if isinstance(layer, dict) else getattr(layer, name, None)
    if name in TEXT_FIELDS and isinstance(value, str) and not value.strip():
        return None
    return value


def resolve(
    repo: Any, workspace: dict[str, Any] | None, install: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Source]]:
    """(effective values, which layer each came from) for every inheritable
    field. `verifier_enabled` folds in the deny-list's old spelling of off:
    "verifier" in the effective `disabled_agents` wins, as it does in
    `ReviewOrchestrator._verifier_enabled`."""
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for name in INHERITABLE_FIELDS:
        for source, layer in (("repo", repo), ("workspace", workspace)):
            value = _layer_value(layer, name)
            if value is not None:
                values[name] = list(value) if name in _LIST_FIELDS else value
                sources[name] = source        # type: ignore[assignment]
                break
        else:
            default = install.get(name)
            values[name] = list(default) if isinstance(default, list) else default
            sources[name] = "install"
    if "verifier" in (values.get("disabled_agents") or []):
        values["verifier_enabled"] = False
        sources["verifier_enabled"] = sources["disabled_agents"]
    return values, sources


def inherited_value(
    name: str, workspace: dict[str, Any] | None, install: dict[str, Any],
) -> tuple[Any, Source]:
    """What `name` resolves to for a repository that does not set it."""
    values, sources = resolve(None, workspace, install)
    return values[name], sources[name]


def blank_policy() -> dict[str, Any]:
    """The policy dict of a repository with no policy row: every field
    "inherit", nothing switched off. What a review reads when something other
    than the row (workspace defaults, workspace review rules) has to ride on
    a policy the repository never wrote."""
    out: dict[str, Any] = {
        "enabled": True,
        "prompt_template": "",
        "folder_rules": [],
        "agent_prompt_overrides": {},
        "agent_prompt_guidelines": {},
        "agent_guidelines_extend": [],
        "agents": {},
        "mcp_sources": [],
        "review_language": None,
    }
    for name in INHERITABLE_FIELDS:
        out[name] = None
    return out


def merge_policy(policy: Any, workspace: dict[str, Any] | None) -> Any:
    """The policy dict a review reads, with the workspace defaults filled in
    wherever the repository says nothing.

    Unresolved fields stay None so the orchestrator's own install fallbacks
    (REVIEW_MAX_INLINE_COMMENTS, REVIEW_VERIFIER_ENABLED, …) keep answering —
    one place states each install default, not two. For the settings with
    no env knob that place is `BUILTIN_DEFAULTS`, which the orchestrator's
    `_policy_setting` falls back to. No workspace row → the policy unchanged
    (None stays None: "no policy, defaults apply").
    """
    if not workspace or not any(workspace.get(n) is not None for n in INHERITABLE_FIELDS):
        return policy
    if policy is None:
        merged: dict[str, Any] = blank_policy()
    elif isinstance(policy, dict):
        merged = dict(policy)
    else:
        # A test double or legacy object: nothing to merge into safely.
        return policy
    for name in INHERITABLE_FIELDS:
        # Through `_layer_value`, so a blank text on the repository is an
        # inherit here exactly as it is in `resolve` — the page and the review
        # agree on what a cleared box means.
        if _layer_value(merged, name) is None:
            value = _layer_value(workspace, name)
            merged[name] = (
                None if value is None
                else list(value) if name in _LIST_FIELDS else value
            )
    return merged


def _sync_url() -> str | None:
    raw_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw_url:
        return None
    if "+psycopg" in raw_url:
        return raw_url
    if raw_url.startswith("sqlite"):
        return raw_url.replace("sqlite+aiosqlite://", "sqlite://")
    return (raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
                   .replace("postgresql://", "postgresql+psycopg://"))


def load_workspace_defaults_sync(workspace_id: str | None) -> dict[str, Any] | None:
    """The workspace's defaults row as a dict, or None. Blocking; never raises
    — a review must not fail because its defaults could not be read, it falls
    back to the install defaults and says so in the log."""
    if not workspace_id:
        return None
    url = _sync_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.db.models import WorkspaceReviewDefaults

        engine = create_engine(url, pool_pre_ping=True)
        try:
            with Session(engine) as s:
                return defaults_from_row(s.get(WorkspaceReviewDefaults, workspace_id))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_review_defaults_load_failed ws=%s err=%s",
                       workspace_id, exc)
        return None


def run_on_drafts_for_repo(provider: str, full_name: str) -> bool:
    """Whether a draft PR of this repository is reviewed — for the webhook,
    which decides before any review (and so before any policy) exists.

    The same resolution the orchestrator's drafts gate applies (repo policy >
    the defaults of the workspace owning it > built-in False), asked of the
    same binding `_dispatch_review` will use. Blocking; never raises — an
    unbound or ambiguous repository, or a database that cannot be read, is
    False, which is exactly the skip every draft got before the setting
    existed.
    """
    value = _setting_for_repo(provider, full_name, "run_on_drafts")
    return bool(builtin_default("run_on_drafts") if value is None else value)


def target_branches_for_repo(provider: str, full_name: str) -> list[str]:
    """The target-branch patterns in force for this repository (repo policy >
    workspace default > built-in [] = every branch) — for the webhook's early
    draft skip, which has to say "this branch is never reviewed" rather than
    "reviewed once ready" when the patterns leave the base branch out.
    Blocking; never raises — unreadable is [] (no restriction claimed)."""
    value = _setting_for_repo(provider, full_name, "target_branches")
    return [str(v) for v in value] if isinstance(value, list) else []


def _setting_for_repo(provider: str, full_name: str, name: str) -> Any:
    """One inheritable setting for the repository a delivery names, resolved
    repo policy > workspace default; None when neither says anything (or the
    repository is unbound, or the database unreadable)."""
    try:
        from src.api.auto_review import get_auto_review_store

        cfg = get_auto_review_store().config_for_repo(provider, full_name)
        if cfg is None:
            return None
        url = _sync_url()
        if not url:
            return None
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults
        from src.sync.git_providers import parse_repo_url

        # `PullRequest.local_slug` — the key the orchestrator loads the
        # policy by — is this parse, so the two gates read the same row.
        try:
            slug = parse_repo_url(f"{provider}:{full_name}").slug
        except Exception:  # noqa: BLE001
            slug = cfg.repo_slug
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with Session(engine) as s:
                row = s.get(RepoReviewPolicy, slug)
                value = getattr(row, name, None) if row is not None else None
                if value is not None:
                    return value
                owner = row.workspace_id if row is not None else cfg.workspace_id
                ws = s.get(WorkspaceReviewDefaults, owner)
                value = getattr(ws, name, None) if ws is not None else None
                if value is not None:
                    return value
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s_lookup_failed provider=%s repo=%s err=%s",
                       name, provider, full_name, exc)
    return None
