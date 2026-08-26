"""Who writes the documentation, and what that engine is allowed to do.

Documentation generation was the one large surface with no engine choice and no
profile: chat, review and embeddings each resolve one, review picks between
`api` and `claude_code`, the dependency report picks between them too, and
generation called `get_gemini_client()` three files deep. So a workspace could
not use its own provider for the artefact it most wants to keep, and those
calls left through the google-genai SDK rather than the LiteLLM gateway every
other surface uses.

The agent engine matters more here than it does for review, and for a reason
worth stating plainly: a PRD is interpretation, so there is no `evidence_kind`
that can separate a proven claim from a plausible one the way there is for a
lock-file drift. Documentation is the surface where a model is freest to
invent. The defence is not a sterner prompt — it is denying the model any way
to answer except by looking something up.

Which is why most of this file is about what the agent CANNOT do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.generation.doc_language import resolve_doc_engine
from src.generation.engines import (
    DEFAULT_ENGINE,
    ENGINE_API,
    ENGINE_CLAUDE_CODE,
    ENGINES,
    ApiEngine,
    build_engine,
    compose_prompt,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CLAUDE_DOCS = (SRC / "generation" / "claude_docs.py").read_text(encoding="utf-8")


# ─── the choice ──────────────────────────────────────────────────────


def test_the_two_engines_are_named_the_same_as_everywhere_else():
    """`review_engine` already uses these two words. A third vocabulary for the
    same idea makes "claude_code" mean something page-dependent."""
    assert ENGINES == (ENGINE_API, ENGINE_CLAUDE_CODE)
    review = (SRC / "api" / "routers" / "llm.py").read_text(encoding="utf-8")
    assert '"^(api|claude_code)$"' in review or "api|claude_code" in review


def test_the_default_engine_goes_through_the_gateway():
    """This shipped once with the default still on the legacy direct-Gemini
    path — an engine choice nobody had chosen, so nothing actually changed.

    Generation now dispatches through an engine unconditionally, and the api
    engine goes through build_llm_client. That is the whole point: chat and
    review leave as litellm_proxy/celmis-{ws}-{surface} under the tenant's
    virtual key, and generation used to appear in none of the routing, keys or
    spend that implies.
    """
    assert DEFAULT_ENGINE == ENGINE_API
    orch = (SRC / "generation" / "orchestrator.py").read_text(encoding="utf-8")
    assert "gen.engine = selected" in orch, "the legacy bypass is back"
    assert "gen.engine = None" not in orch


def test_the_api_engine_uses_the_factory_not_a_bare_client():
    """A bare LLMClient has no key resolver bound and no gateway routing: on a
    workspace behind the gateway it asks for "gemini-3-pro", the resolver goes
    looking for a raw google credential the workspace does not hold, and the
    call dies with LLMCredentialError before sending a token. That is a
    documented past outage in src/llm/client.py."""
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert "build_llm_client(" in engines
    assert "LLMClient(workspace_id" not in engines


def test_generation_stays_on_the_chat_profile():
    """It always has — get_gemini_client() resolves "chat". Moving these
    documents onto the review model would change the output of a feature
    nobody asked to change."""
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert 'surface="chat"' in engines
    assert 'resolve_profile("chat"' in engines


def test_the_person_who_queued_the_build_reaches_the_engine():
    """A Claude subscription token belongs to a person, not a workspace. Without
    the user the agent engine could only ever find a workspace-level Anthropic
    key, and would report itself unavailable for a user who has a
    subscription."""
    repos = (SRC / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    assert '"user_id": user.id' in repos
    handlers = (SRC / "sync" / "handlers.py").read_text(encoding="utf-8")
    assert 'user_id=p.get("user_id")' in handlers
    orch = (SRC / "generation" / "orchestrator.py").read_text(encoding="utf-8")
    assert "self.user_id" in orch


def test_a_run_level_choice_beats_the_workspace(monkeypatch):
    """The real workflow is "the api engine swept the repository, now let the
    agent redo the five modules that matter". A setting you must change and
    change back does not support that."""
    from src.generation import doc_language

    monkeypatch.setattr(doc_language, "get_workspace_engine",
                        lambda ws="default": ENGINE_API)
    assert resolve_doc_engine(ENGINE_CLAUDE_CODE, "ws-1") == ENGINE_CLAUDE_CODE


def test_an_unknown_engine_is_ignored_rather_than_fatal(monkeypatch):
    """This is read inside a background worker mid-build."""
    from src.generation import doc_language

    monkeypatch.setattr(doc_language, "get_workspace_engine",
                        lambda ws="default": ENGINE_API)
    assert resolve_doc_engine("nonsense", "ws-1") == ENGINE_API


def test_setting_an_unknown_engine_is_refused():
    from src.generation.doc_language import set_workspace_engine

    with pytest.raises(ValueError, match="unsupported"):
        set_workspace_engine("gpt-please", "ws-1")


def test_an_unavailable_agent_falls_back_rather_than_failing_the_build():
    """A vault build is a long job over many modules. A workspace whose Claude
    subscription lapsed should get documentation from the API rather than a
    dead job and no documents — and the result must say which engine ran, so
    nothing claims to be agent-written when it is not."""
    engine = build_engine(ENGINE_CLAUDE_CODE, "workspace-with-no-claude")
    assert engine.name == ENGINE_API
    assert isinstance(engine, ApiEngine)


# ─── redaction survives the choice ───────────────────────────────────


def test_code_is_redacted_before_it_reaches_any_engine():
    """GeminiClient ran `redact()` over the code, and that is a security
    control, not formatting. An engine choice must not become a way around it,
    so both engines compose through the same function."""
    prompt, _stats = compose_prompt(
        "TASK", "token = 'sk-live-abcdefghijklmnopqrst'", None, operation="t")
    assert "sk-live-abcdefghijklmnopqrst" not in prompt


def test_both_engines_compose_through_the_same_function():
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert engines.count("compose_prompt(") >= 2       # definition + api engine
    assert "compose_prompt(" in CLAUDE_DOCS, (
        "the agent engine assembles its own payload and can skip redaction"
    )


def test_the_metadata_and_code_sections_keep_their_shape():
    """The prompts describe these headings. Renaming them here changes what the
    model is reading against instructions that still name the old ones."""
    prompt, _ = compose_prompt("TASK", "x = 1", {"k": "v"}, operation="t")
    assert "## Context (metadata)" in prompt
    assert "## Source code (redacted)" in prompt


# ─── what the agent cannot do ────────────────────────────────────────


def test_the_agent_cannot_touch_the_filesystem():
    """It researches through the Celmis index or not at all. Given Read and
    Grep it would answer from whatever file it happened to open, which is the
    guessing this engine exists to prevent."""
    for tool in ("Bash", "Read", "Write", "Edit", "Grep", "Glob"):
        assert f'"{tool}"' in CLAUDE_DOCS, f"{tool} is no longer denied"
    assert 'allowed_tools=["mcp__celmis__*", "mcp__celmis"]' in CLAUDE_DOCS


def test_the_agent_cannot_reach_the_internet():
    for tool in ("WebFetch", "WebSearch"):
        assert f'"{tool}"' in CLAUDE_DOCS, f"{tool} is no longer denied"


def test_subagents_are_denied_for_the_reason_that_broke_review():
    """Agent/Task default to a BACKGROUND subagent whose result lands in a turn
    this loop never reads — the document would end at "let me look into that"."""
    assert '"Agent"' in CLAUDE_DOCS and '"Task"' in CLAUDE_DOCS


def test_the_brief_forbids_inferring_behaviour_from_a_name():
    """`validateOrder` may not validate. This is the single most common way a
    generated PRD states something false."""
    assert "Never infer behaviour from a name" in CLAUDE_DOCS


def test_the_brief_asks_for_an_admission_instead_of_a_guess():
    """Whitespace-insensitive: the sentence wraps, and an assertion that
    breaks when a line is re-wrapped is not testing anything real."""
    brief = " ".join(CLAUDE_DOCS.split()).lower()
    assert "determined from the code" in brief
    assert "a confident guess is not" in brief


def test_tool_calls_are_recorded_as_evidence():
    """A document produced with zero tool calls was written from the prompt
    alone — which is precisely what this engine is for avoiding, and it is only
    detectable if the calls are kept."""
    assert "tools_used" in CLAUDE_DOCS
    from src.generation.engines import DocResult

    assert "tools_used" in DocResult.__dataclass_fields__


def test_availability_gives_a_reason_not_a_bare_false():
    from src.generation.claude_docs import claude_docs_available

    ok, reason = claude_docs_available("workspace-with-no-claude")
    assert ok is False
    assert "Claude" in reason or "claude" in reason


# ─── the transport ───────────────────────────────────────────────────


def test_the_code_itself_reaches_the_model():
    """A PRD written without the code is a summary of filenames. Both engines
    must receive the source — the agent gets it AND the tools, not the tools
    instead of it."""
    prompt, _ = compose_prompt("TASK", "def charge(order):\n    return 1", None,
                               operation="generate_module_prd")
    assert "def charge(order):" in prompt

    for module in ("module_prd.py", "feature_doc.py", "integration_doc.py"):
        text = (SRC / "generation" / module).read_text(encoding="utf-8")
        assert "code_context=code_bundle.as_markdown()" in text, (
            f"{module} no longer sends the code it is documenting"
        )


def test_the_api_engine_uses_the_same_transport_as_review():
    """Review dispatches through LLMClient, which resolves the workspace
    profile and — when the LiteLLM gateway is on — sends the call as
    litellm_proxy/celmis-{ws}-{surface} under that tenant's virtual key.
    Generation went to the google-genai SDK directly and appeared in none of
    it."""
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert "from src.llm.client import build_llm_client" in engines
    base = (SRC / "review" / "agents" / "base.py").read_text(encoding="utf-8")
    assert "llm_client.generate(" in base


def test_the_engine_travels_with_the_queued_job():
    """Resolved when the job is queued, so a build started an hour ago uses the
    engine chosen then rather than whatever the setting says now."""
    repos = (SRC / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    assert '"engine": doc_engine' in repos
    handlers = (SRC / "sync" / "handlers.py").read_text(encoding="utf-8")
    assert 'engine=p.get("engine")' in handlers


@pytest.mark.parametrize("module", [
    "module_prd.py", "feature_doc.py", "integration_doc.py",
])
def test_every_generator_can_take_an_engine(module):
    text = (SRC / "generation" / module).read_text(encoding="utf-8")
    assert "self._dispatch(" in text, f"{module} still calls the client directly"
    # An engine is mandatory now. Falling back to a direct provider SDK would
    # be the exact bypass this module was fixed to remove, and it would be
    # invisible in the output.
    assert "self.engine is None" in text and "raise RuntimeError" in text
