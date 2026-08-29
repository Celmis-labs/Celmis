"""The Open button in an alert card pointed wherever the sender said.

`_alerts_page` built the link from the incoming request's Host. That request
comes from somebody else's monitoring, and the reverse proxy passes Host
through — Caddy overwrites X-Forwarded-Host, not Host. Measured against the
production box, an alert POSTed with

    Host: evil2.example.test

was delivered into the workspace's chat room as a card carrying this
product's branding, a title and body the sender wrote, and an Open button
on `http://evil2.example.test/alerts`. The only requirement is the ingest
token, which is handed to third-party monitoring on purpose.

Keyed on the property: nothing the sender writes can reach the link.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks

HOSTILE = "evil2.example.test"


class _Session:
    def add(self, row):  # noqa: D102
        pass

    async def commit(self):  # noqa: D102
        return None


class _Req:
    """An ingest request whose every sender-controlled field is hostile."""

    headers = {
        "host": HOSTILE,
        "x-forwarded-host": HOSTILE,
        "x-forwarded-proto": "https",
    }
    url = type("U", (), {"scheme": "https", "netloc": HOSTILE})()

    async def json(self):
        return {"title": "disk full", "body": "94% used", "severity": "warning"}


def _dispatched_link(monkeypatch, public_base_url: str | None) -> str | None:
    """Ingest one alert and return the link handed to the dispatcher."""
    from src import config
    from src.api.routers import alerts as mod

    monkeypatch.setattr(mod, "_load_secret", lambda ws: "s3cret")
    monkeypatch.setattr(
        config, "get_settings",
        lambda: type("S", (), {"public_base_url": public_base_url})(),
    )

    background = BackgroundTasks()
    asyncio.run(mod.ingest("ws-1.s3cret", _Req(), background, _Session()))

    assert background.tasks, "the alert was not queued for dispatch at all"
    # add_task(_dispatch_alerts, workspace_id, parsed, link)
    return background.tasks[0].args[2]


@pytest.mark.parametrize(
    "configured", [None, "", "   ", "http://celmis.example.com", "celmis.example.com"],
)
def test_the_hostile_host_never_reaches_the_link(monkeypatch, configured) -> None:
    link = _dispatched_link(monkeypatch, configured)
    assert HOSTILE not in (link or ""), (
        f"PUBLIC_BASE_URL={configured!r} produced {link!r} — the sender's own "
        f"Host header is in the address that goes to the chat room"
    )


def test_a_configured_origin_is_the_one_that_travels(monkeypatch) -> None:
    assert _dispatched_link(monkeypatch, "http://celmis.example.com") == (
        "http://celmis.example.com/alerts"
    )
    # A trailing slash is the operator's, not a second one in the URL.
    assert _dispatched_link(monkeypatch, "https://celmis.example.com/") == (
        "https://celmis.example.com/alerts"
    )


def test_unset_means_no_link_rather_than_a_guessed_one(monkeypatch) -> None:
    """A card without a button still carries the alarm; a bad card carries nothing.

    Google Chat rejects a card whose button is not an absolute URL, and the
    rejected card IS the alert — so a relative path here is worse than None.
    """
    for unset in (None, "", "   "):
        assert _dispatched_link(monkeypatch, unset) is None, (
            f"PUBLIC_BASE_URL={unset!r} produced a link where there is no "
            f"trustworthy origin to build one from"
        )


def test_an_origin_without_a_scheme_is_not_a_link(monkeypatch) -> None:
    assert _dispatched_link(monkeypatch, "celmis.example.com") is None
    assert _dispatched_link(monkeypatch, "localhost:3000") is None


def test_the_alert_itself_still_goes_out(monkeypatch) -> None:
    """Refusing the link must never cost the alarm."""
    from src import config
    from src.api.routers import alerts as mod

    monkeypatch.setattr(mod, "_load_secret", lambda ws: "s3cret")
    monkeypatch.setattr(
        config, "get_settings", lambda: type("S", (), {"public_base_url": None})(),
    )
    background = BackgroundTasks()
    out = asyncio.run(mod.ingest("ws-1.s3cret", _Req(), background, _Session()))
    assert out["created"] == 1
    assert background.tasks, "no link meant no dispatch — the alarm was lost"
