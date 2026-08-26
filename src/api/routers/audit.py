"""Audit log reader + retention (Stage 21).

The audit trail is append-only JSONL (`audit.jsonl` + size-rotated
`.1..N` siblings — see src/security/audit.py). This router provides:

    GET /api/audit           — filtered, paginated read
    GET /api/audit/facets    — distinct values for the filter dropdowns
    GET /api/audit/stats     — aggregates for the current filter
    GET /api/audit/export    — CSV download with the same filters

Filters: from_ts/to_ts (ISO, lexicographic compare works for UTC ISO
strings), mode, operation, repo. Records stream newest-first: current
file is read last-to-first, then rotated files in ascending .N order.

Retention: `purge_expired_audit()` deletes rotated files whose newest
record is older than CELMIS_AUDIT_RETENTION_DAYS (default 90). The
active audit.jsonl is never deleted — only rotated archives. Wired into
the ownership scheduler's nightly loop.

Who sees what
-------------
This was global-admin-only, and honestly so: `AuditRecord` had no
workspace field, so there was no way to show a workspace owner their own
calls without showing them everyone's — their models, their repository
names, which files were sent, how much they spent. The refusal was
correct; the fix was upstream, and it has now landed.

`AuditRecord.workspace_id` is a real, optional field, stamped by the
writers that know the tenant (src/llm/client.py, src/llm/gemini_client.py,
the gateway streaming path in src/llm/completion.py). Reads are scoped
rather than gated:

    global admin      every record, every tenant
    everyone else     records whose workspace_id equals THEIR workspace

Records with NO workspace_id stay global-admin-only. That is every record
written before the field existed (the key is simply absent), plus the
writers that genuinely do not know a tenant — an `Embedder` built from
settings alone, and any client built without a workspace.
`_Scope.allows` compares `ws is not None and ws == self.workspace_id`, so
"no tenant" never matches a tenant, by construction rather than by a rule
someone has to remember. `/stats` returns `hidden_unattributed` so the page
can print a line saying those records exist and are not being shown; a log
that quietly drops rows is worse than one that admits it.

The scope is fail-closed in the other direction too. A caller whose active
workspace resolves to the placeholder 'default' gets
`_Scope(global_=False, workspace_id=None)` — which matches NOTHING, not
everything. That is why the scope is a small object with an explicit
`global_` flag instead of jobs.py's `str | None` sentinel: here the tenant
side can legitimately normalize to None, and "None means no filter" would
turn that into a read of the whole installation.

Owner or admin of the workspace, not any member — and this is stricter than
the spend page next door on purpose. Usage answers "what did this workspace
cost", an aggregate. A record here answers "which files did this call carry",
per call, for everybody in the workspace. That is more than a colleague needs
in order to work here, and the page says so in as many words; an API looser
than its own copy is a promise the product does not keep.

What deliberately stays installation-level
------------------------------------------
Retention (`purge_expired_audit`), file rotation (`AuditLogger`) and the
list of log files on disk. Purge and rotation delete or rename shared
files that hold every tenant's records — one workspace owner must not be
able to destroy another's history, or to shorten the whole installation's
retention window. Neither has an HTTP route at all: purge is called by the
nightly scheduler (src/ownership/scheduler.py) and rotation happens inside
the logger on write. There is nothing here for a workspace owner to
trigger, and this file must not grow one. The `files` list in the list
response is admin-only for the same reason — it describes the
installation's storage, not the caller's activity.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.api.deps import current_workspace_id, require_workspace_admin
from src.security.audit import AuditRecord, normalize_workspace_id
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])


# ─── Tenant scope ────────────────────────────────────────────────────


@dataclass
class _Scope:
    """Which records this caller may see.

    `global_` is a separate flag on purpose. A `workspace_id` of None with
    `global_=False` means "this caller has no tenant of their own", and the
    only safe reading of that is: matches nothing. Collapsing the two into a
    single nullable field — the way the job queue can, because its column
    never holds the 'default' placeholder — would turn a tenant-less caller
    into a global read.
    """

    global_: bool
    workspace_id: str | None
    #: The caller's own user id. An action row naming them is theirs to read
    #: whatever tenant it carries — a login is recorded before any workspace
    #: is chosen, so those rows are untenanted by construction, and without
    #: this every one of them is invisible to the person who performed it.
    #: Observed on production: four real `auth.login` rows, zero readable by
    #: the account that owns the installation.
    #:
    #: Scoped to `actor_id`, so it widens the read by exactly one person's own
    #: history and by nothing else. A record about somebody ELSE in no
    #: workspace stays hidden and stays counted.
    actor_id: str | None = None
    # Records refused ONLY because they carry no tenant at all. Records that
    # belong to some OTHER workspace are refused silently and never counted:
    # a count of another tenant's calls is itself a fact about that tenant.
    hidden_unattributed: int = 0

    def allows(self, rec: dict[str, Any]) -> bool:
        if self.global_:
            return True
        if self.actor_id and rec.get("actor_id") == self.actor_id:
            return True
        ws = normalize_workspace_id(rec.get("workspace_id"))
        if ws is None:
            self.hidden_unattributed += 1
            return False
        return ws == self.workspace_id


def _scope(user: User, workspace_id: str) -> _Scope:
    """The read scope for this caller. Global admins see the installation;
    everyone else sees their own active workspace, plus their own actions
    wherever those were recorded."""
    if user.is_admin:
        return _Scope(global_=True, workspace_id=None)
    return _Scope(
        global_=False,
        workspace_id=normalize_workspace_id(workspace_id),
        actor_id=user.id,
    )


# ─── Reading ─────────────────────────────────────────────────────────


def _audit_files() -> list[Path]:
    """Newest-first list: audit.jsonl, then .1, .2, ... ascending."""
    from src.config import get_settings
    base = Path(get_settings().audit_log_path)
    files: list[Path] = []
    if base.exists():
        files.append(base)
    n = 1
    while True:
        rotated = base.with_name(base.name + f".{n}")
        if not rotated.exists():
            break
        files.append(rotated)
        n += 1
    return files


def _validate_ts(value: str | None, field: str) -> str | None:
    """An ISO timestamp, or a 422 naming the field.

    The filter below is a lexicographic string compare, which is correct for
    UTC ISO and silently wrong for anything else. `from_ts=not-a-date` made
    every record fail `ts < from_ts` — so a typo in the date box returned
    `{"records": [], "count": 0}`: HTTP 200, no error, and on a compliance
    page indistinguishable from "nothing happened".

    A filter that cannot be satisfied must say so rather than answer "none".
    """
    if value is None or value == "":
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field} must be an ISO-8601 timestamp "
                f"(e.g. 2026-08-23 or 2026-08-23T07:00:00Z), got {value!r}"
            ),
        ) from None
    return value


def _iter_records(
    *,
    scope: _Scope,
    from_ts: str | None,
    to_ts: str | None,
    mode: str | None,
    operation: str | None,
    repo: str | None,
    examine_limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield matching records newest-first across all files.

    `scope` has no default: tenant isolation lives inside the iterator so
    that no endpoint can read the log without stating whose log it is.
    `examine_limit` bounds records INSPECTED (not yielded) — a tenant with
    twenty records must not walk 50 MB of everyone else's on every request.
    """
    examined = 0
    for path in _audit_files():
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            examined += 1
            if examine_limit is not None and examined > examine_limit:
                return
            ts = str(rec.get("timestamp", ""))
            if from_ts and ts < from_ts:
                continue
            if to_ts and ts > to_ts:
                continue
            if mode and rec.get("mode") != mode:
                continue
            if operation and rec.get("operation") != operation:
                continue
            if repo and rec.get("repo") != repo:
                continue
            if not scope.allows(rec):
                continue
            yield rec


@router.get("")
def list_audit(
    from_ts: str | None = Query(default=None, description="ISO timestamp lower bound"),
    to_ts: str | None = Query(default=None, description="ISO timestamp upper bound"),
    mode: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    scope = _scope(user, workspace_id)
    records: list[dict[str, Any]] = []
    skipped = 0
    from_ts = _validate_ts(from_ts, "from_ts")
    to_ts = _validate_ts(to_ts, "to_ts")
    for rec in _iter_records(scope=scope, from_ts=from_ts, to_ts=to_ts, mode=mode,
                             operation=operation, repo=repo):
        if skipped < offset:
            skipped += 1
            continue
        records.append(rec)
        if len(records) >= limit:
            break
    out: dict[str, Any] = {
        "records": records,
        "count": len(records),
        "offset": offset,
        "limit": limit,
        "scoped": not scope.global_,
        "workspace_id": scope.workspace_id,
    }
    if scope.global_:
        # Which files exist on disk is installation storage, not the
        # caller's activity — see the module docstring.
        out["files"] = [str(p.name) for p in _audit_files()]
    return out


@router.get("/facets")
def audit_facets(
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Distinct mode/operation/repo values — feeds the UI dropdowns so nobody
    has to guess filter strings.

    Scoped exactly like the record list, and for a sharper reason: the facet
    list is a naked set of REPOSITORY NAMES. Left unscoped it would hand one
    tenant the name of every repository in the installation even while the
    records themselves were being filtered correctly — the filtered list
    would just come back empty for names its owner was never meant to know.
    """
    scope = _scope(user, workspace_id)
    modes: set[str] = set()
    operations: set[str] = set()
    repos: set[str] = set()
    matched = 0
    for rec in _iter_records(scope=scope, from_ts=None, to_ts=None, mode=None,
                             operation=None, repo=None, examine_limit=50_000):
        if rec.get("mode"):
            modes.add(str(rec["mode"]))
        if rec.get("operation"):
            operations.add(str(rec["operation"]))
        if rec.get("repo"):
            repos.add(str(rec["repo"]))
        matched += 1
    return {
        "modes": sorted(modes),
        "operations": sorted(operations),
        "repos": sorted(repos),
        "scanned": matched,
    }


@router.get("/stats")
def audit_stats(
    from_ts: str | None = Query(default=None),
    to_ts: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Aggregates for the current filter: calls, tokens, errors.

    `hidden_unattributed` is how many records matched the filter but carry no
    tenant, so the page can say "N records are not shown" instead of
    presenting a silently short total as the whole truth. It is 0 for a
    global admin, for whom nothing is hidden.
    """
    from_ts = _validate_ts(from_ts, "from_ts")
    to_ts = _validate_ts(to_ts, "to_ts")
    scope = _scope(user, workspace_id)
    total = tokens_in = tokens_out = errors = 0
    for rec in _iter_records(scope=scope, from_ts=from_ts, to_ts=to_ts, mode=mode,
                             operation=operation, repo=repo, examine_limit=200_000):
        total += 1
        # The record field is `*_estimated` (src/security/audit.py) — reading
        # the bare name meant every token tile on the page read 0 no matter
        # how much had been spent. The bare names are still accepted so an
        # older hand-written line does not become invisible.
        tokens_in += int(rec.get("input_tokens_estimated")
                         or rec.get("input_tokens") or 0)
        tokens_out += int(rec.get("output_tokens_estimated")
                          or rec.get("output_tokens") or 0)
        if rec.get("error"):
            errors += 1
        if total >= 100_000:
            break
    return {
        "total_calls": total,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "errors": errors,
        "hidden_unattributed": scope.hidden_unattributed,
        "scoped": not scope.global_,
    }


@router.get("/export")
def export_audit(
    from_ts: str | None = Query(default=None),
    to_ts: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    """CSV export — capped at 50k rows to bound memory.

    Same scope as the list: an export is the list in another file format, and
    a filter that only holds on screen is not a filter.
    """
    from_ts = _validate_ts(from_ts, "from_ts")
    to_ts = _validate_ts(to_ts, "to_ts")
    scope = _scope(user, workspace_id)
    # WHO, then what. `actor`, `actor_id`, `ip` and `target` were added to the
    # record and not to this list, and `extrasaction="ignore"` drops an
    # unlisted key without a word — so the export, which is the artefact an
    # auditor is handed, said an action happened in a workspace at a time and
    # nothing about who did it or from where. The columns the fix existed to
    # create were the exact ones missing from the file it produces.
    #
    # Derived from the dataclass rather than retyped, so the next field added
    # to `AuditRecord` cannot go missing here the same way. `files_sent`,
    # `redaction` and `extra` are excluded on purpose: they are lists and
    # dicts, and a CSV cell holding a JSON blob is worse than no cell.
    from dataclasses import fields as dataclass_fields

    _EXCLUDED = {"files_sent", "redaction", "extra", "question_hash",
                 "response_hash"}
    fields = [
        f.name for f in dataclass_fields(AuditRecord)
        if f.name not in _EXCLUDED
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for n, rec in enumerate(_iter_records(scope=scope, from_ts=from_ts, to_ts=to_ts,
                                          mode=mode, operation=operation, repo=repo),
                            start=1):
        writer.writerow(rec)
        if n >= 50_000:
            break
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_export.csv"},
    )


# ─── Retention (installation-level — no HTTP route, see docstring) ───


def purge_expired_audit() -> int:
    """Delete rotated audit files whose NEWEST record is past retention.

    Only `.N` archives are candidates — the live audit.jsonl always
    stays. Returns count of deleted files. Called by the nightly
    scheduler; safe to call ad hoc.

    Deliberately NOT exposed as an endpoint. The files hold every tenant's
    records, so deleting them is not a workspace setting: one owner must not
    be able to erase another's history. If it ever needs a route, that route
    is `require_admin`.
    """
    days = int(os.environ.get("CELMIS_AUDIT_RETENTION_DAYS", "90"))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    deleted = 0
    for path in _audit_files():
        if not path.name.split(".")[-1].isdigit():
            continue  # skip the live file
        newest = ""
        try:
            for line in path.read_text(errors="replace").splitlines():
                try:
                    ts = str(json.loads(line).get("timestamp", ""))
                    if ts > newest:
                        newest = ts
                except (json.JSONDecodeError, AttributeError):
                    continue
        except OSError:
            continue
        if newest and newest < cutoff:
            try:
                path.unlink()
                deleted += 1
                logger.info("audit_retention_deleted file=%s newest=%s", path.name, newest)
            except OSError as exc:
                logger.warning("audit_retention_delete_failed file=%s err=%s", path, exc)
    return deleted


__all__ = ["router", "purge_expired_audit"]
