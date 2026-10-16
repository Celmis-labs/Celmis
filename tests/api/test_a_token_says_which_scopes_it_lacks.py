"""Verifying a git token says which scopes it lacks, not just "OK".

The catalogue (src/credentials/token_scopes.py) lists what each kind of token
must be allowed to do. Verify compares it with the scopes the provider reports
for the token: a missing required one is named, a missing optional one is named
as optional, and a token whose scopes cannot be read (fine-grained GitHub) is
never accused of lacking anything.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_current_user
from src.api.routers import connections as conn
from src.api.schemas import ConnectionUpsert
from src.credentials import token_scopes as ts

API_TOKEN_ALL = [
    "read:user:bitbucket", "read:workspace:bitbucket", "read:repository:bitbucket",
    "read:pullrequest:bitbucket", "write:pullrequest:bitbucket",
]


def _keys(items):
    return [i["key"] for i in items]


# ── assess() ────────────────────────────────────────────────────────────────


def test_a_bitbucket_api_token_without_read_user_lacks_a_required_scope():
    granted = [s for s in API_TOKEN_ALL if s != "read:user:bitbucket"]
    out = ts.assess("bitbucket:api_token", granted)
    assert out["checked"] is True
    assert _keys(out["missing_required"]) == ["bb_user"]
    assert out["missing_required"][0]["label"] == "read:user:bitbucket"
    # Apply fix and Install webhook are optional, so they are listed as such.
    assert set(_keys(out["missing_optional"])) == {"bb_repo_write", "bb_webhook"}


def test_a_bitbucket_api_token_with_everything_lacks_nothing_required():
    out = ts.assess("bitbucket:api_token", API_TOKEN_ALL + [
        "write:repository:bitbucket", "write:webhook:bitbucket"])
    assert out["missing_required"] == [] and out["missing_optional"] == []


def test_the_webhook_scope_is_optional_for_a_bitbucket_api_token():
    out = ts.assess("bitbucket:api_token", API_TOKEN_ALL)
    assert out["missing_required"] == []
    assert "bb_webhook" in _keys(out["missing_optional"])


def test_a_bitbucket_access_token_is_satisfied_by_the_classic_names():
    out = ts.assess("bitbucket:access_token",
                    ["repository:write", "pullrequest:write", "webhook"])
    assert out["missing_required"] == [] and out["missing_optional"] == []


def test_a_github_classic_token_with_only_repo_lacks_only_optional_scopes():
    out = ts.assess("github:classic", ["repo"])
    assert out["checked"] is True and out["missing_required"] == []
    assert set(_keys(out["missing_optional"])) == {"gh_notifications", "gh_hook"}


def test_a_github_classic_token_without_repo_lacks_it():
    out = ts.assess("github:classic", ["notifications"])
    assert _keys(out["missing_required"]) == ["gh_repo"]


@pytest.mark.parametrize("kind,granted", [
    ("github:fine_grained", []),
    ("github:fine_grained", None),
    ("github:classic", None),
])
def test_a_token_whose_scopes_cannot_be_read_is_not_reported_as_lacking(kind, granted):
    out = ts.assess(kind, granted)
    assert out["checked"] is False
    assert out["missing_required"] == [] and out["missing_optional"] == []


def test_a_gitlab_token_with_read_api_only_lacks_api():
    out = ts.assess("gitlab", ["read_api"])
    assert _keys(out["missing_required"]) == ["gl_api"]
    assert ts.assess("gitlab", ["api"])["missing_required"] == []


# ── token_kind() ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider,token,kind", [
    ("github", "github_pat_11AAA", "github:fine_grained"),
    ("github", "ghp_abc123", "github:classic"),
    ("bitbucket", "ATCTT3xFfGN0abc", "bitbucket:access_token"),
    ("bitbucket", "ATATT3xFfGN0abc", "bitbucket:api_token"),
    ("gitlab", "glpat-abc", "gitlab"),
])
def test_the_kind_of_a_token_is_read_from_its_prefix(provider, token, kind):
    assert ts.token_kind(provider, token) == kind


def test_every_provider_has_a_default_kind_in_the_catalogue():
    ui = ts.catalogue_for_ui()
    for kind in ui["default_kind"].values():
        assert kind in ui["kinds"]


# ── verify (HTTP mocked) ────────────────────────────────────────────────────


def _mock_client(monkeypatch, handler):
    def build_client(**kw):
        return httpx.Client(transport=httpx.MockTransport(handler), auth=kw.get("auth"))

    monkeypatch.setattr("src.http.build_client", build_client)
    return build_client


def _bitbucket(monkeypatch, scopes_header):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        headers = {} if scopes_header is None else {"x-oauth-scopes": scopes_header}
        return httpx.Response(200, json={"slug": "acme"}, headers=headers)

    _mock_client(monkeypatch, handler)
    req = ConnectionUpsert(provider="bitbucket", token="ATATT3xabc",
                           email="dev@acme.example", workspace="acme")
    return conn._verify_token("bitbucket", req), seen


def test_verify_names_the_bitbucket_scope_the_token_lacks(monkeypatch):
    header = ", ".join(s for s in API_TOKEN_ALL if s != "read:user:bitbucket")
    result, seen = _bitbucket(monkeypatch, header)
    assert seen == ["/2.0/workspaces/acme"]
    assert result.ok and result.scopes_checked and result.scopes_known
    assert result.token_kind == "bitbucket:api_token"
    assert _keys(result.missing_scopes) == ["bb_user"]
    assert "bb_webhook" in _keys(result.missing_optional_scopes)


def test_verify_without_a_scopes_header_does_not_claim_anything_is_missing(monkeypatch):
    result, _ = _bitbucket(monkeypatch, None)
    assert result.ok and not result.scopes_checked
    assert result.missing_scopes == [] and result.missing_optional_scopes == []


def test_verify_a_gitlab_token_with_read_api_names_api_as_missing(monkeypatch):
    from src.sync import gitlab_instance as gi

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/personal_access_tokens/self"):
            return httpx.Response(200, json={"scopes": ["read_api"]})
        return httpx.Response(200, json={"username": "robot"})

    monkeypatch.setattr(gi, "validate_base_url", gi.instance_of)
    monkeypatch.setattr(
        gi, "build_gitlab_client",
        lambda instance, **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    req = ConnectionUpsert(provider="gitlab", token="glpat-abc")
    result = conn._verify_token("gitlab", req)
    assert result.ok and result.scopes_checked and result.token_kind == "gitlab"
    assert _keys(result.missing_scopes) == ["gl_api"]


# ── GET /api/connections/scopes ─────────────────────────────────────────────


def test_the_scopes_route_serves_the_catalogue():
    app = FastAPI()
    app.include_router(conn.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u1", email="dev@acme.example")
    res = TestClient(app).get("/api/connections/scopes")
    assert res.status_code == 200
    body = res.json()
    assert set(body["kinds"]) == set(ts.CATALOGUE)
    assert body["default_kind"]["bitbucket"] == "bitbucket:api_token"
    entry = body["kinds"]["bitbucket:api_token"][0]
    assert set(entry) == {"key", "label", "required", "purpose"}
