"""`body_md` is markdown, and Google Chat does not read markdown.

Every other adapter in `dispatch.py` is fed the same string and every other
one is right to be: Slack takes mrkdwn, Discord takes markdown. A Google Chat
`textParagraph` renders a small HTML subset and prints everything else
verbatim — so the review card that says

    **0** critical · **4** error · **0** warn · **2** info

arrived on somebody's phone with the asterisks in it. The numbers were
correct and the punctuation was shouting.

The second assertion here is the one that matters more. A notification body
carries text this installation did not write — an alert title from somebody
else's monitoring, a pull request title from a contributor. Converting
markdown to markup without escaping first would let that text put markup into
a card delivered under this product's branding, which is the same shape as
the alert-link spoof this repository already fixed once.
"""

from __future__ import annotations

import pytest

from src.notifications.dispatch import _md_to_chat


@pytest.mark.parametrize("markdown,rendered", [
    ("**0** critical · **4** error", "<b>0</b> critical · <b>4</b> error"),
    ("see `src/ledger.js` line 5", "see <code>src/ledger.js</code> line 5"),
    ("[PR #7](https://example.test/p/7)",
     '<a href="https://example.test/p/7">PR #7</a>'),
])
def test_the_markup_this_product_sends_is_converted(markdown, rendered) -> None:
    assert _md_to_chat(markdown) == rendered


def test_no_asterisk_survives_a_bold_run() -> None:
    """The literal symptom, kept as its own case because it is the report."""
    out = _md_to_chat("**0** critical · **4** error · **0** warn · **2** info")
    assert "*" not in out, f"asterisks still reach the card: {out}"


def test_a_sender_cannot_put_markup_in_our_card() -> None:
    hostile = '<a href="https://evil.test">click</a> <b>urgent</b>'
    out = _md_to_chat(hostile)
    assert "<a href" not in out and "<b>" not in out, (
        f"text from a sender rendered as markup inside a card carrying this "
        f"product's branding: {out}"
    )
    assert "&lt;a href" in out


def test_ordinary_prose_is_left_alone() -> None:
    """A lone asterisk is punctuation, not an unclosed emphasis run."""
    for text in ("2 * 3 = 6", "see the note * below", "no markup at all"):
        assert _md_to_chat(text) == text, text


def test_the_multiplication_of_adapters_did_not_leak() -> None:
    """Slack and Discord still get markdown; only Chat gets markup."""
    import inspect

    from src.notifications import dispatch

    for name in ("_post_slack", "_post_discord"):
        src = inspect.getsource(getattr(dispatch, name))
        assert "_md_to_chat" not in src, (
            f"{name} converts markdown to HTML, but its transport reads "
            f"markdown — the card would show the tags instead"
        )
    assert "_md_to_chat" in inspect.getsource(dispatch._post_google_chat)
