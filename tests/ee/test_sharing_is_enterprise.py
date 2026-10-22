# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Making a share and giving more of one are licensed; everything that makes it safer is not.

The line (docs/SHARING.md, ee/README.md): if switching a thing off would make the free
build less safe, it is not an enterprise feature. So the licence claim ``sharing``
mounts exactly two routes, creating a share and extending one in place, and nothing
else about sharing depends on a licence: enforcement on every call, narrowing, blocking
a person, ending a share, reading who used it, and putting a share on a token.

A licence that lapses, or is removed, takes the making of new shares away and ends no
share that exists: each runs to its own end or until its owner revokes it. The world is
the HTTP one of tests/api/sharing_world.py, with the licence mounted by
``mount_enterprise`` as production does it (tests/ee/sharing_support.py).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from src.api.routers import capabilities as caps
from src.ee import license as lic
from src.ee import mount_enterprise
from tests.api.rbac_world import B_REPO
from tests.api.sharing_world import HOLDER, HOLDER_EMAIL, WS_A, add_share, share_row
from tests.ee.licensing import mint_test_license, trust_test_key
from tests.ee.sharing_support import GRANTS, create, extend, relicense

WIDENING = ("widening a share needs a Celmis Enterprise licence (sharing); "
            "with one, use POST /api/sharing-grants/{id}/extend")


def _paths(world) -> set[str]:
    return caps.mounted_paths(world.app)


def _reads_the_shared_repository(world, share_id: str) -> bool:
    """The decision for a token of A's member that carries ``share_id`` and no
    repository of its own: the one every call reads, with no licence in it."""
    from src.access.effective import Principal, effective_access

    got = effective_access(
        Principal(user_id=world.uid(HOLDER), repo_patterns=(), share_ids=(share_id,)),
        WS_A, [B_REPO])
    return got[B_REPO].code_visible


# ─── what the licence mounts ─────────────────────────────────────────


async def test_without_a_licence_the_two_grant_routes_are_not_mounted(nw):
    share_id = add_share(nw)

    assert (await create(nw)).status_code == 404
    assert (await extend(nw, share_id, {"days": 60})).status_code == 404
    assert not any(p.startswith(GRANTS) for p in _paths(nw))
    assert nw.app.state.celmis_license is None


async def test_with_the_claim_both_grant_routes_are_mounted(gw):
    paths = _paths(gw)
    assert GRANTS in paths and f"{GRANTS}/{{share_id}}/extend" in paths

    made = await create(gw)
    assert made.status_code == 201, made.text
    assert (await extend(gw, made.json()["share_id"], {"allow_ask": True})).status_code == 200


@pytest.mark.parametrize("features", [["sso"], ["analytics"], ["sso", "analytics"]])
async def test_a_licence_for_other_features_does_not_open_sharing(gw, features):
    relicense(gw, features)

    assert not any(p.startswith(GRANTS) for p in _paths(gw))
    assert (await create(gw)).status_code == 404


def test_the_claim_opens_sharing_and_nothing_else(monkeypatch):
    """On a bare app: the suite's HTTP world mounts the analytics router itself."""
    trust_test_key(monkeypatch)
    app = FastAPI()
    mount_enterprise(app, env={lic.ENV_KEY: mint_test_license(["sharing"])})

    paths = caps.mounted_paths(app)
    assert any(p.startswith(GRANTS) for p in paths)
    assert not any(p.startswith("/api/analytics") for p in paths), \
        "the sharing claim mounted the analytics routes"
    assert app.state.celmis_license["features"] == ["sharing"]


async def test_the_capabilities_document_says_what_is_mounted(gw):
    on = caps.build_capabilities(gw.app)
    assert on.features["repo_sharing"].available is True
    assert on.features["repo_sharing_grant"].available is True
    assert on.license.features == ["sharing"]

    relicense(gw, ())
    off = caps.build_capabilities(gw.app)
    assert off.features["repo_sharing"].available is True, "the free routes never move"
    assert off.features["repo_sharing_grant"].available is False
    assert off.license.features == []


async def test_a_build_that_knows_the_claim_reads_it_from_a_licence(monkeypatch):
    trust_test_key(monkeypatch)
    got = lic.verify(mint_test_license(["sharing"]))

    assert "sharing" in lic.KNOWN_FEATURES
    assert got.features == frozenset({"sharing"}) and got.grants("sharing")
    assert not got.grants("analytics")


async def test_an_unknown_claim_beside_sharing_is_ignored_and_sharing_is_granted(monkeypatch):
    """Unknown claims are ignored (a newer licence on an older build), so a licence that
    names `sharing` beside `from-the-future` grants the one this build has."""
    import time

    import jwt

    from tests.ee.licensing import TEST_KEY

    trust_test_key(monkeypatch)
    now = int(time.time())
    token = jwt.encode(
        {"iss": lic.ISSUER, "sub": "Test Customer Ltd", "iat": now, "exp": now + 3600,
         "features": ["sharing", "from-the-future"]}, TEST_KEY, algorithm="EdDSA")
    assert lic.verify(token).features == frozenset({"sharing"})


# ─── the licence is asked on every request ───────────────────────────


async def test_a_licence_that_lapses_while_the_process_runs_stops_new_shares_only(gw, monkeypatch):
    share_id = add_share(gw)
    assert (await extend(gw, share_id, {"max_people": 6})).status_code == 200

    monkeypatch.setattr(lic.License, "expired", lambda self, now=None: True)

    for r in (await create(gw), await extend(gw, share_id, {"max_people": 7})):
        assert r.status_code == 403, r.text
        assert "expired" in r.json()["detail"]
    assert share_row(gw, share_id).max_people == 6, "a refused extension changed the share"
    assert _reads_the_shared_repository(gw, share_id)


async def test_a_licence_removed_from_the_ui_unmounts_the_grant_routes(gw):
    share_id = add_share(gw)
    assert (await extend(gw, share_id, {"max_people": 6})).status_code == 200

    relicense(gw, ())

    assert (await create(gw)).status_code == 404
    assert (await extend(gw, share_id, {"max_people": 7})).status_code == 404
    assert share_row(gw, share_id).max_people == 6
    assert _reads_the_shared_repository(gw, share_id)


async def test_a_licence_put_back_remounts_the_grant_routes(nw):
    share_id = add_share(nw)
    assert (await extend(nw, share_id, {"max_people": 6})).status_code == 404

    relicense(nw, ["sharing"])

    assert (await extend(nw, share_id, {"max_people": 6})).status_code == 200


# ─── what stays free ─────────────────────────────────────────────────


async def test_a_share_that_exists_is_enforced_without_any_licence(nw):
    share_id = add_share(nw)

    assert _reads_the_shared_repository(nw, share_id)
    assert nw.app.state.celmis_license is None


@pytest.mark.parametrize("body", [
    {"add_deny": ["docs/**"]},
    {"allow_ask": False},
    {"max_people": 4},
    {"block": ["somebody"]},
])
async def test_narrowing_a_share_needs_no_licence(nw, body):
    share_id = add_share(nw, ask=True)

    r = await nw.client.patch(f"/api/sharing/shares/{share_id}", json=body,
                              headers=nw.h("admin_b", "ws-b"))

    assert r.status_code == 200, r.text


async def test_widening_through_the_free_route_still_says_it_needs_the_licence(nw):
    share_id = add_share(nw, days=10)
    r = await nw.client.patch(
        f"/api/sharing/shares/{share_id}", headers=nw.h("admin_b", "ws-b"),
        json={"expires_at": "2099-01-01T00:00:00Z"})

    assert r.status_code == 409 and r.json()["detail"] == WIDENING


async def test_listing_reading_and_the_usage_of_a_share_need_no_licence(nw):
    share_id = add_share(nw)
    for url in ("/api/sharing/shares", f"/api/sharing/shares/{share_id}",
                f"/api/sharing/shares/{share_id}/activity"):
        r = await nw.client.get(url, headers=nw.h("admin_b", "ws-b"))
        assert r.status_code == 200, (url, r.text)


async def test_revoking_a_share_needs_no_licence(nw):
    share_id = add_share(nw)

    r = await nw.client.post(f"/api/sharing/shares/{share_id}/revoke",
                             headers=nw.h("admin_b", "ws-b"))

    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert not _reads_the_shared_repository(nw, share_id)


async def test_putting_a_share_on_a_token_needs_no_licence(nw):
    share_id = add_share(nw)

    r = await nw.client.post(
        "/api/admin/mcp-tokens", headers=nw.h("su", "ws-a"),
        json={"user_ref": HOLDER_EMAIL, "workspace_id": "ws-a", "label": "laptop",
              "repos": [], "shares": [share_id]})

    assert r.status_code == 201, r.text
    assert [s["share_id"] for s in r.json()["shares"]] == [share_id]


# ─── the community build ─────────────────────────────────────────────


def test_the_free_half_of_sharing_does_not_import_the_licensed_half():
    """The import graph is pinned for the whole tree by
    tests/api/test_the_community_build_needs_no_ee.py; this is the one reading of it
    that names sharing, and it looks at every import, including the ones inside a
    function."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    free = [root / "src" / "access" / "shares.py", root / "src" / "api" / "routers" / "sharing.py"]
    for path in free:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n == "src.ee" or n.startswith("src.ee.") for n in names), \
                f"{path.name} imports the enterprise package"


def test_the_application_without_the_package_has_the_free_routes_and_not_the_grant_ones(
        monkeypatch):
    import sys

    monkeypatch.setenv("CELMIS_JWT_SECRET", "test-community-secret-0123456789abcdef")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345678")
    for name in [m for m in sys.modules if m == "src.ee" or m.startswith("src.ee.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "src.ee", None)

    from src.api.main import build_app

    app: FastAPI = build_app()
    doc = caps.build_capabilities(app)
    paths = caps.mounted_paths(app)

    assert doc.edition == "community"
    assert doc.features["repo_sharing"].available is True
    assert doc.features["repo_sharing_grant"].available is False
    assert doc.pages["/admin/sharing"] is True
    assert "/api/sharing/shares" in paths
    assert not any(p.startswith(GRANTS) for p in paths)


def test_mounting_twice_does_not_mount_the_routes_twice(monkeypatch):
    trust_test_key(monkeypatch)
    app = FastAPI()
    token = mint_test_license(["sharing"])
    mount_enterprise(app, env={lic.ENV_KEY: token})
    first = [p for p in caps.mounted_paths(app) if p.startswith(GRANTS)]
    mount_enterprise(app, env={lic.ENV_KEY: token})
    second = [p for p in caps.mounted_paths(app) if p.startswith(GRANTS)]

    assert sorted(first) == sorted(second)
    assert sorted(first) == [GRANTS, f"{GRANTS}/workspaces", f"{GRANTS}/{{share_id}}/extend"]
