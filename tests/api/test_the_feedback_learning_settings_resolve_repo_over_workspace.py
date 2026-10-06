"""`learning_suppression` and `learning_excluded_reviewers` resolve like every
other inheritable setting: the repository's own value, else the workspace's,
else the built-in (`shadow`, nobody excluded); NULL inherits, an explicit empty
list is a decision, and a value outside the choices is refused.
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

KEYS = ("learning_suppression", "learning_excluded_reviewers")


def _resolve(repo: dict | None, workspace: dict | None):
    return resolve(repo, workspace, install_defaults(ReviewSettings()))


def test_the_built_in_is_shadow_and_nobody_is_excluded():
    values, sources = _resolve(None, None)
    assert (values["learning_suppression"], values["learning_excluded_reviewers"]) == ("shadow", [])
    assert BUILTIN_DEFAULTS["learning_suppression"] == "shadow"
    assert {sources[k] for k in KEYS} == {"install"}


def test_the_workspace_value_applies_where_the_repository_says_nothing():
    values, sources = _resolve(
        {"learning_suppression": None},
        {"learning_suppression": "on", "learning_excluded_reviewers": ["ci-bot"]})
    assert values["learning_suppression"] == "on" and sources["learning_suppression"] == "workspace"
    assert values["learning_excluded_reviewers"] == ["ci-bot"]


def test_the_repository_value_beats_the_workspace_value():
    values, sources = _resolve(
        {"learning_suppression": "off", "learning_excluded_reviewers": ["qa"]},
        {"learning_suppression": "on", "learning_excluded_reviewers": ["ci-bot"]})
    assert (values["learning_suppression"], values["learning_excluded_reviewers"]) == ("off", ["qa"])
    assert {sources[k] for k in KEYS} == {"repo"}


def test_an_empty_list_in_a_repository_is_a_decision_not_an_inherit():
    values, sources = _resolve({"learning_excluded_reviewers": []},
                               {"learning_excluded_reviewers": ["ci-bot"]})
    assert values["learning_excluded_reviewers"] == []
    assert sources["learning_excluded_reviewers"] == "repo"


async def test_a_policy_round_trips_the_settings_and_says_what_is_in_force():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        policy = await _get(client)
        assert [policy[k] for k in KEYS] == [None, None]
        assert policy["learning_suppression_effective"] == "shadow"
        assert policy["learning_excluded_reviewers_effective"] == []

        saved = await _put(client, learning_suppression="on",
                           learning_excluded_reviewers=["CI-Bot", "ci-bot", " qa@acme.io ", ""])
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["learning_suppression_effective"] == "on"
        assert policy["learning_excluded_reviewers"] == ["CI-Bot", "qa@acme.io"], (
            "trimmed, blank dropped, a case-insensitive duplicate dropped")


async def test_a_save_that_omits_the_settings_keeps_them_and_null_inherits_again():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, learning_suppression="off", learning_excluded_reviewers=["ci-bot"])
        await _put(client, prompt_template="a new template")
        policy = await _get(client)
        assert (policy["learning_suppression"], policy["learning_excluded_reviewers"]) == (
            "off", ["ci-bot"])
        await _put(client, learning_suppression=None, learning_excluded_reviewers=None)
        policy = await _get(client)
        assert [policy[k] for k in KEYS] == [None, None]
        assert policy["learning_suppression_effective"] == "shadow"


async def test_a_mode_outside_the_choices_and_a_bad_identity_are_refused():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        assert (await _put(client, learning_suppression="maybe")).status_code == 422
        bad = await _put(client, learning_excluded_reviewers=["ok", "bad\u0000name"])
        assert bad.status_code == 422
