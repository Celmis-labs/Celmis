"""One transport for completions: LiteLLM, for every provider.

The gateway exists so that swapping a provider — or absorbing a vendor's API
change — is a LiteLLM concern rather than ours. A second code path that calls
google-genai directly defeats that for whoever lands on it, and it was
unreachable in production anyway: every workspace resolves to
`via_gateway=True`, so the branch was dead code that nonetheless decided the
answer to "what happens when Google changes generateContent".

So the completion branches are gone and this keeps them gone. `p.litellm_model`
already yields `litellm_proxy/<deployment>` behind the gateway and
`gemini/<model>` without one, so a single call covers both.

Embeddings held out longest, as a deliberate, documented exception — a vector
is baked into a stored Qdrant collection, and a transport that drifts from the
vectors the collection was built with degrades search silently. The exception
was retired when the fear was answered with evidence: the installed LiteLLM's
`gemini/` route was captured at the wire posting the same batchEmbedContents
body the native SDK posted, and the request fields are now pinned in
test_embedding_requests_do_not_drift.py. So embeddings are LiteLLM too, and
the allow-list below is down to the client that holds the SDK and its one
live consumer.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

#: The native client itself, and its one live consumer. Everything else must
#: go through LiteLLM.
#:
#: This list was audited down from twelve entries (2026-08). Nine of them no
#: longer did the thing they were listed for — the QA orchestrator, the vault
#: retriever, both review-agent fallbacks and all five generation modules had
#: been migrated to `build_llm_client` and kept only comments naming the old
#: call, which the matcher below already strips. A stale exemption is a
#: standing invitation to regress unnoticed, so they are gone rather than
#: grandfathered.
ALLOWED = {
    # Defines GeminiClient and constructs it (the per-workspace cache in
    # `_gemini_for`). Kept for exactly one surface: the native tool loop.
    "llm/gemini_client.py",
    # The consumer of that surface. Gemini's function-calling loop round-trips
    # thoughtSignature as raw Parts — Gemini 3.x answers 400 INVALID_ARGUMENT
    # without them, and LiteLLM's OpenAI shape does not contract to carry
    # them. CLI-only, not reachable from the API.
    "qa/exploration_agent.py",
}


def _api_path_files() -> list[Path]:
    return [
        p for p in SRC.rglob("*.py")
        if str(p.relative_to(SRC)) not in ALLOWED
    ]


def test_no_completion_path_calls_google_genai_directly():
    offenders: list[str] = []
    for path in _api_path_files():
        source = path.read_text()
        # Docstrings and comments name the old call while explaining why it is
        # gone — only real code counts.
        source = re.sub(r'""".*?"""', "", source, flags=re.S)
        source = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        for match in re.finditer(r"get_gemini_client\(|GeminiClient\(", source):
            line = source[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(SRC)}:{line}")
    assert not offenders, (
        "these reach google-genai directly instead of going through LiteLLM: "
        f"{offenders}"
    )


def test_the_surfaces_that_were_migrated_use_litellm():
    for rel in ("review/architecture.py", "api/routers/deps.py", "deps/report.py"):
        source = (SRC / rel).read_text()
        assert "litellm.completion(" in source, f"{rel} lost its LiteLLM call"
        assert "p.litellm_model" in source, f"{rel} is not using the profile model"


def test_streaming_has_no_vendor_branch_left():
    source = (SRC / "llm" / "completion.py").read_text()
    idx = source.find("async def stream_chat(")
    end = source.find("\ndef ", idx)
    body = source[idx:end if end > 0 else idx + 3000]
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "if p.is_google:" not in code
    assert "_litellm_stream(" in code


def test_the_embedding_exception_is_retired_not_forgotten():
    """The exception said: a changed transport can drift from the vectors the
    Qdrant collection was built with, silently. It was retired on evidence —
    the wire request through LiteLLM was captured identical, and the fields
    are pinned behaviourally in test_embedding_requests_do_not_drift.py.

    What THIS pin keeps is the shape that makes drift reviewable: exactly one
    `litellm.embedding` call site (embed and embed_batch funnel through
    `_litellm_embed`, so there is one place to read and one to get wrong),
    and no way back to the native client from this module."""
    source = (SRC / "llm" / "completion.py").read_text()
    # Same stripping the ban above uses: docstrings and comments may name the
    # old client while explaining why it is gone — only real code counts.
    source = re.sub(r'""".*?"""', "", source, flags=re.S)
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert code.count("litellm.embedding(") == 1, (
        "embed and embed_batch must share the single _litellm_embed call site"
    )
    assert "GeminiClient" not in code and "get_gemini_client" not in code, (
        "completion.py found a way back to the native client"
    )


def test_the_verifier_uses_the_shared_client_when_there_is_one():
    """It had no such branch, so on a gateway workspace it went straight to
    google-genai, found no raw key, and failed into its own pass-through —
    silently. A verifier that never runs turns every review into an unfiltered
    agent dump while reporting nothing wrong."""
    source = (SRC / "review" / "agents" / "verifier.py").read_text()
    assert "context.llm_client is not None" in source
    # Stronger than the ordering this used to check: there is no direct-client
    # branch left to come second. The fallback builds a gateway client too.
    assert "get_gemini_client" not in source
    assert "build_llm_client(" in source


def test_the_agent_client_is_routed_through_the_gateway():
    """The review agents asked for a bare "gemini-3-pro", so the key resolver
    looked for a raw `gemini` credential the workspace does not hold — every
    LLM agent died before sending a token, which is what "PARTIAL REVIEW —
    architect, security agent(s) failed" with tokens 0/0 actually was."""
    source = (SRC / "llm" / "client.py").read_text()
    idx = source.find("def build_llm_client(")
    body = source[idx:]
    assert "resolve_profile(" in body
    assert "p.litellm_model" in body
    assert 'provider == "litellm_proxy"' in body
    assert "gateway_url" in body, "the proxy address must be passed to litellm"


def test_token_details_are_read_without_assuming_a_dict():
    """`prompt_tokens_details` is a pydantic wrapper on some providers and a
    dict on others. Calling `.get` on the wrapper raised AttributeError AFTER
    the model had answered — the call succeeded and the bookkeeping killed
    it."""
    source = (SRC / "llm" / "client.py").read_text()
    assert "isinstance(details, dict)" in source
    assert 'getattr(details, "cached_tokens", 0)' in source


def test_the_profile_model_works_without_a_gateway():
    """The migration rests on this: with no gateway the profile still yields a
    model name LiteLLM understands, so removing the native branch does not
    strand a direct-key workspace."""
    source = (SRC / "llm" / "profiles.py").read_text()
    idx = source.find("def litellm_model(")
    body = source[idx:idx + 500]
    assert "litellm_prefix(self.provider)" in body
    assert 'f"{prefix}/{self.model}"' in body
