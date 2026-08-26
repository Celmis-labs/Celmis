"""Cross-repo edge materializer (Stage 6, May 2026).

Goal: find the links BETWEEN repositories in a RepoGroup and materialize
them as edges. Without cross-repo edges every repo is a silo; with them we
get a unified architecture graph for cross-team integration Q&A (our core
use case).

MVP resolvers (May 2026):
    1. **Image references**: Compose/K8s container references an image string.
       We look for a Dockerfile in another repo of the group whose repo name
       matches the image base name.
       Edge: container/service →[REFERENCES_REPO]→ file_module of Dockerfile.
       Confidence: weak (heuristic) | strong (exact match).

    2. **Build context**: Compose service with `build: ./repo-name/...`. If the
       group has a repo with exactly that slug — an edge to that repo.

Edge type: 'REFERENCES_REPO' (vendor.cross_repo.REFERENCES_REPO) — emitted
as a plain str so as not to violate the core enum (warning suppressed by
convention).

Further extensions (post-MVP):
    - HTTP call URL → endpoint route in a backend repo (needs route extractors)
    - K8s Service.selector → Pod templates in another repo (label-based matching)
    - Cross-repo IMPORTS (npm/pypi packages → repo that publishes the package)
    - CI/CD pipeline → triggered repos
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from src.groups.models import RepoGroup
from src.indexing.graph.extractor import ExtractionResult
from src.sync.git_providers import parse_repo_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrossRepoEdge:
    """An edge between a symbol in one repo and a symbol/file in another."""

    from_repo: str  # repo slug
    from_id: str  # local symbol id (formatted as in the per-repo graph)
    to_repo: str  # target repo slug
    to_id: str  # target id (usually file_module)
    kind: str  # 'REFERENCES_REPO' | 'BUILD_CONTEXT' | etc.
    confidence: str  # 'strong' | 'weak'
    rationale: str  # for diagnostics: why the resolver decided this is a match


@dataclass
class _RepoIndex:
    """Per-repo summary for the resolution stage."""

    slug: str  # repo slug — stable identifier within the group
    repo_name: str  # last segment of repo name (e.g. 'pro-back' from 'acme/pro-back')
    extractions: dict[str, ExtractionResult]  # file_path → ExtractionResult
    owner: str = ""  # 'acme' — the half a bare name throws away


class CrossRepoMaterializer:
    """Coordinates the resolution of cross-repo edges across the whole group.

    Usage:
        m = CrossRepoMaterializer(group)
        m.add_repo("acme-pro-back", repo_path, extractions)
        m.add_repo("acme-pro-front", ...)
        edges = m.materialize()
    """

    def __init__(self, group: RepoGroup) -> None:
        self.group = group
        self._indices: dict[str, _RepoIndex] = {}
        self._names = _names_from_group(group)
        self._owners = _owners_from_group(group)

    def add_repo(
        self,
        slug: str,
        extractions: dict[str, ExtractionResult],
    ) -> None:
        """Register the extracted per-file data for one repo of the group.

        Args:
            slug: repo slug (from ParsedRepo.slug — `github_pallets-click` etc.)
            extractions: dict file_path (relative to repo_root) → ExtractionResult
        """
        # Prefer the name the group already parsed; the slug cannot be
        # un-flattened, so _extract_repo_name() is a guess of last resort.
        repo_name = self._names.get(slug) or _extract_repo_name(slug)
        self._indices[slug] = _RepoIndex(
            slug=slug, repo_name=repo_name, extractions=extractions,
            owner=self._owners.get(slug, ""),
        )

    def materialize(self) -> list[CrossRepoEdge]:
        """Run all resolvers, return the combined cross-repo edges."""
        edges: list[CrossRepoEdge] = []
        edges.extend(self._resolve_image_references())
        edges.extend(self._resolve_build_context())
        logger.info(
            "cross_repo_materialized group=%s edges=%d",
            self.group.name, len(edges),
        )
        return edges

    # ─── Resolvers ───────────────────────────────────────────────

    def _resolve_image_references(self) -> list[CrossRepoEdge]:
        """Compose/K8s RUNS_IMAGE → Dockerfile in another repo of the group.

        Heuristic:
            image string 'myorg/pro-back:v1.0' → image base name 'pro-back'
            if the group has a repo with repo_name == 'pro-back' (last segment
            of the slug) → edge to the file_module of the Dockerfile in that repo.

        Confidence:
            'strong' if the image base name exactly equals repo_name
            (case-insensitive)
            'weak' if it is a substring match (rarer)
        """
        edges: list[CrossRepoEdge] = []

        # Build target index: repo_name → (slug, dockerfile_path)
        # Several Dockerfiles — we take the first one (rare edge case of
        # multi-Dockerfile per repo)
        repo_dockerfiles: dict[str, list[tuple[str, str]]] = {}
        for slug, idx in self._indices.items():
            for file_path, res in idx.extractions.items():
                # Detect Dockerfile via language=dockerfile in the symbols
                has_dockerfile = any(
                    s.language == "dockerfile" for s in res.symbols
                )
                if has_dockerfile:
                    repo_dockerfiles.setdefault(idx.repo_name.lower(), []).append(
                        (slug, file_path)
                    )

        if not repo_dockerfiles:
            return edges

        # Walk all image references (RUNS_IMAGE edges)
        for slug, idx in self._indices.items():
            for res in idx.extractions.values():
                for edge in res.edges:
                    if edge.kind != "RUNS_IMAGE" or not edge.raw_target:
                        continue
                    image_base = _image_base_name(edge.raw_target)
                    if not image_base:
                        continue
                    image_base_lower = image_base.lower()

                    # Exact match — strong
                    if image_base_lower in repo_dockerfiles:
                        candidates = repo_dockerfiles[image_base_lower]
                        # When the reference names an owner and one candidate
                        # is that owner's, it is the one meant. Otherwise the
                        # name is genuinely ambiguous and every candidate is a
                        # guess — say so in the confidence rather than
                        # publishing several edges as certain.
                        image_owner = _image_owner(edge.raw_target).lower()
                        if image_owner:
                            owned = [c for c in candidates
                                     if self._indices[c[0]].owner.lower() == image_owner]
                            if owned:
                                candidates = owned
                        ambiguous = len(candidates) > 1
                        for target_slug, target_path in candidates:
                            if target_slug == slug:
                                continue  # within-repo, not cross-repo
                            edges.append(CrossRepoEdge(
                                from_repo=slug,
                                from_id=edge.from_id,
                                to_repo=target_slug,
                                to_id=f"{target_path}::__module__",
                                kind="REFERENCES_REPO",
                                confidence="weak" if ambiguous else "strong",
                                rationale=(
                                    f"image '{edge.raw_target}' → base '{image_base}' "
                                    f"matches repo name {target_slug}"
                                ),
                            ))

        return edges

    def _resolve_build_context(self) -> list[CrossRepoEdge]:
        """Compose service `build: ./pro-back` → repo with a matching slug in
        the group.

        Heuristic:
            BUILT_FROM raw_target = 'context_path/Dockerfile' (for example
            './backend/Dockerfile' or '../api/Dockerfile').
            We take the last directory segment (`backend`, `api`) and look for
            a repo in the group whose repo_name matches.
        """
        edges: list[CrossRepoEdge] = []

        repo_dockerfiles: dict[str, list[tuple[str, str]]] = {}
        for slug, idx in self._indices.items():
            for file_path, res in idx.extractions.items():
                has_dockerfile = any(
                    s.language == "dockerfile" for s in res.symbols
                )
                if has_dockerfile:
                    repo_dockerfiles.setdefault(idx.repo_name.lower(), []).append(
                        (slug, file_path)
                    )

        if not repo_dockerfiles:
            return edges

        for slug, idx in self._indices.items():
            for res in idx.extractions.values():
                # Only compose-style BUILT_FROM (path-based, not image:tag)
                # Compose target = `path/Dockerfile`; Dockerfile target = base image
                # we tell them apart by the presence of '/' and the 'Dockerfile'
                # suffix
                for edge in res.edges:
                    if edge.kind != "BUILT_FROM" or not edge.raw_target:
                        continue
                    target = edge.raw_target
                    # Skip if not path-based (e.g. 'python:3.13' — that is a
                    # Dockerfile FROM)
                    if "/" not in target and "." not in target:
                        continue
                    # Extract the directory name before /Dockerfile
                    directory = _extract_build_directory(target)
                    if not directory:
                        continue
                    dir_lower = directory.lower()
                    if dir_lower in repo_dockerfiles:
                        for target_slug, target_path in repo_dockerfiles[dir_lower]:
                            if target_slug == slug:
                                continue
                            edges.append(CrossRepoEdge(
                                from_repo=slug,
                                from_id=edge.from_id,
                                to_repo=target_slug,
                                to_id=f"{target_path}::__module__",
                                kind="BUILD_CONTEXT",
                                confidence="weak",
                                rationale=(
                                    f"compose build '{target}' → directory "
                                    f"'{directory}' matches repo {target_slug}"
                                ),
                            ))

        return edges


# ─── helpers ────────────────────────────────────────────────────────


def _names_from_group(group: RepoGroup) -> dict[str, str]:
    """slug → repo name, taken from the group's own identifiers.

    The slug flattens owner and name into one hyphenated string, and that
    flattening is not reversible: 'github_celmis-codereviewer-celmis-e2e-probe'
    reads as owner 'celmis' / name 'codereviewer-celmis-e2e-probe' exactly as
    plausibly as owner 'celmis-codereviewer' / name 'celmis-e2e-probe'.
    _extract_repo_name() picks the first hyphen and is therefore wrong for
    every owner that contains one — on GitHub, most organisations.

    A wrong name here is silent: the resolvers match a compose build directory
    against it, miss, and emit no edge. The group stores the unflattened
    identifier, so parse that instead of guessing.
    """
    names: dict[str, str] = {}
    for identifier in group.repos:
        try:
            parsed = parse_repo_url(identifier)
        except ValueError:
            continue
        names[parsed.slug] = parsed.name
    return names


def _owners_from_group(group: RepoGroup) -> dict[str, str]:
    """slug → owner, from the group's own identifiers.

    A repository name alone does not identify a repository: two members of one
    group can both be called "api" under different owners. An image reference
    that carries the owner ("acme/api") can say which one it means; without
    this the resolver drew an edge to every repo of that name, and every one
    but a single winner was wrong.
    """
    owners: dict[str, str] = {}
    for identifier in group.repos:
        try:
            parsed = parse_repo_url(identifier)
        except ValueError:
            continue
        owners[parsed.slug] = parsed.owner
    return owners


def _image_owner(image: str) -> str:
    """'myorg/api:v1' → 'myorg'; 'nginx:latest' → ''.

    The segment before the last, when the reference has one and it is not a
    registry host. A host is told apart the way a registry does it: it has a
    dot or a port, or it is localhost.
    """
    if "@" in image:
        image = image.split("@", 1)[0]
    parts = [p for p in image.split("/") if p]
    if len(parts) < 2:
        return ""
    candidate = parts[-2]
    if candidate == "localhost" or "." in candidate or ":" in candidate:
        return "" if len(parts) < 3 else parts[-2]
    return candidate


def _extract_repo_name(slug: str) -> str:
    """slug → repo name (without the owner), guessed.

    Only for a slug the group does not name — see _names_from_group().

    Convention: slug = '{provider_prefix}{owner}-{name}', where owner is a
    single word (without '-'), while name may contain '-' (like 'pro-back',
    'auth-service').

    Splitting on the FIRST '-' after the provider prefix:
        'github_pallets-click'        → 'click'
        'gitlab_group-sub-repo'       → 'sub-repo' (group='group', name='sub-repo')
        'acme-frontend' (BB)      → 'frontend'
        'acme-pro-back'              → 'pro-back'
        'github_acme-api-service'     → 'api-service'

    For GitLab subgroups (`group-sub-repo`) this is an approximation — more
    precise handling needs metadata from parse_repo_url(). YAGNI for now.
    """
    s = slug
    for prefix in ("github_", "gitlab_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break

    # Split on the FIRST '-' (owner | name)
    if "-" in s:
        return s.split("-", 1)[1]
    return s


def _image_base_name(image: str) -> str:
    """Extract the base name from an image string.

    'nginx:latest'                  → 'nginx'
    'myorg/myapp:v1.0'              → 'myapp'
    'registry.local/team/api:v1'    → 'api'
    'docker.io/library/redis@sha256:...' → 'redis'
    """
    # Strip digest
    if "@" in image:
        image = image.split("@", 1)[0]
    # LAST PATH SEGMENT FIRST, THEN THE TAG. The other order stripped a
    # registry PORT as though it were a tag: 'ghcr.io:443/acme/pro-back' has no
    # tag, so rsplit(":", 1) cut at the port and left 'ghcr.io', which then had
    # no '/' to split and became the "repo name". The colon that separates a
    # tag can only appear in the final segment; the one in a host:port cannot.
    if "/" in image:
        image = image.rsplit("/", 1)[1]
    if ":" in image:
        image = image.rsplit(":", 1)[0]
    return image.strip()


def _extract_build_directory(build_target: str) -> str | None:
    """`./backend/Dockerfile` → 'backend'.

    `../api/Dockerfile.prod`   → 'api'
    `services/auth/Dockerfile` → 'auth'
    `Dockerfile` (no path)     → None (in-place build)
    """
    target = Path(build_target)
    parent = target.parent
    name = parent.name  # last directory segment
    if name in (".", "..", ""):
        return None
    return name
