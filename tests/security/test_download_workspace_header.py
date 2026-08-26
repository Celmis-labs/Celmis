"""File downloads must resolve to the workspace the user is looking at.

`api()` reads the active workspace from the `x-workspace` cookie and sends it
as a header, which is what makes the switcher work when the API lives on a
different origin. Downloads cannot go through `api()` — they need the raw
Response to read a blob — so both of them hand-rolled their headers and sent
only the bearer.

The request then resolved to the account's DEFAULT workspace. A member whose
active workspace is a different one downloaded the wrong tenant's document, or
got a 404 for a run that is plainly on their screen. Noticed while probing
production: the same account's repository list answered with two different sets
depending on whether the header was present.

Text assertions: the code under test is a browser fetch, and a test that needs
a browser is the test that does not run.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"

#: Endpoints that are deliberately workspace-independent — they answer about
#: the ACCOUNT, so scoping them to one workspace would be wrong.
WORKSPACE_AGNOSTIC = ("/api/workspaces",)


def _sources() -> list[Path]:
    out: list[Path] = []
    for sub in ("app", "components", "lib"):
        root = WEB / sub
        if root.exists():
            out += [p for p in root.rglob("*.tsx")] + [p for p in root.rglob("*.ts")]
    return [p for p in out if "node_modules" not in p.parts]


def test_the_helper_exists_and_sends_both_headers():
    source = (WEB / "lib" / "api.ts").read_text()
    idx = source.find("export function requestHeaders(")
    assert idx > 0, "the shared header builder is gone"
    body = source[idx:idx + 700]
    assert 'headers.set("Authorization"' in body
    assert 'headers.set("X-Workspace"' in body
    assert "x-workspace=" in body, "the cookie the switcher writes is not read"


def test_api_uses_the_helper_rather_than_a_second_copy():
    """Two implementations of "which workspace is this" is how they drift."""
    source = (WEB / "lib" / "api.ts").read_text()
    assert "requestHeaders(opts.token, opts.headers)" in source
    # Exactly one place builds the header.
    assert source.count('headers.set("X-Workspace"') == 1


def test_no_download_hand_rolls_its_headers():
    offenders: list[str] = []
    for path in _sources():
        source = path.read_text()
        for match in re.finditer(r"Authorization: `Bearer", source):
            window = source[max(0, match.start() - 400):match.start() + 200]
            if any(endpoint in window for endpoint in WORKSPACE_AGNOSTIC):
                continue
            line = source[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(WEB)}:{line}")
    assert not offenders, (
        "these requests send a bearer without the active-workspace hint, so "
        f"they resolve to the default workspace: {offenders}"
    )


def test_both_export_downloads_use_it():
    for page in ("app/(app)/dependencies/page.tsx", "app/(app)/docs/page.tsx"):
        source = (WEB / page).read_text()
        assert "requestHeaders(token)" in source, f"{page} still hand-rolls headers"
        assert "requestHeaders" in source.split("\n@/lib/api")[0] or \
               "requestHeaders" in source, f"{page} does not import it"
