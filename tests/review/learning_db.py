"""Fakes for the feedback-learning tests. Not a test module.

`FakeQdrant` keeps points in memory and answers the four calls the signals
index makes (`get_collection`, `create_collection`, `upsert`, `query_points`,
`delete`) with the same tenant filter a real server applies, so an isolation
test fails if the code forgets to send it. `hash_embed` turns words into a
64-wide vector: texts sharing words are close, texts sharing none are
orthogonal — enough to tell "the same finding, reworded" from "another one".
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

WIDTH = 64


def hash_embed(texts: list[str]) -> list[list[float]]:
    out = []
    for text in texts:
        v = [0.0] * WIDTH
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.sha1(word.encode()).hexdigest(), 16) % WIDTH] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass
class FakeQdrant:
    width: int | None = None
    points: dict[str, tuple[list[float], dict]] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    queries: list[Any] = field(default_factory=list)
    broken: bool = False

    def get_collection(self, name: str):
        if self.width is None:
            raise RuntimeError("not found")
        vectors = SimpleNamespace(size=self.width)
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))

    def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self.width = vectors_config.size
        self.created.append(collection_name)

    def create_payload_index(self, **_kw) -> None:
        return None

    def upsert(self, collection_name: str, points: list[Any]) -> None:
        if self.broken:
            raise RuntimeError("qdrant is down")
        for p in points:
            self.points[str(p.id)] = (list(p.vector), dict(p.payload))

    def query_points(self, collection_name, query, query_filter, limit, with_payload=False):
        if self.broken:
            raise RuntimeError("qdrant is down")
        self.queries.append(query_filter)
        hits = []
        for pid, (vec, payload) in self.points.items():
            if all(payload.get(c.key) == c.match.value for c in query_filter.must):
                hits.append(SimpleNamespace(id=pid, score=_cos(query, vec)))
        hits.sort(key=lambda h: -h.score)
        return SimpleNamespace(points=hits[:limit])

    def delete(self, collection_name: str, points_selector: Any) -> None:
        ids = getattr(points_selector, "points", None)
        if ids is not None:
            for i in ids:
                self.points.pop(str(i), None)
            return
        must = points_selector.filter.must
        for pid in [pid for pid, (_v, payload) in self.points.items()
                    if all(payload.get(c.key) == c.match.value for c in must)]:
            self.points.pop(pid)
