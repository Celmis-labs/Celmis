"""A vector store that writes down every filter it is asked with.

Not a test module (no ``test_`` prefix). It wraps a real Qdrant client (the in-memory
engine the isolation tests use) and delegates every query to it, so the answer is the
real engine's; what it adds is the list of filters, which is what the tests about who
may read whose vectors are really about.
"""

from __future__ import annotations

from qdrant_client.models import Filter, IsEmptyCondition


class RecordingQdrant:
    def __init__(self, client, collection: str) -> None:  # noqa: ANN001
        self._client, self.collection = client, collection
        self.filters: list[dict] = []

    def get_collection(self, name):  # noqa: ANN001, ANN201
        return self._client.get_collection(name)

    def query_points(self, **kw):  # noqa: ANN003, ANN201
        flt = kw["query_filter"]
        assert not flt.should and not flt.must_not, "an OR across tenants is no isolation"
        entry: dict = {"workspace": None, "repos": None, "conditions": len(flt.must or []),
                       "unattributed": False}
        for cond in flt.must or []:
            if isinstance(cond, Filter):
                # The one OR a query may hold: this tenant, or a point that has none. Any
                # other branch (a second tenant, a repository) is an OR across tenants.
                for branch in cond.should:
                    if isinstance(branch, IsEmptyCondition):
                        assert branch.is_empty.key == "workspace_id", branch
                        entry["unattributed"] = True
                    else:
                        assert branch.key == "workspace_id", "an OR across tenants is no isolation"
                        entry["workspace"] = branch.match.value
                assert not cond.must and not cond.must_not, cond
                continue
            match = cond.match
            if cond.key == "workspace_id":
                entry["workspace"] = match.value
            elif cond.key == "repo":
                entry["repos"] = sorted(match.any)
        self.filters.append(entry)
        return self._client.query_points(**kw)

    def asked(self) -> dict[str | None, list[str] | None]:
        """``{tenant: repos}`` of the queries made."""
        return {f["workspace"]: f["repos"] for f in self.filters}
