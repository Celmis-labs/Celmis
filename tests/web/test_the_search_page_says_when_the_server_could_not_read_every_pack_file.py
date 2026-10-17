"""When the server says a search or a brief was made without every pack file, the
page says so in one line.

The contract (src/api/routers/codebase.py, web/lib/api.ts): the REST search and
brief answers may carry `truncated: true` — the files that could be searched hit
a ceiling, so a pack may be missing from the result. The types name the optional
flag, the search page shows ONE notice for it next to the results and next to
the brief, and the sentence exists in all sixteen languages (English and
Ukrainian written by hand, the other fourteen translated from them).
"""

from __future__ import annotations

import json
import re

from tests.web.test_a_configured_reasoning_setting_survives_the_save import WEB, _strip_comments

API = WEB / "lib" / "api.ts"
PAGE = WEB / "app" / "(app)" / "codebase" / "search" / "page.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"
KEY = "codebase.search.truncated"


def test_the_types_of_the_search_and_brief_answers_carry_the_optional_flag():
    code = _strip_comments(API.read_text(encoding="utf-8"))
    brief = re.search(r"export type PackBrief = \{(.*?)\n\};", code, re.S)
    assert brief and "truncated?: boolean;" in brief.group(1)
    assert re.search(r"api<\{ hits: PackSearchHit\[\]; truncated\?: boolean \}>", code)


def test_the_page_shows_the_notice_for_the_hits_and_for_the_brief_and_nowhere_else():
    code = _strip_comments(PAGE.read_text(encoding="utf-8"))
    assert "hits.data?.truncated" in code
    assert "brief.data.truncated" in code
    assert code.count(f't("{KEY}")') == 2


def test_the_notice_is_one_line_in_every_language_and_written_for_real_in_two():
    texts = {}
    for path in sorted(MESSAGES.glob("*.json")):
        texts[path.stem] = json.loads(path.read_text(encoding="utf-8")).get(KEY)
    assert len(texts) == 16 and all(texts.values()), texts
    assert all("\n" not in text and "{" not in text for text in texts.values())
    assert texts["en"] != texts["uk"] and re.search(r"[А-Яа-яІіЇїЄє]", texts["uk"])
    assert all(t != texts["en"] for k, t in texts.items() if k not in ("en", "uk"))
