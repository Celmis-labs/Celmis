"""Team guidelines through the API — workspace (/api/agents) and repository (policies).

  * the workspace stores guidelines beside its replacement prompts, at most
    2000 characters (422 above, never a silent cut), and reports them with
    the short "what it already checks" hint;
  * a repository policy carries `agent_prompt_guidelines` and
    `agent_guidelines_extend`; absent keeps what is stored, too long is 422,
    unknown agents are dropped;
  * the previews — per repository and for the workspace — return the
    composed prompt as labelled parts, the guidelines among them;
  * the overrides summary names the repositories with guidelines of their own;
  * the workspace conversion of pre-2.3.1 prompts sorts once, is marked, and
    reverts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user, require_prompt_editor
from src.api.routers import agents as agents_router
from src.credentials.store import CredentialStore
from src.review.prompt_guidelines import GUIDELINES_MAX_CHARS, MIGRATION_REVISION
from tests.api.test_a_repo_customises_its_review import (
    REPO,
    _get,
    _preview,
    _put,
    policy_api,
)

WS = "ws-1"
_USER = SimpleNamespace(id="u-1", email="lead@test", is_admin=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real (sqlite, encrypted) credential store of the test's own."""
    s = CredentialStore(tmp_path / "credentials.db", Fernet.generate_key())
    import src.credentials as credentials

    monkeypatch.setattr(credentials, "get_credential_store", lambda: s)
    return s


@pytest.fixture
def agents_api(store):
    app = FastAPI()
    app.include_router(agents_router.router)
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[require_prompt_editor] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app)


# ─── workspace: /api/agents ──────────────────────────────────────────


def test_workspace_guidelines_round_trip(agents_api):
    r = agents_api.put("/api/agents/security/guidelines",
                       json={"guidelines": "  - flag raw SQL  "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["guidelines"] == "- flag raw SQL"
    assert body["has_guidelines"] is True and body["has_override"] is False
    assert body["guidelines_max"] == GUIDELINES_MAX_CHARS
    assert "OWASP" in body["guidelines_hint"]

    listed = {a["name"]: a for a in agents_api.get("/api/agents").json()}
    assert listed["security"]["guidelines"] == "- flag raw SQL"
    assert listed["defect"]["has_guidelines"] is False

    r = agents_api.delete("/api/agents/security/guidelines")
    assert r.status_code == 200 and r.json()["guidelines"] == ""


def test_workspace_guidelines_are_capped_at_2000(agents_api):
    r = agents_api.put("/api/agents/defect/guidelines",
                       json={"guidelines": "x" * (GUIDELINES_MAX_CHARS + 1)})
    assert r.status_code == 422
    ok = agents_api.put("/api/agents/defect/guidelines",
                        json={"guidelines": "x" * GUIDELINES_MAX_CHARS})
    assert ok.status_code == 200


def test_workspace_guidelines_refuse_unknown_agents_and_blank(agents_api):
    assert agents_api.put("/api/agents/nobody/guidelines",
                          json={"guidelines": "x"}).status_code == 404
    assert agents_api.put("/api/agents/defect/guidelines",
                          json={"guidelines": "   "}).status_code == 422


def test_guidelines_and_a_replacement_live_side_by_side(agents_api, store):
    agents_api.put("/api/agents/defect/prompt", json={"system_prompt": "MY FULL PROMPT"})
    agents_api.put("/api/agents/defect/guidelines", json={"guidelines": "- G"})
    body = agents_api.get("/api/agents/defect").json()
    assert body["system_prompt"] == "MY FULL PROMPT" and body["has_override"]
    assert body["guidelines"] == "- G"
    assert agents_router.get_workspace_guidelines("defect", WS) == "- G"
    assert agents_router.get_effective_system_prompt("defect", WS) == "MY FULL PROMPT"


# ─── workspace conversion of the old overrides ───────────────────────


KODUS_LIST = "- Logic errors: inverted conditions\n- Resource leaks on error paths"
REAL = "You are a reviewer. " + "Read every line. " * 200 + ' Output JSON with "reasoning".'


def _seed(store, ws: str, agent: str, text: str) -> None:
    from src.llm.keys import workspace_slot

    store.save(provider="__agent_prompt__", secret=text, metadata={},
               user_id=workspace_slot(ws), account_label=agent)


def test_the_workspace_conversion_sorts_once_and_reverts(store):
    _seed(store, WS, "defect", KODUS_LIST)
    _seed(store, WS, "security", REAL)
    _seed(store, "ws-2", "verifier", KODUS_LIST)

    done = agents_router.migrate_workspace_prompt_overrides()
    assert done == {"ws:ws-1": ["defect"], "ws:ws-2": ["verifier"]}
    assert agents_router._load_override("defect", WS) is None
    assert agents_router.get_workspace_guidelines("defect", WS) == KODUS_LIST
    assert agents_router._load_override("security", WS) == REAL
    assert agents_router.get_workspace_guidelines("security", WS) == ""

    # Marked: a short replacement chosen after the upgrade is never touched.
    _seed(store, WS, "contract", "- short but deliberate")
    assert agents_router.migrate_workspace_prompt_overrides() is None
    assert agents_router._load_override("contract", WS) == "- short but deliberate"

    # A guideline a person writes afterwards survives the revert.
    agents_router._save_guidelines("performance", "- mine", "lead@test", WS)
    restored = agents_router.revert_workspace_prompts_migration()
    assert restored == {"ws:ws-1": ["defect"], "ws:ws-2": ["verifier"]}
    assert agents_router._load_override("defect", WS) == KODUS_LIST
    assert agents_router.get_workspace_guidelines("defect", WS) == ""
    assert agents_router.get_workspace_guidelines("performance", WS) == "- mine"
    assert store.load(provider="__agent_prompt_migration__", user_id="__system__",
                      account_label=MIGRATION_REVISION) is None


def test_the_conversion_never_overwrites_existing_guidelines(store):
    _seed(store, WS, "defect", KODUS_LIST)
    agents_router._save_guidelines("defect", "- already here", "lead@test", WS)
    assert agents_router.migrate_workspace_prompt_overrides() == {}
    assert agents_router._load_override("defect", WS) == KODUS_LIST
    assert agents_router.get_workspace_guidelines("defect", WS) == "- already here"


def test_reading_prompts_never_converts_anything(store):
    """The read path (a review, a page load) only reads: nothing is sorted
    until the startup hook or the CLI runs."""
    _seed(store, WS, "defect", KODUS_LIST)
    assert agents_router.get_effective_system_prompt("defect", WS) == KODUS_LIST
    assert agents_router.get_workspace_guidelines("defect", WS) == ""
    assert agents_router._load_override("defect", WS) == KODUS_LIST


def test_the_cli_reverts(store, capsys):
    from src.review.prompt_guidelines_cli import main

    _seed(store, WS, "defect", KODUS_LIST)
    assert main(["migrate"]) == 0
    assert "ws:ws-1 agents=defect" in capsys.readouterr().out
    assert main(["revert"]) == 0
    assert agents_router._load_override("defect", WS) == KODUS_LIST


# ─── repository: /api/review-policies ────────────────────────────────


@pytest.fixture
def no_workspace_layers(monkeypatch):
    monkeypatch.setattr(agents_router, "_load_override", lambda *a, **k: None)
    monkeypatch.setattr(agents_router, "_load_guidelines", lambda *a, **k: None)


async def test_a_policy_stores_guidelines_and_extend(no_workspace_layers):
    async with policy_api() as client:
        r = await _put(client, agent_prompt_guidelines={
            "defect": "  - money is Decimal  ", "security": "", "nobody": "x"},
            agent_guidelines_extend=["defect", "nobody", "defect"])
        assert r.status_code == 200, r.text
        p = await _get(client)
        assert p["agent_prompt_guidelines"] == {"defect": "- money is Decimal"}
        assert p["agent_guidelines_extend"] == ["defect"]

        # Absent keeps what is stored.
        r = await _put(client)
        assert r.status_code == 200
        p = await _get(client)
        assert p["agent_prompt_guidelines"] == {"defect": "- money is Decimal"}
        assert p["agent_guidelines_extend"] == ["defect"]


async def test_a_policy_refuses_guidelines_over_the_cap(no_workspace_layers):
    async with policy_api() as client:
        r = await _put(client, agent_prompt_guidelines={
            "defect": "x" * (GUIDELINES_MAX_CHARS + 1)})
        assert r.status_code == 422
        assert "2000" in r.text


async def test_the_preview_shows_the_guidelines_as_an_added_part(no_workspace_layers):
    async with policy_api() as client:
        await _put(client, agent_prompt_guidelines={"defect": "- PREVIEW-GUIDELINE"})
        p = await _preview(client, "defect")
    kinds = [part["kind"] for part in p["parts"]]
    assert kinds[0] == "base" and p["parts"][0]["source"] == "builtin"
    assert kinds[1] == "guidelines"
    assert "- PREVIEW-GUIDELINE" in p["parts"][1]["text"]
    assert p["guidelines_source"] == "repository"
    assert p["system_prompt"] == "\n\n".join(part["text"] for part in p["parts"])


async def test_the_verifier_preview_carries_its_guidelines(no_workspace_layers):
    async with policy_api() as client:
        await _put(client, agent_prompt_guidelines={"verifier": "- keep perf findings"})
        p = await _preview(client, "verifier")
    assert "- keep perf findings" in p["system_prompt"]
    assert [part["kind"] for part in p["parts"]][:2] == ["base", "guidelines"]


async def test_the_workspace_preview_composes_without_a_repository(monkeypatch):
    monkeypatch.setattr(agents_router, "_load_override", lambda *a, **k: None)
    monkeypatch.setattr(agents_router, "_load_guidelines",
                        lambda agent, workspace_id="default": "- WS-G" if agent == "security" else None)
    async with policy_api() as client:
        r = await client.get("/api/review-policies/prompt-preview",
                             params={"agent": "security"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["guidelines_source"] == "workspace"
    assert "- WS-G" in p["system_prompt"]
    assert p["prompt_source"] == "builtin"


async def test_the_summary_names_repositories_with_their_own_guidelines(no_workspace_layers):
    async with policy_api(rows=[{"agent_prompt_guidelines": {"security": "- g"},
                                 "agent_guidelines_extend": []}]) as client:
        r = await client.get("/api/review-policies/overrides-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["repo_slug"] for e in body["guideline_overrides"]["security"]] == [REPO]
    assert body["prompt_overrides"]["security"] == []
