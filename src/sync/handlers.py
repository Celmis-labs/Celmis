"""Handlers for each job kind. All async — sync work runs in thread pool
so we don't block the event loop.

Payload contracts (documented per handler). Change means bump version /
add a new kind — old queued rows have no migration path other than
"drain then deploy new code".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ─── review ──────────────────────────────────────────────────────────
# payload: {provider, repo, pr_number, post_comments (bool), user_id?, workspace_id?}


async def handle_review(job: dict[str, Any]) -> None:
    p = job["payload"]
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.providers import get_provider_for
    # Resolve BOTH halves (git provider + LLM orchestrator) under the SAME
    # tenant — otherwise comments post with one workspace's PAT while another
    # workspace's LLM key is billed (the split-brain the critique flagged; note
    # get_provider_for previously received no identity at all).
    user_id = p.get("user_id", "default")
    workspace_id = p.get("workspace_id", "default")
    orch = ReviewOrchestrator()

    # A review that leaves no row is a review nobody can look at afterwards.
    # This path — webhook and poller — posted its comments to the pull request
    # and recorded nothing, so /api/reviews/history was empty on an install
    # where auto review was configured, the findings were unreachable once the
    # comments were read, and the cost appeared in no report.
    import uuid as _uuid

    from src.api.review_runs import (
        ReviewRun,
        get_review_run_store,
        record_completed_review,
    )

    run_id = str(_uuid.uuid4())
    pr_ref = f"{p['provider']}:{p['repo']}#{p['pr_number']}"
    store = get_review_run_store()
    await asyncio.to_thread(store.insert, ReviewRun(
        id=run_id, user_id=user_id, pr_ref=pr_ref, workspace_id=workspace_id,
        # So the history can tell an automatic review from one somebody asked
        # for — they answer different questions when something goes wrong.
        status="running",
    ))

    provider = await asyncio.to_thread(
        get_provider_for, p["provider"], user_id=user_id, workspace_id=workspace_id,
    )
    try:
        result = await asyncio.to_thread(
            orch.review, p["provider"], p["repo"], int(p["pr_number"]),
            dry_run=not p.get("post_comments", True),
            post_comments=p.get("post_comments", True),
            provider=provider,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        await asyncio.to_thread(record_completed_review, result, run_id=run_id,
                                store=store)
    except Exception as exc:
        # Recorded as failed rather than left "running" forever — a row stuck
        # in that state is indistinguishable from a worker that died.
        await asyncio.to_thread(
            store.update, run_id, status="error", finished=True,
            summary=str(exc)[:500])
        raise
    finally:
        await asyncio.to_thread(provider.close)


# ─── index_repo (incremental) ────────────────────────────────────────
# payload: {repo_slug, force_full?: bool, since_sha?: str}


async def handle_index_repo(job: dict[str, Any]) -> None:
    p = job["payload"]
    from src.sync.incremental import run_index
    await asyncio.to_thread(
        run_index, p["repo_slug"],
        force_full=bool(p.get("force_full", False)),
        since_sha=p.get("since_sha"),
    )


# ─── full index (clone + tree-sitter) ────────────────────────────────
# payload: {repo_slug, workspace_id, user_id}


async def handle_index_repo_full(job: dict[str, Any]) -> None:
    """What the per-repo Index button does, run from the queue.

    Shares `index_repo_sync` with the route rather than reimplementing it, so
    the credential resolution and the "indexed nothing" check cannot drift
    between the button and the bulk action.
    """
    p = job["payload"]
    from src.repos.indexing import index_repo_sync
    result = await asyncio.to_thread(
        index_repo_sync, p["repo_slug"],
        user_id=p["user_id"], workspace_id=p["workspace_id"],
    )
    logger.info("index_full_done repo=%s symbols=%d", result.repo_slug, result.symbols)


# ─── ownership rebuild ───────────────────────────────────────────────
# payload: {repo_slug, lookback_days?}


async def handle_ownership_rebuild(job: dict[str, Any]) -> None:
    p = job["payload"]
    from src.ownership.builder import compute_ownership
    await asyncio.to_thread(
        compute_ownership, p["repo_slug"],
        lookback_days=int(p.get("lookback_days", 90)),
        computed_by=job.get("enqueued_by") or "worker",
    )


# ─── cross-repo materialize ──────────────────────────────────────────
# payload: {group_name}


async def handle_cross_repo_materialize(job: dict[str, Any]) -> None:
    p = job["payload"]
    from src.groups import get_group_manager
    from src.groups.indexer import rematerialize_group
    gm = get_group_manager()
    ws = p.get("workspace_id")
    try:
        group = gm.load(p["group_name"], ws)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cross_repo_group_not_found group=%s ws=%s err=%s",
                       p["group_name"], ws or "-", exc)
        return
    n = await asyncio.to_thread(rematerialize_group, group)
    logger.info("cross_repo_materialized group=%s edges=%s", p["group_name"], n)


# ─── regenerate notes (Stage 21 — targeted vault refresh) ───────────
# payload: {repo_slug, note_paths: [..]}
#
# For each affected note we read its frontmatter to learn the type +
# module name, then re-run the matching generator with force=True so
# the LLM content is rebuilt from CURRENT code. Falls back to a plain
# re-embed when the generator can't be resolved (feature/integration
# notes whose spec is auto-detected — regenerating those correctly
# needs the full auto-detect pass, which the nightly full run covers).


async def handle_regenerate_notes(job: dict[str, Any]) -> None:
    p = job["payload"]
    repo_slug = p["repo_slug"]
    note_paths = list(p.get("note_paths") or [])
    if not note_paths:
        return
    await asyncio.to_thread(
        _regenerate_notes_sync, repo_slug, note_paths,
        p.get("engine"), p.get("language"), p.get("workspace_id", "default"),
        p.get("user_id"),
    )


def _regenerate_notes_sync(
    repo_slug: str, note_paths: list[str], engine: str | None = None,
    language: str | None = None, workspace_id: str = "default",
    user_id: str | None = None,
) -> None:
    import frontmatter

    from src.config import get_settings
    settings = get_settings()
    vault_root = settings.repo_vault_path(repo_slug)
    repo_path = settings.repo_path(repo_slug)
    if not repo_path.exists():
        logger.warning("regen_no_clone repo=%s", repo_slug)
        return

    commit_sha = _git_head_or_empty(repo_path)

    regenerated = 0
    reembedded = 0
    module_names: list[str] = []
    reembed_paths: list[str] = []

    for note_path in note_paths:
        full = vault_root / note_path
        if not full.exists():
            continue
        try:
            post = frontmatter.load(full)
        except Exception:  # noqa: BLE001
            continue
        ntype = str(post.metadata.get("type", ""))
        module = post.metadata.get("module")
        if ntype == "module" and module:
            module_names.append(str(module))
        else:
            reembed_paths.append(note_path)

    # ── Module PRDs — full LLM regeneration for current code.
    if module_names:
        try:
            from src.generation.module_prd import ModulePRDGenerator
            from src.indexing.modules import ModuleDiscovery
            discovery = ModuleDiscovery(settings)
            gen = ModulePRDGenerator(settings)
            # Redoing ONE document with a different engine is the whole
            # point of this path: improving a single PRD should not mean
            # rebuilding the entire vault.
            from src.generation.doc_language import (
                resolve_doc_engine,
                resolve_doc_language,
            )
            from src.generation.engines import build_engine

            gen.language = resolve_doc_language(language, workspace_id)
            gen.engine = build_engine(
                resolve_doc_engine(engine, workspace_id), workspace_id, user_id)
            logger.info("regen_settings repo=%s engine=%s language=%s",
                        repo_slug, gen.engine.name, gen.language)
            modules = {m.name: m for m in discovery.discover(repo_path)}
            with gen.vault_writer.batched_qdrant():
                for name in module_names:
                    mod = modules.get(name)
                    if mod is None:
                        logger.info("regen_module_gone repo=%s module=%s "
                                    "(deleted from code?)", repo_slug, name)
                        continue
                    try:
                        gen.generate(
                            repo=repo_slug, repo_path=repo_path,
                            commit_sha=commit_sha, module=mod, force=True,
                        )
                        regenerated += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("regen_module_failed module=%s err=%s",
                                       name, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("regen_modules_unavailable err=%s — falling back "
                           "to re-embed", exc)
            reembed_paths.extend(f"modules/{n}.md" for n in module_names)

    # ── Everything else — re-embed from disk (content may be slightly
    # stale but retrieval stays consistent with what's on disk).
    if reembed_paths:
        try:
            from src.api.auto_review import workspace_for_repo_slug
            from src.retrieval.tier1_vault import VaultRetriever
            from src.vault.reader import VaultReader
            from src.vault.writer import VaultWriter
            reader = VaultReader(settings)
            retriever = VaultRetriever(
                settings,
                workspace_id=workspace_for_repo_slug(repo_slug),
            )
            items: list[tuple[str, str, dict]] = []
            for note in reader.list_notes(repo_slug):
                if note.relative_path not in reembed_paths:
                    continue
                payload = {**note.metadata, "note_path": note.relative_path}
                embed_text = "\n".join([
                    f"Path: {note.relative_path}",
                    f"Type: {note.type}",
                    "",
                    note.content[:6000],
                ])
                items.append((
                    VaultWriter._note_id(repo_slug, note.relative_path),
                    embed_text, payload,
                ))
            if items:
                retriever.upsert_notes_batch(items)
                reembedded = len(items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("regen_reembed_failed err=%s", exc)

    logger.info("regen_done repo=%s modules_regenerated=%d notes_reembedded=%d",
                repo_slug, regenerated, reembedded)


def _git_head_or_empty(repo_path) -> str:  # noqa: ANN001
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


# ─── reindex qdrant ──────────────────────────────────────────────────
# payload: {repo_slug}


async def handle_reindex_qdrant(job: dict[str, Any]) -> None:
    p = job["payload"]
    from src.api.auto_review import workspace_for_repo_slug
    from src.config import get_settings
    from src.retrieval.tier1_vault import VaultRetriever
    from src.vault.reader import VaultReader
    from src.vault.writer import VaultWriter
    settings = get_settings()
    reader = VaultReader(settings)
    # Whoever enqueued this may have known the tenant; if not, the repo→
    # workspace registration does. Neither knowing means the points go in
    # unattributed rather than into somebody's workspace by accident.
    workspace_id = (
        p.get("workspace_id") or workspace_for_repo_slug(p["repo_slug"])
    )
    retriever = VaultRetriever(settings, workspace_id=workspace_id)
    notes = reader.list_notes(p["repo_slug"])
    items: list[tuple[str, str, dict]] = []
    for note in notes:
        payload = {**note.metadata, "note_path": note.relative_path}
        embed_text = "\n".join([
            f"Path: {note.relative_path}",
            f"Type: {note.type}",
            *([f"Module: {note.metadata.get('module')}"] if note.metadata.get("module") else []),
            *([f"Keywords: {', '.join(note.keywords)}"] if note.keywords else []),
            "",
            note.content[:6000],
        ])
        items.append((VaultWriter._note_id(p["repo_slug"], note.relative_path), embed_text, payload))
    batch = int(__import__("os").environ.get("CELMIS_EMBED_BATCH_SIZE", "32"))
    for i in range(0, len(items), batch):
        await asyncio.to_thread(retriever.upsert_notes_batch, items[i:i + batch])


# ─── generate_vault (full LLM vault + Qdrant embed) ──────────────────
# payload: {repo_url, repo_slug, force?: bool}


async def handle_generate_vault(job: dict[str, Any]) -> None:
    """Full vault generation: sync → discover modules → LLM notes → Qdrant.

    Long-running (one LLM call per module) and safe to re-run: the
    orchestrator's resume-mode skips modules whose vault note already matches
    the current commit, so a lease-reclaim re-run continues instead of
    duplicating spend. Requires a resolvable LLM key (BYOK or env).
    """
    p = job["payload"]
    from src.generation.orchestrator import GenerationOrchestrator
    from src.sync.queue import is_cancel_requested

    job_id = job["id"]
    orch = GenerationOrchestrator(
        workspace_id=p.get("workspace_id", "default"),
        user_id=p.get("user_id"),
    )
    result = await asyncio.to_thread(
        orch.run, p.get("repo_url") or p["repo_slug"],
        force=bool(p.get("force", False)),
        cancel_check=lambda: is_cancel_requested(job_id),
        # Resolved when the job was queued, so a run started an hour ago writes
        # the language that was chosen then rather than whatever the workspace
        # setting says by the time a worker picks it up. Absent on jobs queued
        # before this existed → orch.run falls back to the workspace setting.
        language=p.get("language"),
        # Absent on jobs queued before the engine choice existed →
        # orch.run falls back to the workspace setting.
        engine=p.get("engine"),
    )
    if getattr(result, "produced_nothing", False):
        # The queue records a job as successful unless the handler raises, so a
        # build in which every document failed was indistinguishable from one
        # that worked — the Jobs page said done, the vault was empty, and the
        # retry that would have helped never happened.
        raise RuntimeError(
            f"vault generation produced nothing for {p.get('repo_slug')}: "
            f"{len(result.failures)} document(s) failed — "
            + ", ".join(result.failures[:5]))
    if getattr(result, "embedded_nothing", False):
        # Documents exist, vectors do not. The job used to pass here, and the
        # cost of passing is a loop with no exit: the chat banner tells the
        # user to generate a vault, they do, the Jobs page says done, and the
        # banner still says generate a vault. Forever.
        #
        # Raised rather than logged, because the queue's own retry is the
        # right response to a vector store that was briefly unreachable, and
        # because a job that says "failed" is the only signal a person will
        # act on. The markdown notes stay on disk either way.
        raise RuntimeError(
            f"vault generation embedded nothing for {p.get('repo_slug')}: "
            f"{len(result.embedding_failures)} vector-store failure(s) — "
            + "; ".join(result.embedding_failures[:3])
            + " — the notes are on disk; semantic search will stay empty "
              "until they are indexed"
        )
    logger.info("generate_vault_done repo=%s language=%s failures=%d embedded=%d summary=%s",
                p.get("repo_slug"), p.get("language"),
                len(getattr(result, "failures", [])),
                getattr(result, "notes_embedded", 0),
                str(result.summary())[:300])


# ─── deps_audit (workspace dependency + vulnerability sweep) ─────────
# payload: {run_id, workspace_id}


async def handle_deps_audit(job: dict[str, Any]) -> None:
    p = job["payload"]
    from src.deps.auditor import run_audit
    from src.sync.queue import is_cancel_requested

    job_id = job["id"]
    await asyncio.to_thread(
        run_audit, p["run_id"], p.get("workspace_id", "default"),
        repo_slugs=p.get("repo_slugs"), owner=p.get("owner"),
        branch=p.get("branch"),
        cancel_check=lambda: is_cancel_requested(job_id),
    )
    # Optional auto-report with the engine chosen at launch. Best-effort:
    # a report failure must not fail the (already successful) audit job.
    engine = p.get("report_engine") or "none"
    if engine in ("api", "claude_code"):
        try:
            from src.deps.report import (
                build_prompt_sync,
                run_api_report,
                run_claude_report,
                save_report_sync,
            )
            prompt = await asyncio.to_thread(build_prompt_sync, p["run_id"])
            if prompt:
                ws = p.get("workspace_id", "default")
                if engine == "claude_code":
                    report = await run_claude_report(
                        prompt, p.get("user_id", "default"), ws)
                else:
                    report = await asyncio.to_thread(run_api_report, prompt, ws)
                await asyncio.to_thread(
                    save_report_sync, p["run_id"], report, engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning("deps_auto_report_failed run=%s err=%s", p.get("run_id"), exc)
            # Persist the failure so the UI can say WHY there is no report
            # (e.g. claude_code chosen without a connected account).
            try:
                from sqlalchemy import create_engine
                from sqlalchemy.orm import Session as _S

                from src.db.models import DepAuditRun
                from src.db.session import get_database_url
                eng = create_engine(get_database_url().replace(
                    "postgresql+asyncpg://", "postgresql+psycopg://"))
                try:
                    with _S(eng) as s:
                        run_row = s.get(DepAuditRun, p["run_id"])
                        if run_row is not None:
                            summ = dict(run_row.summary or {})
                            summ["ai_report_error"] = str(exc)[:300]
                            summ["ai_report_engine"] = engine
                            run_row.summary = summ
                            s.commit()
                finally:
                    eng.dispose()
            except Exception:  # noqa: BLE001
                pass


async def handle_automation_plan(job: dict[str, Any]) -> None:
    """Read a sentence into a plan, on the worker rather than in the request.

    The reading used to happen inside POST /plan, which meant a person who
    navigated away while it was thinking lost it: the row stayed empty, the
    answer was computed and thrown away, and pressing again paid for the same
    model call twice.

    Here it survives the browser, survives an API restart through the queue's
    retry, and — the reason a background task would not have been enough — it
    can be told to stop.

    It also reports itself while it works. The reading takes 2-6 seconds and
    used to put nothing on screen until all of it was done, which is why it
    read as "the whole answer is generated and then shown". The planner now
    streams, the human-readable sentence is written first (see
    `src.automation.chat.interpret`), and it lands on the row as it arrives.
    """
    from src.automation.chat import interpret, resolve_scope
    from src.sync.queue import is_cancel_requested

    p = job["payload"]
    run_id = p["run_id"]
    workspace_id = p.get("workspace_id", "default")

    if is_cancel_requested(job["id"]):
        return

    # Both of these are called from inside `interpret`, which runs on a worker
    # thread — so both are synchronous. Awaiting an async session from there
    # would be an await on a loop that is not running there.
    on_note = _partial_note_writer(run_id)
    should_stop = _cancel_poller(job["id"])

    try:
        plan = await asyncio.to_thread(
            interpret, p["message"], workspace_id=workspace_id,
            user_id=p.get("user_id", ""),
            on_note=on_note, should_stop=should_stop,
        )
        plan = resolve_scope(plan, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        # A model that cannot be reached is a failed reading, not a failed
        # job: retrying it would charge for the same sentence again, and the
        # person is looking at a spinner that has to end.
        logger.warning("automation_plan_failed run=%s err=%s", run_id, exc)
        await _finish_automation_plan(
            run_id, status="failed", error=f"Could not read that: {exc}"[:500])
        return

    if is_cancel_requested(job["id"]):
        # Read but discarded on purpose: the person pressed stop while it was
        # thinking, and showing them a plan they cancelled is worse than
        # showing nothing.
        await _finish_automation_plan(run_id, status="stopped")
        return

    d = plan.as_dict()

    if plan.reads_only and not plan.blocked:
        # A question. Answering it IS the reply — putting a card in front of
        # somebody so they can approve "list my repositories" would be a form
        # with one button. Reads touch nothing and cost nothing.
        try:
            answer = await _run_automation_reads(plan, p)
        except Exception as exc:  # noqa: BLE001
            await _finish_automation_plan(
                run_id, status="failed", steps=d["steps"], note=d["note"],
                error=str(exc)[:500])
            return
        await _finish_automation_plan(
            run_id, status="answered", steps=d["steps"], note=d["note"],
            language=d.get("language", ""), result=answer)
        return

    await _finish_automation_plan(
        run_id, status="planned", steps=d["steps"], note=d["note"],
        language=d.get("language", ""),
        resolved_repos=d["resolved_repos"], blocked=d["blocked"],
    )


#: How often the sentence being written is allowed to reach the database.
#: One UPDATE per token would be a database write per token for no visible
#: gain: the page polls far slower than a model emits, so all but one write
#: per poll is never seen by anybody.
_PARTIAL_WRITE_INTERVAL = 0.3
#: …unless the text jumped by this much, which is a whole clause appearing at
#: once — worth a write of its own rather than sitting invisible until the
#: next tick.
_PARTIAL_WRITE_JUMP = 120
#: The cancel flag lives in Postgres. Asked between chunks it would be a SELECT
#: per token; asked this often it still stops the model within a keystroke of
#: the person pressing the button.
_CANCEL_POLL_INTERVAL = 0.5


def _partial_note_writer(run_id: str):
    """A sink for the sentence as the model writes it, throttled, sync.

    Written to the ROW rather than pushed down a socket, and that is the whole
    point: closing the page mid-sentence and coming back shows the sentence,
    which is precisely what ordinary chat streaming cannot do.

    `status = 'reading'` in the WHERE clause is not decoration — a person who
    pressed Stop has a row that says `stopped`, and a partial arriving a
    moment later must not repaint it as if it were still thinking.
    """
    state = {"at": 0.0, "text": ""}

    def _write(note: str) -> None:
        now = time.monotonic()
        if not note or note == state["text"]:
            return
        if (now - state["at"] < _PARTIAL_WRITE_INTERVAL
                and len(note) - len(state["text"]) < _PARTIAL_WRITE_JUMP):
            return
        state["at"] = now
        state["text"] = note
        try:
            from sqlalchemy import text as _text

            # The worker's own pooled sync engine. A fresh create_engine per
            # write would be a connection handshake three times a second.
            from src.sync.queue import _engine

            with _engine().begin() as conn:
                conn.execute(_text(
                    "UPDATE automation_runs "
                    "SET partial_note = :p, updated_at = now() "
                    "WHERE id = :id AND status = 'reading'"
                ), {"p": note[:2000], "id": run_id})
        except Exception:  # noqa: BLE001
            # A partial is a courtesy. Losing one costs a moment of spinner;
            # raising here would lose the reading itself.
            logger.debug("automation_partial_not_written run=%s", run_id,
                         exc_info=True)

    return _write


def _cancel_poller(job_id: str):
    """`True` once the operator has asked for this job to stop, cached.

    Handed to the planner, which asks it between chunks — so Stop now
    interrupts the model mid-sentence instead of waiting for it to finish
    writing a plan nobody will look at.
    """
    state = {"at": 0.0, "stop": False}

    def _stop() -> bool:
        if state["stop"]:
            return True
        now = time.monotonic()
        if now - state["at"] < _CANCEL_POLL_INTERVAL:
            return False
        state["at"] = now
        try:
            from src.sync.queue import is_cancel_requested

            state["stop"] = bool(is_cancel_requested(job_id))
        except Exception:  # noqa: BLE001
            # The database being unreachable is not a stop request. The
            # handler checks again after the reading, so a cancel missed here
            # is still honoured before anything is shown.
            return False
        return state["stop"]

    return _stop


async def _run_automation_reads(plan: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a read-only plan through the same execute every write goes through.

    Not a second implementation: the point of the catalogue split is that
    reads and writes take the same path and differ only in whether a person
    has to press anything.
    """
    from src.automation.actions import Actor
    from src.automation.chat import execute
    from src.db.session import async_session

    actor = Actor(
        user_id=payload.get("user_id", ""),
        email=payload.get("user_email", ""),
        workspace_id=payload.get("workspace_id", "default"),
        label="chat",
    )
    async with async_session() as session:
        return await execute(plan, actor, session)


async def _finish_automation_plan(
    run_id: str,
    *,
    status: str,
    steps: list | None = None,
    note: str = "",
    resolved_repos: list | None = None,
    blocked: str | None = None,
    error: str | None = None,
    result: dict | None = None,
    language: str = "",
) -> None:
    """Write the reading onto its row. Never raises into the worker."""
    import json as _json

    from sqlalchemy import text as _text

    from src.db.session import async_session

    try:
        async with async_session() as session:
            await session.execute(_text(
                "UPDATE automation_runs SET status = :s, "
                "  steps = CAST(:st AS jsonb), note = :n, "
                "  resolved_repos = CAST(:r AS jsonb), blocked = :b, "
                "  error = :e, result = CAST(:res AS jsonb), "
                "  language = COALESCE(NULLIF(:lang, ''), language), "
                "  updated_at = now() "
                "WHERE id = :id"
            ), {
                "s": status, "st": _json.dumps(steps or []), "n": note or "",
                "r": _json.dumps(resolved_repos or []), "b": blocked,
                "e": error, "res": _json.dumps(result or {}),
                "lang": language or "", "id": run_id,
            })
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("automation_plan_row_not_written run=%s", run_id)
