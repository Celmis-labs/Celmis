"""The cross-repository impact lookup reads facts of its own workspace only.

Recorded facts name files, lines and hosts of a repository. The breaking-change
agent prints them into a pull request comment, so a lookup that opened another
tenant's facts would leak them even if it filtered the output afterwards. The
facts must never be loaded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.mcp_server import tenancy, tools
from src.review import breaking_change
from tests.review._xrepo_support import APP, BILLING, CONSUMER, pull_request, serve_facts

OWNER = "acme-billing-service"


@pytest.fixture(autouse=True)
def tenants(monkeypatch):
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()
    monkeypatch.setattr(tools, "list_repo_slugs", lambda: [OWNER, "acme-web-shop", "rival-app"])
    monkeypatch.setattr(
        tenancy, "workspace_slugs",
        lambda ws: {"ws-acme": {OWNER, "acme-web-shop"}, "ws-rival": {"rival-app"}}.get(ws, set()))
    monkeypatch.setattr("src.notifications.notify", lambda **kw: None)
    yield
    reset_mode_cache()


def _run(monkeypatch, workspace: str):
    asked = serve_facts(monkeypatch, {OWNER: BILLING, "acme-web-shop": CONSUMER,
                                      "rival-app": CONSUMER})
    pr, provider = pull_request(
        {"app/main.py": (APP, APP.replace('"/invoices/{id}/pay"', '"/v2/pay/{id}"'))})
    ctx = SimpleNamespace(pull_request=pr, workspace_id=workspace, file_provider=provider)
    return breaking_change.run_breaking_change(ctx), asked


def test_another_tenants_facts_are_never_loaded_and_never_named(monkeypatch) -> None:
    out, asked = _run(monkeypatch, "ws-acme")
    assert "rival-app" not in asked
    assert out.findings
    assert all("rival-app" not in f.body and "rival-app" not in f.title for f in out.findings)


def test_the_other_tenant_sees_nothing_of_ours(monkeypatch) -> None:
    out, asked = _run(monkeypatch, "ws-rival")
    assert "acme-web-shop" not in asked
    assert all("acme-web-shop" not in f.body for f in out.findings)


def test_an_empty_workspace_loads_no_consumer_facts(monkeypatch) -> None:
    out, asked = _run(monkeypatch, "")
    assert out.findings == []
    assert set(asked) <= {OWNER}
