"""The review rules store — the one module that reads and writes `review_rules`.

Stable API (the HTTP router, the rules generator and the Celmis agent's chat
actions all call these; none of them touches the table directly):

    list_rules(ws, repo_slug=None, status=None, *, scope=None, origin=None, q=None)
    effective_rules_for(ws, repo_slug)                  -> the rules a review applies
    propose_rules(ws, repo_slug, rules, origin, created_by) -> list[int]   (pending)
    create_rule(ws, *, repo_slug, title, instructions, ...) -> dict
    update_rule(ws, rule_id, changes, actor)            -> dict | None
    set_status(ws, ids, status, actor)                  -> list[int]
    delete_rules(ws, ids, actor)                        -> list[int]
    add_from_library(ws, repo_slug, library_ids, status, actor) -> list[int]
    load_active_rules_sync(ws, repo_slug)               -> list[dict]  (blocking)

Every function is scoped by the workspace id it is handed — a row of another
workspace is invisible to it, by id or otherwise. Permission checks (who may
write, whether the caller may touch this repository) belong to the caller:
the router does them per request, an agent action per actor. The async
functions take an optional `session`; without one they open their own.

How a review composes its rules (`compose_effective`): every ACTIVE rule of
the workspace (repo_slug NULL), then every ACTIVE rule of the repository; a
repository rule whose title equals a workspace rule's (case folded) replaces
it. Pending and rejected rules never reach a review.

Validation (`validate_rule`) is here, not in the router, so an agent action
cannot write what the page would refuse: title ≤ 200, instructions ≤ 2000,
severity info|warning|error|critical, agents a subset of the LLM finders,
examples ≤ 2000 each, a path glob without control characters.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any

logger = logging.getLogger(__name__)

STATUSES: tuple[str, ...] = ("active", "pending", "rejected")
ORIGINS: tuple[str, ...] = (
    "manual", "library", "generated", "imported", "agent", "learned")
SEVERITIES: tuple[str, ...] = ("info", "warning", "error", "critical")

MAX_TITLE = 200
MAX_INSTRUCTIONS = 2000
MAX_EXAMPLE = 2000
MAX_GLOB = 500
MAX_RATIONALE = 2000
MAX_SOURCE_REF = 500
#: Rules one scope (the workspace, or one repository) may hold. A prompt is
#: finite; a thousand rules is a store nobody reviews.
MAX_RULES_PER_SCOPE = 300

#: Fields `update_rule` may change.
EDITABLE_FIELDS: tuple[str, ...] = (
    "title", "instructions", "path_glob", "severity", "agents",
    "examples_good", "examples_bad", "status", "rationale",
)


class RuleValidationError(ValueError):
    """A rule the store refuses; `field` names the offending field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class RuleConflictError(ValueError):
    """A rule with this title already exists in this scope."""


class RuleLimitError(ValueError):
    """The scope already holds MAX_RULES_PER_SCOPE rules."""


# ─── Validation ──────────────────────────────────────────────────────


def target_agents() -> tuple[str, ...]:
    """The agents a rule may be addressed to: the LLM finders the
    orchestrator dispatches (asked of the roster, never listed here)."""
    from src.review.agents.base import LLMReviewAgent
    from src.review.orchestrator import ReviewOrchestrator

    return tuple(
        a.name for a in ReviewOrchestrator._default_agents()
        if isinstance(a, LLMReviewAgent)
    )


def _text(value: Any, field: str, limit: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise RuleValidationError(field, "is required")
        return None
    if not isinstance(value, str):
        raise RuleValidationError(field, "must be text")
    text = value.strip()
    if not text:
        if required:
            raise RuleValidationError(field, "is required")
        return None
    if len(text) > limit:
        raise RuleValidationError(field, f"is {len(text)} characters, the limit is {limit}")
    return text


def validate_rule(data: dict, *, partial: bool = False) -> dict:
    """The stored shape of `data`, or RuleValidationError.

    `partial=True` validates only the keys present (an update)."""
    out: dict[str, Any] = {}

    def has(key: str) -> bool:
        return not partial or key in data

    if has("title"):
        title = _text(data.get("title"), "title", MAX_TITLE, required=True)
        out["title"] = " ".join(str(title).split())
    if has("instructions"):
        out["instructions"] = _text(
            data.get("instructions"), "instructions", MAX_INSTRUCTIONS, required=True)
    if has("path_glob"):
        glob = _text(data.get("path_glob"), "path_glob", MAX_GLOB)
        if glob and any(ord(ch) < 32 and ch not in " \t" for ch in glob):
            raise RuleValidationError("path_glob", "contains control characters")
        out["path_glob"] = glob
    if has("severity"):
        severity = str(data.get("severity") or "warning").strip().lower()
        if severity not in SEVERITIES:
            raise RuleValidationError(
                "severity", f"{severity!r} — expected one of {', '.join(SEVERITIES)}")
        out["severity"] = severity
    if has("agents"):
        raw = data.get("agents") or []
        if not isinstance(raw, list | tuple):
            raise RuleValidationError("agents", "must be a list")
        agents = list(dict.fromkeys(str(a).strip() for a in raw if str(a).strip()))
        known = target_agents()
        unknown = [a for a in agents if a not in known]
        if unknown:
            raise RuleValidationError("agents", (
                f"unknown agent(s) {', '.join(unknown)} — a rule can target: "
                f"{', '.join(known)}"))
        # Naming every agent is naming none: store [] so an agent added
        # later is not silently left out of the rule.
        out["agents"] = [] if set(agents) == set(known) else agents
    for key in ("examples_good", "examples_bad"):
        if has(key):
            out[key] = _text(data.get(key), key, MAX_EXAMPLE)
    if has("rationale"):
        out["rationale"] = _text(data.get("rationale"), "rationale", MAX_RATIONALE)
    if has("status"):
        # A whole rule without a status is a proposal; an update must say.
        default = "" if partial else "pending"
        status = str(data.get("status") or default).strip().lower()
        if status not in STATUSES:
            raise RuleValidationError(
                "status", f"{status!r} — expected one of {', '.join(STATUSES)}")
        out["status"] = status
    return out


def _title_key(title: str | None) -> str:
    return " ".join(str(title or "").split()).casefold()


# ─── Shapes ──────────────────────────────────────────────────────────


def rule_to_dict(row: Any) -> dict[str, Any]:
    """A `ReviewRule` row as the plain dict every caller receives."""
    def _iso(value: Any) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": int(row.id),
        "workspace_id": row.workspace_id,
        "repo_slug": row.repo_slug,
        "scope": "repo" if row.repo_slug else "workspace",
        "title": row.title,
        "instructions": row.instructions,
        "path_glob": row.path_glob or "",
        "severity": row.severity,
        "agents": list(row.agents or []),
        "examples_good": row.examples_good or "",
        "examples_bad": row.examples_bad or "",
        "rationale": row.rationale or "",
        "status": row.status,
        "origin": row.origin,
        "source_ref": row.source_ref or "",
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "created_at": _iso(getattr(row, "created_at", None)),
        "updated_at": _iso(getattr(row, "updated_at", None)),
    }


def compose_effective(
    workspace_rules: Iterable[dict], repo_rules: Iterable[dict],
) -> list[dict]:
    """The rules a review of one repository applies, in prompt order.

    Active rules only. Workspace rules first, in id order; a repository rule
    whose title matches a workspace rule's (case folded) takes that rule's
    place, and the remaining repository rules follow. One title, one rule:
    two active rules of one scope with the same title keep the first.
    """
    def _active(rows: Iterable[dict]) -> list[dict]:
        return sorted(
            (r for r in rows if r.get("status") == "active"),
            key=lambda r: int(r.get("id") or 0),
        )

    repo_by_title: dict[str, dict] = {}
    for rule in _active(repo_rules):
        repo_by_title.setdefault(_title_key(rule.get("title")), rule)

    out: list[dict] = []
    seen: set[str] = set()
    for rule in _active(workspace_rules):
        key = _title_key(rule.get("title"))
        if key in seen:
            continue
        seen.add(key)
        out.append(repo_by_title.get(key, rule))
    for key, rule in repo_by_title.items():
        if key not in seen:
            seen.add(key)
            out.append(rule)
    return out


# ─── Sessions ────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _session(session: Any = None) -> AsyncIterator[Any]:
    if session is not None:
        yield session
        return
    from src.db.session import async_session

    async with async_session() as own:
        yield own


def _scope_filter(stmt: Any, model: Any, repo_slug: str | None) -> Any:
    if repo_slug:
        return stmt.where(model.repo_slug == repo_slug)
    return stmt.where(model.repo_slug.is_(None))


# ─── Reads ───────────────────────────────────────────────────────────


async def list_rules(
    ws: str,
    repo_slug: str | None = None,
    status: str | None = None,
    *,
    scope: str | None = None,
    origin: str | None = None,
    q: str | None = None,
    session: Any = None,
) -> list[dict]:
    """The workspace's rules, newest-first within each scope.

    `scope`: None = both scopes ("all"); "workspace" = repo_slug NULL only;
    "repo" = repository rules (of `repo_slug` when given, else of every
    repository). With `scope` None and a `repo_slug`, the repository's own
    rules. `status`, `origin` filter exactly; `q` is a case-insensitive
    substring of the title, instructions or glob.
    """
    from sqlalchemy import select

    from src.db.models import ReviewRule

    stmt = select(ReviewRule).where(ReviewRule.workspace_id == ws)
    if scope == "workspace":
        stmt = stmt.where(ReviewRule.repo_slug.is_(None))
    elif scope == "repo" or repo_slug:
        stmt = stmt.where(ReviewRule.repo_slug.is_not(None))
        if repo_slug:
            stmt = stmt.where(ReviewRule.repo_slug == repo_slug)
    if status:
        stmt = stmt.where(ReviewRule.status == status)
    if origin:
        stmt = stmt.where(ReviewRule.origin == origin)
    stmt = stmt.order_by(ReviewRule.id.desc())
    async with _session(session) as s:
        rows = [rule_to_dict(r) for r in (await s.scalars(stmt)).all()]
    needle = (q or "").strip().casefold()
    if needle:
        rows = [r for r in rows if needle in " ".join(
            (r["title"], r["instructions"], r["path_glob"])).casefold()]
    return rows


async def get_rules(ws: str, ids: Iterable[int], *, session: Any = None) -> list[dict]:
    """The rules of `ws` among `ids` (others' ids are simply absent)."""
    from sqlalchemy import select

    from src.db.models import ReviewRule

    wanted = sorted({int(i) for i in ids})
    if not wanted:
        return []
    async with _session(session) as s:
        rows = (await s.scalars(select(ReviewRule).where(
            ReviewRule.workspace_id == ws, ReviewRule.id.in_(wanted)))).all()
        return [rule_to_dict(r) for r in rows]


async def effective_rules_for(
    ws: str, repo_slug: str | None, *, session: Any = None,
) -> list[dict]:
    """What a review of `repo_slug` in `ws` applies (see compose_effective)."""
    from sqlalchemy import select

    from src.db.models import ReviewRule

    stmt = select(ReviewRule).where(
        ReviewRule.workspace_id == ws, ReviewRule.status == "active")
    async with _session(session) as s:
        rows = [rule_to_dict(r) for r in (await s.scalars(stmt)).all()]
    workspace = [r for r in rows if not r["repo_slug"]]
    repo = [r for r in rows if repo_slug and r["repo_slug"] == repo_slug]
    return compose_effective(workspace, repo)


def load_active_rules_sync(ws: str | None, repo_slug: str | None) -> list[dict]:
    """The review's rules, blocking. Never raises: a review must not fail
    because its rules could not be read — it runs without them and the log
    says so."""
    if not ws:
        return []
    from src.review.review_defaults import _sync_url

    url = _sync_url()
    if not url:
        return []
    try:
        from sqlalchemy import create_engine, or_, select
        from sqlalchemy.orm import Session

        from src.db.models import ReviewRule

        engine = create_engine(url, pool_pre_ping=True)
        try:
            with Session(engine) as s:
                scope = ReviewRule.repo_slug.is_(None)
                if repo_slug:
                    scope = or_(scope, ReviewRule.repo_slug == repo_slug)
                rows = [rule_to_dict(r) for r in s.scalars(select(ReviewRule).where(
                    ReviewRule.workspace_id == ws, ReviewRule.status == "active",
                    scope)).all()]
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_rules_load_failed ws=%s repo=%s err=%s",
                       ws, repo_slug, exc)
        return []
    return compose_effective(
        [r for r in rows if not r["repo_slug"]],
        [r for r in rows if r["repo_slug"]],
    )


# ─── Writes ──────────────────────────────────────────────────────────


async def _scope_titles(s: Any, ws: str, repo_slug: str | None) -> dict[str, str]:
    """{title key: status} of every rule in one scope."""
    from sqlalchemy import select

    from src.db.models import ReviewRule

    stmt = _scope_filter(
        select(ReviewRule.title, ReviewRule.status).where(ReviewRule.workspace_id == ws),
        ReviewRule, repo_slug)
    return {_title_key(t): st for t, st in (await s.execute(stmt)).all()}


def _new_row(ws: str, repo_slug: str | None, fields: dict, *, status: str,
             origin: str, source_ref: str | None, created_by: str | None) -> Any:
    from src.db.models import ReviewRule

    if origin not in ORIGINS:
        raise RuleValidationError("origin", f"{origin!r} — expected one of {', '.join(ORIGINS)}")
    return ReviewRule(
        workspace_id=ws,
        repo_slug=(repo_slug or None),
        title=fields["title"],
        instructions=fields["instructions"],
        path_glob=fields.get("path_glob"),
        severity=fields.get("severity") or "warning",
        agents=list(fields.get("agents") or []),
        examples_good=fields.get("examples_good"),
        examples_bad=fields.get("examples_bad"),
        rationale=fields.get("rationale"),
        status=status,
        origin=origin,
        source_ref=(str(source_ref).strip()[:MAX_SOURCE_REF] or None) if source_ref else None,
        created_by=created_by,
        updated_by=created_by,
    )


async def create_rule(
    ws: str,
    *,
    repo_slug: str | None,
    title: str,
    instructions: str,
    path_glob: str | None = None,
    severity: str = "warning",
    agents: list[str] | None = None,
    examples_good: str | None = None,
    examples_bad: str | None = None,
    rationale: str | None = None,
    status: str = "active",
    origin: str = "manual",
    source_ref: str | None = None,
    created_by: str | None = None,
    session: Any = None,
) -> dict:
    """One rule, written now. RuleConflictError when an active or pending
    rule of this scope already has the title (a rejected one does not
    block: writing it by hand is the person deciding again)."""
    fields = validate_rule({
        "title": title, "instructions": instructions, "path_glob": path_glob,
        "severity": severity, "agents": agents or [], "examples_good": examples_good,
        "examples_bad": examples_bad, "rationale": rationale, "status": status,
    })
    async with _session(session) as s:
        titles = await _scope_titles(s, ws, repo_slug)
        if titles.get(_title_key(fields["title"])) in ("active", "pending"):
            raise RuleConflictError(
                f"a rule titled {fields['title']!r} already exists in this scope")
        if len(titles) >= MAX_RULES_PER_SCOPE:
            raise RuleLimitError(f"this scope already holds {MAX_RULES_PER_SCOPE} rules")
        row = _new_row(ws, repo_slug, fields, status=fields["status"], origin=origin,
                       source_ref=source_ref, created_by=created_by)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        out = rule_to_dict(row)
    logger.info("review_rule_created ws=%s repo=%s id=%s origin=%s status=%s by=%s",
                ws, repo_slug or "-", out["id"], origin, out["status"], created_by)
    return out


async def propose_rules(
    ws: str,
    repo_slug: str | None,
    rules: list[dict],
    origin: str,
    created_by: str | None,
    *,
    skip_invalid: bool = False,
    session: Any = None,
) -> list[int]:
    """Write `rules` as PENDING proposals; the ids of the rows written.

    Each dict carries the rule fields (title, instructions, path_glob,
    severity, agents, examples_good, examples_bad, rationale) and optionally
    `source_ref`. A proposal whose title already exists in the scope — in any
    status, rejected included, so a rejected rule is not proposed again — is
    skipped, as is a repeat inside `rules`; a generated or imported proposal
    for a repository is also skipped when the workspace has that title. An invalid proposal raises
    RuleValidationError, or is skipped with `skip_invalid=True` (a model's
    output). Nothing is written when one raises.
    """
    if origin not in ORIGINS:
        raise RuleValidationError("origin", f"{origin!r} — expected one of {', '.join(ORIGINS)}")
    prepared: list[tuple[dict, str | None]] = []
    for idx, raw in enumerate(rules or []):
        if not isinstance(raw, dict):
            if skip_invalid:
                continue
            raise RuleValidationError(f"rules[{idx}]", "must be an object")
        try:
            fields = validate_rule({**raw, "status": "pending"})
        except RuleValidationError as exc:
            if skip_invalid:
                logger.info("review_rule_proposal_skipped ws=%s reason=%s", ws, exc)
                continue
            raise RuleValidationError(f"rules[{idx}].{exc.field}", exc.message) from exc
        prepared.append((fields, raw.get("source_ref")))

    ids: list[int] = []
    async with _session(session) as s:
        titles = await _scope_titles(s, ws, repo_slug)
        room = MAX_RULES_PER_SCOPE - len(titles)
        # A machine's proposal for a repository that repeats a workspace rule
        # is noise, not an override: an override is a person's decision.
        taken = dict(titles)
        if repo_slug and origin in ("generated", "imported", "learned"):
            taken.update(await _scope_titles(s, ws, None))
        rows = []
        for fields, source_ref in prepared:
            key = _title_key(fields["title"])
            if key in taken or room <= 0:
                continue
            taken[key] = "pending"
            room -= 1
            row = _new_row(ws, repo_slug, fields, status="pending", origin=origin,
                           source_ref=source_ref, created_by=created_by)
            s.add(row)
            rows.append(row)
        if rows:
            await s.commit()
            for row in rows:
                await s.refresh(row)
                ids.append(int(row.id))
    logger.info("review_rules_proposed ws=%s repo=%s origin=%s offered=%d written=%d by=%s",
                ws, repo_slug or "-", origin, len(rules or []), len(ids), created_by)
    return ids


async def update_rule(
    ws: str, rule_id: int, changes: dict, actor: str | None, *, session: Any = None,
) -> dict | None:
    """Apply `changes` (keys of EDITABLE_FIELDS) to one rule of `ws`; the
    updated rule, or None when `ws` has no such rule. A title change that
    collides with another active/pending rule of the scope is a conflict."""
    from src.db.models import ReviewRule

    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise RuleValidationError(unknown[0], "cannot be changed")
    fields = validate_rule(changes, partial=True)
    async with _session(session) as s:
        row = await s.get(ReviewRule, int(rule_id))
        if row is None or row.workspace_id != ws:
            return None
        if "title" in fields and _title_key(fields["title"]) != _title_key(row.title):
            titles = await _scope_titles(s, ws, row.repo_slug)
            if titles.get(_title_key(fields["title"])) in ("active", "pending"):
                raise RuleConflictError(
                    f"a rule titled {fields['title']!r} already exists in this scope")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_by = actor
        await s.commit()
        await s.refresh(row)
        out = rule_to_dict(row)
    logger.info("review_rule_updated ws=%s id=%s fields=%s by=%s",
                ws, rule_id, ",".join(sorted(fields)), actor)
    return out


async def set_status(
    ws: str, ids: Iterable[int], status: str, actor: str | None, *, session: Any = None,
) -> list[int]:
    """Move rules of `ws` to `status`; the ids that exist in `ws`."""
    from sqlalchemy import select

    from src.db.models import ReviewRule

    status = str(status or "").strip().lower()
    if status not in STATUSES:
        raise RuleValidationError("status", f"{status!r} — expected one of {', '.join(STATUSES)}")
    wanted = sorted({int(i) for i in ids})
    if not wanted:
        return []
    async with _session(session) as s:
        rows = (await s.scalars(select(ReviewRule).where(
            ReviewRule.workspace_id == ws, ReviewRule.id.in_(wanted)))).all()
        for row in rows:
            row.status = status
            row.updated_by = actor
        await s.commit()
        done = sorted(int(r.id) for r in rows)
    logger.info("review_rules_status ws=%s status=%s ids=%s by=%s", ws, status, done, actor)
    return done


async def delete_rules(
    ws: str, ids: Iterable[int], actor: str | None, *, session: Any = None,
) -> list[int]:
    """Delete rules of `ws`; the ids that existed in `ws` and are gone."""
    from sqlalchemy import select

    from src.db.models import ReviewRule

    wanted = sorted({int(i) for i in ids})
    if not wanted:
        return []
    async with _session(session) as s:
        rows = (await s.scalars(select(ReviewRule).where(
            ReviewRule.workspace_id == ws, ReviewRule.id.in_(wanted)))).all()
        done = sorted(int(r.id) for r in rows)
        for row in rows:
            await s.delete(row)
        await s.commit()
    logger.info("review_rules_deleted ws=%s ids=%s by=%s", ws, done, actor)
    return done


async def add_from_library(
    ws: str,
    repo_slug: str | None,
    library_ids: Iterable[str],
    status: str,
    actor: str | None,
    *,
    session: Any = None,
) -> list[int]:
    """Copy library entries into a scope as `status` (active or pending).

    An entry whose title the scope already holds (any status) is skipped, so
    pressing "Add" twice adds once. Unknown library ids are a validation
    error — the page only offers ids it was given.
    """
    from src.review.rules_library import get_library_rule

    status = str(status or "").strip().lower()
    if status not in ("active", "pending"):
        raise RuleValidationError("status", "must be active or pending")
    entries = []
    for lib_id in dict.fromkeys(str(i) for i in library_ids):
        entry = get_library_rule(lib_id)
        if entry is None:
            raise RuleValidationError("library_ids", f"unknown library rule {lib_id!r}")
        entries.append(entry)

    ids: list[int] = []
    async with _session(session) as s:
        titles = await _scope_titles(s, ws, repo_slug)
        rows = []
        for entry in entries:
            key = _title_key(entry.title)
            if key in titles or len(titles) >= MAX_RULES_PER_SCOPE:
                continue
            titles[key] = status
            fields = validate_rule({
                "title": entry.title, "instructions": entry.instructions,
                "path_glob": entry.path_glob or None, "severity": entry.severity,
                "agents": [], "examples_good": entry.examples_good or None,
                "examples_bad": entry.examples_bad or None, "status": status,
            })
            row = _new_row(ws, repo_slug, fields, status=status, origin="library",
                           source_ref=f"library:{entry.id}", created_by=actor)
            s.add(row)
            rows.append(row)
        if rows:
            await s.commit()
            for row in rows:
                await s.refresh(row)
                ids.append(int(row.id))
    logger.info("review_rules_from_library ws=%s repo=%s status=%s added=%d by=%s",
                ws, repo_slug or "-", status, len(ids), actor)
    return ids


async def counts(ws: str, repo_slug: str | None = None, *, scope: str | None = None,
                 session: Any = None) -> dict[str, int]:
    """{all, active, pending, rejected} for the same scope `list_rules` reads."""
    rows = await list_rules(ws, repo_slug, scope=scope, session=session)
    out = {"all": len(rows), "active": 0, "pending": 0, "rejected": 0}
    for r in rows:
        if r["status"] in out:
            out[r["status"]] += 1
    return out


__all__ = [
    "EDITABLE_FIELDS", "MAX_INSTRUCTIONS", "MAX_RULES_PER_SCOPE", "MAX_TITLE",
    "ORIGINS", "RuleConflictError", "RuleLimitError", "RuleValidationError",
    "SEVERITIES", "STATUSES", "add_from_library", "compose_effective", "counts",
    "create_rule", "delete_rules", "effective_rules_for", "get_rules",
    "list_rules", "load_active_rules_sync", "propose_rules", "rule_to_dict",
    "set_status", "target_agents", "update_rule", "validate_rule",
]
