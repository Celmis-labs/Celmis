"""Per-repo AI-reviewer policies (Stage 10).

Endpoints:
    GET    /api/review-policies                 — list (filter by department + search)
    GET    /api/review-policies/{slug}          — full detail (defaults if no row)
    PUT    /api/review-policies/{slug}          — upsert
    DELETE /api/review-policies/{slug}          — reset to default (delete row)
    GET    /api/review-policies/{slug}/branches — branches (provider, else local clone)
    GET    /api/review-policies/{slug}/prompt-preview — effective prompt of one agent
    GET    /api/review-policies/prompt-preview  — the same for a repo with no settings
    GET    /api/review-policies/overrides-summary — agent → repos overriding its
                                                 prompt / with guidelines of their own

Per-agent prompts come in two kinds (src/review/prompt_guidelines.py):
`agent_prompt_guidelines` are ADDED to the agent's prompt (the default
customisation, at most 2000 characters, the repository's replacing the
workspace's unless the agent is listed in `agent_guidelines_extend`), and
`agent_prompt_overrides` REPLACE it (the advanced mode).

Every setting the workspace review defaults also carry (/api/review-defaults:
agent participation, verifier, comment threshold, inline cap, summary,
started comment, ignore globs, target branches, suppressed rules) is
three-layered: this policy's value when it is not null, else the workspace
default, else the install default. GET reports what the policy says, the
`*_effective` value, `sources[field]` ("repo" | "workspace" | "install") and
`inherited[field]` — what a reset to inherited would give.

Per-agent LLM knobs live here too, and this is the layer that WINS: a repo
policy beats the workspace `agents` entry, which beats the review profile,
which beats ReviewSettings. The model has been per-repo since Stage 11 (the
five `<agent>_model` columns); the output ceiling and the reasoning level had
no per-repo home at all, so the screen with the most authority showed the
least — an operator could pick a model here and never learn that the two
settings which decide whether that model can answer at all lived on another
page, nor that their combination can be invalid. They are one JSONB column
now, `agent_llm_overrides`, shaped exactly like the workspace `agents` blob so
that both screens share one validator and one resolver.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
    require_prompt_editor,
    require_repo_permission,
)
from src.api.schemas import (
    AgentOverridesSummary,
    AgentPromptOverrideRepo,
    FolderRule,
    RepoBranchesOut,
    ReviewPolicyIn,
    ReviewPolicyListItem,
    ReviewPolicyOut,
)
from src.db.models import RepoReviewPolicy
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review-policies", tags=["review-policies"])

# Agents the orchestrator dispatches per PR and can therefore skip.
# Keep in sync with `ReviewOrchestrator._default_agents()`. `verifier` is NOT
# here: it is a post-processor (dedup + FP filter) over the other agents'
# findings, not a producer, so it is always on.
def _llm_agent_names() -> tuple[str, ...]:
    """The LLM agents the orchestrator actually dispatches, in roster order.

    Asked of the orchestrator rather than listed, because three separate
    literals in this file went stale the day the roster was renamed: the
    prompt-preview query pattern, its registry, and the prompt-override
    whitelist. Two of them failed loudly (422 / 500) and the third failed in
    silence, which is the worse one.
    """
    from src.review.agents.base import LLMReviewAgent
    from src.review.orchestrator import ReviewOrchestrator

    return tuple(
        a.name for a in ReviewOrchestrator._default_agents()
        if isinstance(a, LLMReviewAgent)
    )


def _added_llm_finders() -> tuple[str, ...]:
    from src.review.review_defaults import ADDED_LLM_FINDERS
    return ADDED_LLM_FINDERS


def _participation_defaults() -> dict[str, bool]:
    from src.review.review_defaults import AGENT_PARTICIPATION_DEFAULTS
    return dict(AGENT_PARTICIPATION_DEFAULTS)


#: `^(defect|contract|security)$` — computed at import so FastAPI can compile
#: it into the route's schema, which is where a hand-written alternation could
#: never keep up with a rename.
#: The verifier is previewable too: it takes a per-repo system prompt like the
#: finders, so the box on the policy page needs the same "show me" button.
_PREVIEWABLE_PATTERN = "^(" + "|".join((*_llm_agent_names(), "verifier")) + ")$"

#: Agents a per-repo prompt override may name, in roster order. The finders,
#: plus the verifier: it takes a system prompt like the rest even though it
#: finds nothing itself.
#: Plus the 2.3.0 finders (`ADDED_LLM_FINDERS`), accepted by name before the
#: roster dispatches them — the agent work lands separately, and a prompt
#: saved for it in the meantime must not be dropped in silence on save (the
#: failure this whitelist's history is about). De-duplicated, so the day they
#: join the roster nothing changes here.
_OVERRIDABLE_AGENT_ORDER: tuple[str, ...] = tuple(dict.fromkeys(
    (*_llm_agent_names(), *_added_llm_finders(), "verifier")))
_OVERRIDABLE_AGENTS = frozenset(_OVERRIDABLE_AGENT_ORDER)


#: Every agent the participation lists may name — the keys of
#: `review_defaults.AGENT_PARTICIPATION_DEFAULTS`, whose values say which
#: run by default (business_logic does not: it is opt-in, switched on through
#: `enabled_agents`) — and then the verifier.
TOGGLEABLE_AGENTS = (
    *_participation_defaults(),
    # The verifier is a stage, not an agent, but it is switchable for the same
    # reason the agents are: measured on a 50-PR benchmark it dropped 40 of
    # 187 candidates at a 1024-token ceiling and 61 of 75 once that ceiling
    # was lifted — every one through its LLM step, none through the
    # confidence threshold — and in 5 of 14 reviews it kept nothing at all.
    # An operator who can see that needs a way to turn it off without a
    # deploy.
    "verifier",
)

#: The key that must NEVER appear inside `agent_llm_overrides`. The model of
#: this layer is the `<agent>_model` COLUMN, and one field with two homes is
#: the failure this project keeps hitting — the last one asked litellm about a
#: model that was not the model in play, three separate times in one review of
#: the workspace layer. A payload that puts it in the blob is refused, loudly,
#: and told where it lives instead.
_MODEL_FIELD = "model"

#: The `<agent>_model` columns a PUT leaves alone when it does not name them
#: (the 2.3.0 finders'). The five older columns are replaced on every save,
#: as they always were; these arrived after the policy page, and a page that
#: cannot render a field must not clear it.
_KEPT_MODEL_FIELDS = ("performance_model", "business_logic_model")


# ─── Helpers ──────────────────────────────────────────────────────────


def _agent_names() -> tuple[str, ...]:
    """The agents that may carry a per-repo LLM entry.

    `src.review.settings.REVIEW_AGENTS` is the one spelling of that set, and
    it deliberately includes `compliance` — which has no model column here and
    inherits one from /settings/llm. Imported inside the function to keep this
    router's import graph free of `src.review`, the way every other handler in
    this file already treats it.
    """
    from src.review.settings import REVIEW_AGENTS
    return REVIEW_AGENTS


def _default_suppressed_rules() -> list[str]:
    """The code default the prefilter hides when a policy says nothing.

    Imported inside the function for the reason `_agent_names` is: this
    router's import graph stays free of `src.review`.
    """
    from src.review.settings import get_review_settings
    return sorted(get_review_settings().suppressed_rules)


def _verifier_default() -> bool:
    """Whether an unconfigured repository runs the model's veto. Off — see
    `ReviewSettings.verifier_enabled`. Read through the settings rather than
    written here, so the API and the orchestrator cannot disagree about what
    "inherit" resolves to."""
    from src.review.settings import get_review_settings

    return bool(get_review_settings().verifier_enabled)


def _rule_target_agents() -> tuple[str, ...]:
    """The agents a custom rule may be addressed to: the LLM finders, the
    agents whose prompts carry the policy's rules."""
    return _llm_agent_names()


def _folder_rules_from_payload(incoming: list[FolderRule]) -> list[dict]:
    """Shape a PUT's `folder_rules` into what the row stores.

    Stored minimally: `{pattern, prompt}` plus only the optional fields that
    say something. A rule using none of them is stored exactly as every rule
    was before they existed, so older readers keep reading it unchanged.

    An agent the rule cannot reach is refused, not dropped: a rule addressed
    only to a misspelt agent would otherwise reach nobody while the page
    showed it as active.
    """
    known = _rule_target_agents()
    out: list[dict] = []
    for idx, fr in enumerate(incoming):
        entry: dict[str, Any] = {"pattern": fr.pattern, "prompt": fr.prompt}
        title = (fr.title or "").strip()
        if title:
            entry["title"] = title
        if fr.severity_hint:
            entry["severity_hint"] = fr.severity_hint
        agents = [str(a).strip() for a in (fr.agents or []) if str(a).strip()]
        unknown = [a for a in agents if a not in known]
        if unknown:
            raise HTTPException(status_code=422, detail=(
                f"folder_rules[{idx}].agents: unknown agent(s) "
                f"{', '.join(unknown)} — a rule can target: {', '.join(known)}"
            ))
        agents = list(dict.fromkeys(agents))
        if agents and set(agents) != set(known):
            # Naming every agent is the same as naming none; store it as none
            # so a newly added agent is not silently left out of the rule.
            entry["agents"] = agents
        out.append(entry)
    return out


def _language_codes() -> tuple[str, ...]:
    from src.llm.prompts.language import LANGUAGE_NAMES
    return tuple(LANGUAGE_NAMES)


def _review_language_from_payload(incoming: str | None) -> str | None:
    if incoming is None:
        return None
    value = str(incoming).strip()
    if not value:
        return None
    codes = _language_codes()
    # Codes are case-sensitive in storage ("zh-CN"); accept any casing in.
    match = next((c for c in codes if c.lower() == value.lower()), None)
    if match is None:
        raise HTTPException(status_code=422, detail=(
            f"review_language: {incoming!r} — expected one of "
            f"{', '.join(codes)}, or null to inherit the workspace language"
        ))
    return match


def _workspace_review_language(workspace_id: str) -> str:
    """The language an unconfigured repository reviews in. Blocking."""
    return _workspace_review_language_layer(workspace_id)[0]


def _workspace_review_language_layer(workspace_id: str) -> tuple[str, str]:
    """(language, "workspace" | "install") — the workspace LLM config's
    `review_language` when it holds one, else the built-in English. Blocking."""
    try:
        from src.api.routers.llm import _load_workspace_config
        value = _load_workspace_config(workspace_id).get("review_language")
    except Exception:  # noqa: BLE001
        value = None
    if isinstance(value, str) and value.strip():
        return value.strip(), "workspace"
    return "en", "install"


async def _load_workspace_defaults(
    session: AsyncSession, workspace_id: str,
) -> dict[str, Any] | None:
    """The workspace review defaults as the resolver's dict, or None.

    Called FIRST in a handler, before anything else is loaded into the
    session: a failure (a database the migration has not reached yet) is
    rolled back, and a rollback expires whatever the session already holds.
    The page still renders — on the install defaults — rather than 500.
    """
    from src.db.models import WorkspaceReviewDefaults
    from src.review.review_defaults import defaults_from_row

    try:
        return defaults_from_row(await session.get(WorkspaceReviewDefaults, workspace_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace_review_defaults_unavailable ws=%s err=%s",
                       workspace_id, exc)
        await session.rollback()
        return None


def _install_defaults() -> dict[str, Any]:
    from src.review.review_defaults import install_defaults
    return install_defaults()


def _max_inline_default() -> int:
    from src.review.settings import get_review_settings
    return int(get_review_settings().max_inline_comments)


#: The comment thresholds a policy may name, most to least strict, and what an
#: unset one means: post every finding.
COMMENT_SEVERITY_LEVELS = ("critical", "error", "warning", "info")
COMMENT_SEVERITY_DEFAULT = "info"


def _comment_min_severity_from_payload(incoming: str | None) -> str | None:
    if incoming is None:
        return None
    value = str(incoming).strip().lower()
    if not value:
        return None
    if value not in COMMENT_SEVERITY_LEVELS:
        raise HTTPException(status_code=422, detail=(
            f"comment_min_severity: {incoming!r} — expected one of "
            f"{', '.join(COMMENT_SEVERITY_LEVELS)}, or null to inherit"
        ))
    return value


def _ignore_globs_from_payload(incoming: list[str] | None) -> list[str] | None:
    """Validate and clean a PUT's `ignore_globs`; None (inherit) stays None."""
    if incoming is None:
        return None
    from src.review.ignore_globs import validate_ignore_globs
    try:
        cleaned = validate_ignore_globs(incoming)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"ignore_globs: {exc}") from exc
    # [] is kept: "nothing extra for this repo" over a workspace default.
    return cleaned


def _suppressed_rules_from_payload(incoming: list[str] | None) -> list[str] | None:
    """Shape a PUT's `suppressed_rules` into what the row stores.

    `None` stays None — "inherit the code default" — and a list is kept in the
    order it was sent, stripped and de-duplicated. There is no known set to
    check a rule id against (agents mint them, `sec.cve-GHSA-…` included), so a
    typo cannot be caught here the way an unknown agent name is; what CAN be
    caught is a value that is not a rule id at all — empty, or with whitespace
    inside it — and that is refused loudly rather than stored to match nothing.
    """
    if incoming is None:
        return None
    cleaned: list[str] = []
    for raw in incoming:
        rule = str(raw).strip()
        if not rule or any(ch.isspace() for ch in rule):
            raise HTTPException(status_code=422, detail=(
                f"suppressed_rules: {raw!r} is not a rule id — expected the "
                f"dotted id an agent emits, e.g. 'quality.todo'"
            ))
        cleaned.append(rule)
    return list(dict.fromkeys(cleaned))


def target_branches_from_payload(incoming: list[str] | None) -> list[str] | None:
    """A PUT's `target_branches`, cleaned: None stays None (inherit), entries
    stripped and de-duplicated in order. Names, globs and `!` exclusions are
    accepted (src/review/branch_patterns.py); an entry that could never match
    — a bare `!`, `!!x`, a space inside — is a 422 naming it. Shared by both
    layers, so a repository and its workspace refuse the same entries."""
    if incoming is None:
        return None
    from src.review.branch_patterns import clean_patterns, pattern_error

    cleaned = clean_patterns(incoming)
    for entry in cleaned:
        problem = pattern_error(entry)
        if problem:
            raise HTTPException(status_code=422, detail=f"target_branches: {problem}")
    return cleaned


# ─── 2.3.0 settings: shaping + validation, shared by both layers ─────


def _choice_from_payload(name: str, incoming: str | None) -> str | None:
    """A closed-vocabulary setting (`review_defaults.SETTING_CHOICES`):
    case and whitespace forgiven, blank = inherit, anything else a 422 that
    names the choices."""
    from src.review.review_defaults import SETTING_CHOICES

    if incoming is None:
        return None
    value = str(incoming).strip().lower()
    if not value:
        return None
    choices = SETTING_CHOICES[name]
    if value not in choices:
        raise HTTPException(status_code=422, detail=(
            f"{name}: {incoming!r} — expected one of {', '.join(choices)}, "
            f"or null to inherit"
        ))
    return value


def _text_setting_from_payload(name: str, incoming: str | None) -> str | None:
    """A free-text setting: stripped, blank = inherit (the built-in), at most
    `TEXT_SETTING_MAX` characters (the schema refuses longer before this
    runs; checked again here for a caller that bypasses it). A message
    template must use only the documented placeholders."""
    from src.review.review_defaults import (
        MESSAGE_FIELDS,
        TASK_FIELD_PATTERN,
        TEXT_SETTING_MAX,
        message_template_error,
    )

    if incoming is None:
        return None
    value = str(incoming).strip()
    if not value:
        return None
    if len(value) > TEXT_SETTING_MAX:
        raise HTTPException(status_code=422, detail=(
            f"{name}: {len(value)} characters — at most {TEXT_SETTING_MAX}"
        ))
    if name in MESSAGE_FIELDS:
        problem = message_template_error(value)
        if problem:
            raise HTTPException(status_code=422, detail=f"{name}: {problem}")
    if name == "task_acceptance_field" and not re.match(TASK_FIELD_PATTERN, value):
        raise HTTPException(status_code=422, detail=(
            f"{name}: {value!r} is not a Jira custom field id — expected "
            f"customfield_ and digits (e.g. customfield_10042), or blank to "
            f"read the criteria from the description"
        ))
    return value


def _project_keys_from_payload(name: str, incoming: list[str] | None) -> list[str] | None:
    """Jira project keys: trimmed, upper-cased, de-duplicated, each in Jira's
    own shape. Refused, not dropped, when one is malformed — a typo would
    otherwise silently stop every task from being found."""
    from src.review.review_defaults import PROJECT_KEY_PATTERN

    if incoming is None:
        return None
    keys = [str(k).strip().upper() for k in incoming if str(k).strip()]
    bad = [k for k in keys if not re.match(PROJECT_KEY_PATTERN, k)]
    if bad:
        raise HTTPException(status_code=422, detail=(
            f"{name}: {', '.join(bad[:5])} — a Jira project key is capital "
            f"letters and digits, starting with a letter (e.g. PROJ)"
        ))
    return list(dict.fromkeys(keys))


def _enabled_agents_from_payload(incoming: list[str] | None) -> list[str] | None:
    """The opt-in list: names from the participation map only. Refused, not
    dropped, when unknown — a misspelt opt-in would otherwise switch on
    nothing while the page showed it on — and "verifier" is pointed at its
    own switch rather than accepted here."""
    if incoming is None:
        return None
    known = tuple(_participation_defaults())
    names = [str(a).strip().lower() for a in incoming if str(a).strip()]
    if "verifier" in names:
        raise HTTPException(status_code=422, detail=(
            "enabled_agents: the verifier has its own switch — set "
            "verifier_enabled instead"
        ))
    unknown = [a for a in names if a not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=(
            f"enabled_agents: unknown agent(s) {', '.join(unknown)} — "
            f"the agents are: {', '.join(known)}"
        ))
    return list(dict.fromkeys(names))


def _int_setting_from_payload(name: str, incoming: int | None) -> int | None:
    """A whole-number setting (`review_defaults.INT_FIELDS`): null inherits,
    anything outside its range is a 422 naming the range (the schema refuses
    it first; checked again for a caller that bypasses the schema)."""
    from src.review.review_defaults import INT_FIELDS

    if incoming is None:
        return None
    low, high = INT_FIELDS[name]
    if isinstance(incoming, bool) or not isinstance(incoming, int) or not low <= incoming <= high:
        raise HTTPException(status_code=422, detail=(
            f"{name}: {incoming!r} — expected a whole number from {low} to {high}, "
            f"or null to inherit"
        ))
    return incoming


def _title_keywords_from_payload(incoming: list[str] | None) -> list[str] | None:
    """`ignored_title_keywords`: trimmed, blanks and case-insensitive
    duplicates dropped; at most 50 entries of 100 characters. Null inherits;
    an empty list is this layer's own "no keyword"."""
    from src.review.review_defaults import (
        TITLE_KEYWORD_MAX_CHARS,
        TITLE_KEYWORDS_MAX,
        normalise_title_keywords,
    )

    if incoming is None:
        return None
    words = normalise_title_keywords(incoming)
    if len(words) > TITLE_KEYWORDS_MAX:
        raise HTTPException(status_code=422, detail=(
            f"ignored_title_keywords: {len(words)} keywords — at most {TITLE_KEYWORDS_MAX}"
        ))
    too_long = [w for w in words if len(w) > TITLE_KEYWORD_MAX_CHARS]
    if too_long:
        raise HTTPException(status_code=422, detail=(
            f"ignored_title_keywords: a keyword is longer than "
            f"{TITLE_KEYWORD_MAX_CHARS} characters"
        ))
    return words
#: The longest identity one trusted-commenter entry may be (an e-mail, a
#: login, an Atlassian account id or a `{uuid}`).
TRUSTED_COMMENTER_MAX = 200


def _trusted_commenters_from_payload(
    incoming: list[str] | None, name_of_field: str = "memory_trusted_commenters",
) -> list[str] | None:
    """A list of identities (the people whose "remember" is active at once, or
    the reviewers learning ignores): trimmed, blanks and repeats dropped
    (compared case-insensitively, first spelling kept), each short and free of
    control characters. [] is a decision (nobody but the token owner); null
    inherits."""
    if incoming is None:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for raw in incoming:
        name = str(raw or "").strip()
        if not name:
            continue
        if len(name) > TRUSTED_COMMENTER_MAX or any(ord(ch) < 32 for ch in name):
            raise HTTPException(status_code=422, detail=(
                f"{name_of_field}: an identity is at most "
                f"{TRUSTED_COMMENTER_MAX} characters, on one line"))
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def v23_updates_from_payload(payload: Any) -> dict[str, Any]:
    """{field: value to store} for every 2.3.0 setting the request NAMED.

    Absent keeps what is stored (not in the result), null inherits, a value
    is validated and shaped. Everything is checked before the caller writes
    anything, so a 422 leaves no half-saved settings. One function for both
    layers, so a repository and its workspace can never be validated by two
    different rules.
    """
    from src.review.review_defaults import (
        INT_FIELDS,
        SETTING_CHOICES,
        TEXT_FIELDS,
        V23_FIELDS,
    )

    sent = payload.model_fields_set
    out: dict[str, Any] = {}
    for name in V23_FIELDS:
        if name not in sent:
            continue
        value = getattr(payload, name)
        if name == "enabled_agents":
            out[name] = _enabled_agents_from_payload(value)
        elif name == "ignored_title_keywords":
            out[name] = _title_keywords_from_payload(value)
        elif name in INT_FIELDS:
            out[name] = _int_setting_from_payload(name, value)
        elif name == "task_project_keys":
            out[name] = _project_keys_from_payload(name, value)
        elif name in SETTING_CHOICES:
            out[name] = _choice_from_payload(name, value)
        elif name in TEXT_FIELDS:
            out[name] = _text_setting_from_payload(name, value)
        elif name in ("memory_trusted_commenters", "learning_excluded_reviewers"):
            out[name] = _trusted_commenters_from_payload(value, name)
        else:
            out[name] = None if value is None else bool(value)
    return out


def _agent_llm_fields() -> tuple[str, ...]:
    """The keys one agent's entry may carry HERE.

    Derived from the workspace layer's `AGENT_FIELDS` minus the model, rather
    than written out again: when that surface grows a fourth knob, this one
    grows it in the same commit instead of in the bug report that follows.
    """
    from src.api.routers.llm import AGENT_FIELDS
    return tuple(f for f in AGENT_FIELDS if f != _MODEL_FIELD)


def _model_field_for(agent: str) -> str | None:
    """The policy column carrying `agent`'s model, or None if it has none.

    Asked of the model class rather than listed here, so the day a
    `compliance_model` column lands this answers for it without a second edit.
    Compliance is the live case today: it is a configurable agent with no
    column, so its model comes from the workspace and a value saved here is
    validated against THAT.

    The columns kept their pre-restructure names — no migration, and every
    stored pin keeps working — so the current agents map onto legacy columns:
    contract reads `architect_model`, defect reads `quality_model`. The same
    mapping `resolve_agent_llm` applies when a review runs, imported from the
    one place it is spelled; a second copy here would be the two-homes bug
    this file's own comments keep warning about.
    """
    field = f"{agent}_model"
    if hasattr(RepoReviewPolicy, field):
        return field
    from src.review.settings import LEGACY_AGENT_NAMES
    legacy = next((f"{old}_model" for old, new_ in LEGACY_AGENT_NAMES.items()
                   if new_ == agent), None)
    if legacy and hasattr(RepoReviewPolicy, legacy):
        return legacy
    return None


def _model_columns(source: Any) -> dict[str, str | None]:
    """The `<agent>_model` fields read off a payload or a row.

    Both shapes spell them identically, which is what lets the PUT validate
    against the models it is SAVING rather than the ones it is replacing.
    """
    out: dict[str, str | None] = {}
    for agent in _agent_names():
        field = _model_field_for(agent)
        if field:
            out[field] = getattr(source, field, None)
    return out


def _policy_view(models: dict[str, str | None], overrides: dict) -> dict:
    """A policy in the shape `resolve_agent_llm` reads.

    The same shape `ReviewOrchestrator._load_policy` builds for a review, so
    what this page computes and what a review does are one question asked
    twice, not two questions.
    """
    return {**models, "agents": overrides}


def _agent_llm_overrides_from_payload(
    incoming: dict[str, dict | None] | None, stored: dict | None,
) -> dict[str, dict[str, Any]]:
    """Shape a PUT's `agent_llm_overrides` into the map that replaces the stored one.

    Sent WHOLE, not as a patch, exactly like the workspace `agents` block:
    absent already means "inherit" at every layer of this chain, so an omitted
    agent — or an omitted field inside one — is the only way a form can say
    "stop overriding that". A per-key merge would read the same request as
    "leave it alone" and keep a value the operator watched disappear from the
    screen.

    `None` (the key not sent at all) keeps what is stored. That is for the
    rollout window in which the page has model dropdowns and no ceiling
    controls yet: without it, every save from that page would silently wipe
    settings it cannot render.
    """
    if incoming is None:
        return dict(stored or {})

    known = _agent_names()
    allowed = _agent_llm_fields()
    merged: dict[str, dict[str, Any]] = {}

    for name, entry in incoming.items():
        if name not in known:
            raise HTTPException(status_code=422, detail=(
                f"unknown agent '{name}' — the review agents are: "
                f"{', '.join(known)}"
            ))
        if entry is None:
            continue                    # null → no overrides, same as omitting it
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail=(
                f"agent '{name}' must be an object of overrides, or null to "
                f"clear them"
            ))
        if _MODEL_FIELD in entry:
            column = _model_field_for(name)
            where = (
                f"set '{column}' on this policy instead"
                if column else
                f"'{name}' has no per-repo model — it inherits the one chosen "
                f"on /settings/llm"
            )
            raise HTTPException(status_code=422, detail=(
                f"agent '{name}': the model does not live in "
                f"agent_llm_overrides — {where}. One field, one place."
            ))
        unknown = sorted(k for k in entry if k not in allowed)
        if unknown:
            raise HTTPException(status_code=422, detail=(
                f"agent '{name}': unknown field(s) {', '.join(unknown)} — "
                f"allowed: {', '.join(allowed)}"
            ))
        cur: dict[str, Any] = {}
        for field in allowed:
            value = entry.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue                # absent, null or blank → inherit this one
            cur[field] = value.strip() if isinstance(value, str) else value
        if cur:
            merged[name] = cur          # an empty entry is no entry, not "set to nothing"

    return merged


def _effective_agents(policy: dict, workspace_id: str) -> dict[str, dict]:
    """What every agent would run with under `policy`, right now.

    Walks the whole chain through `src.review.settings.resolve_agent_llm` —
    the same call the orchestrator makes — and returns the model as the
    LiteLLM string `LLMClient.generate` will put on the wire, so a limit shown
    on this page, a limit enforced on save and a limit hit by a review are all
    the same number.

    The workspace blob is read ONCE for the whole map: the workspace-layer
    helper that does it per agent would charge six credential-store reads for
    one page render. Blocking I/O — callers on the request path hand it to a
    thread.
    """
    from src.api.routers.llm import (
        _effective_agent,
        _load_workspace_config,
        _review_selection,
    )
    cfg = _load_workspace_config(workspace_id)
    selection = _review_selection(cfg, workspace_id)
    return {
        agent: _effective_agent(
            agent, cfg, workspace_id, policy=policy, selection=selection,
        )
        for agent in _agent_names()
    }


async def _effective_agents_for_display(
    policy: dict, workspace_id: str,
) -> dict[str, dict]:
    """`_effective_agents`, but a failure costs the panel and not the page.

    The form has to render for a workspace whose credential store is having a
    bad day; "we could not work out what is in force" is an empty panel, not a
    500 on the screen an operator opens to fix things. The SAVE path does not
    get this net — see `_validate_agent_llm_overrides`.
    """
    try:
        return await asyncio.to_thread(_effective_agents, policy, workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("effective_agents_unavailable ws=%s err=%s", workspace_id, exc)
        return {}


def _validate_agent_llm_overrides(
    overrides: dict[str, dict[str, Any]], policy: dict, workspace_id: str,
) -> None:
    """Refuse a per-agent override that cannot do what it says.

    `policy` must be the policy as it will be AFTER this save. Each agent is
    judged on the model it will END UP on — the one this very request is
    setting, or, when it sets none, the one the agent inherits. Judging the
    model being REPLACED is a bug that was found and fixed at the workspace
    layer (PUT {"architect": {"reasoning": "high"}} over a stored
    architect.model="gpt-4o" came back 422 naming gpt-4o, for a save after
    which the architect inherits a model that takes "high" happily), and it is
    reachable here through a more ordinary gesture still: this form changes the
    model and the ceiling in the same submit.

    `_validate_agent_entry` is the workspace layer's own validator, imported
    rather than twinned — this layer OUTRANKS that one, and a winning layer
    that validates by different rules is how an invalid combination reaches a
    provider. It also coerces in place (a budget posted as "4096" is stored as
    4096), and `overrides` is the very map that gets saved, so the coercion
    lands in the row.

    Deliberately no try/except: if the effective model cannot be worked out,
    nothing here has been checked, and storing an unchecked combination is the
    failure this function exists to prevent.
    """
    from src.api.routers.llm import _validate_agent_entry

    effective = _effective_agents(policy, workspace_id)
    for agent, entry in overrides.items():
        model = (effective.get(agent) or {}).get(_MODEL_FIELD) or ""
        _validate_agent_entry(agent, entry, model)




def _catalog_fields() -> dict[str, Any]:
    """What every policy response carries about the roster, stored row or not."""
    return {
        "overridable_agents": list(_OVERRIDABLE_AGENT_ORDER),
        "rule_target_agents": list(_rule_target_agents()),
        "review_languages": list(_language_codes()),
        **settings_vocabulary(),
    }


def settings_vocabulary() -> dict[str, Any]:
    """The 2.3.0 vocabularies both layers' responses carry: the closed
    choices, the message placeholders and the participation defaults."""
    from src.review.review_defaults import MESSAGE_PLACEHOLDERS, SETTING_CHOICES

    return {
        "setting_choices": {k: list(v) for k, v in SETTING_CHOICES.items()},
        "message_placeholders": list(MESSAGE_PLACEHOLDERS),
        "agent_participation_defaults": _participation_defaults(),
    }


def _stored_folder_rules(raw: list | None) -> list[FolderRule]:
    """The stored rules as the API shape; a malformed row is skipped rather
    than turning the whole page into a 500."""
    out: list[FolderRule] = []
    for fr in raw or []:
        if not isinstance(fr, dict):
            continue
        try:
            out.append(FolderRule(**{
                k: v for k, v in fr.items()
                if k in ("pattern", "prompt", "title", "severity_hint", "agents")
            }))
        except Exception:  # noqa: BLE001
            logger.warning("folder_rule_unreadable rule=%r", fr)
    return out


def _layered_fields(
    row: Any, ws_defaults: dict[str, Any] | None,
    workspace_language: tuple[str, str],
) -> dict[str, Any]:
    """Every inheritable field of a policy response: what THIS policy says,
    the effective value, its source and what inheriting would give.

    `row` is None for a repository without a policy row — everything then
    inherits. One function for both shapes, so a stored row and the synthetic
    default can never disagree about what "inherit" means.
    """
    from src.review.review_defaults import (
        INHERITABLE_FIELDS,
        LIST_FIELDS,
        TEXT_FIELDS,
        V23_FIELDS,
        agent_participation,
        resolve,
    )

    install = _install_defaults()
    effective, sources = resolve(row, ws_defaults, install)
    inherited, inherited_sources = resolve(None, ws_defaults, install)

    def own(name: str) -> Any:
        value = None if row is None else getattr(row, name, None)
        if isinstance(value, list):
            return list(value)
        if name in TEXT_FIELDS and isinstance(value, str) and not value.strip():
            return None
        return value

    # The 2.3.0 settings, uniformly: own value, effective value. Their
    # sources / inherited entries come with the loop over INHERITABLE_FIELDS.
    v23: dict[str, Any] = {}
    for name in V23_FIELDS:
        v23[name] = own(name)
        value = effective[name]
        v23[f"{name}_effective"] = (
            list(value or []) if name in LIST_FIELDS else value)
    v23["agent_participation_effective"] = agent_participation(
        effective["disabled_agents"], effective["enabled_agents"])

    language, language_source = workspace_language
    own_language = own("review_language")
    sources = dict(sources)
    sources["review_language"] = "repo" if own_language else language_source
    inherited = {n: inherited[n] for n in INHERITABLE_FIELDS}
    inherited["review_language"] = language

    return {
        "target_branches": own("target_branches"),
        "target_branches_effective": list(effective["target_branches"] or []),
        "disabled_agents": own("disabled_agents"),
        "disabled_agents_effective": list(effective["disabled_agents"] or []),
        "suppressed_rules": own("suppressed_rules"),
        "suppressed_rules_effective": list(effective["suppressed_rules"] or []),
        "verifier_enabled": own("verifier_enabled"),
        "verifier_enabled_effective": bool(effective["verifier_enabled"]),
        "verifier_enabled_default": bool(inherited["verifier_enabled"]),
        "ignore_globs": own("ignore_globs"),
        "ignore_globs_effective": list(effective["ignore_globs"] or []),
        "comment_min_severity": own("comment_min_severity"),
        "comment_min_severity_effective": (
            effective["comment_min_severity"] or COMMENT_SEVERITY_DEFAULT),
        "summary_enabled": own("summary_enabled"),
        "summary_enabled_effective": effective["summary_enabled"] is not False,
        "summary_instructions": own("summary_instructions"),
        "summary_instructions_effective": effective["summary_instructions"],
        "started_comment_enabled": own("started_comment_enabled"),
        "started_comment_enabled_effective": (
            effective["started_comment_enabled"] is not False),
        "max_inline_comments": own("max_inline_comments"),
        "max_inline_comments_effective": int(effective["max_inline_comments"]),
        "review_language": own_language,
        "review_language_effective": own_language or language,
        **v23,
        "sources": sources,
        "inherited": inherited,
        "inherited_sources": {
            **inherited_sources, "review_language": language_source},
    }


def _row_to_out(
    row: RepoReviewPolicy, agents_effective: dict[str, dict] | None = None,
    workspace_language: tuple[str, str] = ("en", "install"),
    ws_defaults: dict[str, Any] | None = None,
) -> ReviewPolicyOut:
    return ReviewPolicyOut(
        repo_slug=row.repo_slug,
        enabled=row.enabled,
        prompt_template=row.prompt_template,
        folder_rules=_stored_folder_rules(row.folder_rules),
        department=row.department,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        architect_model=row.architect_model,
        security_model=row.security_model,
        quality_model=row.quality_model,
        tests_model=row.tests_model,
        verifier_model=row.verifier_model,
        performance_model=getattr(row, "performance_model", None),
        business_logic_model=getattr(row, "business_logic_model", None),
        agent_prompt_overrides=dict(row.agent_prompt_overrides or {}),
        agent_prompt_guidelines=dict(getattr(row, "agent_prompt_guidelines", None) or {}),
        agent_guidelines_extend=list(getattr(row, "agent_guidelines_extend", None) or []),
        # NULL for every row written before the column existed, and NULL is
        # exactly "inherit" — the same thing an absent key means at every
        # other layer of this chain.
        agent_llm_overrides=dict(row.agent_llm_overrides or {}),
        agents_effective=dict(agents_effective or {}),
        mcp_sources=list(row.mcp_sources or []),
        **_layered_fields(row, ws_defaults, workspace_language),
        **_catalog_fields(),
    )


def _row_to_list_item(
    row: RepoReviewPolicy, ws_defaults: dict[str, Any] | None = None,
) -> ReviewPolicyListItem:
    from src.review.review_defaults import resolve

    effective, _sources = resolve(row, ws_defaults, _install_defaults())
    return ReviewPolicyListItem(
        repo_slug=row.repo_slug,
        department=row.department,
        enabled=row.enabled,
        target_branches=list(effective["target_branches"] or []),
        has_custom_prompt=bool((row.prompt_template or "").strip()),
        folder_rules_count=len(row.folder_rules or []),
        disabled_agents=list(effective["disabled_agents"] or []),
        updated_at=row.updated_at,
    )


def _default_out(
    repo_slug: str, agents_effective: dict[str, dict] | None = None,
    workspace_language: tuple[str, str] = ("en", "install"),
    ws_defaults: dict[str, Any] | None = None,
) -> ReviewPolicyOut:
    """Synthetic 'default' policy when no row exists yet: every inheritable
    field inherits."""
    now = datetime.now(UTC)
    return ReviewPolicyOut(
        repo_slug=repo_slug,
        enabled=True,
        prompt_template="",
        folder_rules=[],
        department=None,
        created_at=now,
        updated_at=now,
        updated_by=None,
        architect_model=None,
        security_model=None,
        quality_model=None,
        tests_model=None,
        verifier_model=None,
        agent_prompt_overrides={},
        agent_prompt_guidelines={},
        agent_guidelines_extend=[],
        agent_llm_overrides={},
        agents_effective=dict(agents_effective or {}),
        mcp_sources=[],
        **_layered_fields(None, ws_defaults, workspace_language),
        **_catalog_fields(),
    )


def _require_repo_in_workspace(repo_slug: str, ws_id: str) -> None:
    """404 unless `repo_slug` is registered in `ws_id`.

    `repo_review_policies` is keyed by slug alone and the write path used to
    create a row for ANY slug it was handed. A policy row is only ever read
    for a repository of the workspace that owns it, so one written for a
    repository this workspace does not have is at best junk — and, since the
    slug is the primary key, it squatted the slug: the workspace that really
    registers that repository later was refused its own policy (404 above).
    Either registered spelling is accepted, as for team grants.
    """
    from src.api.auto_review import get_auto_review_store

    for cfg in get_auto_review_store().list_for_workspace(ws_id):
        if repo_slug in (cfg.repo_slug, cfg.full_name):
            return
    raise HTTPException(status_code=404, detail="Repository not registered in this workspace")


# ─── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=list[ReviewPolicyListItem])
async def list_policies(
    department: str | None = Query(default=None, max_length=128),
    search: str | None = Query(default=None, max_length=128),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[ReviewPolicyListItem]:
    """List existing policies + filter by department / fuzzy slug search."""
    stmt = (
        select(RepoReviewPolicy)
        .where(RepoReviewPolicy.workspace_id == ws_id)
        .order_by(RepoReviewPolicy.repo_slug)
    )
    if department:
        stmt = stmt.where(RepoReviewPolicy.department == department)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                RepoReviewPolicy.repo_slug.ilike(pattern),
                RepoReviewPolicy.department.ilike(pattern),
            )
        )
    ws_defaults = await _load_workspace_defaults(session, ws_id)
    rows = (await session.scalars(stmt)).all()
    return [_row_to_list_item(r, ws_defaults) for r in rows]


# NOTE: routes with `{repo_slug:path}` are greedy — `/foo/branches` would
# match the catch-all GET below and set repo_slug="foo/branches". So the
# specific-suffix routes MUST be registered above the catch-all.


@router.get("/overrides-summary", response_model=AgentOverridesSummary)
async def overrides_summary(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> AgentOverridesSummary:
    """agent → the repositories whose policy overrides its system prompt.

    What /admin/agents needs to say "overridden in N repositories": a
    workspace-wide prompt edited there does nothing for those repositories,
    and nothing on that page used to say so.

    Scoped twice: to the caller's ACTIVE workspace (a policy row of another
    tenant is never read), and to the repositories the caller may read — a
    repository whose team grants exclude them is not named, the same rule the
    per-repo routes apply. A caller who is not a member of the workspace
    (only reachable for the shared single-tenant default) gets 403.
    """
    from src.api.deps import enforce_repo_permission, workspace_role

    if not user.is_admin:
        role = await asyncio.to_thread(workspace_role, user.id, ws_id)
        if role is None:
            from src.deployment import is_multi_tenant
            if is_multi_tenant():
                raise HTTPException(status_code=403, detail="Not a member of this workspace")

    rows = (await session.scalars(
        select(RepoReviewPolicy)
        .where(RepoReviewPolicy.workspace_id == ws_id)
        .order_by(RepoReviewPolicy.repo_slug)
    )).all()

    out: dict[str, list[AgentPromptOverrideRepo]] = {
        agent: [] for agent in _OVERRIDABLE_AGENT_ORDER
    }
    guidelines_out: dict[str, list[AgentPromptOverrideRepo]] = {
        agent: [] for agent in _OVERRIDABLE_AGENT_ORDER
    }
    readable: dict[str, bool] = {}

    def _own(mapping: Any) -> set[str]:
        return {
            k for k, v in (mapping or {}).items()
            if k in _OVERRIDABLE_AGENTS and isinstance(v, str) and v.strip()
        }

    for row in rows:
        overrides = _own(row.agent_prompt_overrides)
        guidelines = _own(getattr(row, "agent_prompt_guidelines", None))
        if not overrides and not guidelines:
            continue
        if row.repo_slug not in readable:
            try:
                await enforce_repo_permission(row.repo_slug, user, "read", ws_id)
                readable[row.repo_slug] = True
            except HTTPException:
                readable[row.repo_slug] = False
        if not readable[row.repo_slug]:
            continue
        for agent in _OVERRIDABLE_AGENT_ORDER:
            entry = AgentPromptOverrideRepo(
                repo_slug=row.repo_slug, updated_at=row.updated_at)
            if agent in overrides:
                out[agent].append(entry)
            if agent in guidelines:
                guidelines_out[agent].append(entry)
    return AgentOverridesSummary(prompt_overrides=out,
                                 guideline_overrides=guidelines_out)


@router.get("/prompt-preview")
async def workspace_prompt_preview(
    agent: str = Query(default="defect", pattern=_PREVIEWABLE_PATTERN),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Dry-run of the prompt a repository with no settings of its own would
    send for `agent`: the workspace's replacement or the built-in, the
    workspace's guidelines, the workspace base instruction and the
    workspace-wide review rules. The Global scope's "Preview"."""
    ws_defaults = await _load_workspace_defaults(session, ws_id)
    try:
        from src.review.rules_store import effective_rules_for

        review_rules = await effective_rules_for(ws_id, None, session=session)
    except Exception as exc:  # noqa: BLE001 — a preview without them beats a 500
        logger.warning("prompt_preview_review_rules_unavailable ws=%s err=%s",
                       ws_id, exc)
        await session.rollback()
        review_rules = []
    memories_ok = await _may_preview_memories(user, ws_id, None)
    return await asyncio.to_thread(
        _compose_preview, agent, ws_id, None, None, ws_defaults, review_rules,
        memories_ok)


@router.get("/{repo_slug:path}/prompt-preview")
async def prompt_preview(
    repo_slug: str,
    # The pattern is BUILT from the roster, not spelled here. It was a literal
    # `^(architect|security|quality|tests)$` and the Phase-18 restructure left
    # it behind: `defect` and `contract` were refused 422 while `architect` and
    # `quality` were accepted and then crashed on a class that no longer
    # exists. Both halves measured on production — 422 for the live agents,
    # 500 for the dead ones — which is the whole endpoint dead either way.
    agent: str = Query(default="defect", pattern=_PREVIEWABLE_PATTERN),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Dry-run: compose the effective system_prompt + user_prompt_template
    for `agent` on `repo_slug`, exactly as the review runtime would build it.

    Uses a mock PR context (empty diff, no changed files) so no LLM call is
    made and the response is deterministic — useful for debugging why a
    prompt looks the way it does after all the layers stack up.

    Besides the joined text, `parts` lists the blocks in order — the agent's
    own prompt ("base") and every block appended to it — so the page can fold
    the long built-in part and highlight what the team added.
    """
    # First, before the policy row is loaded: a failure here rolls the
    # session back (see `_load_workspace_defaults`).
    ws_defaults = await _load_workspace_defaults(session, ws_id)
    # The review rules of THIS workspace (/admin/review-rules), composed as a
    # review composes them. Read in the caller's workspace only, so a foreign
    # slug previews no rules rather than another tenant's. Read FIRST: a
    # failure (a database the migration has not reached) is rolled back, and
    # a rollback would expire the policy row loaded below.
    try:
        from src.review.rules_store import effective_rules_for

        review_rules = await effective_rules_for(ws_id, repo_slug, session=session)
    except Exception as exc:  # noqa: BLE001 — a preview without them beats a 500
        logger.warning("prompt_preview_review_rules_unavailable ws=%s err=%s",
                       ws_id, exc)
        await session.rollback()
        review_rules = []

    row = await session.get(RepoReviewPolicy, repo_slug)
    if row is not None and row.workspace_id != ws_id:
        row = None  # another tenant's policy — never disclose; preview defaults
    memories_ok = await _may_preview_memories(user, ws_id, repo_slug)
    return await asyncio.to_thread(
        _compose_preview, agent, ws_id, repo_slug, row, ws_defaults, review_rules,
        memories_ok)


async def _may_preview_memories(user: User, ws_id: str, repo_slug: str | None) -> bool:
    """Do the previews show the team memories to this caller? Only to the
    people who may open /memories (editor, admin, owner) and, for a repository,
    only when they may read it: a memory is the team's own words, and a viewer
    or member must not read them through the prompt."""
    from src.api.deps import may_use_memories, readable_repo_slugs

    if not await may_use_memories(user, ws_id):
        return False
    if repo_slug is None:
        return True
    return repo_slug in await readable_repo_slugs(user, ws_id, [repo_slug])


def _compose_preview(
    agent: str, ws_id: str, repo_slug: str | None, row: Any,
    ws_defaults: dict[str, Any] | None, review_rules: list,
    show_memories: bool = True,
) -> dict[str, Any]:
    """Both previews' body — blocking (the workspace prompt layers live in
    the credential store). `row` None previews the workspace defaults.
    `show_memories` False leaves the team memories out of the prompt shown."""
    from src.review.agents.base import (
        AgentContext,
        LLMReviewAgent,
        compose_system_prompt_parts,
        team_guidelines_entries,
    )
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.policy_rules import render_policy_rules
    from src.review.prompt_guidelines import guidelines_source

    agent_overrides = dict(row.agent_prompt_overrides or {}) if row else {}
    agent_guidelines = dict(getattr(row, "agent_prompt_guidelines", None) or {}) if row else {}
    guidelines_extend = list(getattr(row, "agent_guidelines_extend", None) or []) if row else []
    review_language = getattr(row, "review_language", None) if row else None
    base_instruction = _preview_base_instruction(row, ws_defaults)
    source = _prompt_source(agent, agent_overrides, ws_id)
    preview_pr = _preview_pr(repo_slug or "(workspace)")

    def _out(parts: list, user_template: str, ctx: AgentContext,
             system_prompt: str) -> dict[str, Any]:
        return {
            "agent": agent,
            "system_prompt": system_prompt,
            "user_prompt_template": user_template,
            "prompt_source": source,
            "guidelines_source": guidelines_source(team_guidelines_entries(ctx, agent)),
            "parts": [{"kind": p.kind, "source": p.source, "text": p.text} for p in parts],
        }

    if agent == "verifier":
        # The verifier reads no repo rules and is handed the findings, not a
        # diff template — its prompt is the system prompt and nothing else.
        from src.review.agents.verifier import (
            verifier_system_prompt,
            verifier_system_prompt_parts,
        )

        ctx = AgentContext(
            pull_request=preview_pr,
            repo_agent_prompts=agent_overrides,
            repo_agent_guidelines=agent_guidelines,
            repo_guidelines_extend=guidelines_extend,
            workspace_id=ws_id,
            base_instruction=base_instruction,
        )
        return _out(verifier_system_prompt_parts(ctx), "", ctx,
                    verifier_system_prompt(ctx))

    # ASKED OF THE ORCHESTRATOR, not restated. The previous version listed
    # four agent classes by name, so a renamed roster left this endpoint
    # importing classes that no longer existed — a 500 that no test caught,
    # because the import is lazy and nothing exercised the route.
    registry = {
        a.name: a for a in ReviewOrchestrator._default_agents()
        if isinstance(a, LLMReviewAgent)
    }
    a = registry.get(agent)
    if a is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no previewable agent {agent!r} — the LLM agents are: "
                f"{', '.join(sorted(registry))}"
            ),
        )

    # The renderer a review uses, with every rule shown (there is no PR to
    # match a pattern against) and each targeted rule only in its agents'
    # prompts — the preview answers "what will THIS agent be told".
    rendered = render_policy_rules(
        {
            "prompt_template": (row.prompt_template if row else "") or "",
            "folder_rules": list(row.folder_rules or []) if row else [],
            "review_rules": review_rules,
            "memories": (_preview_memories(ws_id, repo_slug, row, ws_defaults)
                         if show_memories else []),
        },
        None, match_files=False,
    )
    ctx = AgentContext(
        pull_request=preview_pr,
        custom_rules=rendered.shared,
        agent_custom_rules=rendered.per_agent,
        repo_agent_prompts=agent_overrides,
        repo_agent_guidelines=agent_guidelines,
        repo_guidelines_extend=guidelines_extend,
        workspace_id=ws_id,
        review_language=review_language,
        base_instruction=base_instruction,
    )
    parts = compose_system_prompt_parts(
        agent_name=agent,
        default_system=a.system_prompt,
        context=ctx,
    )
    body = _out(parts, a.user_prompt_template, ctx,
                "\n\n".join(p.text for p in parts))
    # The memories the prompt above carries (ids), and how many matching ones
    # the character budget left out — the page's "what will it be told".
    body["memories_used"] = rendered.memories_used
    body["memories_omitted"] = rendered.memories_omitted
    return body


def _preview_memories(
    ws_id: str, repo_slug: str | None, row: Any, ws_defaults: dict[str, Any] | None,
) -> list[dict]:
    """The team memories a review of this repository would be told: the
    active ones, unless the repository (else the workspace) switched them
    off — the order `merge_policy` gives a review. Blocking."""
    from src.review.memories import load_active_sync

    own = getattr(row, "memories_enabled", None) if row is not None else None
    if own is None:
        own = (ws_defaults or {}).get("memories_enabled")
    if own is False:
        return []
    return load_active_sync(ws_id, repo_slug)


def _preview_base_instruction(row: Any, ws_defaults: dict[str, Any] | None) -> str:
    """The base instruction a review of this repository would carry: the
    repository's own, else the workspace default — the order
    `merge_policy` gives a review — clamped exactly as the orchestrator
    clamps it. Read with getattr/.get because the setting is newer than
    some rows (and than the code that may have loaded them)."""
    from src.review.agents.base import clamp_base_instruction

    own = getattr(row, "base_instruction", None) if row is not None else None
    if own is None:
        own = (ws_defaults or {}).get("base_instruction")
    return clamp_base_instruction(own)


def _preview_pr(repo_slug: str):
    from src.review.models import PullRequest

    return PullRequest(
        provider="preview", repo=repo_slug, number=0, title="(preview)",
        description="", author="", base_ref="main", base_sha="",
        head_ref="preview", head_sha="", state="open", url="",
    )


def _prompt_source(agent: str, repo_overrides: dict, workspace_id: str) -> str:
    """Which layer the agent's base system prompt comes from:
    "repo" | "workspace" | "builtin" — the precedence of
    `_compose_effective_system_prompt`, said in one word. Blocking."""
    if str(repo_overrides.get(agent) or "").strip():
        return "repo"
    try:
        from src.api.routers.agents import _load_override
        if _load_override(agent, workspace_id) is not None:
            return "workspace"
    except Exception:  # noqa: BLE001
        pass
    return "builtin"


def _clone_branches(repo_path: Any) -> tuple[list[str], str | None]:
    """Branch names the local clone knows, and its origin/HEAD.

    The clone is `--single-branch`, so this is usually one name — the reason
    the provider is asked first. Kept as the fallback for an install with no
    token saved for the provider (a clone made from a public URL).
    """
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return [], None
    result = subprocess.run(
        ["git", "-C", str(repo_path), "for-each-ref",
         "--format=%(refname:short)", "refs/heads/", "refs/remotes/origin/"],
        capture_output=True, text=True, timeout=10,
    )
    names: set[str] = set()
    for raw in result.stdout.splitlines():
        b = raw.strip()
        if not b:
            continue
        if b.startswith("origin/"):
            b = b[len("origin/"):]
        if b in ("HEAD",) or "/HEAD" in b:
            continue
        names.add(b)
    default = None
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_path), "symbolic-ref",
             "refs/remotes/origin/HEAD", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0:
            default = head.stdout.strip().removeprefix("origin/") or None
    except Exception:  # noqa: BLE001
        pass
    ordered = sorted(names, key=str.lower)
    if default in names:
        ordered.remove(default)
        ordered.insert(0, default)
    return ordered, default


def _provider_branches(registered: Any, user: User, q: str, limit: int) -> Any:
    """A `BranchPage` from the provider, or None when there is no token or
    the provider failed — the caller then falls back to the clone."""
    from src.credentials import resolve_git_credential
    from src.repos.branches import branch_page

    try:
        creds = resolve_git_credential(
            registered.provider, user_id=user.id,
            workspace_id=registered.workspace_id,
        )
    except Exception as exc:  # noqa: BLE001 — an unreadable store is "no token"
        logger.warning("branch_credential_unreadable repo=%s err=%s",
                       registered.repo_slug, type(exc).__name__)
        return None
    if creds is None or not registered.full_name:
        return None
    email = str((creds.metadata or {}).get("atlassian_email") or "")
    try:
        from src.sync.gitlab_instance import gitlab_kwarg

        return branch_page(registered.provider, registered.full_name,
                           creds.secret, email, q=q, limit=limit,
                           **gitlab_kwarg(registered.provider, creds))
    except Exception as exc:  # noqa: BLE001 — never 500 a picker
        logger.warning("branch_list_failed repo=%s provider=%s err=%s",
                       registered.repo_slug, registered.provider,
                       type(exc).__name__)
        return None


@router.get("/{repo_slug:path}/branches", response_model=RepoBranchesOut)
async def list_branches(
    repo_slug: str,
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=1000),
) -> RepoBranchesOut:
    """Branches for the 'target branches' picker.

    From the provider when the workspace has a token for it — every page,
    searchable by `q`, at most `limit` names (src/repos/branches.py). Falls
    back to the local clone, which is `--single-branch` and so usually knows
    one branch; an empty list still lets the user type names by hand.

    Only for a repository registered to the caller's workspace (the same
    lookup the /api/repos/{slug}/* routes use). This route ran `git` in
    `repos_dir / repo_slug` for any string — another tenant's clone, or with
    `{repo_slug:path}`, any directory `../` could reach. An unregistered,
    foreign or unaddressable repo gets 404, like the /api/repos/{slug}/* routes.

    Under multi_tenant the workspace row is the only authority: the
    user-keyed fallback (`store.get(user.id, slug)`) ignores the workspace, so
    someone removed from workspace B kept reading B's clone through the row
    they registered there. single_tenant keeps the fallback.
    """
    from src.api.auto_review import get_auto_review_store
    from src.config import get_settings, is_valid_repo_slug
    from src.deployment import is_multi_tenant
    from src.repos.branches import normalize_query, search_names

    # Unknown, foreign and unaddressable slugs all get the same 404 the
    # /api/repos/{slug}/* routes give: no tenant learns another's repo exists.
    not_found = HTTPException(status_code=404, detail="Repo not registered")
    if not is_valid_repo_slug(repo_slug):
        raise not_found
    store = get_auto_review_store()
    registered = store.get_in_workspace(ws_id, repo_slug)
    if registered is None and not is_multi_tenant():
        registered = store.get(user.id, repo_slug)
    if registered is None:
        raise not_found

    # Called directly (tests, internal callers) the Query() defaults arrive
    # as FieldInfo objects, not values.
    q = normalize_query(q if isinstance(q, str) else "")
    limit = max(1, min(limit if isinstance(limit, int) else 100, 1000))

    page = await asyncio.to_thread(_provider_branches, registered, user, q, limit)
    if page is not None:
        return RepoBranchesOut(
            repo_slug=repo_slug, branches=page.branches,
            default_branch=page.default_branch, total=page.total,
            truncated=page.truncated, source="provider",
        )

    try:
        names, default = await asyncio.to_thread(
            _clone_branches, get_settings().repo_path(repo_slug))
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("list_branches_failed repo=%s err=%s", repo_slug, exc)
        names, default = [], None
    matched = search_names(names, q, default)
    return RepoBranchesOut(
        repo_slug=repo_slug, branches=matched[:limit], default_branch=default,
        total=len(matched), truncated=False,
        source="clone" if names else "none",
    )


@router.get("/{repo_slug:path}", response_model=ReviewPolicyOut)
async def get_policy(
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ReviewPolicyOut:
    """Return the policy for `repo_slug` in the caller's workspace. If no row
    exists (or it belongs to another tenant) — synthesize defaults so the UI can
    render the form without disclosing another workspace's config."""
    ws_defaults = await _load_workspace_defaults(session, ws_id)
    row = await session.get(RepoReviewPolicy, repo_slug)
    ws_language = await asyncio.to_thread(_workspace_review_language_layer, ws_id)
    if row is None or row.workspace_id != ws_id:
        # No policy of its own — but every agent still runs with SOMETHING,
        # and this page is where an operator comes to find out what.
        return _default_out(
            repo_slug, await _effective_agents_for_display({}, ws_id),
            workspace_language=ws_language, ws_defaults=ws_defaults,
        )
    return _row_to_out(
        row,
        await _effective_agents_for_display(
            _policy_view(
                _model_columns(row), dict(row.agent_llm_overrides or {}),
            ),
            ws_id,
        ),
        workspace_language=ws_language, ws_defaults=ws_defaults,
    )


@router.put("/{repo_slug:path}", response_model=ReviewPolicyOut)
async def upsert_policy(
    repo_slug: str,
    payload: ReviewPolicyIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    _perm: User = Depends(require_repo_permission("review")),
    ws_id: str = Depends(current_workspace_id),
) -> ReviewPolicyOut:
    """Create or fully replace the policy for `repo_slug` in the caller's ws.

    Three gates, each answering a different question: editor/admin/owner of
    the workspace (the prompt template, folder rules and per-agent prompt
    overrides are prompts — the editor's job, not a member's); the repository
    is this workspace's; and the caller's teams grant `review` on it.
    """
    await asyncio.to_thread(_require_repo_in_workspace, repo_slug, ws_id)
    ws_defaults = await _load_workspace_defaults(session, ws_id)
    row = await session.get(RepoReviewPolicy, repo_slug)
    if row is not None and row.workspace_id != ws_id:
        # Policy rows are PK'd by repo_slug alone; refuse to overwrite one owned
        # by another workspace (cross-tenant policy/mcp_sources tampering).
        raise HTTPException(status_code=404, detail="Policy not found in this workspace")

    # Shaped and checked BEFORE the row is touched, so a refusal leaves no
    # half-written policy and no empty row behind for a repo that had none.
    # The check is against the models this request is SAVING — see
    # `_validate_agent_llm_overrides` for why the model being replaced is the
    # wrong one to ask about.
    agent_llm_overrides = _agent_llm_overrides_from_payload(
        payload.agent_llm_overrides,
        None if row is None else row.agent_llm_overrides,
    )
    ignore_globs = _ignore_globs_from_payload(payload.ignore_globs)
    comment_min_severity = _comment_min_severity_from_payload(
        payload.comment_min_severity)
    folder_rules = _folder_rules_from_payload(payload.folder_rules)
    review_language = _review_language_from_payload(payload.review_language)
    target_branches = target_branches_from_payload(payload.target_branches)
    v23_updates = v23_updates_from_payload(payload)
    if payload.agent_llm_overrides is not None and agent_llm_overrides:
        # Only what this request actually sent. Re-checking a map the payload
        # never mentioned would let a model change on this page lock an
        # operator out of every other field on it, with no control on screen to
        # undo the combination — and /api/llm/config draws the same line.
        # The models this save LEAVES in place: the 2.3.0 model columns keep
        # their stored value when the request does not name them.
        saving_models = _model_columns(payload)
        for field in _KEPT_MODEL_FIELDS:
            if field not in payload.model_fields_set and row is not None:
                saving_models[field] = getattr(row, field, None)
        await asyncio.to_thread(
            _validate_agent_llm_overrides,
            agent_llm_overrides,
            _policy_view(saving_models, agent_llm_overrides),
            ws_id,
        )

    if row is None:
        row = RepoReviewPolicy(repo_slug=repo_slug, workspace_id=ws_id)
        session.add(row)

    fields = payload.model_fields_set
    row.enabled = payload.enabled
    row.prompt_template = payload.prompt_template
    # Three states: absent keeps what is stored, null inherits the workspace
    # review defaults, a list ([] = every branch) is this repository's own.
    if "target_branches" in fields:
        row.target_branches = target_branches
    row.folder_rules = folder_rules
    row.department = payload.department
    row.updated_by = user.email
    row.architect_model = payload.architect_model
    row.security_model = payload.security_model
    row.quality_model = payload.quality_model
    row.tests_model = payload.tests_model
    row.verifier_model = payload.verifier_model
    # Absent keeps (the page predates these two), null clears, a string pins.
    for field in _KEPT_MODEL_FIELDS:
        if field in fields:
            setattr(row, field, (getattr(payload, field) or "").strip() or None)
    row.agent_prompt_overrides = {
        k: v for k, v in (payload.agent_prompt_overrides or {}).items()
        # From the roster plus the verifier — spelled out once, in
        # `_OVERRIDABLE_AGENTS`. This was a literal set holding the retired
        # names, so a per-repo prompt override for `defect` or `contract` —
        # the two boxes the policy page now renders — was dropped in silence
        # on save.
        if k in _OVERRIDABLE_AGENTS
        and isinstance(v, str) and v.strip()
    }
    # Team guidelines (ADDED to the prompt). Absent keeps what is stored; a
    # map replaces it whole. Length was refused by the schema (422).
    if payload.agent_prompt_guidelines is not None:
        row.agent_prompt_guidelines = {
            k: v.strip() for k, v in payload.agent_prompt_guidelines.items()
            if k in _OVERRIDABLE_AGENTS and isinstance(v, str) and v.strip()
        }
    if payload.agent_guidelines_extend is not None:
        row.agent_guidelines_extend = [
            a for a in dict.fromkeys(payload.agent_guidelines_extend)
            if a in _OVERRIDABLE_AGENTS
        ]
    row.agent_llm_overrides = agent_llm_overrides
    # Sanitize MCP source entries — drop anything missing name/url.
    row.mcp_sources = [
        {
            "name": str(m["name"]),
            "url": str(m["url"]),
            "auth_type": str(m.get("auth_type", "none")),
            "api_key_ref": m.get("api_key_ref"),
            "allowed_tools": list(m.get("allowed_tools") or []),
            "trigger_patterns": list(m.get("trigger_patterns") or []),
        }
        for m in (payload.mcp_sources or [])
        if isinstance(m, dict) and m.get("name") and m.get("url")
    ]
    # Drop unknown names so a typo can never silently disable nothing (or,
    # worse, look like it disabled something in the UI). Absent keeps, null
    # inherits the workspace review defaults, a list ([] = every agent runs)
    # is this repository's own answer.
    if "disabled_agents" in fields:
        row.disabled_agents = (
            None if payload.disabled_agents is None
            else [a for a in dict.fromkeys(payload.disabled_agents)
                  if a in TOGGLEABLE_AGENTS]
        )
    # Only when the request said something: a client without this control
    # — the policy page today — must not reset a list it never rendered.
    if "suppressed_rules" in payload.model_fields_set:
        row.suppressed_rules = _suppressed_rules_from_payload(payload.suppressed_rules)
    # Same three-state courtesy: absent keeps what is stored, explicit null
    # goes back to inheriting the install default, true/false is a decision.
    # A client that cannot render this control must not answer for it.
    if "verifier_enabled" in payload.model_fields_set:
        row.verifier_enabled = (
            None if payload.verifier_enabled is None
            else bool(payload.verifier_enabled)
        )
    # Same courtesy for the two review-output settings: a client that does
    # not render them must not reset them.
    if "ignore_globs" in payload.model_fields_set:
        row.ignore_globs = ignore_globs
    if "comment_min_severity" in payload.model_fields_set:
        row.comment_min_severity = comment_min_severity
    # Review output. Absent keeps what is stored; null inherits the
    # workspace review default (then on); true/false is this repo's own.
    if "summary_enabled" in fields:
        row.summary_enabled = payload.summary_enabled
    if "started_comment_enabled" in fields:
        row.started_comment_enabled = payload.started_comment_enabled
    if "summary_instructions" in fields:
        row.summary_instructions = (payload.summary_instructions or "").strip() or None
    if "review_language" in fields:
        row.review_language = review_language
    if "max_inline_comments" in fields:
        row.max_inline_comments = payload.max_inline_comments
    # The 2.3.0 settings, validated above with the workspace layer's rules.
    for name, value in v23_updates.items():
        setattr(row, name, value)

    await session.commit()
    await session.refresh(row)
    from src.security import audit

    # The SHAPE of the change (which fields the request named), never the
    # values: prompts and instructions are operator text, and the audit
    # trail is exported.
    audit.record_action(
        action="review_policy.changed", actor=user.email, actor_id=user.id,
        workspace_id=ws_id, target=repo_slug, ip=client_ip(request),
        detail={"fields": sorted(fields)},
    )
    logger.info(
        "review_policy_upserted repo=%s by=%s enabled=%s branches=%s "
        "folder_rules=%d disabled_agents=%s agent_llm_overrides=%s "
        "suppressed_rules=%s",
        repo_slug, user.email, row.enabled,
        "inherit" if row.target_branches is None else len(row.target_branches),
        len(row.folder_rules),
        "inherit" if row.disabled_agents is None
        else (",".join(row.disabled_agents) or "none"),
        ",".join(sorted(row.agent_llm_overrides or {})) or "-",
        "inherit" if row.suppressed_rules is None
        else (",".join(row.suppressed_rules) or "none"),
    )
    return _row_to_out(
        row,
        await _effective_agents_for_display(
            _policy_view(
                _model_columns(row), dict(row.agent_llm_overrides or {}),
            ),
            ws_id,
        ),
        workspace_language=await asyncio.to_thread(
            _workspace_review_language_layer, ws_id),
        ws_defaults=ws_defaults,
    )


@router.delete("/{repo_slug:path}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_policy(
    repo_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_prompt_editor),
    # `review`, the same as saving: PUT replaces the whole policy, so a reset
    # demanding more than a save only made the editor's undo the hard part.
    _perm: User = Depends(require_repo_permission("review")),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    """Reset to default (delete the row) — only within the caller's workspace."""
    await asyncio.to_thread(_require_repo_in_workspace, repo_slug, ws_id)
    row = await session.get(RepoReviewPolicy, repo_slug)
    if row is None or row.workspace_id != ws_id:
        return
    await session.delete(row)
    await session.commit()
    logger.info("review_policy_reset repo=%s by=%s", repo_slug, user.email)
    from src.security import audit

    audit.record_action(
        action="review_policy.reset", actor=user.email, actor_id=user.id,
        workspace_id=ws_id, target=repo_slug, ip=client_ip(request),
    )

