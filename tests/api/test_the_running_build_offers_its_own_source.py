"""AGPL §13: a network service must offer its source to the people using it.

Celmis is used through a browser, so §13 binds. The interface said nothing —
no version, no licence, no link — which left the obligation documented in a
file only somebody who already had the source would read.

The link has to point at the source of the RUNNING BUILD, not at the default
branch. "Here is our repository" is not the offer; "here is the code you are
talking to" is. `api_version` is `0.1.0+<sha>` for a build off a commit and
`0.1.0` for a tagged one, so the two address differently.

These read the TSX as text because Python cannot parse it. They check the
bindings that matter, not prose.
"""

from __future__ import annotations

import pathlib

WEB = pathlib.Path(__file__).resolve().parents[2] / "web"
FOOTER = WEB / "components/license-footer.tsx"
SHELL = WEB / "components/app-shell.tsx"


def test_the_footer_exists():
    assert FOOTER.is_file()


def test_every_page_carries_it():
    """In the shell, not on one page: §13 is about anyone interacting with the
    program, not about whoever finds the About screen."""
    src = SHELL.read_text(encoding="utf-8")

    assert "LicenseFooter" in src
    assert "import { LicenseFooter }" in src


def test_it_names_the_licence():
    src = FOOTER.read_text(encoding="utf-8")

    assert "AGPL-3.0" in src
    assert "gnu.org/licenses/agpl-3.0" in src


def test_it_shows_the_running_version():
    src = FOOTER.read_text(encoding="utf-8")

    assert "api_version" in src
    assert "/api/capabilities" in src


def test_the_source_link_follows_the_build_not_the_branch():
    src = FOOTER.read_text(encoding="utf-8")

    assert "/tree/" in src, "a build off a commit must link to that commit"
    assert "/releases/tag/" in src, "a tagged build must link to its tag"
    assert "/tree/main" not in src, "linking to main offers code nobody is running"


def test_the_repository_url_is_one_constant():
    """It moves once, when the project moves. Not in five places."""
    src = FOOTER.read_text(encoding="utf-8")

    assert src.count("https://github.com/") == 1
