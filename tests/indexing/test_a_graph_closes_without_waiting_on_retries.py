"""Closing a graph is one SHUTDOWN, not a retry loop against a stopped server.

Every MCP tool call opens the repository's embedded graph and closes it again.
SHUTDOWN ends with the server dropping the connection; redis-py's default retry
policy took that for a network fault and reconnected, with backoff, to a server
that was already gone — about four seconds on every close. A `list_repos` over
four repositories took sixteen, and the access matrix, which opens graphs a few
hundred times, ran CI past its time limit.
"""

from __future__ import annotations

import time

from src.indexing.graph.graph_store import make_graph_store


def test_a_close_is_quick_and_what_was_written_is_still_there(tmp_path):
    db = tmp_path / "g.db"
    store = make_graph_store(db)
    store.query("CREATE (:Symbol {id: 'a.py::f', name: 'f', file: 'a.py'})")

    started = time.monotonic()
    store.close()
    assert time.monotonic() - started < 2.0

    again = make_graph_store(db)
    try:
        assert again.query("MATCH (s:Symbol) RETURN count(s) AS c")[0]["c"] == 1
    finally:
        again.close()
