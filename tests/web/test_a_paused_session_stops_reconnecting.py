"""A paused session reconnected every two seconds, forever.

Measured against production before the fix: opening one paused session and
leaving the tab alone produced **36 SSE connections in 75 seconds**, with an
amber "Reconnecting…" badge over a conversation that was perfectly healthy and
simply waiting for somebody to type.

Two causes, and they compound:

  * the retry loop stopped only on `final`, which means "cannot continue at
    all". A paused session reports `final: false, resumable: true` — it
    continues when a person sends a turn, and never on its own. Nothing was
    coming, and the client kept asking.

  * the backoff reset on any frame, and `stream_end` is a frame. Every
    reconnect that found nothing reset the attempt counter, so the delay never
    grew past its two-second floor. The retry that was supposed to back off
    was the thing keeping itself hot.

WHAT MUST STILL RETRY, and why this cannot simply stop on `stream_end`: a
RUNNING session whose API restarted underneath it reports `live: false,
final: false, resumable: false`. That is the deploy case, and the server's own
comment says the client must reconnect through it. Three states, and the
middle one is the whole reason the first version got this wrong.

Read as source rather than run: this is React inside a Next.js page, and the
behaviour under test is a retry loop over an event stream. What is asserted is
the shape of the decision, not the pixels.
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[2] / "web" / "app" / "(app)"
        / "claude" / "[id]" / "page.tsx")


def _source() -> str:
    return PAGE.read_text(encoding="utf-8")


def _without_comments() -> str:
    """Comments explain the fix and name every flag in it."""
    text = re.sub(r"/\*.*?\*/", "", _source(), flags=re.S)
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


def test_the_retry_loop_stops_when_only_a_person_can_continue() -> None:
    code = _without_comments()
    stop = re.search(r"const stopped\s*=(.+?);", code, re.S)
    assert stop, "nothing decides when to stop retrying"
    body = stop.group(1)
    assert "resumable" in body, (
        "the retry loop does not stop on a resumable stream_end, so a paused "
        "session reconnects for as long as the tab is open"
    )
    assert "final" in body, "the retry loop no longer stops on a final stream"


def test_a_restarted_api_still_reconnects() -> None:
    """The state between the two: running, stream closed, nobody to type."""
    code = _without_comments()
    stop = re.search(r"const stopped\s*=(.+?);", code, re.S).group(1)
    assert "stream_end" in stop, "the stop condition ignores stream_end entirely"
    # A stop on stream_end ALONE would kill the deploy case.
    naked = re.search(r'ev\s*===\s*"stream_end"\s*\)?\s*;', stop)
    assert not naked, (
        "the client stops on any stream_end, which strands a running session "
        "whose API restarted under it — the case the server comment describes"
    )


def test_the_backoff_is_not_reset_by_an_empty_reconnect() -> None:
    code = _without_comments()
    reset = re.search(r'if \(ev !== "stream_end"\) attemptRef\.current = 0;', code)
    assert reset, (
        "attemptRef resets on every frame including stream_end, so a "
        "reconnect that finds nothing resets the backoff it was supposed to "
        "grow — 36 connections in 75 seconds, measured"
    )


def test_sending_a_turn_brings_the_stream_back() -> None:
    """Stopping the loop is only safe if typing restarts it."""
    code = _without_comments()
    send = re.search(r"const sendDraft = async \(\) => \{.+?\n  \};", code, re.S)
    assert send, "sendDraft is gone"
    assert "wakeRef.current" in send.group(0), (
        "the composer does not wake the stream, so a resumed session shows "
        "nothing until the page is reloaded — which is worse than the loop"
    )
    assert re.search(r"wakeRef\.current = \(\) => \{\s*doneRef\.current = false;",
                     code), (
        "the wake handle does not clear doneRef, so it cannot restart a loop "
        "that stopped"
    )
