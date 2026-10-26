"""The Connect page tells the plugin's real setup: Claude Code asks for the server
address and the token, and `/plugin configure` changes them.

Before 3.0.2 the page said `export CELMIS_URL=...` and `export CELMIS_TOKEN=<your
token>` and "The plugin reads the variables". The plugin no longer reads those two
variables (it keeps an address and a token as `userConfig` options, see
`packaging/claude-plugin/celmis-code`), so a person following the page ended with a
plugin that showed no tools. These tests hold the page, its sentences in every
language and the plugin manifest to one another.

The page keeps showing the address (`publicMcpUrl("")`): it is what Claude Code asks
for, and the one value this page can supply.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    WEB,
    _strip_comments,
)

PAGE = WEB / "app" / "(app)" / "codebase" / "connect" / "page.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"
PLUGIN = WEB.parent / "packaging" / "claude-plugin" / "celmis-code"
MARKETPLACE = WEB.parent / ".claude-plugin" / "marketplace.json"

#: The sentences that describe the plugin route's two values, in the order the page shows
#: them. (`plugin.note` and `plugin.change` were folded into the `enter.*` step of the
#: connection guide, which says the same things and the ones that went wrong on the way.)
PLUGIN_KEYS = ("codebase.connect.enter.desc", "codebase.connect.plugin.address",
               "codebase.connect.enter.token", "codebase.connect.enter.verify")
#: Of those, the ones that may name the program the command is typed into.
NAMES_THE_PROGRAM = ("codebase.connect.enter.desc",)


def _code() -> str:
    return _strip_comments(PAGE.read_text(encoding="utf-8"))


def _messages(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))


def _locales() -> list[str]:
    return sorted(p.stem for p in MESSAGES.glob("*.json"))


def test_the_page_no_longer_asks_for_an_exported_token_or_address() -> None:
    code = _code()
    for gone in ("CELMIS_TOKEN", "export CELMIS_URL", "tokenPlaceholder"):
        assert gone not in code, f"the page still teaches: {gone}"
    # the manual route is a different thing (`claude mcp add`, its own variable) and stays
    assert "CELMIS_MCP_TOKEN" in code


def test_the_page_shows_the_install_commands_the_address_and_the_configure_command() -> None:
    code = _code()
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    ref = f"{plugin['name']}@{market['name']}"
    assert f'"/plugin install {ref}"' in code
    assert f'"/plugin configure {ref}"' in code
    assert 'publicMcpUrl("")' in code, "the address Claude Code asks for must stay on the page"
    for key in PLUGIN_KEYS:
        assert f'"{key}"' in code, f"the page never shows {key}"


def test_the_commands_on_the_page_are_the_ones_the_readme_documents() -> None:
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    for command in re.findall(r'"(/plugin (?:install|configure|marketplace add) [^"]+)"', _code()):
        assert command in readme, f"the README does not document: {command}"


def test_no_token_value_or_shell_assignment_is_typed_into_the_page() -> None:
    code = _code()
    assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}", code)
    # the one place a header with a bearer is shown is the manual route's command, and it
    # names the shell variable: the token itself is never on the page
    assert "Bearer" not in code.replace("Authorization: Bearer \\${CELMIS_MCP_TOKEN}", ""), (
        "only the manual route's template")


@pytest.mark.parametrize("locale", _locales())
def test_every_language_says_it_without_the_old_instruction(locale: str) -> None:
    en, mine = _messages("en"), _messages(locale)
    assert "codebase.connect.tokenPlaceholder" not in mine
    for key in PLUGIN_KEYS:
        assert mine[key].strip(), f"{key} is empty in {locale}"
        assert "CELMIS_TOKEN" not in mine[key] and "export" not in mine[key].lower().split()
        if locale != "en":
            assert mine[key] != en[key], f"{key} is still English in {locale}"
    # the command is typed into Claude Code, and the sentence that says so names it
    for key in NAMES_THE_PROGRAM:
        assert "Claude Code" in mine[key], f"{key} does not say where to type it in {locale}"
    for key in PLUGIN_KEYS:
        if key not in NAMES_THE_PROGRAM:
            assert "Claude" not in mine[key], key
    # the two sentences the guide folded away are gone, and "the variables" was the old promise
    assert "codebase.connect.plugin.note" not in mine
    assert "codebase.connect.plugin.change" not in mine
    assert mine["codebase.connect.enter.desc"] != _old_note(locale)


def _old_note(locale: str) -> str:
    """The sentence that was here until 3.0.2, per language (kept so a revert fails)."""
    return {
        "en": "Set the two variables in your shell, then run the two commands inside Claude Code. The plugin reads the variables.",
        "uk": "Задайте дві змінні в оболонці, потім виконайте дві команди всередині Claude Code. Плагін читає ці змінні.",
    }.get(locale, "")


def test_the_token_and_the_address_sentences_agree_with_what_the_plugin_does() -> None:
    en = _messages("en")
    ask = en["codebase.connect.enter.desc"]
    assert "address" in ask and "token" in ask
    assert "masked" in en["codebase.connect.enter.token"]
    # (the page used to add a sentence about where the plugin keeps the token; it was
    # removed as a claim the page cannot check, and the manual route's note says the
    # opposite for itself)
    assert "codebase.connect.plugin.stored" not in en
    assert "restart" in en["codebase.connect.check.restart"].lower()
    # the manifest asks for exactly those two things
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert set(plugin["userConfig"]) == {"celmis_url", "celmis_token"}
