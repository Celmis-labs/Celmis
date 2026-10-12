"""A queued review sends the workspace's base reasoning level — or says it did not.

The shape that was measured in production: a workspace LiteLLM proxy, a bare
alias as the review model, a base reasoning level ("low") and no per-agent or
per-repo override at all. Every agent has to inherit the base level, and the
request that leaves for the proxy has to carry it. When Celmis cannot tell what
model the alias runs on (the proxy's /model/info is unreachable), the level
cannot be translated and is withheld — which is allowed, but never silently:
each agent's result must carry a parameter adjustment that says so.

Nothing here touches the network: litellm.completion and /model/info are
stubs; the workspace config and the proxy endpoint live in a real (temporary)
credential store, and the agents run through the orchestrator's own client
builder and thread pool.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.llm import litellm_proxy
from src.llm.capabilities import ADJUST_DROPPED, PARAM_REASONING, reset_capability_caches

BASE = "https://proxy.example.test"
KEY = "sk-virtual-key-for-tests-0000"
ALIAS = "3.5-flash-lite-myalias"
UNDERLYING = "gemini/gemini-3.5-flash-lite"
WORKSPACE = "ws-review"
AGENTS = ("defect", "contract", "security", "performance", "business_logic",
          "verifier", "compliance")


def _completion():
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = "[]"
    choice.finish_reason = "stop"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 20000
    resp.usage.completion_tokens = 1
    resp.usage.total_cost = None
    return resp


@pytest.fixture
def prod_like(monkeypatch, tmp_path):
    """Workspace config + proxy endpoint in a real store; returns the stubs."""
    from cryptography.fernet import Fernet

    from src.api.routers import llm as llm_router
    from src.credentials.store import CredentialStore

    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(litellm_proxy, "_resolve", lambda host, port: ["93.184.216.34"])
    reset_capability_caches()
    litellm_proxy.reset_cache()

    store = CredentialStore(tmp_path / "creds.db", Fernet.generate_key())
    sent: list[dict] = []
    model_info: dict[str, dict] = {ALIAS: {"underlying": UNDERLYING}}

    def _completion_stub(**kwargs):
        sent.append(kwargs)
        return _completion()

    with patch("src.credentials.get_credential_store", return_value=store), \
            patch("litellm.completion", side_effect=_completion_stub), \
            patch.object(litellm_proxy, "fetch_model_info",
                         side_effect=lambda ep: dict(model_info)), \
            patch("src.llm.budget.record_spend"):
        litellm_proxy.save_endpoint(
            WORKSPACE, litellm_proxy.Endpoint(base_url=BASE, api_key=KEY))
        llm_router._save_workspace_config({
            "provider": "litellm", "model": ALIAS, "review_reasoning": "low",
            "temperature": 0.1,
            "agents": {a: {"model": None, "reasoning": None, "temperature": None}
                       for a in AGENTS},
        }, "test", WORKSPACE)
        yield sent, model_info
    reset_capability_caches()
    litellm_proxy.reset_cache()


def _run_review_agents():
    """What a queued review does with the config: the orchestrator builds the
    client and the per-agent settings, then runs its LLM agents in its pool."""
    from src.review.agents.base import AgentContext, LLMReviewAgent
    from src.review.models import Hunk, PullRequest
    from src.review.orchestrator import ReviewOrchestrator

    def _agent(agent_name):
        class _Agent(LLMReviewAgent):
            name = agent_name
            system_prompt = "find problems"

            def _build_prompt(self, context):
                return "review this"
        return _Agent()

    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )
    orch = ReviewOrchestrator(agents=[_agent(n) for n in ("defect", "contract", "security")])
    client, agent_llm = orch._build_llm_client("u1", WORKSPACE, {})
    context = AgentContext(pull_request=pr, llm_client=client, agent_llm=agent_llm,
                           workspace_id=WORKSPACE, user_id="u1")
    return orch._run_agents_parallel(context)


def test_every_agent_inherits_the_base_level_and_sends_it(prod_like):
    sent, _ = prod_like

    results = _run_review_agents()

    assert {r.agent for r in results} == {"defect", "contract", "security"}
    assert len(sent) >= 3
    for kwargs in sent:
        assert kwargs["model"] == f"litellm_proxy/{ALIAS}"
        assert kwargs["api_base"] == BASE
        assert kwargs.get("reasoning_effort") == "low", kwargs
    for r in results:
        assert not [a for a in r.parameter_adjustments if a.parameter == PARAM_REASONING]


def test_a_level_that_cannot_be_translated_is_reported_on_every_agent(prod_like):
    """/model/info answers nothing: the alias is unknown, the level is withheld,
    and each agent's result says so instead of reading like a run that asked
    for no reasoning."""
    sent, model_info = prod_like
    model_info.clear()

    results = _run_review_agents()

    assert sent and all("reasoning_effort" not in kw for kw in sent)
    for r in results:
        drops = [a for a in r.parameter_adjustments
                 if a.parameter == PARAM_REASONING and a.action == ADJUST_DROPPED]
        assert len(drops) == 1, (r.agent, r.parameter_adjustments)
        assert drops[0].requested == "low" and drops[0].sent is None
        assert "/model/info" in drops[0].reason
