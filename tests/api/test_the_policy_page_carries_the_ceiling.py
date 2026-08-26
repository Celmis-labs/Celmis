"""The screen with the most authority now shows — and checks — all three knobs.

/admin/review-policies wins over /settings/llm: whatever a repo policy says is
what the review runs with. It could say only WHICH MODEL, through the five
`<agent>_model` columns, so an operator who picked a model there had no way to
know that an output ceiling and a reasoning level existed at all, that they
lived on another page, or that their combination with the model just chosen can
be invalid — the architect agent failed in 43% of runs against a ceiling
somebody had to guess at.

`agent_llm_overrides` is one JSONB column shaped exactly like the workspace
`agents` blob, and `_validate_agent_entry` — the workspace layer's own
validator — is imported rather than twinned, because a layer that OUTRANKS
another while validating by different rules is how an invalid combination
reaches a provider.

The one property worth more than the rest is which MODEL a value is judged
against: the effective one AFTER the save. This form changes the model and the
ceiling in the same submit, and `compliance` has no model column at all, so
"the model in the row" and "the model this agent will run on" are routinely
different strings. Asking about the wrong one was a real bug at the workspace
layer — a save refused in the name of a model that save was replacing.

Model facts come from the installed litellm rather than from literals here, on
purpose: a hand-written table of ceilings is the thing this whole feature
exists to avoid.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import llm as llm_router
from src.api.routers import review_policies as policies_router
from src.db.models import RepoReviewPolicy
from src.db.session import get_async_session


# The suite has no Postgres, and `repo_review_policies` is JSONB from
# `target_branches` down. A TEST-side rendering of JSONB as sqlite's JSON: the
# DDL that reaches a real database still comes from Alembic.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


REPO = "acme/api"
WS = "ws-1"
_USER = SimpleNamespace(id="u-1", email="lead@test", is_admin=True)

# Known to the installed litellm, reasons, and takes an effort WORD.
REASONING_MODEL = "gemini-3-flash-preview"
REASONING_LITELLM = "gemini/gemini-3-flash-preview"
# Equally known, and takes NO reasoning parameter at all — so a reasoning value
# aimed at it would be dropped by litellm and reach nothing, which is the
# silent no-op this surface refuses.
PLAIN_MODEL = "gpt-4o"
PLAIN_LITELLM = "gpt-4o"


def _workspace(model: str, provider: str, **agents) -> dict:
    """A workspace LLM blob whose review surface is set to `model`.

    Both keys are set because they answer different questions: `model` is the
    layer of the inheritance chain every agent without an entry lands on, and
    `profiles.review` is where the vendor prefix for a bare id comes from.
    """
    blob: dict = {
        "model": model,
        "profiles": {"review": {"provider": provider, "model": model}},
    }
    if agents:
        blob["agents"] = agents
    return blob


@pytest.fixture(autouse=True)
def _fresh_capability_cache():
    """Capability answers are memoised for the life of the process."""
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


def _ceiling(model: str) -> int:
    """The model's real output ceiling, asked of the installed litellm."""
    from src.llm.capabilities import model_capabilities
    caps = model_capabilities(model)
    assert caps.known and caps.max_output_tokens, (
        f"the installed litellm no longer knows {model} — these tests are "
        f"written against what it reports, so this is the thing that says so"
    )
    return int(caps.max_output_tokens)


@asynccontextmanager
async def policy_api(workspace: dict, *, rows: list[dict] | None = None):
    """The real router over a sqlite policy table and a fixed workspace config.

    `rows` are inserted before the app is built — that is how a policy written
    BEFORE this column existed is expressed: the key simply absent.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(RepoReviewPolicy.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for row in rows or []:
            session.add(RepoReviewPolicy(**{
                "repo_slug": REPO, "workspace_id": WS, "enabled": True,
                "prompt_template": "", "target_branches": [], "folder_rules": [],
                "agent_prompt_overrides": {}, "mcp_sources": [],
                "disabled_agents": [], **row,
            }))
        await session.commit()

    app = FastAPI()
    app.include_router(policies_router.router)

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: WS

    async def _permitted(_slug, _user, _workspace_id=None):
        return "admin", True

    original_perm = deps_module._effective_repo_permission
    original_cfg = llm_router._load_workspace_config
    deps_module._effective_repo_permission = _permitted
    llm_router._load_workspace_config = lambda workspace_id="default": workspace
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://policies") as c:
            yield c
    finally:
        deps_module._effective_repo_permission = original_perm
        llm_router._load_workspace_config = original_cfg
        await engine.dispose()


def _body(**overrides) -> dict:
    """A full policy upsert — this endpoint replaces the whole policy."""
    return {"enabled": True, "prompt_template": "", "target_branches": [],
            "folder_rules": [], **overrides}


async def _put(client: AsyncClient, **overrides):
    return await client.put(f"/api/review-policies/{REPO}", json=_body(**overrides))


async def _get(client: AsyncClient) -> dict:
    response = await client.get(f"/api/review-policies/{REPO}")
    assert response.status_code == 200, response.text
    return response.json()


# ══════════════════════════════════════════════════════════════════════
#  What the page can now say, and what it reports is in force
# ══════════════════════════════════════════════════════════════════════


async def test_a_ceiling_and_a_reasoning_level_survive_the_round_trip():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        saved = await _put(client, agent_llm_overrides={
            "contract": {"max_output_tokens": 40000, "reasoning": "high"},
        })
        assert saved.status_code == 200, saved.text

        policy = await _get(client)
        assert policy["agent_llm_overrides"] == {
            "contract": {"max_output_tokens": 40000, "reasoning": "high"},
        }


async def test_the_page_reports_what_each_agent_would_actually_run_with():
    """The complaint that started this: the layer with the most authority
    showed the least. A policy that overrides one agent has to be able to show
    what the other five inherit, or the operator is guessing again."""
    workspace = _workspace(
        REASONING_MODEL, "google",
        contract={"max_output_tokens": 20000, "reasoning": "medium"},
    )
    async with policy_api(workspace) as client:
        await _put(client, agent_llm_overrides={
            "contract": {"max_output_tokens": 40000},
        })

        effective = (await _get(client))["agents_effective"]

        assert effective["contract"]["max_output_tokens"] == 40000, (
            "the policy is the layer that WINS — the workspace entry's 20000 "
            "came back instead"
        )
        # The reasoning the policy did not mention still comes from below it.
        assert effective["contract"]["reasoning"] == "medium"
        assert effective["contract"]["model"] == REASONING_LITELLM
        # And an agent this policy says nothing about reports its inheritance
        # rather than going silent.
        assert effective["verifier"]["model"] == REASONING_LITELLM
        assert isinstance(effective["verifier"]["max_output_tokens"], int)


async def test_a_repo_with_no_policy_still_says_what_its_agents_run_with():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        policy = await _get(client)

        assert policy["agent_llm_overrides"] == {}
        assert policy["agents_effective"]["contract"]["model"] == REASONING_LITELLM


async def test_the_neighbouring_prompt_overrides_still_come_back():
    """Adding a field to `ReviewPolicyOut` must not silently remove one.

    `agent_prompt_overrides` was dropped from that model in the same edit that
    added `agent_llm_overrides`. Nothing failed: the router kept passing the
    keyword, pydantic ignores an undeclared one, and the field simply stopped
    appearing on the wire. The detail page then loaded every prompt box empty
    and PUT those empty boxes back over the stored prompts on the first save.

    Asserted through the round trip rather than by inspecting the model,
    because what the operator loses is the value, not the annotation.
    """
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        saved = await _put(client, agent_prompt_overrides={
            "security": "Name the CVE, not the category.",
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["agent_prompt_overrides"] == {
            "security": "Name the CVE, not the category.",
        }

        policy = await _get(client)
        assert policy["agent_prompt_overrides"] == {
            "security": "Name the CVE, not the category.",
        }


async def test_a_policy_written_before_the_column_existed_reads_as_inherit():
    """Every row in every existing installation has NULL here."""
    workspace = _workspace(
        REASONING_MODEL, "google",
        contract={"max_output_tokens": 20000},
    )
    async with policy_api(workspace, rows=[{"architect_model": PLAIN_MODEL}]) as client:
        policy = await _get(client)

        assert policy["agent_llm_overrides"] == {}, (
            "NULL has to mean inherit, the same as an absent key at every "
            "other layer — not 'override with nothing'"
        )
        assert policy["agents_effective"]["contract"]["max_output_tokens"] == 20000
        assert policy["agents_effective"]["contract"]["model"] == PLAIN_LITELLM


async def test_a_save_that_does_not_mention_the_blob_leaves_it_alone():
    """The rollout window: the page has model dropdowns before it has ceiling
    controls, and a save from it must not wipe what it cannot render."""
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, agent_llm_overrides={
            "contract": {"max_output_tokens": 40000},
        })

        await _put(client, architect_model=REASONING_MODEL)

        policy = await _get(client)
        assert policy["agent_llm_overrides"] == {
            "contract": {"max_output_tokens": 40000},
        }


async def test_an_override_is_cleared_by_leaving_it_out_of_the_next_save():
    """Sent whole, replacing the stored map — the only shape that can express
    a removal, since absent already means inherit."""
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, agent_llm_overrides={
            "contract": {"max_output_tokens": 40000, "reasoning": "high"},
            "verifier": {"max_output_tokens": 2048},
        })

        await _put(client, agent_llm_overrides={
            "contract": {"max_output_tokens": 40000},
        })

        assert (await _get(client))["agent_llm_overrides"] == {
            "contract": {"max_output_tokens": 40000},
        }


# ══════════════════════════════════════════════════════════════════════
#  Refusals, in front of the person who typed them
# ══════════════════════════════════════════════════════════════════════


async def test_a_ceiling_above_the_model_ceiling_is_refused_naming_both_numbers():
    """A request over the model's ceiling is a 400 from the provider hours
    later, in a message that names neither number."""
    ceiling = _ceiling(REASONING_LITELLM)
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        response = await _put(client, agent_llm_overrides={
            "contract": {"max_output_tokens": ceiling + 1},
        })

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert str(ceiling) in detail and str(ceiling + 1) in detail
        assert REASONING_LITELLM in detail

        assert (await _get(client))["agent_llm_overrides"] == {}, (
            "a refused save still wrote something"
        )


async def test_a_reasoning_value_the_model_does_not_take_is_refused_naming_what_it_does():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        response = await _put(client, agent_llm_overrides={
            "contract": {"reasoning": "ludicrous"},
        })

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "high" in detail, f"the refusal named no accepted value: {detail}"
        assert REASONING_LITELLM in detail


async def test_a_reasoning_value_is_refused_for_a_model_that_reasons_not_at_all():
    """The silent no-op this surface exists to end: litellm would drop the
    parameter and the setting would change nothing."""
    async with policy_api(_workspace(PLAIN_MODEL, "openai")) as client:
        response = await _put(client, agent_llm_overrides={
            "contract": {"reasoning": "high"},
        })

        assert response.status_code == 422
        assert PLAIN_LITELLM in response.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
#  …judged against the model that will be EFFECTIVE after the save
# ══════════════════════════════════════════════════════════════════════


async def test_a_value_is_judged_against_the_model_this_save_is_choosing():
    """Both halves travel in one submit, so the stored model is the wrong one
    to ask about — it is the model being REPLACED."""
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        # Stored state: a model that takes "high", and a policy using it.
        assert (await _put(client, agent_llm_overrides={
            "contract": {"reasoning": "high"},
        })).status_code == 200

        response = await _put(
            client,
            architect_model=PLAIN_MODEL,
            agent_llm_overrides={"contract": {"reasoning": "high"}},
        )

        assert response.status_code == 422, (
            "the save was judged against the model it was replacing — the "
            "review would have gone out on gpt-4o with the reasoning dropped"
        )
        assert PLAIN_LITELLM in response.json()["detail"]


async def test_a_value_is_not_refused_in_the_name_of_a_model_being_replaced():
    """The other direction, and the exact bug fixed at the workspace layer: a
    refusal naming a model the policy is on its way OFF."""
    async with policy_api(_workspace(PLAIN_MODEL, "openai")) as client:
        response = await _put(
            client,
            architect_model=REASONING_MODEL,
            agent_llm_overrides={"contract": {"reasoning": "high"}},
        )

        assert response.status_code == 200, (
            "refused in the name of the inherited gpt-4o, for a save after "
            f"which the architect runs on {REASONING_MODEL} and takes 'high' "
            f"happily: {response.text}"
        )
        policy = await _get(client)
        assert policy["agents_effective"]["contract"]["model"] == REASONING_LITELLM
        assert policy["agents_effective"]["contract"]["reasoning"] == "high"


async def test_a_policy_value_is_judged_against_an_inherited_model():
    """`compliance` has no model column and never had one, so a ceiling saved
    for it here is always judged against a model from a layer below."""
    ceiling = _ceiling(REASONING_LITELLM)
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        refused = await _put(client, agent_llm_overrides={
            "compliance": {"max_output_tokens": ceiling + 1},
        })
        assert refused.status_code == 422
        assert REASONING_LITELLM in refused.json()["detail"]

        allowed = await _put(client, agent_llm_overrides={
            "compliance": {"max_output_tokens": ceiling, "reasoning": "low"},
        })
        assert allowed.status_code == 200, allowed.text


async def test_the_inherited_model_that_is_judged_is_the_workspace_entry_not_the_profile():
    """The chain has two layers below this one, and they can disagree.

    A workspace `agents` entry that puts one agent on a different model is
    what that agent will run on — so that is the model its policy value has to
    be judged against, not the review profile the other five use.
    """
    workspace = _workspace(
        REASONING_MODEL, "google", compliance={"model": PLAIN_MODEL},
    )
    async with policy_api(workspace) as client:
        response = await _put(client, agent_llm_overrides={
            "compliance": {"reasoning": "high"},
        })

        assert response.status_code == 422
        assert PLAIN_LITELLM in response.json()["detail"], (
            "judged against the review profile's model, which is not the one "
            "compliance runs on"
        )


# ══════════════════════════════════════════════════════════════════════
#  One field, one place
# ══════════════════════════════════════════════════════════════════════


async def test_a_model_inside_the_blob_is_refused_and_told_where_it_lives():
    """Two sources for one field is the failure this project keeps hitting.

    The model of this layer is the column; the blob carries the other two. A
    payload that puts a model in the blob is not quietly ignored — silently
    dropping it is how an operator ends up watching a review run on a model
    they believe they changed.
    """
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        response = await _put(client, agent_llm_overrides={
            "contract": {"model": PLAIN_MODEL},
        })

        assert response.status_code == 422
        assert "architect_model" in response.json()["detail"]


async def test_the_agent_with_no_model_column_is_told_where_its_model_comes_from():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        response = await _put(client, agent_llm_overrides={
            "compliance": {"model": PLAIN_MODEL},
        })

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "compliance_model" not in detail, (
            "named a column that does not exist — the operator would go "
            "looking for a control nothing renders"
        )
        assert "/settings/llm" in detail


async def test_an_agent_nobody_ships_is_refused_and_named():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        response = await _put(client, agent_llm_overrides={
            "archtiect": {"max_output_tokens": 8192},
        })

        assert response.status_code == 422
        assert "contract" in response.json()["detail"]


async def test_a_misspelled_field_is_refused_rather_than_stored():
    """`max_tokens` saved silently, and a ceiling that changes nothing, is the
    failure the whole per-agent surface was built to make impossible."""
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        response = await _put(client, agent_llm_overrides={
            "contract": {"max_tokens": 8192},
        })

        assert response.status_code == 422
        assert "max_output_tokens" in response.json()["detail"]


async def test_the_stored_row_is_what_a_review_would_read():
    """The column is the whole point: what the API wrote has to be what the
    orchestrator's policy loader hands to the resolver."""
    from src.review.settings import ReviewSettings, resolve_agent_llm

    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(
            client,
            architect_model=PLAIN_MODEL,
            agent_llm_overrides={"contract": {"max_output_tokens": 9000}},
        )
        stored = await _get(client)

    resolved = resolve_agent_llm(
        "contract",
        policy={
            "architect_model": stored["architect_model"],
            "agents": stored["agent_llm_overrides"],
        },
        workspace_cfg=_workspace(REASONING_MODEL, "google"),
        settings=ReviewSettings(),
    )

    assert resolved.model == PLAIN_MODEL
    assert resolved.max_output_tokens == 9000
