"""The three learning settings — `memories_enabled`, `knowledge_approval`,
`memory_trusted_commenters` — resolve like every other inheritable setting:
the repository's own value, else the workspace's, else the built-in; NULL
inherits, and an explicit empty list is a decision.

The policy fixture is the sibling file's: the same router over the same
sqlite table.
"""

from __future__ import annotations

from src.review.review_defaults import BUILTIN_DEFAULTS, install_defaults, resolve
from src.review.settings import ReviewSettings
from tests.api.test_the_policy_page_carries_the_ceiling import (
    REASONING_MODEL,
    _get,
    _put,
    _workspace,
    policy_api,
)

KEYS = ("memories_enabled", "knowledge_approval", "memory_trusted_commenters")


def _resolve(repo: dict | None, workspace: dict | None):
    return resolve(repo, workspace, install_defaults(ReviewSettings()))


def test_a_repository_with_nothing_set_gets_the_built_ins():
    values, sources = _resolve(None, None)
    assert (values["memories_enabled"], values["knowledge_approval"],
            values["memory_trusted_commenters"]) == (True, True, [])
    assert {sources[k] for k in KEYS} == {"install"}, "nothing set anywhere: the install default"
    assert BUILTIN_DEFAULTS["memories_enabled"] is True


def test_the_workspace_value_applies_where_the_repository_says_nothing():
    values, sources = _resolve(
        {"memories_enabled": None},
        {"memories_enabled": False, "memory_trusted_commenters": ["lead"]})
    assert values["memories_enabled"] is False and sources["memories_enabled"] == "workspace"
    assert values["memory_trusted_commenters"] == ["lead"]


def test_the_repository_value_beats_the_workspace_value():
    values, sources = _resolve(
        {"memories_enabled": True, "knowledge_approval": False,
         "memory_trusted_commenters": ["dev"]},
        {"memories_enabled": False, "knowledge_approval": True,
         "memory_trusted_commenters": ["lead"]})
    assert values["memories_enabled"] is True
    assert values["knowledge_approval"] is False
    assert values["memory_trusted_commenters"] == ["dev"]
    assert {sources[k] for k in KEYS} == {"repo"}


def test_an_empty_list_in_a_repository_is_a_decision_not_an_inherit():
    values, sources = _resolve({"memory_trusted_commenters": []},
                               {"memory_trusted_commenters": ["lead"]})
    assert values["memory_trusted_commenters"] == []
    assert sources["memory_trusted_commenters"] == "repo"


async def test_a_policy_round_trips_the_settings_and_says_what_is_in_force():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        policy = await _get(client)
        assert [policy[k] for k in KEYS] == [None, None, None]
        assert policy["memories_enabled_effective"] is True
        assert policy["knowledge_approval_effective"] is True
        assert policy["memory_trusted_commenters_effective"] == []

        saved = await _put(client, memories_enabled=False, knowledge_approval=False,
                           memory_trusted_commenters=["Jane", "jane", " dev@acme.io ", ""])
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["memories_enabled"] is False
        assert policy["memories_enabled_effective"] is False
        assert policy["knowledge_approval_effective"] is False
        assert policy["memory_trusted_commenters"] == ["Jane", "dev@acme.io"], (
            "trimmed, blank dropped, a case-insensitive duplicate dropped")
        assert policy["memory_trusted_commenters_effective"] == ["Jane", "dev@acme.io"]


async def test_a_save_that_omits_the_settings_keeps_them():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, memories_enabled=False, memory_trusted_commenters=["lead"])
        saved = await _put(client, prompt_template="a new template")
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["prompt_template"] == "a new template"
        assert policy["memories_enabled"] is False, "a body without the key is not a request to forget it"
        assert policy["memory_trusted_commenters"] == ["lead"]


async def test_an_explicit_null_goes_back_to_inheriting():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, memories_enabled=False, memory_trusted_commenters=["lead"])
        saved = await _put(client, memories_enabled=None, memory_trusted_commenters=None)
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["memories_enabled"] is None
        assert policy["memory_trusted_commenters"] is None
        assert policy["memories_enabled_effective"] is True


async def test_an_identity_with_a_control_character_is_refused():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        saved = await _put(client, memory_trusted_commenters=["ok", "bad\u0000name"])
        assert saved.status_code == 422, saved.text
