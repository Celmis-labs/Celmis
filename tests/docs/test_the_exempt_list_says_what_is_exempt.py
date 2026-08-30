"""The middleware docstring listed "webhooks" as exempt from rate limiting.

It was true when written, of the git webhooks it was written for, and it
stayed on the page after `_EXEMPT_PREFIXES` was narrowed to the three
provider routes. `/webhook/alerts/{token}` has no signature over the body and
no dedup — the token in the path is compared and every delivery is stored and
fanned out to a chat room — so it is rate-limited like anything else. A reader
of the docstring would have concluded the opposite, which is how the
exemption got written in the first place.

Keyed on the property: the paths the docstring calls exempt are the paths the
code exempts, in both directions.
"""

from __future__ import annotations

import re

from src.api import middleware


def _documented() -> set[str]:
    """Paths named in the 'Exempt:' paragraph, which ends at the blank line.

    Scoped to that paragraph on purpose. The prose below it names
    `/webhook/alerts/{token}` precisely to say it is NOT exempt, and a scan of
    the whole docstring would read that as a claim of exemption — the same
    kind of mistake this test exists to catch.
    """
    doc = middleware.__doc__ or ""
    match = re.search(r"Exempt:(.*?)\n\s*\n", doc, re.S)
    assert match, "the docstring no longer has an 'Exempt:' paragraph"
    return set(re.findall(r"/[a-zA-Z0-9._/-]*", match.group(1)))


def test_every_exempt_path_is_written_down() -> None:
    missing = set(middleware._EXEMPT_PREFIXES) - _documented()
    assert not missing, (
        f"exempt in code and absent from the docstring: {sorted(missing)}. "
        f"An undocumented exemption is how one gets added without anybody "
        f"weighing it."
    )


def test_nothing_is_documented_as_exempt_that_is_not() -> None:
    extra = _documented() - set(middleware._EXEMPT_PREFIXES)
    assert not extra, (
        f"the docstring calls these exempt and the code does not: "
        f"{sorted(extra)}"
    )


def test_the_alert_ingest_is_not_exempt() -> None:
    """The specific route the old wording swept in."""
    assert not middleware._is_exempt("/webhook/alerts/ws-1.secret")
    assert middleware._is_exempt("/webhook/github/ws-1")


def test_the_docstring_does_not_call_a_fixed_window_sliding() -> None:
    """Both backends floor the clock to the window and count inside it.

    Which allows a full allowance at the end of one window and another at the
    start of the next. "Sliding" promises exactly that cannot happen.
    """
    doc = (middleware.__doc__ or "").lower()
    assert "sliding-window counters" not in doc, (
        "the module docstring calls the limiter sliding-window while both "
        "_MemoryWindow and _RedisWindow compute `now - now % _WINDOW_SECONDS`"
    )
    for backend in (middleware._MemoryWindow, middleware._RedisWindow):
        import inspect
        assert "_WINDOW_SECONDS" in inspect.getsource(backend.hit)
