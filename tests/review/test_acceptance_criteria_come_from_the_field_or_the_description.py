"""Acceptance criteria are numbered once, from the best place they are written.

A finding cites `AC2`, so the numbering has to be stable and the source has to
be the one the team uses: the custom field when the workspace named it, else
the "Acceptance criteria" list in the description, else nothing — and then the
description itself is the specification, which the prompt says out loud.
"""

from __future__ import annotations

from src.review.task_context import service
from src.review.task_context.criteria import MAX_CRITERIA, extract_criteria
from src.review.task_context.models import TaskContext
from src.sync.jira_instance import JiraInstance
from tests.review.jira_fakes import SITE, bullets, doc, issue, para


def _texts(criteria):
    return [(c.id, c.text) for c in criteria]


def test_a_heading_followed_by_a_list_gives_numbered_criteria():
    criteria, source = extract_criteria("Intro\n## Acceptance criteria\n- one\n- two\n")
    assert source == "description"
    assert _texts(criteria) == [("AC1", "one"), ("AC2", "two")]


def test_the_custom_field_wins_over_the_description():
    criteria, source = extract_criteria(
        "## Acceptance criteria\n- from description", "- from field one\n- from field two")
    assert source == "field"
    assert _texts(criteria) == [("AC1", "from field one"), ("AC2", "from field two")]


def test_a_field_that_arrives_as_rich_text_is_read_too():
    criteria, source = extract_criteria("", doc(bullets("rich one", "rich two")))
    assert source == "field"
    assert [c.text for c in criteria] == ["rich one", "rich two"]


def test_a_description_without_a_list_has_no_criteria_and_says_so():
    criteria, source = extract_criteria("Just prose about what is wanted.")
    assert criteria == [] and source == "none"


def test_the_numbering_stops_at_the_cap():
    text = "## Acceptance criteria\n" + "\n".join(f"- item {n}" for n in range(60))
    criteria, _ = extract_criteria(text)
    assert len(criteria) == MAX_CRITERIA


def test_a_built_issue_carries_its_criteria_and_its_site_link():
    task = service.build_issue(issue(), JiraInstance(SITE), acceptance_field=None)
    assert task.url == f"{SITE}/browse/PROJ-6066"
    assert task.criteria_source == "description"
    assert [c.id for c in task.criteria] == ["AC1", "AC2"]


def test_a_task_with_no_list_tells_the_model_the_description_is_the_spec():
    raw = issue(description=doc(para("Make the export faster.")))
    task = service.build_issue(raw, JiraInstance(SITE), acceptance_field=None)
    block = service.render_task_block(TaskContext(status="ok", tasks=[task]))
    assert "the description itself is the specification" in block


def test_the_configured_field_is_the_one_that_is_read():
    raw = issue(fields={"customfield_10042": "- field criterion"})
    task = service.build_issue(raw, JiraInstance(SITE), acceptance_field="customfield_10042")
    assert task.criteria_source == "field"
    assert [c.text for c in task.criteria] == ["field criterion"]
