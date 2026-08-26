"""Data model for RepoGroup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.sync.git_providers import ParsedRepo, parse_repo_url


@dataclass
class RepoGroup:
    """Group of repositories for cross-repo analysis.

    Each repo identifier is parsed via parse_repo_url() — which supports:
        owner/name              (Bitbucket by default, legacy)
        github:owner/name       (explicit prefix)
        gitlab:group/sub/name
        https://github.com/...  (full URL)
        git@host:owner/name.git (SSH)
    """

    name: str
    description: str = ""
    repos: list[str] = field(default_factory=list)
    #: Which tenant owns this group.
    #:
    #: Groups were installation-global — one directory of YAML files, no
    #: notion of who they belong to — because the only way to create one was a
    #: shell command on the server. The moment they became reachable over HTTP
    #: that stopped being harmless: cross-repo drift GREPS every sibling in a
    #: group, so a group naming another tenant's repository would read that
    #: tenant's source and quote it back in a review comment.
    #:
    #: Default "default" so groups written by the CLI before this field
    #: existed still load; they belong to the default workspace, which is what
    #: a single-tenant install has anyway.
    workspace_id: str = "default"
    #: Set when this group is a VIEW of a Project rather than a YAML file.
    #:
    #: A user creates a "Project" in the web interface; cross-repo drift and
    #: the cross-repo edge graph key on a "Group", which only the CLI and one
    #: HTTP route could ever make. So a workspace set up the normal way had
    #: the concept and not the capability, and nothing in the product said so
    #: — the review simply reported no cross-repo findings, which is what a
    #: truthful answer looks like too.
    #:
    #: Rather than teach six consumers about a second kind of grouping, a
    #: project is read as a group. This field marks the ones that are views:
    #: they are read-only, because the project is the source of truth and
    #: writing here would create a second one.
    project_id: str | None = None
    created_at: str = ""  # ISO timestamp
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            now = datetime.now(UTC).isoformat()
            self.created_at = now
            self.updated_at = now

    def add_repo(self, repo_identifier: str) -> bool:
        """Add a repo. Idempotent — a duplicate is not created.

        Returns True if the repo was added, False if it already exists (by
        parsed slug).
        """
        # Validate identifier — raises ValueError if it cannot be parsed
        new_parsed = parse_repo_url(repo_identifier)
        new_slug = new_parsed.slug

        # Duplicate check by canonical slug
        for existing in self.repos:
            try:
                existing_slug = parse_repo_url(existing).slug
            except ValueError:
                continue
            if existing_slug == new_slug:
                return False

        self.repos.append(repo_identifier)
        self.updated_at = datetime.now(UTC).isoformat()
        return True

    def remove_repo(self, repo_identifier: str) -> bool:
        """Remove a repo. Match by parsed slug — any form of the same repo
        in the list is removed."""
        try:
            target_slug = parse_repo_url(repo_identifier).slug
        except ValueError:
            return False

        kept: list[str] = []
        removed = False
        for existing in self.repos:
            try:
                existing_slug = parse_repo_url(existing).slug
            except ValueError:
                kept.append(existing)
                continue
            if existing_slug == target_slug:
                removed = True
                continue
            kept.append(existing)

        if removed:
            self.repos = kept
            self.updated_at = datetime.now(UTC).isoformat()
        return removed

    def parsed_repos(self) -> list[ParsedRepo]:
        """List of ParsedRepo for every repo in the group. Skips invalid
        identifiers."""
        out: list[ParsedRepo] = []
        for r in self.repos:
            try:
                out.append(parse_repo_url(r))
            except ValueError:
                continue
        return out

    def to_dict(self) -> dict[str, Any]:
        """Serialization for YAML."""
        return {
            "name": self.name,
            "description": self.description,
            "repos": list(self.repos),
            "workspace_id": self.workspace_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoGroup:
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            repos=list(data.get("repos", [])),
            # Absent in files the CLI wrote before groups had an owner.
            workspace_id=str(data.get("workspace_id") or "default"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
