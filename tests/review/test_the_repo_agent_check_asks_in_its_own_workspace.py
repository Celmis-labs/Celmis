"""A per-repository agent override is judged in the workspace that owns it.

The capability lookup behind the save-time check resolves a LiteLLM proxy alias
through the workspace's own proxy connection. The repository layer called the
validator without the workspace, so it asked the "default" workspace, found no
proxy, and refused a reasoning level the model takes ("LiteLLM has no entry for
that model") — while the workspace layer, the Test button and the review itself
all accepted it.
"""

from __future__ import annotations

from src.api.routers import llm, review_policies


def test_the_repository_check_passes_the_workspace_on(monkeypatch):
    seen: list[str] = []

    def spy(agent, entry, model, workspace_id="default"):
        seen.append(workspace_id)

    monkeypatch.setattr(llm, "_validate_agent_entry", spy)
    monkeypatch.setattr(
        review_policies, "_effective_agents",
        lambda policy, workspace_id: {"defect": {"model": "litellm_proxy/x"}},
    )
    review_policies._validate_agent_llm_overrides(
        {"defect": {"reasoning": "medium"}}, {}, "ws-123",
    )
    assert seen == ["ws-123"]
