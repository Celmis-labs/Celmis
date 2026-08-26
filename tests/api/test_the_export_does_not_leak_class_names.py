"""A personal-data export is a document a person is entitled to read.

`auth_method` once left this endpoint as the string "UserAuthMethod.PASSWORD"
— a Python class name in a GDPR export — because the member was rendered with
str() while a (str, Enum) mixin still spelled itself that way. Every other
reader of the field (UserStore, /auth/me) used .value and saw "password", so
the export disagreed with the rest of the system about the same user.

Two tests, because there are two ways to break it again: change how the enum
renders, or go back to rendering it with str().
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from src.users.models import UserAuthMethod

GDPR = Path(__file__).resolve().parents[2] / "src" / "api" / "routers" / "gdpr.py"


def test_a_member_spells_itself_the_way_the_wire_does():
    for member in UserAuthMethod:
        assert str(member) == member.value, (
            f"str({member!r}) is {str(member)!r}, not {member.value!r} — a "
            "(str, Enum) base renders the class name; StrEnum renders the value"
        )
        assert f"{member}" == member.value
        assert json.loads(json.dumps(member)) == member.value


def test_the_export_hands_over_the_value_not_the_repr():
    """AST, not grep: the docstring above names the broken form, and a text
    search would find it there and pass on a file that still ships the bug."""
    tree = ast.parse(GDPR.read_text())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "auth_method"):
                continue
            assert not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "str"
            ), (
                f"gdpr.py:{value.lineno} wraps auth_method in str(). Use "
                ".value — the export must read the same as /auth/me."
            )
