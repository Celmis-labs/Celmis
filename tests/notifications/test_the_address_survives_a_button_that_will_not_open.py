"""A card's link has to be reachable even when the button is refused.

Google Chat will not open a plain-http link to a bare IP address. The
button renders inert, and following it reports the site as unavailable —
while the address itself was correct and served a 307 to the login page
the whole time. Reached by IP is how this product is installed by anyone
who has not put a hostname in front of it, so the alert cannot depend on
the button being allowed to work.

Keyed on the property: whatever link a Google Chat card carries, the same
address is in the card as text, which no link policy can switch off.
"""

from __future__ import annotations

import json

from src.notifications import dispatch

LINK = "http://198.51.100.7/alerts"


def _card(monkeypatch, link_url: str | None) -> dict:
    sent: dict = {}

    def fake_post(url, payload, headers=None):
        sent["url"] = url
        sent["payload"] = payload

    monkeypatch.setattr(dispatch, "_post_json", fake_post)
    dispatch._post_google_chat(
        {"webhook_url": "https://chat.googleapis.com/v1/spaces/x"},
        title="Disk almost full",
        body_md="94% used on /dev/sda2",
        severity="warning",
        link_url=link_url,
    )
    return sent["payload"]


def _widgets(card: dict) -> list[dict]:
    return card["cardsV2"][0]["card"]["sections"][0]["widgets"]


def test_the_address_is_in_the_card_as_text(monkeypatch) -> None:
    card = _card(monkeypatch, LINK)
    texts = [
        w["textParagraph"]["text"] for w in _widgets(card) if "textParagraph" in w
    ]
    assert any(LINK in t for t in texts), (
        f"the card links to {LINK} but never spells it out; when Google Chat "
        f"refuses the button there is no way left to reach it. Widgets: "
        f"{json.dumps(_widgets(card))[:400]}"
    )


def test_the_button_is_still_there(monkeypatch) -> None:
    """The text is an addition. Where the button works it is the better path."""
    card = _card(monkeypatch, LINK)
    buttons = [w for w in _widgets(card) if "buttonList" in w]
    assert buttons, "the Open button was dropped"
    assert (
        buttons[0]["buttonList"]["buttons"][0]["onClick"]["openLink"]["url"] == LINK
    )


def test_no_link_means_no_stray_widgets(monkeypatch) -> None:
    """A card with nothing to link to must not grow an empty line."""
    card = _card(monkeypatch, None)
    widgets = _widgets(card)
    assert len(widgets) == 1, f"expected only the body, got {json.dumps(widgets)[:300]}"
    assert "94% used" in widgets[0]["textParagraph"]["text"]
