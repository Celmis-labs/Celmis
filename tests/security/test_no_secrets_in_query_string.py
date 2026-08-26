"""A form that carries a secret must not be able to submit it as a GET.

Found on production: the login URL read

    /login?email=someone%40example.com&password=<their actual password>

The handler calls preventDefault, so this never happens once React has
hydrated. Before that it is a plain HTML form — and a form with no `method`
submits GET, putting every named field in the query string. A slow first paint
is the whole exploit. The password then lives in the browser history, the
reverse proxy's access log and the Referer header of the next request, and this
deployment is plain HTTP on a bare IP.

`method="post"` costs nothing and closes it: the fields travel in a body the
browser never records, hydrated or not.

The test scans the source rather than driving a browser, because the failure is
a missing attribute, and a browser test would only catch it in the narrow
window before hydration — the same window that made this hard to notice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web" / "app"

#: A field whose value must never appear in a URL.
SECRET_FIELDS = re.compile(
    r'\b(?:name|id)=["\'](?:password|new_password|token|api_key|secret|'
    r'client_secret|webhook_url)["\']',
    re.IGNORECASE,
)

FORM_OPEN = re.compile(r"<form\b[^>]*>", re.S)


def _pages() -> list[Path]:
    return sorted(WEB.rglob("page.tsx"))


def test_there_are_pages_to_scan() -> None:
    """Guards the guard — a bad glob would pass everything silently."""
    assert len(_pages()) > 10, len(_pages())


@pytest.mark.parametrize("page", _pages(), ids=lambda p: str(p.relative_to(WEB)))
def test_a_form_holding_a_secret_posts(page: Path) -> None:
    text = page.read_text()
    if not SECRET_FIELDS.search(text):
        return  # nothing sensitive on this page

    forms = FORM_OPEN.findall(text)
    assert forms, f"{page.name} names a secret field but has no <form>"
    missing = [f for f in forms if 'method="post"' not in f.lower()]
    assert not missing, (
        f"{page.relative_to(WEB)} has a form without method=\"post\" while the "
        f"page carries a secret input — a pre-hydration submit would put it in "
        f"the URL:\n" + "\n".join(m[:120] for m in missing)
    )


def test_the_login_form_specifically() -> None:
    """The page this was found on, pinned by name so it cannot regress quietly."""
    login = (WEB / "login" / "page.tsx").read_text()
    forms = FORM_OPEN.findall(login)
    assert len(forms) >= 2, "expected a sign-in and a sign-up form"
    for form in forms:
        assert 'method="post"' in form.lower(), form[:120]
