"""Three consumers asked the graph for callers of a NAME, and got none, ever.

`find_callers` matches `WHERE b.id = $id`, and a graph id carries its file:
`src/indexing/graph/extractor.py:21` documents it as `"{file}::{name}"`. A bare
`apply_refund` never equals `src/billing.py::apply_refund`, so the query
returned nothing — not an error, an empty list, which reads exactly like the
truthful answer that nothing calls the symbol.

Three call sites passed a name:

  * `src/review/breaking_change.py:150` — the breaking-change review agent.
    Its `ChangedSymbol` is parsed out of a diff by regex and carries only a
    name; there is no file to attach. So the agent's cross-repo consumer
    search found zero consumers for every symbol it ever examined, on every
    review, and reported that as its finding.
  * `src/api/routers/intel.py:347` — the deprecation consumer scan.
  * `src/mcp_server/http_app.py:962` — `_legacy_callers`, three MCP tools.

Resolved inside `find_callers` rather than at each site, because the parameter
is called `symbol_id` and three of its four callers still passed a name: an
API that invites the mistake will keep collecting it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.mcp_server import tools

CALLER = {"id": "src/api/handler.py::charge", "name": "charge",
          "kind": "function", "file": "src/api/handler.py",
          "start_line": 10, "hops": 1}


class _Store:
    """A graph where `apply_refund` lives in two files."""

    def __init__(self) -> None:
        self.queried_ids: list[str] = []
        self.by_name = {
            "apply_refund": [
                SimpleNamespace(id="src/billing.py::apply_refund"),
                SimpleNamespace(id="src/legacy/refunds.py::apply_refund"),
            ],
        }

    def find_by_name(self, name, limit=10):
        return self.by_name.get(name, [])

    def query(self, cypher, params=None):
        self.queried_ids.append(params["id"])
        if params["id"] == "src/billing.py::apply_refund":
            return [dict(CALLER)]
        if params["id"] == "src/legacy/refunds.py::apply_refund":
            return [dict(CALLER)]           # the same caller, from both
        return []

    def close(self):
        pass


@pytest.fixture
def wired(tmp_path, monkeypatch):
    graph = tmp_path / "g.fdblite"
    graph.write_bytes(b"")
    store = _Store()
    monkeypatch.setattr(tools, "get_settings",
                        lambda: SimpleNamespace(repo_graph_path=lambda s: graph))
    monkeypatch.setattr(tools, "make_graph_store", lambda p: store)
    return store


def test_a_bare_name_finds_the_callers(wired):
    out = tools.find_callers(symbol_id="apply_refund", repo_slug="acme-billing")

    assert len(out["callers"]) == 1, "a name found nothing where a caller exists"
    assert out["callers"][0]["name"] == "charge"


def test_a_name_asks_about_every_file_that_defines_it(wired):
    tools.find_callers(symbol_id="apply_refund", repo_slug="acme-billing")

    assert wired.queried_ids == [
        "src/billing.py::apply_refund",
        "src/legacy/refunds.py::apply_refund",
    ]


def test_the_same_caller_is_not_reported_twice(wired):
    out = tools.find_callers(symbol_id="apply_refund", repo_slug="acme-billing")

    ids = [c["id"] for c in out["callers"]]
    assert len(ids) == len(set(ids))


def test_an_id_is_passed_through_untouched(wired):
    """A caller that already knows the address keeps its exact behaviour."""
    tools.find_callers(symbol_id="src/billing.py::apply_refund",
                       repo_slug="acme-billing")

    assert wired.queried_ids == ["src/billing.py::apply_refund"]


def test_an_unknown_name_still_answers_empty(wired):
    out = tools.find_callers(symbol_id="no_such_symbol", repo_slug="acme-billing")

    assert out["callers"] == []
    assert out["resolved_ids"] == ["no_such_symbol"], (
        "an unresolvable name must not silently become a different question"
    )


def test_the_answer_says_which_addresses_it_used(wired):
    out = tools.find_callers(symbol_id="apply_refund", repo_slug="acme-billing")

    assert out["resolved_ids"] == [
        "src/billing.py::apply_refund",
        "src/legacy/refunds.py::apply_refund",
    ], "a caller cannot tell what was actually looked up"


def test_callees_resolve_a_name_too(wired):
    tools.find_callees(symbol_id="apply_refund", repo_slug="acme-billing")

    assert wired.queried_ids[0] == "src/billing.py::apply_refund"
