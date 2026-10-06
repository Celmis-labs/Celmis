"""`memories.remember` — the one entry for "the team wants this remembered".

What is checked, each by behaviour (a SQLite file behind the real blocking
store, a fake model for the dedup call):

  * who is asking decides the status: a trusted commenter's memory is active
    at once, a stranger's waits for approval (and does not reach a review);
  * the same memory twice is one memory, and a trusted repeat approves a
    pending copy;
  * the model's dedup verdict (skip / update / create) is obeyed, an
    untrusted request never rewrites an active memory, and a model that is
    down or answers rubbish does not lose the memory;
  * a request the store refuses is a skip with the reason, never a raise.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.review import memories as store
from src.review.memories import ActorRef, SourceRef
from tests.review.rules_db import rules_db

WS = "ws-a"
REPO = "github_aco-app"
TRUSTED = ActorRef(provider="github", external_id="lead", display="lead", is_token_owner=True)
STRANGER = ActorRef(provider="github", external_id="stranger", display="stranger")
SOURCE = SourceRef(provider="github", repo="aco/app", pr_number=7, comment_id="c1",
                   url="https://example.test/pr/7#c1")


class FakeModel:
    """Answers the dedup call with a canned reply and records the prompts."""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list[dict] = []

    def generate(self, **kw):
        self.calls.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(text=self.reply)


@pytest.fixture(autouse=True)
def _no_user_store(monkeypatch):
    monkeypatch.setattr(store, "_user_by_email", lambda email: None)


def _rows(tmp_path):
    import sqlalchemy as sa

    with sa.create_engine(f"sqlite:///{tmp_path}/rules.db").connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT id, status, text, origin, created_by FROM review_memories ORDER BY id"))]


# ─── who is asking ───────────────────────────────────────────────────


async def test_a_remember_by_a_trusted_commenter_is_active_at_once(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        out = store.remember(WS, "Money is stored as integer cents.", repo_slug=REPO,
                             actor=TRUSTED, source=SOURCE, llm=FakeModel("{}"))
        assert out.action == "create"
        assert out.memory["status"] == "active"
        assert out.memory["source_url"] == SOURCE.url and out.memory["source_pr"] == 7
        assert [m["id"] for m in store.load_active_sync(WS, REPO)] == [out.memory["id"]]


async def test_a_remember_by_a_stranger_waits_for_approval(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        out = store.remember(WS, "We never log card numbers.", repo_slug=REPO,
                             actor=STRANGER, source=SOURCE, llm=FakeModel("{}"))
        assert out.action == "pending"
        assert out.memory["status"] == "pending"
        assert "approve" in out.reason
        assert store.load_active_sync(WS, REPO) == [], "pending memories reach no review"


async def test_a_listed_commenter_is_trusted_by_login_or_email(tmp_path, monkeypatch):
    from src.db.models import RepoReviewPolicy

    async with rules_db(tmp_path, monkeypatch) as factory:
        async with factory() as s:
            s.add(RepoReviewPolicy(repo_slug=REPO, workspace_id=WS,
                                   memory_trusted_commenters=["Jane", "dev@acme.io"]))
            await s.commit()
        by_login = ActorRef(provider="github", external_id="jane")
        by_mail = ActorRef(provider="github", external_id="x1", email="DEV@acme.io")
        assert store.remember(WS, "First fact here.", repo_slug=REPO, actor=by_login,
                              llm=FakeModel("{}")).action == "create"
        assert store.remember(WS, "Second fact there.", repo_slug=REPO, actor=by_mail,
                              llm=FakeModel("{}")).action == "create"
        assert store.remember(WS, "Third fact elsewhere.", repo_slug=REPO, actor=STRANGER,
                              llm=FakeModel("{}")).action == "pending"


async def test_a_workspace_editor_is_trusted_and_a_viewer_is_not(tmp_path, monkeypatch):
    from src.db.models import WorkspaceMember

    async with rules_db(tmp_path, monkeypatch) as factory:
        async with factory() as s:
            s.add_all([WorkspaceMember(workspace_id=WS, user_id="u-ed", role="editor"),
                       WorkspaceMember(workspace_id=WS, user_id="u-vw", role="viewer")])
            await s.commit()
        editor = ActorRef(provider="github", external_id="ed", user_id="u-ed")
        viewer = ActorRef(provider="github", external_id="vw", user_id="u-vw")
        assert store.remember(WS, "Editors teach quickly.", actor=editor,
                              llm=FakeModel("{}")).action == "create"
        assert store.remember(WS, "Viewers only suggest.", actor=viewer,
                              llm=FakeModel("{}")).action == "pending"


async def test_with_approval_switched_off_a_machine_proposal_is_active(tmp_path, monkeypatch):
    from src.db.models import WorkspaceReviewDefaults

    async with rules_db(tmp_path, monkeypatch) as factory:
        proposal = dict(actor=STRANGER, origin="agent", llm=FakeModel("{}"))
        assert store.remember(WS, "A reviewer noticed this.", **proposal).action == "pending"
        async with factory() as s:
            s.add(WorkspaceReviewDefaults(workspace_id=WS, knowledge_approval=False))
            await s.commit()
        assert store.remember(WS, "A reviewer noticed that.", **proposal).action == "create"


# ─── the same memory twice ───────────────────────────────────────────


async def test_the_same_memory_twice_is_one_memory(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        model = FakeModel("{}")
        first = store.remember(WS, "Money is stored as integer cents.", actor=TRUSTED, llm=model)
        again = store.remember(WS, "  money is stored as INTEGER cents ", actor=TRUSTED, llm=model)
        assert (first.action, again.action) == ("create", "skip")
        assert again.memory["id"] == first.memory["id"]
        assert len(_rows(tmp_path)) == 1
        assert model.calls == [], "an exact repeat is settled without asking the model"


async def test_a_repo_memory_already_covered_by_a_workspace_one_is_skipped(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        store.remember(WS, "Every endpoint is idempotent.", actor=TRUSTED, llm=FakeModel("{}"))
        out = store.remember(WS, "Every endpoint is idempotent.", repo_slug=REPO,
                             actor=TRUSTED, llm=FakeModel("{}"))
        assert out.action == "skip" and out.code == "covered"


async def test_a_trusted_repeat_approves_the_pending_copy(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        waiting = store.remember(WS, "Use UTC everywhere.", actor=STRANGER, llm=FakeModel("{}"))
        approved = store.remember(WS, "Use UTC everywhere.", actor=TRUSTED, llm=FakeModel("{}"))
        assert (waiting.action, approved.action) == ("pending", "update")
        assert approved.memory["id"] == waiting.memory["id"]
        assert approved.memory["status"] == "active"
        assert len(_rows(tmp_path)) == 1


async def test_a_memory_somebody_rejected_is_not_brought_back_by_a_stranger(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        first = store.remember(WS, "Tabs, not spaces.", actor=TRUSTED, llm=FakeModel("{}"))
        await store.set_status(WS, [first.memory["id"]], "rejected", "admin@acme.io")
        again = store.remember(WS, "Tabs, not spaces.", actor=STRANGER, llm=FakeModel("{}"))
        assert again.action == "skip" and again.code == "rejected_before"
        back = store.remember(WS, "Tabs, not spaces.", actor=TRUSTED, llm=FakeModel("{}"))
        assert back.action == "update" and back.memory["status"] == "active"


# ─── the model's say ─────────────────────────────────────────────────


async def test_a_memory_the_model_calls_a_duplicate_is_skipped(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        old = store.remember(WS, "Amounts are integer cents in the database.", actor=TRUSTED,
                             llm=FakeModel("{}"))
        reply = f'{{"action": "skip", "target_id": {old.memory["id"]}}}'
        model = FakeModel(reply)
        out = store.remember(WS, "We keep money as integer cents in the database.",
                             actor=TRUSTED, llm=model)
        assert out.action == "skip" and out.memory["id"] == old.memory["id"]
        assert len(model.calls) == 1
        call = model.calls[0]
        assert call["operation"] == "memory_dedup"
        assert "<existing_facts>" in call["prompt"] and "<new_fact>" in call["prompt"]
        assert len(_rows(tmp_path)) == 1


async def test_a_memory_the_model_merges_rewrites_the_old_one(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        old = store.remember(WS, "Amounts are integer cents.", actor=TRUSTED, llm=FakeModel("{}"))
        reply = json.dumps({"action": "update", "target_id": old.memory["id"],
                            "merged_text": "Amounts are integer cents; never floats."})
        out = store.remember(WS, "Never use floats for amounts, cents only.", actor=TRUSTED,
                             llm=FakeModel(reply))
        assert out.action == "update"
        assert out.memory["text"] == "Amounts are integer cents; never floats."
        rows = _rows(tmp_path)
        assert len(rows) == 1 and rows[0][2] == "Amounts are integer cents; never floats."


async def test_a_stranger_cannot_rewrite_an_active_memory(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        old = store.remember(WS, "Amounts are integer cents.", actor=TRUSTED, llm=FakeModel("{}"))
        reply = json.dumps({"action": "update", "target_id": old.memory["id"],
                            "merged_text": "Amounts may be floats."})
        out = store.remember(WS, "Amounts may be floats, cents are optional.", actor=STRANGER,
                             llm=FakeModel(reply))
        assert out.action == "pending", "stored as its own proposal"
        rows = {r[0]: r for r in _rows(tmp_path)}
        assert rows[old.memory["id"]][2] == "Amounts are integer cents."
        assert rows[old.memory["id"]][1] == "active"
        assert out.memory["id"] != old.memory["id"]


@pytest.mark.parametrize("reply", [
    RuntimeError("model is down"),
    "not json at all",
    '{"action": "skip", "target_id": 999}',
    '{"action": "explode"}',
])
async def test_a_model_that_cannot_answer_does_not_lose_the_memory(tmp_path, monkeypatch, reply):
    async with rules_db(tmp_path, monkeypatch):
        store.remember(WS, "Amounts are integer cents.", actor=TRUSTED, llm=FakeModel("{}"))
        out = store.remember(WS, "Amounts must be integer cents always.", actor=TRUSTED,
                             llm=FakeModel(reply))
        assert out.action == "create"
        assert len(_rows(tmp_path)) == 2


# ─── refusals ────────────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs, why", [
    ({"text": "   "}, "empty"),
    ({"text": "x" * (store.MAX_TEXT + 1)}, "too long"),
    ({"text": "ok text here", "path_glob": "src/**"}, "a directory needs a repository"),
    ({"text": "ok text here", "repo_slug": REPO, "path_glob": "../etc"}, "escapes the repo"),
])
async def test_a_request_the_store_refuses_is_a_skip_with_a_reason(tmp_path, monkeypatch,
                                                                   kwargs, why):
    async with rules_db(tmp_path, monkeypatch):
        out = store.remember(WS, actor=TRUSTED, llm=FakeModel("{}"), **kwargs)
        assert out.action == "skip" and out.code == "invalid", why
        assert out.reason
        assert _rows(tmp_path) == []


async def test_an_unknown_origin_is_a_programming_error(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        with pytest.raises(ValueError):
            store.remember(WS, "A fact worth keeping.", actor=TRUSTED, origin="whim")


async def test_a_full_scope_takes_no_more(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        from src.db.models import ReviewMemory

        async with factory() as s:
            s.add_all([ReviewMemory(workspace_id=WS, repo_slug=None, text=f"Fact number {i}",
                                    status="active")
                       for i in range(store.MAX_MEMORIES_PER_SCOPE)])
            await s.commit()
        out = store.remember(WS, "One more thing entirely.", actor=TRUSTED, llm=FakeModel("{}"))
        assert out.action == "skip" and out.code == "limit"


# ─── reading ─────────────────────────────────────────────────────────


async def test_the_workspace_never_reads_another_workspaces_memories(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        store.remember("ws-b", "Secret of the other workspace.", actor=TRUSTED, llm=FakeModel("{}"))
        store.remember(WS, "Ours alone.", actor=TRUSTED, llm=FakeModel("{}"))
        assert [m["text"] for m in store.load_active_sync(WS, REPO)] == ["Ours alone."]
        assert store.load_active_sync("", REPO) == []


async def test_a_directory_memory_is_found_only_for_paths_under_it(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        store.remember(WS, "Billing rounds half-even.", repo_slug=REPO, path_glob="src/billing",
                       actor=TRUSTED, llm=FakeModel("{}"))
        store.remember(WS, "Repo-wide fact.", repo_slug=REPO, actor=TRUSTED, llm=FakeModel("{}"))
        under = store.relevant_memories(WS, REPO, ["src/billing/a.py"])
        away = store.relevant_memories(WS, REPO, ["web/a.tsx"])
        assert [m["text"] for m in under] == ["Billing rounds half-even.", "Repo-wide fact."]
        assert [m["text"] for m in away] == ["Repo-wide fact."]


async def test_the_chat_gets_the_block_a_review_gets_unless_memories_are_off(tmp_path, monkeypatch):
    from src.db.models import RepoReviewPolicy

    async with rules_db(tmp_path, monkeypatch) as factory:
        store.remember(WS, "Repo-wide fact.", repo_slug=REPO, actor=TRUSTED, llm=FakeModel("{}"))
        text = store.render_for_chat(WS, REPO, ["a.py"])
        assert "Repo-wide fact." in text and "<team_memories>" in text
        async with factory() as s:
            s.add(RepoReviewPolicy(repo_slug=REPO, workspace_id=WS, memories_enabled=False))
            await s.commit()
        assert store.render_for_chat(WS, REPO, ["a.py"]) == ""


# ─── review fixes ────────────────────────────────────────────────────


async def test_a_display_name_alone_does_not_make_someone_a_trusted_commenter(tmp_path, monkeypatch):
    from src.db.models import RepoReviewPolicy

    async with rules_db(tmp_path, monkeypatch) as factory:
        async with factory() as s:
            s.add(RepoReviewPolicy(repo_slug=REPO, workspace_id=WS,
                                   memory_trusted_commenters=["alice"]))
            await s.commit()
        pretender = ActorRef(provider="gitlab", external_id="mallory", display="Alice")
        real = ActorRef(provider="gitlab", external_id="alice", display="Somebody Else")
        assert store.remember(WS, "Planted instruction one.", repo_slug=REPO, actor=pretender,
                              llm=FakeModel("{}")).action == "pending"
        assert store.remember(WS, "A genuine fact here.", repo_slug=REPO, actor=real,
                              llm=FakeModel("{}")).action == "create"


async def test_strangers_filling_the_queue_do_not_block_the_trusted(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        from src.db.models import ReviewMemory

        async with factory() as s:
            s.add_all([ReviewMemory(workspace_id=WS, repo_slug=None, text=f"Proposal number {i}",
                                    status="pending")
                       for i in range(store.MAX_PENDING_PER_SCOPE)])
            await s.commit()
        again = store.remember(WS, "One proposal too many.", actor=STRANGER, llm=FakeModel("{}"))
        assert again.action == "skip" and again.code == "limit"
        mine = store.remember(WS, "The owner's own fact.", actor=TRUSTED, llm=FakeModel("{}"))
        assert mine.action == "create" and mine.memory["status"] == "active"


@pytest.mark.parametrize("glob, path, hit", [
    (".github", ".github/workflows/ci.yml", True),
    ("docs.v2", "docs.v2/a.md", True),
    ("Makefile", "Makefile", True),
    ("src/billing", "src/billing/a.py", True),
    ("src/billing", "src/billing2/a.py", False),
    ("src/billing/", "src/billing/a.py", True),
    ("src/*.py", "src/a.py", True),
])
async def test_a_plain_path_memory_covers_the_file_or_the_directory_of_that_name(
        tmp_path, monkeypatch, glob, path, hit):
    async with rules_db(tmp_path, monkeypatch):
        store.remember(WS, "A fact about that place.", repo_slug=REPO, path_glob=glob,
                       actor=TRUSTED, llm=FakeModel("{}"))
        assert bool(store.relevant_memories(WS, REPO, [path])) is hit


async def test_a_trusted_request_is_not_dropped_for_a_strangers_pending_one_elsewhere(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        theirs = store.remember(WS, "Amounts are integer cents in the database.",
                                actor=STRANGER, llm=FakeModel("{}"))
        reply = f'{{"action": "skip", "target_id": {theirs.memory["id"]}}}'
        out = store.remember(WS, "We keep money as integer cents in the database.",
                             repo_slug=REPO, actor=TRUSTED, llm=FakeModel(reply))
        assert out.action == "create"
        assert [m["id"] for m in store.load_active_sync(WS, REPO)] == [out.memory["id"]]


async def test_editing_only_the_glob_cannot_make_a_twin(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        a = store.remember(WS, "Billing rounds half-even.", repo_slug=REPO, path_glob="src/a",
                           actor=TRUSTED, llm=FakeModel("{}"))
        b = store.remember(WS, "Billing rounds half-even.", repo_slug=REPO, path_glob="src/b",
                           actor=TRUSTED, llm=FakeModel("{}"))
        assert a.action == b.action == "create"
        with pytest.raises(store.MemoryConflictError):
            await store.update_memory(WS, b.memory["id"], {"path_glob": "src/a"}, "me")


async def test_the_chat_does_not_tell_a_repositorys_memories_to_somebody_who_cannot_read_it(
    tmp_path, monkeypatch,
):
    async with rules_db(tmp_path, monkeypatch):
        store.remember(WS, "Workspace-wide fact.", actor=TRUSTED, llm=FakeModel("{}"))
        store.remember(WS, "Repo-wide fact.", repo_slug=REPO, actor=TRUSTED, llm=FakeModel("{}"))
        store.remember(WS, "Billing fact.", repo_slug=REPO, path_glob="src/billing",
                       actor=TRUSTED, llm=FakeModel("{}"))
        allowed = store.render_for_chat(WS, REPO, ["src/billing/a.py"])
        assert "Repo-wide fact." in allowed and "Billing fact." in allowed
        refused = store.render_for_chat(
            WS, REPO, ["src/billing/a.py"], reader_may_read_repo=False)
        assert "Workspace-wide fact." in refused
        assert "Repo-wide fact." not in refused and "Billing fact." not in refused
