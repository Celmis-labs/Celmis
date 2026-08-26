"""One tenant's group name blocked, and disclosed itself to, every other.

`RepoGroup.workspace_id` exists and `list()` filters on it. Its docstring says
why: "a group is a grep target for cross-repo drift, so listing another
tenant's group names is the first step to reading their source."

The FILE was `groups/{name}.yaml`. No tenant in the path. So the read side was
scoped and the write side was not, and the two halves of one rule disagreed:

  * a stranger with an EMPTY group list posted `{"name": "settlement"}` and got
    422 "already exists" — learning that somebody, somewhere on the
    installation, has a group by that name. Reproduced on prod before the fix;
    `backend` and `frontend` returned 201, `settlement` did not.
  * and they could not have the name. In a multi-tenant install the first
    tenant to create "backend" takes it from everyone, permanently.

New groups live under `groups/<tenant>/`. Groups written before this still
live flat and still resolve — the CLI, `purge` and every file already on disk
keep working, and a legacy file belonging to THIS tenant still collides,
because it is the same group at its old address.
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.groups.manager import GroupManager, GroupValidationError, _ws_dir


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    from src.config import Settings

    s = Settings(workspace_dir=tmp_path)
    return GroupManager(settings=s)


WS_A, WS_B = "ws-alpha", "ws-beta"


# ─── the name is per tenant ──────────────────────────────────────────


def test_two_tenants_can_hold_the_same_name(mgr):
    a = mgr.create("backend", workspace_id=WS_A)
    a.workspace_id = WS_A
    mgr.save(a)

    b = mgr.create("backend", workspace_id=WS_B)   # must not raise
    b.workspace_id = WS_B
    mgr.save(b)

    assert mgr.list(WS_A) == ["backend"]
    assert mgr.list(WS_B) == ["backend"]


def test_one_tenant_still_cannot_take_its_own_name_twice(mgr):
    g = mgr.create("backend", workspace_id=WS_A)
    g.workspace_id = WS_A
    mgr.save(g)

    with pytest.raises(GroupValidationError):
        mgr.create("backend", workspace_id=WS_A)


def test_each_tenant_reads_back_its_own(mgr):
    """The failure this would hide: two files, one name, and `load` picking
    whichever it met first."""
    for ws, desc in ((WS_A, "alpha's"), (WS_B, "beta's")):
        g = mgr.create("backend", workspace_id=ws)
        g.workspace_id, g.description = ws, desc
        mgr.save(g)

    assert mgr.load("backend", WS_A).description == "alpha's"
    assert mgr.load("backend", WS_B).description == "beta's"


def test_a_stranger_does_not_learn_the_name_is_taken(mgr):
    """The disclosure, stated as the behaviour rather than as the message: a
    tenant who owns nothing gets the name."""
    a = mgr.create("settlement", workspace_id=WS_A)
    a.workspace_id = WS_A
    mgr.save(a)

    assert mgr.list(WS_B) == [], "beta owns nothing"
    mgr.create("settlement", workspace_id=WS_B)  # and can still have the name


def test_deleting_one_leaves_the_other(mgr):
    for ws in (WS_A, WS_B):
        g = mgr.create("backend", workspace_id=ws)
        g.workspace_id = ws
        mgr.save(g)

    assert mgr.delete("backend", WS_A) is True
    assert mgr.list(WS_A) == []
    assert mgr.list(WS_B) == ["backend"], "beta's group went with alpha's"


# ─── and nothing already on disk is orphaned ─────────────────────────


def test_a_group_written_before_this_still_loads(mgr, tmp_path):
    """The CLI wrote `groups/{name}.yaml` with no tenant. Those files are the
    installation's real data and a namespace change must not lose them."""
    import yaml

    flat = tmp_path / "groups" / "legacy.yaml"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text(yaml.safe_dump({
        "name": "legacy", "description": "written by the CLI",
        "repos": ["github:acme/api"], "workspace_id": "default",
    }), encoding="utf-8")

    assert mgr.load("legacy", "default").description == "written by the CLI"
    assert "legacy" in mgr.list("default")


def test_a_legacy_name_still_collides_for_its_own_owner(mgr, tmp_path):
    """It is the same group at its old address — granting the name again would
    give one tenant two groups that are one group."""
    import yaml

    flat = tmp_path / "groups" / "shared.yaml"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text(yaml.safe_dump({"name": "shared", "workspace_id": WS_A}),
                    encoding="utf-8")

    with pytest.raises(GroupValidationError):
        mgr.create("shared", workspace_id=WS_A)


def test_a_legacy_name_does_not_block_a_different_tenant(mgr, tmp_path):
    import yaml

    flat = tmp_path / "groups" / "shared.yaml"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text(yaml.safe_dump({"name": "shared", "workspace_id": WS_A}),
                    encoding="utf-8")

    mgr.create("shared", workspace_id=WS_B)  # must not raise


def test_a_workspace_id_cannot_escape_the_groups_directory(mgr, tmp_path):
    """Ids are uuids today. A group directory is not where you want to find
    out that tomorrow's slug contained a slash."""
    g = mgr.create("x", workspace_id="../../etc")
    g.workspace_id = "../../etc"
    mgr.save(g)

    written = list((tmp_path / "groups").rglob("x.yaml"))
    assert written, "nothing was written"
    assert (tmp_path / "groups") in written[0].parents


# ─── the payload that was silently ignored ───────────────────────────


def test_the_create_payload_refuses_a_field_it_would_ignore():
    """`{"repo_slugs": [...]}` — the wrong name, and an easy one to reach for —
    returned 201 and an EMPTY group. Every sibling payload in this API forbids
    extras; this one did not."""
    from pydantic import ValidationError

    from src.api.routers.groups import GroupCreate

    with pytest.raises(ValidationError):
        GroupCreate(name="g", repo_slugs=["github_acme-api"])

    assert GroupCreate(name="g", repos=["github_acme-api"]).repos == ["github_acme-api"]


# ─── list and load have to agree ─────────────────────────────────────


def test_a_group_the_list_shows_is_a_group_the_loader_can_read(mgr):
    """The asymmetry the scoped path introduced, and the reason the drift
    detector went blind for one commit: `list(ws)` walked the tenant's
    directory while `load(name)` looked only at the flat address, so a group
    was visible and unreadable at once. Every `list(ws)` in the tree is now
    followed by `load(name, ws)`."""
    g = mgr.create("visible", workspace_id=WS_A)
    mgr.save(g)

    for name in mgr.list(WS_A):
        assert mgr.load(name, WS_A).name == name


def test_the_drift_detector_finds_a_tenant_group(mgr, monkeypatch):
    """End to end for the feature the namespace exists to serve. A group only
    fires drift when it has a SECOND member, so both are added."""
    import src.review.cross_repo_drift as drift

    g = mgr.create("settlement", workspace_id=WS_A)
    g.add_repo("github:acme/probe")
    g.add_repo("github:acme/sibling")
    mgr.save(g)
    monkeypatch.setattr(drift, "get_group_manager", lambda: mgr)

    found = drift._find_group_for_repo("github_acme-probe", WS_A)
    assert found is not None, "drift cannot find a group its own tenant owns"
    name, others = found
    assert name == "settlement"
    assert others == ["github_acme-sibling"]


def test_the_drift_detector_does_not_find_another_tenant_s(mgr, monkeypatch):
    """The half the scoping is for: drift GREPS every sibling and quotes what
    it finds into a review comment."""
    import src.review.cross_repo_drift as drift

    g = mgr.create("settlement", workspace_id=WS_A)
    g.add_repo("github:acme/probe")
    g.add_repo("github:acme/sibling")
    mgr.save(g)
    monkeypatch.setattr(drift, "get_group_manager", lambda: mgr)

    assert drift._find_group_for_repo("github_acme-probe", WS_B) is None


# ─── a name is not an address ────────────────────────────────────────


def test_groups_containing_can_open_everything_it_finds(mgr):
    """The regression the namespace change introduced, and the reason a
    feature stopped working without a single test going red.

    `groups_containing()` listed NAMES installation-wide and then loaded each
    from the flat address. A tenant-scoped group was therefore visible and
    unopenable — and `sync/incremental` uses exactly this call to decide which
    groups need their cross-repo edges rebuilt after a re-index. Cross-repo
    materialisation quietly stopped for every tenant that had a group."""
    g = mgr.create("settlement", workspace_id=WS_A)
    g.add_repo("github:acme/probe")
    mgr.save(g)

    found = mgr.groups_containing("github_acme-probe")   # installation-wide
    assert [x.name for x in found] == ["settlement"], (
        "listed by name, opened at the wrong address"
    )


def test_groups_containing_is_still_scoped_when_asked(mgr):
    for ws in (WS_A, WS_B):
        g = mgr.create("settlement", workspace_id=ws)
        g.add_repo("github:acme/probe")
        mgr.save(g)

    assert len(mgr.groups_containing("github_acme-probe")) == 2
    assert len(mgr.groups_containing("github_acme-probe", WS_A)) == 1
    assert mgr.groups_containing("github_acme-probe", WS_A)[0].workspace_id == WS_A


def test_the_handler_rebuilds_the_tenant_it_was_given(tmp_path, monkeypatch):
    """Two tenants hold a group called "product". The job names one of them,
    and the handler must open THAT one.

    This used to read the source of `handle_cross_repo_materialize` and look
    for the string `p.get("workspace_id")` — as did the two tests either side
    of it, which grepped `src/sync/incremental.py` for the payload key and the
    dedup key. All three passed on text and broke the moment the enqueue moved
    into the shared hook both index paths now call, without anything about the
    behaviour changing. A test that fails when the code improves was keyed on
    the wrong thing; the payload and the key are asserted on the hook itself in
    tests/sync/test_both_index_paths_schedule_cross_repo_edges.py.
    """
    import asyncio

    from src.sync.handlers import handle_cross_repo_materialize

    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))
    for ws, repo in ((WS_A, "github:acme/a-only"), (WS_B, "github:acme/b-only")):
        g = mgr.create("product", workspace_id=ws)
        g.workspace_id = ws
        g.add_repo(repo)
        mgr.save(g)

    opened: list = []
    monkeypatch.setattr("src.groups.get_group_manager", lambda: mgr)
    monkeypatch.setattr("src.groups.indexer.rematerialize_group",
                        lambda group: opened.append(group) or 0)

    asyncio.run(handle_cross_repo_materialize(
        {"payload": {"group_name": "product", "workspace_id": WS_B}}
    ))

    assert len(opened) == 1
    assert opened[0].workspace_id == WS_B
    assert opened[0].repos == ["github:acme/b-only"], (
        "the handler rebuilt the other tenant's group"
    )


def test_the_handler_rebuilds_nothing_when_the_group_is_gone(tmp_path, monkeypatch):
    """A deleted group must not fall back to whatever sits at the flat address."""
    import asyncio

    from src.sync.handlers import handle_cross_repo_materialize

    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))
    g = mgr.create("product", workspace_id=WS_A)
    g.workspace_id = WS_A
    g.add_repo("github:acme/a-only")
    mgr.save(g)

    opened: list = []
    monkeypatch.setattr("src.groups.get_group_manager", lambda: mgr)
    monkeypatch.setattr("src.groups.indexer.rematerialize_group",
                        lambda group: opened.append(group) or 0)

    asyncio.run(handle_cross_repo_materialize(
        {"payload": {"group_name": "product", "workspace_id": WS_B}}
    ))

    assert opened == []


# ─── the write address is not "wherever that name already sits" ──────
#
# The scoping fix left `save()` choosing its address with `legacy.exists()` —
# "somebody has this name flat" read as "I have this name flat". A legacy flat
# group is exactly what a CLI-era install is full of, so this was reachable the
# day the scoping shipped.


def _legacy_flat_group(tmp_path, name, ws, repos):
    """What the CLI left behind before groups were tenant-scoped."""
    import yaml

    d = tmp_path / "groups"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(
        yaml.safe_dump({"name": name, "description": "", "repos": repos,
                        "workspace_id": ws}),
        encoding="utf-8",
    )
    return d / f"{name}.yaml"


def test_creating_a_name_a_stranger_holds_flat_does_not_destroy_it(tmp_path):
    import yaml

    victim = _legacy_flat_group(tmp_path, "backend", WS_A,
                                ["github:tenant-a/secret-billing"])
    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))

    g = mgr.create("backend", workspace_id=WS_B)
    g.add_repo("github:tenant-b/checkout")
    mgr.save(g)

    still = yaml.safe_load(victim.read_text(encoding="utf-8"))
    assert still["workspace_id"] == WS_A
    assert still["repos"] == ["github:tenant-a/secret-billing"], (
        "one tenant's create replaced another tenant's group, repos and all"
    )


def test_the_stranger_still_gets_their_own_group(tmp_path):
    _legacy_flat_group(tmp_path, "backend", WS_A, ["github:tenant-a/billing"])
    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))

    g = mgr.create("backend", workspace_id=WS_B)
    g.add_repo("github:tenant-b/checkout")
    mgr.save(g)

    assert mgr.load("backend", WS_B).repos == ["github:tenant-b/checkout"]
    assert mgr.load("backend", WS_A).repos == ["github:tenant-a/billing"]


def test_a_tenants_own_legacy_group_stays_at_its_old_address(tmp_path):
    """Back-compat, unchanged: moving files under a running install is a
    migration, not a save."""
    legacy = _legacy_flat_group(tmp_path, "backend", WS_A, ["github:acme/one"])
    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))

    g = mgr.load("backend", WS_A)
    g.add_repo("github:acme/two")
    mgr.save(g)

    assert not (tmp_path / "groups" / _ws_dir(WS_A) / "backend.yaml").exists()
    assert len(mgr.load("backend", WS_A).repos) == 2
    assert legacy.exists()


def test_an_unreadable_flat_file_does_not_capture_the_write(tmp_path):
    """A corrupt file owns nothing: the save must go to the tenant's own
    address rather than overwrite something it could not identify."""
    d = tmp_path / "groups"
    d.mkdir(parents=True, exist_ok=True)
    (d / "backend.yaml").write_text("{{{ not yaml", encoding="utf-8")
    mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))

    g = mgr.create("backend", workspace_id=WS_B)
    g.add_repo("github:tenant-b/checkout")
    mgr.save(g)

    assert (d / _ws_dir(WS_B) / "backend.yaml").exists()
    assert (d / "backend.yaml").read_text(encoding="utf-8") == "{{{ not yaml"
