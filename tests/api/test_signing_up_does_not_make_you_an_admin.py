"""Two documents promised opposite things, and only one of them was true.

README said "That account also becomes the workspace admin (first user gets
is_admin=true)". `.env.example` said the master identity is "the ONLY way to
obtain global-admin rights". Both shipped. On a clean install the operator
followed the README, signed up, found /admin/* closed, and had no way to
tell which sentence was the bug.

The env design is the right one for a self-hosted product — admin is
whoever runs the box, not whoever reached the sign-up form first — so the
README sentence was the defect. These tests pin the behaviour so the two
cannot drift apart again, by asserting on the CODE rather than on prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "src" / "api" / "routers" / "auth.py"
README = ROOT / "README.md"


def _function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(AUTH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {AUTH}")


def _sets_is_admin_true(node: ast.AST) -> bool:
    """Does this function hand out admin rights anywhere in its body?"""
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.keyword)
            and inner.arg == "is_admin"
            and isinstance(inner.value, ast.Constant)
            and inner.value.value is True
        ):
            return True
        if isinstance(inner, ast.Assign):
            for target in inner.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "is_admin"
                    and isinstance(inner.value, ast.Constant)
                    and inner.value.value is True
                ):
                    return True
    return False


def test_the_signup_route_never_grants_admin():
    """Not to the first account, not to any. AST, not a grep: the words
    'is_admin' and 'first user' both appear in comments in this file."""
    for name in ("signup", "register"):
        try:
            node = _function(name)
        except AssertionError:
            continue
        assert not _sets_is_admin_true(node), (
            f"{name}() grants admin. If that is now intended, the README and "
            f".env.example both need rewriting — they say the master identity "
            f"is the only path."
        )
        break
    else:
        raise AssertionError("no signup route found — did it move?")


def test_the_master_login_is_the_path_that_does_grant_it():
    """The other half: the documented route must actually work, or the
    product has no way in at all."""
    assert _sets_is_admin_true(_function("_master_login"))


def test_the_readme_no_longer_promises_what_signup_does_not_do():
    text = README.read_text()
    assert "first user gets" not in text, (
        "the README claim that sign-up grants admin is back; the code does "
        "not do it and an operator following it is locked out of /admin/*"
    )


def test_the_readme_says_where_admin_actually_comes_from():
    text = README.read_text()
    assert "CELMIS_MASTER_EMAIL" in text and "CELMIS_MASTER_KEY" in text, (
        "an operator who cannot reach /admin/* needs the answer in the file "
        "they were told to follow"
    )
    assert "make-admin" in text, (
        "and the way to promote a normal account, which is what most people "
        "actually want after the first login"
    )
