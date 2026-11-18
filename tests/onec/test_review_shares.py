"""A configuration another workspace shares, in the web tab: on the same terms as MCP.

The share counts only when it is named on one of the person's own live read-only
developer tokens in this workspace; a slug the workspace has itself stays its own;
a model sees shared code only when the owner allowed asking; every report built
on a share is noted by share id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.access import effective, shares
from src.access.resolver import RepoAccessDecision
from src.access.shares import SharedGrant
from src.mcp_server.token_store import TokenView
from src.onec import review

WS, OWNER, ME = "ws-recv", "ws-owner", "u-1"
LATER = datetime.now(UTC) + timedelta(days=30)


def _token(tid: str, *, kind: str = "pat", profile: str = "dev", write: bool = False,
           shares_: tuple[str, ...] = ("sh-1",), user: str = ME, ws: str = WS,
           revoked: datetime | None = None) -> TokenView:
    return TokenView(id=tid, kind=kind, workspace_id=ws, user_id=user, issued_by="admin",
                     label=tid, patterns=(), allow_write=write, scopes=(), profile=profile,
                     expires_at=LATER, revoked_at=revoked, shares=shares_)


def _grant(slug: str, *, ask: bool) -> SharedGrant:
    return SharedGrant(share_id="sh-1", repo_slug=slug, owner_workspace_id=OWNER,
                       grantee_workspace_id=WS, deny_globs=(), allow_ask=ask, expires_at=LATER)


@pytest.mark.parametrize(("token", "counted"), [
    (_token("t-ok"), True),
    (_token("t-self", kind="self"), False),             # self-service never carries a share
    (_token("t-write", write=True), False),             # a token that can write never does
    (_token("t-full", profile="full"), False),          # only the developer profile
    (_token("t-other", user="u-2"), False),             # somebody else's token
    (_token("t-elsewhere", ws="ws-x"), False),          # a token of another workspace
    (_token("t-revoked", revoked=datetime.now(UTC)), False),
])
def test_only_a_read_only_developer_token_of_mine_carries_a_share(monkeypatch, token, counted):
    monkeypatch.setattr(review, "_live_tokens", lambda u, w: [token])
    assert (review._held_shares(ME, WS) == {"sh-1": token.id}) is counted


@pytest.fixture()
def world(monkeypatch):
    """Own repo `own`, shared repo `erp` (and `own` shared too, which must stay own)."""
    state = {"held": {"sh-1": "t-ok"}, "ask": False}
    own = {"own": RepoAccessDecision(repo_slug="own")}
    monkeypatch.setattr(effective, "effective_access", lambda p, w, repos=None: dict(own))
    monkeypatch.setattr(effective, "registered_repos",
                        lambda w: {"own": ("acme/own",)} if w == WS else {"erp": ("acme/erp",),
                                                                          "own": ("acme/own",)})
    monkeypatch.setattr(review, "_held_shares", lambda u, w: dict(state["held"]))

    def live(session, *, share_ids, workspace_id, user_id, slugs=None, now=None):
        if "sh-1" not in share_ids or workspace_id != WS:
            return {}
        return {"erp": _grant("erp", ask=state["ask"]), "own": _grant("own", ask=state["ask"])}

    monkeypatch.setattr(shares, "live_grants", live)

    class _S:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("sqlalchemy.orm.Session", _S)
    monkeypatch.setattr("src.access.resolver._sync_engine", lambda: None)
    monkeypatch.setattr(review, "_owner_names", lambda d: {OWNER: "R&D"})
    monkeypatch.setattr(review, "_fresh", lambda slugs: {
        s: SimpleNamespace(branch="main", sha="abcdef1234", age="14d") for s in slugs})
    monkeypatch.setattr(review, "_is_dump", lambda slug, sha, dec: True)
    monkeypatch.setattr("src.repos.upload.upload_revision", lambda slug: None)
    return state


def test_a_share_on_my_token_is_offered_and_says_whose_it_is(world):
    rows = {c.slug: c for c in review.list_configs(ME, False, WS)}
    assert set(rows) == {"own", "erp"}
    assert rows["erp"].shared_from == "R&D" and rows["erp"].name == "acme/erp"
    assert rows["own"].shared_from == ""          # the workspace's own slug stays its own


def test_a_search_finds_a_shared_repository_by_its_name(world):
    assert [c.slug for c in review.list_configs(ME, False, WS, query="acme/erp")] == ["erp"]


def test_without_the_share_on_a_token_nothing_shared_is_offered(world):
    world["held"] = {}
    assert [c.slug for c in review.list_configs(ME, False, WS)] == ["own"]


def test_an_unreadable_share_grants_nothing(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(shares, "live_grants", boom)
    assert [c.slug for c in review.list_configs(ME, False, WS)] == ["own"]


def test_opening_a_shared_configuration_carries_the_share_and_its_ask_flag(world, monkeypatch, tmp_path):
    from tests.onec import acme

    dump = acme.write_dump(tmp_path / "dump")
    from src.onec.config import DirSource

    monkeypatch.setattr(review, "_source", lambda slug, sha, dec: DirSource(dump))
    _cfg, info = review.open_config(ME, False, WS, "erp")
    assert info["share"] == "sh-1" and info["ask_ok"] is False
    _cfg, info = review.open_config(ME, False, WS, "own")
    assert "share" not in info
    world["held"] = {}
    with pytest.raises(review.ReviewError) as e:
        review.open_config(ME, False, WS, "erp")
    assert e.value.status == 403
