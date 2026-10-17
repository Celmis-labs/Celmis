"""The workspace agent's roles section is computed from the roles module, so it
names exactly the roles that may create a knowledge pack — no more, no fewer.

The contract (src/automation/knowledge/access.py, src/users/roles.py):

  * each role in `PACK_AUTHOR_ROLES` carries the flag "may create Codebase
    knowledge packs" in the agent's text, and no other role does;
  * changing the set changes the sentence, with no second edit.
"""

from __future__ import annotations

from src.automation.knowledge import access
from src.users import roles

FLAG = "may create Codebase knowledge packs"


def _lines() -> dict[str, str]:
    out = {}
    for line in access._roles_body().splitlines():
        for role in roles.WORKSPACE_ROLE_RANK:
            if line.startswith(f"- `{role}`"):
                out.setdefault(role, line)          # the first mention is the description
    return out


def test_exactly_the_author_roles_are_flagged():
    lines = _lines()
    assert set(lines) == set(roles.WORKSPACE_ROLE_RANK)
    assert {role for role, line in lines.items() if FLAG in line} == set(roles.PACK_AUTHOR_ROLES)


def test_the_sentence_follows_the_set(monkeypatch):
    monkeypatch.setattr(roles, "PACK_AUTHOR_ROLES", frozenset({"owner"}))
    assert {role for role, line in _lines().items() if FLAG in line} == {"owner"}
