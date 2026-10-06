"""The on-demand business-logic check: `@celmis -v business-logic <task>`.

A person on a pull request asks, in a comment, whether the change does what a
task says. The task is named by a Jira key (`PROJ-123`), by a link to the
issue on the connected site, or — when the repository allows it
(`task_urls_enabled`) — by a link to a Confluence page of that same site. With
nothing named, the task is the one the pull request itself names.

What happens is deliberately smaller than a review:

* ONE agent, forced on (`ReviewOrchestrator.run_single_agent`): the policy's
  switches for the business-logic agent do not apply, because somebody asked
  for it by name. The task they named is used as it is, so
  `task_project_keys` does not filter it either.
* Nothing is posted to the code, nothing is recorded on the pull request, the
  stored summary is untouched. The answer is markdown (`OnDemandResult`) that
  the command handler posts as a reply in the thread.
* Every way it can fail is a sentence in that answer, never a stack trace and
  never the provider's own error text: "the check did not run: …".

Google Docs and arbitrary links are refused: there is no OAuth for them and an
arbitrary URL would be an egress hole. Only the connected site is ever read,
with the connection's own credentials.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from src.review import messages
from src.review.task_context import checklist
from src.review.task_context.keys import parse_key
from src.review.task_context.models import TaskContext

logger = logging.getLogger(__name__)

MAX_FINDINGS_LISTED = 10
MAX_REASONING_CHARS = 400
MAX_SPEC_CHARS = 500

_PAGE_PATH = re.compile(r"/wiki/(?:spaces/[^/\s]+/)?pages/(\d{1,15})(?:/|$)")
_SEVERITY_EMOJI = {"critical": "🔴", "error": "🟠", "warning": "🟡", "info": "🔵"}


@dataclass(frozen=True)
class TaskSpec:
    """What a person named: an issue key, or a Confluence page id."""

    kind: str       # "issue" | "page"
    value: str      # PROJ-123 | 123456789


@dataclass
class OnDemandResult:
    """The answer to one on-demand check."""

    #: What the command handler posts. Always set, whatever happened.
    markdown: str
    findings: list[Any] = field(default_factory=list)
    requirements: list[checklist.Requirement] = field(default_factory=list)
    #: A short sentence when the check did not run or did not finish; "" when
    #: it ran. The same sentence is in `markdown`.
    error: str = ""
    task: TaskContext | None = None


def parse_task_spec(spec: str | None, instance: Any = None) -> TaskSpec | None:
    """The task a person's words name, or None.

    `instance` (the connection's `JiraInstance`) lets a link to the connected
    site count: an issue link (`/browse/PROJ-123`, a board link with
    `selectedIssue=`) and a Confluence page link (`/wiki/spaces/X/pages/123/…`).
    A link to any other host names nothing.
    """
    text = str(spec or "").strip()[:MAX_SPEC_CHARS]
    if not text:
        return None
    host = getattr(instance, "host", None)
    if instance is not None and text.lower().startswith("http"):
        if not instance.owns_url(text):
            return None
        try:
            path = urlsplit(text).path
        except ValueError:
            return None
        page = _PAGE_PATH.search(path)
        if page:
            return TaskSpec("page", page.group(1))
    key = parse_key(text, jira_host=host)
    return TaskSpec("issue", key) if key else None


# ─── The answer ──────────────────────────────────────────────────────


def _not_run(lang: str | None, reason: str, *, task: TaskContext | None = None) -> OnDemandResult:
    reason = checklist._one_line(reason, 300).rstrip(".")
    text = "\n\n".join([
        messages.t("business_logic.title_plain", lang),
        messages.t("business_logic.not_run", lang, reason=reason),
    ])
    return OnDemandResult(markdown=text, error=reason, task=task)


def _finding_line(f: Any) -> str:
    severity = str(getattr(getattr(f, "severity", None), "value", "") or "warning").lower()
    path, line_no = getattr(f, "file_path", ""), getattr(f, "line", "")
    where = f"`{checklist.safe_evidence(f'{path}:{line_no}')}`"
    title = checklist._one_line(getattr(f, "title", "") or "", 160)
    why = checklist._one_line(getattr(f, "reasoning", "") or "", MAX_REASONING_CHARS)
    line = f"- {_SEVERITY_EMOJI.get(severity, '🟡')} {where} **{title}**"
    return f"{line} — {why}" if why else line


def _title(task: TaskContext, lang: str | None) -> str:
    links = ", ".join(checklist._task_heading(t) for t in task.tasks)
    return messages.t("business_logic.title", lang, task=links)


def compose_answer(
    task: TaskContext, findings: list[Any], rows: list[checklist.Requirement],
    lang: str | None, *, mode: str, sha: str = "",
) -> str:
    """The markdown for a check that ran."""
    parts = [_title(task, lang)]
    if task.note:                     # some of several tasks could not be read
        parts.append(messages.t("business_logic.partial_note", lang,
                                note=checklist._one_line(task.note, 300)))
    if findings:
        lines = [messages.t("business_logic.gaps", lang)]
        lines += [_finding_line(f) for f in findings[:MAX_FINDINGS_LISTED]]
        if len(findings) > MAX_FINDINGS_LISTED:
            lines.append(messages.t("business_logic.more", lang,
                                    count=len(findings) - MAX_FINDINGS_LISTED))
        parts.append("\n".join(lines))
    else:
        parts.append(messages.t("business_logic.clean", lang))
    section = checklist.requirements_section(task, rows, lang, mode=mode)
    if section:
        parts.append(section)
    parts.append(messages.t("business_logic.footer", lang, sha=re.sub(r"[^0-9a-fA-F]", "", sha)[:12]))
    return "\n\n".join(parts)


# ─── Reading what was named ──────────────────────────────────────────


def _read_page(page_id: str, workspace_id: str, cfg: dict[str, Any]) -> TaskContext:
    """A Confluence page of the connected site as a one-task context. Never raises."""
    from src.review.task_context import service
    from src.review.task_context.jira_client import JiraError

    conn = service.load_connection(workspace_id)
    if conn is None:
        return TaskContext(
            status="no_connection", explicit=True,
            note="no Jira connection is saved for this workspace (Connections page)")
    try:
        client = service.open_client(conn)
    except JiraError as exc:
        return TaskContext(status="error", explicit=True, note=exc.sentence)
    try:
        issue = service.build_page(client.get_page(page_id), conn.instance)
    except JiraError as exc:
        status = ("not_found" if exc.kind == "not_found"
                  else "forbidden" if exc.kind in ("forbidden", "auth") else "error")
        return TaskContext(status=status, explicit=True, note=exc.sentence)
    except Exception as exc:  # noqa: BLE001
        logger.warning("task_page_failed workspace=%s err=%s", workspace_id, type(exc).__name__)
        return TaskContext(status="error", explicit=True,
                           note="the page could not be read (unexpected error; see the server log)")
    finally:
        client.close()
    return TaskContext(status="ok", tasks=[issue], explicit=True)


def _curated(result: Any) -> str:
    """A sentence about a failed agent that is safe in a public comment."""
    try:
        from src.llm.errors import curated_reason

        return curated_reason(getattr(result, "error_code", None)) or (
            "the model could not be reached or did not answer")
    except Exception:  # noqa: BLE001
        return "the model could not be reached or did not answer"


def run_business_logic_check(
    pr: Any, spec: str | None, *, workspace_id: str, user_id: str = "default",
    policy: dict | None = None, provider: Any = None, orchestrator: Any = None,
    language: str | None = None,
) -> OnDemandResult:
    """Check `pr` against the task named by `spec` and answer in markdown.

    `spec` may be empty (the pull request's own task), a key, an issue link or,
    with `task_urls_enabled`, a page link. Never raises: the answer says what
    went wrong. `orchestrator` is for tests and for the handler, which already
    holds one.
    """
    from src.review.task_context import service

    lang = messages.resolve_language(
        language or (policy or {}).get("review_language"), workspace_id)
    cfg = service.task_settings(policy)

    try:
        task = _resolve(pr, spec, workspace_id, user_id, policy, provider, cfg, lang)
        if isinstance(task, OnDemandResult):
            return task
        if not task.ok:
            return _not_run(lang, task.note or "the task could not be read", task=task)

        orch = orchestrator
        if orch is None:
            from src.review.orchestrator import ReviewOrchestrator

            orch = ReviewOrchestrator()
        run = orch.run_single_agent(
            pr, "business_logic", task_override=task, policy=policy,
            user_id=user_id, workspace_id=workspace_id, provider=provider)
        result = run.result
        if getattr(result, "skip_reason", None) and not result.error:
            return _not_run(lang, result.skip_reason, task=task)
        if result.error:
            logger.warning("on_demand_business_logic_failed pr=%s code=%s",
                           getattr(pr, "number", "?"), getattr(result, "error_code", None))
            reason = _curated(result)
            text = "\n\n".join([_title(task, lang),
                                messages.t("business_logic.failed", lang,
                                           reason=checklist._one_line(reason, 300).rstrip("."))])
            return OnDemandResult(markdown=text, error=reason, task=task)

        findings = list(result.findings)
        mode = cfg["requirements_mode"]
        rows: list[checklist.Requirement] = []
        if mode != "off" and checklist.criteria_of(task):
            if mode == "findings":
                rows = checklist.rows_from_findings(task, findings)
            else:
                agent_llm = None
                try:
                    from src.review.agents.base import agent_llm_settings

                    agent_llm = agent_llm_settings(run.context, "business_logic")
                except Exception:  # noqa: BLE001
                    agent_llm = None
                checked = checklist.check_requirements(
                    run.context.pull_request, task, findings,
                    llm_client=getattr(run.context, "llm_client", None), agent_llm=agent_llm)
                rows = checked.rows
                if checked.error:
                    mode = "findings"
        markdown = compose_answer(task, findings, rows, lang, mode=mode,
                                  sha=getattr(pr, "head_sha", "") or "")
        return OnDemandResult(markdown=markdown, findings=findings, requirements=rows, task=task)
    except Exception as exc:  # noqa: BLE001 — a command answers, it does not crash the worker
        logger.warning("on_demand_business_logic_crashed pr=%s err=%s",
                       getattr(pr, "number", "?"), type(exc).__name__, exc_info=True)
        from src.review.orchestrator import _safe_failure_reason

        return _not_run(lang, _safe_failure_reason(exc))


def _resolve(
    pr: Any, spec: str | None, workspace_id: str, user_id: str, policy: dict | None,
    provider: Any, cfg: dict[str, Any], lang: str | None,
) -> TaskContext | OnDemandResult:
    """The task context for `spec`, or an answer when `spec` cannot be used."""
    from src.review.task_context import service

    named = str(spec or "").strip()
    if not named:
        # Nothing named: the task the pull request itself names, found the way
        # a review finds it (project allowlist included).
        def commit_messages() -> list[str]:
            fetch = getattr(provider, "fetch_commit_messages", None)
            try:
                return list(fetch(pr) or []) if fetch else []
            except Exception:  # noqa: BLE001
                return []

        return service.resolve_task_context(
            pr, workspace_id=workspace_id, user_id=user_id, policy=policy,
            commit_messages=commit_messages)

    conn = service.load_connection(workspace_id)
    if conn is None:
        return _not_run(lang, messages.t("business_logic.reason.no_connection", lang))
    parsed = parse_task_spec(named, conn.instance)
    if parsed is None:
        return _not_run(lang, messages.t("business_logic.reason.no_task", lang))
    # `task_project_keys` is an admin's limit on which Jira projects this
    # repository's pull requests may pull text from. Naming the task does not
    # lift it here: any commenter may run this command, and the answer echoes
    # the task's text. (A review's own explicit task is the author's, not a
    # commenter's, and keeps bypassing it.) An empty list limits nothing.
    allowed = cfg["project_keys"]
    if parsed.kind == "page":
        if not cfg["urls_enabled"]:
            return _not_run(lang, messages.t("business_logic.reason.pages_off", lang))
        if not cfg["enabled"]:
            return _not_run(lang, messages.t("business_logic.reason.task_off", lang))
        if allowed:
            return _not_run(lang, messages.t("business_logic.reason.pages_restricted", lang))
        return _read_page(parsed.value, workspace_id, cfg)
    if allowed and parsed.value.rsplit("-", 1)[0].upper() not in allowed:
        return _not_run(lang, messages.t("business_logic.reason.project_not_allowed", lang))
    return service.resolve_task_context(
        pr, workspace_id=workspace_id, user_id=user_id, policy=policy,
        explicit=parsed.value)
