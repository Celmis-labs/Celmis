"""The model asked about is the model that runs.

A capability answer is only worth something when it is about the string the
request will actually carry, and for a while it was not. Two functions answered
"which LiteLLM string does this configured model resolve to": `LLMClient` for
the call, and the settings router for the page. They agreed on everything except
the one case the per-agent overrides exist for — a bare model id belonging to a
vendor OTHER than the workspace's review profile.

The router took the vendor from the review profile unconditionally, so a
workspace on Google that pointed its architect at "gpt-4o" had the page ask
LiteLLM about "gemini/gpt-4o". Nothing by that name exists, so the answer was
`known=false`, and PUT /api/llm/config refused the save naming a model that has
never existed — while the review path was calling the very same override as
"gpt-4o" and getting a perfectly good answer. It is point-and-click reachable:
GET /api/models/available hands the dropdown LiteLLM's map keys, 529 of which
are bare, "gpt-4o" among them. The `review_policies.<agent>_model` columns have
stored bare cross-vendor ids since the day they were added.

The fix is one function — `src.llm.capabilities.resolve_litellm_model` — and
these tests are about the ANSWER it makes possible, not about the string it
returns: what matters is that a model's ceiling and its reasoning vocabulary
come back as that model's own, and that a model nobody can vouch for keeps
saying so.

The facts are read out of the installed litellm on purpose. A fixture of
hand-written model facts would be the stale vendor table this whole module
exists to avoid; if an upgrade moves them, these tests are what says so.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.llm.capabilities import (
    model_capabilities,
    provider_of,
    reset_capability_caches,
    resolve_litellm_model,
)

#: The workspace's review profile throughout: Google, the project default.
PROFILE = "google"
#: …and the model that profile is on. Same vendor, bare id.
SAME_VENDOR = "gemini-3-flash-preview"
#: A bare id from a DIFFERENT vendor, of the kind the dropdown offers and the
#: policy columns store. Chosen because it reasons and its effort vocabulary is
#: WIDER than the review profile's model — see the "xhigh" assertion below.
CROSS_VENDOR = "claude-sonnet-4-5"
#: The shape of a self-hosted selection: the operator's own name, no catalogue.
SELF_HOSTED = "openai/celmis-vllm-not-in-any-catalogue"


@pytest.fixture(autouse=True)
def _fresh_capability_cache():
    """Answers are memoised for the life of the process — that is the shipped
    behaviour, and it means one test's lookup would satisfy the next test's."""
    reset_capability_caches()
    yield
    reset_capability_caches()


# ══════════════════════════════════════════════════════════════════════
#  Which model the answer is about
# ══════════════════════════════════════════════════════════════════════


def test_a_model_from_another_vendor_keeps_its_own_vendors_answer():
    """The review profile does not get to decide what a model can do.

    This is the defect itself: the ceiling and the effort words reported for a
    cross-vendor override were the ones for a name nobody has ever served.
    """
    under_foreign_profile = model_capabilities(
        resolve_litellm_model(CROSS_VENDOR, PROFILE))
    under_its_own_vendor = model_capabilities(f"anthropic/{CROSS_VENDOR}")

    assert under_foreign_profile.known is True
    assert under_its_own_vendor.known is True
    assert under_foreign_profile.max_output_tokens == \
        under_its_own_vendor.max_output_tokens
    assert under_foreign_profile.reasoning_kind == \
        under_its_own_vendor.reasoning_kind
    assert under_foreign_profile.reasoning_values == \
        under_its_own_vendor.reasoning_values

    # The answer this used to be: the profile's vendor welded onto a model that
    # is not that vendor's. Asserted so the test above cannot go vacuous the day
    # litellm starts recognising the combination.
    assert model_capabilities(f"gemini/{CROSS_VENDOR}").known is False


def test_the_cross_vendor_answer_is_richer_than_the_profiles_own_model():
    """Not the same answer wearing a different name.

    Anthropic takes an effort word Gemini 3 Flash refuses. If the resolution
    ever collapses back onto the profile's vendor, the vocabulary reported for
    the override loses that word — which is how an operator gets told their
    valid choice is invalid.
    """
    cross = model_capabilities(resolve_litellm_model(CROSS_VENDOR, PROFILE))
    profile_model = model_capabilities(resolve_litellm_model(SAME_VENDOR, PROFILE))

    assert "xhigh" in (cross.reasoning_values or ())
    assert "xhigh" not in (profile_model.reasoning_values or ()), \
        "the profile's model gained xhigh — this test is now vacuous"


def test_a_bare_gemini_id_is_still_asked_about_as_gemini_not_vertex():
    """The rewrite that must survive the fix.

    litellm reads a bare "gemini-3-flash-preview" as vertex_ai, which sends the
    call looking for Application Default Credentials a container has no reason
    to hold — while /settings/llm shows the Gemini key saved and its Test button
    passing. The prefix is what makes the key we hold and the vendor litellm
    routes to the same vendor.

    Asked of litellm, not of `provider_of`: what matters is which credentials
    the request goes looking for, and litellm is what decides that.
    `provider_of` reads the bare id as Gemini either way — that disagreement is
    the thing being closed, so it cannot also be the thing doing the checking.
    """
    import litellm

    resolved = resolve_litellm_model(SAME_VENDOR, PROFILE)

    assert litellm.get_llm_provider(model=resolved)[1] == "gemini"
    assert litellm.get_llm_provider(model=SAME_VENDOR)[1] == "vertex_ai", \
        "litellm now agrees with us about a bare gemini id — the prefix is moot"
    assert provider_of(resolved) == "gemini", "…and the key we fetch is that one"

    caps = model_capabilities(resolved)
    assert caps.known is True
    assert caps.reasoning_kind == "effort"


def test_a_model_that_already_names_its_vendor_is_not_prefixed_again():
    """An operator who typed the full string gets the answer for it.

    Re-prefixing produced "gemini/anthropic/claude-…", which is unknown — a
    fully-qualified, entirely valid selection reported as unvouchable.
    """
    caps = model_capabilities(
        resolve_litellm_model(f"anthropic/{CROSS_VENDOR}", PROFILE))

    assert caps.known is True
    assert caps.max_output_tokens == \
        model_capabilities(CROSS_VENDOR).max_output_tokens


def test_a_self_hosted_model_stays_unknown_instead_of_borrowing_a_ceiling():
    """"Unknown" is an answer, and for a self-hosted server it is the honest one.

    The operator names the model whatever they like and litellm has no entry.
    A borrowed ceiling would clamp a call that would have worked; a borrowed
    reasoning vocabulary would let a setting be saved that gets dropped before
    the request goes out.
    """
    caps = model_capabilities(
        resolve_litellm_model(SELF_HOSTED, "openai_compatible"))

    assert caps.known is False
    assert caps.max_output_tokens is None
    assert caps.reasoning_kind is None
    assert caps.reasoning_values is None


def test_a_bare_self_hosted_name_takes_its_vendor_from_the_profile():
    """The one case where the profile DOES decide, and why.

    A self-hosted model id carries no vendor of its own, and the name-shape
    heuristics then answer for it: an operator serving Llama from their own vLLM
    calls it "llama-3.3-70b", which reads as groq — so the call goes looking for
    a groq key this workspace never had. The dialect is known here without
    guessing, because the profile says so and the address is its base_url.
    """
    local_name = "llama-3.3-70b"          # shaped like a vendor it is not served by
    resolved = resolve_litellm_model(local_name, "openai_compatible")

    assert provider_of(resolved) == "openai", \
        f"a self-hosted model was routed to {provider_of(resolved)}"
    assert provider_of(local_name) != "openai", \
        "the name no longer misleads the heuristics — this test is now vacuous"
    # Still nothing litellm can vouch for — a prefix is not a catalogue entry.
    assert model_capabilities(resolved).known is False


def test_a_name_litellm_never_heard_of_does_not_acquire_a_plausible_vendor():
    """A release newer than the installed package must read as unknown.

    "gemini-3-pro" is unmapped today while "gemini-3-pro-preview" is mapped, and
    the difference has to reach the operator as "unknown" rather than as an
    invented ceiling. The name that comes back is the one they typed, because
    that is the name the refusal will quote back at them.
    """
    typed = "celmis-frontier-model-2031"
    caps = model_capabilities(resolve_litellm_model(typed, PROFILE))

    assert caps.known is False
    assert caps.model == typed
    assert caps.max_output_tokens is None


# ══════════════════════════════════════════════════════════════════════
#  …and the call goes out as that same string
# ══════════════════════════════════════════════════════════════════════


def _response() -> MagicMock:
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = "ok"
    choice.finish_reason = "stop"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.total_cost = None
    return resp


def _sent(model: str, **kw) -> dict:
    """Run one generate() and hand back the kwargs litellm.completion saw."""
    from src.llm.client import LLMClient

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _response()

    with patch("litellm.completion", side_effect=fake_completion):
        LLMClient(resolve_key=lambda p: "sk-fake-key-for-tests").generate(
            model=model, prompt="hi", mode="review", operation="t", **kw,
        )
    return captured


@pytest.mark.parametrize("configured", [SAME_VENDOR, CROSS_VENDOR, f"anthropic/{CROSS_VENDOR}"])
def test_the_call_goes_out_as_the_string_the_capability_answer_was_about(configured):
    """One function, both callers.

    The point of extracting it: whatever /settings/llm asked litellm about is
    the string that reaches litellm.completion. Two inferences is how the page
    came to refuse a save for a model the review path was calling happily.
    """
    assert _sent(configured)["model"] == resolve_litellm_model(configured)


def test_the_ceiling_that_cuts_the_call_is_the_cross_vendor_models_own():
    """The end-to-end shape of the bug, in one assertion.

    A Google workspace whose architect is on a bare "gpt-4o" gets clamped to
    OpenAI's 16 384, not left uncut because "gemini/gpt-4o" was unknown. An
    uncut call here is a provider 400 hours later that never names the number
    that caused it.
    """
    ceiling = model_capabilities(resolve_litellm_model("gpt-4o", PROFILE)).max_output_tokens
    assert ceiling, "gpt-4o left the installed litellm's map — this test is vacuous"

    assert _sent("gpt-4o", max_output_tokens=ceiling * 4)["max_tokens"] == ceiling
