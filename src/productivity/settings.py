"""Productivity settings: repo row > workspace row > built-in, field by field.

A table of its own (`productivity_repo_settings`) rather than more columns on
the review policies: these knobs only matter to the sync and the metrics, and
the review-settings wiring (ten files) is not the place to grow them. The
layering is the same: NULL in a row means "not said here", and a repository
that says nothing inherits the workspace, which inherits the built-ins.

The workspace default is the row with `provider=''` and `repo=''`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from typing import Any, Final

logger = logging.getLogger(__name__)

DEPLOY_SOURCES: Final[tuple[str, ...]] = ("merge", "provider", "tags")

#: Titles, in two alphabets: "Revert …", "Rollback …", "Відкат …".
DEFAULT_REVERT_PATTERNS: Final[tuple[str, ...]] = (r"^\s*(revert|rollback|відкат\w*)",)
DEFAULT_HOTFIX_BRANCH_PATTERNS: Final[tuple[str, ...]] = (r"^(hotfix|revert)[/_-]",)
DEFAULT_BUGFIX_PATTERNS: Final[tuple[str, ...]] = (r"\b(fix|bug|hotfix|баг|виправ\w*)\b",)
#: A comment carrying one of these on a line of its own is a bot's. The two
#: built-in kinds (`markers.is_bot_text`) are always checked as well.
DEFAULT_BOT_MARKERS: Final[tuple[str, ...]] = ("<!-- code-analyzer:", "<!-- celmis:")

#: Bounds a settings write is held to (and a stored value is clamped to).
BACKFILL_DAYS_MAX: Final[int] = 730
RATE_PER_HOUR_MIN: Final[int] = 30
RATE_PER_HOUR_MAX: Final[int] = 900
_PATTERN_MAX: Final[int] = 200
_LIST_MAX: Final[int] = 50


@dataclass(frozen=True)
class ProductivitySettings:
    """The resolved settings of one repository. Every field has a value."""

    enabled: bool = False
    backfill_days: int = 180
    #: Merges into these branches are deployments. Built-in `main`/`master`:
    #: an empty list would mean "nothing ever deploys" on a repository nobody
    #: configured, and a default branch is the least surprising reading.
    production_branches: tuple[str, ...] = ("main", "master")
    integration_branches: tuple[str, ...] = ()
    deploy_source: str = "merge"
    tag_pattern: str = r"^v?\d"
    revert_patterns: tuple[str, ...] = DEFAULT_REVERT_PATTERNS
    hotfix_branch_patterns: tuple[str, ...] = DEFAULT_HOTFIX_BRANCH_PATTERNS
    bugfix_patterns: tuple[str, ...] = DEFAULT_BUGFIX_PATTERNS
    ignored_authors: tuple[str, ...] = ()
    bot_markers: tuple[str, ...] = DEFAULT_BOT_MARKERS
    failure_window_days: int = 7
    deploy_group_minutes: int = 30
    rate_per_hour: int = 500


BUILTIN: Final[ProductivitySettings] = ProductivitySettings()
SETTING_NAMES: Final[tuple[str, ...]] = tuple(f.name for f in fields(ProductivitySettings))

_LIST_FIELDS: Final[frozenset[str]] = frozenset({
    "production_branches", "integration_branches", "revert_patterns",
    "hotfix_branch_patterns", "bugfix_patterns", "ignored_authors", "bot_markers",
})
_PATTERN_FIELDS: Final[frozenset[str]] = frozenset({
    "revert_patterns", "hotfix_branch_patterns", "bugfix_patterns",
})
_INT_BOUNDS: Final[dict[str, tuple[int, int]]] = {
    "backfill_days": (1, BACKFILL_DAYS_MAX),
    "failure_window_days": (1, 90),
    "deploy_group_minutes": (0, 24 * 60),
    "rate_per_hour": (RATE_PER_HOUR_MIN, RATE_PER_HOUR_MAX),
}


class SettingsError(ValueError):
    """A settings write the caller can fix; the message says which field."""


def _clean_list(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[\n,]", value)]
    if not isinstance(value, (list, tuple)):
        raise SettingsError(f"{name} must be a list")
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text or text in out:
            continue
        if len(text) > _PATTERN_MAX:
            raise SettingsError(f"{name}: an entry is longer than {_PATTERN_MAX} characters")
        if name in _PATTERN_FIELDS:
            try:
                re.compile(text)
            except re.error as exc:
                raise SettingsError(f"{name}: {text!r} is not a valid pattern ({exc})") from None
        out.append(text)
    if len(out) > _LIST_MAX:
        raise SettingsError(f"{name} takes at most {_LIST_MAX} entries")
    return tuple(out)


def validate(name: str, value: Any) -> Any:
    """The value in its stored shape, or `SettingsError`. None passes (= inherit)."""
    if name not in SETTING_NAMES:
        raise SettingsError(f"unknown setting {name!r}")
    if value is None:
        return None
    if name == "enabled":
        if not isinstance(value, bool):
            raise SettingsError("enabled must be true or false")
        return value
    if name in _INT_BOUNDS:
        lo, hi = _INT_BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{name} must be a whole number")
        if not lo <= value <= hi:
            raise SettingsError(f"{name} must be between {lo} and {hi}")
        return value
    if name == "deploy_source":
        if value not in DEPLOY_SOURCES:
            raise SettingsError(f"deploy_source must be one of {', '.join(DEPLOY_SOURCES)}")
        return value
    if name == "tag_pattern":
        text = str(value).strip()
        if len(text) > _PATTERN_MAX:
            raise SettingsError(f"tag_pattern is longer than {_PATTERN_MAX} characters")
        try:
            re.compile(text)
        except re.error as exc:
            raise SettingsError(f"tag_pattern is not a valid pattern ({exc})") from None
        return text
    if name in _LIST_FIELDS:
        return list(_clean_list(name, value))
    raise SettingsError(f"unknown setting {name!r}")  # pragma: no cover


def _layer(base: ProductivitySettings, row: Mapping[str, Any] | None) -> ProductivitySettings:
    """`base` with every field this row actually sets laid over it."""
    if not row:
        return base
    changes: dict[str, Any] = {}
    for name in SETTING_NAMES:
        value = row.get(name)
        if value is None:
            continue
        try:
            value = validate(name, value)
        except SettingsError as exc:
            # A stored value the rules reject (written by an older version, or
            # by hand) falls back to the layer below; it must not stop a sync.
            logger.warning("productivity_setting_ignored name=%s err=%s", name, exc)
            continue
        changes[name] = tuple(value) if name in _LIST_FIELDS else value
    return replace(base, **changes) if changes else base


def resolve(
    *, workspace_row: Mapping[str, Any] | None = None,
    repo_row: Mapping[str, Any] | None = None,
) -> ProductivitySettings:
    """Repository row over workspace row over the built-ins."""
    return _layer(_layer(BUILTIN, workspace_row), repo_row)


# ─── the table ────────────────────────────────────────────────────────

def _engine(engine=None):
    from src.productivity.db import get_engine

    return get_engine(engine)


def _row_dict(row) -> dict[str, Any]:
    return {name: getattr(row, name) for name in SETTING_NAMES}


@dataclass(frozen=True)
class SettingsIndex:
    """Every settings row of the install, read once — what a scheduler tick uses."""

    rows: Mapping[tuple[str, str, str], Mapping[str, Any]]

    def for_repo(self, workspace_id: str, provider: str, repo: str) -> ProductivitySettings:
        return resolve(
            workspace_row=self.rows.get((workspace_id, "", "")),
            repo_row=self.rows.get((workspace_id, provider, repo)),
        )


def load_index(engine=None) -> SettingsIndex:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ProductivityRepoSettings as Row

    with Session(_engine(engine)) as s:
        rows = {(r.workspace_id, r.provider, r.repo): _row_dict(r)
                for r in s.execute(select(Row)).scalars()}
    return SettingsIndex(rows)


def load(workspace_id: str, provider: str, repo: str, *, engine=None) -> ProductivitySettings:
    """The settings one repository runs with."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ProductivityRepoSettings as Row

    with Session(_engine(engine)) as s:
        found = {
            (r.provider, r.repo): _row_dict(r)
            for r in s.execute(select(Row).where(
                Row.workspace_id == workspace_id,
                ((Row.provider == provider) & (Row.repo == repo))
                | ((Row.provider == "") & (Row.repo == "")),
            )).scalars()
        }
    return resolve(workspace_row=found.get(("", "")), repo_row=found.get((provider, repo)))


def load_layers(workspace_id: str, provider: str, repo: str, *, engine=None) -> dict[str, Any]:
    """For a settings page: the raw rows and the resolved result, so the UI can say which layer each value came from."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ProductivityRepoSettings as Row

    with Session(_engine(engine)) as s:
        found = {
            (r.provider, r.repo): _row_dict(r)
            for r in s.execute(select(Row).where(
                Row.workspace_id == workspace_id,
                ((Row.provider == provider) & (Row.repo == repo))
                | ((Row.provider == "") & (Row.repo == "")),
            )).scalars()
        }
    workspace_row, repo_row = found.get(("", "")), found.get((provider, repo))
    resolved = resolve(workspace_row=workspace_row, repo_row=repo_row)
    return {
        "workspace": {k: v for k, v in (workspace_row or {}).items() if v is not None},
        "repo": {k: v for k, v in (repo_row or {}).items() if v is not None},
        "resolved": {name: (list(v) if isinstance(v, tuple) else v)
                     for name, v in ((n, getattr(resolved, n)) for n in SETTING_NAMES)},
        "builtin": {name: (list(v) if isinstance(v, tuple) else v)
                    for name, v in ((n, getattr(BUILTIN, n)) for n in SETTING_NAMES)},
    }


def save(
    workspace_id: str, provider: str, repo: str, changes: Mapping[str, Any], *, engine=None,
) -> ProductivitySettings:
    """Write `changes` into the layer's row (`provider=''`, `repo=''` = workspace).

    A key with value None clears that field (back to inheriting). Raises
    `SettingsError` and writes nothing when any value is rejected.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ProductivityRepoSettings as Row

    clean = {name: validate(name, value) for name, value in changes.items()}
    if bool(provider) != bool(repo):
        raise SettingsError("a repository setting needs both provider and repo")
    with Session(_engine(engine)) as s:
        row = s.execute(select(Row).where(
            Row.workspace_id == workspace_id, Row.provider == provider, Row.repo == repo,
        )).scalar_one_or_none()
        if row is None:
            row = Row(workspace_id=workspace_id, provider=provider, repo=repo)
            s.add(row)
        for name, value in clean.items():
            setattr(row, name, value)
        row.updated_at = datetime.now(UTC)
        s.commit()
    return load(workspace_id, provider, repo, engine=engine)
