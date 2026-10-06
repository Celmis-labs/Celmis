"""`command_permission`: who may command the bot on a repository.

The worker asks the provider; a provider that cannot tell never grants more
than the participants rule does.
"""

from __future__ import annotations

import pytest

from src.review.commands import gate, ledger
from src.review.commands.handlers import CommandContext, _run
from tests.review.comment_support import (  # noqa: F401
    FakeProvider,
    command,
    event,
    ledger_engine,
)


def test_anyone_who_can_comment_on_a_private_repository_may_command():
    assert gate.authorize(event(repo_private=True), FakeProvider(), "repo_access").allowed


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_github_member_of_a_public_repository_may_command(assoc):
    ev = event(repo_private=False, actor_assoc=assoc)
    assert gate.authorize(ev, FakeProvider(), "repo_access").allowed


@pytest.mark.parametrize("assoc", ["NONE", "CONTRIBUTOR", "FIRST_TIMER", ""])
def test_a_stranger_on_a_public_repository_may_not(assoc):
    ev = event(repo_private=False, actor_assoc=assoc)
    verdict = gate.authorize(ev, FakeProvider(permission="none"), "repo_access")
    assert not verdict.allowed and verdict.reason


def test_a_person_with_write_access_may_even_without_an_association():
    ev = event(repo_private=False, actor_assoc="")
    assert gate.authorize(ev, FakeProvider(permission="write"), "repo_access").allowed


def test_read_access_alone_is_what_every_stranger_has_on_a_public_repository():
    ev = event(repo_private=False)
    assert not gate.authorize(ev, FakeProvider(permission="read"), "repo_access").allowed


def test_a_participant_of_the_pull_request_may_command_it_when_the_provider_cannot_say():
    ev = event(repo_private=False)
    provider = FakeProvider(permission="unknown", participants={"101"})
    assert gate.authorize(ev, provider, "repo_access").allowed


def test_an_unknown_repository_visibility_is_treated_as_public():
    assert not gate.authorize(event(repo_private=None), FakeProvider(), "repo_access").allowed


def test_participants_mode_lets_only_the_author_and_reviewers_in():
    ev = event(repo_private=True)
    assert not gate.authorize(ev, FakeProvider(participants={"someone"}),
                              "participants").allowed
    assert gate.authorize(ev, FakeProvider(participants={"alice"}), "participants").allowed


def test_anyone_mode_lets_everyone_in():
    assert gate.authorize(event(repo_private=False), FakeProvider(), "anyone").allowed


def test_an_unknown_mode_falls_back_to_repository_access():
    ev = event(repo_private=False)
    assert not gate.authorize(ev, FakeProvider(), "wide-open").allowed


def test_an_author_nobody_can_identify_is_refused():
    ev = event(actor_id="", actor_ids=())
    assert not gate.authorize(ev, FakeProvider(), "repo_access").allowed


def test_a_provider_that_raises_does_not_open_the_door():
    class Broken(FakeProvider):
        def actor_permission(self, *a, **k):
            raise RuntimeError("boom")

        def pr_participants(self, *a, **k):
            raise RuntimeError("boom")

    assert not gate.authorize(event(repo_private=False), Broken(), "repo_access").allowed


def _ctx(provider, **over):
    return CommandContext(
        ev=event(repo_private=False), provider=provider, command=command(),
        settings={"command_permission": "repo_access"}, workspace_id="ws", user_id="u",
        **over)


def test_a_refused_command_is_recorded_as_denied_and_answered_once(
        ledger_engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr("src.review.issues._ENGINE", ledger_engine)
    provider = FakeProvider(permission="none")
    ctx = _ctx(provider)
    assert _run(ctx, {}) == "denied"
    assert len(provider.replies) == 1 and "alice" in provider.replies[0]
    ledger.claim("ws", "github", "acme/shop", 7, "c1", command="start-review",
                 actor_id="101", status="denied", engine=ledger_engine)
    assert _run(_ctx(provider), {}) == "denied"
    assert len(provider.replies) == 1  # said once, then silent
