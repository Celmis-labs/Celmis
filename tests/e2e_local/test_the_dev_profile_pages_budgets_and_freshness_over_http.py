"""Pagination, size and freshness of ``/mcp/dev/`` on the real stack.

Needs the tools lane (skipped by feature probe, a failure under
``CELMIS_E2E_STRICT=1``). What the plan's acceptance list asks for:

* following the cursors visits every hit once: no gap, no duplicate;
* a cursor from before a re-index is not trusted: page 1 and a note;
* a default call stays inside its tool's default token budget;
* every answer opens with the ``idx:`` line, and it says ``STALE`` after a push
  the index has not caught up with and ``fresh`` again after the re-index.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e_local.client import McpClient
from tests.e2e_local.stack import Stack
from tests.plugin import contract as C

pytestmark = pytest.mark.needs_lane("tools")

DEV = "/mcp/dev/"
SHOP = "acme/shop"
CHARS_PER_TOKEN = 3.7
#: Default token budget per tool (designs/dev-tools.md section 2.2), and the slack
#: allowed over it: the budgets are estimates of tokens, not an exact byte count.
DEFAULT_TOKENS = {"repos": 400, "find": 600, "outline": 800, "read_symbol": 1200, "grep": 800,
                  "map": 800}
SLACK = 1.5
MORE = re.compile(C.MORE_RE, re.M)
HIT = re.compile(C.HIT_RE, re.M)
IDX_ENTRY = re.compile(C.IDX_ENTRY_RE)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    with Stack(tmp_path_factory.mktemp("dev-pages")) as st:
        yield st


def _client(st: Stack) -> McpClient:
    return McpClient(st.url, DEV, st.token("su", repos=["*"]))


def _hits(text: str) -> list[tuple[str, str, str]]:
    return [(m["slug"], m["path"], m["start"]) for m in HIT.finditer(text)]


def _state(text: str, slug: str) -> str:
    entries = [IDX_ENTRY.fullmatch(e) or IDX_ENTRY.search(e)
               for e in text.splitlines()[0].removeprefix("idx: ").split(" · ")]
    return next(m["state"] for m in entries if m and m["slug"] == slug)


def _all_hits(client: McpClient, **args) -> list[tuple[str, str, str]]:
    """Every hit of one question, one page at a time, following the cursor to the end."""
    seen: list[tuple[str, str, str]] = []
    cursor, pages = "", 0
    while True:
        res = client.call("find", {**args, "limit": 3, **({"cursor": cursor} if cursor else {})})
        assert not res.is_error, res.text
        seen += _hits(res.text)
        more = MORE.search(res.text)
        pages += 1
        assert pages < 200, "the cursor never ends"
        if not more:
            return seen
        cursor = more["cursor"]


def test_following_the_cursor_visits_every_hit_once(stack) -> None:
    client = _client(stack)
    everything = _hits(client.call("find", {"query": "o", "limit": 200}).text)
    assert len(everything) > 3, "the fixture must hold more hits than one page"
    paged = _all_hits(client, query="o")
    assert len(paged) == len(set(paged)), "a hit came back twice"
    assert set(paged) == set(everything), "a hit was skipped"


def test_a_cursor_for_another_question_is_not_followed(stack) -> None:
    client = _client(stack)
    first = client.call("find", {"query": "o", "limit": 2})
    more = MORE.search(first.text)
    assert more, "the fixture must hold more hits than one page"
    other = client.call("find", {"query": "order", "limit": 2, "cursor": more["cursor"]})
    assert "cursor" in other.text.lower() and "page 1" in other.text.lower()


@pytest.mark.parametrize("tool,args", [
    ("repos", {}),
    ("find", {"query": "o"}),
    ("outline", {"repo": SHOP, "path": "."}),
    ("read_symbol", {"repo": SHOP, "name": "create_order"}),
    ("grep", {"pattern": "order"}),
    ("map", {"repo": SHOP}),
])
def test_a_default_call_stays_inside_its_default_budget(stack, tool, args) -> None:
    args = {k: (stack.slug_of(v) if v == SHOP else v) for k, v in args.items()}
    res = _client(stack).call(tool, args)
    ceiling = int(DEFAULT_TOKENS[tool] * CHARS_PER_TOKEN * SLACK)
    assert len(res.text) <= ceiling, f"{tool}: {len(res.text)} chars > {ceiling}"
    assert res.text.splitlines()[0].startswith("idx: ")


def test_the_freshness_line_goes_stale_after_a_push_and_fresh_after_the_reindex(stack) -> None:
    client = _client(stack)
    slug = stack.slug_of(SHOP)
    assert _state(client.call("find", {"query": "create_order"}).text, slug) == "fresh"

    # A cursor ranked on today's index ...
    page = client.call("find", {"query": "o", "limit": 2})
    more = MORE.search(page.text)
    assert more

    stack.push_commit(SHOP, "app/extra.py", "def extra_fn() -> int:\n    return 1\n", "add extra")
    stack.observe_remote(SHOP)
    for tool, args in (("find", {"query": "create_order"}), ("repos", {}),
                       ("grep", {"pattern": "nothing-matches-this-zzz", "repo": slug})):
        res = client.call(tool, args)
        assert _state(res.text, slug) == "STALE", (tool, res.text.splitlines()[0])

    stack.reindex(slug)
    res = client.call("find", {"query": "extra_fn"})
    assert _state(res.text, slug) == "fresh" and "extra_fn" in res.text

    # ... is not trusted once the index has moved.
    stale = client.call("find", {"query": "o", "limit": 2, "cursor": more["cursor"]})
    assert "cursor stale" in stale.text.lower() and "page 1" in stale.text.lower()
