"""The Jira task settings layer like every other: repository, then workspace, then built-in.

Five settings (`task_context_enabled`, `task_project_keys`,
`task_acceptance_field`, `task_include_comments`, `business_logic_auto`) are
saved by the same two endpoints as the rest, validated by the same function at
both layers, and read by the review from the merged policy. A value that would
silently stop tasks from being found, a project key with a typo or a field id
that is not one, is refused rather than saved.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.routers.review_policies import v23_updates_from_payload
from src.api.schemas import ReviewPolicyIn, WorkspaceReviewDefaultsIn
from src.review.review_defaults import (
    BUILTIN_DEFAULTS,
    INHERITABLE_FIELDS,
    SETTING_CHOICES,
    merge_policy,
    resolve,
)
from src.review.task_context.service import task_settings

JIRA = ("task_context_enabled", "task_project_keys", "task_acceptance_field",
        "task_include_comments", "business_logic_auto")


def _install() -> dict:
    return {name: BUILTIN_DEFAULTS.get(name) for name in INHERITABLE_FIELDS}


def test_with_nothing_set_a_task_is_read_and_the_agent_is_not_switched_on():
    values, sources = resolve({}, None, _install())
    assert values["task_context_enabled"] is True
    assert values["task_project_keys"] == []
    assert values["task_acceptance_field"] is None
    assert values["task_include_comments"] == 0
    assert values["business_logic_auto"] == "off"
    assert {sources[k] for k in JIRA} == {"install"}


def test_the_workspace_overrides_the_built_in_and_the_repository_overrides_the_workspace():
    workspace = {"task_project_keys": ["PROJ"], "business_logic_auto": "when_task_found",
                 "task_include_comments": 3}
    repo = {"task_project_keys": ["AIR"], "task_include_comments": 0}
    values, sources = resolve(repo, workspace, _install())
    assert values["task_project_keys"] == ["AIR"] and sources["task_project_keys"] == "repo"
    assert values["task_include_comments"] == 0 and sources["task_include_comments"] == "repo"
    assert values["business_logic_auto"] == "when_task_found"
    assert sources["business_logic_auto"] == "workspace"


def test_a_repository_can_switch_the_task_read_off_where_the_workspace_has_it_on():
    values, _ = resolve({"task_context_enabled": False},
                        {"task_context_enabled": True}, _install())
    assert values["task_context_enabled"] is False


def test_an_empty_project_list_at_a_repository_is_an_answer_not_a_blank():
    values, sources = resolve({"task_project_keys": []}, {"task_project_keys": ["PROJ"]},
                              _install())
    assert values["task_project_keys"] == [] and sources["task_project_keys"] == "repo"


def test_the_review_reads_the_settings_from_the_merged_policy():
    policy = merge_policy({"enabled": True, "task_acceptance_field": "customfield_10042"},
                          {"task_project_keys": ["PROJ", "AIR"], "business_logic_auto": "when_task_found"})
    cfg = task_settings(policy)
    assert cfg == {"enabled": True, "project_keys": ["PROJ", "AIR"],
                   "acceptance_field": "customfield_10042", "include_comments": 0,
                   "auto": "when_task_found",
                   "requirements_mode": "checklist", "urls_enabled": False}


def test_a_nonsense_value_in_a_stored_row_falls_back_instead_of_breaking_a_review():
    cfg = task_settings({"business_logic_auto": "sometimes", "task_include_comments": "many",
                         "task_project_keys": [" proj ", ""]})
    assert cfg["auto"] == "off" and cfg["include_comments"] == 0
    assert cfg["project_keys"] == ["PROJ"]


@pytest.mark.parametrize("model", [ReviewPolicyIn, WorkspaceReviewDefaultsIn])
def test_both_layers_save_and_normalise_a_valid_set(model):
    payload = model(task_context_enabled=True, task_project_keys=[" proj ", "AIR", "PROJ"],
                    task_acceptance_field=" customfield_10042 ", task_include_comments=5,
                    business_logic_auto="when_task_found")
    assert v23_updates_from_payload(payload) == {
        "task_context_enabled": True, "task_project_keys": ["PROJ", "AIR"],
        "task_acceptance_field": "customfield_10042", "task_include_comments": 5,
        "business_logic_auto": "when_task_found"}


@pytest.mark.parametrize("model", [ReviewPolicyIn, WorkspaceReviewDefaultsIn])
def test_a_field_not_named_is_left_alone_and_null_goes_back_to_inheriting(model):
    assert v23_updates_from_payload(model()) == {}
    cleared = model(task_project_keys=None, task_acceptance_field="  ", business_logic_auto=None)
    assert v23_updates_from_payload(cleared) == {
        "task_project_keys": None, "task_acceptance_field": None, "business_logic_auto": None}


@pytest.mark.parametrize(("field", "value"), [
    ("task_project_keys", ["vp 2d"]),
    ("task_project_keys", ["2D"]),
    ("task_project_keys", ["TOOLONGKEYNAME"]),
    ("task_acceptance_field", "description"),
    ("task_acceptance_field", "customfield_"),
    ("task_acceptance_field", "customfield_12/../x"),
    ("task_include_comments", 11),
    ("task_include_comments", -1),
    ("business_logic_auto", "always"),
])
def test_a_value_that_would_silently_break_task_lookup_is_refused(field, value):
    try:
        payload = WorkspaceReviewDefaultsIn(**{field: value})
    except ValueError:
        return                                  # the schema already refused it
    with pytest.raises(HTTPException) as err:
        v23_updates_from_payload(payload)
    assert err.value.status_code == 422 and field in err.value.detail


def test_the_vocabulary_of_the_auto_setting_is_the_one_the_page_offers():
    assert SETTING_CHOICES["business_logic_auto"] == ("off", "when_task_found")


def test_every_jira_setting_is_inheritable_and_has_a_built_in():
    for name in JIRA:
        assert name in INHERITABLE_FIELDS and name in BUILTIN_DEFAULTS
