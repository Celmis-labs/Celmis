"""Versions of a pack: numbering, the current pointer, rollback, retention.

The contract (docs/CODEBASE.md, "Versions"): every upload is a new immutable
version that becomes the current one; rolling back only moves the pointer; the
newest 20 versions are kept (never dropping the current one) and an older
version's items go with it; the diff names paths, the archive is a faithful and
repeatable zip, and an item of any retained version can be read — but only by
someone who may see the pack.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select

from src.db.models import KnowledgeItem, KnowledgePackVersion
from src.knowledge import store
from src.knowledge.ingest import ingest_zip, version_sha
from src.knowledge.visibility import PackNotAccessible
from tests.knowledge.pack_rig import A, upload

SKILL = "---\nname: billing\ndescription: Call the billing client.\n---\n# Billing\n\n{}\n"


async def pack(world, who="editor", audience="workspace"):
    async with world.session() as s:
        return (await store.create_pack(s, world.principal(who), A, slug="p", title="P",
                                        audience=audience)).pack.id


async def view(s, world, pid, who="editor"):
    return await store.get_pack(s, world.principal(who), A, pid)


async def put(world, pid, *pairs, note="", who="editor"):
    async with world.session() as s:
        return await store.add_version(s, await view(s, world, pid, who), upload(*pairs), note)


async def test_each_upload_is_the_next_number_and_becomes_current(world):
    pid = await pack(world)
    one = await put(world, pid, ("notes/a.md", "# a\n"), note="first")
    two = await put(world, pid, ("notes/a.md", "# a2\n"))
    assert (one.number, two.number) == (1, 2) and one.note == "first"
    async with world.session() as s:
        v = await view(s, world, pid)
        assert v.pack.current_version_id == two.id
        [info] = await store.describe(s, [v])
        assert info.version.number == 2 and info.files == 2 and info.version.current
        listed = await store.list_versions(s, v)
    assert [(x.number, x.current) for x in listed] == [(2, True), (1, False)]


async def test_a_version_records_its_size_count_and_hash(world):
    pid = await pack(world)
    res = upload(("notes/a.md", "# a\n"), ("notes/b.md", "# bb\n"))
    async with world.session() as s:
        ver = await store.add_version(s, await view(s, world, pid), res)
    assert ver.file_count == 3 and ver.total_bytes == sum(f.size_bytes for f in res.files)
    assert ver.sha == version_sha(res.files) and ver.created_by_id == world.uid("editor")


async def test_only_a_maintainer_uploads(world):
    pid = await pack(world)
    async with world.session() as s:
        with pytest.raises(store.NotMaintainer):
            await store.add_version(s, await view(s, world, pid, "member"), upload(("a.md", "# a\n")))
    assert await world.count(KnowledgePackVersion) == 0
    assert await world.count(KnowledgeItem) == 0


async def test_a_note_over_500_characters_is_refused(world):
    pid = await pack(world)
    async with world.session() as s:
        with pytest.raises(store.BadInput):
            await store.add_version(s, await view(s, world, pid), upload(("a.md", "# a\n")),
                                   "n" * 501)


async def test_rolling_back_moves_only_the_pointer(world):
    pid = await pack(world)
    one = await put(world, pid, ("notes/a.md", "# one\n"))
    await put(world, pid, ("notes/a.md", "# two\n"))
    async with world.session() as s:
        await store.activate_version(s, await view(s, world, pid), 1)
    async with world.session() as s:
        v = await view(s, world, pid)
        assert v.pack.current_version_id == one.id
        paths = {i.path: i for i in await store.list_items(s, v)}
        assert "notes/a.md" in paths
        text = (await store.get_item(s, v, paths["notes/a.md"].id))[0].content
    assert "one" in text
    assert await world.count(KnowledgePackVersion) == 2


async def test_activating_a_version_that_is_not_there_is_refused(world):
    pid = await pack(world)
    await put(world, pid, ("a.md", "# a\n"))
    async with world.session() as s:
        v = await view(s, world, pid)
        for number in (0, 2, 99):
            with pytest.raises(store.VersionNotFound):
                await store.activate_version(s, v, number)
        with pytest.raises(store.NotMaintainer):
            await store.activate_version(s, await view(s, world, pid, "member"), 1)


async def test_the_newest_twenty_versions_are_kept_and_older_items_go(world, monkeypatch):
    monkeypatch.setattr(store, "KEEP_VERSIONS", 3)
    pid = await pack(world)
    for n in range(1, 6):
        await put(world, pid, ("notes/a.md", f"# v{n}\n"))
    async with world.session() as s:
        listed = await store.list_versions(s, await view(s, world, pid))
    assert [v.number for v in listed] == [5, 4, 3]
    assert await world.count(KnowledgePackVersion) == 3
    assert await world.count(KnowledgeItem) == 3 * 2                 # README + a.md per version


async def test_pruning_never_drops_the_version_that_is_current(world, monkeypatch):
    pid = await pack(world)
    for n in range(1, 5):
        await put(world, pid, ("notes/a.md", f"# v{n}\n"))
    async with world.session() as s:
        await store.activate_version(s, await view(s, world, pid), 1)
    monkeypatch.setattr(store, "KEEP_VERSIONS", 2)
    async with world.session() as s:
        v = await view(s, world, pid)
        await store._prune_versions(s, v.pack)
        await s.commit()
    async with world.session() as s:
        v = await view(s, world, pid)
        assert [x.number for x in await store.list_versions(s, v)] == [4, 3, 1]
        assert [i.path for i in await store.list_items(s, v)]          # the pointer does not dangle


async def test_the_diff_names_added_removed_and_changed_paths(world):
    pid = await pack(world)
    await put(world, pid, ("notes/keep.md", "# k\n"), ("notes/gone.md", "# g\n"),
              ("notes/edit.md", "# e1\n"))
    await put(world, pid, ("notes/keep.md", "# k\n"), ("notes/edit.md", "# e2\n"),
              ("notes/new.md", "# n\n"))
    async with world.session() as s:
        v = await view(s, world, pid)
        assert await store.diff_versions(s, v, 2) == {
            "added": ["notes/new.md"], "removed": ["notes/gone.md"], "changed": ["notes/edit.md"]}
        assert await store.diff_versions(s, v, 2, 1) == await store.diff_versions(s, v, 2)
        first = await store.diff_versions(s, v, 1)
        assert first["added"] == ["README.md", "notes/edit.md", "notes/gone.md", "notes/keep.md"]
        assert first["removed"] == [] and first["changed"] == []
        assert (await store.diff_versions(s, v, 1, 2))["added"] == ["notes/gone.md"]
        with pytest.raises(store.VersionNotFound):
            await store.diff_versions(s, v, 3)
        with pytest.raises(store.VersionNotFound):
            await store.diff_versions(s, v, 2, 7)


async def test_the_archive_is_a_zip_of_the_version_and_repeats_exactly(world):
    pid = await pack(world)
    await put(world, pid, ("skills/billing/SKILL.md", SKILL.format("x")), ("notes/a.md", "# é\n"))
    async with world.session() as s:
        v = await view(s, world, pid)
        data, name = await store.version_archive(s, v, 1)
        again, _ = await store.version_archive(s, v, 1)
    assert name == "p-v1.zip" and data == again
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["README.md", "notes/a.md", "skills/billing/SKILL.md"]
        assert zf.read("notes/a.md").decode() == "# é\n"
        assert all(i.external_attr >> 16 == 0o644 for i in zf.infolist())


async def test_the_archive_can_be_uploaded_again_unchanged(world):
    pid = await pack(world)
    first = await put(world, pid, ("skills/billing/SKILL.md", SKILL.format("x")),
                      ("notes/a.md", "# a\n"))
    async with world.session() as s:
        data, _ = await store.version_archive(s, await view(s, world, pid), 1)
    assert version_sha(ingest_zip(data).files) == first.sha


async def test_an_item_of_any_retained_version_can_be_read_by_those_who_see_the_pack(world):
    pid = await pack(world)
    await put(world, pid, ("notes/a.md", "# first\n"))
    await put(world, pid, ("notes/a.md", "# second\n"))
    async with world.session() as s:
        old = (await s.scalars(select(KnowledgeItem).where(
            KnowledgeItem.pack_id == pid, KnowledgeItem.path == "notes/a.md"))).all()
        texts = {}
        for item in old:
            row, number = await store.get_item(s, await view(s, world, pid, "member"), item.id)
            texts[number] = row.content
        assert set(texts) == {1, 2} and "first" in texts[1] and "second" in texts[2]
        with pytest.raises(store.ItemNotFound):
            await store.get_item(s, await view(s, world, pid), "not-an-id")
        with pytest.raises(store.ItemNotFound):
            await store.get_item(s, await view(s, world, pid), "99999999-9999-4999-8999-999999999999")


async def test_an_item_of_another_pack_is_not_found_through_this_one(world):
    async with world.session() as s:
        other = (await store.create_pack(s, world.principal("editor"), A, slug="q", title="Q",
                                         audience="workspace")).pack.id
        await store.add_version(s, await view(s, world, other), upload(("a.md", "# secret q\n")))
    pid = await pack(world)
    async with world.session() as s:
        foreign = await s.scalar(select(KnowledgeItem.id).where(KnowledgeItem.pack_id == other))
        with pytest.raises(store.ItemNotFound):
            await store.get_item(s, await view(s, world, pid), foreign)


async def test_a_pack_one_may_not_see_has_no_versions_items_or_archive(world):
    pid = await pack(world, audience="restricted")
    await put(world, pid, ("a.md", "# a\n"))
    async with world.session() as s:
        with pytest.raises(PackNotAccessible):
            await view(s, world, pid, "member")


async def test_two_uploads_at_once_never_share_a_number(world):
    import asyncio

    pid = await pack(world)

    async def go(n):
        try:
            async with world.session() as s:
                return (await store.add_version(
                    s, await view(s, world, pid), upload((f"notes/{n}.md", f"# {n}\n")))).number
        except store.Conflict:
            return None

    numbers = [n for n in await asyncio.gather(*(go(i) for i in range(4))) if n]
    assert len(numbers) == len(set(numbers)) >= 1
    assert await world.count(KnowledgePackVersion) == len(numbers)
