"""GroupManager — CRUD operations over YAML-based group definitions.

Storage: ~/code-analysis/groups/{name}.yaml
Every group is a separate YAML file (easy to back up, to version-control).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from src.config import Settings, get_settings
from src.groups.models import RepoGroup

logger = logging.getLogger(__name__)


class GroupNotFoundError(KeyError):
    """No group with such a name exists in the workspace."""


class GroupValidationError(ValueError):
    """Invalid group name (forbidden characters, too long, and so on)."""


# Allowed group names: letters/digits/-/_, max 64 chars
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_name(name: str) -> None:
    if not _VALID_NAME_RE.match(name):
        raise GroupValidationError(
            f"invalid group name {name!r} — allowed: A-Z, a-z, 0-9, '_', '-', "
            f"max 64 chars (for filename safety)"
        )


DEFAULT_WORKSPACE = "default"


def _ws_dir(workspace_id: str) -> str:
    """A workspace id as one safe path segment.

    Ids are uuids today, but a slug could arrive tomorrow and a group
    directory is not the place to discover that one contained a slash.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(workspace_id))[:120] or "default"


def _read(path: Path) -> RepoGroup:
    """One group file, by path rather than by name — `list` walks two layouts
    and cannot ask `load` for a name that exists in both."""
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RepoGroup.from_dict(data)


class GroupManager:
    """File-based group store. Every group = a separate YAML file."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.groups_dir: Path = self.settings.workspace_dir / "groups"
        self.groups_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str, workspace_id: str | None = None) -> Path:
        """Where one tenant's group lives.

        ONE FLAT DIRECTORY WAS A SHARED NAMESPACE. `RepoGroup.workspace_id`
        exists and `list()` filters on it — its docstring says why, that
        listing another tenant's group names is the first step to reading
        their source — but the FILE was `groups/{name}.yaml`, with no tenant
        in the path. So the read side was scoped and the write side was not,
        and `create()` answered 422 "already exists" for a name it could not
        show you. A stranger with an empty group list learned that
        `settlement` was taken somewhere on the installation, and could not
        use the name themselves.

        Both halves of that are bugs, and the second is the one that bites
        daily: in a multi-tenant install the first tenant to create "backend"
        takes it from everyone.

        `workspace_id=None` keeps the flat path, which is where every group
        written before this lives — the CLI, `purge`, and any file already on
        disk still resolve. New groups go under their tenant.
        """
        if workspace_id is None or workspace_id == DEFAULT_WORKSPACE:
            # The flat address, which is where every group on every existing
            # install already lives. A single-tenant box has exactly one
            # workspace and it is this one, so nothing there moves or needs to.
            return self.groups_dir / f"{name}.yaml"
        return self.groups_dir / _ws_dir(workspace_id) / f"{name}.yaml"

    def _write_path(self, name: str, workspace_id: str | None) -> Path:
        """Where a save goes — which is NOT where a read comes from.

        A group already living at the legacy flat address stays there: moving
        files under a running install is a migration, not a save. But only if
        the file there is THIS tenant's group.

        Testing `legacy.exists()` alone read "somebody has this name flat" as
        "I have this name flat". The flat address is the shared namespace the
        tenant directories exist to end, so the test handed one tenant's write
        to another tenant's file: creating a group whose name a legacy group
        already held replaced that group wholesale — description, repo list,
        workspace_id — with no error, no trace, and no way back. The victim's
        cross-repo drift then grepped the attacker's repositories.
        """
        legacy = self._path_for(name)
        if legacy.exists() and self._owner_of(legacy) == (
            workspace_id or DEFAULT_WORKSPACE
        ):
            return legacy
        return self._path_for(name, workspace_id)

    def _owner_of(self, path: Path) -> str | None:
        """Which tenant the group in this file belongs to, or None if it will
        not parse. An unreadable file owns nothing and blocks nobody."""
        try:
            return _read(path).workspace_id or DEFAULT_WORKSPACE
        except Exception:  # noqa: BLE001
            return None

    def _resolve(self, name: str, workspace_id: str | None = None) -> Path:
        """The scoped path if it exists, else the legacy flat one."""
        if workspace_id is not None:
            scoped = self._path_for(name, workspace_id)
            if scoped.exists():
                return scoped
        return self._path_for(name)

    # ─── CRUD ────────────────────────────────────────────────────

    def create(self, name: str, description: str = "",
               workspace_id: str | None = None) -> RepoGroup:
        """Create a new group. Raises GroupValidationError if the name is invalid
        or the file already exists FOR THIS TENANT."""
        _validate_name(name)
        path = self._path_for(name, workspace_id)
        if path.exists():
            raise GroupValidationError(f"group {name!r} already exists")
        # A legacy flat file belonging to THIS tenant is the same group under
        # its old address, so it still collides. One belonging to somebody
        # else is not this tenant's business and must not block the name.
        legacy = self._path_for(name)
        if workspace_id is not None and legacy.exists():
            try:
                if self.load(name).workspace_id == workspace_id:
                    raise GroupValidationError(f"group {name!r} already exists")
            except GroupValidationError:
                raise
            except Exception:  # noqa: BLE001 — unreadable file blocks nobody
                pass
        path.parent.mkdir(parents=True, exist_ok=True)

        # THE TENANT IS SET BEFORE THE FILE IS WRITTEN, not after.
        #
        # `create()` used to build the group, save it, and leave the caller to
        # stamp `workspace_id` and save again. Two writes, and between them the
        # group belonged to "default" — the router has a long comment about
        # what that cost: a failure in the second step left a group nobody's
        # tenant could see, delete or re-create, with the name squatted for
        # good. Scoping the path by tenant made it worse still, because the
        # first write went to the wrong directory and the second to the right
        # one, leaving two files for one group.
        group = RepoGroup(name=name, description=description)
        if workspace_id is not None:
            group.workspace_id = workspace_id
        self.save(group)
        logger.info("group_created name=%s ws=%s", name, workspace_id or "default")
        return group

    def save(self, group: RepoGroup) -> None:
        """Write the group into a YAML file.

        A project-derived group is a VIEW and refuses to be written: the
        project is the source of truth, and a file next to it would be a
        second one, free to disagree from the first edit onward.
        """
        if group.project_id:
            raise GroupValidationError(
                f"{group.name!r} is a view of a project — edit the project"
            )
        _validate_name(group.name)
        # `_resolve` is the READ address: scoped if it exists, else flat. For a
        # write that is backwards — a new group would always land flat, which
        # is the shared namespace this change exists to end. A group already
        # living at the legacy flat address stays there (moving files under a
        # running install is a migration, not a save); everything else is
        # written under its tenant.
        ws = group.workspace_id or None
        path = self._write_path(group.name, ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                group.to_dict(),
                f,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            )

    def load(self, name: str, workspace_id: str | None = None) -> RepoGroup:
        """Read a group by name. Raises GroupNotFoundError.

        Falls through to projects, because the materialize handler resolves the
        name its job was queued with — and after `groups_containing()` learned
        to see projects, that name can belong to one. A listing that finds
        something its loader cannot open is the defect this codebase has now
        shipped twice.
        """
        # A PROJECT NAME IS NOT A FILENAME. `_validate_name` exists because a
        # YAML group's name becomes a path, and it rejects a space — while
        # "Acme Platform" is exactly what somebody types into the project form.
        # Validating before looking made every project with a space in its
        # name unloadable, so the materialize job that carried it would fail
        # on the one thing that had no file to be unsafe about.
        try:
            _validate_name(name)
        except GroupValidationError:
            for group in self._project_groups(workspace_id):
                if group.name == name:
                    return group
            raise
        path = self._resolve(name, workspace_id)
        if not path.exists():
            for group in self._project_groups(workspace_id):
                if group.name == name:
                    return group
            raise GroupNotFoundError(f"group {name!r} not found at {path}")

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise GroupValidationError(f"corrupt group file: {path}")
        return RepoGroup.from_dict(data)

    def delete(self, name: str, workspace_id: str | None = None) -> bool:
        """Delete the group file. Returns True if it existed."""
        _validate_name(name)
        path = self._resolve(name, workspace_id)
        if not path.exists():
            return False
        # The caller named a tenant, so the file has to belong to it. Without
        # this the route that checked ownership scoped and then deleted by
        # bare name unlinked whichever group sat at the shared flat address —
        # somebody else's — and left its own in place, reporting 204.
        if workspace_id is not None:
            owner = self._owner_of(path)
            if owner is not None and owner != workspace_id:
                logger.warning(
                    "group_delete_refused name=%s asked_by=%s owned_by=%s",
                    name, workspace_id, owner,
                )
                return False
        path.unlink()
        logger.info("group_deleted name=%s ws=%s", name, workspace_id or "-")
        return True

    def _iter_paths(self, workspace_id: str | None = None):
        """Every group file this caller may see, as paths.

        `list()` returns NAMES, and a name stopped identifying a group the
        moment two tenants could hold the same one. Anything that lists and
        then loads has to carry the path or the workspace across, or it finds
        a group and cannot open it — which is exactly what happened to
        `groups_containing` and to the `cross_repo_materialize` job: the group
        was listed installation-wide and loaded from the flat address, so a
        tenant-scoped group was visible and unopenable, and cross-repo
        materialisation stopped for every tenant that had one.
        """
        if not self.groups_dir.exists():
            return
        flat = sorted(self.groups_dir.glob("*.yaml"))
        scoped = ([] if workspace_id is None
                  else sorted((self.groups_dir / _ws_dir(workspace_id)).glob("*.yaml")))
        if workspace_id is None:
            scoped = sorted(self.groups_dir.glob("*/*.yaml"))
        for f in [*flat, *scoped]:
            if not _VALID_NAME_RE.match(f.stem):
                continue
            yield f

    def _project_groups(self, workspace_id: str | None = None) -> list[RepoGroup]:
        """Every Project, as a read-only group.

        THE PRODUCT HAS ONE CONCEPT AND THE CODE HAD TWO. A user groups
        repositories by making a "Project" in the web interface. Cross-repo
        drift and the cross-repo edge graph key on a "Group", which only the
        CLI and a single HTTP route could create, and which the web has no
        page for at all. So a workspace set up the normal way had the concept
        and not the capability — and nothing said so: the review reported no
        cross-repo findings, which is exactly what the truthful answer looks
        like.

        Projects are read HERE rather than taught to every consumer, because
        after the addressing work drift, materialisation, the review graph
        context, purge and the MCP tools all reach groups through
        `iter_groups()`. One seam, six callers.

        `ProjectRepo` stores the local indexed slug while `RepoGroup.repos`
        holds parseable identifiers, so the workspace registry supplies the
        provider and owner — a slug cannot be un-flattened back into them. A
        repository the registry does not know is skipped rather than guessed.

        Never raises. This runs inside the indexer and inside a review: a
        database that is briefly unreachable must cost the project view, not
        the run.
        """
        try:
            from sqlalchemy import create_engine, select
            from sqlalchemy.orm import Session

            from src.api.auto_review import get_auto_review_store
            from src.db.models import Project, ProjectRepo
            from src.db.session import get_database_url
        except Exception:  # noqa: BLE001
            return []

        try:
            url = get_database_url().replace(
                "postgresql+asyncpg://", "postgresql+psycopg://"
            )
            engine = create_engine(url, pool_pre_ping=True)
            try:
                with Session(engine) as db:
                    q = select(Project)
                    if workspace_id is not None:
                        q = q.where(Project.workspace_id == workspace_id)
                    projects = list(db.scalars(q))
                    if not projects:
                        return []
                    members: dict[str, list[str]] = {}
                    for row in db.scalars(select(ProjectRepo).where(
                            ProjectRepo.project_id.in_([p.id for p in projects]))):
                        members.setdefault(str(row.project_id), []).append(row.repo_slug)
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("project_groups_unavailable err=%s", exc)
            return []

        out: list[RepoGroup] = []
        registries: dict[str, dict] = {}
        for proj in projects:
            slugs = members.get(str(proj.id)) or []
            if len(slugs) < 2:
                # One repository is not a cross-repo anything, and the
                # resolvers skip a group of one regardless.
                continue
            ws = proj.workspace_id or DEFAULT_WORKSPACE
            if ws not in registries:
                try:
                    registries[ws] = {
                        c.repo_slug: c
                        for c in get_auto_review_store().list_for_workspace(ws)
                    }
                except Exception:  # noqa: BLE001
                    registries[ws] = {}
            reg = registries[ws]
            repos = [f"{reg[s].provider}:{reg[s].full_name}"
                     for s in slugs if s in reg and reg[s].full_name]
            if len(repos) < 2:
                continue
            out.append(RepoGroup(
                name=proj.name, description=proj.description or "",
                repos=repos, workspace_id=ws, project_id=str(proj.id),
            ))
        return out

    def iter_groups(self, workspace_id: str | None = None):
        """Every group this caller may see, as (path, group) pairs.

        Use this instead of `list()` + `load()` whenever the loop needs the
        group and not merely its name. That pairing is the bug this codebase
        keeps rediscovering: the listing walks both layouts, the load resolves
        a bare name against the flat address, and a tenant-scoped group is
        therefore listed and unopenable at once. It cost cross-repo
        materialisation, then purge, then the MCP group listing.

        A file that will not parse is skipped rather than raised: one bad YAML
        must not stop the sweep.
        """
        seen: set[tuple[str, str]] = set()
        for path in self._iter_paths(workspace_id):
            try:
                group = _read(path)
            except Exception:  # noqa: BLE001
                continue
            if workspace_id is not None and group.workspace_id != workspace_id:
                continue
            seen.add((group.workspace_id, group.name))
            yield path, group
        # PROJECTS ARE GROUPS TOO. Last, and never shadowing a YAML group of
        # the same name in the same tenant: a file somebody wrote by hand wins
        # over a view.
        for group in self._project_groups(workspace_id):
            if (group.workspace_id, group.name) in seen:
                continue
            yield self.graph_path(group).with_suffix(".yaml"), group

    def graph_path(self, group: RepoGroup) -> Path:
        """The group's cross-repo edge index, beside its definition.

        Deriving it from the bare name put two tenants' edges in ONE
        `groups/{name}.fdblite`: the YAML got a tenant directory and the graph
        file next to it did not, so a tenant holding a group called "product"
        read the other's cross-repo edges. Anchoring the graph to the YAML's
        own address makes the two move together and keeps every existing flat
        install exactly where it is.
        """
        if group.project_id:
            # Keyed on the ID, not the name: a project can be renamed, and two
            # tenants can hold projects called the same thing.
            return self._path_for(
                f"_project-{_ws_dir(group.project_id)}", group.workspace_id
            ).with_suffix(".fdblite")
        return self._resolve(group.name, group.workspace_id).with_suffix(".fdblite")

    def list(self, workspace_id: str | None = None) -> list[str]:
        """Group names. With `workspace_id`, only that tenant's.

        None means every group, which is what the CLI and the installation-wide
        maintenance jobs want. Every HTTP caller passes a workspace: a group
        is a grep target for cross-repo drift, so listing another tenant's
        group names is the first step to reading their source.
        """
        if not self.groups_dir.exists():
            return []
        names = []
        # Both layouts: `groups/*.yaml` is where everything written before the
        # namespace was scoped still lives, `groups/<tenant>/*.yaml` is where
        # new ones go. A name can legitimately appear in both directories now,
        # owned by different tenants — which is the entire point — so the
        # filter below still decides, and `dict.fromkeys` keeps this tenant
        # from seeing its own group twice.
        for f in sorted(self.groups_dir.glob("*.yaml")) + \
                 sorted(self.groups_dir.glob("*/*.yaml")):
            stem = f.stem
            if not _VALID_NAME_RE.match(stem):
                continue
            if workspace_id is not None:
                try:
                    if _read(f).workspace_id != workspace_id:
                        continue
                except Exception:  # noqa: BLE001
                    continue
            names.append(stem)
        return list(dict.fromkeys(names))

    # ─── Convenience ─────────────────────────────────────────────

    def groups_containing(self, repo_slug: str,
                          workspace_id: str | None = None) -> list[RepoGroup]:
        """Every group whose repo list contains `repo_slug`.

        `repo_slug` is the LOCAL slug form (e.g. 'gitlab_owner-name' or
        'owner-name') — the same directory-name key used by
        settings.repo_path. Group repo entries are raw identifiers
        ('gitlab:owner/name', 'owner/name', a URL, …), so we normalise
        each via parse_repo_url().slug and compare on that canonical
        form. Used by the incremental indexer to enqueue cross-repo
        rematerialization for affected groups only.
        """
        from src.sync.git_providers import parse_repo_url

        out: list[RepoGroup] = []
        for _path, group in self.iter_groups(workspace_id):
            for r in group.repos:
                try:
                    if parse_repo_url(r).slug == repo_slug:
                        out.append(group)
                        break
                except Exception:  # noqa: BLE001
                    # Fall back to loose match if the identifier won't parse.
                    if repo_slug == r:
                        out.append(group)
                        break
        return out

    def add_repo(self, group_name: str, repo_identifier: str) -> bool:
        """Add a repo to the group. Persists. Returns True if new, False if it
        was already there."""
        group = self.load(group_name)
        added = group.add_repo(repo_identifier)
        if added:
            self.save(group)
        return added

    def remove_repo(self, group_name: str, repo_identifier: str) -> bool:
        """Remove a repo from the group. Returns True if found + removed."""
        group = self.load(group_name)
        removed = group.remove_repo(repo_identifier)
        if removed:
            self.save(group)
        return removed


_default_manager: GroupManager | None = None


def get_group_manager() -> GroupManager:
    """Singleton."""
    global _default_manager
    if _default_manager is None:
        _default_manager = GroupManager()
    return _default_manager
