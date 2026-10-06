"""Reactions, resolved threads and rules learned from the history.

  * thumbs on a finding comment are read at the next review of the same pull
    request: a verdict per person, a taken-back reaction is withdrawn, both
    directions from one person say nothing, a provider that cannot list
    reactions costs one call and no signal, a listed reviewer is skipped;
  * a resolved thread is a weak signal, reopening it withdraws it, a bot's
    resolve and a thread that holds no finding are ignored;
  * the history becomes PENDING rules (origin "learned") only where several
    pull requests agree and nobody argues, and a repository with no pattern
    says so without asking the model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import sqlalchemy as sa

from src.review import rules_store
from src.review.learning import history_rules as hist
from src.review.learning import reactions, resolve
from src.review.learning import signals as sig
from tests.review.rules_db import rules_db

WS, REPO = "ws-a", "github_acme-shop"
PR = sig.PRRef("github", "acme/shop", 7)
SNAP = sig.FindingSnapshot(
    title="Possible null dereference in user lookup", file_path="src/users/lookup.py",
    body="user may be None here", rule_id="defect.null", agent="defect", severity="warning")


def _post(*snaps, first=901):
    comments = [{"comment_id": first + i, "fingerprint": sig.snapshot_of(s).fingerprint[:16]}
                for i, s in enumerate(snaps)]
    assert sig.record_posted(WS, PR, list(snaps), comments) == len(snaps)


def _signals(tmp_path):
    with sa.create_engine(f"sqlite:///{tmp_path}/rules.db").connect() as conn:
        return sorted(tuple(r) for r in conn.execute(sa.text(
            "SELECT signal, source, actor, weight FROM finding_signals")))


class Provider:
    def __init__(self, by_comment, *, strangers=()):
        self.by_comment, self.calls = by_comment, []
        self.strangers = {u.lower() for u in strangers}

    def actor_permission(self, repo, *, actor_id="", actor_name=""):
        return "read" if (actor_name or actor_id).lower() in self.strangers else "write"

    def pr_participants(self, repo, pr_number):
        return frozenset()

    def list_comment_reactions(self, repo, number, comment_id):
        self.calls.append((repo, number, str(comment_id)))
        got = self.by_comment.get(str(comment_id), [])
        if isinstance(got, Exception):
            raise got
        return got


# ─── reactions ───────────────────────────────────────────────────────


def test_a_person_with_both_thumbs_says_nothing():
    pairs = [("Jane", "down"), ("omar", "up"), ("li", "up"), ("li", "down"), ("x", "heart")]
    assert reactions.classify_reactions(pairs) == {"jane": "dismissed", "omar": "accepted"}


async def test_a_stranger_who_can_only_read_teaches_nothing_by_a_thumb(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        prov = Provider({"901": [("passerby", "down"), ("jane", "down")]},
                        strangers=["passerby"])
        out = reactions.poll_pr_reactions(WS, PR, prov)
        assert out["recorded"] == 1 and [s[2] for s in _signals(tmp_path)] == ["jane"]


async def test_a_thumb_given_before_stays_when_the_person_cannot_be_vouched_for_now(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        reactions.poll_pr_reactions(WS, PR, Provider({"901": [("jane", "down")]}))
        out = reactions.poll_pr_reactions(
            WS, PR, Provider({"901": [("jane", "down")]}, strangers=["jane"]))
        assert out["withdrawn"] == 0 and [s[2] for s in _signals(tmp_path)] == ["jane"]


async def test_thumbs_become_one_verdict_per_person(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        prov = Provider({"901": [("jane", "down"), ("omar", "up")]})
        out = reactions.poll_pr_reactions(WS, PR, prov)
        assert (out["checked"], out["recorded"]) == (1, 2)
        assert [(s[0], s[1], s[2]) for s in _signals(tmp_path)] == [
            ("accepted", "reaction", "omar"), ("dismissed", "reaction", "jane")]
        again = reactions.poll_pr_reactions(WS, PR, prov)
        assert again["recorded"] == 0 and len(_signals(tmp_path)) == 2


async def test_a_reaction_taken_back_is_withdrawn(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        reactions.poll_pr_reactions(WS, PR, Provider({"901": [("jane", "down")]}))
        out = reactions.poll_pr_reactions(WS, PR, Provider({"901": []}))
        assert out["withdrawn"] == 1 and _signals(tmp_path) == []


async def test_a_listed_reviewer_and_a_skipped_user_teach_nothing_by_a_thumb(
        tmp_path, monkeypatch):
    from src.db.models import RepoReviewPolicy

    async with rules_db(tmp_path, monkeypatch) as factory:
        async with factory() as s:
            s.add(RepoReviewPolicy(repo_slug=REPO, workspace_id=WS,
                                   learning_excluded_reviewers=["ci-bot"]))
            await s.commit()
        _post(SNAP)
        prov = Provider({"901": [("ci-bot", "down"), ("pr-author", "down"), ("jane", "down")]})
        out = reactions.poll_pr_reactions(WS, PR, prov, skip_users=["PR-Author"])
        assert out["recorded"] == 1 and [s[2] for s in _signals(tmp_path)] == ["jane"]


async def test_a_provider_that_cannot_list_reactions_costs_a_few_calls_at_most(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        snaps = [sig.FindingSnapshot(title=f"Finding number {i}", file_path=f"f{i}.py",
                                     rule_id=f"r.{i}") for i in range(10)]
        _post(*snaps)
        prov = Provider({str(901 + i): RuntimeError("not supported") for i in range(10)})
        out = reactions.poll_pr_reactions(WS, PR, prov)
        assert len(prov.calls) == reactions.MAX_LEADING_FAILURES and out["checked"] == 0
        assert out["skipped"] == "RuntimeError" and _signals(tmp_path) == []


async def test_one_unreadable_comment_does_not_stop_the_poll(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        other = sig.FindingSnapshot(title="Unused import", file_path="a.py", rule_id="style.x")
        _post(SNAP, other)
        prov = Provider({"901": RuntimeError("404 deleted"), "902": [("jane", "down")]})
        out = reactions.poll_pr_reactions(WS, PR, prov)
        assert len(prov.calls) == 2 and out["checked"] == 1 and out["recorded"] == 1


async def test_a_finding_posted_twice_keeps_the_thumb_given_on_either_comment(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        _post(SNAP, first=902)
        prov = Provider({"901": [], "902": [("jane", "down")]})
        out = reactions.poll_pr_reactions(WS, PR, prov)
        assert out["recorded"] == 1 and out["withdrawn"] == 0
        assert [(r[0], r[2]) for r in _signals(tmp_path)] == [("dismissed", "jane")]
        again = reactions.poll_pr_reactions(WS, PR, prov)
        assert again["withdrawn"] == 0 and len(_signals(tmp_path)) == 1


async def test_a_thumb_is_withdrawn_only_when_no_comment_of_the_finding_has_it(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        _post(SNAP, first=902)
        reactions.poll_pr_reactions(WS, PR, Provider({"902": [("jane", "down")]}))
        out = reactions.poll_pr_reactions(WS, PR, Provider({"901": [], "902": []}))
        assert out["withdrawn"] == 1 and _signals(tmp_path) == []


async def test_a_thumb_is_kept_when_one_of_the_finding_comments_cannot_be_read(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        _post(SNAP, first=902)
        reactions.poll_pr_reactions(WS, PR, Provider({"902": [("jane", "down")]}))
        out = reactions.poll_pr_reactions(
            WS, PR, Provider({"901": [], "902": RuntimeError("rate limited")}))
        assert out["withdrawn"] == 0 and len(_signals(tmp_path)) == 1


async def test_no_more_comments_are_polled_than_the_cap(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        snaps = [sig.FindingSnapshot(title=f"Finding number {i}", file_path=f"f{i}.py",
                                     rule_id=f"r.{i}") for i in range(reactions.MAX_COMMENTS + 5)]
        _post(*snaps)
        prov = Provider({})
        out = reactions.poll_pr_reactions(WS, PR, prov)
        assert out["checked"] == reactions.MAX_COMMENTS == len(prov.calls)


async def test_reactions_never_raise_into_the_review(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = reactions.poll_pr_reactions(WS, PR, Provider({}))
    assert out["checked"] == 0


# ─── resolved threads ────────────────────────────────────────────────


def _payload(action="resolved", sender=None, comments=(901,), number=7):
    return {"action": action, "repository": {"full_name": "acme/shop"},
            "pull_request": {"number": number},
            "thread": {"comments": [{"id": c} for c in comments]},
            "sender": sender or {"login": "Jane", "type": "User"}}


def test_a_thread_delivery_is_read_into_an_event():
    ev = resolve.extract_github_thread_event(_payload())
    assert (ev.repo, ev.pr_number, ev.resolved, ev.comment_ids, ev.actor_id) == (
        "acme/shop", 7, True, ["901"], "Jane")
    assert resolve.extract_github_thread_event(_payload("unresolved")).resolved is False


def test_a_delivery_that_is_not_about_resolving_is_not_an_event():
    assert resolve.extract_github_thread_event(_payload("edited")) is None
    assert resolve.extract_github_thread_event(_payload(comments=())) is None
    assert resolve.extract_github_thread_event({"action": "resolved"}) is None
    assert resolve.extract_github_thread_event("nope") is None


async def test_resolving_a_thread_is_a_weak_signal_and_reopening_withdraws_it(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        done = resolve.handle_thread_event(
            resolve.extract_github_thread_event(_payload()), workspace_id=WS)
        assert done["action"] == "resolved"
        assert _signals(tmp_path) == [("resolved", "resolve", "jane", 0.4)]
        back = resolve.handle_thread_event(
            resolve.extract_github_thread_event(_payload("unresolved")), workspace_id=WS)
        assert (back["action"], back["withdrawn"]) == ("reopened", 1)
        assert _signals(tmp_path) == []


async def test_a_bots_resolve_and_a_thread_without_a_finding_are_ignored(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post(SNAP)
        bot = resolve.extract_github_thread_event(
            _payload(sender={"login": "ci[bot]", "type": "Bot"}))
        assert resolve.handle_thread_event(bot, workspace_id=WS)["handled"] is False
        foreign = resolve.extract_github_thread_event(_payload(comments=(555,)))
        assert resolve.handle_thread_event(foreign, workspace_id=WS)["action"] == "not a finding"
        other_ws = resolve.extract_github_thread_event(_payload())
        assert resolve.handle_thread_event(other_ws, workspace_id="ws-b")["handled"] is False
        assert _signals(tmp_path) == []


# ─── rules from the history ──────────────────────────────────────────


def row(i, signal="dismissed", *, actor=None, pr=None, path="src/legacy/a.py",
        rule="style.naming", title="Rename the variable", reason=""):
    return SimpleNamespace(
        signal=signal, reason=reason, file_path=path, rule_id=rule, category="style",
        title=title, pr_provider="github", pr_repo="acme/shop", pr_number=pr or i,
        actor=actor or f"p{i}")


def test_a_group_needs_several_pull_requests_of_one_opinion():
    assert hist.build_clusters([row(1), row(2)], 3) == []
    [c] = hist.build_clusters([row(1), row(2), row(3)], 3)
    assert (c.direction, c.directory, len(c.prs)) == ("dismissed", "src/legacy", 3)


def test_the_same_person_on_one_pull_request_is_one_piece_of_evidence():
    rows = [row(1, actor="a", pr=1), row(2, actor="a", pr=1), row(3, actor="a", pr=1)]
    assert hist.build_clusters(rows, 2) == []


def test_a_group_the_team_argues_about_gives_no_rule():
    rows = [row(i) for i in range(1, 5)] + [row(i, "accepted") for i in range(10, 13)]
    assert hist.build_clusters(rows, 3) == []


def test_a_duplicate_dismissal_is_not_evidence():
    assert hist.build_clusters([row(i, reason="duplicate") for i in range(1, 6)], 3) == []


def test_weak_signals_count_as_evidence_for_a_rule_but_not_for_hiding():
    rows = [row(1, "ignored"), row(2, "resolved"), row(3, "dismissed")]
    [c] = hist.build_clusters(rows, 3)
    assert c.direction == "dismissed"


class Model:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def generate(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=self.reply)


REPLY = json.dumps({"rules": [{
    "title": "Do not flag naming in the legacy package",
    "instructions": "Variable names under src/legacy follow an old convention; leave them.",
    "severity": "info", "path_glob": "src/legacy/**",
    "rationale": "dismissed in 3 pull requests"}]})


async def _teach(n=3, **kw):
    for i in range(1, n + 1):
        sig.record_verdict(
            WS, REPO, sig.FindingSnapshot(title=kw.get("title", "Rename the variable"),
                                          file_path=f"src/legacy/f{i}.py", rule_id="style.naming"),
            "dismissed", "reply", pr=sig.PRRef("github", "acme/shop", i), actor=f"p{i}")


async def test_the_history_becomes_pending_learned_rules(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach()
        model = Model(REPLY)
        out = await hist.learn_rules(WS, REPO, "lead@example.com", llm=model)
        assert out["proposed"] == 1 and len(out["created"]) == 1
        prompt = model.calls[0]["prompt"]
        assert "<evidence>" in prompt and "DISMISSED in 3 reviews" in prompt
        [rule] = await rules_store.list_rules(WS)
        assert (rule["status"], rule["origin"], rule["source_ref"]) == (
            "pending", "learned", "history")


async def test_a_repository_without_a_pattern_says_so_without_asking_the_model(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(n=2)
        model = Model(REPLY)
        out = await hist.learn_rules(WS, REPO, "lead@example.com", llm=model)
        assert out["note"] == "no_pattern" and out["created"] == [] and model.calls == []


async def test_a_rule_the_team_already_has_is_not_proposed_again(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach()
        await hist.learn_rules(WS, REPO, "lead@example.com", llm=Model(REPLY))
        again = await hist.learn_rules(WS, REPO, "lead@example.com", llm=Model(REPLY))
        assert again["created"] == [] and again["skipped"] == 1


async def test_an_unreadable_model_reply_fails_the_job_after_one_retry(tmp_path, monkeypatch):
    import pytest

    from src.review.rules_generate import RulesJobError

    async with rules_db(tmp_path, monkeypatch):
        await _teach()
        model = Model("I could not think of anything")
        with pytest.raises(RulesJobError):
            await hist.learn_rules(WS, REPO, "lead@example.com", llm=model)
        assert len(model.calls) == 2


async def test_another_repositorys_feedback_is_not_in_the_evidence(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        for i in range(1, 5):
            sig.record_verdict(
                WS, "github_acme-api", sig.FindingSnapshot(
                    title="Rename the variable", file_path=f"src/legacy/f{i}.py",
                    rule_id="style.naming"),
                "dismissed", "reply", pr=sig.PRRef("github", "acme/api", i), actor=f"p{i}")
        out = await hist.learn_rules(WS, REPO, "lead@example.com", llm=Model(REPLY))
        assert out["note"] == "no_pattern" and out["context"]["signals"] == 0
