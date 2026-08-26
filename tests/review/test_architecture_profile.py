"""Repo intel's "Rebuild" never worked on a gateway workspace.

Reported as: "Could not build the architecture summary. The model call failed,
or this repository has no indexed clone to read. Check LLM Setup, then index
the repository and try again." — on a repository that was indexed, with a
working LLM. The message sent people to fix two things that were not broken.

What actually happened, reproduced against production:

    _collect_context(...)              -> 2 KB of file tree + readmes   OK
    build_llm_client(...)              -> LLMClient                     OK
    client.generate(model=...)         -> LLMCredentialError

The model came from `get_review_settings().quality_model`, i.e. a BARE Google
name like "gemini-3-flash-preview". Every production workspace is routed
through the LiteLLM gateway and holds a LiteLLM virtual key, not a raw
`gemini` one, so the client refused: "no 'gemini' key for workspace". Passing
the gateway model instead does not help — build_llm_client rejects the
`litellm_proxy` provider outright.

The fix is to go out the way the dependency report already does: resolve the
workspace profile and call litellm with its model and key.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import src.review.architecture as arch

SOURCE = (Path(__file__).resolve().parents[2]
          / "src" / "review" / "architecture.py").read_text()


def test_the_model_comes_from_the_workspace_profile():
    body = inspect.getsource(arch.generate_summary)
    assert "resolve_profile(" in body, (
        "the model is picked outside the profile system again — on a gateway "
        "workspace that yields a bare provider model and no usable key"
    )
    assert "p.litellm_model" in body


def test_it_does_not_go_through_the_byok_client():
    """build_llm_client does not know the `litellm_proxy` provider, so no
    gateway workspace can ever reach a model through it."""
    body = inspect.getsource(arch.generate_summary)
    # Comments still name it — they explain why it is gone. Only a real call
    # or import is a regression.
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "build_llm_client(" not in code
    assert "import build_llm_client" not in code


def test_both_routes_go_through_one_transport():
    """A direct-key workspace and a gateway workspace take the SAME call.
    `p.litellm_model` is `gemini/<model>` without a gateway and
    `litellm_proxy/<deployment>` with one, so no vendor branch is needed."""
    body = inspect.getsource(arch.generate_summary)
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "if p.is_google:" not in code
    assert "litellm.completion(" in code


def test_a_missing_key_fails_quietly_rather_than_raising():
    """The contract is documented in the docstring: never raises, empty string
    means "could not". A raise here would 500 the rebuild endpoint instead of
    producing its explanatory 502."""
    body = inspect.getsource(arch.generate_summary)
    assert "if not p.api_key:" in body
    assert 'return "", None, 0' in body
    assert "Never raises" in (arch.generate_summary.__doc__ or "")


def test_the_gateway_call_is_recorded_in_the_spend_ledger():
    """The Google branch bills itself inside GeminiClient; this one has to be
    recorded explicitly or a rebuild is invisible on the Usage page — which is
    the same blind spot that hid the failing review agents."""
    body = inspect.getsource(arch.generate_summary)
    assert "record_completion_spend(" in body
    assert 'operation="architecture_summary"' in body


def test_an_empty_summary_is_never_stored():
    """Storing it would overwrite a good summary with nothing and report
    success — a green toast above "(no summary — rebuild)"."""
    intel = (Path(__file__).resolve().parents[2]
             / "src" / "api" / "routers" / "intel.py").read_text()
    idx = intel.find("async def rebuild_architecture(")
    body = intel[idx:idx + 1400]
    assert 'if not (summary_md or "").strip():' in body
    assert "status_code=502" in body
    assert body.find("raise HTTPException") < body.find("session.get(RepoSummary")
