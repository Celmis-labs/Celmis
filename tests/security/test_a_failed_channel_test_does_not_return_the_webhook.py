"""Pressing Test on a misconfigured channel answered with the credential.

A Google Chat webhook URL carries `key` and `token` in its query string — the
URL *is* the credential. `httpx` puts the request URL in its error string, and
the endpoint returned `str(exc)` verbatim, so a failed test replied with the
secret it was testing: into a toast in the browser, into anything that logs the
response, and into any screenshot of that page.

Found by pressing the button. The channel had been saved with the wrong kind —
a Google Chat URL under `kind: slack`, which the form accepted without a word —
so the send failed with 400 and the 400 came back carrying everything.

Two lessons, and the second is the durable one: a URL is not always an address.
Sometimes it is a password with a hostname attached, and error paths are where
those escape, because nobody reads an error message expecting to find one.
"""

from __future__ import annotations

import pytest

from src.api.routers.intel import _kind_matches_url, _redact_url

CHAT = ("https://chat.googleapis.com/v1/spaces/AAQAmv3bD04/messages"
        "?key=AIza-EXAMPLE-KEY&token=EXAMPLE-TOKEN")
SLACK = "https://hooks.slack.com/services/T000/B000/XXXXEXAMPLE"


def test_the_exact_url_is_gone():
    msg = f"Client error '400 Bad Request' for url '{CHAT}'"

    out = _redact_url(msg, CHAT)

    assert CHAT not in out
    assert "AIza-EXAMPLE-KEY" not in out
    assert "EXAMPLE-TOKEN" not in out


def test_the_status_survives():
    """An operator needs to know it was a 400 and not a timeout. Redacting the
    whole message would trade one useless answer for another."""
    msg = f"Client error '400 Bad Request' for url '{CHAT}'"

    out = _redact_url(msg, CHAT)

    assert "400" in out
    assert "Bad Request" in out


def test_a_url_we_never_stored_is_redacted_too():
    """A client library is free to quote a redirect target. The shape has to be
    handled, not only the one string we know about."""
    other = "https://hooks.slack.com/services/T111/B111/SECRETPART?x=1"
    msg = f"redirected to {other} and failed"

    out = _redact_url(msg, CHAT)

    assert "SECRETPART?x=1" not in out


def test_the_path_of_a_stored_url_goes_even_without_its_query():
    """Some errors quote the URL with the query already stripped. The space id
    is not a secret, but it identifies the room, and nothing needs it here."""
    msg = f"connection refused: {CHAT.split('?')[0]}"

    out = _redact_url(msg, CHAT)

    assert "AAQAmv3bD04" not in out


@pytest.mark.parametrize("url", [CHAT, SLACK])
def test_it_never_raises_on_anything_it_is_given(url):
    """This runs inside an except block. It failing there would replace a
    handled error with an unhandled one."""
    for msg in ("", "no url here", url, f"x {url} y", "https://", "?"):
        assert isinstance(_redact_url(msg, url), str)


def test_the_reply_is_bounded():
    """A provider that echoes the whole request body would otherwise put it in
    a toast."""
    assert len(_redact_url("x" * 5000, CHAT)) <= 400


def test_the_endpoint_uses_it():
    """Keyed on the call, not on the word: a comment naming the helper must not
    satisfy this."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src/api/routers/intel.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
              and n.name == "test_channel")
    called = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }

    assert "_redact_url" in called, "the error goes back unredacted"


# ─── and the mistake that produced it ────────────────────────────────


class TestTheKindMustMatchTheHost:
    """A Google Chat URL saved under `kind: slack` was accepted by every layer.

    The pattern allows both values, the URL is a valid string, the row stores
    fine. It fails at the first send, with a 400 from a provider that was never
    asked — and until somebody presses Test, the only symptom is alerts that
    quietly never arrive. Which is the worst symptom an alerting channel can
    have, because its whole job is to be the thing that tells you.
    """

    def test_google_chat_url_under_slack_is_caught(self):
        assert _kind_matches_url("slack", CHAT) == "google_chat"

    def test_slack_url_under_google_chat_is_caught(self):
        assert _kind_matches_url("google_chat", SLACK) == "slack"

    def test_the_matching_pair_passes(self):
        assert _kind_matches_url("google_chat", CHAT) is None
        assert _kind_matches_url("slack", SLACK) is None

    def test_webhook_accepts_anything(self):
        """`webhook` means "an endpoint of my own" and has no host to check —
        including, deliberately, a provider's own host behind a proxy."""
        assert _kind_matches_url("webhook", CHAT) is None
        assert _kind_matches_url("webhook", "https://example.com/hook") is None

    def test_an_unknown_host_is_not_second_guessed(self):
        """A self-hosted Mattermost is Slack-compatible and lives anywhere.
        Guessing there would block a legitimate setup."""
        assert _kind_matches_url("slack", "https://mm.example.com/hooks/abc") is None
