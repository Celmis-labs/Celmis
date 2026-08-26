"""Per-repo AI-reviewer policies (Stage 10).

Endpoints:
    GET    /api/review-policies                 — list (filter by department + search)
    GET    /api/review-policies/{slug}          — full detail (defaults if no row)
    PUT    /api/review-policies/{slug}          — upsert
    DELETE /api/review-policies/{slug}          — reset to default (delete row)
    GET    /api/review-policies/{slug}/branches — discover branches from local clone

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
import subprocess
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user, require_repo_permission
from src.api.schemas import (
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


#: `^(defect|contract|security)$` — computed at import so FastAPI can compile
#: it into the route's schema, which is where a hand-written alternation could
#: never keep up with a rename.
_PREVIEWABLE_PATTERN = "^(" + "|".join(_llm_agent_names()) + ")$"

#: Agents a per-repo prompt override may name. The finders, plus the verifier:
#: it takes a system prompt like the rest even though it finds nothing itself.
_OVERRIDABLE_AGENTS = frozenset({*_llm_agent_names(), "verifier"})


TOGGLEABLE_AGENTS = (
    "defect", "contract", "security", "structural", "cve",
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




def _row_to_out(
    row: RepoReviewPolicy, agents_effective: dict[str, dict] | None = None,
) -> ReviewPolicyOut:
    return ReviewPolicyOut(
        repo_slug=row.repo_slug,
        enabled=row.enabled,
        prompt_template=row.prompt_template,
        target_branches=list(row.target_branches or []),
        folder_rules=[FolderRule(**fr) for fr in (row.folder_rules or [])],
        department=row.department,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        architect_model=row.architect_model,
        security_model=row.security_model,
        quality_model=row.quality_model,
        tests_model=row.tests_model,
        verifier_model=row.verifier_model,
        agent_prompt_overrides=dict(row.agent_prompt_overrides or {}),
        # NULL for every row written before the column existed, and NULL is
        # exactly "inherit" — the same thing an absent key means at every
        # other layer of this chain.
        agent_llm_overrides=dict(row.agent_llm_overrides or {}),
        agents_effective=dict(agents_effective or {}),
        mcp_sources=list(row.mcp_sources or []),
        disabled_agents=list(row.disabled_agents or []),
        suppressed_rules=(
            None if row.suppressed_rules is None else list(row.suppressed_rules)
        ),
        suppressed_rules_effective=(
            _default_suppressed_rules() if row.suppressed_rules is None
            else list(row.suppressed_rules)
        ),
        verifier_enabled=row.verifier_enabled,
        verifier_enabled_effective=(
            # "verifier" in the agent deny-list is the old spelling of off and
            # still wins, the same order `_verifier_enabled` applies in the
            # orchestrator. Two readers of one rule, so the rule is stated the
            # same way in both.
            False if "verifier" in (row.disabled_agents or [])
            else _verifier_default() if row.verifier_enabled is None
            else bool(row.verifier_enabled)
        ),
    )


def _row_to_list_item(row: RepoReviewPolicy) -> ReviewPolicyListItem:
    return ReviewPolicyListItem(
        repo_slug=row.repo_slug,
        department=row.department,
        enabled=row.enabled,
        target_branches=list(row.target_branches or []),
        has_custom_prompt=bool((row.prompt_template or "").strip()),
        folder_rules_count=len(row.folder_rules or []),
        disabled_agents=list(row.disabled_agents or []),
        updated_at=row.updated_at,
    )


def _default_out(
    repo_slug: str, agents_effective: dict[str, dict] | None = None,
) -> ReviewPolicyOut:
    """Synthetic 'default' policy when no row exists yet."""
    now = datetime.now(UTC)
    return ReviewPolicyOut(
        repo_slug=repo_slug,
        enabled=True,
        prompt_template="",
        target_branches=[],
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
        agent_llm_overrides={},
        agents_effective=dict(agents_effective or {}),
        mcp_sources=[],
        disabled_agents=[],
        suppressed_rules=None,
        suppressed_rules_effective=_default_suppressed_rules(),
        verifier_enabled=None,
        verifier_enabled_effective=_verifier_default(),
    )


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
    rows = (await session.scalars(stmt)).all()
    return [_row_to_list_item(r) for r in rows]


# NOTE: routes with `{repo_slug:path}` are greedy — `/foo/branches` would
# match the catch-all GET below and set repo_slug="foo/branches". So the
# specific-suffix routes MUST be registered above the catch-all.


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
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, str]:
    """Dry-run: compose the effective system_prompt + user_prompt_template
    for `agent` on `repo_slug`, exactly as the review runtime would build it.

    Uses a mock PR context (empty diff, no changed files) so no LLM call is
    made and the response is deterministic — useful for debugging why a
    prompt looks the way it does after all the layers stack up.
    """
    from src.review.agents.base import (
        AgentContext,
        LLMReviewAgent,
        _compose_effective_system_prompt,
    )
    from src.review.models import PullRequest
    from src.review.orchestrator import ReviewOrchestrator

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

    row = await session.get(RepoReviewPolicy, repo_slug)
    if row is not None and row.workspace_id != ws_id:
        row = None  # another tenant's policy — never disclose; preview defaults
    prompt_template = (row.prompt_template if row else "") or ""
    folder_rules = list(row.folder_rules or []) if row else []
    agent_overrides = dict(row.agent_prompt_overrides or {}) if row else {}

    custom_rules_parts: list[str] = []
    if prompt_template.strip():
        custom_rules_parts.append("**Repo-level rules (from admin panel):**\n" + prompt_template.strip())
    for fr in folder_rules:
        pat = fr.get("pattern", "")
        prompt = (fr.get("prompt") or "").strip()
        if pat and prompt:
            custom_rules_parts.append(f"**Folder rule — `{pat}`:**\n{prompt}")
    custom_rules = "\n\n".join(custom_rules_parts)

    fake_pr = PullRequest(
        provider="preview", repo=repo_slug, number=0, title="(preview)",
        description="", author="", base_ref="main", base_sha="",
        head_ref="preview", head_sha="", state="open", url="",
    )
    ctx = AgentContext(
        pull_request=fake_pr,
        custom_rules=custom_rules,
        repo_agent_prompts=agent_overrides,
        workspace_id=ws_id,
    )
    effective_system = _compose_effective_system_prompt(
        agent_name=agent,
        default_system=a.system_prompt,
        context=ctx,
    )
    return {
        "agent": agent,
        "system_prompt": effective_system,
        "user_prompt_template": a.user_prompt_template,
    }


@router.get("/{repo_slug:path}/branches", response_model=RepoBranchesOut)
async def list_branches(
    repo_slug: str,
    _user: User = Depends(get_current_user),
) -> RepoBranchesOut:
    """Discover branches from the local clone. Used to populate the
    'target branches' checkbox list in the UI.

    Falls back to an empty list if the repo is not cloned yet (the user can
    still type branch names by hand, or run `analyzer sync` to populate).
    """
    from src.config import get_settings

    settings = get_settings()
    repo_path = settings.repo_path(repo_slug)
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return RepoBranchesOut(repo_slug=repo_slug, branches=[], default_branch=None)

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "for-each-ref",
             "--format=%(refname:short)", "refs/heads/", "refs/remotes/origin/"],
            capture_output=True, text=True, timeout=10,
        )
        raw = [b.strip() for b in result.stdout.splitlines() if b.strip()]
        names: set[str] = set()
        for b in raw:
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

        return RepoBranchesOut(
            repo_slug=repo_slug,
            branches=sorted(names),
            default_branch=default,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("list_branches_failed repo=%s err=%s", repo_slug, exc)
        return RepoBranchesOut(repo_slug=repo_slug, branches=[], default_branch=None)


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
    row = await session.get(RepoReviewPolicy, repo_slug)
    if row is None or row.workspace_id != ws_id:
        # No policy of its own — but every agent still runs with SOMETHING,
        # and this page is where an operator comes to find out what.
        return _default_out(
            repo_slug, await _effective_agents_for_display({}, ws_id),
        )
    return _row_to_out(
        row,
        await _effective_agents_for_display(
            _policy_view(
                _model_columns(row), dict(row.agent_llm_overrides or {}),
            ),
            ws_id,
        ),
    )


@router.put("/{repo_slug:path}", response_model=ReviewPolicyOut)
async def upsert_policy(
    repo_slug: str,
    payload: ReviewPolicyIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    _perm: User = Depends(require_repo_permission("review")),
    ws_id: str = Depends(current_workspace_id),
) -> ReviewPolicyOut:
    """Create or fully replace the policy for `repo_slug` in the caller's ws."""
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
    if payload.agent_llm_overrides is not None and agent_llm_overrides:
        # Only what this request actually sent. Re-checking a map the payload
        # never mentioned would let a model change on this page lock an
        # operator out of every other field on it, with no control on screen to
        # undo the combination — and /api/llm/config draws the same line.
        await asyncio.to_thread(
            _validate_agent_llm_overrides,
            agent_llm_overrides,
            _policy_view(_model_columns(payload), agent_llm_overrides),
            ws_id,
        )

    if row is None:
        row = RepoReviewPolicy(repo_slug=repo_slug, workspace_id=ws_id)
        session.add(row)

    row.enabled = payload.enabled
    row.prompt_template = payload.prompt_template
    row.target_branches = list(payload.target_branches)
    row.folder_rules = [
        {"pattern": fr.pattern, "prompt": fr.prompt} for fr in payload.folder_rules
    ]
    row.department = payload.department
    row.updated_by = user.email
    row.architect_model = payload.architect_model
    row.security_model = payload.security_model
    row.quality_model = payload.quality_model
    row.tests_model = payload.tests_model
    row.verifier_model = payload.verifier_model
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
    # worse, look like it disabled something in the UI).
    row.disabled_agents = [
        a for a in dict.fromkeys(payload.disabled_agents or [])
        if a in TOGGLEABLE_AGENTS
    ]
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

    await session.commit()
    await session.refresh(row)
    logger.info(
        "review_policy_upserted repo=%s by=%s enabled=%s branches=%d "
        "folder_rules=%d disabled_agents=%s agent_llm_overrides=%s "
        "suppressed_rules=%s",
        repo_slug, user.email, row.enabled,
        len(row.target_branches), len(row.folder_rules),
        ",".join(row.disabled_agents) or "-",
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
    )


@router.delete("/{repo_slug:path}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_policy(
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    _perm: User = Depends(require_repo_permission("admin")),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    """Reset to default (delete the row) — only within the caller's workspace."""
    row = await session.get(RepoReviewPolicy, repo_slug)
    if row is None or row.workspace_id != ws_id:
        return
    await session.delete(row)
    await session.commit()
    logger.info("review_policy_reset repo=%s by=%s", repo_slug, user.email)

