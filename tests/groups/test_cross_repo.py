"""Tests для cross-repo edge materializer."""

from __future__ import annotations

import pytest

from src.groups.cross_repo import (
    CrossRepoMaterializer,
    _extract_build_directory,
    _extract_repo_name,
    _image_base_name,
    _names_from_group,
)
from src.groups.models import RepoGroup
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolInfo

# ─── Helpers ────────────────────────────────────────────────────────


class TestImageBaseName:
    @pytest.mark.parametrize("image,expected", [
        ("nginx:latest", "nginx"),
        ("myorg/myapp:v1.0", "myapp"),
        ("registry.local/team/api:v1", "api"),
        ("docker.io/library/redis@sha256:abc123", "redis"),
        ("postgres:16", "postgres"),
        ("ghcr.io/owner/repo:tag", "repo"),
    ])
    def test_image_base_extraction(self, image: str, expected: str) -> None:
        assert _image_base_name(image) == expected


class TestRepoName:
    @pytest.mark.parametrize("slug,expected", [
        ("github_pallets-click", "click"),
        ("gitlab_group-sub-repo", "sub-repo"),  # group='group', name='sub-repo'
        ("acme-frontend", "frontend"),
        ("acme-pro-back", "pro-back"),  # owner='acme', name='pro-back'
        ("github_acme-api-service", "api-service"),
    ])
    def test_repo_name_extraction(self, slug: str, expected: str) -> None:
        assert _extract_repo_name(slug) == expected


class TestBuildDirectory:
    @pytest.mark.parametrize("target,expected", [
        ("./backend/Dockerfile", "backend"),
        ("../api/Dockerfile.prod", "api"),
        ("services/auth/Dockerfile", "auth"),
        ("backend/Dockerfile", "backend"),
        ("Dockerfile", None),  # in-place
    ])
    def test_build_dir_extraction(self, target: str, expected) -> None:
        assert _extract_build_directory(target) == expected


# ─── Materializer ───────────────────────────────────────────────────


def _make_dockerfile_extraction(
    file: str = "Dockerfile",
    image_name: str = "myapp",
) -> ExtractionResult:
    """Synthetic Dockerfile extraction result з image symbol."""
    return ExtractionResult(
        symbols=[
            SymbolInfo(
                id=f"{file}::__module__", name=file, kind="file_module",
                file=file, start_line=1, language="dockerfile",
            ),
            SymbolInfo(
                id=f"{file}::stage_0", name="stage_0", kind="image",
                file=file, start_line=1, language="dockerfile",
            ),
        ],
        edges=[
            EdgeInfo(
                from_id=f"{file}::stage_0",
                to_id=None,
                kind="BUILT_FROM",
                confidence="unresolved",
                raw_target=f"{image_name}:base",
            ),
        ],
    )


def _make_compose_extraction(
    file: str = "docker-compose.yml",
    image_target: str | None = None,
    build_target: str | None = None,
) -> ExtractionResult:
    """Synthetic compose extraction з RUNS_IMAGE або BUILT_FROM edge."""
    edges = []
    if image_target:
        edges.append(EdgeInfo(
            from_id=f"{file}::api",
            to_id=None,
            kind="RUNS_IMAGE",
            confidence="strong",
            raw_target=image_target,
        ))
    if build_target:
        edges.append(EdgeInfo(
            from_id=f"{file}::api",
            to_id=None,
            kind="BUILT_FROM",
            confidence="strong",
            raw_target=build_target,
        ))
    return ExtractionResult(
        symbols=[
            SymbolInfo(
                id=f"{file}::__module__", name=file, kind="file_module",
                file=file, start_line=1, language="compose",
            ),
            SymbolInfo(
                id=f"{file}::api", name="api", kind="service",
                file=file, start_line=1, language="compose",
            ),
        ],
        edges=edges,
    )


class TestImageReferenceResolver:
    def test_compose_image_matches_dockerfile_repo(self) -> None:
        """compose у repo A references image of repo B."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)

        # Repo "acme-pro-back" з Dockerfile
        m.add_repo(
            "acme-pro-back",
            {"Dockerfile": _make_dockerfile_extraction()},
        )

        # Repo "acme-pro-deploy" з compose що references image "myorg/pro-back:latest"
        m.add_repo(
            "acme-pro-deploy",
            {"compose.yml": _make_compose_extraction(
                image_target="myorg/pro-back:latest",
            )},
        )

        edges = m.materialize()
        # 1 edge: compose service → Dockerfile у back repo
        ref_edges = [e for e in edges if e.kind == "REFERENCES_REPO"]
        assert len(ref_edges) == 1
        e = ref_edges[0]
        assert e.from_repo == "acme-pro-deploy"
        assert e.to_repo == "acme-pro-back"
        assert e.confidence == "strong"
        assert "pro-back" in e.rationale

    def test_no_match_no_edge(self) -> None:
        """Compose references image with name що НЕ matches any group repo."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)
        m.add_repo("repo-a", {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(
            "repo-b",
            {"compose.yml": _make_compose_extraction(
                image_target="completely-different-image:v1",
            )},
        )
        edges = m.materialize()
        ref_edges = [e for e in edges if e.kind == "REFERENCES_REPO"]
        assert len(ref_edges) == 0

    def test_within_repo_no_cross_edge(self) -> None:
        """Якщо compose і Dockerfile у тому ж repo — НЕ cross-repo edge."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)
        m.add_repo(
            "acme-pro-back",
            {
                "Dockerfile": _make_dockerfile_extraction(),
                "compose.yml": _make_compose_extraction(
                    image_target="myorg/pro-back:v1",
                ),
            },
        )
        edges = m.materialize()
        # Усі resolvers — обмежують within-repo (target_slug == slug skip)
        ref_edges = [e for e in edges if e.kind == "REFERENCES_REPO"]
        assert len(ref_edges) == 0

    def test_multi_repo_chain(self) -> None:
        """3 repos: front → uses 'api' image; api → uses 'db' image."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)

        m.add_repo("acme-api", {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo("acme-db", {"Dockerfile": _make_dockerfile_extraction()})

        # frontend uses 'api' image
        m.add_repo(
            "acme-front",
            {"compose.yml": _make_compose_extraction(
                image_target="acme/api:latest",
            )},
        )
        # api compose uses 'db' image
        m.add_repo(
            "acme-deploy",
            {"compose.yml": _make_compose_extraction(
                image_target="acme/db:16",
            )},
        )

        edges = m.materialize()
        ref_edges = [e for e in edges if e.kind == "REFERENCES_REPO"]
        # 2 cross-repo edges: front→api, deploy→db
        assert len(ref_edges) == 2
        targets = {(e.from_repo, e.to_repo) for e in ref_edges}
        assert ("acme-front", "acme-api") in targets
        assert ("acme-deploy", "acme-db") in targets


class TestBuildContextResolver:
    def test_compose_build_path_resolves_to_repo(self) -> None:
        """Compose `build: ./backend/Dockerfile` → repo з name 'backend'."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)
        m.add_repo("acme-backend", {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(
            "acme-deploy",
            {"compose.yml": _make_compose_extraction(
                build_target="./backend/Dockerfile",
            )},
        )

        edges = m.materialize()
        build_edges = [e for e in edges if e.kind == "BUILD_CONTEXT"]
        assert len(build_edges) == 1
        e = build_edges[0]
        assert e.from_repo == "acme-deploy"
        assert e.to_repo == "acme-backend"
        assert e.confidence == "weak"

    def test_dockerfile_built_from_not_treated_as_build_context(self) -> None:
        """Dockerfile BUILT_FROM `python:3.13` — це base image, not cross-repo."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)
        m.add_repo("repo-a", {"Dockerfile": _make_dockerfile_extraction(
            image_name="python",  # raw_target = 'python:base'
        )})
        m.add_repo("python", {"Dockerfile": _make_dockerfile_extraction()})

        edges = m.materialize()
        build_edges = [e for e in edges if e.kind == "BUILD_CONTEXT"]
        # 'python:base' — not path-based, no cross-repo build context
        assert len(build_edges) == 0


class TestEmptyAndEdgeCases:
    def test_empty_group_no_edges(self) -> None:
        group = RepoGroup(name="empty")
        m = CrossRepoMaterializer(group)
        edges = m.materialize()
        assert edges == []

    def test_single_repo_no_cross_edges(self) -> None:
        """Одна repo — не може мати cross-repo edges."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)
        m.add_repo(
            "solo",
            {"compose.yml": _make_compose_extraction(image_target="any/img:v1")},
        )
        edges = m.materialize()
        assert edges == []

    def test_repos_without_dockerfile_no_image_edges(self) -> None:
        """Якщо у group немає Dockerfiles — image refs не resolve."""
        group = RepoGroup(name="g")
        m = CrossRepoMaterializer(group)
        m.add_repo(
            "repo1",
            {"compose.yml": _make_compose_extraction(image_target="foo/bar:v1")},
        )
        m.add_repo(
            "repo2",
            {"compose.yml": _make_compose_extraction(image_target="bar/foo:v2")},
        )
        edges = m.materialize()
        ref_edges = [e for e in edges if e.kind == "REFERENCES_REPO"]
        assert len(ref_edges) == 0


class TestHyphenatedOwner:
    """An owner containing '-' is the common case on GitHub, and the slug
    cannot be un-flattened to recover the boundary.

    Every test above uses a single-word owner ('acme', 'pallets'), so the
    guess in _extract_repo_name() always happened to be right and the seam
    between "what the group knows" and "what the resolver matches on" was
    never exercised. These pin the seam.
    """

    OWNER = "celmis-codereviewer"
    PROBE = f"github:{OWNER}/celmis-e2e-probe"
    SIBLING = f"github:{OWNER}/celmis-e2e-sibling"
    PROBE_SLUG = "github_celmis-codereviewer-celmis-e2e-probe"
    SIBLING_SLUG = "github_celmis-codereviewer-celmis-e2e-sibling"

    def test_the_group_knows_the_name_the_slug_lost(self) -> None:
        group = RepoGroup(name="g", repos=[self.PROBE, self.SIBLING])
        names = _names_from_group(group)
        assert names[self.PROBE_SLUG] == "celmis-e2e-probe"
        assert names[self.SIBLING_SLUG] == "celmis-e2e-sibling"
        # The guess this replaces gets it wrong, which is the whole point.
        assert _extract_repo_name(self.PROBE_SLUG) != "celmis-e2e-probe"

    def test_build_context_resolves_for_a_hyphenated_owner(self) -> None:
        """`build: ./celmis-e2e-probe/Dockerfile` in the sibling → an edge."""
        group = RepoGroup(name="g", repos=[self.PROBE, self.SIBLING])
        m = CrossRepoMaterializer(group)
        m.add_repo(self.PROBE_SLUG, {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(self.SIBLING_SLUG, {
            "docker-compose.yml": _make_compose_extraction(
                build_target="./celmis-e2e-probe/Dockerfile",
            ),
        })

        build_edges = [e for e in m.materialize() if e.kind == "BUILD_CONTEXT"]
        assert len(build_edges) == 1
        assert build_edges[0].from_repo == self.SIBLING_SLUG
        assert build_edges[0].to_repo == self.PROBE_SLUG

    def test_image_reference_resolves_for_a_hyphenated_owner(self) -> None:
        group = RepoGroup(name="g", repos=[self.PROBE, self.SIBLING])
        m = CrossRepoMaterializer(group)
        m.add_repo(self.PROBE_SLUG, {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(self.SIBLING_SLUG, {
            "docker-compose.yml": _make_compose_extraction(
                image_target="celmis-e2e-probe:latest",
            ),
        })

        ref_edges = [e for e in m.materialize() if e.kind == "REFERENCES_REPO"]
        assert len(ref_edges) == 1
        assert ref_edges[0].to_repo == self.PROBE_SLUG
        assert ref_edges[0].confidence == "strong"

    def test_a_slug_the_group_does_not_name_still_falls_back(self) -> None:
        """The guess is not deleted — it covers a repo added out of band."""
        group = RepoGroup(name="g", repos=[])
        m = CrossRepoMaterializer(group)
        m.add_repo("acme-backend", {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo("acme-deploy", {
            "compose.yml": _make_compose_extraction(
                build_target="./backend/Dockerfile",
            ),
        })

        build_edges = [e for e in m.materialize() if e.kind == "BUILD_CONTEXT"]
        assert len(build_edges) == 1

    def test_an_unparseable_identifier_does_not_sink_the_group(self) -> None:
        group = RepoGroup(name="g", repos=[self.PROBE])
        group.repos.append("")  # bypasses add_repo() validation, as a bad YAML would
        names = _names_from_group(group)
        assert names[self.PROBE_SLUG] == "celmis-e2e-probe"


class TestAmbiguousImageNames:
    """A repository name does not identify a repository.

    The Dockerfile index is keyed on the bare name, so a group holding
    `acme/api` and `partner/api` had one key with two targets — and the
    resolver drew a strong edge to BOTH. One of the two was always wrong, and
    it claimed the same confidence as a real match.

    The image reference usually carries the owner. When it does, it says which
    one it means; when it does not, the guess is a guess and must not read as
    certainty.
    """

    ACME = "github:acme/api"
    PARTNER = "github:partner/api"
    DEPLOY = "github:acme/deploy"
    ACME_SLUG = "github_acme-api"
    PARTNER_SLUG = "github_partner-api"
    DEPLOY_SLUG = "github_acme-deploy"

    def _materializer(self, image_target: str) -> CrossRepoMaterializer:
        group = RepoGroup(name="g", repos=[self.ACME, self.PARTNER, self.DEPLOY])
        m = CrossRepoMaterializer(group)
        m.add_repo(self.ACME_SLUG, {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(self.PARTNER_SLUG, {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(self.DEPLOY_SLUG, {
            "compose.yml": _make_compose_extraction(image_target=image_target),
        })
        return m

    def test_the_owner_in_the_image_picks_the_repo(self):
        edges = [e for e in self._materializer("acme/api:v1").materialize()
                 if e.kind == "REFERENCES_REPO"]

        assert len(edges) == 1
        assert edges[0].to_repo == self.ACME_SLUG
        assert edges[0].confidence == "strong"

    def test_the_other_owner_picks_the_other_repo(self):
        edges = [e for e in self._materializer("partner/api:v1").materialize()
                 if e.kind == "REFERENCES_REPO"]

        assert [e.to_repo for e in edges] == [self.PARTNER_SLUG]

    def test_a_registry_host_is_not_mistaken_for_an_owner(self):
        edges = [e for e in self._materializer("ghcr.io/acme/api:v1").materialize()
                 if e.kind == "REFERENCES_REPO"]

        assert [e.to_repo for e in edges] == [self.ACME_SLUG]

    def test_a_bare_name_is_ambiguous_and_says_so(self):
        edges = [e for e in self._materializer("api:v1").materialize()
                 if e.kind == "REFERENCES_REPO"]

        assert len(edges) == 2, "both are still candidates"
        assert {e.confidence for e in edges} == {"weak"}, (
            "an ambiguous guess claimed the confidence of an exact match"
        )

    def test_a_single_candidate_is_still_strong(self):
        group = RepoGroup(name="g", repos=[self.ACME, self.DEPLOY])
        m = CrossRepoMaterializer(group)
        m.add_repo(self.ACME_SLUG, {"Dockerfile": _make_dockerfile_extraction()})
        m.add_repo(self.DEPLOY_SLUG, {
            "compose.yml": _make_compose_extraction(image_target="api:v1"),
        })

        edges = [e for e in m.materialize() if e.kind == "REFERENCES_REPO"]
        assert [e.confidence for e in edges] == ["strong"]


class TestRegistryPortIsNotATag:
    @pytest.mark.parametrize("image,expected", [
        ("ghcr.io:443/acme/pro-back", "pro-back"),   # port, no tag
        ("localhost:5000/api", "api"),
        ("registry.local:5000/team/api:v1", "api"),  # port AND tag
        ("nginx:latest", "nginx"),
        ("docker.io/library/redis@sha256:abc", "redis"),
    ])
    def test_the_host_port_survives_tag_stripping(self, image, expected):
        assert _image_base_name(image) == expected
