"""A relative link is not a smaller notification — it is no notification.

Google Chat validates `openLink.url` in a card. A value like
`/claude/78dcfe6c` fails that validation and the WHOLE CARD is dropped: what
arrives in the room is the bot's name with an empty body, and our own log
records `notif_delivered ... delivered=1`.

Seen on a real phone before this was written — a hole in the chat feed
between a firing alert and its recovery, at 16:14:21, which is the minute an
agent session finished and sent `link_url="/claude/<id>"`.

The alerts path had already learned this: `_alerts_link` returns an absolute
URL from configuration or None, and says why in a long comment. The agent path
had not. So the rule now lives in one place, `dispatch.public_link`, and this
reads every call site to check nobody hand-rolls a relative one again.

Parsed with `ast`, not grepped: the string "/claude/" appears in this
docstring and in the comment that explains the fix.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


def _notify_calls() -> list[tuple[str, int, ast.expr]]:
    """Every `link_url=` argument passed to a notify()-shaped call."""
    found: list[tuple[str, int, ast.expr]] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute)
                    else "")
            if "notify" not in name and name != "to_thread":
                continue
            for kw in node.keywords:
                if kw.arg == "link_url":
                    found.append((str(path.relative_to(SRC)), node.lineno, kw.value))
    return found


def test_there_are_call_sites_to_check() -> None:
    calls = _notify_calls()
    assert len(calls) >= 3, (
        f"only found {len(calls)} notify(link_url=...) call sites; this guard "
        f"is aimed at nothing"
    )


@pytest.mark.parametrize("relpath,lineno,value",
                         _notify_calls(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_no_call_site_passes_a_relative_link(relpath, lineno, value) -> None:
    literal = None
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        literal = value.value
    elif isinstance(value, ast.JoinedStr):
        parts = [v.value for v in value.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        literal = "".join(parts)
    if literal is None:
        return          # computed — public_link() and pr.url both land here
    assert literal.startswith(("http://", "https://")), (
        f"src/{relpath}:{lineno} passes link_url={literal!r}. Google Chat "
        f"drops a card whose button URL is not absolute, and the delivery is "
        f"logged as a success — pass dispatch.public_link(path) instead, which "
        f"returns an absolute URL or None."
    )


def test_the_helper_refuses_rather_than_guesses() -> None:
    """No base configured means NO link, not a link to somewhere invented."""
    source = (SRC / "notifications" / "dispatch.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "public_link"), None)
    assert fn is not None, "dispatch.public_link is gone"
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert any(isinstance(r.value, ast.Constant) and r.value.value is None
               for r in returns), (
        "public_link never returns None, so an installation with no "
        "PUBLIC_BASE_URL gets a guessed address instead of no button"
    )
