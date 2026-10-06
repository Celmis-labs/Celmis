"""Jira's rich text becomes plain text the model can read, and stays bounded.

Jira Cloud hands descriptions over as Atlassian Document Format, a JSON tree.
The acceptance criteria live in it as bullet lists, task lists or table rows;
losing them leaves the agent with "the description is the spec" when the
author wrote a checklist. The same tree is also untrusted input: it may be
absurdly deep, absurdly long or full of characters that hide text from a
reader, and none of that may reach the prompt.
"""

from __future__ import annotations

from src.review.task_context.adf import CUT, OMITTED, adf_to_text
from tests.review.jira_fakes import bullets, doc, heading, para


def test_paragraphs_headings_and_bullets_keep_their_shape():
    text = adf_to_text(doc(para("hello"), heading("Acceptance criteria"), bullets("a", "b")))
    assert text == "hello\n\n## Acceptance criteria\n\n- a\n- b"


def test_a_task_list_keeps_which_items_are_done():
    items = {"type": "taskList", "content": [
        {"type": "taskItem", "attrs": {"state": "DONE"},
         "content": [{"type": "text", "text": "done thing"}]},
        {"type": "taskItem", "attrs": {"state": "TODO"},
         "content": [{"type": "text", "text": "todo thing"}]},
    ]}
    assert adf_to_text(doc(items)) == "- [x] done thing\n- [ ] todo thing"


def test_a_table_is_read_row_by_row():
    cell = lambda kind, t: {"type": kind, "content": [para(t)]}  # noqa: E731
    table = {"type": "table", "content": [
        {"type": "tableRow", "content": [cell("tableHeader", "H1"), cell("tableHeader", "H2")]},
        {"type": "tableRow", "content": [cell("tableCell", "a"), cell("tableCell", "b")]},
    ]}
    assert adf_to_text(doc(table)) == "H1 | H2\na | b"


def test_a_mention_keeps_the_name_and_an_attachment_is_dropped():
    mention = {"type": "paragraph", "content": [
        {"type": "mention", "attrs": {"id": "1", "text": "@Ann"}},
        {"type": "text", "text": " please check"}]}
    media = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "x"}}]}
    assert adf_to_text(doc(mention, media)) == f"@Ann please check\n\n{OMITTED}"


def test_a_document_nested_far_beyond_reason_is_cut_instead_of_followed():
    node = para("x")
    for _ in range(80):
        node = {"type": "blockquote", "content": [node]}
    assert adf_to_text(doc(node)).strip() == CUT


def test_a_huge_document_is_capped_at_the_limit_it_was_given():
    text = adf_to_text(doc(*[para("x" * 100) for _ in range(500)]), max_chars=1000)
    assert len(text) <= 1000
    assert text.endswith(CUT)


def test_characters_that_hide_text_from_a_reader_are_removed():
    text = adf_to_text(doc(para("a\x00b‮ c​d")))
    assert text == "ab cd"


def test_a_plain_string_and_nothing_at_all_are_both_accepted():
    assert adf_to_text("already text") == "already text"
    assert adf_to_text(None) == ""
