"""/api/review-defaults — the workspace layer under every repo policy.

Through the real routers, the real role checks and the real workspace
resolution (tests/api/rbac_world.py):

  * members read, only owner / admin (or a global admin) write — an editor
    may edit prompts and repo policies, not what every repository spends;
  * a write lands in the ACTIVE workspace and nowhere else;
  * a repository that says nothing inherits the workspace default, one that
    decided keeps its decision, and the policy GET names the source of each;
  * `review_language` and the per-agent LLM block are written to the
    workspace LLM config, their one workspace home, not to a second copy.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, B_SECRET, world

URL = "/api/review-defaults"


async def _get(w, who: str, ws: str = "ws-a") -> dict:
    r = await w.client.get(URL, headers=w.h(who, ws))
    assert r.status_code == 200, r.text
    return r.json()


async def _put(w, who: str, body: dict, ws: str = "ws-a"):
    return await w.client.put(URL, json=body, headers=w.h(who, ws))


@pytest.mark.parametrize("who", ["viewer_a", "member_a", "editor_a", "admin_a", "owner_a"])
async def test_every_member_reads(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        body = await _get(w, who)
        assert body["workspace_id"] == w.ws["ws-a"]
        assert body["can_edit"] is (who in ("admin_a", "owner_a"))
        assert body["disabled_agents"] is None
        assert body["effective"]["disabled_agents"] == []
        assert set(body["sources"].values()) == {"install"}
        assert "verifier" in body["toggleable_agents"]
        assert B_SECRET not in str(body)


@pytest.mark.parametrize("who, status", [
    ("viewer_a", 403), ("member_a", 403), ("editor_a", 403),
    ("admin_a", 200), ("owner_a", 200), ("gadmin", 200),
])
async def test_only_owner_and_admin_write(tmp_path, monkeypatch, who, status):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, who, {"max_inline_comments": 7})
        assert r.status_code == status, r.text
        stored = (await _get(w, "member_a"))["max_inline_comments"]
        assert stored == (7 if status == 200 else None)


async def test_a_write_stays_in_the_active_workspace(tmp_path, monkeypatch):
    from src.db.models import WorkspaceReviewDefaults

    async with world(tmp_path, monkeypatch) as w:
        # admin_a asks for B: pinned back to A, and B's row is untouched.
        r = await _put(w, "admin_a", {"summary_instructions": "A only"}, ws="ws-b")
        assert r.status_code == 200, r.text
        assert r.json()["workspace_id"] == w.ws["ws-a"]
        b = await w.scalar(WorkspaceReviewDefaults, w.ws["ws-b"])
        assert b.summary_instructions == f"{B_SECRET} summary instructions"
        a = await w.scalar(WorkspaceReviewDefaults, w.ws["ws-a"])
        assert a.summary_instructions == "A only"
        # And B's admin reads B's, never A's.
        body = await _get(w, "admin_b", "ws-b")
        assert body["summary_instructions"] == f"{B_SECRET} summary instructions"
        assert body["disabled_agents"] == ["structural"]


async def test_null_goes_back_to_the_install_default(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, "admin_a", {"disabled_agents": ["security"],
                                      "comment_min_severity": "Error",
                                      "target_branches": [" main ", "main", ""]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["disabled_agents"] == ["security"]
        assert body["comment_min_severity"] == "error"
        assert body["target_branches"] == ["main"]
        assert body["sources"]["disabled_agents"] == "workspace"

        # Absent keeps; null clears back to the install default.
        r = await _put(w, "admin_a", {"comment_min_severity": None})
        body = r.json()
        assert body["disabled_agents"] == ["security"]
        assert body["comment_min_severity"] is None
        assert body["effective"]["comment_min_severity"] == "info"
        assert body["sources"]["comment_min_severity"] == "install"


async def test_the_old_spelling_of_verifier_off_is_translated(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        body = (await _put(w, "admin_a", {"disabled_agents": ["verifier", "cve"]})).json()
        assert body["disabled_agents"] == ["cve"]
        assert body["verifier_enabled"] is False


@pytest.mark.parametrize("body, field", [
    ({"disabled_agents": ["bogus"]}, "disabled_agents"),
    ({"comment_min_severity": "nits"}, "comment_min_severity"),
    ({"ignore_globs": ["!keep.py"]}, "ignore_globs"),
    ({"review_language": "klingon"}, "review_language"),
    ({"suppressed_rules": ["has space"]}, "suppressed_rules"),
    ({"agents": {"nobody": {"max_output_tokens": 4096}}}, "nobody"),
])
async def test_bad_values_are_refused_and_nothing_is_saved(tmp_path, monkeypatch, body, field):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, "admin_a", {**body, "max_inline_comments": 9})
        assert r.status_code == 422 and field in r.text, r.text
        assert (await _get(w, "admin_a"))["max_inline_comments"] is None


async def test_language_and_agents_live_in_the_llm_config(tmp_path, monkeypatch):
    from src.api.routers.llm import _load_workspace_config

    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, "admin_a", {"review_language": "UK",
                                      "agents": {"defect": {"max_output_tokens": 4096}}})
        assert r.status_code == 200, r.text
        cfg = _load_workspace_config(w.ws["ws-a"])
        assert cfg["review_language"] == "uk"
        assert cfg["agents"] == {"defect": {"max_output_tokens": 4096}}
        body = r.json()
        assert body["review_language"] == "uk"
        assert body["agents"]["defect"]["max_output_tokens"] == 4096
        assert body["sources"]["review_language"] == "workspace"
        assert "review_language" not in _load_workspace_config(w.ws["ws-b"])

        # The repo policy page inherits it, and says from where.
        p = (await w.client.get(f"/api/review-policies/{A_REPO}",
                                headers=w.h("member_a", "ws-a"))).json()
        assert p["review_language_effective"] == "uk"
        assert p["sources"]["review_language"] == "workspace"

        # null hands the language back to the install default.
        await _put(w, "admin_a", {"review_language": None})
        assert "review_language" not in _load_workspace_config(w.ws["ws-a"])


async def test_a_repo_inherits_until_it_decides(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, "admin_a", {"disabled_agents": ["structural"],
                                  "summary_enabled": False,
                                  "max_inline_comments": 4})
        policy_url = f"/api/review-policies/{A_REPO}"

        p = (await w.client.get(policy_url, headers=w.h("member_a", "ws-a"))).json()
        assert p["disabled_agents"] is None
        assert p["disabled_agents_effective"] == ["structural"]
        assert p["summary_enabled"] is None and p["summary_enabled_effective"] is False
        assert p["max_inline_comments_effective"] == 4
        assert p["sources"]["disabled_agents"] == "workspace"
        assert p["inherited"]["disabled_agents"] == ["structural"]
        assert p["inherited_sources"]["max_inline_comments"] == "workspace"

        # The repo decides: [] is "every agent runs", above the workspace.
        r = await w.client.put(policy_url, headers=w.h("editor_a", "ws-a"),
                               json={"disabled_agents": [], "summary_enabled": True})
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["disabled_agents"] == [] and p["disabled_agents_effective"] == []
        assert p["sources"]["disabled_agents"] == "repo"
        assert p["summary_enabled_effective"] is True
        assert p["max_inline_comments_effective"] == 4, "untouched fields still inherit"

        # A later workspace change does not reach the decided field…
        await _put(w, "admin_a", {"disabled_agents": ["structural", "cve"]})
        p = (await w.client.get(policy_url, headers=w.h("member_a", "ws-a"))).json()
        assert p["disabled_agents_effective"] == []

        # …until the repo resets it to inherited.
        r = await w.client.put(policy_url, headers=w.h("editor_a", "ws-a"),
                               json={"disabled_agents": None})
        assert r.json()["disabled_agents_effective"] == ["structural", "cve"]

        # The workspace page counts the repositories overriding a field.
        body = await _get(w, "admin_a")
        assert body["repo_overrides"]["summary_enabled"] == 1
        assert body["repo_overrides"]["disabled_agents"] == 0


async def test_a_stored_repo_value_outranks_a_new_workspace_default(tmp_path, monkeypatch):
    """An existing policy row with an explicit answer keeps it."""
    from src.db.models import RepoReviewPolicy

    async with world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            s.add(RepoReviewPolicy(
                repo_slug=A_REPO, workspace_id=w.ws["ws-a"], prompt_template="",
                folder_rules=[], agent_prompt_overrides={}, mcp_sources=[],
                disabled_agents=["security"], target_branches=["develop"],
                summary_enabled=False))
            await s.commit()
        await _put(w, "admin_a", {"disabled_agents": ["structural"],
                                  "target_branches": ["main"], "summary_enabled": True})
        p = (await w.client.get(f"/api/review-policies/{A_REPO}",
                                headers=w.h("member_a", "ws-a"))).json()
        assert p["disabled_agents_effective"] == ["security"]
        assert p["target_branches_effective"] == ["develop"]
        assert p["summary_enabled_effective"] is False
        assert {p["sources"][f] for f in
                ("disabled_agents", "target_branches", "summary_enabled")} == {"repo"}
        listed = (await w.client.get("/api/review-policies",
                                     headers=w.h("member_a", "ws-a"))).json()
        assert listed[0]["disabled_agents"] == ["security"]
