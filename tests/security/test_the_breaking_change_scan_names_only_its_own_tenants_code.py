"""The breaking-change agent names consumers from its own tenant's code only.

It lists every repository indexed on disk and asks each for callers of the
symbols a pull request changed; the slugs, files and caller names it finds go
into a comment on that pull request and a notification to its workspace. Under
multi_tenant "every repository on disk" is every tenant's code, and the scan
did not filter it: a pull request in one tenant could print the file paths of
another's. The deprecation consumer scan had the filter; this one did not.
Both now go through `tenancy.within_workspace`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.mcp_server import tenancy, tools
from src.review import breaking_change

OURS = {"acme-api", "acme-web", "acme-worker"}
THEIRS = "rival-billing"
DIFF = "+def charge(amount, currency):\n+    return amount\n"


@pytest.fixture
def disk(monkeypatch):
    """Four repositories on disk, each with one caller of every symbol."""
    asked: list[str] = []

    def find_callers(symbol_id, repo_slug):  # noqa: ANN001, ANN202
        asked.append(repo_slug)
        return {"callers": [{"name": f"use_{symbol_id}", "file": f"{repo_slug}/secret_path.py",
                             "start_line": 7}]}

    monkeypatch.setattr(tools, "list_repos", lambda *a, **k: [
        SimpleNamespace(slug=s) for s in sorted(OURS | {THEIRS})])
    monkeypatch.setattr(tools, "find_callers", find_callers)
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
    pr = SimpleNamespace(raw_diff=DIFF, local_slug="acme-api", changed_files=["app/pay.py"],
                         repo="acme/api", number=7, title="Change charge", url="https://x.example/7")
    return SimpleNamespace(pull_request=pr, workspace_id=workspace_id)


def test_under_multi_tenant_another_tenants_repository_is_never_asked_or_named(
        disk, monkeypatch):
    _mode(monkeypatch, "multi_tenant")
    sent: list[dict] = []
    monkeypatch.setattr("src.notifications.notify", lambda **kw: sent.append(kw))

    out = breaking_change.run_breaking_change(_context("ws-acme"))

    assert THEIRS not in disk
    assert sorted(disk) == ["acme-web", "acme-worker"]  # its own repo excluded too
    (finding,) = out.findings
    assert THEIRS not in finding.body and "acme-web" in finding.body
    assert sent and all(THEIRS not in kw["body_md"] for kw in sent)


def test_under_multi_tenant_an_unknown_or_empty_workspace_scans_nothing(disk, monkeypatch):
    _mode(monkeypatch, "multi_tenant")
    for ws in ("", "ws-nobody"):
        sym = [breaking_change.ChangedSymbol(kind="function", name="charge")]
        assert breaking_change._find_consumers_across_repos("acme-api", sym, ws) == {}
    assert disk == []


def test_under_multi_tenant_an_unreadable_registry_scans_nothing(monkeypatch):
    _mode(monkeypatch, "multi_tenant")
    asked: list[str] = []
    monkeypatch.setattr(tools, "list_repos", lambda *a, **k: [SimpleNamespace(slug=THEIRS)])
    monkeypatch.setattr(tools, "find_callers",
                        lambda symbol_id, repo_slug: asked.append(repo_slug) or {"callers": []})

    def broken():  # noqa: ANN202
        raise RuntimeError("registry down")

    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", broken)
    sym = [breaking_change.ChangedSymbol(kind="function", name="charge")]
    assert breaking_change._find_consumers_across_repos("acme-api", sym, "ws-acme") == {}
    assert asked == []


def test_under_single_tenant_every_other_repository_is_still_scanned(disk, monkeypatch):
    _mode(monkeypatch, "single_tenant")
    sym = [breaking_change.ChangedSymbol(kind="function", name="charge")]
    found = breaking_change._find_consumers_across_repos("acme-api", sym, "ws-acme")
    assert sorted(c["repo_slug"] for c in found["charge"]) == sorted(
        (OURS | {THEIRS}) - {"acme-api"})


def test_the_deprecation_scan_uses_the_same_filter(disk, monkeypatch):
    from src.api.routers import intel

    _mode(monkeypatch, "multi_tenant")
    rows = intel._scan_consumers("charge", workspace_id="ws-acme")
    assert {r["repo_slug"] for r in rows} == OURS
