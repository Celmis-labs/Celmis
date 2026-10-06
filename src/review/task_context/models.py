"""The shapes the task context passes around: plain data, JSON both ways.

`TaskIssue` is what is kept in the cache and shown by the preview: already
curated (ADF turned into text, criteria numbered, lengths capped), never the
raw Jira answer and never anything about the token.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

#: How a task context ended. Everything but "ok" is a sentence the PR may
#: show (`TaskContext.note`), never an exception.
Status = Literal[
    "ok", "disabled", "no_connection", "no_key", "not_found", "forbidden", "error",
]

#: Where a task key was found, best source first.
Source = Literal["title", "branch", "description", "commit", "explicit"]

#: Where the acceptance criteria came from.
CriteriaSource = Literal["field", "description", "none"]


@dataclass(frozen=True)
class TaskRef:
    """A Jira key found on a pull request."""

    key: str
    source: Source = "description"


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion, numbered AC1..ACn so a finding can cite it."""

    id: str
    text: str


@dataclass(frozen=True)
class RelatedIssue:
    """A parent epic or a subtask, reduced to what orients a reader."""

    key: str
    summary: str = ""
    status: str = ""


@dataclass
class TaskIssue:
    """One Jira issue, as the review reads it."""

    key: str
    url: str = ""
    summary: str = ""
    status: str = ""
    issue_type: str = ""
    priority: str = ""
    description: str = ""
    criteria: list[Criterion] = field(default_factory=list)
    criteria_source: CriteriaSource = "none"
    parent: RelatedIssue | None = None
    subtasks: list[RelatedIssue] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    updated: str | None = None
    #: The description was cut to the character cap.
    truncated: bool = False

    @property
    def has_statement(self) -> bool:
        """Is there anything to hold a change against?"""
        return bool(self.criteria or self.description.strip())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskIssue:
        parent = raw.get("parent")
        return cls(
            key=str(raw.get("key") or ""),
            url=str(raw.get("url") or ""),
            summary=str(raw.get("summary") or ""),
            status=str(raw.get("status") or ""),
            issue_type=str(raw.get("issue_type") or ""),
            priority=str(raw.get("priority") or ""),
            description=str(raw.get("description") or ""),
            criteria=[Criterion(str(c.get("id")), str(c.get("text")))
                      for c in raw.get("criteria") or [] if isinstance(c, dict)],
            criteria_source=raw.get("criteria_source") or "none",
            parent=(RelatedIssue(**{k: str(parent.get(k) or "")
                                    for k in ("key", "summary", "status")})
                    if isinstance(parent, dict) and parent.get("key") else None),
            subtasks=[RelatedIssue(**{k: str(s.get(k) or "")
                                      for k in ("key", "summary", "status")})
                      for s in raw.get("subtasks") or [] if isinstance(s, dict)],
            labels=[str(x) for x in raw.get("labels") or []],
            comments=[str(x) for x in raw.get("comments") or []],
            updated=raw.get("updated"),
            truncated=bool(raw.get("truncated")),
        )

    def ref_dict(self) -> dict[str, str]:
        """The shape `review_pull_requests.task_refs` stores per task."""
        return {"key": self.key, "url": self.url, "summary": self.summary,
                "status": self.status, "issue_type": self.issue_type}


@dataclass
class TaskContext:
    """What a review learned about the task(s) behind a pull request."""

    status: Status = "no_key"
    tasks: list[TaskIssue] = field(default_factory=list)
    #: One sentence saying what happened when `status` is not "ok" (or when
    #: some of several tasks could not be read). Safe for a PR comment: no
    #: token, no URL with credentials, no raw exception text.
    note: str = ""
    #: The user named the task (on-demand check) instead of the PR doing it.
    explicit: bool = False
    #: Every key looked for, found or not.
    refs: list[TaskRef] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.tasks)

    @property
    def has_statement(self) -> bool:
        return any(t.has_statement for t in self.tasks)

    @property
    def criteria(self) -> list[tuple[str, Criterion]]:
        """(task key, criterion) for every criterion of every task."""
        return [(t.key, c) for t in self.tasks for c in t.criteria]

    def task_refs(self) -> list[dict[str, str]]:
        return [t.ref_dict() for t in self.tasks]
