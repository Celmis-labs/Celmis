"""The review features read repositories through the one access model.

Memories, learning, the issue backlog, feedback on a finding, the commands tab
and the requirements check of a pull request all hold what a repository
contains (a finding with its snippet, a rule taught on it, a run's verdict).
They are therefore readable exactly when the repository's CODE is — the rule of
docs/mcp-access.md, the one every MCP tool and REST read applies:

* owner, admin, global admin and the superadmin hold every repository of their
  workspace;
* everybody else holds what a team grant of ``read`` or higher, or a research
  rule at ``code``, gives them;
* a repository held only at ``metadata`` may be NAMED, never read: it is as
  absent for these features as one with no rule at all;
* a repository the caller may not read is not listed, not counted and not
  confirmed by an error: the answer is the one for a repository that is not
  there.

The role gates (memories editor and above, productivity owner and admin) are
the subject of ``tests/api/test_every_endpoint_of_a_restricted_surface_names_
its_gate.py``; this file is about WHICH repositories those roles may see.

World (workspace A): GRANTED a team grant, RULED a research rule at ``code``
for member_a, META a rule at ``metadata`` for viewer_a, WILD nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.api.rbac_world import A_REPO, world

GRANTED = A_REPO
RULED = "github_aco-ruled"
META = "github_aco-meta"
WILD = "github_aco-wild"
ALL = {GRANTED, RULED, META, WILD}

#: principal -> the repositories whose CODE they may read in workspace A
CODE = {
    "su": ALL,
    "owner_a": ALL,
    "admin_a": ALL,
    "admin2_a": ALL,
    "editor_a": {GRANTED},
    "member_a": {RULED},
    "viewer_a": set(),      # holds META at `metadata`: named, never read
    "both": set(),
}
PRINCIPALS = list(CODE)
#: who passes the editor gate of /memories and /learning
EDITORS = ("su", "owner_a", "admin_a", "admin2_a", "editor_a")


def _full(slug: str) -> str:
    return slug.split("_", 1)[1].replace("-", "/", 1)


def _routers() -> tuple:
    from src.api.routers import feedback, issues, learning, memories, pull_requests, reviews

    return (issues.router, memories.router, learning.router, feedback.router,
            pull_requests.router, reviews.router)


async def _seed(w) -> None:
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.api.review_runs import ReviewRun, get_review_run_store
    from src.db.models import (
        RepoAccessRule,
        ReviewIssue,
        ReviewMemory,
        ReviewPullRequest,
        Team,
        TeamMember,
    )
    from src.review.learning import signals as sig

    a = w.ws["ws-a"]
    now = datetime.now(UTC)
    store = get_auto_review_store()
    for slug in (RULED, META, WILD):
        store.upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=slug, provider="github",
            full_name=_full(slug), url=f"https://github.com/{_full(slug)}",
            workspace_id=a))
    run_store = get_review_run_store()
    async with w.factory() as s:
        s.add(Team(id="team-r", name="rule-team", description="", workspace_id=a))
        s.add(TeamMember(team_id="team-r", user_id=w.uid("member_a"), role="member"))
        s.add(RepoAccessRule(id="rule-r", workspace_id=a, team_id="team-r",
                             repo_slug=RULED, visibility="code"))
        s.add(Team(id="team-m", name="meta-team", description="", workspace_id=a))
        s.add(TeamMember(team_id="team-m", user_id=w.uid("viewer_a"), role="member"))
        s.add(RepoAccessRule(id="rule-m", workspace_id=a, team_id="team-m",
                             repo_slug=META, visibility="metadata"))
        s.add(ReviewMemory(workspace_id=a, text="memory of the workspace", status="active"))
        for i, slug in enumerate(sorted(ALL)):
            s.add(ReviewIssue(
                id=f"issue-{slug}", workspace_id=a, repo_slug=slug, fingerprint=f"fp{i}",
                file_path="x.py", line=1, agent="defect", rule_id="defect.x",
                category="bug", severity="error", title=f"issue in {slug}", body="",
                suggestion=None, status="open", resolution_source=None,
                pr_provider="github", pr_repo=_full(slug), pr_number=i,
                pr_url=None, first_run_id="r1", last_run_id="r1", occurrences=1,
                first_seen_at=now, last_seen_at=now, closed_at=None,
                merged_at=now - timedelta(days=1), base_ref="main"))
            s.add(ReviewMemory(workspace_id=a, repo_slug=slug, status="active",
                               text=f"memory of {slug}"))
            s.add(ReviewPullRequest(
                id=f"pr-{slug}", workspace_id=a, provider="github", repo=_full(slug),
                number=i, repo_slug=slug, title=f"pr of {slug}"))
            run_store.insert(ReviewRun(
                id=f"run-{slug}", user_id=w.uid("admin_a"), pr_ref=f"github:{_full(slug)}#{i}",
                status="complete", verdict="approve", findings_count=1,
                started_at=now.isoformat(), workspace_id=a,
                pr_provider="github", pr_repo=_full(slug), pr_number=i))
        await s.commit()
    for slug in sorted(ALL):
        assert sig.record_verdict(
            a, slug, sig.FindingSnapshot(title=f"signal on {slug}", file_path="src/a.py",
                                         rule_id="defect.x"),
            "dismissed", "reply", pr=sig.PRRef("github", _full(slug), 1),
            actor="jane@example.com") == "created"


@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, event

    from src.access import resolver
    from tests.api.rbac_world import _sqlite_booleans

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        sync = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(sync, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", sync)
        await _seed(w)
        try:
            yield w
        finally:
            sync.dispose()


def _named(body: str) -> set[str]:
    return {slug for slug in ALL if slug in body}


# ─── one model: the helper agrees with the resolver ──────────────────


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_features_and_the_resolver_agree_on_who_reads_which_code(seeded, who):
    """`code_readable_repo_slugs` (what the features call) is the resolver's
    ``code_visible`` (what every MCP tool calls), principal by principal."""
    from src.access.effective import Principal, effective_access
    from src.api.deps import code_readable_repo_slugs

    w = seeded
    user, a = w.users[who], w.ws["ws-a"]
    mine = await code_readable_repo_slugs(user, a, sorted(ALL))
    decisions = effective_access(Principal(user.id, bool(user.is_admin)), a, sorted(ALL))
    via_resolver = {s for s, d in decisions.items() if d.code_visible}
    # Only the workspace's own admins are exempt from registration: GRANTED is
    # registered by the world, so the two readings cover the same four repos.
    assert mine == via_resolver == CODE[who], (who, mine, via_resolver)


# ─── the issue backlog ───────────────────────────────────────────────


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_issue_list_and_its_numbers_cover_only_readable_code(seeded, who):
    h = seeded.h(who, "ws-a")
    r = await seeded.client.get("/api/issues", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {i["repo_slug"] for i in body["items"]} == CODE[who]
    assert body["total"] == len(CODE[who])
    assert set(body["repos"]) == CODE[who]
    summary = await seeded.client.get("/api/issues/summary", headers=h)
    assert summary.status_code == 200
    assert summary.json()["backlog_open"] == len(CODE[who]), "the numbers count unseen repos"


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_summary_of_an_unreadable_repository_is_a_404(seeded, who):
    for slug in ALL - CODE[who]:
        r = await seeded.client.get("/api/issues/summary", params={"repo": slug},
                                    headers=seeded.h(who, "ws-a"))
        assert r.status_code == 404, (who, slug)


@pytest.mark.parametrize("who", ["owner_a", "admin_a", "editor_a", "member_a"])
async def test_a_recheck_runs_only_for_readable_repositories(seeded, monkeypatch, who):
    """One branch per readable repository: the unseen ones cost no model call."""
    import src.review.issue_resolver as resolver

    called: list[str] = []
    monkeypatch.setattr(resolver, "recheck_backlog",
                        lambda ws, provider, repo, base, reason="": called.append(repo))
    h = seeded.h(who, "ws-a")
    r = await seeded.client.post("/api/issues/recheck", json={}, headers=h)
    assert r.status_code == 202, r.text
    assert r.json()["queued"] == len(CODE[who])
    import asyncio

    await asyncio.sleep(0.2)
    assert {_ for _ in called} == {_full(s) for s in CODE[who]}


@pytest.mark.parametrize("who", ["editor_a", "member_a"])
async def test_a_recheck_that_names_an_unreadable_repository_is_a_404(seeded, who):
    for slug in ALL - CODE[who]:
        r = await seeded.client.post("/api/issues/recheck", json={"repo": slug},
                                     headers=seeded.h(who, "ws-a"))
        assert r.status_code == 404, (who, slug, r.text)


@pytest.mark.parametrize("who", ["editor_a", "member_a"])
async def test_an_issue_of_an_unreadable_repository_cannot_be_changed_or_confirmed(seeded, who):
    h = seeded.h(who, "ws-a")
    missing = await seeded.client.patch("/api/issues/issue-nope", json={"status": "dismissed"},
                                        headers=h)
    for slug in sorted(ALL):
        r = await seeded.client.patch(f"/api/issues/issue-{slug}", json={"status": "dismissed"},
                                      headers=h)
        if slug in CODE[who]:
            assert r.status_code == 200, (who, slug, r.text)
        else:
            assert (r.status_code, r.json()) == (missing.status_code, missing.json()), (who, slug)


# ─── memories and learning (editor and above) ────────────────────────


@pytest.mark.parametrize("who", EDITORS)
async def test_the_memories_shown_and_counted_are_those_of_readable_repositories(seeded, who):
    r = await seeded.client.get("/api/memories", headers=seeded.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    texts = {m["text"] for m in r.json()["memories"]}
    assert ("memory of the workspace" in texts)
    assert {s for s in ALL if f"memory of {s}" in texts} == CODE[who]
    assert r.json()["counts"]["all"] == 1 + len(CODE[who])


@pytest.mark.parametrize("who", ["editor_a"])
async def test_a_memory_of_a_repository_held_at_metadata_is_not_read_or_edited(seeded, who):
    h = seeded.h(who, "ws-a")
    for slug in ALL - CODE[who]:
        r = await seeded.client.get("/api/memories", params={"repo": slug}, headers=h)
        assert r.status_code == 404, (slug, r.text)
        w = await seeded.client.post(
            "/api/memories", json={"text": "taught", "repo_slug": slug}, headers=h)
        assert w.status_code in (403, 404), (slug, w.status_code)


@pytest.mark.parametrize("who", EDITORS)
async def test_the_learning_summary_and_signals_cover_only_readable_repositories(seeded, who):
    h = seeded.h(who, "ws-a")
    listed = await seeded.client.get("/api/learning/signals", headers=h)
    assert listed.status_code == 200, listed.text
    assert {s for s in ALL if f"signal on {s}" in listed.text} == CODE[who]
    assert listed.json()["total"] == len(CODE[who])
    summary = await seeded.client.get("/api/learning/summary", headers=h)
    assert summary.status_code == 200
    for slug in ALL - CODE[who]:
        assert slug not in summary.text and _full(slug) not in summary.text


@pytest.mark.parametrize("who", ["editor_a"])
async def test_a_learning_signal_of_an_unreadable_repository_cannot_be_named_or_forgotten(
        seeded, who):
    from sqlalchemy import select

    from src.db.models import FindingSignal

    async with seeded.factory() as s:
        rows = {r.repo_slug: r.id for r in (await s.scalars(select(FindingSignal))).all()}
    h = seeded.h(who, "ws-a")
    for slug in ALL - CODE[who]:
        by_repo = await seeded.client.get("/api/learning/signals", params={"repo": slug}, headers=h)
        gone = await seeded.client.delete(f"/api/learning/signals/{rows[slug]}", headers=h)
        assert by_repo.status_code == 404 and gone.status_code == 404, (slug,)


# ─── the pages of a run and of a pull request ────────────────────────


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_feedback_on_a_finding_follows_the_run_s_repository(seeded, who):
    h = seeded.h(who, "ws-a")
    missing = await seeded.client.get("/api/feedback/run/run-nope", headers=h)
    assert missing.status_code == 200 and missing.json() == []
    for slug in sorted(ALL):
        run = f"run-{slug}"
        read = await seeded.client.get(f"/api/feedback/run/{run}", headers=h)
        write = await seeded.client.put(
            f"/api/feedback/run/{run}", headers=h,
            json={"finding_key": "key-0001", "state": "dismissed", "reason": "noise"})
        undo = await seeded.client.delete(f"/api/feedback/run/{run}/key-0001", headers=h)
        if slug in CODE[who]:
            assert (read.status_code, write.status_code, undo.status_code) == (200, 200, 204), (
                who, slug, write.text)
        else:
            assert (read.status_code, write.status_code, undo.status_code) == (404, 404, 404), (
                who, slug)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_commands_tab_and_the_requirements_of_a_pr_follow_its_repository(
        seeded, monkeypatch, who):
    from src.review.commands import ledger

    # The timeline's own store is another suite's subject; here only the gate is.
    monkeypatch.setattr(ledger, "for_pr", lambda *_a, **_k: [])
    h = seeded.h(who, "ws-a")
    for slug in sorted(ALL):
        for tail in ("commands", "requirements"):
            r = await seeded.client.get(f"/api/pull-requests/pr-{slug}/{tail}", headers=h)
            if slug in CODE[who]:
                assert r.status_code == 200, (who, slug, tail, r.text)
            else:
                assert r.status_code == 404, (who, slug, tail, r.status_code)


@pytest.mark.parametrize("who", ["viewer_a", "member_a", "editor_a"])
async def test_pausing_and_resuming_a_pr_needs_the_repository_too(seeded, who):
    h = seeded.h(who, "ws-a")
    for slug in sorted(ALL - CODE[who]):
        for tail in ("pause", "resume"):
            r = await seeded.client.post(f"/api/pull-requests/pr-{slug}/{tail}", headers=h)
            assert r.status_code in (403, 404), (who, slug, tail, r.status_code)
            assert r.status_code != 200


# ─── what a reply may carry ──────────────────────────────────────────


def test_a_chat_answer_is_redacted_by_the_central_layer_even_when_prompts_are_not(monkeypatch):
    """The reply is posted where everybody who opens the pull request reads it.
    The prompt's redactor can be switched off; this layer cannot."""
    from src.config import get_settings
    from src.review.commands.chat import clean_answer

    monkeypatch.setenv("REDACTION_ENABLED", "false")
    get_settings.cache_clear()
    try:
        secret = "Zq8" + "x" * 9 + "Lm2pW"
        text = (f"The service connects with postgresql://app:{secret}@db.internal/app "
                "and reads os.getenv('DB_PASSWORD').")
        out = clean_answer(text)
    finally:
        get_settings.cache_clear()
    assert secret not in out
    assert "os.getenv('DB_PASSWORD')" in out, "a reference to a secret is not a secret"


def test_an_answer_that_could_not_be_checked_is_withheld(monkeypatch):
    import src.security.mcp_redact as central
    from src.review.commands.chat import ANSWER_WITHHELD, clean_answer

    def boom(*_a, **_k):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(central, "redact_for_mcp", boom)
    assert clean_answer("anything at all") == ANSWER_WITHHELD


def test_the_chat_prompt_reads_only_the_repository_of_the_pull_request():
    """An answer must never carry code of a repository the asker cannot read:
    the only code the chat reads is the anchored hunk and the digest of the pull
    request it was asked on, and the only memories are the workspace's and this
    repository's. No search, graph or cross-repository retrieval is imported."""
    import inspect

    import src.review.commands.chat as chat

    source = inspect.getsource(chat)
    for forbidden in ("retrieval", "multi_repo", "graph_store", "cross_repo", "vault"):
        assert forbidden not in source, f"chat.py reaches for {forbidden}"
