"""What search, the brief and the MCP helpers are allowed to read.

The contract (docs/CODEBASE.md, "Search"): only the CURRENT version of a pack
the caller may see is searched; an item of a hidden pack, an old version or
another workspace never ranks; the async and the synchronous readers answer
alike; anything that goes wrong reads as "nothing found"; and file text is only
handed out for items inside visible packs.
"""

from __future__ import annotations

import pytest

from src.knowledge import store
from src.knowledge.visibility import NOBODY
from tests.knowledge.pack_rig import TEAM_A, A, B, upload

SKILL = "---\nname: {name}\ndescription: {desc}\n---\n# {name}\n\n{body}\n"


def skill(name, desc="Calls the billing client", body="Use retries."):
    return SKILL.format(name=name, desc=desc, body=body)


async def pack(world, slug, who="editor", ws=A, **kw):
    async with world.session() as s:
        return (await store.create_pack(s, world.principal(who, ws), ws, slug=slug,
                                        title=slug.title(), **kw)).pack.id


async def put(world, pid, *pairs, who="editor", ws=A):
    async with world.session() as s:
        view = await store.get_pack(s, world.principal(who, ws), ws, pid)
        return await store.add_version(s, view, upload(*pairs))


@pytest.fixture
async def filled(world):
    shown = await pack(world, "shown", audience="workspace")
    await put(world, shown, ("billing/SKILL.md", skill("billing")),
              ("billing/reference/retries.md", "# Retries\n\nBack off twice on a 429 reply.\n"),
              ("notes/ledger.md", "# Ledger\n\nReconcile ledgers nightly.\n"))
    secret = await pack(world, "secret", audience="restricted")
    await put(world, secret, ("notes/payroll.md", "# Payroll\n\nReconcile payroll ledgers quarterly.\n"))
    teamed = await pack(world, "teamed", audience="teams", teams=[(TEAM_A, "read")])
    await put(world, teamed, ("notes/ops.md", "# Ops\n\nReconcile ops ledgers hourly.\n"))
    other = await pack(world, "other", who="admin_b", ws=B, audience="workspace")
    await put(world, other, ("notes/b.md", "# B\n\nReconcile ledgers in B.\n"), who="admin_b", ws=B)
    return world


async def found(world, who, query, ws=A, limit=20):
    async with world.session() as s:
        return [(h.pack.slug, h.item.path) for h in
                await store.search(s, world.principal(who, ws), ws, query, limit)]


async def test_a_search_returns_only_what_the_caller_may_read(filled):
    q = "reconcile ledgers"
    assert {p for p, _ in await found(filled, "viewer", q)} == {"shown"}
    assert {p for p, _ in await found(filled, "member", q)} == {"shown", "teamed"}
    assert {p for p, _ in await found(filled, "editor", q)} == {"shown", "secret", "teamed"}
    assert {p for p, _ in await found(filled, "su", q)} == {"shown", "secret", "teamed"}
    assert await found(filled, "outsider", q) == []
    assert {p for p, _ in await found(filled, "admin_b", q, ws=B)} == {"other"}


async def test_a_hidden_packs_words_do_not_even_change_the_count_of_hits(filled):
    assert await found(filled, "viewer", "payroll") == []
    assert await found(filled, "viewer", "quarterly payroll") == []


async def test_the_best_match_comes_first_with_its_paragraph(filled):
    async with filled.session() as s:
        hits = await store.search(s, filled.principal("viewer"), A, "retries 429", 5)
    assert hits[0].item.path == "billing/reference/retries.md"
    assert "Back off twice" in hits[0].snippet and hits[0].score > 0
    assert hits[0].pack.slug == "shown" and hits[0].version == 1


async def test_a_limit_caps_the_hits_and_a_query_nothing_matches_is_empty(filled):
    assert len(await found(filled, "editor", "reconcile ledgers", limit=2)) == 2
    assert await found(filled, "editor", "zzzzqqqq") == []
    assert await found(filled, "editor", "") == []


async def test_an_old_version_is_not_searched(world):
    pid = await pack(world, "p", audience="workspace")
    await put(world, pid, ("notes/a.md", "# Old\n\nThe zanzibar procedure.\n"))
    await put(world, pid, ("notes/a.md", "# New\n\nThe reykjavik procedure.\n"))
    assert await found(world, "viewer", "zanzibar") == []
    assert await found(world, "viewer", "reykjavik") == [("p", "notes/a.md")]
    async with world.session() as s:
        await store.activate_version(s, await store.get_pack(s, world.principal("editor"), A, pid), 1)
    assert await found(world, "viewer", "zanzibar") == [("p", "notes/a.md")]
    assert await found(world, "viewer", "reykjavik") == []


async def test_a_pack_with_no_upload_yet_has_nothing_to_find(world):
    await pack(world, "empty", audience="workspace")
    assert await found(world, "viewer", "anything") == []
    async with world.session() as s:
        assert await store.visible_current_items(s, world.principal("viewer"), A) == []


async def test_the_sync_reader_agrees_with_the_async_one_for_everyone(filled):
    for who in ("su", "ga", "owner", "admin", "editor", "editor2", "member", "viewer", "outsider"):
        principal = filled.principal(who)
        async with filled.session() as s:
            wanted = sorted(i.id for _p, _n, i in await store.visible_current_items(s, principal, A))
        got = sorted(i.id for _p, _n, i in store.visible_current_items_sync(principal, A))
        assert got == wanted, who


async def test_nobody_reads_nothing_and_a_broken_database_reads_as_nothing(filled, monkeypatch):
    assert store.visible_current_items_sync(NOBODY, A) == []
    assert store.find_skill_sync(NOBODY, A, "shown/billing") is None
    assert store.item_contents_sync(NOBODY, A, ["99999999-9999-4999-8999-999999999999"]) == {}
    principal = filled.principal("editor")
    from src.access import resolver

    class Boom:
        def connect(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(resolver, "_ENGINE", Boom())
    assert store.visible_current_items_sync(principal, A) == []
    assert store.find_skill_sync(principal, A, "shown/billing") is None


async def test_a_skill_is_found_by_pack_and_name_with_its_files(filled):
    bundle = store.find_skill_sync(filled.principal("viewer"), A, "shown/billing")
    assert bundle.pack.slug == "shown" and bundle.version == 1
    assert bundle.skill.path == "billing/SKILL.md" and bundle.directory == "billing"
    assert "Use retries." in bundle.content
    assert [f.path for f in bundle.files] == ["billing/SKILL.md", "billing/reference/retries.md"]


async def test_a_bare_skill_name_works_only_when_it_is_unique_among_visible_packs(filled):
    again = await pack(filled, "again", audience="workspace")
    await put(filled, again, ("billing/SKILL.md", skill("billing", "A second one")))
    viewer = filled.principal("viewer")
    assert store.find_skill_sync(viewer, A, "billing") is None            # two visible: ambiguous
    assert store.find_skill_sync(viewer, A, "again/billing").pack.slug == "again"
    assert store.find_skill_sync(viewer, A, "shown/billing").pack.slug == "shown"
    hidden = await pack(filled, "zzz", audience="restricted")
    await put(filled, hidden, ("only/SKILL.md", skill("only")))
    assert store.find_skill_sync(filled.principal("editor"), A, "only").pack.slug == "zzz"
    assert store.find_skill_sync(viewer, A, "only") is None
    assert store.find_skill_sync(viewer, A, "zzz/only") is None


@pytest.mark.parametrize("name", ["", "/", "shown/", "/billing", "shown/billing/x", "nope/nope",
                                  "shown/reference", "..", "billing\x00"])
async def test_a_skill_name_that_is_nothing_is_none(filled, name):
    assert store.find_skill_sync(filled.principal("su"), A, name) is None


async def test_a_pack_by_its_slug_lists_its_current_items_when_visible(filled):
    assert {t[2].path for t in store.pack_items_sync(filled.principal("viewer"), A, "shown")} >= {
        "notes/ledger.md", "billing/SKILL.md"}
    assert store.pack_items_sync(filled.principal("viewer"), A, "secret") == []
    assert store.pack_items_sync(filled.principal("viewer"), A, "missing") == []


async def test_text_is_handed_out_only_for_items_in_visible_packs(filled):
    editor = filled.principal("editor")
    viewer = filled.principal("viewer")
    items = {i.path: i.id for _p, _n, i in store.visible_current_items_sync(editor, A)}
    ledger, payroll = items["notes/ledger.md"], items["notes/payroll.md"]
    ids = [ledger, payroll, "garbage", "", ledger]
    async with filled.session() as s:
        got = await store.item_contents(s, viewer, A, ids)
        assert list(got) == [ledger] and "nightly" in got[ledger]
        assert set(await store.item_contents(s, editor, A, ids)) == {ledger, payroll}
        assert await store.item_contents(s, viewer, B, ids) == {}              # wrong workspace
    assert list(store.item_contents_sync(viewer, A, ids)) == [ledger]
    assert set(store.item_contents_sync(editor, A, ids)) == {ledger, payroll}
    assert store.item_contents_sync(viewer, B, ids) == {}


async def test_the_brief_built_from_the_sync_reader_quotes_only_visible_packs(filled):
    from src.knowledge.brief import build_brief

    def brief(who):
        principal = filled.principal(who)
        rows = store.visible_current_items_sync(principal, A)
        return build_brief("reconcile ledgers nightly", rows, lambda _s: False, 4000,
                           load_content=lambda ids: store.item_contents_sync(principal, A, ids))

    viewer, editor = brief("viewer"), brief("editor")
    assert "payroll" not in viewer.text.lower() and "ops" not in viewer.text.lower()
    assert "Reconcile ledgers nightly" in viewer.text
    assert "payroll" in editor.text.lower()
    assert len(editor.item_ids) > len(viewer.item_ids)
