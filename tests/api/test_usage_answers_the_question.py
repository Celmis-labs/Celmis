"""The Usage page has to answer a question, not display a table.

The question is always some version of "where did that money go" — and the
answer is only worth anything if the numbers hold still while you narrow them.
These tests pin the backend behaviour that makes narrowing trustworthy:

    GET /api/spend/summary   totals + every breakdown, one window, one filter set
    GET /api/spend/daily     the same window and the same filters, over time

They run the real handlers against a real database. SQLite stands in for
Postgres — the ledger is one flat append-only table, so the only Postgres-ism
in the queries is ``date_trunc``, which is supplied below as a user function so
the bucketing logic under test is the shipped logic and not a re-implementation.

Where a property cannot be observed over HTTP — "there is a migration for this
column" — the source is parsed with :mod:`ast` rather than grepped. Grepping
for a token finds it in the comment explaining its absence, which has happened
five times in this repository.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
import types
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import DateTime, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.functions import GenericFunction

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import spend as spend_router
from src.db.models import LlmSpend
from src.db.session import get_async_session
from src.users.models import User

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"

WS = "ws-under-test"
OTHER_WS = "ws-somebody-else"


# ══════════════════════════════════════════════════════════════════════
#  Harness — a real ledger, the real handlers
# ══════════════════════════════════════════════════════════════════════


class date_trunc(GenericFunction):  # noqa: N801 — the SQL function's own name
    """Teach SQLAlchemy that ``date_trunc`` returns a timestamp.

    Without a type the driver hands back a bare string and the handler's
    ``.date().isoformat()`` blows up — on SQLite only. Declaring the type is
    true of Postgres too, so this does not bend the query under test.
    """

    type = DateTime()
    name = "date_trunc"
    inherit_cache = True


_TRUNC_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _sqlite_date_trunc(unit: str, value: str | None) -> str | None:
    """Postgres' ``date_trunc(unit, ts)``, for SQLite."""
    if value is None:
        return None
    stamp = dt.datetime.fromisoformat(value)
    if unit == "hour":
        stamp = stamp.replace(minute=0, second=0, microsecond=0)
    elif unit == "week":
        stamp = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
        stamp -= dt.timedelta(days=stamp.weekday())
    elif unit == "month":
        stamp = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # "day" — and anything the handler mapped to it
        stamp = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return stamp.strftime(_TRUNC_FMT)


def _fake_completion(**_kwargs):
    """A LiteLLM response carrying the fields the client reads off it."""
    return types.SimpleNamespace(
        usage=types.SimpleNamespace(
            prompt_tokens=1200,
            completion_tokens=340,
            prompt_tokens_details={"cached_tokens": 900},
        ),
        choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content="{}"),
            finish_reason="stop",
        )],
    )


def row(
    *,
    surface: str = "qa",
    agent: str | None = None,
    model: str = "gemini-flash",
    provider: str = "google",
    cost: float = 0.0,
    cost_source: str = "litellm_estimate",
    tokens_in: int = 100,
    tokens_out: int = 10,
    cached: int = 0,
    user_id: str | None = None,
    repo: str | None = None,
    operation: str | None = None,
    ago_hours: float = 6.0,
    ws: str = WS,
) -> dict:
    """One ledger entry, described relative to "now"."""
    return dict(
        surface=surface, agent=agent, model=model, provider=provider,
        cost=cost, cost_source=cost_source, tokens_in=tokens_in,
        tokens_out=tokens_out, cached=cached, user_id=user_id, repo=repo,
        operation=operation, ago_hours=ago_hours, ws=ws,
    )


@asynccontextmanager
async def usage_api(rows: list[dict], *, ws: str = WS):
    """A spend API backed by a ledger containing exactly `rows`.

    Yields ``(client, now)`` — `now` is the instant the rows were positioned
    against, so a test can compute the window it means to ask for.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_conn, _record):  # pragma: no cover - driver callback
        dbapi_conn.create_function("date_trunc", 2, _sqlite_date_trunc)

    async with engine.begin() as conn:
        await conn.run_sync(LlmSpend.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)

    async with factory() as session:
        for entry in rows:
            session.add(LlmSpend(
                id=str(uuid.uuid4()),
                workspace_id=entry["ws"],
                surface=entry["surface"],
                agent=entry["agent"],
                model=entry["model"],
                provider=entry["provider"],
                cost_usd=float(entry["cost"]),
                cost_source=entry["cost_source"],
                tokens_in=int(entry["tokens_in"]),
                tokens_out=int(entry["tokens_out"]),
                cached_tokens_in=int(entry["cached"]),
                user_id=entry["user_id"],
                repo_slug=entry["repo"],
                operation=entry["operation"],
                created_at=now - dt.timedelta(hours=float(entry["ago_hours"])),
            ))
        await session.commit()

    app = FastAPI()
    app.include_router(spend_router.router)

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: User(
        id="u-1", email="lead@example.com",
    )
    app.dependency_overrides[current_workspace_id] = lambda: ws

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://usage") as client:
            yield client, now
    finally:
        await engine.dispose()


async def summary(client: AsyncClient, **params) -> dict:
    response = await client.get("/api/spend/summary", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def daily(client: AsyncClient, **params) -> list[dict]:
    response = await client.get("/api/spend/daily", params=params)
    assert response.status_code == 200, response.text
    return response.json()


BREAKDOWNS = (
    "by_billing", "by_surface", "by_agent", "by_model",
    "by_provider", "by_repo", "by_operation", "by_user",
)


# ══════════════════════════════════════════════════════════════════════
#  1. The agent planner is its own line on the bill
# ══════════════════════════════════════════════════════════════════════


def test_the_agent_planner_books_to_its_own_surface_not_qa(monkeypatch):
    """What breaks: Usage cannot separate the agent from chat.

    The planner — reading a sentence into a plan — booked to ``qa``, the same
    bucket as a person asking questions about the code. Those are two different
    spending decisions, and a workspace looking at one number could not tell
    which of them it was paying for. If the planner ever asks for the chat
    surface again, the two merge back into one row and the split is gone with
    no error anywhere.
    """
    import src.llm.client as llm_client
    from src.automation import chat as planner
    from src.llm.budget import SURFACE_AUTOMATION, SURFACE_QA

    assert SURFACE_AUTOMATION != SURFACE_QA, (
        "the planner's surface is an alias of chat's — they cannot be told "
        "apart in by_surface no matter what the writers do"
    )

    seen: dict = {}

    def _fake_build(*args, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(
            generate=lambda **_: types.SimpleNamespace(text='{"steps": [], "note": "ok"}'),
        )

    monkeypatch.setattr(llm_client, "build_llm_client", _fake_build)
    planner.interpret("index everything", workspace_id=WS, user_id="u-1")

    assert seen.get("spend_surface") == SURFACE_AUTOMATION, (
        f"the planner bills to {seen.get('spend_surface')!r}; it must bill to "
        f"{SURFACE_AUTOMATION!r} or it is indistinguishable from Q&A chat"
    )


def test_the_surface_a_caller_asks_for_is_the_surface_that_lands_in_the_ledger(monkeypatch):
    """What breaks: every surface silently reverts to "review".

    The planner asking for ``spend_surface="automation"`` only means something
    if the client honours it. This client was written when review was its only
    caller and the surface was a constant; documentation and Q&A later moved
    onto it and arrived labelled "review" — worse than unlabelled, because a
    budget set on review would then throttle a vault build.
    """
    import litellm

    from src.llm import budget
    from src.llm.client import LLMClient

    recorded: dict = {}
    monkeypatch.setattr(budget, "record_spend", lambda **kw: recorded.update(kw))
    monkeypatch.setattr(litellm, "completion", _fake_completion)

    LLMClient(
        resolve_key=lambda _provider: "key",
        workspace_id=WS,
        surface="automation",
    ).generate(
        prompt="index everything",
        model="gemini/gemini-flash",
        agent="automation",
        operation="automation_interpret",
    )

    assert recorded.get("surface") == "automation"
    assert recorded.get("workspace_id") == WS


# ══════════════════════════════════════════════════════════════════════
#  2. A filter reaches the totals AND every breakdown
# ══════════════════════════════════════════════════════════════════════


def _mixed_ledger() -> list[dict]:
    """Four rows this workspace owns, plus one it must never see."""
    return [
        row(surface="review", agent="architect", model="sonnet", repo="acme/api",
            operation="review_diff", user_id="u-1", cost=1.0, tokens_in=100,
            tokens_out=10, cached=20),
        row(surface="review", agent="security", model="sonnet", repo="acme/web",
            operation="review_diff", user_id="u-2", cost=2.0, tokens_in=200,
            tokens_out=20, cached=0),
        row(surface="qa", model="flash", repo="acme/api", operation="qa_answer",
            user_id="u-1", cost=4.0, tokens_in=400, tokens_out=40, cached=100),
        row(surface="vault", model="flash", repo="acme/web", operation="module_prd",
            user_id="u-2", cost=8.0, tokens_in=800, tokens_out=80, cached=0),
        # Another tenant, deliberately the most expensive row in the table.
        row(surface="review", agent="architect", model="sonnet", repo="acme/api",
            operation="review_diff", cost=100.0, ws=OTHER_WS),
    ]


async def test_the_unfiltered_summary_is_this_workspace_and_only_this_workspace():
    """What breaks: one tenant's bill appears on another tenant's page.

    This is also the baseline every filter case below is measured against — if
    the totals were wrong to begin with, "the filter agrees with the totals"
    would be agreement about the wrong number.
    """
    async with usage_api(_mixed_ledger()) as (client, _now):
        data = await summary(client, days=30)

    assert data["calls"] == 4
    assert data["cost_usd"] == pytest.approx(15.0)
    assert data["tokens_in"] == 1500
    assert data["cached_tokens_in"] == 120
    assert 100.0 not in [r["cost_usd"] for r in data["by_surface"]], (
        "another workspace's spend is on this workspace's page"
    )


@pytest.mark.parametrize(
    "field,value,breakdown,calls,cost",
    [
        ("surface", "review", "by_surface", 2, 3.0),
        ("repo", "acme/api", "by_repo", 2, 5.0),
        ("model", "flash", "by_model", 2, 12.0),
        ("operation", "module_prd", "by_operation", 1, 8.0),
        ("agent", "architect", "by_agent", 1, 1.0),
    ],
)
async def test_a_filter_narrows_the_totals_and_every_breakdown_alike(
    field, value, breakdown, calls, cost,
):
    """What breaks: a page whose columns do not add up to its header.

    A filter that reaches the totals but not a breakdown (or the reverse) does
    not throw and does not look broken — it looks like a rounding error, and it
    reads as one for about a week before somebody adds the column up by hand.
    Every breakdown is therefore checked against the header, not just the one
    the filter names.
    """
    async with usage_api(_mixed_ledger()) as (client, _now):
        data = await summary(client, days=30, **{field: value})

    assert data["calls"] == calls
    assert data["cost_usd"] == pytest.approx(cost)

    for name in BREAKDOWNS:
        rows = data[name]
        assert rows, f"{name} is empty while the header reports {calls} calls"
        assert sum(r["calls"] for r in rows) == data["calls"], (
            f"{name} sums to {sum(r['calls'] for r in rows)} calls but the "
            f"header says {data['calls']} — the filter reached one and not the other"
        )
        assert sum(r["cost_usd"] for r in rows) == pytest.approx(data["cost_usd"]), (
            f"{name} does not sum to the header's cost"
        )
        assert sum(r["tokens_in"] for r in rows) == data["tokens_in"]
        assert sum(r["tokens_out"] for r in rows) == data["tokens_out"]
        assert sum(r["cached_tokens_in"] for r in rows) == data["cached_tokens_in"]

    assert [r["key"] for r in data[breakdown]] == [value], (
        f"{breakdown} still shows rows the {field} filter excluded"
    )


@pytest.mark.parametrize(
    "field,value,calls,cost",
    [
        ("surface", "review", 2, 3.0),
        ("repo", "acme/api", 2, 5.0),
        ("model", "flash", 2, 12.0),
        ("operation", "module_prd", 1, 8.0),
        ("agent", "architect", 1, 1.0),
    ],
)
async def test_the_series_answers_the_same_filter_as_the_summary(
    field, value, calls, cost,
):
    """What breaks: the chart and the cards disagree on the same screen.

    The two endpoints take the same parameters precisely so a question asked of
    the summary can be asked of the series without rephrasing it. If a filter
    reaches only one of them, a person narrows the page and watches the chart
    stay put — and stops trusting both numbers.
    """
    async with usage_api(_mixed_ledger()) as (client, _now):
        head = await summary(client, days=30, **{field: value})
        series = await daily(client, days=30, **{field: value})

    assert sum(p["calls"] for p in series) == head["calls"] == calls
    assert sum(p["cost_usd"] for p in series) == pytest.approx(cost)
    assert sum(p["tokens_in"] for p in series) == head["tokens_in"]
    assert sum(p["tokens_out"] for p in series) == head["tokens_out"]


# ══════════════════════════════════════════════════════════════════════
#  3. The window
# ══════════════════════════════════════════════════════════════════════


def _two_eras() -> list[dict]:
    """One recent row and one far outside the default window."""
    return [
        row(surface="qa", cost=1.0, ago_hours=6),
        row(surface="vault", cost=2.0, ago_hours=24 * 40),
    ]


async def test_explicit_dates_beat_days():
    """What breaks: a shared link shows a window nobody asked for.

    ``days`` is the default the page loads with; ``since``/``until`` is what a
    person picked, or what a colleague pasted into chat. A caller that names
    both dates means them, so the pair has to win — otherwise a link to "that
    week in June" quietly renders the last thirty days and the conversation
    around it is about the wrong numbers.
    """
    async with usage_api(_two_eras()) as (client, now):
        default = await summary(client, days=1)
        explicit = await summary(
            client, days=1,
            since=(now - dt.timedelta(days=45)).isoformat(),
            until=now.isoformat(),
        )

    assert default["calls"] == 1, "days=1 should only reach the recent row"
    assert explicit["calls"] == 2, (
        "days=1 overrode an explicit 45-day window — the dates lost to the default"
    )
    assert explicit["cost_usd"] == pytest.approx(3.0)

    # And the window is echoed back, so the page's caption cannot disagree
    # with the numbers underneath it.
    assert dt.datetime.fromisoformat(explicit["since"]) == pytest.approx(
        now - dt.timedelta(days=45), abs=dt.timedelta(seconds=1),
    )
    assert dt.datetime.fromisoformat(explicit["until"]) == pytest.approx(
        now, abs=dt.timedelta(seconds=1),
    )


async def test_until_alone_moves_the_end_of_the_window():
    """What breaks: "everything up to the incident" silently means "up to now".

    ``until`` on its own is the common shape of that question, with ``days``
    still carrying the length. The end has to move even when the caller never
    named a start.
    """
    async with usage_api(_two_eras()) as (client, now):
        data = await summary(
            client, days=365, until=(now - dt.timedelta(days=10)).isoformat(),
        )

    assert data["calls"] == 1
    assert data["cost_usd"] == pytest.approx(2.0), (
        "the row from six hours ago is inside a window that ends ten days ago"
    )


async def test_a_malformed_date_falls_back_to_the_default_window():
    """What breaks: a hand-edited bookmark answers with a 422 nobody can act on.

    ``?since=last-tuesday`` is somebody editing a URL, not an attack. Refusing
    it shows a red error where a page should be; falling back to the default
    window shows them thirty days and lets them fix the field. The fallback is
    the DEFAULT window specifically — not "everything", which would quietly
    inflate every figure on the page.
    """
    async with usage_api(_two_eras()) as (client, _now):
        response = await client.get(
            "/api/spend/summary",
            params={"since": "last-tuesday", "until": "🙂"},
        )
        series = await client.get(
            "/api/spend/daily", params={"since": "not-a-date"},
        )

    assert response.status_code == 200, (
        f"a malformed date returned {response.status_code}: {response.text}"
    )
    assert series.status_code == 200
    data = response.json()
    assert data["calls"] == 1, (
        "the fallback window is not the default 30 days — it picked up the "
        "40-day-old row, so every figure on the page is inflated"
    )
    assert data["cost_usd"] == pytest.approx(1.0)


async def test_since_later_than_until_is_swapped_rather_than_empty():
    """What breaks: two dates in the wrong order look like "you spent nothing".

    A backwards range is a slip in a date picker. Answering it with an empty
    page is indistinguishable from a workspace that genuinely spent nothing,
    and the reader has no way to tell which they are looking at.
    """
    async with usage_api(_two_eras()) as (client, now):
        data = await summary(
            client,
            since=now.isoformat(),
            until=(now - dt.timedelta(days=45)).isoformat(),
        )
        series = await daily(
            client,
            since=now.isoformat(),
            until=(now - dt.timedelta(days=45)).isoformat(),
        )

    assert data["calls"] == 2, "a backwards window answered with nothing"
    assert data["cost_usd"] == pytest.approx(3.0)
    assert sum(p["calls"] for p in series) == 2
    assert dt.datetime.fromisoformat(data["since"]) < dt.datetime.fromisoformat(
        data["until"]
    ), "the echoed window is still backwards, so the caption reads inside out"


# ══════════════════════════════════════════════════════════════════════
#  4. Rows a reader has to be able to read
# ══════════════════════════════════════════════════════════════════════


async def test_a_null_group_key_renders_as_a_dash():
    """What breaks: a table row labelled "None".

    Most surfaces have no agent, no repository and no user — embeddings and
    chat span the workspace. Those rows are not missing data and must not read
    as a bug or as a repository somebody named None. ``str(None)`` is the
    natural mistake here, and it is invisible until somebody screenshots it.
    """
    ledger = [
        row(surface="embeddings", agent=None, repo=None, operation=None,
            user_id=None, cost=1.0),
        row(surface="review", agent="architect", repo="acme/api",
            operation="review_diff", user_id="u-1", cost=2.0),
    ]
    async with usage_api(ledger) as (client, _now):
        data = await summary(client, days=30)

    for name in BREAKDOWNS:
        keys = [r["key"] for r in data[name]]
        assert "None" not in keys, f"{name} has a row labelled 'None': {keys}"
        assert "" not in keys, f"{name} has a row with a blank label: {keys}"

    for name in ("by_agent", "by_repo", "by_operation", "by_user"):
        assert "—" in [r["key"] for r in data[name]], (
            f"{name} lost the row for entries that have no {name[3:]}"
        )
        placeholder = next(r for r in data[name] if r["key"] == "—")
        assert placeholder["calls"] == 1
        assert placeholder["cost_usd"] == pytest.approx(1.0)


async def test_a_surface_nobody_has_labelled_yet_still_shows_up():
    """What breaks: a new surface spends money invisibly.

    The page maps the known surface keys to readable names. A surface added
    later has no entry in that map, and the tempting implementation drops what
    it cannot label. The backend's job is to keep answering with the raw key so
    the row is there to be asked about — a number with an ugly name beats a
    number that is missing.
    """
    ledger = [
        row(surface="qa", cost=1.0),
        row(surface="fine_tuning", cost=9.0),
    ]
    async with usage_api(ledger) as (client, _now):
        data = await summary(client, days=30)

    keys = {r["key"] for r in data["by_surface"]}
    assert "fine_tuning" in keys, (
        "an unmapped surface vanished from by_surface — its spend is in the "
        "header total and in no row, so the columns stop adding up"
    )
    assert data["cost_usd"] == pytest.approx(10.0)


async def test_every_breakdown_row_carries_the_whole_shape():
    """What breaks: a column renders as `undefined` for one breakdown only.

    Each by_* row is the same six fields, so the page can render them all with
    one component. A breakdown that omits one is only visible on the tab
    nobody opened.
    """
    async with usage_api(_mixed_ledger()) as (client, _now):
        data = await summary(client, days=30)

    # `label` is on every row, not only the by-user ones: a field that appears
    # for one breakdown and vanishes for the others is exactly the shape drift
    # this test exists to catch, and the renderer would then need to know
    # which breakdown it is drawing.
    expected = {"key", "label", "calls", "tokens_in", "tokens_out",
                "cached_tokens_in", "cost_usd"}
    for name in BREAKDOWNS:
        for entry in data[name]:
            assert set(entry) == expected, f"{name} row has {set(entry) ^ expected}"


# ══════════════════════════════════════════════════════════════════════
#  5. The series grain
# ══════════════════════════════════════════════════════════════════════


async def test_the_bucket_changes_the_grain_and_never_the_total():
    """What breaks: zooming in changes how much you apparently spent.

    Hour exists for "what happened during that run this afternoon", which a
    daily bar cannot answer at all. Whichever grain is chosen, the series is
    the same money — and only the hour bucket carries a time in its label, or
    every daily row prints midnight beside its date for no reason.
    """
    ledger = [row(surface="qa", cost=1.0, ago_hours=h) for h in (1, 5, 30, 100)]

    async with usage_api(ledger) as (client, _now):
        hourly = await daily(client, days=30, bucket="hour")
        by_day = await daily(client, days=30, bucket="day")
        monthly = await daily(client, days=30, bucket="month")
        nonsense = await daily(client, days=30, bucket="fortnight")

    for series in (hourly, by_day, monthly, nonsense):
        assert sum(p["cost_usd"] for p in series) == pytest.approx(4.0)
        assert sum(p["calls"] for p in series) == 4

    assert len(hourly) >= len(by_day) >= len(monthly)
    assert len(hourly) == 4, "four calls an hour apart collapsed into one bucket"

    assert all("T" in p["date"] for p in hourly), (
        "an hourly point has no hour in its label — the whole point of the bucket"
    )
    for series, grain in ((by_day, "day"), (monthly, "month"), (nonsense, "fallback")):
        assert all("T" not in p["date"] for p in series), (
            f"the {grain} bucket prints a time beside every date"
        )
        assert all(len(p["date"]) == len("2026-08-18") for p in series)

    assert nonsense == by_day, "an unknown bucket did not fall back to day"


# ══════════════════════════════════════════════════════════════════════
#  6. The breakdown is capped
# ══════════════════════════════════════════════════════════════════════


async def test_the_breakdown_is_capped_and_keeps_the_expensive_rows():
    """What breaks: a summary answers with two thousand rows nobody scrolls.

    A workspace with thousands of repositories would otherwise turn one
    summary request into a payload proportional to its repository count — slow
    to build, slow to send, and useless to read. Capping is only safe if the
    rows that survive are the ones worth reading, so the cap keeps the most
    expensive; the header still counts everything.
    """
    total = 120
    ledger = [
        row(surface="review", repo=f"acme/repo-{i:03d}", cost=float(i) + 1.0)
        for i in range(total)
    ]
    async with usage_api(ledger) as (client, _now):
        data = await summary(client, days=30)

    by_repo = data["by_repo"]
    assert data["calls"] == total, "the header stopped counting at the cap"
    assert len(by_repo) < total, f"{total} repositories produced {len(by_repo)} rows"
    assert len(by_repo) <= 100, (
        f"the cap is {len(by_repo)} rows — still more than a person reads"
    )
    assert sum(r["calls"] for r in by_repo) < data["calls"], (
        "the breakdown is not actually dropping anything"
    )

    expensive = [f"acme/repo-{i:03d}" for i in range(total - 1, -1, -1)]
    assert [r["key"] for r in by_repo] == expensive[:len(by_repo)], (
        "the cap kept an arbitrary slice instead of the costliest repositories"
    )


# ══════════════════════════════════════════════════════════════════════
#  7. `operation` reaches the ledger
# ══════════════════════════════════════════════════════════════════════


def test_operation_reaches_the_ledger_from_the_provider_agnostic_generate(monkeypatch):
    """What breaks: "which job cost that" has no answer.

    ``operation`` was already a parameter of every call — it went into the
    audit log and stopped there. Without it in the ledger, ``surface="vault"``
    can say the vault cost $40 and nothing can say that $34 of it was
    integration guides. The failure is silent: the column exists, the endpoint
    groups by it, and every row is NULL.
    """
    import litellm

    from src.llm import budget
    from src.llm.client import LLMClient

    recorded: dict = {}
    monkeypatch.setattr(budget, "record_spend", lambda **kw: recorded.update(kw))
    monkeypatch.setattr(litellm, "completion", _fake_completion)

    LLMClient(
        resolve_key=lambda _provider: "key",
        workspace_id=WS,
        surface="vault",
    ).generate(
        prompt="write the integration guide",
        model="anthropic/claude-sonnet-5",
        agent="writer",
        operation="integration_doc",
        repo="acme/api",
    )

    assert recorded.get("operation") == "integration_doc", (
        f"the ledger row was written with operation={recorded.get('operation')!r}"
    )
    assert recorded.get("repo_slug") == "acme/api"
    assert recorded.get("tokens_in") == 1200
    assert recorded.get("cached_tokens_in") == 900


def test_operation_reaches_the_ledger_from_the_google_tool_loop(monkeypatch, tmp_path):
    """What breaks: the same gap, for every workspace on a Google key.

    The exploration agent's tool loop is the one call that still bypasses
    LiteLLM and bills itself (generate/streaming/embed moved to LiteLLM and
    are proven above and in tests/llm/), so it needs its own proof. A
    workspace whose questions run the subagent would otherwise have a
    populated Usage page with an empty `by_operation`.
    """
    from src.llm import budget
    from src.llm.gemini_client import GeminiClient
    from src.security.audit import AuditLogger

    recorded: dict = {}
    monkeypatch.setattr(budget, "record_spend", lambda **kw: recorded.update(kw))

    client = GeminiClient(audit=AuditLogger(tmp_path / "audit.jsonl"), workspace_id=WS)
    monkeypatch.setattr(client, "_call_subagent_turn", lambda *_a, **_k: types.SimpleNamespace(
        usage_metadata=types.SimpleNamespace(
            prompt_token_count=500,
            candidates_token_count=60,
            thoughts_token_count=40,
            cached_content_token_count=120,
        ),
        candidates=[],
    ))

    client.generate_with_tools_turn(
        contents=[],
        tools=[],
        system_instruction="explore",
        operation="subagent_turn_1",
        repo="acme/api",
    )

    assert recorded.get("operation") == "subagent_turn_1"
    assert recorded.get("surface") == "qa", (
        "a subagent turn billed to another surface — by_surface and "
        "by_operation would then tell two different stories about the same call"
    )
    assert recorded.get("repo_slug") == "acme/api"
    assert recorded.get("tokens_out") == 100, (
        "thinking tokens are billed as output; dropping them understates the bill"
    )


async def test_operation_is_a_question_the_endpoint_can_answer():
    """What breaks: the column is written and still unusable.

    Writing `operation` only pays off if the reads split by it — the whole
    point is turning "the vault cost $40" into "$34 of it was integration
    guides", both as a breakdown and as a filter.
    """
    ledger = [
        row(surface="vault", operation="integration_doc", cost=34.0),
        row(surface="vault", operation="module_prd", cost=6.0),
    ]
    async with usage_api(ledger) as (client, _now):
        data = await summary(client, days=30, surface="vault")
        narrowed = await summary(client, days=30, operation="integration_doc")

    assert data["cost_usd"] == pytest.approx(40.0)
    assert {r["key"]: r["cost_usd"] for r in data["by_operation"]} == {
        "integration_doc": pytest.approx(34.0),
        "module_prd": pytest.approx(6.0),
    }
    assert narrowed["cost_usd"] == pytest.approx(34.0)


# ══════════════════════════════════════════════════════════════════════
#  8. The column exists on deployed databases
# ══════════════════════════════════════════════════════════════════════


def _module_constants(tree: ast.Module) -> dict:
    """Top-level literal assignments — `revision`, `down_revision`."""
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None:
            target, value = node.target.id, node.value
        else:
            continue
        try:
            out[target] = ast.literal_eval(value)
        except (ValueError, SyntaxError, TypeError):
            out[target] = None
    return out


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _columns_created_for(table: str) -> set[str]:
    """Every column any migration adds to `table`, read from the syntax tree.

    Parsed rather than grepped on purpose: the string "operation" appears in
    this repository's migration prose, and a grep would have been satisfied by
    the docstring explaining what the column is for.
    """
    found: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in {"create_table", "add_column"}:
                continue
            if not node.args:
                continue
            head = node.args[0]
            if not (isinstance(head, ast.Constant) and head.value == table):
                continue
            for arg in node.args[1:]:
                if isinstance(arg, ast.Call) and _call_name(arg) == "Column" \
                        and arg.args and isinstance(arg.args[0], ast.Constant):
                    found.add(arg.args[0].value)
    return found


def test_every_llm_spend_column_has_a_migration():
    """What breaks: the column exists in the model and on no deployed database.

    A `mapped_column` is enough for tests, for a fresh `create_all`, and for
    nothing else. On a database that already exists, the first query naming the
    column fails — and it is the SELECT that fails, so Usage goes to 500 for
    everyone while local development is perfectly fine.
    """
    migrated = _columns_created_for("llm_spend")
    modelled = set(LlmSpend.__table__.columns.keys())
    missing = modelled - migrated
    assert not missing, (
        f"llm_spend.{sorted(missing)} exist on the model and in no migration — "
        f"every deployed database is missing them"
    )
    assert "operation" in migrated


def test_the_operation_migration_is_on_the_chain_that_actually_runs():
    """What breaks: the migration exists and never executes.

    A revision on a branch nothing points at is upgraded by no deployment, and
    a second head makes `alembic upgrade head` refuse outright. Either way the
    column is still missing in production while the file sits in the repo
    looking like proof that it is not.
    """
    graph: dict[str, str | None] = {}
    files: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("*.py")):
        info = _module_constants(ast.parse(path.read_text(encoding="utf-8")))
        revision = info.get("revision")
        assert revision, f"{path.name} declares no revision id"
        graph[revision] = info.get("down_revision")
        files[revision] = path.name

    dangling = {r: d for r, d in graph.items() if d is not None and d not in graph}
    assert not dangling, f"migrations point at revisions that do not exist: {dangling}"

    referenced = {d for d in graph.values() if d}
    heads = sorted(r for r in graph if r not in referenced)
    assert len(heads) == 1, (
        f"{len(heads)} heads ({[files[h] for h in heads]}) — `alembic upgrade "
        f"head` refuses to run with more than one"
    )

    # Walk back from the single head; the operation column must be on that path.
    adds_operation = {
        rev for rev, name in files.items()
        if _adds_llm_spend_operation(MIGRATIONS / name)
    }
    assert adds_operation, "no migration adds llm_spend.operation"

    chain, cursor = set(), heads[0]
    while cursor is not None:
        chain.add(cursor)
        cursor = graph[cursor]
    assert adds_operation & chain, (
        f"the migration adding llm_spend.operation ({[files[r] for r in adds_operation]}) "
        f"is not an ancestor of the head — no deployment will ever run it"
    )


def _adds_llm_spend_operation(path: pathlib.Path) -> bool:
    """True if this file's `upgrade()` adds the column (not just mentions it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "upgrade":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or _call_name(call) != "add_column":
                continue
            if len(call.args) < 2:
                continue
            table, column = call.args[0], call.args[1]
            if not (isinstance(table, ast.Constant) and table.value == "llm_spend"):
                continue
            if isinstance(column, ast.Call) and _call_name(column) == "Column" \
                    and column.args and isinstance(column.args[0], ast.Constant) \
                    and column.args[0].value == "operation":
                return True
    return False
