"""`review_scope` through the real routers.

Same shape as the other review settings: repo > workspace > built-in
(`incremental`), each answer saying where it came from, a value outside the
closed list refused (422, nothing saved), absent keeps, null inherits.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, world

DEFAULTS = "/api/review-defaults"
POLICY = f"/api/review-policies/{A_REPO}"


async def _get(w, url, who="member_a"):
    r = await w.client.get(url, headers=w.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    return r.json()


async def _put(w, url, who, body):
    return await w.client.put(url, json=body, headers=w.h(who, "ws-a"))


async def test_both_layers_read_incremental_as_the_builtin(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        d, p = await _get(w, DEFAULTS), await _get(w, POLICY)

        assert d["review_scope"] is None and d["effective"]["review_scope"] == "incremental"
        assert d["sources"]["review_scope"] == "install"
        assert p["review_scope"] is None and p["review_scope_effective"] == "incremental"
        assert p["sources"]["review_scope"] == "install"
        assert d["setting_choices"]["review_scope"] == ["incremental", "full"]
        assert p["setting_choices"]["review_scope"] == ["incremental", "full"]


async def test_the_workspace_value_reaches_the_repo_and_the_repo_wins(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        d = (await _put(w, DEFAULTS, "admin_a", {"review_scope": "full"})).json()
        assert d["review_scope"] == "full" and d["sources"]["review_scope"] == "workspace"

        p = await _get(w, POLICY)
        assert p["review_scope"] is None and p["review_scope_effective"] == "full"
        assert p["sources"]["review_scope"] == "workspace"

        p = (await _put(w, POLICY, "editor_a", {"review_scope": "incremental"})).json()
        assert p["review_scope"] == "incremental"
        assert p["review_scope_effective"] == "incremental"
        assert p["sources"]["review_scope"] == "repo"
        assert p["inherited"]["review_scope"] == "full"


async def test_absent_keeps_and_null_inherits(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, DEFAULTS, "admin_a", {"review_scope": "full"})

        d = (await _put(w, DEFAULTS, "admin_a", {"approve_when_clean": True})).json()
        assert d["review_scope"] == "full", "absent must keep what is stored"

        d = (await _put(w, DEFAULTS, "admin_a", {"review_scope": None})).json()
        assert d["review_scope"] is None and d["effective"]["review_scope"] == "incremental"


async def test_a_value_is_trimmed_and_lowercased(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        d = (await _put(w, DEFAULTS, "admin_a", {"review_scope": " Full "})).json()

        assert d["review_scope"] == "full"


async def test_a_blank_value_is_an_inherit(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, DEFAULTS, "admin_a", {"review_scope": "full"})

        d = (await _put(w, DEFAULTS, "admin_a", {"review_scope": "  "})).json()

        assert d["review_scope"] is None and d["effective"]["review_scope"] == "incremental"


@pytest.mark.parametrize("value", ["whole", "incremental,full", 3])
async def test_both_layers_refuse_a_value_outside_the_list_and_save_nothing(
        tmp_path, monkeypatch, value):
    async with world(tmp_path, monkeypatch) as w:
        for url, who in ((DEFAULTS, "admin_a"), (POLICY, "editor_a")):
            r = await _put(w, url, who, {"review_scope": value, "approve_when_clean": True})

            assert r.status_code == 422 and "review_scope" in r.text, r.text
            assert (await _get(w, url))["approve_when_clean"] is None


@pytest.mark.parametrize("who", ["viewer_a", "member_a", "editor_a"])
async def test_the_workspace_layer_keeps_its_gate(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, who, {"review_scope": "full"})

        assert r.status_code == 403
        assert (await _get(w, DEFAULTS))["review_scope"] is None
