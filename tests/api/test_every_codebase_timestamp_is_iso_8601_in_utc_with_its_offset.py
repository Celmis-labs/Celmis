"""Every moment the Codebase routes answer with is ISO-8601 in UTC and carries its offset.

The contract (docs/CODEBASE.md, "REST"):

  * each `*_at` field of a pack, a version, an invite, a publish token and the
    publish status reads like `2026-10-17T09:30:00+00:00` - the same text whether
    the database hands back timezone-aware values (PostgreSQL) or naive ones
    (SQLite): a browser reads a string without an offset as LOCAL time, and a pack
    uploaded just now would show hours off;
  * a value in another zone is converted to UTC, a naive one is taken as UTC, and
    a missing one stays null;
  * the usage block (`last_used_at`) follows the same rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.knowledge.timestamps import iso_utc
from src.knowledge.usage import summarise
from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world
from tests.api.test_a_pack_token_publishes_one_pack_and_nothing_else import bearer, make_token


def _moments(node, found=None, key=""):
    """Every string under a key that ends in ``_at``, anywhere in the JSON."""
    found = [] if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            _moments(v, found, k)
    elif isinstance(node, list):
        for v in node:
            _moments(v, found, key)
    elif key.endswith("_at") and node is not None:
        found.append((key, node))
    return found


def _is_utc_iso(text: str) -> bool:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0) and text.endswith("+00:00")


def test_a_naive_value_is_taken_as_utc_an_aware_one_is_converted_and_none_stays_none():
    assert iso_utc(datetime(2026, 10, 17, 9, 30, 5)) == "2026-10-17T09:30:05+00:00"
    assert iso_utc(datetime(2026, 10, 17, 9, 30, 5, tzinfo=UTC)) == "2026-10-17T09:30:05+00:00"
    kyiv = timezone(timedelta(hours=3))
    assert iso_utc(datetime(2026, 10, 17, 12, 30, 5, tzinfo=kyiv)) == "2026-10-17T09:30:05+00:00"
    assert iso_utc(None) is None


def test_the_usage_block_writes_its_times_in_utc_even_for_a_naive_row():
    naive = datetime(2026, 10, 17, 9, 30, 5)
    usage = summarise([(naive, ["pack-1/billing"])], "pack-1", skills=["billing"])
    assert usage["last_used_at"] == "2026-10-17T09:30:05+00:00"
    assert usage["skills"][0]["last_used_at"] == "2026-10-17T09:30:05+00:00"


@pytest.mark.parametrize("route", ["pack", "versions", "invites", "tokens", "publish"])
async def test_every_time_a_route_answers_with_is_in_utc_with_its_offset(tmp_path, monkeypatch, route):
    async with world(tmp_path, monkeypatch) as w:
        made = await call(w, "editor_a", "POST", "/api/codebase/packs",
                          json={"slug": "ledger", "title": "Ledger", "audience": "workspace"})
        pid = made.json()["id"]
        assert (await upload_zip(w, "editor_a", pid, note_zip("ledger"))).status_code == 201
        token, _info = await make_token(w, pid)
        assert (await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/invites", json={})).status_code == 201
        base = f"/api/codebase/packs/{pid}"
        if route == "publish":
            answer = await w.client.get("/api/codebase/publish", headers=bearer(token))
        else:
            path = {"pack": base, "versions": f"{base}/versions", "invites": f"{base}/invites",
                    "tokens": f"{base}/tokens"}[route]
            answer = await call(w, "editor_a", "GET", path)
        assert answer.status_code == 200, answer.text
        moments = _moments(answer.json())
        assert moments, f"no timestamp found in the {route} answer"
        assert [(k, v) for k, v in moments if not _is_utc_iso(v)] == []


async def test_the_pack_list_and_the_pack_page_agree_on_when_it_was_last_changed(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        made = await call(w, "editor_a", "POST", "/api/codebase/packs",
                          json={"slug": "ledger", "title": "Ledger", "audience": "workspace"})
        pid = made.json()["id"]
        assert (await upload_zip(w, "editor_a", pid, note_zip("ledger"))).status_code == 201
        listed = (await call(w, "editor_a", "GET", "/api/codebase/packs")).json()["packs"][0]
        page = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")).json()
        assert listed["updated_at"] == page["updated_at"] and _is_utc_iso(page["updated_at"])
