"""A command the web shows for copying names a shell variable, never a token.

A token typed into a command is on screen while the command is read and in the
shell history after it is run. The project token card showed
`claude mcp add ... --header "Authorization: Bearer <the token>"` with the
issued token in the command text, a second way to leak the token that the
connect guide (/codebase/connect) had already closed for its manual route:
there the command names `${CELMIS_MCP_TOKEN}` and a box of its own, run alone,
fills the variable with `read -rs` (no echo, no history).

What is pinned:

  * no template literal in the web tree that looks like a shell command
    (`claude ...`, `curl ...`, `export ...`, a `--header`) interpolates a token,
    a secret or a JWT. The scan understands template literals (nested `${}`
    included), so the shell's own `\\${VAR}` is text, not an interpolation;
  * the scan is not vacuous: it flags the old project card and the shapes it
    could take, and lets the safe ones through;
  * the project token card: the token sits in a field of its own with its own
    copy button, the command names the variable, the box that fills the variable
    comes first and is the guide's line, and the guide's sentence explains both.

A JSON file for an editor (`settings/mcp`, `admin/mcp-tokens`) is a file to save,
not a command to run, and carries the token by design; it is not a shell
command and the scan does not take it for one.

There is no browser here, so the source is read, with comments removed — a test
must not pass on the explanation a file gives of its rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    _strip_comments,
)

WEB = Path(__file__).resolve().parents[2] / "web"
CARD = WEB / "components" / "project-mcp-tokens.tsx"
GUIDE = WEB / "app" / "(app)" / "codebase" / "connect" / "page.tsx"

#: A template literal is a shell command when one of its lines starts with a
#: program an operator types, or it carries a header flag.
SHELL_LINE = re.compile(
    r"(^|\n)\s*(claude|curl|wget|export|celmis-pack|npx|npm|pnpm|docker|git)\s", re.S,
)
#: What must not be interpolated into one. Whole identifiers only: `tokenHint`
#: (the words "the token above") is a label, `issued.token` is the value.
SECRET_NAME = re.compile(
    r"(?<![A-Za-z0-9_])(token|secret|jwt|accessToken|access_token|apiKey|api_key|password)"
    r"(?![A-Za-z0-9_])"
)


def _sources() -> list[Path]:
    out: list[Path] = []
    for sub in ("app", "components", "lib", "ee"):
        root = WEB / sub
        if root.exists():
            out += list(root.rglob("*.tsx")) + list(root.rglob("*.ts"))
    return sorted(p for p in out if "node_modules" not in p.parts and not p.name.endswith(".test.ts"))


def _template(src: str, i: int) -> tuple[str, list[str], int]:
    """The template literal that opens at `src[i]` (a backtick).

    Returns its literal text (every `${...}` replaced by a placeholder), the
    expressions it interpolates, and where it ends. `\\${` is an escape: text.
    """
    text: list[str] = []
    exprs: list[str] = []
    n = len(src)
    i += 1
    while i < n:
        ch = src[i]
        if ch == "\\":
            text.append(src[i:i + 2])
            i += 2
        elif ch == "`":
            return "".join(text), exprs, i + 1
        elif ch == "$" and src.startswith("${", i):
            start, depth, j = i + 2, 1, i + 2
            while j < n and depth:
                c = src[j]
                if c == "`":
                    _, inner, j = _template(src, j)
                    exprs.extend(inner)
                    continue
                if c in "\"'":
                    j += 1
                    while j < n and src[j] not in (c, "\n"):
                        j += 2 if src[j] == "\\" else 1
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
            exprs.append(src[start:j - 1])
            text.append("\x00")
            i = j
        else:
            text.append(ch)
            i += 1
    return "".join(text), exprs, n


def templates(src: str) -> list[tuple[int, str, list[str]]]:
    """Every top-level template literal: (line, text, interpolated expressions)."""
    found: list[tuple[int, str, list[str]]] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'":
            i += 1  # a string ends at its quote or, failing that, its line (JSX prose has apostrophes)
            while i < n and src[i] not in (ch, "\n"):
                i += 2 if src[i] == "\\" else 1
            i += 1
        elif ch == "`":
            text, exprs, end = _template(src, i)
            found.append((src.count("\n", 0, i) + 1, text, exprs))
            i = end
        else:
            i += 1
    return found


def leaking_commands(src: str) -> list[tuple[int, str]]:
    """The shell-looking template literals of `src` that interpolate a token."""
    bad: list[tuple[int, str]] = []
    for line, text, exprs in templates(src):
        if not (SHELL_LINE.search(text) or "--header" in text):
            continue
        for expr in exprs:
            if SECRET_NAME.search(expr):
                bad.append((line, expr.strip()))
    return bad


# ─── the scan itself ─────────────────────────────────────────────────

LEAKS = [
    # the project token card as it was
    '`claude mcp add --transport http project-search ${url()} --header "Authorization: Bearer ${issued.token}"`',
    '`claude mcp add celmis \\\n  ${url} \\\n  --header "Authorization: Bearer ${secret}"`',
    '`curl -H "Authorization: Bearer ${token}" ${url}`',
    '`export CELMIS_PACK_TOKEN=${fresh.token}`',
    '`claude mcp add x ${a} --header "X: ${ok ? `y ${session.jwt}` : ""}"`',
]
SAFE = [
    '`claude mcp add --transport http p ${url()} --header "Authorization: Bearer \\${CELMIS_MCP_TOKEN}"`',
    '`export CELMIS_PACK_TOKEN=<${tokenHint}>`',
    '`Bearer ${token}`',  # a request header is not a command shown on screen
    '`${a}/api/x?token=${b}`',
    '"claude mcp add x --header \\"Authorization: Bearer ${token}\\""',  # plain string: no interpolation at all
    "const s = 'it\\'s'; const u = `claude mcp add ${url}`;",
]


@pytest.mark.parametrize("src", LEAKS)
def test_the_scan_flags_a_command_that_holds_a_token(src: str) -> None:
    assert leaking_commands(src), src


@pytest.mark.parametrize("src", SAFE)
def test_the_scan_lets_a_command_that_names_a_variable_through(src: str) -> None:
    assert leaking_commands(src) == [], src


# ─── the web tree ────────────────────────────────────────────────────


def test_no_command_shown_anywhere_in_the_web_holds_a_token_value() -> None:
    offenders = {
        f"{path.relative_to(WEB)}:{line}": expr
        for path in _sources()
        for line, expr in leaking_commands(_strip_comments(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "a token typed into a command stays on screen and in the shell history; name a "
        f"variable (${{CELMIS_MCP_TOKEN}}) and fill it with `read -rs`: {offenders}"
    )


def test_the_commands_that_show_a_bearer_all_name_the_variable() -> None:
    """The three `claude mcp add` commands the web shows are the same safe line."""
    found = 0
    for path in _sources():
        for line, text, _ in templates(_strip_comments(path.read_text(encoding="utf-8"))):
            if "claude mcp add" in text and "Bearer" in text:
                found += 1
                assert "Bearer \\${CELMIS_MCP_TOKEN}" in text, f"{path.name}:{line}"
    assert found == 3, "the guide, the MCP settings page and the project token card"


# ─── the project token card ──────────────────────────────────────────


def _card() -> str:
    return _strip_comments(CARD.read_text(encoding="utf-8"))


def test_the_card_command_names_the_variable_and_holds_no_token() -> None:
    code = _card()
    command = re.search(r"const connectCommand = \(\) =>\s*`(.*?)`;", code, re.S)
    assert command, "the command moved"
    body = command.group(1)
    assert 'Authorization: Bearer \\${CELMIS_MCP_TOKEN}"' in body
    assert "\n" not in body, "one line: a command pasted in several is how a line lands in the wrong place"
    assert not re.search(r"\$\{(?!mcpUrl\(\)\})", body.replace("\\${", "")), (
        "only the address is filled in")
    assert "issued" not in body
    assert "AUTH_HEADER" not in code and not re.search(r"Authorization: `Bearer", code)


def test_the_token_is_shown_once_in_a_field_of_its_own_and_only_there() -> None:
    code = _card()
    uses = re.findall(r"issued\.token", code)
    assert len(uses) == 2, "the field and its copy button: the token goes nowhere else"
    assert "<code" in code and re.search(r"<code[^>]*>\{issued\.token\}</code>", code)
    assert re.search(r"copy\(issued\.token,", code)


def test_the_box_that_fills_the_variable_is_the_guides_line_and_comes_first() -> None:
    code = _card()
    guide = _strip_comments(GUIDE.read_text(encoding="utf-8"))
    line = 'const READ_TOKEN = "read -rs CELMIS_MCP_TOKEN && export CELMIS_MCP_TOKEN";'
    assert line in code and line in guide, "the card and the guide teach the same line"
    assert len(re.findall(r"\bread -", code)) == 1
    assert set(re.findall(r"export (CELMIS\w+)", code)) == {"CELMIS_MCP_TOKEN"}
    # a sentence first, then the box that fills the variable, then the command
    sentence = code.index('t("codebase.connect.manual.prepare")')
    fills = code.index("{READ_TOKEN}</code>")
    command = code.index("{command}</code>")
    assert sentence < fills < command
    assert "copy(READ_TOKEN," in code and "copy(command," in code


def test_the_sentence_the_card_reuses_exists_and_says_how_without_echoing() -> None:
    import json

    messages = WEB / "lib" / "i18n" / "messages"
    key = "codebase.connect.manual.prepare"
    for path in sorted(messages.glob("*.json")):
        mine = json.loads(path.read_text(encoding="utf-8"))
        assert mine.get(key, "").strip(), f"{key} is missing in {path.stem}"
    en = json.loads((messages / "en.json").read_text(encoding="utf-8"))[key]
    assert "on its own" in en and "nothing shows while you paste" in en


def test_the_card_copies_where_the_clipboard_api_may_not_exist() -> None:
    """The token is copied from this card and nowhere else; the product is often
    served over plain HTTP, where `navigator.clipboard` is undefined."""
    code = _card()
    assert "navigator.clipboard" not in code
    assert re.search(r"import \{ copyText \} from \"@/lib/copy\";", code)
