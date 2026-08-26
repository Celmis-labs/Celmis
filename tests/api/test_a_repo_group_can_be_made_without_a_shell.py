"""Repository groups are reachable from the product, and only within a tenant.

THE DEFECT. Cross-repo drift — the deterministic grep that catches a constant
changed in one repository and left behind in its siblings — only runs when a
repository belongs to a GROUP with another member. Groups were YAML files
written by `analyzer group create` on the server, and an audit of the deployed
product found no HTTP route that could make one: 187 paths in `openapi.json`,
not one containing "group".

So a workspace set up over the web — the normal way to use the product — had
no groups, could never have one, and the advertised feature was unreachable.
Worse than absent: the review still ran, found no group, and the model
rendered that silence as a completed cross-repo search that found nothing.

TENANCY IS THE NEW HAZARD. Groups were installation-global, which was harmless
while only a shell could create them. Over HTTP it is not: drift GREPS every
sibling in a group and quotes what it finds into a review comment, so a group
naming another tenant's repository would read their source and publish it.
"""

from __future__ import annotations

import pytest

from src.groups.manager import GroupManager
from src.groups.models import RepoGroup


@pytest.fixture()
def mgr(tmp_path, monkeypatch) -> GroupManager:
    """A manager over an empty directory.

    `workspace_dir` is a pydantic field, so patching it on the CLASS does not
    reach an existing instance — the first version of this fixture did that
    and every test ran against the real groups directory. Setting
    `groups_dir` after construction is the honest way to isolate it.
    """
    m = GroupManager()
    m.groups_dir = tmp_path / "groups"
    m.groups_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.groups.manager._default_manager", m, raising=False)
    monkeypatch.setattr("src.groups.get_group_manager", lambda: m, raising=False)
    monkeypatch.setattr("src.groups.manager.get_group_manager", lambda: m, raising=False)
    # AND the name as the CALLER sees it. `src/review/cross_repo_drift.py:42`
    # does `from src.groups import get_group_manager` at module level, so that
    # module holds its own reference and patching `src.groups` never reaches
    # it. Without this line the drift test passed or failed purely on whether
    # some earlier test had already imported `cross_repo_drift` — which is
    # what "passes alone, fails in company" actually meant here.
    monkeypatch.setattr("src.review.cross_repo_drift.get_group_manager",
                        lambda: m, raising=False)
    yield m
    # See the neighbour file: the cached singleton outlives a function patch.
    import src.groups.manager as gm
    gm._default_manager = None


def make(mgr: GroupManager, name: str, ws: str, repos: list[str]) -> RepoGroup:
    """Create the way the route does — tenant first, one write.

    This used to call `mgr.create(name)` and stamp `workspace_id` afterwards.
    That is the two-write pattern `create()`'s own docstring records as a bug:
    between the two writes the group belonged to "default", and a save that
    then re-stamped it moved the flat file's ownership — which is how one
    tenant could take over another's group by saving over it.
    """
    g = mgr.create(name, workspace_id=ws)
    for r in repos:
        g.add_repo(r)
    mgr.save(g)
    return g


# ─── the routes exist at all ─────────────────────────────────────────


def test_the_product_exposes_group_routes():
    """The whole defect in one assertion: there were none."""
    from src.api.main import build_app

    paths = set(build_app().openapi()["paths"])

    assert "/api/repos/groups" in paths
    assert "/api/repos/groups/{name}/repos" in paths


# ─── tenancy ─────────────────────────────────────────────────────────


def test_a_group_belongs_to_one_workspace(mgr):
    make(mgr, "payments", "ws-1", ["github:acme/api"])

    assert mgr.list("ws-1") == ["payments"]
    assert mgr.list("ws-2") == []


def test_listing_without_a_workspace_still_sees_everything(mgr):
    """The CLI and installation-wide maintenance need the old behaviour."""
    make(mgr, "a", "ws-1", [])
    make(mgr, "b", "ws-2", [])

    assert mgr.list() == ["a", "b"]


def test_groups_containing_respects_the_tenant(mgr):
    from src.sync.git_providers import parse_repo_url
    make(mgr, "payments", "ws-1", ["github:acme/api"])
    slug = parse_repo_url("github:acme/api").slug

    assert [g.name for g in mgr.groups_containing(slug, "ws-1")] == ["payments"]
    assert mgr.groups_containing(slug, "ws-2") == []


def test_drift_only_searches_the_callers_groups(mgr):
    """`_find_group_for_repo` is what stands between a review comment and
    another tenant's source code."""
    from src.review.cross_repo_drift import _find_group_for_repo
    from src.sync.git_providers import parse_repo_url
    make(mgr, "payments", "ws-1", ["github:acme/api", "github:acme/billing"])
    slug = parse_repo_url("github:acme/api").slug

    assert _find_group_for_repo(slug, "ws-1") is not None
    assert _find_group_for_repo(slug, "ws-2") is None


# ─── back-compat with what the CLI already wrote ─────────────────────


def test_a_group_written_before_the_field_existed_still_loads(mgr):
    """Files on disk have no `workspace_id`. Refusing to load them would take
    drift away from the installs that already had it working."""
    path = mgr.groups_dir / "legacy.yaml"
    path.write_text("name: legacy\ndescription: ''\nrepos:\n  - github:acme/api\n")

    g = mgr.load("legacy")

    assert g.workspace_id == "default"
    assert mgr.list("default") == ["legacy"]


def test_the_field_round_trips(mgr):
    make(mgr, "payments", "ws-7", ["github:acme/api"])

    # WITH the tenant. A tenant-scoped group does not answer to its bare name
    # any more — that is the whole point of the scoping, and this assertion
    # used to read `mgr.load("payments")`, which resolves to the flat address
    # shared by everyone.
    assert mgr.load("payments", "ws-7").workspace_id == "ws-7"


def test_the_bare_name_no_longer_reaches_a_scoped_group(mgr):
    """The flat address belongs to nobody in particular, so a lookup without a
    tenant must miss rather than return whatever sits there."""
    from src.groups.manager import GroupNotFoundError

    make(mgr, "payments", "ws-7", ["github:acme/api"])

    with pytest.raises(GroupNotFoundError):
        mgr.load("payments")
