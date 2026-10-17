"""A control character in a pack's text is refused, wherever it was typed.

The contract (src/knowledge/store.py `_check_text`, docs/CODEBASE.md "Fields"):

  * a title, a description, a version note and a token label may not hold a
    control character — NUL, ESC, a stray carriage return — nor a bidirectional
    override or isolate, which reorders what a reader sees without changing what
    is stored; the answer is the 422 every bad field gets and nothing is saved;
  * a description and a note are multi-line: a line break (also CRLF, folded to a
    line break) and a tab are fine; a title and a label are one line;
  * a zero-width joiner, as inside an emoji, is ordinary text.
"""

from __future__ import annotations

import pytest

from src.knowledge import store
from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world

BAD = ["a\x00b", "a\x1b[31mred", "a\rb", "a‮b", "a⁦b", "a\x7fb"]


@pytest.mark.parametrize("bad", BAD)
def test_every_text_field_refuses_a_control_or_bidi_character(bad):
    for what, multiline in (("title", False), ("description", True), ("note", True),
                            ("label", False)):
        with pytest.raises(store.BadInput) as refused:
            store._check_text(bad, 100, what, multiline=multiline)
        assert what in str(refused.value) and "control characters" in str(refused.value)


def test_line_breaks_and_tabs_are_for_the_multi_line_fields_only():
    assert store._check_text("a\r\nb\tc", 100, "note", multiline=True) == "a\nb\tc"
    for what in ("title", "label"):
        with pytest.raises(store.BadInput):
            store._check_text("a\nb", 100, what)
        with pytest.raises(store.BadInput):
            store._check_text("a\tb", 100, what)


def test_a_joiner_inside_an_emoji_and_plain_accents_are_ordinary_text():
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    assert store._check_text(f" {family} Ємність ", 100, "title") == f"{family} Ємність"


async def test_the_routes_answer_422_and_save_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        for field, bad in (("title", "Mine\x00"), ("description", "Line\x1b[2J"),
                           ("title", "Mi‮ne")):
            body = {"slug": "mine", "title": "Mine", "description": "d", "audience": "workspace",
                    field: bad}
            r = await call(w, "editor_a", "POST", "/api/codebase/packs", json=body)
            assert r.status_code == 422, (field, r.text)
            assert "control characters" in r.text
        assert (await call(w, "editor_a", "GET", "/api/codebase/packs")).json()["packs"] == []

        r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
            "slug": "mine", "title": "Mine", "description": "one\ntwo\tthree", "audience": "workspace"})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]

        r = await upload_zip(w, "editor_a", pid, note_zip("zz"), note="fixed\x00it")
        assert r.status_code == 422 and "control characters" in r.text
        r = await upload_zip(w, "editor_a", pid, note_zip("zz"), note="fixed\nit")
        assert r.status_code == 201, r.text

        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/tokens",
                       json={"label": "ci\x1b", "expires_in_days": 30})
        assert r.status_code == 422 and "control characters" in r.text
