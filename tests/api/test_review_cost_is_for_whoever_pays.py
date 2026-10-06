"""What a review COST is for whoever pays for the workspace.

Owner and admin of the workspace (and global admin) see `cost_usd` /
`cost_source` on review runs and the cost block of the review analytics;
everybody else gets the run — verdict, findings, tokens — without a price. The
field is absent or null, never 0: "free" would be a claim.

Surfaces covered: /api/reviews (history, run detail), /api/pull-requests/{id}/runs,
/api/analytics/summary, the agent verbs list_reviews / get_review_run, and the
MCP `get_review` tool. `/api/usage/summary` is deliberately not part of this.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, world

RUN_ID = "run-cost-1"
PR_ID = "pr-cost-1"
COST = 1.2345


async def _seed(w) -> None:
    from src.api.review_runs import ReviewRun, get_review_run_store
    from src.db.models import ReviewPullRequest

    store = get_review_run_store()
    store.insert(ReviewRun(
        id=RUN_ID, user_id=w.uid("admin_a"), pr_ref=f"github:{A_REPO_FULL}#7",
        status="complete", verdict="approve", findings_count=1,
        started_at=datetime.now(UTC).isoformat(), elapsed_seconds=3.0,
        workspace_id=w.ws["ws-a"], pr_provider="github",
        pr_repo=A_REPO_FULL, pr_number=7))
    # The insert writes the identity of a run; cost and tokens arrive on finish.
    store.update(RUN_ID, cost_usd=COST, cost_source="litellm_estimate",
                 tokens_input=1000, tokens_output=200)
    async with w.factory() as s:
        # Reading a run is reading its repository: the people below who see a
        # run without its price are on the team that holds the grant.
        from src.db.models import TeamMember

        for who in ("viewer_a", "member_a"):
            s.add(TeamMember(team_id="team-a", user_id=w.uid(who), role="member"))
        s.add(ReviewPullRequest(
            id=PR_ID, workspace_id=w.ws["ws-a"], provider="github", repo=A_REPO_FULL,
            number=7, repo_slug=A_REPO, title="t"))
        await s.commit()


PAYERS = ["admin_a", "owner_a", "gadmin"]
OTHERS = ["viewer_a", "member_a", "editor_a"]


def _runs(body) -> list[dict]:
    if isinstance(body, dict) and "items" in body:
        return body["items"]
    return body if isinstance(body, list) else [body]


def _urls() -> list[str]:
    return ["/api/reviews/history", f"/api/reviews/{RUN_ID}",
            f"/api/pull-requests/{PR_ID}/runs"]


@pytest.mark.parametrize("who", PAYERS)
async def test_the_payer_sees_what_a_review_cost(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        for url in _urls():
            r = await w.client.get(url, headers=w.h(who, "ws-a"))
            assert r.status_code == 200, (url, r.text)
            run = next(x for x in _runs(r.json()) if x["id"] == RUN_ID)
            assert run["cost_usd"] == COST and run["cost_source"] == "litellm_estimate", url


@pytest.mark.parametrize("who", OTHERS)
async def test_everyone_else_gets_the_run_without_a_price(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        for url in _urls():
            r = await w.client.get(url, headers=w.h(who, "ws-a"))
            assert r.status_code == 200, (url, r.text)
            run = next(x for x in _runs(r.json()) if x["id"] == RUN_ID)
            assert run["cost_usd"] is None and run["cost_source"] is None, url
            assert run["tokens_input"] == 1000, "tokens are not the price"
            assert str(COST) not in r.text


async def _analytics(w, who: str) -> dict:
    # `started_at` must be inside the window; the seeded run is "now".
    r = await w.client.get("/api/analytics/summary", params={"days": 30},
                           headers=w.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("who", ["admin_a", "owner_a", "gadmin"])
async def test_analytics_shows_the_cost_block_to_the_payer(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        out = await _analytics(w, who)
        assert out["cost_usd"]["total"] == pytest.approx(COST)
        assert out["cost_basis"]


async def test_analytics_leaves_the_cost_out_for_an_editor(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        out = await _analytics(w, "editor_a")
        assert "cost_usd" not in out and "cost_basis" not in out
        assert out["reviews"]["total"] == 1, "everything else is still answered"


async def test_an_owner_elsewhere_sees_no_cost_of_this_workspace(tmp_path, monkeypatch):
    """Owner of B asking in B reads B's runs; A's run is not found, let alone priced."""
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        r = await w.client.get(f"/api/reviews/{RUN_ID}", headers=w.h("admin_b", "ws-b"))
        assert r.status_code == 404 and str(COST) not in r.text


async def test_the_database_row_is_untouched(tmp_path, monkeypatch):
    """Hiding is a presentation rule: the stored cost stays for the payer."""
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        await w.client.get(f"/api/reviews/{RUN_ID}", headers=w.h("member_a", "ws-a"))
        from src.api.review_runs import get_review_run_store

        with sqlite3.connect(get_review_run_store().db_path) as c:
            (cost,) = c.execute("SELECT cost_usd FROM review_runs WHERE id=?",
                                (RUN_ID,)).fetchone()
        assert cost == COST


# ─── the agent: same people, same rule ───────────────────────────────


def _actor(w, who: str):
    from src.automation.actions import Actor

    return Actor(user_id=w.uid(who), email=f"{who}@acme-corp.io",
                 workspace_id=w.ws["ws-a"], label="test")


@pytest.mark.parametrize("who", PAYERS + ["editor_a"])  # editor_a holds the team grant
async def test_the_agent_never_prices_a_review_for_anyone_below_admin(
        tmp_path, monkeypatch, who):
    """`list_reviews` / `get_review_run` read the route functions, so they get
    whatever that caller may see; the rows they build carry no price at all, and
    the model that explains them is handed none."""
    from src.automation.actions_reviews import get_review_run, list_reviews

    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        async with w.factory() as s:
            listed = await list_reviews(_actor(w, who), s)
            one = await get_review_run(_actor(w, who), s, run_id=RUN_ID)
        for out in (listed, one):
            assert "cost_usd" not in repr(out) and "price" not in repr(out).lower()
            assert str(COST) not in repr(out)
        assert listed["runs"], "the run is listed"


# ─── MCP ─────────────────────────────────────────────────────────────


def _mcp_review(monkeypatch, w, who: str):
    """`get_review` as the token of `who` would see it."""
    from src.mcp_server import http_app
    from src.mcp_server.identity import McpCaller

    user = w.users[who]
    caller = McpCaller(user.id, bool(user.is_admin), w.ws["ws-a"], (), authenticated=True)
    monkeypatch.setattr("src.mcp_server.identity.resolve_caller", lambda: caller)
    return http_app._caller_sees_review_cost()


@pytest.mark.parametrize("who, sees", [
    ("owner_a", True), ("admin_a", True), ("gadmin", True),
    ("editor_a", False), ("member_a", False), ("viewer_a", False),
])
async def test_mcp_get_review_follows_the_token_owners_role(tmp_path, monkeypatch, who, sees):
    async with world(tmp_path, monkeypatch) as w:
        assert _mcp_review(monkeypatch, w, who) is sees


async def test_an_mcp_token_of_another_workspace_sees_no_cost(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        from src.mcp_server import http_app
        from src.mcp_server.identity import McpCaller

        # admin_b's token, bound to workspace A they do not belong to.
        caller = McpCaller(w.uid("admin_b"), False, w.ws["ws-a"], (), authenticated=True)
        monkeypatch.setattr("src.mcp_server.identity.resolve_caller", lambda: caller)
        assert http_app._caller_sees_review_cost() is False
