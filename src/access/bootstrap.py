"""Finding the repositories nobody has a rule for, and giving them one.

A repository with no research rule and no team grant is visible only to the
workspace owner, its admins and the superadmin (``src/access/policy.py``). This
module answers "which ones are those" for the admin banner, the
``GET /api/access/unruled`` endpoint and ``analyzer access bootstrap``, which is
the one-step way to hand an existing team the access it had before the default
turned to deny.
"""

from __future__ import annotations

import uuid

from src.access.effective import match_any, registered_repos

VISIBILITIES = ("none", "metadata", "code")


def unruled_repos(session, workspace_id: str) -> list[str]:  # noqa: ANN001
    """Indexed slugs of the workspace's repos that have no rule and no grant."""
    from sqlalchemy import select

    from src.access.resolver import _team_grants
    from src.db.models import RepoAccessRule

    registry = registered_repos(workspace_id)
    if not registry:
        return []
    ruled = set(session.execute(
        select(RepoAccessRule.repo_slug).where(RepoAccessRule.workspace_id == workspace_id)
    ).scalars().all())
    slugs = [s for s in sorted(registry) if s not in ruled
             and not any(n in ruled for n in registry[s])]
    granted = _team_grants(session, workspace_id, slugs)
    return [s for s in slugs if s not in granted]


def bootstrap_team(
    session,  # noqa: ANN001
    *,
    workspace_id: str,
    team_id: str,
    visibility: str = "code",
    only: list[str] | None = None,
    created_by: str = "cli",
) -> list[str]:
    """Give ``team_id`` a rule (and, unless ``none``, a read grant) on every unruled repo (or on
    those matching the globs in ``only``). Returns the slugs written."""
    from src.db.models import RepoAccessRule, RepoTeamAccess

    if visibility not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
    registry = registered_repos(workspace_id)
    targets = unruled_repos(session, workspace_id)
    if only:
        targets = [s for s in targets if match_any(only, *registry.get(s, (s,)))]
    for slug in targets:
        session.add(RepoAccessRule(
            id=str(uuid.uuid4()), workspace_id=workspace_id, team_id=team_id,
            repo_slug=slug, visibility=visibility, allow_globs=[], deny_globs=[],
            sensitivity_tags=[], note="bootstrap", created_by=created_by,
        ))
        if visibility != "none":
            # The REST surface (repos list, PRs, reviews) reads team grants, not
            # research rules: without a `read` grant the team would see the repo
            # in the graph and not on the repos page.
            session.add(RepoTeamAccess(repo_slug=slug, team_id=team_id, permission="read"))
    session.commit()
    return targets


__all__ = ["VISIBILITIES", "bootstrap_team", "unruled_repos"]
