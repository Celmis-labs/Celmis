"""The 2.3.0 review settings through the real routers, roles and tenancy.

tests/api/rbac_world.py: two workspaces, a cast of roles, B's data marked.

  * members read both layers; only owner / admin write the workspace layer,
    and a repository's layer keeps the gate it had (editor+ with a `review`
    grant) — a member or viewer cannot write either;
  * every new key round-trips at both layers with *_effective, sources and
    inherited; absent keeps, null inherits;
  * both layers refuse the same bad values (enums, lengths, placeholders,
    agent names — the new agents accepted, nonsense refused) and save
    nothing on a 422;
  * writes are audited by field name, never by value;
  * GET /api/review-settings/overview: the active workspace's defaults and
    readable repos, "Overridden N", the last review — and nothing of B's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, B_REPO, B_SECRET, world

DEFAULTS = "/api/review-defaults"
POLICY = f"/api/review-policies/{A_REPO}"
OVERVIEW = "/api/review-settings/overview"

#: One non-built-in value per new key.
VALUES = {
    "enabled_agents": ["business_logic"],
    "run_on_drafts": True,
    "approve_when_clean": True,
    "request_changes_on_critical": True,
    "status_feedback": False,
    "committable_suggestions": True,
    "apply_filters_to_rules": False,
    "summary_target": "description",
    "summary_on_new_commits": "append",
    "summary_existing_description": "complement",
    "base_instruction": "Write like a senior reviewer: terse, kind, specific.",
    "message_started": "Reviewing {commit} with {agents} ({files} files) on #{pr_number}",
    "message_finished_header": "Review of {commit}",
}
BUILTINS = {
    "enabled_agents": [], "run_on_drafts": False, "approve_when_clean": False,
    "request_changes_on_critical": False, "status_feedback": True,
    "committable_suggestions": False, "apply_filters_to_rules": True,
    "summary_target": "comment", "summary_on_new_commits": "replace",
    "summary_existing_description": "append", "base_instruction": None,
    "message_started": None, "message_finished_header": None,
}

#: (body, what the 422 must name) — refused at BOTH layers.
BAD = [
    ({"summary_target": "slack"}, "summary_target"),
    ({"summary_on_new_commits": "overwrite"}, "summary_on_new_commits"),
    ({"summary_existing_description": "prepend"}, "summary_existing_description"),
    ({"base_instruction": "x" * 2001}, "base_instruction"),
    ({"message_started": "y" * 2001}, "message_started"),
    ({"message_started": "Hello {author}"}, "author"),
    ({"message_finished_header": "Unbalanced { brace"}, "message_finished_header"),
    ({"enabled_agents": ["bogus"]}, "enabled_agents"),
    ({"enabled_agents": ["verifier"]}, "verifier_enabled"),
    ({"run_on_drafts": "maybe"}, "run_on_drafts"),
]


async def _get(w, url: str, who: str, ws: str = "ws-a") -> dict:
    r = await w.client.get(url, headers=w.h(who, ws))
    assert r.status_code == 200, r.text
    return r.json()


async def _put(w, url: str, who: str, body: dict, ws: str = "ws-a"):
    return await w.client.put(url, json=body, headers=w.h(who, ws))


# ─── reading ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["viewer_a", "member_a", "editor_a", "admin_a", "owner_a"])
async def test_every_member_reads_both_layers_with_the_builtins(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        d = await _get(w, DEFAULTS, who)
        p = await _get(w, POLICY, who)
        for name, builtin in BUILTINS.items():
            assert d[name] is None, name
            assert d["effective"][name] == builtin, name
            assert d["install"][name] == builtin, name
            assert d["sources"][name] == "install", name
            assert p[name] is None, name
            assert p[f"{name}_effective"] == builtin, name
            assert p["sources"][name] == "install", name
            assert p["inherited"][name] == builtin, name
        for body in (d, p):
            assert body["agent_participation_defaults"]["business_logic"] is False
            assert body["agent_participation_defaults"]["performance"] is True
            assert body["agent_participation_effective"]["business_logic"] is False
            assert body["setting_choices"]["summary_target"] == ["comment", "description"]
            assert body["message_placeholders"] == ["commit", "agents", "files",
                                                    "pr_number"]
        assert {"performance", "business_logic"} <= set(d["toggleable_agents"])
        assert {"performance", "business_logic"} <= set(p["overridable_agents"])


# ─── writing the workspace layer ─────────────────────────────────────


@pytest.mark.parametrize("who, status", [
    ("viewer_a", 403), ("member_a", 403), ("editor_a", 403),
    ("admin_a", 200), ("owner_a", 200),
])
async def test_only_owner_and_admin_write_the_workspace_layer(tmp_path, monkeypatch,
                                                              who, status):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, who, {"run_on_drafts": True})
        assert r.status_code == status, r.text
        assert (await _get(w, DEFAULTS, "member_a"))["run_on_drafts"] == (
            True if status == 200 else None)


async def test_every_key_round_trips_on_the_workspace_and_reaches_the_repo(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, "admin_a", VALUES)
        assert r.status_code == 200, r.text
        d = r.json()
        for name, value in VALUES.items():
            assert d[name] == value, name
            assert d["effective"][name] == value, name
            assert d["sources"][name] == "workspace", name
        assert d["agent_participation_effective"]["business_logic"] is True

        # The repository inherits every one, and says from where.
        p = await _get(w, POLICY, "member_a")
        for name, value in VALUES.items():
            assert p[name] is None, name
            assert p[f"{name}_effective"] == value, name
            assert p["sources"][name] == "workspace", name
            assert p["inherited_sources"][name] == "workspace", name

        # Absent keeps; null goes back to the built-in; blank text is null.
        d = (await _put(w, DEFAULTS, "admin_a",
                        {"summary_target": None, "base_instruction": "   "})).json()
        assert d["summary_target"] is None
        assert d["effective"]["summary_target"] == "comment"
        assert d["base_instruction"] is None
        assert d["run_on_drafts"] is True, "absent must keep what is stored"

        audit = [a for a in w.audit if a["action"] == "review_defaults.changed"]
        assert set(VALUES) <= set(audit[0]["detail"]["fields"])
        assert all(VALUES["base_instruction"] not in str(a) for a in audit), (
            "the audit trail carries field names, never the text")


async def test_enums_are_forgiving_about_case_and_space(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        d = (await _put(w, DEFAULTS, "admin_a",
                        {"summary_target": " Description ",
                         "enabled_agents": ["Business_Logic", "business_logic"]})).json()
        assert d["summary_target"] == "description"
        assert d["enabled_agents"] == ["business_logic"]


@pytest.mark.parametrize("body, named", BAD)
async def test_the_workspace_layer_refuses_bad_values_and_saves_nothing(
        tmp_path, monkeypatch, body, named):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, "admin_a", {**body, "approve_when_clean": True})
        assert r.status_code == 422 and named in r.text, r.text
        assert (await _get(w, DEFAULTS, "admin_a"))["approve_when_clean"] is None


async def test_the_new_agents_are_valid_names_everywhere_at_the_workspace(
        tmp_path, monkeypatch):
    from src.api.routers.llm import _load_workspace_config

    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, "admin_a", {
            "disabled_agents": ["performance"],
            "enabled_agents": ["business_logic"],
            "agents": {"performance": {"max_output_tokens": 4096},
                       "business_logic": {"max_output_tokens": 2048}},
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["disabled_agents"] == ["performance"]
        assert d["agent_participation_effective"]["performance"] is False
        assert d["agent_participation_effective"]["business_logic"] is True
        cfg = _load_workspace_config(w.ws["ws-a"])
        assert cfg["agents"]["performance"] == {"max_output_tokens": 4096}
        assert cfg["agents"]["business_logic"] == {"max_output_tokens": 2048}


# ─── writing the repository layer ────────────────────────────────────


@pytest.mark.parametrize("who, status", [
    ("viewer_a", 403), ("member_a", 403),
    ("editor_a", 200), ("admin_a", 200), ("owner_a", 200),
])
async def test_the_repo_layer_keeps_its_gate(tmp_path, monkeypatch, who, status):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, POLICY, who, {"approve_when_clean": True})
        assert r.status_code == status, r.text
        p = await _get(w, POLICY, "member_a")
        assert p["approve_when_clean"] == (True if status == 200 else None)


async def test_every_key_round_trips_on_the_repo_over_the_workspace(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _put(w, DEFAULTS, "admin_a", VALUES)
        own = {**BUILTINS, "enabled_agents": [], "base_instruction": "Repo voice.",
               "message_started": "Started {commit}", "message_finished_header": "Done"}
        r = await _put(w, POLICY, "editor_a", own)
        assert r.status_code == 200, r.text
        p = r.json()
        for name, value in own.items():
            assert p[name] == value, name
            assert p[f"{name}_effective"] == value, name
            assert p["sources"][name] == "repo", name
            assert p["inherited"][name] == VALUES[name], name
        assert p["agent_participation_effective"]["business_logic"] is False

        # Absent keeps — the policy page predates these and must not wipe them.
        p = (await _put(w, POLICY, "editor_a", {"prompt_template": "rules"})).json()
        assert p["run_on_drafts"] is False and p["base_instruction"] == "Repo voice."

        # null inherits the workspace again.
        p = (await _put(w, POLICY, "editor_a", {"run_on_drafts": None})).json()
        assert p["run_on_drafts"] is None and p["run_on_drafts_effective"] is True

        audit = [a for a in w.audit if a["action"] == "review_policy.changed"]
        assert audit and audit[0]["target"] == A_REPO
        assert set(own) <= set(audit[0]["detail"]["fields"])
        assert all("Repo voice." not in str(a) for a in audit)


@pytest.mark.parametrize("body, named", BAD)
async def test_the_repo_layer_refuses_the_same_values(tmp_path, monkeypatch, body, named):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, POLICY, "editor_a", {**body, "approve_when_clean": True})
        assert r.status_code == 422 and named in r.text, r.text
        assert (await _get(w, POLICY, "editor_a"))["approve_when_clean"] is None


async def test_the_new_agents_are_valid_names_everywhere_on_a_repo(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, POLICY, "editor_a", {
            "disabled_agents": ["performance", "business_logic"],
            "enabled_agents": ["business_logic"],
            "agent_prompt_overrides": {"performance": "Hot loops only.",
                                       "business_logic": "Check invariants."},
            "agent_llm_overrides": {"performance": {"max_output_tokens": 4096}},
            "performance_model": "gemini/gemini-2.5-flash",
        })
        assert r.status_code == 200, r.text
        p = r.json()
        assert p["disabled_agents"] == ["performance", "business_logic"]
        assert p["enabled_agents"] == ["business_logic"]
        assert p["agent_participation_effective"]["business_logic"] is False, (
            "disabled wins over enabled")
        assert p["agent_prompt_overrides"] == {"performance": "Hot loops only.",
                                               "business_logic": "Check invariants."}
        assert p["agent_llm_overrides"] == {"performance": {"max_output_tokens": 4096}}
        assert p["performance_model"] == "gemini/gemini-2.5-flash"

        # The model column: absent keeps, null clears.
        p = (await _put(w, POLICY, "editor_a", {"prompt_template": ""})).json()
        assert p["performance_model"] == "gemini/gemini-2.5-flash"
        p = (await _put(w, POLICY, "editor_a", {"performance_model": None})).json()
        assert p["performance_model"] is None


async def test_a_write_stays_in_the_active_workspace(tmp_path, monkeypatch):
    from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults

    async with world(tmp_path, monkeypatch) as w:
        r = await _put(w, DEFAULTS, "admin_a", {"base_instruction": "A only"}, ws="ws-b")
        assert r.status_code == 200 and r.json()["workspace_id"] == w.ws["ws-a"]
        assert (await w.scalar(WorkspaceReviewDefaults, w.ws["ws-b"])).base_instruction is None
        r = await _put(w, f"/api/review-policies/{B_REPO}", "admin_a",
                       {"run_on_drafts": True}, ws="ws-b")
        assert r.status_code in (403, 404), r.text
        assert (await w.scalar(RepoReviewPolicy, B_REPO)).run_on_drafts is None
        body = await _get(w, DEFAULTS, "admin_b", "ws-b")
        assert body["base_instruction"] is None


# ─── the overview ────────────────────────────────────────────────────


async def test_the_overview_counts_overrides_and_shows_the_last_review(tmp_path,
                                                                       monkeypatch):
    from src.db.models import ReviewPullRequest

    async with world(tmp_path, monkeypatch) as w:
        now = datetime.now(UTC)
        async with w.factory() as s:
            for i, (status, at) in enumerate((("complete", now - timedelta(hours=2)),
                                              ("failed", now))):
                s.add(ReviewPullRequest(
                    id=f"pr-a{i}", workspace_id=w.ws["ws-a"], provider="github",
                    repo=A_REPO_FULL, repo_slug=A_REPO, number=i + 1, title="t",
                    state="open", reviews_count=1, last_review_status=status,
                    opened_at=at, updated_at=at))
            await s.commit()

        # A member reads the workspace layer; A's repository is granted to
        # team-a only, so it is not theirs to see — scoped like the policy
        # routes, not listed.
        o = await _get(w, OVERVIEW, "member_a")
        assert o["workspace"]["workspace_id"] == w.ws["ws-a"]
        assert o["workspace"]["set_count"] == 0 and o["workspace"]["can_edit"] is False
        assert o["repositories"] == []

        o = await _get(w, OVERVIEW, "editor_a")
        assert o["workspace"]["can_edit"] is False
        [repo] = o["repositories"]
        assert repo["repo_slug"] == A_REPO and repo["provider"] == "github"
        assert repo["has_policy"] is False and repo["overridden_count"] == 0
        assert repo["last_review_status"] == "failed"
        assert repo["last_review_at"] is not None

        await _put(w, DEFAULTS, "admin_a", {"run_on_drafts": True, "summary_target":
                                            "description"})
        await _put(w, POLICY, "editor_a", {"approve_when_clean": True,
                                           "base_instruction": "Mine.",
                                           "disabled_agents": ["cve"]})
        o = await _get(w, OVERVIEW, "admin_a")
        assert o["workspace"]["set_count"] == 2
        assert set(o["workspace"]["set_fields"]) == {"run_on_drafts", "summary_target"}
        assert o["workspace"]["can_edit"] is True
        [repo] = o["repositories"]
        assert repo["has_policy"] is True
        assert repo["overridden_count"] == 3
        assert set(repo["overridden_fields"]) == {"approve_when_clean", "base_instruction",
                                                 "disabled_agents"}
        assert B_SECRET not in str(o)


async def test_the_overview_is_the_active_workspaces_only(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        # Asking for B: pinned back to A — B's repo, defaults and PRs absent.
        for who, repos in (("member_a", []), ("editor_a", [A_REPO]),
                           ("admin_a", [A_REPO])):
            o = await _get(w, OVERVIEW, who, "ws-b")
            assert o["workspace"]["workspace_id"] == w.ws["ws-a"]
            assert [r["repo_slug"] for r in o["repositories"]] == repos
            assert B_SECRET not in str(o)
        # B's own member (in the team B's repository is granted to) sees B:
        # its defaults set three fields.
        o = await _get(w, OVERVIEW, "member_b", "ws-b")
        assert o["workspace"]["workspace_id"] == w.ws["ws-b"]
        assert o["workspace"]["set_count"] == 3
        assert [r["repo_slug"] for r in o["repositories"]] == [B_REPO]
