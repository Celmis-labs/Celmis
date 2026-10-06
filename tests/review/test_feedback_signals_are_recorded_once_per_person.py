"""`signals` — what people said about a finding, and what became of it.

By behaviour, against a SQLite file behind the real blocking store:

  * a person's verdict is one row however many times it is sent, and a
    changed mind replaces it instead of piling up;
  * the finding is recognised across pull requests by its fingerprint, not by
    the line it was on;
  * a listed reviewer teaches nothing, by any of their names, on the repo they
    are listed for and not on another;
  * the reviews page teaches only through the run row (PR and repository come
    from the server, a run of another workspace teaches nothing);
  * the issues ledger's outcomes become signals: a fix counts as accepted, an
    unfixed merge is a weak "ignored", a resolved thread is upgraded or
    dropped, a reopened issue withdraws the automatic signals;
  * forgetting a signal removes it, and only in its own workspace.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from src.review.learning import signals as sig
from tests.review.rules_db import rules_db

WS = "ws-a"
OTHER = "ws-b"
REPO = "github_acme-shop"
PR7 = sig.PRRef("github", "acme/shop", 7)
PR8 = sig.PRRef("github", "acme/shop", 8)
SNAP = sig.FindingSnapshot(
    title="Possible null dereference in user lookup", file_path="src/users/lookup.py",
    body="user may be None here", rule_id="defect.null", agent="defect",
    severity="warning", line=40)


def _rows(tmp_path, where: str = "1=1") -> list[tuple]:
    with sa.create_engine(f"sqlite:///{tmp_path}/rules.db").connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT signal, source, actor, weight, pr_number, workspace_id "
            f"FROM finding_signals WHERE {where} ORDER BY created_at, id"))]


async def test_the_same_verdict_twice_is_one_row(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        first = sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="Jane")
        again = sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="jane")
        assert (first, again) == ("created", "exists")
        assert _rows(tmp_path) == [("dismissed", "reply", "jane", 1.0, 7, WS)]


async def test_a_changed_mind_replaces_the_verdict_instead_of_adding_one(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="jane")
        sig.record_verdict(WS, REPO, SNAP, "accepted", "reply", pr=PR7, actor="jane")
        assert [r[0] for r in _rows(tmp_path)] == ["accepted"]


async def test_the_finding_is_the_same_one_on_another_line_and_another_pull_request(
        tmp_path, monkeypatch):
    moved = sig.FindingSnapshot(
        title=SNAP.title, file_path=SNAP.file_path, rule_id=SNAP.rule_id, line=88)
    async with rules_db(tmp_path, monkeypatch):
        sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="jane")
        sig.record_verdict(WS, REPO, moved, "dismissed", "reply", pr=PR8, actor="jane")
        with sa.create_engine(f"sqlite:///{tmp_path}/rules.db").connect() as conn:
            fps = {r[0] for r in conn.execute(sa.text("SELECT fingerprint FROM finding_signals"))}
        assert len(fps) == 1 and len(next(iter(fps))) == 64


async def test_a_listed_reviewer_teaches_nothing_by_any_of_their_names(tmp_path, monkeypatch):
    from src.db.models import RepoReviewPolicy

    async with rules_db(tmp_path, monkeypatch) as factory:
        async with factory() as s:
            s.add(RepoReviewPolicy(repo_slug=REPO, workspace_id=WS,
                                   learning_excluded_reviewers=["CI-Bot", "qa@example.com"]))
            await s.commit()
        by_login = sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="ci-bot")
        by_mail = sig.record_verdict(WS, REPO, SNAP, "dismissed", "ui", pr=PR7, actor="x1",
                                     also_known_as=["QA@example.com"])
        fine = sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="jane")
        elsewhere = sig.record_verdict(WS, "github_acme-api", SNAP, "dismissed", "reply",
                                       pr=PR7, actor="ci-bot")
        assert (by_login, by_mail, fine, elsewhere) == ("excluded", "excluded", "created", "created")


async def test_the_page_verdict_takes_its_pull_request_from_the_run_not_the_client(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        monkeypatch.setattr(sig, "pr_of_run", lambda run_id: (WS, PR7))
        assert sig.record_ui(WS, "run-1", "dismissed", actor="jane", snapshot=SNAP) == "created"
        assert _rows(tmp_path)[0][4] == 7
        assert sig.clear_ui(WS, "run-1", actor="jane", snapshot=SNAP) == 1
        assert _rows(tmp_path) == []


async def test_a_run_of_another_workspace_teaches_nothing(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        monkeypatch.setattr(sig, "pr_of_run", lambda run_id: (OTHER, PR7))
        assert sig.record_ui(WS, "run-1", "dismissed", actor="jane", snapshot=SNAP) == "invalid"
        assert sig.record_ui(WS, "gone", "dismissed", actor="jane", snapshot=None) == "invalid"
        assert _rows(tmp_path) == []


async def test_an_unknown_signal_or_source_is_refused(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        assert sig.record_signal(WS, REPO, SNAP, "loved", "reply", pr=PR7) == "invalid"
        assert sig.record_signal(WS, REPO, SNAP, "dismissed", "telepathy", pr=PR7) == "invalid"
        assert sig.record_signal(WS, REPO, sig.FindingSnapshot(title=""), "dismissed",
                                 "reply", pr=PR7) == "invalid"


# ─── outcomes from the issues ledger ─────────────────────────────────


async def _issue(factory, **over):
    from src.db.models import ReviewIssue

    fp = sig.snapshot_of(SNAP).fingerprint
    async with factory() as s:
        row = ReviewIssue(
            workspace_id=WS, repo_slug=REPO, fingerprint=fp, file_path=SNAP.file_path,
            title=SNAP.title, rule_id=SNAP.rule_id, agent=SNAP.agent, severity="warning",
            category="bug", pr_provider="github", pr_repo="acme/shop", pr_number=7, **over)
        s.add(row)
        await s.commit()
        return row.id, fp


def _outcome(kind, issue_id, fp):
    from src.review.outcome_hooks import IssueOutcome

    return IssueOutcome(
        kind=kind, workspace_id=WS, issue_id=issue_id, repo_slug=REPO, pr_provider="github",
        pr_repo="acme/shop", pr_number=7, fingerprint=fp, fixed_in_sha="abc123")


async def test_a_fix_counts_as_accepted(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        issue_id, fp = await _issue(factory)
        sig.on_issue_outcome(_outcome("implemented", issue_id, fp))
        assert [(r[0], r[1]) for r in _rows(tmp_path)] == [("implemented", "auto_next_commit")]
        assert sig.WEIGHTS["implemented"] == 1.0


async def test_an_unfixed_merge_is_a_weak_ignored_signal_that_feeds_only_the_rules(
        tmp_path, monkeypatch):
    from src.review.learning import similarity

    async with rules_db(tmp_path, monkeypatch) as factory:
        issue_id, fp = await _issue(factory)
        sig.on_issue_outcome(_outcome("unimplemented", issue_id, fp))
        assert [(r[0], r[1], r[3]) for r in _rows(tmp_path)] == [("ignored", "auto_merge", 0.3)]
        assert "ignored" not in similarity.JUDGED, "an ignored suggestion never hides a finding"


async def test_a_resolved_thread_is_upgraded_when_the_fix_follows(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        issue_id, fp = await _issue(factory)
        sig.record_signal(WS, REPO, SNAP, "resolved", "resolve", pr=PR7, actor="jane")
        assert _rows(tmp_path)[0][3] == 0.4
        sig.on_issue_outcome(_outcome("implemented", issue_id, fp))
        assert sorted(r[0] for r in _rows(tmp_path)) == ["implemented"]


async def test_a_resolved_thread_with_the_code_unchanged_becomes_a_weak_dismissal(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        issue_id, fp = await _issue(factory)
        sig.record_signal(WS, REPO, SNAP, "resolved", "resolve", pr=PR7, actor="jane")
        sig.on_issue_outcome(_outcome("unimplemented", issue_id, fp))
        assert sorted((r[0], r[3]) for r in _rows(tmp_path)) == [("dismissed", 0.4), ("ignored", 0.3)]


async def test_a_reopened_issue_withdraws_the_automatic_signals_and_keeps_the_people(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        issue_id, fp = await _issue(factory)
        sig.record_verdict(WS, REPO, SNAP, "accepted", "reply", pr=PR7, actor="jane")
        sig.on_issue_outcome(_outcome("implemented", issue_id, fp))
        sig.on_issue_outcome(_outcome("reopened", issue_id, fp))
        assert [(r[0], r[1]) for r in _rows(tmp_path)] == [("accepted", "reply")]


async def test_an_outcome_for_another_workspaces_issue_writes_nothing(tmp_path, monkeypatch):
    from src.review.outcome_hooks import IssueOutcome

    async with rules_db(tmp_path, monkeypatch) as factory:
        issue_id, fp = await _issue(factory)
        sig.on_issue_outcome(IssueOutcome(
            kind="implemented", workspace_id=OTHER, issue_id=issue_id, repo_slug=REPO,
            pr_provider="github", pr_repo="acme/shop", pr_number=7, fingerprint=fp))
        assert _rows(tmp_path) == []


async def test_the_listener_hears_the_ledger_without_the_ledger_importing_it(
        tmp_path, monkeypatch):
    from src.review import outcome_hooks as hooks

    sig.register_listener()
    assert sig.on_issue_outcome in hooks.listeners()


async def test_the_implementation_rate_is_the_ledgers_not_a_second_tally(
        tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.db.models import ReviewIssue

    async with rules_db(tmp_path, monkeypatch):
        assert sig.implementation_rate(WS)["rate"] is None
        merged = datetime.now(UTC) - timedelta(days=2)

        def issue(n, outcome, *, ws=WS, repo=REPO, dup=None, when=merged):
            return ReviewIssue(
                id=f"i{n}", workspace_id=ws, repo_slug=repo, fingerprint=f"{n:064x}",
                merged_at=when, close_outcome=outcome, dup_of=dup)

        with Session(create_engine(f"sqlite:///{tmp_path}/rules.db")) as s:
            s.add_all([
                issue(1, "implemented"), issue(2, "implemented"), issue(3, "unimplemented"),
                # said by a person or never shipped: reported elsewhere, not in the rate
                issue(4, "dismissed"), issue(5, "abandoned"),
                issue(6, "unimplemented", dup="i1"),
                issue(7, "unimplemented", when=merged - timedelta(days=40)),
                issue(8, "implemented", ws=OTHER),
                issue(9, "unimplemented", repo="github_acme-other"),
            ])
            s.commit()
        week = datetime.now(UTC) - timedelta(days=7)
        out = sig.implementation_rate(WS, since=week)
        assert (out["implemented"], out["ignored"], out["total"]) == (2, 2, 4)
        assert out["rate"] == 0.5
        assert sig.implementation_rate(WS, REPO, week)["total"] == 3
        assert sig.implementation_rate(WS, since=week, visible=[REPO])["total"] == 3
        assert sig.implementation_rate(WS, since=week, visible=[])["total"] == 0
        assert sig.implementation_rate(WS)["total"] == 5, "no window: the merge 40 days ago too"
        assert sig.implementation_rate(OTHER)["total"] == 1


async def test_forgetting_a_signal_removes_it_and_only_in_its_own_workspace(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        sig.record_verdict(WS, REPO, SNAP, "dismissed", "reply", pr=PR7, actor="jane")
        with sa.create_engine(f"sqlite:///{tmp_path}/rules.db").connect() as conn:
            sid = conn.execute(sa.text("SELECT id FROM finding_signals")).scalar_one()
        assert sig.forget_signal(OTHER, sid) is None
        assert len(_rows(tmp_path)) == 1
        gone = sig.forget_signal(WS, sid)
        assert gone is not None and gone["signal"] == "dismissed"
        assert _rows(tmp_path) == []


# ─── posted comments ─────────────────────────────────────────────────


def _full(snap=SNAP) -> str:
    return sig.snapshot_of(snap).fingerprint


async def test_a_posted_comment_leads_back_to_its_finding(tmp_path, monkeypatch):
    comments = [{"comment_id": 901, "path": SNAP.file_path, "line": 40,
                 "fingerprint": _full()[:16], "finding_key": _full()},
                {"comment_id": 902, "path": "x.py", "line": 1,
                 "fingerprint": "0" * 16, "finding_key": "0" * 64}]
    async with rules_db(tmp_path, monkeypatch):
        written = sig.record_posted(WS, PR7, [SNAP], comments, sha="abc123def456")
        assert written == 1, "a comment that matches no finding is skipped"
        again = sig.record_posted(WS, PR7, [SNAP], comments, sha="abc123def456")
        assert again == 0, "recording twice does not duplicate"
        found = sig.find_posted(WS, PR7, "901")
        assert found["title"] == SNAP.title and found["sha"] == "abc123def456"
        assert sig.find_posted(OTHER, PR7, "901") is None
        assert sig.find_posted(WS, PR8, "901") is None
        assert sig.find_posted_by_fingerprint(WS, PR7, _full()[:16])["comment_id"] == "901"
        assert sig.find_posted_by_fingerprint(WS, PR7, "not-hex") is None


async def test_the_comment_map_survives_the_provider_comment(tmp_path, monkeypatch):
    """Nothing here deletes a mapping when the provider comment is deleted, so
    a reply to a comment that was later removed still finds its finding."""
    async with rules_db(tmp_path, monkeypatch):
        sig.record_posted(WS, PR7, [SNAP], [{"comment_id": 5, "fingerprint": _full()[:16]}])
        assert sig.posted_for_pr(WS, PR7)[0]["comment_id"] == "5"
        assert not hasattr(sig, "forget_posted")


@pytest.mark.parametrize("name", ["record_signal", "record_verdict", "record_outcome",
                                  "record_posted", "on_issue_outcome"])
def test_a_learning_writer_never_raises_into_its_caller(name, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fn = getattr(sig, name)
    args = {
        "record_signal": (WS, REPO, SNAP, "dismissed", "reply"),
        "record_verdict": (WS, REPO, SNAP, "dismissed", "reply"),
        "record_outcome": (WS, REPO, SNAP, "implemented"),
        "record_posted": (WS, PR7, [SNAP], [{"comment_id": 1, "fingerprint": _full()[:16]}]),
        "on_issue_outcome": (_outcome("implemented", "i", "f"),),
    }[name]
    kwargs = {"pr": PR7, "actor": "jane"} if name == "record_verdict" else (
        {"pr": PR7} if name in ("record_signal", "record_outcome") else {})
    fn(*args, **kwargs)  # no database: it must say so quietly
