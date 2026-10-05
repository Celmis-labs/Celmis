"""/api/review-rules — the rules library, through the real routers, the real
role checks and the real workspace resolution (tests/api/rbac_world.py).

  * every member reads; editor, admin and owner write; viewers and members
    do not;
  * a repository's rules need the repository to be this workspace's and the
    caller's teams to grant `review` on it;
  * bulk approve / reject / delete act on this workspace's rules only;
  * the library adds once, active or pending, and says what is added;
  * generate and import run as jobs that end with pending proposals;
  * every write is audited;
  * the repo policy's `folder_rules` keep working beside the new rules.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, B_SECRET, world

URL = "/api/review-rules"


async def _create(w, who: str, **body):
    payload = {"title": "Do not ignore exceptions", "instructions": "Flag empty catch.",
               **body}
    return await w.client.post(URL, json=payload, headers=w.h(who, "ws-a"))


async def _list(w, who: str = "member_a", **params) -> dict:
    r = await w.client.get(URL, params=params, headers=w.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("who, status", [
    ("viewer_a", 403), ("member_a", 403), ("editor_a", 201), ("admin_a", 201),
    ("owner_a", 201), ("gadmin", 201),
])
async def test_editors_write_members_read(tmp_path, monkeypatch, who, status):
    async with world(tmp_path, monkeypatch) as w:
        r = await _create(w, who)
        assert r.status_code == status, r.text
        body = await _list(w, "viewer_a")
        assert body["counts"]["all"] == (1 if status == 201 else 0)
        assert body["can_edit"] is False
        assert B_SECRET not in json.dumps(body)
        if status == 201:
            rule = r.json()
            assert (rule["scope"], rule["status"], rule["origin"]) == (
                "workspace", "active", "manual")
            assert w.audit[-1]["action"] == "review_rules.created"
            assert w.audit[-1]["workspace_id"] == w.ws["ws-a"]


async def test_a_repo_rule_needs_the_repo_and_review_on_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        # The full name is accepted and stored under the registered slug.
        r = await _create(w, "editor_a", repo_slug=A_REPO_FULL, severity="error",
                          agents=["security"], path_glob="*.py")
        assert r.status_code == 201, r.text
        assert r.json()["repo_slug"] == A_REPO
        assert r.json()["agents"] == ["security"]
        # Not this workspace's repository: 404, nothing written.
        r = await _create(w, "admin_a", repo_slug="github_nobody-here", title="x")
        assert r.status_code == 404
        # admin2_a is in no team granted `review` on A_REPO, but an admin of the
        # workspace holds every repository of it (user decision) — allowed.
        r = await _create(w, "admin2_a", repo_slug=A_REPO, title="y")
        assert r.status_code == 201, r.text
        # An admin of ANOTHER workspace holds nothing here.
        r = await _create(w, "admin_b", repo_slug=A_REPO, title="z")
        assert r.status_code in (403, 404), r.text
        body = await _list(w, repo=A_REPO)
        assert sorted(x["title"] for x in body["rules"]) == ["Do not ignore exceptions", "y"]


@pytest.mark.parametrize("body, field", [
    ({"severity": "fatal"}, "severity"),
    ({"agents": ["nobody"]}, "agents"),
    ({"instructions": "x" * 2001}, "instructions"),
    ({"title": ""}, "title"),
])
async def test_a_bad_rule_is_refused(tmp_path, monkeypatch, body, field):
    async with world(tmp_path, monkeypatch) as w:
        r = await _create(w, "editor_a", **body)
        assert r.status_code == 422 and field in r.text, r.text
        assert (await _list(w))["counts"]["all"] == 0


async def test_one_title_per_scope(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        assert (await _create(w, "editor_a")).status_code == 201
        r = await _create(w, "editor_a", title="do not IGNORE exceptions")
        assert r.status_code == 409
        # The same title on a repository is an override, not a conflict.
        assert (await _create(w, "editor_a", repo_slug=A_REPO)).status_code == 201


async def test_bulk_approve_reject_delete(tmp_path, monkeypatch):
    from src.review import rules_store

    async with world(tmp_path, monkeypatch) as w:
        ids = await rules_store.propose_rules(
            w.ws["ws-a"], None,
            [{"title": f"Rule {i}", "instructions": "x"} for i in range(3)],
            "agent", "bot")
        body = await _list(w)
        assert body["counts"] == {"all": 3, "active": 0, "pending": 3, "rejected": 0}
        assert {r["origin"] for r in body["rules"]} == {"agent"}

        h = w.h("editor_a", "ws-a")
        r = await w.client.post(f"{URL}/bulk-status", headers=h,
                                json={"ids": ids[:2] + [901], "status": "active"})
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == sorted(ids[:2]), "B's rule 901 is not touched"
        r = await w.client.post(f"{URL}/bulk-status", headers=h,
                                json={"ids": [ids[2]], "status": "rejected"})
        assert r.json()["updated"] == [ids[2]]
        assert (await _list(w, status="pending"))["rules"] == []
        assert (await _list(w))["counts"] == {"all": 3, "active": 2, "pending": 0,
                                              "rejected": 1}

        r = await w.client.post(f"{URL}/bulk-status", headers=w.h("member_a", "ws-a"),
                                json={"ids": ids, "status": "active"})
        assert r.status_code == 403

        r = await w.client.post(f"{URL}/bulk-delete", headers=h, json={"ids": ids})
        assert sorted(r.json()["deleted"]) == sorted(ids)
        assert (await _list(w))["counts"]["all"] == 0
        assert [a["action"] for a in w.audit] == [
            "review_rules.status_changed", "review_rules.status_changed",
            "review_rules.deleted"]


async def test_edit_one_rule(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rule = (await _create(w, "editor_a")).json()
        h = w.h("editor_a", "ws-a")
        r = await w.client.patch(f"{URL}/{rule['id']}", headers=h,
                                 json={"severity": "critical", "path_glob": "src/**"})
        assert r.status_code == 200, r.text
        assert (r.json()["severity"], r.json()["path_glob"]) == ("critical", "src/**")
        r = await w.client.patch(f"{URL}/{rule['id']}", headers=h, json={"origin": "x"})
        assert r.status_code == 422, "origin is not editable"
        r = await w.client.delete(f"{URL}/{rule['id']}", headers=h)
        assert r.status_code == 204
        r = await w.client.delete(f"{URL}/{rule['id']}", headers=h)
        assert r.status_code == 404


async def test_the_library_adds_once(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        lib = (await w.client.get(f"{URL}/library", params={"q": "exceptions"},
                                  headers=w.h("viewer_a", "ws-a"))).json()
        ids = [e["id"] for e in lib["rules"]]
        assert "general.no-ignored-exceptions" in ids
        assert not any(e["added"] for e in lib["rules"])

        h = w.h("editor_a", "ws-a")
        body = {"ids": ["general.no-ignored-exceptions", "sql.uniqueness-constraints"],
                "status": "pending", "repo_slug": A_REPO}
        r = await w.client.post(f"{URL}/library/add", headers=h, json=body)
        assert r.status_code == 201, r.text
        assert len(r.json()["created"]) == 2
        again = await w.client.post(f"{URL}/library/add", headers=h, json=body)
        assert again.json() == {"created": [], "skipped": 2}

        rules = (await _list(w, repo=A_REPO))["rules"]
        assert {(r["origin"], r["status"]) for r in rules} == {("library", "pending")}
        assert {r["source_ref"] for r in rules} == {
            "library:general.no-ignored-exceptions", "library:sql.uniqueness-constraints"}
        lib = (await w.client.get(f"{URL}/library", params={"repo": A_REPO},
                                  headers=h)).json()
        assert sum(e["added"] for e in lib["rules"]) == 2

        r = await w.client.post(f"{URL}/library/add", headers=h, json={"ids": ["nope"]})
        assert r.status_code == 422


class _Model:
    def generate(self, **kw):
        return SimpleNamespace(text=json.dumps({"rules": [
            {"title": "Generated rule", "instructions": "Flag it.", "severity": "error",
             "rationale": "seen in app.py"}]}))


async def test_generate_and_import_run_as_jobs(tmp_path, monkeypatch):
    from src.review import rules_generate

    repo = tmp_path / "clone"
    repo.mkdir()
    (repo / "app.py").write_text("def f(x=[]):\n    return x\n" * 30)
    (repo / "CONTRIBUTING.md").write_text(
        "## Style\nAlways use type hints on public functions; never use `Any`.\n")
    monkeypatch.setattr(rules_generate, "repo_root", lambda slug: repo)
    monkeypatch.setattr(rules_generate, "_vault_excerpt", lambda slug, budget=0: ("", 0))
    monkeypatch.setattr(rules_generate, "_llm_client", lambda ws, actor: _Model())

    async with world(tmp_path, monkeypatch) as w:
        h = w.h("editor_a", "ws-a")
        r = await w.client.post(f"{URL}/generate", headers=h, json={"repo_slug": A_REPO})
        assert r.status_code == 202, r.text
        job = (await w.client.get(f"{URL}/jobs/{r.json()['id']}", headers=h)).json()
        assert job["status"] == "completed", job
        assert len(job["result"]["created"]) == 1

        r = await w.client.post(f"{URL}/import", headers=h, json={"repo_slug": A_REPO})
        job = (await w.client.get(f"{URL}/jobs/{r.json()['id']}", headers=h)).json()
        assert job["status"] == "completed" and job["result"]["files"] == ["CONTRIBUTING.md"]

        body = await _list(w, repo=A_REPO, status="pending")
        assert {r["origin"] for r in body["rules"]} == {"generated", "imported"}
        assert body["counts"]["pending"] == 2
        listed = (await w.client.get(f"{URL}/jobs", headers=h)).json()
        assert [j["kind"] for j in listed] == ["import", "generate"]
        assert {"review_rules.generate_started", "review_rules.import_started"} <= {
            a["action"] for a in w.audit}

        r = await w.client.post(f"{URL}/generate", headers=w.h("member_a", "ws-a"),
                                json={"repo_slug": A_REPO})
        assert r.status_code == 403


async def test_folder_rules_keep_working_beside_review_rules(tmp_path, monkeypatch):
    """The policy page's rules: saved and read back through the policy API,
    shown read-only on the rules page, and rendered next to the new rules in
    the prompt a reviewer agent receives."""
    async with world(tmp_path, monkeypatch) as w:
        h = w.h("editor_a", "ws-a")
        policy_url = f"/api/review-policies/{A_REPO}"
        r = await w.client.put(policy_url, headers=h, json={"folder_rules": [
            {"pattern": "src/*", "prompt": "Legacy rule text"}]})
        assert r.status_code == 200, r.text
        assert (await _create(w, "editor_a", repo_slug=A_REPO)).status_code == 201

        p = (await w.client.get(policy_url, headers=h)).json()
        assert [(f["pattern"], f["prompt"]) for f in p["folder_rules"]] == [
            ("src/*", "Legacy rule text")]
        body = await _list(w, repo=A_REPO)
        assert body["legacy_folder_rules"][0]["prompt"] == "Legacy rule text"

        preview = (await w.client.get(f"{policy_url}/prompt-preview?agent=defect",
                                      headers=h)).json()["system_prompt"]
        assert "Legacy rule text" in preview
        assert "Review rule — Do not ignore exceptions" in preview
