"""The search and the brief over the packs say when they stopped reading.

The contract (docs/CODEBASE.md "Search", src/knowledge/store.py `MAX_RANKED_ITEMS`):

  * `GET /api/codebase/search` and `POST /api/codebase/brief` answer
    `"truncated": true` when the items they ranked were cut at the safety cap, so
    a page can say "narrow the query" instead of showing a short list as if it were
    the whole answer;
  * the key is ABSENT otherwise — an answer under the cap is exactly what it was.
"""

from __future__ import annotations

from src.knowledge import store
from tests.api.codebase_support import call, seeded
from tests.api.rbac_world import world


async def test_an_answer_under_the_cap_carries_no_truncated_key(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        found = await call(w, "su", "GET", "/api/codebase/search?q=recipe")
        briefed = await call(w, "su", "POST", "/api/codebase/brief", json={"task": "recipe"})
        for r in (found, briefed):
            assert r.status_code == 200, r.text
            assert "truncated" not in r.json()
        assert found.json()["hits"]


async def test_a_search_and_a_brief_over_the_cap_say_truncated_true(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        monkeypatch.setattr(store, "MAX_RANKED_ITEMS", 3)
        found = await call(w, "su", "GET", "/api/codebase/search?q=recipe")
        briefed = await call(w, "su", "POST", "/api/codebase/brief", json={"task": "recipe"})
        for r in (found, briefed):
            assert r.status_code == 200, r.text
            assert r.json()["truncated"] is True
        assert 0 < len(found.json()["hits"]) <= 3         # what was read is still answered


async def test_a_brief_that_matches_nothing_still_says_when_it_was_cut(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        monkeypatch.setattr(store, "MAX_RANKED_ITEMS", 1)
        r = await call(w, "su", "POST", "/api/codebase/brief", json={"task": "zzunmatchable"})
        assert r.status_code == 200, r.text
        assert r.json()["items"] == [] and r.json()["truncated"] is True
