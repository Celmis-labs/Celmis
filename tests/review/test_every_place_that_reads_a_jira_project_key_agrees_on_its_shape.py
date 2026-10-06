"""One shape for a Jira project key, wherever it is read.

The settings validator, the issue-key check in front of the REST call and the
finder that reads titles and branches must accept and reject the same names: a
key the settings accept but the finder never sees is an allow-list entry that
can never match, and says nothing about it.
"""

from __future__ import annotations

import re

import pytest

from src.api.routers.task_context import _KEY as ROUTER_KEY
from src.review.review_defaults import PROJECT_KEY_PATTERN
from src.review.task_context.jira_client import KEY_PATTERN
from src.review.task_context.keys import keys_in

_PROJECTS = ["PROJ", "AIR", "AB", "ABCDEFGHIJ", "A1", "MY_PROJ", "A", "ABCDEFGHIJK", "1AB", "ab"]


@pytest.mark.parametrize("project", _PROJECTS)
def test_the_validator_the_rest_check_and_the_finder_accept_the_same_projects(project):
    settings_ok = bool(re.match(PROJECT_KEY_PATTERN, project))
    assert settings_ok == bool(KEY_PATTERN.match(f"{project}-12"))
    assert settings_ok == bool(ROUTER_KEY.match(f"{project}-12"))
    if settings_ok:
        assert keys_in(f"{project}-12") == [f"{project}-12"]


def test_a_project_key_with_an_underscore_is_refused_where_it_is_saved():
    assert not re.match(PROJECT_KEY_PATTERN, "MY_PROJ")
