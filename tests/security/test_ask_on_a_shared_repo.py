"""`ask` over a repository another workspace shares: only when its owner said so, and then
only as far as the owner's rules go, at the asker's expense.

A question sends pieces of the owner's code to the asker's language-model provider and is
billed to the asker's workspace. So the owner decides, per share (`allow_ask`, off unless
the owner turned it on), and every part of the answer's path is checked again on the call:

  * closed share: a holder who names it is told that asking is not enabled for it (they
    see it in `repos`), and anybody who cannot read it gets the answer for a repository
    that is not there, word for word; nothing is asked, searched or billed either way, and
    refusing is not a read in the owner's view; with no names the share is left out without
    a word;
  * open share: the answer may use its code and notes; the asker's workspace pays and its
    key and provider are used; the owner's hidden paths, the share's own and the secret floor
    never reach the prompt; the owner's vectors are read with the owner's tenant filter in a
    query of their own and the asker's with theirs; no query is made without a tenant, even
    for a global administrator's token, whose OWN repositories alone also read the notes that
    belong to no tenant (what an upgrade must not take away from them);
  * nothing is remembered between people: a second asker is answered afresh, and a person
    whose share ended in between gets the answer for a repository that is not there.

World: `tests/security/mcp_world.py` + `share_world.py`, in both deployment modes. The model,
the embedding and the budget gate are recorders; the vector store is the real in-memory
engine behind a wrapper that writes down every filter it is asked with.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid

import pytest

from tests.security.mcp_world import (
    FOREIGN,
    GHOST,
    SHARED,
    SHARED_MARKER,
    WILD,
    WS_A,
    WS_B,
    set_env,
)
from tests.security.qdrant_recorder import RecordingQdrant
from tests.security.share_world import end_share, holder_token, make_share

DIM = 8
VEC = [1.0] + [0.0] * (DIM - 1)
QUESTION = "what do handler, helper and load_key do?"

#: Each marker is the content of one vault note, so a leak is a substring of the prompt.
WILD_NOTE = "WILD_NOTE_MARKER_11"
SHARED_NOTE = "SHARED_SRC_NOTE_22"
CONFIG_NOTE = "SHARED_CONFIG_NOTE_33"
UTIL_NOTE = "SHARED_UTIL_NOTE_44"
ENV_NOTE = "SHARED_ENV_NOTE_55"
FOREIGN_NOTE = "FOREIGN_NOTE_66"
#: A note of the shared repository that carries no tenant: nobody asks for it under the owner's tenant.
LEGACY_NOTE = "UNATTRIBUTED_NOTE_77"
#: The same for an own repository: only a global administrator's token still reads it.
OWN_LEGACY_NOTE = "UNATTRIBUTED_OWN_NOTE_88"
#: A note stamped for ANOTHER workspace under the slug of an own repository (a slug two
#: workspaces registered): no token of the asker reads it, a global administrator's included.
CLASH_NOTE = "OTHER_TENANT_SAME_SLUG_99"


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    # The asker's membership is checked again by the verb, which opens the database by URL.
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{mcp_world.engine.url.database}")
    return mcp_world


class Pipeline:
    def __init__(self) -> None:
        self.qdrant: RecordingQdrant
        self.prompts: list[str] = []
        self.system_instructions: list[str | None] = []
        self.files_sent: list[list[str]] = []
        self.model_workspaces: list[str] = []
        self.embed_workspaces: list[str] = []
        self.charged: list[str] = []

    @property
    def prompt(self) -> str:
        assert self.prompts, "the model was never asked"
        return self.prompts[-1]


def _note(repo: str, workspace: str | None, note_path: str, path: str, content: str):
    from qdrant_client import models

    payload = {"repo": repo, "note_path": note_path, "type": "module", "path": path,
               "module": note_path, "content": content, "symbols": [], "keywords": [],
               "cross_refs": []}
    if workspace is not None:
        payload["workspace_id"] = workspace
    return models.PointStruct(id=str(uuid.uuid4()), vector=list(VEC), payload=payload)


@pytest.fixture
def pipeline(world, monkeypatch):
    """The real Q&A pipeline with a recording model, embedding, budget gate and vector store."""
    from qdrant_client import QdrantClient, models

    from src.config import get_settings
    from src.mcp_server.dev_profile import tools_ask

    settings = get_settings()
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=settings.qdrant_collection,
        vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE))
    client.upsert(collection_name=settings.qdrant_collection, points=[
        _note(WILD, WS_A, "modules/wild.md", "src", WILD_NOTE),
        _note(SHARED, WS_B, "modules/src.md", "src", SHARED_NOTE),
        _note(SHARED, WS_B, "modules/cfg.md", "config/settings.yaml", CONFIG_NOTE),
        _note(SHARED, WS_B, "modules/util.md", "src/util.py", UTIL_NOTE),
        _note(SHARED, WS_B, "modules/env.md", ".env", ENV_NOTE),
        _note(FOREIGN, WS_B, "modules/foreign.md", "src", FOREIGN_NOTE),
        # A point that belongs to nobody: only a scope with no tenant condition sees it.
        _note(WILD, None, "modules/legacy.md", "src", OWN_LEGACY_NOTE),
        _note(SHARED, None, "modules/legacy-shared.md", "src", LEGACY_NOTE),
    ])
    p = Pipeline()
    p.qdrant = RecordingQdrant(client, settings.qdrant_collection)

    async def stream_chat(*, prompt, workspace_id=None, system_instruction=None,
                          files_sent=None, **_kw):
        p.prompts.append(prompt)
        p.system_instructions.append(system_instruction)
        p.files_sent.append(list(files_sent or []))
        p.model_workspaces.append(workspace_id)
        yield "an answer"

    def embed(_text, *_a, workspace_id=None, **_kw):
        p.embed_workspaces.append(workspace_id)
        return list(VEC)

    monkeypatch.setattr("src.retrieval.tier1_vault._build_qdrant_client",
                        lambda *_a, **_k: p.qdrant)
    monkeypatch.setattr("src.llm.completion.stream_chat", stream_chat)
    monkeypatch.setattr("src.llm.completion.embed", embed)
    monkeypatch.setattr("src.llm.budget.enforce", lambda ws: p.charged.append(ws))
    tools_ask._calls.clear()
    yield p
    tools_ask._calls.clear()
    client.close()


def _ask(question: str = QUESTION, repos: list[str] | None = None) -> str:
    """One `ask` call, inside a call record as the MCP envelope opens it."""
    from src.mcp_server import callctx
    from src.mcp_server.dev_profile import tools_ask

    _ask.record = callctx.begin("ask", "dev")
    try:
        return asyncio.run(tools_ask.run(question, repos))
    finally:
        callctx.reset()


def _blank(text: str, slug: str) -> str:
    """The answer with the asked slug blanked, and the suggestions in one order."""
    def ordered(m: re.Match) -> str:
        return "similar: " + ", ".join(sorted(m.group(1).split(", ")))

    return re.sub(r"similar: (.*)", ordered, text.replace(slug, "<slug>"))


def _holder(world, *, who: str = "mn", **share) -> tuple[str, str]:
    """(bearer, share id): a person with a token for a new share and one own repository."""
    share_id = make_share(world, **share)
    bearer, _view = holder_token(world, [share_id], own=[WILD], who=who)
    return bearer, share_id


def _nothing_was_asked(pipeline: Pipeline) -> None:
    assert pipeline.qdrant.filters == [], "the vector store was queried"
    assert pipeline.prompts == [] and pipeline.charged == []
    assert pipeline.embed_workspaces == [], "the question was embedded (and paid for)"


# ─── a closed share ──────────────────────────────────────────────────


CLOSED = f'repo "{SHARED}": ask is not enabled for this shared repository'


def test_naming_a_share_whose_owner_did_not_allow_questions_tells_the_holder_so(
        world, pipeline):
    """The holder lists the repository in `repos`; "repo not found" would send them in
    circles. The sentence says what is off and not who decides or what the share hides."""
    bearer, _ = _holder(world)
    world.as_(bearer)
    closed = _ask(repos=[SHARED])
    assert closed.splitlines()[1] == CLOSED
    assert "similar:" not in closed, "the refused repository is not offered back"
    assert "not accessible" not in closed
    _nothing_was_asked(pipeline)


def test_refusing_a_question_about_a_closed_share_is_not_a_read_of_it(world, pipeline):
    """The owner's view of who read the share is made from the call log."""
    bearer, _ = _holder(world)
    world.as_(bearer)
    _ask(repos=[SHARED])
    assert _ask.record.shares == set() and SHARED not in repr(_ask.record.repos)


def test_a_loosely_spelled_name_of_a_closed_share_is_told_the_same(world, pipeline):
    bearer, _ = _holder(world)
    world.as_(bearer)
    loose = SHARED.split("_", 1)[-1]                    # "bco-shared"
    assert _ask(repos=[loose]).splitlines()[1] == f'repo "{loose}": ask is not enabled ' \
        "for this shared repository"
    _nothing_was_asked(pipeline)


def test_a_closed_share_beside_an_own_repository_stops_the_whole_call(world, pipeline):
    bearer, _ = _holder(world)
    world.as_(bearer)
    mixed = _ask(repos=[WILD, SHARED])
    assert mixed.splitlines()[1] == CLOSED, "the refused name is the whole answer"
    _nothing_was_asked(pipeline)


def test_without_names_a_closed_share_is_left_out_without_a_word(world, pipeline):
    bearer, _ = _holder(world)
    world.as_(bearer)
    out = _ask()
    assert "an answer" in out
    assert pipeline.qdrant.asked() == {WS_A: [WILD]}, "only the caller's own repository"
    assert SHARED_MARKER not in pipeline.prompt and SHARED_NOTE not in pipeline.prompt
    assert "closed" not in out and "allow" not in out


def test_a_closed_share_does_not_count_towards_the_limit_of_repositories_without_names(
        world, pipeline):
    from src.mcp_server.dev_profile import tools_ask

    bearer, _ = _holder(world)
    world.as_(bearer)
    old, tools_ask.MAX_REPOS = tools_ask.MAX_REPOS, 1
    try:
        assert "an answer" in _ask(), "one own repository and one closed share fit a limit of one"
    finally:
        tools_ask.MAX_REPOS = old


def test_a_person_with_only_a_closed_share_has_nothing_to_ask_about(world, pipeline):
    share_id = make_share(world)
    world.as_(holder_token(world, [share_id])[0])
    out = _ask()
    assert "no repositories you can read" in out and SHARED not in out
    _nothing_was_asked(pipeline)


def test_switching_asking_off_applies_to_the_next_question(world, pipeline):
    from src.access import shares

    bearer, share_id = _holder(world, allow_ask=True)
    world.as_(bearer)
    assert "an answer" in _ask(repos=[SHARED])
    with world.session() as s:
        shares.narrow(s, share_id, allow_ask=False)
        s.commit()
    pipeline.prompts.clear()
    after = _ask(repos=[SHARED])
    assert after.splitlines()[1] == CLOSED and pipeline.prompts == []


# ─── somebody who does not hold the share ────────────────────────────


def test_somebody_without_the_share_gets_the_answer_for_a_missing_repository(world, pipeline):
    make_share(world, allow_ask=True)                    # live, open, on nobody's token
    world.as_(world.issue("mn", ["*"], kind="pat", profile="dev")[0])
    assert (_blank(_ask(repos=[SHARED]), SHARED) == _blank(_ask(repos=[GHOST]), GHOST))
    _nothing_was_asked(pipeline)


def test_a_holder_of_another_share_gets_the_answer_for_a_missing_repository(world, pipeline):
    other = make_share(world, slug=FOREIGN, allow_ask=True)
    make_share(world, allow_ask=True)
    world.as_(holder_token(world, [other], own=[WILD])[0])
    assert (_blank(_ask(repos=[SHARED]), SHARED) == _blank(_ask(repos=[GHOST]), GHOST))
    _nothing_was_asked(pipeline)


@pytest.mark.parametrize("how", ["blocked", "revoked", "expired", "other_workspace",
                                 "registry_changed"])
def test_a_share_that_stopped_holding_answers_like_a_missing_repository(world, pipeline, how):
    from tests.security.share_world import change_share

    bearer, share_id = _holder(world, allow_ask=True)
    world.as_(bearer)
    assert "an answer" in _ask(repos=[SHARED])           # the token and its cache are warm
    pipeline.prompts.clear()
    if how == "blocked":
        change_share(world, share_id, blocked_user_ids=[world.users["mn"]])
    elif how == "revoked":
        end_share(world, share_id)
    elif how == "expired":
        from datetime import UTC, datetime, timedelta

        change_share(world, share_id, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    elif how == "other_workspace":
        change_share(world, share_id, grantee_workspace_id="wsid-elsewhere")
    else:
        change_share(world, share_id, repo_full_name="someone/else")
    assert (_blank(_ask(repos=[SHARED]), SHARED) == _blank(_ask(repos=[GHOST]), GHOST))
    assert pipeline.prompts == [], "no model call for a share that no longer holds"


# ─── an open share ───────────────────────────────────────────────────


def test_an_open_share_is_answered_from_its_code_and_notes(world, pipeline):
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    out = _ask(repos=[SHARED])
    assert "an answer" in out
    assert SHARED_MARKER in pipeline.prompt, "the code of the shared repository"
    assert SHARED_NOTE in pipeline.prompt, "the vault note of the shared repository"
    assert FOREIGN_NOTE not in pipeline.prompt, "the owner's other repository is not shared"
    assert "note: text from a shared repository" in out


def test_the_asker_pays_and_the_owner_does_not(world, pipeline):
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[SHARED])
    assert pipeline.charged == [WS_A], "the budget of the asking workspace"
    assert set(pipeline.model_workspaces) == {WS_A}, "the asking workspace's key and provider"
    assert set(pipeline.embed_workspaces) == {WS_A}
    assert WS_B not in pipeline.charged + pipeline.model_workspaces + pipeline.embed_workspaces


def test_a_spent_budget_stops_a_question_about_a_share_before_anything_is_read(
        world, pipeline, monkeypatch):
    from src.llm.budget import BudgetExceeded

    def broke(ws):
        pipeline.charged.append(ws)
        raise BudgetExceeded(ws, 10.0, 10.0)

    monkeypatch.setattr("src.llm.budget.enforce", broke)
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    out = _ask(repos=[SHARED])
    assert "budget is used up" in out and WS_A in out
    assert pipeline.charged == [WS_A] and pipeline.prompts == [] and pipeline.qdrant.filters == []


def test_the_owners_vectors_are_read_with_the_owners_tenant_and_the_askers_with_theirs(
        world, pipeline):
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[WILD, SHARED])
    assert pipeline.qdrant.asked() == {WS_A: [WILD], WS_B: [SHARED]}
    assert len(pipeline.qdrant.filters) == 2, "one query per tenant that holds the data"
    assert all(f["conditions"] == 2 and f["workspace"] for f in pipeline.qdrant.filters), (
        "each query: the tenant AND the repositories, never one without a tenant")
    assert WILD_NOTE in pipeline.prompt and SHARED_NOTE in pipeline.prompt
    assert LEGACY_NOTE not in pipeline.prompt, "a point that belongs to nobody"
    assert OWN_LEGACY_NOTE not in pipeline.prompt, "nor for a member: a global administrator's only"


def test_a_question_about_the_share_alone_asks_only_the_owners_tenant(world, pipeline):
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[SHARED])
    assert pipeline.qdrant.asked() == {WS_B: [SHARED]}
    assert WILD_NOTE not in pipeline.prompt


def test_without_names_an_open_share_is_part_of_every_repository_you_can_read(world, pipeline):
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask()
    assert pipeline.qdrant.asked() == {WS_A: [WILD], WS_B: [SHARED]}


def test_an_open_share_counts_towards_the_limit_of_repositories_without_names(world, pipeline):
    from src.mcp_server.dev_profile import tools_ask

    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    old, tools_ask.MAX_REPOS = tools_ask.MAX_REPOS, 1
    try:
        out = _ask()
    finally:
        tools_ask.MAX_REPOS = old
    assert "name at most 1" in out
    _nothing_was_asked(pipeline)


def test_the_merged_notes_are_cut_to_the_top_k_a_single_query_would_return(
        world, pipeline, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "retrieval_vector_topk", 2, raising=False)
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[WILD, SHARED])
    notes = [n for n in (WILD_NOTE, SHARED_NOTE, CONFIG_NOTE, UTIL_NOTE) if n in pipeline.prompt]
    assert len(notes) <= 2, notes


# ─── what the prompt never holds ─────────────────────────────────────


def test_a_question_without_any_hidden_path_does_read_the_files_the_next_tests_hide(
        world, pipeline):
    """The control: these notes and files are what the question reaches when nothing hides them."""
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[SHARED])
    assert CONFIG_NOTE in pipeline.prompt and UTIL_NOTE in pipeline.prompt
    assert "def helper" in pipeline.prompt


def test_what_the_owner_and_the_share_hide_never_reaches_the_prompt(world, pipeline):
    from src.db.models import RepoAccessRule

    with world.session() as s:
        s.add(RepoAccessRule(id="rule-b", workspace_id=WS_B, team_id="team-b",
                             repo_slug=SHARED, visibility="code", deny_globs=["config/**"]))
        s.commit()
    bearer, _ = _holder(world, allow_ask=True, deny_globs=["src/util.py"])
    world.as_(bearer)
    out = _ask(repos=[SHARED])
    prompt = pipeline.prompt
    assert SHARED_MARKER in prompt and SHARED_NOTE in prompt, "the rest is still read"
    assert CONFIG_NOTE not in prompt, "the owner's rule hides the note"
    assert UTIL_NOTE not in prompt and "def helper" not in prompt, "the share's own list"
    assert "util.py" not in out and "settings.yaml" not in out


def test_a_secret_file_of_a_shared_repository_never_reaches_the_prompt(world, pipeline):
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    out = _ask(repos=[SHARED])
    for leaked in (ENV_NOTE, "PLACEHOLDER_SETTING", "not-a-real-value", "return 3",
                   "def load_key"):
        assert leaked not in pipeline.prompt, leaked
    assert ".env" not in out and "secrets/keys.py" not in out, "not even as a source"


def test_a_rule_the_owner_adds_hides_the_path_from_the_next_question(world, pipeline):
    from src.db.models import RepoAccessRule

    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[SHARED])
    assert UTIL_NOTE in pipeline.prompt
    with world.session() as s:
        s.add(RepoAccessRule(id="rule-late", workspace_id=WS_B, team_id="team-b",
                             repo_slug=SHARED, visibility="code", deny_globs=["src/util.py"]))
        s.commit()
    _ask(repos=[SHARED])
    assert UTIL_NOTE not in pipeline.prompt and "def helper" not in pipeline.prompt


# ─── nothing is remembered between people ────────────────────────────


def test_a_second_asker_is_answered_afresh_not_from_the_first(world, pipeline):
    first, share_id = _holder(world, allow_ask=True)
    second, _ = holder_token(world, [share_id], own=[WILD], who="mg")
    world.as_(first)
    _ask(repos=[SHARED])
    world.as_(second)
    _ask(repos=[SHARED])
    assert len(pipeline.prompts) == 2 and len(pipeline.qdrant.filters) == 2
    assert pipeline.charged == [WS_A, WS_A], "each question is billed"


def test_somebody_without_the_share_never_gets_an_answer_the_holder_got(world, pipeline):
    holder, _ = _holder(world, allow_ask=True)
    world.as_(holder)
    assert "an answer" in _ask(repos=[SHARED])
    calls = len(pipeline.prompts)
    world.as_(world.issue("mg", ["*"], kind="pat", profile="dev")[0])
    out = _ask(repos=[SHARED])
    assert _blank(out, SHARED) == _blank(_ask(repos=[GHOST]), GHOST)
    assert len(pipeline.prompts) == calls, "the same question, and no model call"


def test_the_answer_path_has_no_cache(world):
    """A cache keyed by anything but the whole of who is asking would hand one person's
    answer, with the owner's code in it, to another. There is none; keep it so."""
    from src.api.routers import qa
    from src.automation import actions_reviews
    from src.mcp_server.dev_profile import tools_ask
    from src.qa import multi_repo_retriever

    for fn in (qa._generate_full, actions_reviews.ask_code, tools_ask.run,
               multi_repo_retriever.MultiRepoRetriever.retrieve,
               multi_repo_retriever.MultiRepoRetriever._vault_hits):
        source = inspect.getsource(fn)
        assert not re.search(r"lru_cache|functools\.cache|@cache|_cache\b|memoi[sz]e", source), (
            f"{fn.__qualname__} caches")


# ─── a token never searches the whole installation ───────────────────


def test_a_global_administrators_token_asks_with_a_tenant_like_every_other(world, pipeline):
    from src.db.models import WorkspaceMember

    with world.session() as s:
        s.add(WorkspaceMember(workspace_id=WS_A, user_id=world.users["su"], role="member"))
        s.commit()
    bearer, _ = _holder(world, who="su", allow_ask=True)
    world.as_(bearer)
    _ask(repos=[WILD, SHARED])
    assert pipeline.qdrant.asked() == {WS_A: [WILD], WS_B: [SHARED]}
    assert all(f["workspace"] for f in pipeline.qdrant.filters), "no query without a tenant"
    assert LEGACY_NOTE not in pipeline.prompt, "the share's points that belong to nobody are not hers"
    assert OWN_LEGACY_NOTE in pipeline.prompt, "her own repository's still are"


def test_a_global_administrators_token_asking_about_its_own_repository_uses_the_tenant(
        world, pipeline):
    from src.db.models import WorkspaceMember

    with world.session() as s:
        s.add(WorkspaceMember(workspace_id=WS_A, user_id=world.users["su"], role="member"))
        s.commit()
    world.as_(world.issue("su", [WILD], kind="pat", profile="dev")[0])
    _ask(repos=[WILD])
    assert pipeline.qdrant.asked() == {WS_A: [WILD]}
    assert WILD_NOTE in pipeline.prompt
    assert LEGACY_NOTE not in pipeline.prompt


def test_a_persons_own_session_keeps_its_scope(world, pipeline, monkeypatch):
    """Only tokens changed: a global administrator in the web interface still reads the whole
    installation (the operator's own view), and a member reads the tenant of the workspace."""
    import src.qa.multi_repo_retriever as mrr
    from src.access.resolver import RepoAccessDecision

    monkeypatch.setattr(mrr, "resolve_access",
                        lambda **kw: {r: RepoAccessDecision.full(r) for r in kw["repos"]})

    def retrieve(who: str, *, is_admin: bool):
        pipeline.qdrant.filters.clear()
        asyncio.run(mrr.MultiRepoRetriever().retrieve(
            question=QUESTION, repos=[WILD], user_id=world.users[who], is_admin=is_admin,
            workspace_id=WS_A))
        return pipeline.qdrant.filters[0]["workspace"]

    assert retrieve("su", is_admin=True) is None, "the operator's installation-wide scope"
    assert retrieve("mn", is_admin=False) == WS_A


# ─── notes that belong to no tenant, for a global administrator's token ───


def _su_in_a(world) -> None:
    from src.db.models import WorkspaceMember

    with world.session() as s:
        s.add(WorkspaceMember(workspace_id=WS_A, user_id=world.users["su"], role="member"))
        s.commit()


def _also_stamped_for_b(pipeline, repo: str) -> None:
    """Another workspace's note under the slug of an own repository."""
    pipeline.qdrant._client.upsert(
        collection_name=pipeline.qdrant.collection,
        points=[_note(repo, WS_B, "modules/clash.md", "src", CLASH_NOTE)])


def test_a_global_administrators_token_still_reads_the_unstamped_notes_of_its_own_repository(
        world, pipeline):
    """Notes written before notes were stamped have no tenant. In 3.0.2 the token of a global
    administrator read them (it asked the whole installation); a tenant filter alone would
    silently empty that part of every answer after the upgrade."""
    _su_in_a(world)
    world.as_(world.issue("su", [WILD], kind="pat", profile="dev")[0])
    _ask(repos=[WILD])
    assert WILD_NOTE in pipeline.prompt and OWN_LEGACY_NOTE in pipeline.prompt
    assert [(f["workspace"], f["unattributed"]) for f in pipeline.qdrant.filters] == [(WS_A, True)]


def test_that_widening_never_reaches_another_workspaces_notes_under_the_same_slug(
        world, pipeline):
    _su_in_a(world)
    _also_stamped_for_b(pipeline, WILD)
    world.as_(world.issue("su", [WILD], kind="pat", profile="dev")[0])
    _ask(repos=[WILD])
    assert OWN_LEGACY_NOTE in pipeline.prompt, "the widening is on"
    assert CLASH_NOTE not in pipeline.prompt, "another tenant's stamp is still out"


def test_a_shared_repository_is_asked_under_its_owners_tenant_alone_even_for_a_global_administrator(
        world, pipeline):
    _su_in_a(world)
    bearer, _ = _holder(world, who="su", allow_ask=True)
    world.as_(bearer)
    _ask(repos=[SHARED])
    assert pipeline.qdrant.asked() == {WS_B: [SHARED]}
    assert [f["unattributed"] for f in pipeline.qdrant.filters] == [False]
    assert SHARED_NOTE in pipeline.prompt and LEGACY_NOTE not in pipeline.prompt


def test_an_own_and_a_shared_repository_widen_only_the_own_query(world, pipeline):
    _su_in_a(world)
    bearer, _ = _holder(world, who="su", allow_ask=True)
    world.as_(bearer)
    _ask(repos=[WILD, SHARED])
    assert {f["workspace"]: f["unattributed"] for f in pipeline.qdrant.filters} == {
        WS_A: True, WS_B: False}
    assert OWN_LEGACY_NOTE in pipeline.prompt and LEGACY_NOTE not in pipeline.prompt


@pytest.mark.parametrize("who", ["mn", "admin"])
def test_a_token_of_anybody_who_is_not_a_global_administrator_never_reads_unstamped_notes(
        world, pipeline, who):
    """Only what 3.0.2 gave a global administrator comes back; everyone else was held to the
    tenant then and still is."""
    world.as_(world.issue(who, [WILD], kind="pat", profile="dev")[0])
    _ask(repos=[WILD])
    assert pipeline.qdrant.asked() == {WS_A: [WILD]}
    assert [f["unattributed"] for f in pipeline.qdrant.filters] == [False]
    assert OWN_LEGACY_NOTE not in pipeline.prompt and LEGACY_NOTE not in pipeline.prompt


def _retrieve_in_default(world, monkeypatch, *, is_admin: bool, filter_=(WILD,)):
    """The retriever for a token of the built-in ``default`` workspace, whose notes carry no
    tenant because nothing may be stamped with that name."""
    from src.access import effective
    from src.access.resolver import RepoAccessDecision
    from src.qa.multi_repo_retriever import MultiRepoRetriever

    monkeypatch.setattr(effective, "effective_access", lambda _p, _ws, repos: {
        r: RepoAccessDecision.full(r) for r in repos})
    return asyncio.run(MultiRepoRetriever().retrieve(
        question=QUESTION, repos=[WILD], user_id=world.users["su" if is_admin else "mn"],
        is_admin=is_admin, workspace_id="default", token_filter=filter_,
        name_free_notice=True, include_code=False))


def test_the_notes_of_the_built_in_default_workspace_stay_readable_to_a_global_administrators_token(
        world, pipeline, monkeypatch):
    """Its notes are written without a tenant, and a scope for that workspace matches nothing:
    the token of the installation's administrator, who read them in 3.0.2, would find none."""
    _also_stamped_for_b(pipeline, WILD)
    ctx = _retrieve_in_default(world, monkeypatch, is_admin=True)
    assert OWN_LEGACY_NOTE in ctx.prompt, "the unstamped note of the repository"
    assert CLASH_NOTE not in ctx.prompt and WILD_NOTE not in ctx.prompt, "no tenant's stamp"
    assert [f["unattributed"] for f in pipeline.qdrant.filters] == [True]


def test_the_token_of_somebody_else_in_the_default_workspace_reads_no_notes_as_before(
        world, pipeline, monkeypatch):
    ctx = _retrieve_in_default(world, monkeypatch, is_admin=False)
    assert OWN_LEGACY_NOTE not in ctx.prompt and WILD_NOTE not in ctx.prompt
    assert [f["unattributed"] for f in pipeline.qdrant.filters] == [False]


# ─── the record of the call ──────────────────────────────────────────


def test_the_call_log_names_the_share_by_id_and_never_by_slug(world, pipeline):
    bearer, share_id = _holder(world, allow_ask=True)
    world.as_(bearer)
    _ask(repos=[SHARED])
    rec = _ask.record
    assert share_id in rec.shares
    assert SHARED not in rec.repos and not any("bco" in r for r in rec.repos)


# ─── the pieces ──────────────────────────────────────────────────────


def test_the_ask_actor_carries_the_shares_of_the_token_and_a_list_without_them(world):
    from src.mcp_server.dev_profile import tools_ask

    bearer, share_id = _holder(world)
    world.as_(bearer)
    actor = tools_ask._actor()
    assert actor.token_shares == (share_id,) and tuple(actor.token_filter) == (WILD,)
    assert share_id not in repr(actor.token_filter)


def test_ask_code_refuses_a_closed_share_by_name_with_the_text_of_a_missing_repository(world):
    """The second line behind the tool: the verb itself, called with a name."""
    from src.automation.actions import ActionError
    from src.automation.actions_reviews import ask_code
    from src.mcp_server.dev_profile import tools_ask

    bearer, _ = _holder(world)
    world.as_(bearer)
    actor = tools_ask._actor()
    with pytest.raises(ActionError) as closed:
        asyncio.run(ask_code(actor, None, question="how?", repo_slugs=[SHARED]))
    with pytest.raises(ActionError) as ghost:
        asyncio.run(ask_code(actor, None, question="how?", repo_slugs=[GHOST]))
    assert str(closed.value).replace(SHARED, "<slug>") == str(ghost.value).replace(GHOST, "<slug>")


def test_a_person_asking_with_their_own_rights_has_no_shares_to_ask_about(world):
    from src.automation.actions import ActionError, Actor
    from src.automation.actions_reviews import ask_code

    share_id = make_share(world, allow_ask=True)
    actor = Actor(user_id=world.users["admin"], email="admin@acme.io", workspace_id=WS_A,
                  label="agent", token_filter=None, token_shares=(share_id,))
    with pytest.raises(ActionError, match="Not registered in this workspace"):
        asyncio.run(ask_code(actor, None, question="how?", repo_slugs=[SHARED]))


def _retrieve(world, *, repos, shares, filter_=(WILD,)):
    from src.qa.multi_repo_retriever import MultiRepoRetriever

    return asyncio.run(MultiRepoRetriever().retrieve(
        question=QUESTION, repos=repos, user_id=world.users["mn"], is_admin=False,
        workspace_id=WS_A, token_filter=filter_, name_free_notice=True, token_shares=shares))


def test_the_retriever_itself_reads_nothing_of_a_share_its_owner_closed(world, pipeline):
    """The second line, under the verb: the retrieval computes the decision again and
    lets only a share that allows questions through."""
    share_id = make_share(world)
    ctx = _retrieve(world, repos=[SHARED], shares=(share_id,))
    assert pipeline.qdrant.filters == [] and ctx.files_read == []
    assert SHARED_MARKER not in ctx.prompt and SHARED_NOTE not in ctx.prompt


def test_the_retriever_reads_an_open_share_only_when_the_token_carries_it(world, pipeline):
    share_id = make_share(world, allow_ask=True)
    bare = _retrieve(world, repos=[SHARED], shares=())
    assert bare.files_read == [] and pipeline.qdrant.filters == [], "a token without the share"
    full = _retrieve(world, repos=[SHARED], shares=(share_id,))
    assert any(f.startswith(f"{SHARED}/") for f in full.files_read)
    assert pipeline.qdrant.asked() == {WS_B: [SHARED]}


def test_the_retriever_does_not_take_a_share_for_a_glob_of_the_token(world, pipeline):
    share_id = make_share(world, allow_ask=True)
    ctx = _retrieve(world, repos=[SHARED], shares=(share_id,), filter_=("*",))
    assert any(f.startswith(f"{SHARED}/") for f in ctx.files_read), "the share, by its own id"
    other = _retrieve(world, repos=[SHARED], shares=(), filter_=("*",))
    assert other.files_read == [], "a star is not a share"


def test_the_streaming_route_cannot_carry_shares():
    """A person's chat in the web interface answers from the workspace's own registry."""
    from src.api.routers import qa

    assert "token_shares" not in inspect.signature(qa._ask_stream).parameters
    assert "token_shares" not in inspect.getsource(qa._ask_stream)


# ─── what the asker's own side keeps of the question ─────────────────


def test_what_the_provider_is_told_was_sent_names_nothing_of_the_owners_repository(
        world, pipeline):
    """`files_sent` becomes a record of the asker's WORKSPACE audit trail, which that
    workspace's administrators read (`GET /api/audit`) whether or not they hold the
    share. The holder sees the files in the answer; nobody else on the receiving side may
    learn from the trail that the repository exists, its slug or one of its paths. The
    trail still says that code of a shared repository was sent, and how many files."""
    bearer, _ = _holder(world, allow_ask=True)
    world.as_(bearer)
    out = _ask(repos=[SHARED])
    assert "def helper" in pipeline.prompt, "the question did read the shared code"
    assert pipeline.files_sent[-1] == ["(shared repository: 2 files)"], pipeline.files_sent
    assert SHARED not in (pipeline.system_instructions[-1] or ""), "nor the system prompt"
    assert "github_bco-shared/src/app.py" in out, "the holder still reads the names"


def test_the_audit_line_for_a_shared_repository_leaves_the_askers_own_files_alone(
        world, pipeline):
    """A question over an own and a shared repository: the own files stay on the trail
    as they always were, the shared ones become the one line."""
    from src.api.routers.qa import _files_sent
    from src.qa.multi_repo_retriever import RetrievalContext

    ctx = RetrievalContext(
        prompt="", system_instruction="", vault_hits=[],
        files_read=[f"{WILD}/src/a.py", f"{SHARED}/src/app.py", f"{SHARED}/README.md"],
        shared_repos=(SHARED,))
    assert _files_sent(ctx) == [f"{WILD}/src/a.py", "(shared repository: 2 files)"]
    ctx.shared_repos = ()
    assert _files_sent(ctx) == ctx.files_read, "nothing shared, nothing changed"
    ctx.shared_repos = (SHARED,)
    ctx.files_read = [f"{SHARED}/only.py"]
    assert _files_sent(ctx) == ["(shared repository: 1 file)"]


def test_a_global_administrators_token_is_held_to_the_same_hidden_paths(world, pipeline):
    """Being a global administrator widens nothing on a share: the owner's rules, the
    share's own list and the secret floor hold for the token of one as for anybody."""
    from src.db.models import RepoAccessRule, WorkspaceMember

    with world.session() as s:
        s.add(WorkspaceMember(workspace_id=WS_A, user_id=world.users["su"], role="member"))
        s.add(RepoAccessRule(id="rule-b-admin", workspace_id=WS_B, team_id="team-b",
                             repo_slug=SHARED, visibility="code", deny_globs=["config/**"]))
        s.commit()
    bearer, _ = _holder(world, who="su", allow_ask=True, deny_globs=["src/util.py"])
    world.as_(bearer)
    _ask(repos=[SHARED])
    prompt = pipeline.prompt
    assert SHARED_MARKER in prompt and SHARED_NOTE in prompt, "the rest is still read"
    for hidden in (CONFIG_NOTE, UTIL_NOTE, ENV_NOTE, "def helper", "def load_key",
                   "PLACEHOLDER_SETTING", "util.py", "settings.yaml", "keys.py"):
        assert hidden not in prompt, hidden
    assert not [f for f in pipeline.files_sent[-1] if "util.py" in f or ".env" in f]
