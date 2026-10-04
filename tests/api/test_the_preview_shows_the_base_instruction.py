"""The prompt preview shows the base instruction where a review would put it.

"Preview" on the policy page answers "what will THIS agent be told". Once a
team's base instruction rides in every agent's prompt, a preview without it
would show a prompt no review sends. So the endpoint resolves it the way a
review does — the repository's own value, else the workspace default — and
composes it in, for every finder and for the verifier.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.routers import review_policies as policies_router
from tests.api.test_a_repo_customises_its_review import _preview, policy_api

MARK = "BASE-MARK: one imperative sentence per suggestion, no hedging."


@pytest.fixture
def workspace_base_instruction(monkeypatch):
    async def _defaults(_session, _ws):
        return {"base_instruction": MARK}

    monkeypatch.setattr(policies_router, "_load_workspace_defaults", _defaults)


@pytest.mark.parametrize("agent", [*policies_router._llm_agent_names(), "verifier"])
async def test_every_previewable_agent_shows_it(agent, workspace_base_instruction):
    async with policy_api() as client:
        p = await _preview(client, agent)
    assert MARK in p["system_prompt"], agent


async def test_no_instruction_no_block():
    async with policy_api() as client:
        p = await _preview(client, "performance")
    assert "Base instruction" not in p["system_prompt"]


def test_the_repository_value_wins_and_is_clamped():
    row = SimpleNamespace(base_instruction="repo says " + "r" * 3000)
    got = policies_router._preview_base_instruction(row, {"base_instruction": MARK})
    assert got.startswith("repo says") and len(got) == 2000

    unset = SimpleNamespace(base_instruction=None)
    assert policies_router._preview_base_instruction(
        unset, {"base_instruction": MARK}) == MARK
    # A row from before the column existed has no attribute at all.
    assert policies_router._preview_base_instruction(
        SimpleNamespace(), {"base_instruction": MARK}) == MARK
    assert policies_router._preview_base_instruction(None, None) == ""
