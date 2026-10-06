"""Task-context routes — what the review reads from the workspace's Jira.

Admin only, and read only: they exist so a workspace admin can see, before a
review does it, what the business-logic agent would be handed for a task —
the curated summary, the numbered acceptance criteria and the exact text that
goes to the model — and can pick the acceptance-criteria field and the
project keys from lists the site itself gives.

Everything goes through the workspace's own Jira connection
(src/review/task_context/service.py): the site and the token come from the
credential row, never from the request, so these routes cannot be pointed at
another host. Messages are the curated sentences of `JiraError`; none carries
the token or the account email.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import current_workspace_id, require_workspace_admin
from src.review.review_defaults import TASK_FIELD_PATTERN
from src.review.task_context import service
from src.review.task_context.jira_client import JiraError
from src.review.task_context.models import TaskContext
from src.users import User

router = APIRouter(prefix="/api/task-context", tags=["task-context"])

_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,9}$")
_FIELD = re.compile(TASK_FIELD_PATTERN)


class TaskPreview(BaseModel):
    """One task as the review would read it."""

    key: str
    url: str
    summary: str
    status: str
    issue_type: str
    priority: str
    description: str
    criteria: list[dict[str, str]]
    criteria_source: str
    parent: dict[str, str] | None = None
    subtasks: list[dict[str, str]] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    comments: list[str] = Field(default_factory=list)
    truncated: bool = False
    #: The exact text handed to the model for this task (fenced, redacted).
    llm_text: str


class JiraField(BaseModel):
    id: str
    name: str
    schema_type: str = ""


class JiraProject(BaseModel):
    key: str
    name: str


def _raise(exc: JiraError) -> HTTPException:
    # 401 from Jira is the TOKEN being refused, not this user's session: a
    # 401 here would sign the admin out of Celmis.
    status = {"not_found": 404, "forbidden": 403}.get(exc.kind, 502)
    return HTTPException(status_code=status, detail=exc.sentence)


def _client(workspace_id: str):
    conn = service.load_connection(workspace_id)
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail="No Jira connection is saved for this workspace (Connections page)")
    try:
        return service.open_client(conn)
    except JiraError as exc:
        raise _raise(exc) from None


@router.get("/issue/{key}", response_model=TaskPreview)
def preview_issue(
    key: str,
    acceptance_field: str | None = Query(default=None, max_length=32),
    comments: int = Query(default=0, ge=0, le=10),
    _user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> TaskPreview:
    """Read one task fresh (the cache is bypassed) and show it as the review
    would: curated fields plus the text sent to the model."""
    key = key.strip().upper()
    if not _KEY.match(key):
        raise HTTPException(status_code=400, detail="That is not a Jira issue key (e.g. PROJ-6066)")
    field_id = (acceptance_field or "").strip() or None
    if field_id is not None and not _FIELD.match(field_id):
        raise HTTPException(status_code=400,
                            detail="The acceptance field must look like customfield_10042")
    client = _client(workspace_id)
    try:
        with client:
            issue = service.read_issue(
                client, workspace_id, key, acceptance_field=field_id,
                comments_n=comments, use_cache=False)
    except JiraError as exc:
        raise _raise(exc) from None
    block = service.render_task_block(TaskContext(status="ok", tasks=[issue]))
    data: dict[str, Any] = issue.to_dict()
    return TaskPreview(
        key=data["key"], url=data["url"], summary=data["summary"],
        status=data["status"], issue_type=data["issue_type"],
        priority=data["priority"], description=data["description"],
        criteria=[{"id": c["id"], "text": c["text"]} for c in data["criteria"]],
        criteria_source=data["criteria_source"], parent=data["parent"],
        subtasks=data["subtasks"], labels=data["labels"],
        comments=data["comments"], truncated=bool(data["truncated"]),
        llm_text=block,
    )


@router.get("/fields", response_model=list[JiraField])
def list_fields(
    _user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> list[JiraField]:
    """The site's custom fields, for the acceptance-criteria picker."""
    client = _client(workspace_id)
    try:
        with client:
            raw = client.list_fields()
    except JiraError as exc:
        raise _raise(exc) from None
    out: list[JiraField] = []
    for f in raw:
        fid = str(f.get("id") or "")
        if not (f.get("custom") and _FIELD.match(fid)):
            continue
        schema = f.get("schema") if isinstance(f.get("schema"), dict) else {}
        out.append(JiraField(
            id=fid, name=str(f.get("name") or fid)[:120],
            schema_type=str(schema.get("type") or "")[:40]))
    out.sort(key=lambda f: f.name.lower())
    return out


@router.get("/projects", response_model=list[JiraProject])
def list_projects(
    _user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> list[JiraProject]:
    """The projects the connection's token can browse, for the key picker."""
    client = _client(workspace_id)
    try:
        with client:
            raw = client.list_projects()
    except JiraError as exc:
        raise _raise(exc) from None
    return [JiraProject(key=p["key"], name=p["name"][:120]) for p in raw]
