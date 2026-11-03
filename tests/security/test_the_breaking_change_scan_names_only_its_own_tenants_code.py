"""The breaking-change agent names consumers from its own tenant's code only.

It lists every repository indexed on disk and reads each one's recorded
cross-repository facts (routes, calls, imports) for consumers of what a pull
request changed; the slugs, files and lines it finds go into a comment on that
pull request and a notification to its workspace. Under
multi_tenant "every repository on disk" is every tenant's code, and the scan
did not filter it: a pull request in one tenant could print the file paths of
another's. The deprecation consumer scan had the filter; this one did not.
Both now go through `tenancy.within_workspace`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.config import get_settings
from src.groups import impact
from src.indexing.graph.xrepo import CallFact, FactSet, PublishedPackage, RouteFact
from src.mcp_server import tenancy, tools
from src.review import breaking_change

OURS = {"acme-api", "acme-web", "acme-worker"}
THEIRS = "rival-billing"

API = FactSet(
    routes=[RouteFact("POST", "/charge", "app/pay.py", 3, "charge", "fastapi")],
    published=[PublishedPackage("pypi", "acme-api-client", "pyproject.toml", ["acme_api"])],
)
CALLER = FactSet(
    calls=[CallFact("POST", "/charge", "secret_path.py", 7, "api", "", "httpx", "use")],
    imports=[],
)
OLD = "from fastapi import APIRouter\nr = APIRouter()\n\n\n@r.post(\"/charge\")\ndef charge(a):\n    return a\n"
NEW = OLD.replace('"/charge"', '"/charge/v2"')


@pytest.fixture
def disk(monkeypatch):
    """Four repositories on disk, each with one caller of the changed route."""
    asked: list[str] = []
    settings = get_settings()
    paths = {str(settings.repo_graph_path(s)): s for s in sorted(OURS | {THEIRS})}

    def load(path):  # noqa: ANN001, ANN202
        slug = paths[str(path)]
        asked.append(slug)
        return API if slug == "acme-api" else CALLER

    monkeypatch.setattr(tools, "list_repo_slugs", lambda: sorted(OURS | {THEIRS}))
    monkeypatch.setattr(impact, "load_facts", load)
    monkeypatch.setattr(tenancy, "workspace_slugs",
                        lambda ws: set(OURS) if ws == "ws-acme" else set())
    return asked


def _mode(monkeypatch, mode: str) -> None:
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", mode)
    reset_mode_cache()


@pytest.fixture(autouse=True)
def _reset_mode():
    from src.deployment import reset_mode_cache

    yield
    reset_mode_cache()


def _context(workspace_id: str) -> SimpleNamespace:
    from tests.review._xrepo_support import pull_request

    pr, provider = pull_request({"app/pay.py": (OLD, NEW)}, repo="acme/api")
    return SimpleNamespace(pull_request=pr, workspace_id=workspace_id, file_provider=provider)


def test_under_multi_tenant_another_tenants_repository_is_never_read_or_named(
        disk, monkeypatch):
    _mode(monkeypatch, "multi_tenant")
    sent: list[dict] = []
    monkeypatch.setattr("src.notifications.notify", lambda **kw: sent.append(kw))

    out = breaking_change.run_breaking_change(_context("ws-acme"))

    assert THEIRS not in disk
    # the changed repository's own facts are read as the owner, never as a consumer
    assert sorted(set(disk) - {"acme-api"}) == ["acme-web", "acme-worker"]
    assert out.findings
    for finding in out.findings:
        assert THEIRS not in finding.body and "acme-web" in finding.body
    assert sent and all(THEIRS not in kw["body_md"] for kw in sent)


def test_under_multi_tenant_an_unknown_or_empty_workspace_reads_nothing(disk, monkeypatch):
    _mode(monkeypatch, "multi_tenant")
    for ws in ("", "ws-nobody"):
        out = breaking_change.run_breaking_change(_context(ws))
        assert out.findings == []
    assert set(disk) <= {"acme-api"}


def test_under_multi_tenant_an_unreadable_registry_reads_nothing(monkeypatch):
    _mode(monkeypatch, "multi_tenant")
    asked: list[str] = []
    monkeypatch.setattr(tools, "list_repo_slugs", lambda: [THEIRS])
    monkeypatch.setattr(impact, "load_facts", lambda p: asked.append(str(p)) or CALLER)

    def broken():  # noqa: ANN202
        raise RuntimeError("registry down")

    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", broken)
    out = breaking_change.run_breaking_change(_context("ws-acme"))
    assert out.findings == [] and all("rival" not in p for p in asked)
    assert all(p.endswith("acme-api/graph.fdblite") for p in asked)


def test_under_single_tenant_every_other_repository_is_still_read(disk, monkeypatch):
    _mode(monkeypatch, "single_tenant")
    breaking_change.run_breaking_change(_context("ws-acme"))
    assert sorted(set(disk) - {"acme-api"}) == sorted((OURS | {THEIRS}) - {"acme-api"})


def test_the_deprecation_scan_uses_the_same_filter(monkeypatch):
    from src.api.routers import intel

    monkeypatch.setattr(tools, "list_repos", lambda *a, **k: [
        SimpleNamespace(slug=s) for s in sorted(OURS | {THEIRS})])
    monkeypatch.setattr(tools, "find_callers", lambda symbol_id, repo_slug: {
        "callers": [{"name": "use", "file": f"{repo_slug}/x.py", "start_line": 7}]})
    monkeypatch.setattr(tenancy, "workspace_slugs",
                        lambda ws: set(OURS) if ws == "ws-acme" else set())
    _mode(monkeypatch, "multi_tenant")
    rows = intel._scan_consumers("charge", workspace_id="ws-acme")
    assert {r["repo_slug"] for r in rows} == OURS
