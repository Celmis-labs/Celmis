"""Cadence and title-keyword settings through the real routers.

Same shape as the other 2.3 settings: repo > workspace > built-in, each
answer saying where it came from, bad values refused (422, nothing saved),
absent keeps, null inherits, keywords normalised, audit by field name only.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, world

DEFAULTS = "/api/review-defaults"
POLICY = f"/api/review-policies/{A_REPO}"

VALUES = {
    "review_cadence": "auto_pause",
    "auto_pause_pushes": 5,
    "auto_pause_window_minutes": 30,
    "ignored_title_keywords": ["WIP", "[skip review]"],
}
BUILTINS = {
    "review_cadence": "automatic",
    "auto_pause_pushes": 3,
    "auto_pause_window_minutes": 15,
    "ignored_title_keywords": [],
}
BAD = [
    ({"review_cadence": "hourly"}, "review_cadence"),
    ({"auto_pause_pushes": 1}, "auto_pause_pushes"),
    ({"auto_pause_pushes": 21}, "auto_pause_pushes"),
    ({"auto_pause_pushes": "many"}, "auto_pause_pushes"),
    ({"auto_pause_window_minutes": 0}, "auto_pause_window_minutes"),
    ({"auto_pause_window_minutes": 241}, "auto_pause_window_minutes"),
    ({"ignored_title_keywords": ["x" * 101]}, "ignored_title_keywords"),
    ({"ignored_title_keywords": [f"k{i}" for i in range(51)]}, "ignored_title_keywords"),
    ({"ignored_title_keywords": "WIP"}, "ignored_title_keywords"),
]


async def _get(w, url, who="member_a"):
    r = await w.client.get(url, headers=w.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    return r.json()


async def _put(w, url, who, body):
    return await w.client.put(url, json=body, headers=w.h(who, "ws-a"))


async def test_both_layers_read_the_builtins_with_their_source(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        d, p = await _get(w, DEFAULTS), await _get(w, POLICY)
        for name, builtin in BUILTINS.items():
            assert d[name] is None and d["effective"][name] == builtin, name
            assert d["sources"][name] == "install", name
            assert p[name] is None and p[f"{name}_effective"] == builtin, name
            assert p["sources"][name] == "install", name
        assert d["setting_choices"]["review_cadence"] == ["automatic", "auto_pause", "manual"]
        assert p["setting_choices"]["review_cadence"] == ["automatic", "auto_pause", "manual"]


async def test_the_workspace_value_reaches_the_repo_and_the_repo_wins(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, "admin_a", VALUES)
        assert r.status_code == 200, r.text
        d = r.json()
        for name, value in VALUES.items():
            assert d[name] == value and d["effective"][name] == value, name
            assert d["sources"][name] == "workspace", name

        p = await _get(w, POLICY)
        for name, value in VALUES.items():
            assert p[name] is None and p[f"{name}_effective"] == value, name
            assert p["sources"][name] == "workspace", name
            assert p["inherited_sources"][name] == "workspace", name

        own = {"review_cadence": "manual", "auto_pause_pushes": 8,
               "auto_pause_window_minutes": 60, "ignored_title_keywords": ["Revert"]}
        r = await _put(w, POLICY, "editor_a", own)
        assert r.status_code == 200, r.text
        p = r.json()
        for name, value in own.items():
            assert p[name] == value and p[f"{name}_effective"] == value, name
            assert p["sources"][name] == "repo", name
            assert p["inherited"][name] == VALUES[name], name


async def test_a_repo_can_clear_an_inherited_list_with_an_empty_one(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, DEFAULTS, "admin_a", {"ignored_title_keywords": ["WIP"]})
        p = (await _put(w, POLICY, "editor_a", {"ignored_title_keywords": []})).json()
        assert p["ignored_title_keywords"] == []
        assert p["ignored_title_keywords_effective"] == []
        assert p["sources"]["ignored_title_keywords"] == "repo"


async def test_absent_keeps_and_null_inherits(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, DEFAULTS, "admin_a", VALUES)
        d = (await _put(w, DEFAULTS, "admin_a", {"auto_pause_pushes": None})).json()
        assert d["auto_pause_pushes"] is None
        assert d["effective"]["auto_pause_pushes"] == BUILTINS["auto_pause_pushes"]
        assert d["review_cadence"] == "auto_pause", "absent must keep what is stored"
        assert d["ignored_title_keywords"] == VALUES["ignored_title_keywords"]


async def test_keywords_are_trimmed_deduplicated_and_blanks_dropped(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        d = (await _put(w, DEFAULTS, "admin_a", {
            "ignored_title_keywords": ["  WIP ", "wip", "", "  ", "Revert"],
            "review_cadence": " Manual "})).json()
        assert d["ignored_title_keywords"] == ["WIP", "Revert"]
        assert d["review_cadence"] == "manual"


@pytest.mark.parametrize("body, named", BAD)
async def test_both_layers_refuse_bad_values_and_save_nothing(tmp_path, monkeypatch,
                                                              body, named):
    async with world(tmp_path, monkeypatch) as w:
        for url, who in ((DEFAULTS, "admin_a"), (POLICY, "editor_a")):
            r = await _put(w, url, who, {**body, "approve_when_clean": True})
            assert r.status_code == 422 and named in r.text, r.text
            assert (await _get(w, url))["approve_when_clean"] is None


async def test_the_audit_trail_names_fields_not_keywords(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, DEFAULTS, "admin_a", {"ignored_title_keywords": ["secret-project"],
                                            "review_cadence": "manual"})
        audit = [a for a in w.audit if a["action"] == "review_defaults.changed"]
        assert {"ignored_title_keywords", "review_cadence"} <= set(audit[0]["detail"]["fields"])
        assert all("secret-project" not in str(a) for a in audit)


@pytest.mark.parametrize("who", ["viewer_a", "member_a", "editor_a"])
async def test_the_workspace_layer_keeps_its_gate(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, who, {"review_cadence": "manual"})
        assert r.status_code == 403
        assert (await _get(w, DEFAULTS))["review_cadence"] is None
