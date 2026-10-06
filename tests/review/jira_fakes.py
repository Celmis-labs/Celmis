"""A pretend Jira site for the task-context tests. Not a test module.

`FakeJira` answers the handful of REST calls the client makes
(`/myself`, `/issue/{key}`, `/issue/{key}/comment`, `/field`,
`/project/search`) through an httpx MockTransport, counts them, and can be told
to answer a given status for a key. `install` swaps the service's connection
loader, client factory and read cache for ones that talk to it, so nothing
leaves the process and nothing needs a database server.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from src.review.task_context import service
from src.review.task_context.jira_client import JiraClient
from src.sync.jira_instance import JiraInstance

SITE = "https://acme.atlassian.net"
TOKEN = "jira-token-do-not-leak-0123456789"
EMAIL = "robot@acme.example"


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def doc(*blocks: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def para(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def heading(text: str, level: int = 2) -> dict:
    return {"type": "heading", "attrs": {"level": level},
            "content": [{"type": "text", "text": text}]}


def bullets(*items: str) -> dict:
    return {"type": "bulletList", "content": [
        {"type": "listItem", "content": [para(i)]} for i in items]}


def issue(
    key: str = "PROJ-6066", *, summary: str = "Cut profiles by length",
    description: dict | None = None, updated: str = "2026-10-01T10:00:00.000+0000",
    fields: dict[str, Any] | None = None,
) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description if description is not None else doc(
                para("Cut profiles to the ordered length."),
                heading("Acceptance criteria"),
                bullets("A profile is cut to the length on the order",
                        "Offcuts shorter than 10 mm are discarded"),
            ),
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "priority": {"name": "High"},
            "labels": ["cutting"],
            "updated": updated,
            **(fields or {}),
        },
    }


def page(page_id: str = "4242", *, title: str = "Cutting spec", adf: dict | None = None) -> dict:
    """A Confluence page as REST v2 returns it with `body-format=atlas_doc_format`."""
    import json

    body = adf if adf is not None else doc(
        para("Cut profiles to the ordered length."),
        heading("Acceptance criteria"),
        bullets("A profile is cut to the length on the order"),
    )
    return {"id": page_id, "title": title,
            "version": {"createdAt": "2026-10-02T09:00:00.000Z"},
            "_links": {"webui": f"/spaces/ENG/pages/{page_id}/Cutting+spec"},
            "body": {"atlas_doc_format": {"value": json.dumps(body)}}}


class FakeJira:
    """The site: `issues` by key, `status_for` overrides, call counters."""

    def __init__(self) -> None:
        self.issues: dict[str, dict] = {}
        self.status_for: dict[str, int] = {}
        self.comments: dict[str, list[dict]] = {}
        self.fields: list[dict] = []
        self.projects: list[dict] = [{"key": "PROJ", "name": "Acme 2D"},
                                     {"key": "AIR", "name": "AI"}]
        self.pages: dict[str, dict] = {}
        self.myself_status = 200
        self.raises: Exception | None = None
        self.calls: list[str] = []
        self.auth_headers: list[str] = []

    def count(self, fragment: str) -> int:
        return sum(1 for c in self.calls if fragment in c)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/wiki/api/v2/pages/"):
            page_id = request.url.path.rsplit("/", 1)[1]
            self.calls.append(f"/wiki/pages/{page_id}")
            self.auth_headers.append(request.headers.get("authorization", ""))
            if page_id not in self.pages:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=self.pages[page_id])
        path = request.url.path.removeprefix("/rest/api/3")
        self.calls.append(path)
        if self.raises is not None:
            raise self.raises
        self.auth_headers.append(request.headers.get("authorization", ""))
        if path == "/myself":
            if self.myself_status != 200:
                return httpx.Response(self.myself_status, json={})
            return httpx.Response(200, json={"accountId": "abc", "displayName": "Robot"})
        if path == "/field":
            return httpx.Response(200, json=self.fields)
        if path == "/project/search":
            return httpx.Response(200, json={"values": self.projects, "isLast": True})
        parts = path.strip("/").split("/")
        if parts[0] == "issue" and len(parts) >= 2:
            key = parts[1]
            if key in self.status_for:
                return httpx.Response(self.status_for[key], json={})
            if key not in self.issues:
                return httpx.Response(404, json={})
            if len(parts) == 3 and parts[2] == "comment":
                return httpx.Response(200, json={"comments": self.comments.get(key, [])})
            if request.url.params.get("fields") == "updated":
                return httpx.Response(200, json={
                    "key": key, "fields": {"updated": self.issues[key]["fields"]["updated"]}})
            return httpx.Response(200, json=self.issues[key])
        return httpx.Response(404, json={})

    def client(self, instance: JiraInstance | None = None, *, timeout: float = 8.0) -> JiraClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler),
                            auth=(EMAIL, TOKEN))
        return JiraClient(instance or JiraInstance(SITE), EMAIL, TOKEN,
                          timeout=timeout, client=http)


def install(monkeypatch, tmp_path, fake: FakeJira, *, connected: bool = True) -> None:
    """Point the service at `fake` and at a throwaway cache table."""
    from src.db.models import TaskContextCache
    from src.review.task_context import cache

    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    TaskContextCache.__table__.create(engine)
    monkeypatch.setattr(cache, "_engine", lambda: engine)
    conn = service.JiraConnection(instance=JiraInstance(SITE), email=EMAIL, token=TOKEN)
    monkeypatch.setattr(service, "load_connection",
                        lambda workspace_id, store=None: conn if connected else None)
    monkeypatch.setattr(service, "open_client", lambda c: fake.client(c.instance))
    service.forget_projects()
