"""The layer that wins now carries all three settings, not just the model.

/admin/review-policies is the top of the inheritance chain: a repo policy beats
the workspace `agents` entry, which beats the review profile, which beats
ReviewSettings. Until now the only thing it could say was WHICH MODEL — the
five `<agent>_model` columns — so the output ceiling that failed the architect
agent in 43% of runs, and the reasoning level that decides how much of that
ceiling the model spends thinking, could be set one layer down and then be
silently outranked here by a model this layer had chosen alone.

`repo_review_policies.agent_llm_overrides` is where they live now, shaped
exactly like the workspace `agents` blob so that one resolver and one validator
serve both screens. What these tests hold down:

  - each of the two travels the SAME path the model already travelled, layer
    for layer — a ceiling that resolved differently from the model it belongs
    to is how the two drift apart;
  - the MODEL has exactly one home at this layer, the column. The blob carries
    no `model` key, and a value smuggled into one is not a second opinion;
  - the column reaches the orchestrator. A resolver nobody's request reaches is
    the class of bug this project has shipped twice.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.review.settings import (
    ReviewSettings,
    default_agent_max_output_tokens,
    resolve_agent_llm,
)


# The suite has no Postgres — it builds its schema from the models — and
# `repo_review_policies` is JSONB from `target_branches` down. Rendering JSONB
# as sqlite's JSON is a TEST-side shim on purpose: the DDL that reaches a real
# database still comes from Alembic, and nothing about the product changes to
# make a test runnable.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


WORKSPACE = {
    "model": "gemini/gemini-3-flash-preview",
    "agents": {
        "defect": {
            "model": "anthropic/claude-sonnet-4-5",
            "max_output_tokens": 20000,
            "reasoning": "medium",
        },
    },
}


def _policy(**agents) -> dict:
    """A policy in the shape `ReviewOrchestrator._load_policy` hands over."""
    return {"agents": agents}


# ─── the two new settings walk the model's path, layer for layer ─────


def test_the_policy_ceiling_beats_the_workspace_entry():
    resolved = resolve_agent_llm(
        "defect",
        policy=_policy(defect={"max_output_tokens": 40000}),
        workspace_cfg=WORKSPACE,
        settings=ReviewSettings(agent_max_output_tokens=12345),
    )

    assert resolved.max_output_tokens == 40000


def test_the_policy_reasoning_beats_the_workspace_entry():
    resolved = resolve_agent_llm(
        "defect",
        policy=_policy(defect={"reasoning": "high"}),
        workspace_cfg=WORKSPACE,
        settings=ReviewSettings(),
    )

    assert resolved.reasoning == "high"


def test_the_workspace_entry_beats_the_profile_which_beats_the_default():
    """The two layers under the policy, in one place, so the order is visible.

    The profile has no say over the ceiling by design — `workspace_cfg["model"]`
    is the model and only the model, and the legacy top-level
    `max_output_tokens` beside it is the 4096 nothing has ever read.
    """
    settings = ReviewSettings(agent_max_output_tokens=12345)

    defect = resolve_agent_llm(
        "defect", workspace_cfg=WORKSPACE, settings=settings,
    )
    security = resolve_agent_llm(
        "security", workspace_cfg=WORKSPACE, settings=settings,
    )

    assert (defect.max_output_tokens, defect.reasoning) == (20000, "medium")
    assert defect.model == "anthropic/claude-sonnet-4-5"
    assert (security.max_output_tokens, security.reasoning) == (12345, None)
    assert security.model == "gemini/gemini-3-flash-preview"


def test_the_policy_answers_per_agent_and_not_for_the_rest():
    """Six agents, one blob: an entry for one must not reach another. The whole
    reason the ceiling stopped being global is that they differ."""
    policy = _policy(
        defect={"max_output_tokens": 40000, "reasoning": "high"},
        verifier={"max_output_tokens": 2048},
    )
    settings = ReviewSettings(agent_max_output_tokens=12345)

    defect = resolve_agent_llm(
        "defect", policy=policy, workspace_cfg=WORKSPACE, settings=settings,
    )
    verifier = resolve_agent_llm(
        "verifier", policy=policy, workspace_cfg=WORKSPACE, settings=settings,
    )
    quality = resolve_agent_llm(
        "quality", policy=policy, workspace_cfg=WORKSPACE, settings=settings,
    )

    assert (defect.max_output_tokens, defect.reasoning) == (40000, "high")
    assert (verifier.max_output_tokens, verifier.reasoning) == (2048, None)
    assert (quality.max_output_tokens, quality.reasoning) == (12345, None)


def test_an_absent_field_in_a_policy_entry_inherits_rather_than_blanking():
    """A policy that names only the reasoning must not drag the ceiling down
    to the floor — every field is optional at every layer."""
    resolved = resolve_agent_llm(
        "defect",
        policy=_policy(defect={"reasoning": "low"}),
        workspace_cfg=WORKSPACE,
        settings=ReviewSettings(agent_max_output_tokens=12345),
    )

    assert resolved.max_output_tokens == 20000, (
        "the workspace entry's ceiling was blanked by a policy that said "
        "nothing about it"
    )
    assert resolved.reasoning == "low"


# ─── one home for the model ──────────────────────────────────────────


def test_the_model_at_this_layer_is_the_column_and_only_the_column():
    """Two sources for one field is the failure this project keeps hitting.

    The repo blob is written by an API that refuses a `model` key, and the
    resolver does not read one either — so a value put there by hand (or by a
    future caller that forgets) changes nothing, rather than quietly outranking
    or being outranked by the column depending on which layer you read first.
    """
    with_column = resolve_agent_llm(
        "defect",
        policy={
            # defect's legacy column — the one a stored policy row has
            "quality_model": "gpt-4o",
            "agents": {"defect": {"model": "anthropic/claude-sonnet-4-5"}},
        },
        workspace_cfg=WORKSPACE,
        settings=ReviewSettings(),
    )

    assert with_column.model == "gpt-4o"

    # The case that tells the two designs apart. With the column empty, a blob
    # that carried a model would be the top of the chain and would outrank the
    # workspace; with one home for the field, the workspace entry is what this
    # agent runs on and the smuggled value is inert.
    without_column = resolve_agent_llm(
        "security",
        policy={"agents": {"security": {"model": "anthropic/claude-sonnet-4-5"}}},
        workspace_cfg=WORKSPACE,
        settings=ReviewSettings(),
    )

    assert without_column.model == "gemini/gemini-3-flash-preview"


def test_an_agent_with_no_model_column_inherits_one():
    """`compliance` is configurable and has never had a column of its own.

    It can carry a ceiling and a reasoning level here; its model comes from the
    workspace. That is the case the save-time validator has to judge against an
    INHERITED model rather than a policy one.
    """
    resolved = resolve_agent_llm(
        "compliance",
        policy=_policy(compliance={"max_output_tokens": 4000, "reasoning": "low"}),
        workspace_cfg=WORKSPACE,
        settings=ReviewSettings(),
    )

    assert resolved.model == "gemini/gemini-3-flash-preview"
    assert (resolved.max_output_tokens, resolved.reasoning) == (4000, "low")


# ─── and the column reaches the review ───────────────────────────────


def _policy_db(tmp_path, **columns):
    """A sqlite database holding one `repo_review_policies` row."""
    from src.db.models import RepoReviewPolicy

    url = f"sqlite:///{tmp_path}/policies.db"
    engine = sa.create_engine(url)
    RepoReviewPolicy.__table__.create(engine)
    with Session(engine) as session:
        session.add(RepoReviewPolicy(
            repo_slug="acme/api", workspace_id="default", enabled=True,
            prompt_template="", target_branches=[], folder_rules=[],
            agent_prompt_overrides={}, mcp_sources=[], disabled_agents=[],
            **columns,
        ))
        session.commit()
    engine.dispose()
    return url


def test_the_column_reaches_the_policy_the_orchestrator_reviews_with(
    tmp_path, monkeypatch,
):
    """`_load_policy` is the only thing that reads the row, so it is the only
    place the new column can be forgotten."""
    from src.review.orchestrator import ReviewOrchestrator

    monkeypatch.setenv("DATABASE_URL", _policy_db(
        tmp_path,
        architect_model="gpt-4o",
        agent_llm_overrides={"defect": {"max_output_tokens": 40000,
                                           "reasoning": "high"}},
    ))

    policy = ReviewOrchestrator()._load_policy("acme/api")

    assert policy is not None
    assert policy["agents"] == {
        "defect": {"max_output_tokens": 40000, "reasoning": "high"},
    }
    assert policy["architect_model"] == "gpt-4o"


def test_a_row_written_before_the_column_existed_reads_back_as_inherit(
    tmp_path, monkeypatch,
):
    """Every policy in every existing installation has NULL here.

    NULL has to mean exactly what an absent key means everywhere else in this
    chain — inherit — and not "override with nothing", which would hand every
    agent of every repo a ceiling nobody chose.
    """
    from src.review.orchestrator import ReviewOrchestrator

    monkeypatch.setenv("DATABASE_URL", _policy_db(tmp_path))  # column NULL

    policy = ReviewOrchestrator()._load_policy("acme/api")

    assert policy is not None
    assert policy["agents"] == {}
    resolved = resolve_agent_llm(
        "defect", policy=policy, workspace_cfg=WORKSPACE,
        settings=ReviewSettings(agent_max_output_tokens=12345),
    )
    assert (resolved.max_output_tokens, resolved.reasoning) == (20000, "medium")


def test_the_orchestrator_hands_each_agent_the_policy_ceiling(monkeypatch):
    """End to end through the function that builds the per-agent map.

    `_build_llm_client` is what every agent's budget actually comes from, and
    it is where a resolver that answers correctly in isolation stops being
    enough.
    """
    from src.api.routers import llm as llm_router
    from src.review.orchestrator import ReviewOrchestrator

    monkeypatch.setattr(
        llm_router, "_load_workspace_config",
        lambda workspace_id="default": WORKSPACE,
    )

    _client, by_agent = ReviewOrchestrator()._build_llm_client(
        "u", "ws-test",
        policy=_policy(
            defect={"max_output_tokens": 40000, "reasoning": "high"},
            compliance={"max_output_tokens": 4000},
        ),
    )

    assert by_agent["defect"].max_output_tokens == 40000
    assert by_agent["defect"].reasoning == "high"
    assert by_agent["compliance"].max_output_tokens == 4000
    # An agent the policy said nothing about keeps what it inherits: no entry
    # at either layer for `security`, so the chain runs to its floor. Asked of
    # the same function the floor comes from, because that number is an env
    # setting and a literal here would be about the machine, not the chain.
    assert by_agent["security"].max_output_tokens == default_agent_max_output_tokens(
        "security",
    )
    assert by_agent["security"].reasoning is None
    assert by_agent["verifier"].reasoning is None
