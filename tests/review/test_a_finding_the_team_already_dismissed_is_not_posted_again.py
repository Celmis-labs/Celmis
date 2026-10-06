"""The learned filter: a finding the team judged before, and how sure it is.

  * one pull request cannot train it alone, except for an exact repeat;
  * two people on two pull requests can; somebody who said "right" blocks it;
  * a critical finding and a `proven` one are never hidden;
  * older signals weigh less (half-life), a duplicate-reason dismissal says
    nothing against the finding;
  * `shadow` keeps everything and reports, `on` removes, `off` does nothing;
  * another repository, another workspace never feed the decision, in the
    table or in the vector collection;
  * a broken vector store or a collection of another width degrades to the
    title tiers instead of failing the review.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.review.learning import signals as sig
from src.review.learning import similarity as sim
from src.review.learning import suppress
from tests.review.learning_db import FakeQdrant, hash_embed
from tests.review.rules_db import rules_db

WS, OTHER, REPO = "ws-a", "ws-b", "github_acme-shop"
NOW = datetime(2026, 10, 1, tzinfo=UTC)
TITLE = "Possible null dereference in user lookup"


def finding(title=TITLE, path="src/users/lookup.py", rule="defect.null",
            severity="warning", evidence="inferred", body="user may be None here"):
    return SimpleNamespace(title=title, file_path=path, rule_id=rule, agent="defect",
                           severity=severity, evidence_kind=evidence, body=body, line=40)


def view(i, *, signal="dismissed", pr=1, actor="jane", age=0, weight=1.0, reason="",
         title=TITLE, path="src/users/lookup.py", rule="defect.null"):
    snap = sig.snapshot_of(finding(title=title, path=path, rule=rule))
    return sim.SignalView(
        id=f"s{i}", fingerprint=snap.fingerprint, file_path=path, title=title, rule_id=rule,
        category=snap.category, signal=signal, source="reply", weight=weight, reason=reason,
        actor=actor, pr_provider="github", pr_repo="acme/shop", pr_number=pr,
        created_at=NOW - timedelta(days=age))


def decide(findings, views, **kw):
    return suppress.decide(findings, views, min_dismissals=2.0, half_life_days=90,
                           now=NOW, **kw)


def test_one_person_on_one_pull_request_cannot_train_the_filter_on_a_similar_finding():
    reworded = finding(title="Possible null dereference in the user lookup")
    [d] = decide([reworded], [view(1)])
    assert (d.tier, d.suppress) == (2, False)


def test_an_exact_repeat_is_hidden_after_one_dismissal():
    [d] = decide([finding()], [view(1)])
    assert (d.tier, d.needed, d.suppress) == (1, 1.0, True)


def test_a_dismissal_from_last_week_still_counts_as_a_whole_one():
    [d] = decide([finding()], [view(1, age=7)])
    assert d.suppress and 0.9 < d.s_dis < 1.0
    [two] = decide([finding(title="Possible null dereference in the user lookup")],
                   [view(1, pr=1, age=7), view(2, pr=2, actor="omar", age=7)])
    assert two.suppress


def test_two_people_on_two_pull_requests_hide_a_similar_finding():
    reworded = finding(title="Possible null dereference in the user lookup")
    [d] = decide([reworded], [view(1, pr=1, actor="jane"), view(2, pr=2, actor="omar")])
    assert d.suppress and d.tier == 2


def test_the_same_person_on_the_same_pull_request_counts_once():
    reworded = finding(title="Possible null dereference in the user lookup")
    [d] = decide([reworded], [view(1, pr=1), view(2, pr=1)])
    assert (d.s_dis, d.n_dis) == (1.0, 1) and not d.suppress


def test_somebody_who_said_it_was_right_blocks_the_hiding():
    views = [view(1, pr=1, actor="jane"), view(2, pr=2, actor="omar"),
             view(3, signal="accepted", pr=3, actor="li")]
    [d] = decide([finding()], views)
    assert (d.s_dis, d.s_acc) == (2.0, 1.0) and d.suppress, "one voice against two is not a veto"
    views.append(view(5, signal="accepted", pr=5, actor="kim"))
    [tied] = decide([finding()], views)
    assert not tied.suppress, "two against two is not enough"


def test_a_critical_finding_and_a_proven_one_are_never_hidden():
    views = [view(1, pr=1, actor="jane"), view(2, pr=2, actor="omar")]
    crit, proven, plain = decide(
        [finding(severity="critical"), finding(evidence="proven"), finding()], views)
    assert (crit.protected, proven.protected, plain.protected) == ("critical", "proven", "")
    assert (crit.suppress, proven.suppress, plain.suppress) == (False, False, True)


def test_older_signals_weigh_less_by_the_half_life():
    assert suppress.decay(NOW - timedelta(days=90), NOW, 90) == 0.5
    assert suppress.decay(NOW - timedelta(days=180), NOW, 90) == 0.25
    assert suppress.decay(None, NOW, 90) == 1.0
    assert suppress.decay(NOW + timedelta(days=3), NOW, 90) == 1.0, "a future date is not a boost"
    reworded = finding(title="Possible null dereference in the user lookup")
    [old] = decide([reworded], [view(1, pr=1, age=270), view(2, pr=2, actor="omar", age=270)])
    assert not old.suppress, "two dismissals three half-lives old are worth a quarter"
    [aged] = decide([reworded], [view(1, pr=1, age=90), view(2, pr=2, actor="omar", age=90)])
    assert aged.suppress, "a half-life old still counts as half a vote each"


def test_a_dismissal_for_being_a_duplicate_says_nothing_against_the_finding():
    [d] = decide([finding()], [view(1, reason="duplicate")])
    assert (d.tier, d.suppress) == (0, False)


def test_an_ignored_or_resolved_signal_never_decides():
    [d] = decide([finding()], [view(1, signal="ignored", weight=0.3),
                               view(2, signal="resolved", weight=0.4, pr=2)])
    assert not d.suppress and d.s_dis == 0.0


def test_a_similar_title_in_another_directory_or_of_another_rule_is_another_finding():
    reworded = finding(title="Possible null dereference in the user lookup")
    elsewhere = view(1, path="src/billing/lookup.py", pr=1)
    other_rule = view(2, rule="style.naming", pr=2, actor="omar")
    [d] = decide([reworded], [elsewhere, other_rule])
    assert d.tier == 0 and not d.suppress


# ─── the stage, against a database and a vector store ────────────────


async def _teach(*, count=2, ws=WS, repo=REPO, title=TITLE, path="src/users/lookup.py",
                 body="user may be None here"):
    for n in range(count):
        sig.record_verdict(
            ws, repo, sig.snapshot_of(finding(title=title, path=path, body=body)),
            "dismissed", "reply",
            pr=sig.PRRef("github", "acme/shop", n + 1), actor=f"person{n}")


def _apply(findings, mode="on", ws=WS, repo=REPO, **kw):
    return suppress.apply(findings, workspace_id=ws, repo_slug=repo, mode=mode,
                          embed=hash_embed, **kw)


async def test_on_removes_what_was_dismissed_and_keeps_the_rest(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=1)
        fresh = finding(title="Hard-coded timeout in the retry loop", path="src/net/retry.py",
                        rule="perf.timeout")
        out = _apply([finding(), fresh], client=FakeQdrant())
        assert out.kept == [fresh] and len(out.hidden) == 1 and not out.would_hide
        assert out.items[0]["tier"] == 1 and out.items[0]["title"] == TITLE


async def test_shadow_keeps_everything_and_reports_what_it_would_have_hidden(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=1)
        f = finding()
        out = _apply([f], mode="shadow", client=FakeQdrant())
        assert out.kept == [f] and not out.hidden and out.would_hide == [f]


async def test_off_does_nothing_and_does_not_touch_the_database(tmp_path, monkeypatch):
    f = finding()
    out = suppress.apply([f], workspace_id=WS, repo_slug=REPO, mode="off")
    assert out.kept == [f] and out.skipped == "off"


async def test_nothing_is_hidden_on_a_repository_nobody_has_judged(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        out = _apply([finding()], client=FakeQdrant())
        assert out.hidden == [] and "nothing dismissed" in out.skipped


async def test_a_reworded_finding_is_found_by_its_meaning_when_the_titles_differ(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        story = ("the lookup may return None when the account was deleted and the caller "
                 "dereferences the result without checking it first")
        await _teach(count=2, title="Unchecked optional", body=story)
        q = FakeQdrant()
        same_meaning = finding(title="Missing guard", rule="defect.guard", body=story)
        out = _apply([same_meaning], client=q)
        assert q.points, "the signals were embedded lazily, on first need"
        assert out.hidden == [same_meaning] and out.items[0]["tier"] == 3


async def test_another_repository_and_another_workspace_teach_nothing(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=2, repo="github_acme-api")
        await _teach(count=2, ws=OTHER)
        q = FakeQdrant()
        out = _apply([finding()], client=q)
        assert out.hidden == [] and out.kept
        # the vector query, when it ran, carried both the tenant and the repository
        for flt in q.queries:
            keys = {c.key for c in flt.must}
            assert "repo_slug" in keys and len(keys) >= 2


async def test_vectors_of_two_workspaces_never_meet(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=2, ws=OTHER)
        q = FakeQdrant()
        sim.index_pending(OTHER, REPO, client=q, embed=hash_embed)
        assert q.points
        hits = sim.query_similar(WS, REPO, ["null dereference user lookup"], threshold=0.0,
                                 client=q, embed=hash_embed)
        assert hits == [[]]
        mine = sim.query_similar(OTHER, REPO, ["null dereference user lookup"], threshold=0.0,
                                 client=q, embed=hash_embed)
        assert mine[0]


async def test_a_broken_vector_store_degrades_to_the_title_tiers(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=1)
        out = _apply([finding()], client=FakeQdrant(broken=True))
        assert len(out.hidden) == 1 and out.degraded


async def test_a_collection_of_another_width_degrades_instead_of_failing(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=1)
        out = _apply([finding()], client=FakeQdrant(width=1536))
        assert len(out.hidden) == 1 and out.degraded == "CollectionWidthMismatch"


async def test_a_failing_stage_keeps_every_finding(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(sim, "load_signals", boom)
    f = finding()
    out = suppress.apply([f], workspace_id=WS, repo_slug=REPO, mode="on", embed=hash_embed)
    assert out.kept == [f] and out.skipped.startswith("failed")


async def test_embedding_runs_once_per_signal(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=2)
        q = FakeQdrant()
        assert sim.index_pending(WS, REPO, client=q, embed=hash_embed) == 2
        assert sim.index_pending(WS, REPO, client=q, embed=hash_embed) == 0


async def test_forgetting_a_signal_is_possible_after_it_was_embedded(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        await _teach(count=1)
        q = FakeQdrant()
        sim.index_pending(WS, REPO, client=q, embed=hash_embed)
        [pid] = list(q.points)
        sim.delete_vectors([pid], client=q)
        assert q.points == {}
