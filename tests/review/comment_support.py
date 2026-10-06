"""Shared pieces of the comment-command tests: an event, a ledger, a provider."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.db.models import PRCommandEvent
from src.review.commands.events import CommentEvent
from src.review.commands.parser import ParsedCommand


def event(**over) -> CommentEvent:
    base = dict(
        provider="github", repo="acme/shop", pr_number=7, comment_id="c1",
        body="@celmis start-review", actor_id="101", actor_name="alice",
        actor_ids=("101", "alice"), repo_private=True, head_sha="abc123",
        base_ref="develop", pr_state="open", pr_title="Add totals",
    )
    return CommentEvent(**{**base, **over})


def command(name: str = "start-review", **over) -> ParsedCommand:
    return ParsedCommand(name, **over)


@pytest.fixture
def ledger_engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    PRCommandEvent.__table__.create(eng)
    yield eng
    eng.dispose()


class FakeProvider:
    """Records what the command handlers ask of a provider."""

    name = "github"

    def __init__(self, *, viewer=frozenset({"celmis-bot"}), permission="unknown",
                 participants=frozenset(), react=True):
        self.viewer = frozenset(viewer)
        self.permission = permission
        self.participants = frozenset(participants)
        self.react = react
        self.replies: list[str] = []
        self.updates: list[tuple[str, str]] = []
        self.reactions = 0
        self.closed = False

    def viewer_ids(self):
        return self.viewer

    def actor_permission(self, repo, *, actor_id="", actor_name=""):
        return self.permission

    def pr_participants(self, repo, pr_number):
        return self.participants

    def post_reply(self, ev, body):
        self.replies.append(body)
        return f"reply-{len(self.replies)}"

    def update_comment(self, repo, pr_number, comment_id, body, *, kind="issue"):
        self.updates.append((comment_id, body))
        return True

    def acknowledge(self, ev):
        if self.react:
            self.reactions += 1
        return self.react

    def close(self):
        self.closed = True


def routed_transport(routes: dict[str, object]) -> httpx.MockTransport:
    """Answers GETs by the end of the path; anything unlisted is a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                if isinstance(payload, int):
                    return httpx.Response(payload, json={})
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)
