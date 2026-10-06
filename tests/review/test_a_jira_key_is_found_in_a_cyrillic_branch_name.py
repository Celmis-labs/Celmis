"""A task key is found wherever a team writes it, and only where it is one.

Teams type branches in Ukrainian next to the key (`AB2C-6066-порезка-профилей`),
a Cyrillic keyboard layout turns the Latin `VP` into the lookalike `ВР`, and
prose is full of things shaped like keys (`UTF-8`, `SHA-256`, `CVE-2024-1234`)
that are not Jira tasks. A wrong key is a wasted request and, worse, a task
that has nothing to do with the change handed to the model as its spec.
"""

from __future__ import annotations

import pytest

from src.review.models import PullRequest
from src.review.task_context.keys import MAX_TASKS, extract_task_refs, keys_in, parse_key


def _pr(*, title="", head_ref="main", description="") -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=1, title=title,
        description=description, author="a", base_ref="main", base_sha="x",
        head_ref=head_ref, head_sha="y", state="open", hunks=[],
    )


@pytest.mark.parametrize("branch", [
    "AB2C-6066-порезка-профилей",
    "feature/AB2C-6066_порезка",
    "bugfix/ab2c-6066-lower-case",
])
def test_a_key_in_a_branch_is_found_however_the_rest_is_written(branch):
    assert [r.key for r in extract_task_refs(_pr(head_ref=branch))] == ["AB2C-6066"]


def test_a_cyrillic_lookalike_of_a_latin_project_key_is_read_as_the_latin_one():
    # ВР2D uses the Cyrillic В and Р, which look exactly like the Latin ones.
    assert keys_in("ВР2D-1 порезка") == ["BP2D-1"]


@pytest.mark.parametrize("text", [
    "stored as UTF-8", "hashed with SHA-256", "fixes CVE-2024-1234", "see ISO-8601",
])
def test_a_standard_is_not_a_task(text):
    assert keys_in(text) == []


def test_the_title_wins_over_the_branch_and_each_key_is_listed_once():
    refs = extract_task_refs(_pr(title="PROJ-1: fix", head_ref="PROJ-1-and-AB2C-2"))
    assert [(r.key, r.source) for r in refs] == [("PROJ-1", "title"), ("AB2C-2", "branch")]


def test_a_project_allowlist_drops_every_other_project():
    refs = extract_task_refs(_pr(title="PROJ-1 and AB2C-2"), allowed_projects={"AB2C"})
    assert [r.key for r in refs] == ["AB2C-2"]


def test_no_more_than_three_tasks_are_read_for_one_pull_request():
    refs = extract_task_refs(_pr(description="PROJ-1 PROJ-2 PROJ-3 PROJ-4 PROJ-5"))
    assert len(refs) == MAX_TASKS == 3


def test_commit_messages_are_read_last():
    refs = extract_task_refs(_pr(title="no key"), commit_messages=["PROJ-9 wip"])
    assert [(r.key, r.source) for r in refs] == [("PROJ-9", "commit")]


def test_a_link_to_another_site_does_not_name_a_task_but_one_to_ours_does():
    text = "see https://other.example.com/browse/PROJ-7 and https://acme.atlassian.net/browse/PROJ-8"
    assert keys_in(text, jira_host="acme.atlassian.net") == ["PROJ-8"]


def test_a_typed_key_or_browse_link_is_parsed_and_junk_is_refused():
    assert parse_key("ab2c-6066") == "AB2C-6066"
    assert parse_key("https://acme.atlassian.net/browse/PROJ-8",
                     jira_host="acme.atlassian.net") == "PROJ-8"
    assert parse_key("not a key") is None


@pytest.mark.parametrize("url", ["https://a\u2100b/x", "https://exa\uff0fmple.com", "https://a\uff20b.com"])
def test_a_link_whose_host_cannot_be_parsed_is_foreign_and_does_not_hide_a_real_key(url):
    assert keys_in(f"see {url} AB-12", jira_host="x.atlassian.net") == ["AB-12"]
