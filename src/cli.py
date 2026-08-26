"""CLI entrypoint — generate / ask / chat / init commands."""

from __future__ import annotations

import logging
import sys

import structlog
import typer
from rich.console import Console
from rich.markdown import Markdown

app = typer.Typer(
    name="analyzer",
    help="Code Analysis System — generates PRD/BRD and answers questions about code.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    # After the handlers exist, so there is something to attach to. Every
    # command that clones a repository builds a URL with a token in it, and
    # any subprocess or GitPython exception carries that URL in its str().
    from src.security.log_filter import install_log_redaction
    install_log_redaction()


@app.command()
def init() -> None:
    """Creates the directories and validates the configuration."""
    _setup_logging()
    from src.config import get_settings

    settings = get_settings()
    settings.ensure_directories()
    console.print("[green]✅ Directories ensured[/green]")
    console.print(f"  workspace: {settings.workspace_dir}")
    console.print(f"  vault:     {settings.vault_dir}")
    console.print(f"  audit:     {settings.audit_log_path}")
    console.print(f"  models:    {settings.gemini_generation_model} + {settings.gemini_embedding_model}")
    console.print(f"  qdrant:    {settings.qdrant_url} / {settings.qdrant_collection}")


@app.command("bootstrap-qdrant")
def bootstrap_qdrant(
    recreate: bool = typer.Option(False, "--recreate", help="Drop the collection if it exists"),
) -> None:
    """Creates the Qdrant collection for vault embeddings."""
    _setup_logging()
    from scripts.bootstrap_qdrant import bootstrap

    bootstrap(recreate=recreate)


@app.command("reindex-qdrant")
def reindex_qdrant(
    repo: str = typer.Argument(..., help="Repo slug (e.g. 'acme-frontend')"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-index the vault into Qdrant — useful when embed was failing (429)
    during generation and some notes did not make it into the collection.

    Reads all MD files from /vault/projects/{repo}/ and re-embeds them.
    """
    _setup_logging(verbose)
    import time

    from src.api.auto_review import workspace_for_repo_slug
    from src.config import get_settings
    from src.retrieval.tier1_vault import VaultRetriever
    from src.vault.reader import VaultReader

    settings = get_settings()
    reader = VaultReader(settings)
    # The CLI knows a slug, not a tenant. Look the owner up rather than
    # defaulting to one: re-embedding a repo under the wrong workspace would
    # publish its notes to that workspace's chat.
    workspace_id = workspace_for_repo_slug(repo)
    if workspace_id is None:
        console.print(
            f"[yellow]! {repo} is not registered to any workspace — notes will "
            f"be re-indexed WITHOUT a tenant and stay visible to global admins "
            f"only. Register the repo, then re-run.[/yellow]"
        )
    vault_retriever = VaultRetriever(settings, workspace_id=workspace_id)

    notes = reader.list_notes(repo)
    if not notes:
        console.print(f"[yellow]No vault notes found for repo={repo}[/yellow]")
        return

    from src.vault.writer import VaultWriter

    console.print(f"[cyan]Re-embedding {len(notes)} notes for {repo} (batched)...[/cyan]")

    items: list[tuple[str, str, dict]] = []
    for note in notes:
        payload = {**note.metadata, "note_path": note.relative_path}
        embed_text = "\n".join(
            [
                f"Path: {note.relative_path}",
                f"Type: {note.type}",
                *([f"Module: {note.metadata.get('module')}"] if note.metadata.get("module") else []),
                *([f"Keywords: {', '.join(note.keywords)}"] if note.keywords else []),
                *([f"Symbols: {', '.join(note.symbols[:20])}"] if note.symbols else []),
                "",
                note.content[:6000],
            ]
        )
        items.append((VaultWriter._note_id(repo, note.relative_path), embed_text, payload))

    # Chunk into API-friendly batches so a single 5-min HTTP call doesn't
    # timeout and rate-limit backoff stays scoped. Gemini's embed_content
    # accepts long lists, but 32 is a sane conservative default that keeps
    # each batch under Google's max token cap.
    import os as _os
    batch_size = int(_os.environ.get("CELMIS_EMBED_BATCH_SIZE", "32"))
    ok = failed = 0
    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        try:
            vault_retriever.upsert_notes_batch(chunk)
            ok += len(chunk)
        except Exception as exc:  # noqa: BLE001
            failed += len(chunk)
            console.print(f"[red]  batch fail[/red] i={i}-{i+len(chunk)}: {type(exc).__name__} — {exc}")
            if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                console.print("  [dim]sleeping 30s (rate limit)...[/dim]")
                time.sleep(30)
        done = i + len(chunk)
        console.print(f"  [{done}/{len(items)}] batched ok={ok} failed={failed}")
    console.print(f"\n[green]✅ Done[/green]: {ok} embedded, {failed} failed")


@app.command("index")
def index(
    repo: str = typer.Argument(..., help="Repo slug (e.g. 'acme-frontend') — must already be cloned into the workspace"),
    src_subdir: str = typer.Option(
        "src",
        "--src", help="Source subdirectory (for acme — src)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Index a repo into the graph (FalkorDBLite). Phase 5+8 entry point.

    The command performs: tree-sitter extraction + heuristic resolver + write to graph file.
    Vector indexing (Qdrant) is a separate command (TODO in Phase 8+).
    """
    _setup_logging(verbose)
    from src.config import get_settings
    from src.indexing.graph.pipeline import index_repo_graph

    settings = get_settings()
    settings.ensure_directories()
    repo_path = settings.repo_path(repo)
    if not repo_path.exists():
        console.print(f"[red]❌ Repo not cloned:[/red] {repo_path}")
        console.print("Run [bold]analyzer sync <repo-slug>[/bold] first.")
        raise typer.Exit(1)

    console.print(f"[cyan]Indexing graph for [bold]{repo}[/bold]...[/cyan]")
    result = index_repo_graph(repo_path, repo, settings, src_subdir=src_subdir or None)

    console.print(
        f"\n[green]✅ Done in {result.elapsed_seconds:.1f}s[/green]\n"
        f"  files processed:  {result.files_processed}\n"
        f"  parse failures:   {result.parse_failures}\n"
        f"  symbols:          {result.symbols}\n"
        f"  edges:            {result.edges}\n"
        f"  graph file:       {settings.repo_graph_path(repo)}"
    )


@app.command("graph-stats")
def graph_stats(
    repo: str = typer.Argument(..., help="Repo slug"),
) -> None:
    """Show statistics for the indexed graph."""
    _setup_logging()
    from src.config import get_settings
    from src.indexing.graph.graph_store import make_graph_store

    settings = get_settings()
    db_path = settings.repo_graph_path(repo)
    if not db_path.exists():
        console.print(f"[red]❌ No graph at:[/red] {db_path}")
        console.print(f"Run [bold]analyzer index {repo}[/bold] first.")
        raise typer.Exit(1)

    store = make_graph_store(db_path)
    try:
        symbols = store.query("MATCH (s:Symbol) RETURN s.kind AS kind, count(*) AS n ORDER BY n DESC")
        edges = store.query("MATCH ()-[r]->() RETURN type(r) AS kind, count(r) AS n ORDER BY n DESC")
        console.print(f"\n[bold]Graph stats for {repo}[/bold]")
        console.print(f"  file: {db_path}\n")
        console.print("  Symbols by kind:")
        for r in symbols:
            console.print(f"    {r['kind']:15} {r['n']}")
        console.print("\n  Edges by kind:")
        for r in edges:
            console.print(f"    {r['kind']:15} {r['n']}")
    finally:
        store.close()


@app.command("enrich-vault")
def enrich_vault(
    repo: str = typer.Argument(..., help="Repo slug (e.g. 'acme-frontend')"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Recompute computed-fields (cross_refs etc) in the frontmatter of vault notes.

    A cheap operation: graph queries + frontmatter rewrite, NO LLM calls.
    Re-embed into Qdrant only if the computed block actually changed.

    Useful after `analyzer index` or when bumping COMPUTED_SCHEMA_VERSION.
    Runs separately so as not to break the resume logic of a full `generate`.
    """
    _setup_logging(verbose)
    from src.config import get_settings
    from src.generation.cross_refs import CrossRefsComputer
    from src.indexing.graph.graph_store import make_graph_store
    from src.vault.reader import VaultReader
    from src.vault.writer import VaultWriter

    settings = get_settings()
    db_path = settings.repo_graph_path(repo)
    if not db_path.exists():
        console.print(f"[red]❌ No graph at:[/red] {db_path}")
        console.print(f"Run [bold]analyzer index {repo}[/bold] first.")
        raise typer.Exit(1)

    store = make_graph_store(db_path)
    reader = VaultReader(settings)
    # Which workspace owns this repository, from the registry — not a guess.
    # A slug registered nowhere resolves to None, and the writer says so
    # rather than writing notes no workspace can ever see.
    from src.api.auto_review import get_auto_review_store
    _ws = get_auto_review_store().workspace_for_repo_slug(repo)
    writer = VaultWriter(settings, workspace_id=_ws)

    try:
        computer = CrossRefsComputer(store, reader, repo)
        all_computed = computer.compute_for_all_modules()
    finally:
        store.close()

    if not all_computed:
        console.print(f"[yellow]No module notes found for repo={repo}[/yellow]")
        return

    console.print(f"[cyan]Updating computed-fields for {len(all_computed)} modules...[/cyan]")
    updated = 0
    skipped_noop = 0
    failed = 0
    for ref, computed in all_computed.items():
        rel = ref + ".md" if not ref.endswith(".md") else ref
        try:
            changed = writer.update_computed_fields(
                repo=repo,
                relative_path=rel,
                computed=computed.as_dict(),
                index_in_qdrant=True,
            )
            if changed:
                updated += 1
            else:
                skipped_noop += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            console.print(f"[red]  fail[/red] {rel}: {type(exc).__name__}: {exc}")
    console.print(
        f"\n[green]✅ Done[/green]: {updated} updated, "
        f"{skipped_noop} unchanged (no-op), {failed} failed"
    )


@app.command()
def generate(
    repo: str = typer.Argument(..., help="'owner/name' or a full URL, e.g. 'acme/frontend'"),
    branch: str = typer.Option("dev", "--branch", "-b", help="Git branch"),
    skip_semgrep: bool = typer.Option(False, "--skip-semgrep", help="Skip SAST scan"),
    force: bool = typer.Option(
        False,
        "--force", "-f",
        help="Regenerate ALL modules even if they are already in the vault (ignores resume)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Mode A — generates the full vault for a repo.

    Resume-mode by default: modules that already have a vault file at the same commit
    are skipped. That makes it possible to interrupt (Ctrl+C) and continue later.
    """
    _setup_logging(verbose)
    from src.generation.orchestrator import GenerationOrchestrator

    orch = GenerationOrchestrator()
    result = orch.run(repo, branch=branch, skip_semgrep=skip_semgrep, force=force)
    console.print(result.summary())


@app.command()
def ask(
    question: str = typer.Argument(..., help="Natural-language question"),
    repo: str = typer.Option(..., "--repo", "-r", help="Repo slug, e.g. 'acme-frontend'"),
    raw: bool = typer.Option(False, "--raw", help="Print without markdown rendering"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Mode B — a single question → an answer."""
    _setup_logging(verbose)
    from src.qa.orchestrator import QAOrchestrator

    qa = QAOrchestrator()
    answer = qa.ask(question=question, repo=repo)

    if raw:
        console.print(answer.as_markdown())
    else:
        console.print(Markdown(answer.text))
        console.print("\n[dim]---[/dim]")
        console.print(
            f"[dim]type={answer.question_type} route={answer.route} "
            f"tokens={answer.tokens_in}/{answer.tokens_out} "
            f"files={len(answer.files_read)}[/dim]"
        )


@app.command()
def chat(
    repo: str = typer.Option(..., "--repo", "-r", help="Repo slug"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Interactive Q&A session."""
    _setup_logging(verbose)
    from src.qa.orchestrator import QAOrchestrator

    qa = QAOrchestrator()
    console.print(f"[bold cyan]Chat mode[/bold cyan] · repo=[bold]{repo}[/bold] · Ctrl+C or empty line to exit\n")
    while True:
        try:
            q = console.input("[bold green]?[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            break
        if not q:
            break
        try:
            answer = qa.ask(question=q, repo=repo)
            console.print(Markdown(answer.text))
            console.print(
                f"\n[dim]type={answer.question_type} route={answer.route} "
                f"tokens={answer.tokens_in}/{answer.tokens_out}[/dim]\n"
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Error:[/red] {exc}")


@app.command()
def refresh(
    repo: str = typer.Argument(..., help="Repo slug (e.g. 'acme-frontend')"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Refresh the vault for a repo — do a git pull and regenerate only the changed modules.

    (TODO: for now it does a full re-generate; incremental comes in a future iteration.)
    """
    _setup_logging(verbose)
    # For now we redirect to generate
    console.print("[yellow]⚠️  incremental refresh not yet implemented — running full generate[/yellow]")
    repo_ident = repo.replace("-", "/", 1) if "/" not in repo else repo
    generate(repo=repo_ident, branch="dev", skip_semgrep=False, verbose=verbose)


# ─── Review commands (PR review microservice) ──────────────────────


@app.command("review")
def review_cmd(
    pr_ref: str = typer.Argument(
        ...,
        help="PR reference: 'github:owner/repo#NUM' / 'gitlab:group/repo#IID' / "
             "'bitbucket:workspace/repo#NUM' or a PR URL",
    ),
    post: bool = typer.Option(
        False, "--post/--no-post",
        help="Post review comments back to the PR (default: dry-run)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Compute findings but do not post comments (use with --post=False)",
    ),
    workspace: str = typer.Option(
        "default", "--workspace", "-w",
        help="Workspace/tenant whose LLM + git keys to use (default: shared 'default' tenant)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review pull/merge request multi-agent reviewer (Phase 17).

    Examples:
        analyzer review github:acme/api#42                  # dry-run, no post
        analyzer review github:acme/api#42 --post            # post comments
        analyzer review https://github.com/acme/api/pull/42 --post

    Architecture:
        - Architect (Gemini 3 Pro)   — graph-driven impact + cross-repo blast radius
        - Security  (Gemini 3 Pro)   — OWASP/CWE prompted
        - Quality   (Gemini 3 Flash) — style + complexity
        - Tests     (Gemini 3 Flash) — coverage gaps
        - Verifier  (Gemini 3 Pro)   — dedup + FP filter

    Exit codes:
        0  the review is usable — run complete, partial with at least one
           LLM agent's answer in it, or a deliberate skip (draft PR, policy)
        1  CLI error — bad PR ref, missing credentials, provider failure
        2  review FAILED — no agent produced a result; the verdict above
           the exit is not a review, do not treat it as one
        3  review PARTIAL with zero LLM answers — only deterministic checks
           (structural / CVE / breaking-change) ran; no model read the diff
    """
    _setup_logging(verbose)
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.providers.base import PullRequestProviderError

    try:
        provider_name, repo, pr_number = _parse_pr_ref(pr_ref)
    except ValueError as exc:
        console.print(f"[red]✗ Invalid PR ref:[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(
        f"[bold]Reviewing[/bold] [cyan]{provider_name}:{repo}#{pr_number}[/cyan]…"
    )
    console.print("[dim]→ Defect+Contract+Security (Pro), Verifier (Pro)[/dim]\n")

    should_post = post and not dry_run
    orchestrator = ReviewOrchestrator()

    from src.review.providers import get_provider_for
    try:
        provider = get_provider_for(provider_name, workspace_id=workspace)
    except (ValueError, PullRequestProviderError) as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from None

    try:
        result = orchestrator.review(
            provider_name, repo, pr_number,
            dry_run=not should_post,
            post_comments=True,
            provider=provider,
            workspace_id=workspace,
        )
    except PullRequestProviderError as exc:
        console.print(f"[red]✗ Provider error:[/red] {exc}")
        raise typer.Exit(1) from None
    finally:
        provider.close()

    batch = result.batch

    # ── Output ──
    verdict_color = {
        "approve": "green",
        "comment": "yellow",
        "changes": "red",
    }.get(batch.verdict.value, "white")

    # Which of the run's answers came from a model. `agents_run` mixes two
    # kinds of stage: LLM reviewers and deterministic checks (structural,
    # cve, breaking_change) that never make a provider call. The distinction
    # decides the exit code below, because of a measured afternoon: a run in
    # which every provider call died of ConnectError still had deterministic
    # answers in it, came out PARTIAL, and this command printed "Verdict:
    # COMMENT, findings: 0" and exited 0 — "could not check" dressed up as
    # "checked, clean". REVIEW_AGENTS is the roster of configurable, i.e.
    # model-calling, agents and is maintained next to them; claude_code is
    # the whole-review LLM engine, absent from that roster only because it
    # has no per-agent knobs.
    from src.review.settings import REVIEW_AGENTS
    llm_answered = [a for a in batch.agents_run if a in {*REVIEW_AGENTS, "claude_code"}]

    # The verdict carries its own health: anything short of "complete" is
    # printed beside it, in words and in color, so a COMMENT over a hole
    # cannot read like a COMMENT over the whole diff.
    run_status = batch.run_status.value
    status_note = ""
    if run_status == "partial":
        dispatched = len(batch.agents_run) + len(batch.agents_failed)
        tone = "bold yellow" if llm_answered else "bold red"
        status_note = (
            f"  [{tone}]PARTIAL — {len(batch.agents_failed)} of {dispatched} "
            f"agents failed: {', '.join(batch.agents_failed)}[/{tone}]"
        )
    elif run_status == "failed":
        status_note = "  [bold red]FAILED — no agent produced a verdict[/bold red]"
    elif run_status == "skipped":
        status_note = "  [bold yellow]SKIPPED — nothing was reviewed[/bold yellow]"

    console.print()
    console.print(
        f"[bold]Verdict:[/bold] "
        f"[{verdict_color}]{batch.verdict.value.upper()}[/{verdict_color}]"
        f"{status_note}"
    )
    if run_status == "partial" and not llm_answered:
        console.print(
            "[bold red]No LLM agent produced a result — the answers above are "
            "deterministic checks only.[/bold red]"
        )
    console.print(
        f"  findings:    [magenta]{len(batch.findings)}[/magenta] "
        f"(critical={batch.critical_count} error={batch.error_count} "
        f"warning={batch.warning_count} info={batch.info_count})"
    )
    console.print(f"  files:       [cyan]{len(batch.pull_request.changed_files)}[/cyan]")
    console.print(f"  +/- lines:   +{batch.pull_request.total_added_lines}/-{batch.pull_request.total_removed_lines}")
    console.print(f"  cross-repo:  {batch.cross_repo_callers} callers (blast radius)")
    console.print(f"  agents:      {', '.join(batch.agents_run)}")
    console.print(f"  tokens:      {batch.tokens_in:,}/{batch.tokens_out:,}")
    console.print(f"  elapsed:     {batch.elapsed_seconds:.1f}s")

    if batch.findings:
        console.print()
        console.print("[bold]Findings:[/bold]")
        for f in batch.findings[:20]:
            sev_emoji = {"critical": "🔴", "error": "🟠", "warning": "🟡", "info": "💡"}.get(
                f.severity.value, "•",
            )
            console.print(
                f"  {sev_emoji} [bold]{f.file_path}:{f.line}[/bold] "
                f"[dim]({f.agent}/{f.rule_id}, conf={f.confidence:.2f})[/dim]"
            )
            console.print(f"     {f.title or f.body[:120]}")

    if result.posted:
        console.print(f"\n[green]✓ Posted to PR[/green]: {result.provider_response}")
    elif should_post:
        console.print(f"\n[yellow]⚠ Post failed[/yellow]: {result.provider_response}")
    else:
        console.print("\n[dim](dry-run — comments not posted. Use --post to submit.)[/dim]")

    # ── Health → exit code (documented in the command help) ──
    # Non-zero ONLY when no LLM agent produced a result: a FAILED run, or a
    # PARTIAL run whose only answers are deterministic. A PARTIAL run with at
    # least one LLM answer stays exit 0 on purpose — the review is usable,
    # the banner above says what is missing — and 2/3 are distinct from 1 so
    # a caller can tell "the tool broke" from "the tool ran and could not
    # review". Deliberate skips (draft PR, policy) also stay 0: nothing died,
    # and failing a pipeline over a draft would teach people to ignore the
    # exit code — the exact reflex this code exists to end.
    if run_status == "failed":
        raise typer.Exit(2)
    if run_status == "partial" and not llm_answered:
        raise typer.Exit(3)


@app.command("review-serve")
def review_serve_cmd(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),
    port: int = typer.Option(8090, "--port", "-p"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run PR review webhook server (FastAPI/uvicorn).

    Endpoints:
        POST /webhook/github     — pull_request events
        POST /webhook/gitlab     — Merge Request Hook
        POST /webhook/bitbucket  — pullrequest:created/updated
        GET  /healthz            — health check
        GET  /webhook/stats      — diagnostics

    Setup:
        1. Generate webhook secret:
           python -c 'import secrets; print(secrets.token_urlsafe(32))'
        2. export REVIEW_WEBHOOK_SECRET=<secret>      # GitHub
        3. export REVIEW_GITLAB_TOKEN=<token>         # GitLab plaintext
        4. export REVIEW_BITBUCKET_SECRET=<secret>    # Bitbucket
        5. Configure webhook in repo settings → URL + secret matches
    """
    _setup_logging(verbose)
    import uvicorn

    from src.review.webhook import build_webhook_app

    console.print(
        f"[bold]Starting PR review webhook server[/bold] on {host}:{port}…"
    )
    console.print(f"  → http://{host}:{port}/webhook/github")
    console.print(f"  → http://{host}:{port}/webhook/gitlab")
    console.print(f"  → http://{host}:{port}/webhook/bitbucket")

    fastapi_app = build_webhook_app()
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
    no_poller: bool = typer.Option(False, "--no-poller", help="Disable auto-review poller"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the Celmis REST API + webhook server (FastAPI/uvicorn).

    Endpoints:
        /api/auth/*         login / signup / google / me
        /api/connections/*  GitHub / GitLab / Bitbucket tokens
        /api/repos/*        list / add / browse / auto-review toggle / PR list
        /api/reviews/*      manual trigger + history
        /webhook/*          provider webhooks (optional)
        /healthz, /readyz   service status

    Auto-review poller (GitHub Notifications + GitLab MR polling) starts
    automatically unless --no-poller is set or CELMIS_DISABLE_POLLER=1.
    """
    _setup_logging(verbose)
    import os as _os
    if no_poller:
        _os.environ["CELMIS_DISABLE_POLLER"] = "1"

    import uvicorn
    console.print(
        f"[bold]Starting Celmis API[/bold] on http://{host}:{port}"
    )
    console.print("  → /api/auth/login  /api/auth/signup  /api/auth/google")
    console.print("  → /api/connections  /api/repos  /api/reviews")
    console.print("  → /healthz  /readyz")
    if not no_poller:
        console.print("  → poller: ENABLED (GitHub Notifications + GitLab MR)")

    if reload:
        uvicorn.run("src.api.main:app", host=host, port=port, reload=True, log_level="info")
    else:
        from src.api.main import build_app
        uvicorn.run(build_app(), host=host, port=port, log_level="info")


def _parse_pr_ref(ref: str) -> tuple[str, str, int]:
    """Parse 'github:owner/repo#42' / 'https://github.com/o/r/pull/42' / etc.

    Returns (provider, repo, pr_number).
    """
    import re
    s = ref.strip()

    # Form 1: provider:owner/repo#NUM
    m = re.match(r"^(github|gitlab|bitbucket):([\w./-]+)#(\d+)$", s)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # Form 2: GitHub URL
    m = re.match(r"^https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", s)
    if m:
        return "github", m.group(1), int(m.group(2))

    # Form 3: GitLab URL
    m = re.match(r"^https?://gitlab\.com/([\w./-]+)/-/merge_requests/(\d+)", s)
    if m:
        return "gitlab", m.group(1), int(m.group(2))

    # Form 4: Bitbucket URL
    m = re.match(r"^https?://bitbucket\.org/([\w.-]+/[\w.-]+)/pull-requests/(\d+)", s)
    if m:
        return "bitbucket", m.group(1), int(m.group(2))

    raise ValueError(
        "Expected: 'github:owner/repo#NUM' (or gitlab/bitbucket); "
        "or full PR URL"
    )


# ─── SCIP commands (semantic enrichment) ────────────────────────────


scip_app = typer.Typer(
    name="scip",
    help="SCIP (Sourcegraph Code Intelligence) — semantic enrichment on top of the tree-sitter graph.",
    no_args_is_help=True,
)
app.add_typer(scip_app, name="scip")


@scip_app.command("check")
def scip_check_cmd(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Check that the SCIP indexer binaries are present in PATH."""
    _setup_logging(verbose)
    from src.indexing.scip import ScipExternalRunner

    runner = ScipExternalRunner()

    console.print("[bold]SCIP indexer availability:[/bold]")
    languages = sorted(ScipExternalRunner.INDEXER_COMMANDS.keys())

    for lang in languages:
        cmd = ScipExternalRunner.INDEXER_COMMANDS[lang][0]
        available = runner.is_indexer_available(lang)
        icon = "[green]✓[/green]" if available else "[red]✗[/red]"
        console.print(f"  {icon} [cyan]{lang:12s}[/cyan] (binary: [dim]{cmd}[/dim])")

    cli_ok = runner.is_scip_cli_available()
    icon = "[green]✓[/green]" if cli_ok else "[red]✗[/red]"
    console.print(f"\n  {icon} [cyan]scip CLI[/cyan] (for bin → JSON conversion)")

    if not cli_ok:
        console.print(
            "\n[dim]Install scip CLI:[/dim]\n"
            "  go install github.com/sourcegraph/scip/cmd/scip@latest"
        )


@scip_app.command("index")
def scip_index_cmd(
    repo_slug: str = typer.Argument(..., help="Repo slug (e.g. github_pallets-click)"),
    language: str = typer.Option(
        "python", "--language", "-l",
        help="Language: python|typescript|go|java",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the SCIP indexer for a cloned repo + save SCIP output into the data dir.

    Output:
        ~/code-analysis/data/{slug}/index.scip          # binary protobuf
        ~/code-analysis/data/{slug}/index_{lang}.json   # human-readable (if `scip` CLI installed)

    Requires:
        - SCIP indexer binary installed (e.g. `npm install -g @sourcegraph/scip-python`)
        - Optional: `scip` CLI for JSON conversion
        - Repo already cloned (via `analyzer generate` or `analyzer group index`)
    """
    _setup_logging(verbose)
    from src.config import get_settings
    from src.indexing.scip import ScipExternalRunner, ScipRunnerError

    settings = get_settings()
    repo_path = settings.repo_path(repo_slug)
    if not repo_path.exists():
        console.print(
            f"[red]Repo '{repo_slug}' not cloned.[/red]\n"
            f"Expected at: {repo_path}\n"
            f"Run [bold]analyzer generate[/bold] or [bold]analyzer group index[/bold] first."
        )
        raise typer.Exit(1)

    runner = ScipExternalRunner()
    if not runner.is_indexer_available(language):
        cmd = ScipExternalRunner.INDEXER_COMMANDS.get(language, ["?"])[0]
        console.print(
            f"[red]SCIP indexer '{cmd}' not in PATH.[/red]\n"
            f"Run [bold]analyzer scip check[/bold] for the list of install instructions."
        )
        raise typer.Exit(1)

    output_dir = settings.repo_data_path(repo_slug)
    console.print(f"[bold]Running scip-{language} on {repo_slug}…[/bold]")

    try:
        result = runner.run(language, repo_path, output_dir=output_dir)
    except ScipRunnerError as exc:
        console.print(f"[red]✗ SCIP failed:[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(f"[green]✓ SCIP index generated:[/green] {result.output_path}")
    if result.index is not None:
        idx = result.index
        console.print(
            f"  documents:        [cyan]{len(idx.documents)}[/cyan]\n"
            f"  external_symbols: [cyan]{len(idx.external_symbols)}[/cyan]\n"
            f"  tool:             {idx.tool_info_name}/{idx.tool_info_version}"
        )

        # Compute total occurrences/symbols
        total_occurrences = sum(len(d.occurrences) for d in idx.documents)
        total_symbols = sum(len(d.symbols) for d in idx.documents)
        console.print(
            f"  total occurrences: [cyan]{total_occurrences:,}[/cyan]\n"
            f"  total symbols:     [cyan]{total_symbols:,}[/cyan]"
        )
    else:
        console.print(
            "[yellow]  Note:[/yellow] JSON conversion skipped — install "
            "[bold]scip[/bold] CLI for parsed enrichment."
        )


@scip_app.command("enrich")
def scip_enrich_cmd(
    repo_slug: str = typer.Argument(..., help="Repo slug"),
    language: str = typer.Option("python", "--language", "-l"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-run extraction + apply SCIP enrichment + persist enriched graph.

    Pipeline:
        1. Re-walk + re-extract repo via tree-sitter (like `analyzer generate`)
        2. Run SCIP indexer
        3. Merge SCIP definitions into extractions (resolve unresolved CALLS edges)
        4. Persist enriched graph

    This fully rebuilds the repo graph with SCIP-enhanced edges.
    """
    _setup_logging(verbose)
    from src.config import get_settings
    from src.indexing.graph.configs import build_repo_context
    from src.indexing.graph.graph_store import make_graph_store
    from src.indexing.graph.languages.factory import (
        build_default_registry,
        walk_repo_files,
    )
    from src.indexing.graph.resolver import HeuristicResolver
    from src.indexing.scip import (
        ScipEnricher,
        ScipExternalRunner,
        ScipRunnerError,
    )

    settings = get_settings()
    repo_path = settings.repo_path(repo_slug)
    if not repo_path.exists():
        console.print(f"[red]Repo '{repo_slug}' not cloned.[/red]")
        raise typer.Exit(1)

    runner = ScipExternalRunner()
    if not runner.is_indexer_available(language):
        console.print(f"[red]scip-{language} not in PATH. Run analyzer scip check.[/red]")
        raise typer.Exit(1)

    # Re-extract per file
    console.print(f"[bold]Re-extracting {repo_slug}…[/bold]")
    ctx = build_repo_context(repo_path)
    registry = build_default_registry(ctx=ctx, repo_root=repo_path)
    files = walk_repo_files(repo_path)

    extractions: dict = {}
    all_symbols, all_edges = [], []
    parse_failures = 0
    for f in files:
        ext = registry.match(f)
        if ext is None:
            continue
        try:
            res = ext.extract(f)
        except Exception:  # noqa: BLE001
            parse_failures += 1
            continue
        if res.parse_errors:
            parse_failures += 1
        try:
            rel = str(f.relative_to(repo_path))
        except ValueError:
            rel = str(f)
        extractions[rel] = res
        all_symbols.extend(res.symbols)
        all_edges.extend(res.edges)

    console.print(
        f"  files: [cyan]{len(extractions)}[/cyan], "
        f"symbols: [cyan]{len(all_symbols)}[/cyan], "
        f"edges: [cyan]{len(all_edges)}[/cyan]"
    )

    # Run SCIP
    console.print(f"\n[bold]Running scip-{language}…[/bold]")
    try:
        scip_result = runner.run(
            language, repo_path, output_dir=settings.repo_data_path(repo_slug),
        )
    except ScipRunnerError as exc:
        console.print(f"[red]✗ SCIP run failed:[/red] {exc}")
        raise typer.Exit(1) from None

    if scip_result.index is None:
        console.print(
            "[red]✗ SCIP JSON conversion failed.[/red]\n"
            "Install [bold]scip[/bold] CLI: "
            "[dim]go install github.com/sourcegraph/scip/cmd/scip@latest[/dim]"
        )
        raise typer.Exit(1)

    # Enrich
    console.print("\n[bold]Enriching extractions with SCIP data…[/bold]")
    enricher = ScipEnricher()
    stats = enricher.enrich(extractions, scip_result.index)
    console.print(
        f"  documents processed:   [cyan]{stats.documents_processed}[/cyan]\n"
        f"  definitions matched:   [cyan]{stats.definitions_matched}[/cyan]\n"
        f"  references resolved:   [green]{stats.references_resolved}[/green]\n"
        f"  files with no match:   [dim]{len(stats.files_with_no_match)}[/dim]"
    )

    # Persist enriched
    console.print("\n[bold]Persisting enriched graph…[/bold]")
    resolver = HeuristicResolver(ctx, all_symbols)
    resolved = resolver.resolve_edges(all_edges)

    db_path = settings.repo_graph_path(repo_slug)
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = make_graph_store(db_path)
    try:
        store.add_symbols_batch(all_symbols)
        n_edges = store.add_edges_batch(resolved)
        store.commit()
    finally:
        store.close()

    console.print(
        f"\n[green]✓ Enriched graph saved:[/green] {db_path}\n"
        f"  symbols: {len(all_symbols)}\n"
        f"  edges:   {n_edges}"
    )


# ─── Auth commands (multi-provider credentials) ────────────────────


auth_app = typer.Typer(
    name="auth",
    help="Manage credentials for git providers (Bitbucket / GitHub / GitLab).",
    no_args_is_help=True,
)
app.add_typer(auth_app, name="auth")


@auth_app.command("github")
def auth_github_cmd(
    token: str = typer.Option(
        ..., "--token", "-t",
        prompt="GitHub Personal Access Token",
        hide_input=True,
        help="GitHub PAT (classic or fine-grained). 'repo' scope for private repos.",
    ),
    label: str = typer.Option(
        "default", "--label", "-l",
        help="Account label (for multiple GitHub accounts: work, personal etc.)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Save the GitHub PAT into the encrypted credential store + verify."""
    _setup_logging(verbose)
    from src.credentials import get_credential_store
    from src.sync.github_api import (
        GitHubAPIError,
        GitHubAuthError,
        GitHubScopeError,
        authenticate,
    )

    try:
        creds = authenticate(token)
    except GitHubAuthError as exc:
        console.print(f"[red]✗ Authentication failed:[/red] {exc}")
        raise typer.Exit(1) from None
    except (GitHubScopeError, GitHubAPIError) as exc:
        console.print(f"[red]✗ API error:[/red] {exc}")
        raise typer.Exit(1) from None

    store = get_credential_store()
    store.save(
        provider="github",
        secret=creds.token,
        metadata={"login": creds.login, "expires_at": creds.expires_at},
        account_label=label,
    )
    console.print(
        f"[green]✓[/green] GitHub credentials saved as "
        f"[cyan]{label}[/cyan] (login={creds.login})"
    )


@auth_app.command("gitlab")
def auth_gitlab_cmd(
    token: str = typer.Option(
        ..., "--token", "-t",
        prompt="GitLab Personal Access Token",
        hide_input=True,
        help="GitLab PAT with 'read_api' scope at minimum.",
    ),
    label: str = typer.Option("default", "--label", "-l"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Save the GitLab PAT into the encrypted credential store + verify."""
    _setup_logging(verbose)
    from src.credentials import get_credential_store
    from src.sync.gitlab_api import (
        GitLabAPIError,
        GitLabAuthError,
        GitLabScopeError,
        authenticate,
    )

    try:
        creds = authenticate(token)
    except GitLabAuthError as exc:
        console.print(f"[red]✗ Authentication failed:[/red] {exc}")
        raise typer.Exit(1) from None
    except (GitLabScopeError, GitLabAPIError) as exc:
        console.print(f"[red]✗ API error:[/red] {exc}")
        raise typer.Exit(1) from None

    store = get_credential_store()
    store.save(
        provider="gitlab",
        secret=creds.token,
        metadata={
            "username": creds.username,
            "user_id": creds.user_id,
            "expires_at": creds.expires_at,
        },
        account_label=label,
    )
    console.print(
        f"[green]✓[/green] GitLab credentials saved as "
        f"[cyan]{label}[/cyan] (username={creds.username})"
    )


@auth_app.command("google")
def auth_google_cmd(
    no_browser: bool = typer.Option(
        False, "--no-browser",
        help="Do not open the browser automatically (for headless / SSH scenarios)",
    ),
    timeout: int = typer.Option(
        180, "--timeout",
        help="Max wait for browser authorization (seconds)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Login via Google OAuth 2.0 + OIDC. Stores the user record in the local DB.

    Flow:
        1. App generates PKCE pair + opens browser to the Google auth URL
        2. User authorizes
        3. Code exchange → ID token
        4. Token validated → User created/updated in UserStore

    Setup (one-time):
        1. https://console.cloud.google.com/apis/credentials → Create OAuth Client ID
        2. Application type: Desktop app
        3. Copy Client ID → export GOOGLE_OAUTH_CLIENT_ID=...
        4. Run: analyzer auth google
    """
    _setup_logging(verbose)
    from src.users import (
        User,
        UserAuthMethod,
        UserExistsError,
        get_user_store,
    )
    from src.users.google_oauth import (
        GoogleOAuthError,
        run_google_oauth_flow,
    )

    console.print("[bold]Starting Google OAuth flow…[/bold]")
    if not no_browser:
        console.print("[dim]Browser will open for authorization. Waiting…[/dim]")

    try:
        info = run_google_oauth_flow(
            open_browser=not no_browser,
            timeout_seconds=timeout,
        )
    except GoogleOAuthError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(f"[green]✓ Authenticated[/green] as [cyan]{info.email}[/cyan]")
    if info.name:
        console.print(f"  name: {info.name}")
    if not info.email_verified:
        console.print("  [yellow]warning: email not verified by Google[/yellow]")

    # Create or update User in the local DB
    store = get_user_store()
    existing = store.get_by_google_sub(info.sub)
    if existing:
        # Update last_login
        store.update_last_login(existing.id)
        console.print(f"[green]✓[/green] Welcome back, [cyan]{existing.email}[/cyan]")
        return

    # Maybe link to existing email-only user?
    by_email = store.get_by_email(info.email)
    if by_email is not None:
        # Link Google to existing user
        by_email.google_sub = info.sub
        by_email.auth_method = (
            UserAuthMethod.BOTH if by_email.has_password else UserAuthMethod.GOOGLE_OAUTH
        )
        if not by_email.name:
            by_email.name = info.name
        store.update(by_email)
        store.update_last_login(by_email.id)
        console.print(
            f"[green]✓[/green] Linked Google account to existing user [cyan]{by_email.email}[/cyan]"
        )
        return

    # New user
    import uuid
    new_user = User(
        id=str(uuid.uuid4()),
        email=info.email,
        auth_method=UserAuthMethod.GOOGLE_OAUTH,
        google_sub=info.sub,
        name=info.name,
        is_active=True,
    )
    try:
        store.create(new_user)
    except UserExistsError as exc:
        console.print(f"[red]✗ Failed to create user: {exc}[/red]")
        raise typer.Exit(1) from None

    store.update_last_login(new_user.id)
    console.print(f"[green]✓[/green] Created new user [cyan]{new_user.email}[/cyan]")


@auth_app.command("list")
def auth_list_cmd(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """List the saved credentials (without secrets)."""
    _setup_logging(verbose)
    from src.credentials import get_credential_store

    store = get_credential_store()
    items = store.list()
    if not items:
        console.print(
            "[dim]No credentials saved. Add via "
            "`analyzer auth github` or `analyzer auth gitlab`.[/dim]"
        )
        return

    console.print(f"[bold]Saved credentials ({len(items)}):[/bold]")
    for item in items:
        provider = item["provider"]
        label = item["account_label"]
        meta = item["metadata"]
        identity = (
            meta.get("login")
            or meta.get("username")
            or meta.get("bitbucket_username")
            or "?"
        )
        last_used = item.get("last_used_at") or "[dim]never[/dim]"
        console.print(
            f"  • [cyan]{provider}[/cyan]/[magenta]{label}[/magenta] "
            f"(identity={identity}, last_used={last_used})"
        )


@auth_app.command("delete")
def auth_delete_cmd(
    provider: str = typer.Argument(..., help="bitbucket | github | gitlab"),
    label: str = typer.Option("default", "--label", "-l"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Delete credentials from the store."""
    _setup_logging(verbose)
    from src.credentials import get_credential_store

    if not yes:
        confirm = typer.confirm(f"Delete credentials for {provider}/{label}?")
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(0)

    store = get_credential_store()
    if store.delete(provider=provider, account_label=label):
        console.print(f"[green]✓ Deleted credentials for {provider}/{label}.[/green]")
    else:
        console.print(f"[yellow]No credentials found for {provider}/{label}.[/yellow]")
        raise typer.Exit(1)


@auth_app.command("migrate-git-tokens")
def auth_migrate_git_tokens_cmd(
    apply: bool = typer.Option(False, "--apply", help="Without this — only show the plan"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Move git tokens from personal slots into the shared workspace slot.

    Before this change the token belonged to whichever admin saved it, and
    background workers looked it up under exactly that user_id. When that
    person leaves the company, their PAT starts returning 403 — and auto-review
    on their repositories quietly stops. Here we copy the freshest available
    token into the workspace slot, where everyone can see it. The old row stays
    around as back-compat — delete it by hand once you are sure everything
    works.

    Copying credentials silently, at startup, would be wrong: it changes whose
    identity the system acts under on the git host.
    """
    _setup_logging(verbose)
    from src.credentials import GIT_PROVIDERS, WORKSPACE_GIT_USER, get_credential_store

    store = get_credential_store()
    with store._connect() as conn:  # noqa: SLF001 — admin tooling, same package
        rows = [dict(r) for r in conn.execute(
            "SELECT user_id, provider, account_label, updated_at FROM credentials_v2"
        )]

    planned = 0
    for provider in GIT_PROVIDERS:
        mine = [r for r in rows if r["provider"] == provider]
        if any(r["user_id"] == WORKSPACE_GIT_USER for r in mine):
            console.print(f"[dim]{provider}: already workspace-owned, skipping[/dim]")
            continue
        if not mine:
            continue
        newest = max(mine, key=lambda r: r["updated_at"] or "")
        console.print(
            f"[yellow]{provider}[/yellow]: {newest['user_id']}/"
            f"{newest['account_label']} → {WORKSPACE_GIT_USER}"
        )
        planned += 1
        if not apply:
            continue
        stored = store.load(
            provider=provider,
            user_id=newest["user_id"],
            account_label=newest["account_label"],
        )
        if stored is None:
            console.print(f"  [red]could not read {provider} row — skipped[/red]")
            continue
        store.save(
            provider=provider,
            secret=stored.secret,
            metadata={**stored.metadata, "migrated_from": newest["user_id"]},
            user_id=WORKSPACE_GIT_USER,
            account_label=newest["account_label"],
        )
        console.print("  [green]✓ copied[/green]")

    if not planned:
        console.print("[green]Nothing to migrate.[/green]")
    elif not apply:
        console.print("\n[cyan]Dry run — re-run with --apply to make it so.[/cyan]")


@auth_app.command("make-admin")
def auth_make_admin_cmd(
    email: str = typer.Argument(..., help="User email"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Grant a user admin rights (is_admin=True).

    Needed when the first registered user did not get admin — e.g. the
    credential store already held another record (it persists across
    redeploys). Requires access to the server/DB, so that is exactly the trust
    boundary.
    """
    _setup_logging(verbose)
    from src.users import get_user_store

    store = get_user_store()
    user = store.get_by_email(email)
    if user is None:
        console.print(f"[red]No user with email {email}.[/red]")
        raise typer.Exit(1)
    if user.is_admin:
        console.print(f"[green]{email} is already an admin.[/green]")
        return
    user.is_admin = True
    store.update(user)
    console.print(f"[green]✓ {email} is now an admin.[/green]")


@auth_app.command("reset-link")
def auth_reset_link_cmd(
    email: str = typer.Argument(..., help="User email"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Issue a password reset link (server access required).

    The last line of defence: when the only admin is locked out, the link can
    no longer be issued through the UI. Shell access to the installation is the
    trust boundary here — gitea/gitlab/metabase do exactly the same.
    """
    _setup_logging(verbose)
    from src.api.routers.auth import _reset_url, issue_reset_token
    from src.users import get_user_store

    user = get_user_store().get_by_email(email)
    if user is None or not user.is_active:
        console.print(f"[red]No active user with email {email}.[/red]")
        raise typer.Exit(1)

    raw, expires_at = issue_reset_token(user.id)
    console.print(f"[green]✓ Reset link for {user.email}[/green]")
    console.print(f"  {_reset_url(raw)}")
    console.print(
        f"[yellow]Valid until {expires_at:%Y-%m-%d %H:%M UTC}, single use. "
        "Whoever opens it can set the password — treat it as a credential.[/yellow]"
    )


# ─── MCP server commands ────────────────────────────────────────────


mcp_app = typer.Typer(
    name="mcp",
    help="MCP server — exposes code intelligence to Claude Code / Cursor / etc.",
    no_args_is_help=True,
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve_cmd(
    transport: str = typer.Option(
        "stdio", "--transport", "-t",
        help="Transport: 'stdio' (default — for Claude Code/Cursor), "
             "'streamable-http' (for remote scenarios), 'sse' (legacy)",
    ),
    port: int = typer.Option(
        8000, "--port", "-p",
        help="Port for HTTP transport (default 8000). Ignored for stdio.",
    ),
    auth: bool = typer.Option(
        False, "--auth/--no-auth",
        help="Enable JWT Bearer authentication (HTTP transport only). "
             "Requires the MCP_JWT_SECRET env var.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run MCP server. Blocks until interrupted.

    Stdio integration with Claude Code (~/.config/claude-code/mcp.json):

        {
          "mcpServers": {
            "code-analyzer": {
              "command": "analyzer",
              "args": ["mcp", "serve"]
            }
          }
        }

    HTTP scenario (remote/multi-tenant):

        analyzer mcp serve --transport streamable-http --port 8080
    """
    # CRITICAL: stdio transport uses stdout for JSON-RPC.
    # Configure logging to go ONLY to stderr if stdio. Otherwise JSON-RPC breaks.
    import sys
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,  # always stderr — safe for stdio
    )

    from src.mcp_server import run_server

    valid_transports = {"stdio", "streamable-http", "sse"}
    if transport not in valid_transports:
        # Logging to stderr (console.print goes to stdout — NOT OK for stdio mode)
        sys.stderr.write(
            f"Invalid transport '{transport}'. Allowed: {sorted(valid_transports)}\n"
        )
        raise typer.Exit(1)

    if transport != "stdio":
        # Only if NOT stdio — we can write to stdout
        console.print(
            f"[bold]MCP server starting:[/bold] transport={transport} port={port}"
        )

    run_server(
        transport=transport,
        port=port if transport != "stdio" else None,
        enable_auth=auth,
    )


@mcp_app.command("issue-token")
def mcp_issue_token_cmd(
    subject: str = typer.Option(
        "default", "--subject", "-s",
        help="JWT 'sub' claim — user id ('default' for single-user)",
    ),
    scopes: str = typer.Option(
        "read:graph read:groups",
        "--scopes",
        help="Space-separated scopes for the 'scope' claim",
    ),
    duration: int = typer.Option(
        3600, "--duration", "-d",
        help="Token lifetime in seconds (default 1 hour)",
    ),
    client_id: str = typer.Option(
        "code-analyzer-cli", "--client-id",
        help="Client identifier for traceability",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Issue a JWT Bearer token for MCP HTTP transport (local dev / testing).

    Token printed to stdout. Use as:

        export TOKEN=$(analyzer mcp issue-token)
        curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/mcp/...

    Requires the MCP_JWT_SECRET env var.
    """
    _setup_logging(verbose)
    from src.mcp_server.auth import JwtConfig, JwtConfigError, issue_token

    try:
        config = JwtConfig.from_env()
    except JwtConfigError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from None

    token = issue_token(
        config,
        subject=subject,
        scopes=scopes.split(),
        client_id=client_id,
        expires_in=duration,
    )

    # Print to stdout (parseable for $(...) shell interpolation)
    print(token)
    # Metadata to stderr (via console.print → stderr if verbose)
    if verbose:
        import sys
        sys.stderr.write(
            f"# subject={subject} client_id={client_id} "
            f"scopes={scopes} expires_in={duration}s\n"
        )


# ─── Group commands ─────────────────────────────────────────────────


group_app = typer.Typer(
    name="group",
    help="Operations on cross-repo groups (Stage 5+6: cluster repos for cross-team analysis).",
    no_args_is_help=True,
)
app.add_typer(group_app, name="group")


# ════════════════════════════════════════════════════════════════════
# `repo` subcommand — per-repo operations (purge, info)
# ════════════════════════════════════════════════════════════════════

repo_app = typer.Typer(
    name="repo",
    help="Per-repo operations (purge — full removal across all stores).",
    no_args_is_help=True,
)
app.add_typer(repo_app, name="repo")


@repo_app.command("purge")
def repo_purge_cmd(
    slug: str = typer.Argument(
        ...,
        help="Repo slug (e.g. 'gitlab_acme-etl'). "
             "Use `analyzer repo list` (TODO) or check ~/code-analysis/repos/.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    skip_qdrant: bool = typer.Option(
        False, "--skip-qdrant",
        help="Skip Qdrant point deletion (for when Qdrant is down)",
    ),
    skip_disk: bool = typer.Option(
        False, "--skip-disk",
        help="Skip clone/data/vault rmtree (for dry-run or recovery)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Completely remove a repository: Qdrant points + clone + graph + vault
    + Postgres ProjectRepo links + SQLite auto_review/review_runs +
    RepoGroup membership in all groups.

    The steps are isolated — a failure of one does not block the others. Check
    `errors` in the summary.

    Examples:
        analyzer repo purge gitlab_acme-etl --yes
        analyzer repo purge acme-frontend
    """
    _setup_logging(verbose)
    import asyncio

    from src.db import async_session
    from src.repos.purge import purge_repo

    if not yes:
        confirm = typer.confirm(
            f"Purge '{slug}' completely (clone + graph + vault + Qdrant + DB)?",
        )
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(0)

    async def _run():
        async with async_session() as s:
            return await purge_repo(
                slug, session=s,
                skip_qdrant=skip_qdrant, skip_disk=skip_disk,
            )

    console.print(f"[cyan]Purging[/cyan] [bold]{slug}[/bold]…")
    report = asyncio.run(_run())

    console.print()
    console.print("[bold]Purge report:[/bold]")
    console.print(f"  Qdrant points removed:   [cyan]{report.qdrant_points_deleted}[/cyan]")
    console.print(f"  Clone dir removed:       {'[green]yes[/green]' if report.clone_dir_removed else '[dim]no[/dim]'} ({report.clone_dir_path})")
    console.print(f"  Data dir removed:        {'[green]yes[/green]' if report.data_dir_removed else '[dim]no[/dim]'} ({report.data_dir_path})")
    console.print(f"  Vault dir removed:       {'[green]yes[/green]' if report.vault_dir_removed else '[dim]no[/dim]'} ({report.vault_dir_path})")
    console.print(f"  Postgres links removed:  [cyan]{report.project_repo_links_removed}[/cyan]")
    console.print(f"  auto_review rows:        [cyan]{report.auto_review_rows_removed}[/cyan]")
    console.print(f"  review_runs rows:        [cyan]{report.review_run_rows_removed}[/cyan]")
    console.print(f"  Group memberships:       [cyan]{report.group_memberships_removed}[/cyan] ({', '.join(report.groups_touched) or '—'})")
    mb = report.disk_bytes_freed / (1024 * 1024)
    console.print(f"  Disk freed:              [magenta]{mb:.2f} MB[/magenta]")
    console.print(f"  Elapsed:                 {report.elapsed_seconds:.2f}s")
    if report.errors:
        console.print()
        console.print(f"[red]✗ {len(report.errors)} non-fatal errors:[/red]")
        for e in report.errors:
            console.print(f"  • {e}")
        raise typer.Exit(1)
    console.print()
    console.print(f"[green]✓ Purged '{slug}' completely.[/green]")


@group_app.command("list")
def group_list_cmd(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """List all groups in the workspace."""
    _setup_logging(verbose)
    from src.groups import get_group_manager

    mgr = get_group_manager()
    names = mgr.list()
    if not names:
        console.print("[dim]No groups defined yet. Create one with `analyzer group create <name>`.[/dim]")
        return

    console.print(f"[bold]Groups ({len(names)}):[/bold]")
    for name in names:
        try:
            g = mgr.load(name)
            n_repos = len(g.repos)
            desc = g.description or "[dim](no description)[/dim]"
            console.print(f"  • [cyan]{name}[/cyan] ({n_repos} repos) — {desc}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  • [cyan]{name}[/cyan] — [red]error: {exc}[/red]")


@group_app.command("show")
def group_show_cmd(
    name: str = typer.Argument(..., help="Group name"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show the group details with the full list of repos."""
    _setup_logging(verbose)
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError

    mgr = get_group_manager()
    try:
        g = mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    console.print(f"[bold cyan]{g.name}[/bold cyan]")
    if g.description:
        console.print(f"  {g.description}")
    console.print(f"  created: {g.created_at}")
    console.print(f"  updated: {g.updated_at}")
    console.print(f"  repos ({len(g.repos)}):")
    for repo_id in g.repos:
        try:
            from src.sync.git_providers import parse_repo_url
            parsed = parse_repo_url(repo_id)
            console.print(
                f"    • {repo_id} → [dim]slug={parsed.slug}, "
                f"provider={parsed.provider.value}[/dim]"
            )
        except Exception:
            console.print(f"    • {repo_id} [red](unparseable)[/red]")


@group_app.command("create")
def group_create_cmd(
    name: str = typer.Argument(..., help="Group name (A-Z, a-z, 0-9, _, -; max 64)"),
    desc: str = typer.Option("", "--desc", "-d", help="Optional description"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Create a new empty group."""
    _setup_logging(verbose)
    from src.groups import get_group_manager
    from src.groups.manager import GroupValidationError

    mgr = get_group_manager()
    try:
        g = mgr.create(name, description=desc)
    except GroupValidationError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from None

    console.print(f"[green]✓ Group '{g.name}' created.[/green] Add repos with `analyzer group add {name} <repo>`")


@group_app.command("delete")
def group_delete_cmd(
    name: str = typer.Argument(..., help="Group name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Delete a group. Per-repo data (clones, graphs) is NOT deleted."""
    _setup_logging(verbose)
    from src.groups import get_group_manager

    if not yes:
        confirm = typer.confirm(f"Delete group '{name}'?")
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(0)

    mgr = get_group_manager()
    if mgr.delete(name):
        console.print(f"[green]✓ Group '{name}' deleted.[/green]")
    else:
        console.print(f"[yellow]Group '{name}' did not exist.[/yellow]")
        raise typer.Exit(1)


@group_app.command("add")
def group_add_cmd(
    name: str = typer.Argument(..., help="Group name"),
    repo: str = typer.Argument(
        ..., help="Repo identifier — slug (foo/bar), URL, or 'github:foo/bar'",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Add a repo to a group."""
    _setup_logging(verbose)
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError

    mgr = get_group_manager()
    try:
        added = mgr.add_repo(name, repo)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found. Create it first.[/red]")
        raise typer.Exit(1) from None
    except ValueError as exc:
        console.print(f"[red]Invalid repo identifier: {exc}[/red]")
        raise typer.Exit(1) from None

    if added:
        console.print(f"[green]✓ Added '{repo}' to '{name}'.[/green]")
    else:
        console.print(f"[yellow]'{repo}' already in group '{name}'.[/yellow]")


@group_app.command("remove")
def group_remove_cmd(
    name: str = typer.Argument(..., help="Group name"),
    repo: str = typer.Argument(..., help="Repo identifier — any form"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Remove a repo from a group."""
    _setup_logging(verbose)
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError

    mgr = get_group_manager()
    try:
        removed = mgr.remove_repo(name, repo)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    if removed:
        console.print(f"[green]✓ Removed '{repo}' from '{name}'.[/green]")
    else:
        console.print(f"[yellow]'{repo}' not in the group.[/yellow]")
        raise typer.Exit(1)


@group_app.command("add-from-github-org")
def group_add_from_github_org_cmd(
    name: str = typer.Argument(..., help="Group name"),
    org: str = typer.Argument(..., help="GitHub organization name"),
    type_filter: str = typer.Option(
        "all", "--type", "-t",
        help="GitHub repo type: all|public|private|forks|sources|member",
    ),
    limit: int = typer.Option(200, "--limit", "-l", help="Max repos"),
    skip_archived: bool = typer.Option(
        True, "--skip-archived/--include-archived",
        help="Skip archived repos (default: skip)",
    ),
    skip_forks: bool = typer.Option(
        True, "--skip-forks/--include-forks",
        help="Skip forks (default: skip — limit to actual development repos)",
    ),
    label: str = typer.Option("default", "--label",
        help="Credential account label (for multiple GitHub accounts)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Bulk add: all repos from a GitHub organization into a group.

    Requires `analyzer auth github` first. We skip archived + forks by
    default — overrideable via flags.
    """
    _setup_logging(verbose)
    from src.credentials import resolve_git_credential
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError
    from src.sync.github_api import GitHubClient

    # Load credentials
    creds = resolve_git_credential("github", account_label=label)
    if creds is None:
        console.print(
            f"[red]No GitHub credentials saved as '[cyan]{label}[/cyan]'.[/red]\n"
            f"Run [bold]analyzer auth github --label {label}[/bold] first."
        )
        raise typer.Exit(1)

    # Verify group exists
    mgr = get_group_manager()
    try:
        mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    # Fetch repos
    console.print(f"[bold]Fetching {org}/* from GitHub…[/bold]")
    with GitHubClient(creds.secret) as client:
        repos = client.list_org_repos(org, repo_type=type_filter, limit=limit)

    if not repos:
        console.print(f"[yellow]No repos found in org '{org}'.[/yellow]")
        return

    # Filter
    candidates = []
    for r in repos:
        if skip_archived and r.archived:
            continue
        if skip_forks and r.fork:
            continue
        candidates.append(r)

    console.print(
        f"  total fetched: {len(repos)}, "
        f"after filter: {len(candidates)} "
        f"(archived skipped: {sum(1 for r in repos if r.archived)}, "
        f"forks skipped: {sum(1 for r in repos if r.fork)})"
    )

    # Bulk add
    added = 0
    skipped = 0
    for r in candidates:
        repo_id = f"github:{r.full_name}"
        if mgr.add_repo(name, repo_id):
            added += 1
        else:
            skipped += 1  # already in group

    console.print(
        f"[green]✓ Added {added} repos to group '{name}'[/green] "
        f"[dim](skipped {skipped} duplicates)[/dim]"
    )


@group_app.command("add-from-gitlab-group")
def group_add_from_gitlab_group_cmd(
    name: str = typer.Argument(..., help="Local group name"),
    gitlab_group: str = typer.Argument(..., help="GitLab group path (group/subgroup)"),
    include_subgroups: bool = typer.Option(
        True, "--include-subgroups/--no-subgroups",
        help="Include nested subgroups (default: yes)",
    ),
    limit: int = typer.Option(200, "--limit", "-l"),
    skip_archived: bool = typer.Option(True, "--skip-archived/--include-archived"),
    skip_forks: bool = typer.Option(True, "--skip-forks/--include-forks"),
    label: str = typer.Option("default", "--label"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Bulk add: all projects from a GitLab group into a local group."""
    _setup_logging(verbose)
    from src.credentials import resolve_git_credential
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError
    from src.sync.gitlab_api import GitLabClient

    creds = resolve_git_credential("gitlab", account_label=label)
    if creds is None:
        console.print(
            f"[red]No GitLab credentials saved as '[cyan]{label}[/cyan]'.[/red]\n"
            f"Run [bold]analyzer auth gitlab --label {label}[/bold] first."
        )
        raise typer.Exit(1)

    mgr = get_group_manager()
    try:
        mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    console.print(f"[bold]Fetching projects from GitLab group {gitlab_group}…[/bold]")
    with GitLabClient(creds.secret) as client:
        projects = client.list_group_projects(
            gitlab_group,
            include_subgroups=include_subgroups,
            limit=limit,
        )

    if not projects:
        console.print(f"[yellow]No projects found in group '{gitlab_group}'.[/yellow]")
        return

    candidates = [
        p for p in projects
        if not (skip_archived and p.archived)
        and not (skip_forks and p.fork)
    ]
    console.print(
        f"  total fetched: {len(projects)}, "
        f"after filter: {len(candidates)}"
    )

    added = 0
    skipped = 0
    for p in candidates:
        repo_id = f"gitlab:{p.full_path}"
        if mgr.add_repo(name, repo_id):
            added += 1
        else:
            skipped += 1

    console.print(
        f"[green]✓ Added {added} projects to group '{name}'[/green] "
        f"[dim](skipped {skipped} duplicates)[/dim]"
    )


@group_app.command("ask")
def group_ask_cmd(
    name: str = typer.Argument(..., help="Group name"),
    question: str = typer.Argument(..., help="Natural-language question"),
    repo: str = typer.Option(
        None, "--repo", "-r",
        help="Specific repo slug in the group (default: prompt interactively)",
    ),
    raw: bool = typer.Option(False, "--raw"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Q&A scoped to a group: ask about a specific repo or about cross-repo edges.

    If `--repo` is not given — an interactive prompt to pick from the indexed repos
    in the group. Cross-repo edges materialized after `analyzer group index` add
    context about the connections between repos (image refs, build paths).
    """
    _setup_logging(verbose)
    from src.config import get_settings
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError
    from src.qa.orchestrator import QAOrchestrator
    from src.sync.git_providers import parse_repo_url

    mgr = get_group_manager()
    try:
        group = mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    if not group.repos:
        console.print(f"[yellow]Group '{name}' is empty.[/yellow]")
        raise typer.Exit(1)

    # Filter to indexed repos
    settings = get_settings()
    indexed_slugs = []
    for repo_id in group.repos:
        try:
            slug = parse_repo_url(repo_id).slug
        except ValueError:
            continue
        if settings.repo_graph_path(slug).exists():
            indexed_slugs.append(slug)

    if not indexed_slugs:
        console.print(
            f"[yellow]No indexed repos in group '{name}'.[/yellow]\n"
            f"Run [bold]analyzer group index {name}[/bold] first."
        )
        raise typer.Exit(1)

    # Resolve target repo
    if repo:
        if repo not in indexed_slugs:
            console.print(
                f"[red]Repo '{repo}' not indexed in group '{name}'.[/red]\n"
                f"Available: {', '.join(indexed_slugs)}"
            )
            raise typer.Exit(1)
        target_slug = repo
    elif len(indexed_slugs) == 1:
        target_slug = indexed_slugs[0]
        console.print(f"[dim]→ querying [cyan]{target_slug}[/cyan][/dim]")
    else:
        # Interactive prompt
        console.print(f"\n[bold]Indexed repos in group '{name}':[/bold]")
        for i, slug in enumerate(indexed_slugs, 1):
            console.print(f"  {i}. [cyan]{slug}[/cyan]")
        choice = typer.prompt(
            "\nSelect repo number",
            type=int,
            default=1,
        )
        if not 1 <= choice <= len(indexed_slugs):
            console.print(f"[red]Invalid selection {choice}[/red]")
            raise typer.Exit(1)
        target_slug = indexed_slugs[choice - 1]

    console.print(
        f"\n[dim]Querying [cyan]{target_slug}[/cyan] "
        f"(group [magenta]{name}[/magenta], {len(indexed_slugs)} repos indexed)…[/dim]\n"
    )

    qa = QAOrchestrator()
    answer = qa.ask(question=question, repo=target_slug)

    if raw:
        console.print(answer.as_markdown())
    else:
        console.print(Markdown(answer.text))
        console.print("\n[dim]---[/dim]")
        console.print(
            f"[dim]repo={target_slug} type={answer.question_type} "
            f"route={answer.route} tokens={answer.tokens_in}/{answer.tokens_out} "
            f"files={len(answer.files_read)}[/dim]"
        )

    # Show cross-repo edges hint
    from src.groups import get_group_manager as _gm

    try:
        _mgr = _gm()
        cross_repo_path = _mgr.graph_path(_mgr.load(name))
    except Exception:  # noqa: BLE001
        cross_repo_path = settings.workspace_dir / "groups" / f"{name}.fdblite"
    if cross_repo_path.exists():
        console.print(
            f"\n[dim]💡 Group has cross-repo edges materialized. "
            f"Use [bold]analyzer group cross-edges {name}[/bold] for an overview.[/dim]"
        )


@group_app.command("cross-edges")
def group_cross_edges_cmd(
    name: str = typer.Argument(..., help="Group name"),
    limit: int = typer.Option(50, "--limit", "-l"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show materialized cross-repo edges for a group in table form."""
    _setup_logging(verbose)
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError
    from src.indexing.graph.graph_store import make_graph_store

    mgr = get_group_manager()
    try:
        group = mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    # The manager owns the layout; a path rebuilt from the bare name misses
    # every tenant-scoped group.
    db_path = mgr.graph_path(group)
    if not db_path.exists():
        console.print(
            f"[yellow]No cross-repo edges materialized for '{name}'.[/yellow]\n"
            f"Run [bold]analyzer group index {name}[/bold] first."
        )
        raise typer.Exit(1)

    store = make_graph_store(db_path)
    try:
        edges = store.query(
            "MATCH (a:Symbol)-[r]->(b:Symbol) "
            "RETURN a.id AS from_id, a.module AS from_repo, "
            "       b.id AS to_id, b.module AS to_repo, "
            "       type(r) AS kind "
            "LIMIT $limit",
            params={"limit": limit},
        )
    finally:
        store.close()

    if not edges:
        console.print(f"[yellow]No cross-repo edges yet in group '{name}'.[/yellow]")
        return

    from rich.table import Table
    table = Table(title=f"Cross-repo edges in '{name}'", show_lines=False)
    table.add_column("Kind", style="magenta")
    table.add_column("From repo", style="cyan")
    table.add_column("From symbol")
    table.add_column("→ Target repo", style="cyan")
    table.add_column("Target")

    for e in edges:
        from_id = str(e.get("from_id") or "")
        to_id = str(e.get("to_id") or "")
        # Strip the repo prefix from the ID for readability
        from_local = from_id.split("::", 1)[-1] if "::" in from_id else from_id
        to_local = to_id.split("::", 1)[-1] if "::" in to_id else to_id
        table.add_row(
            str(e.get("kind") or ""),
            str(e.get("from_repo") or ""),
            from_local[:40],
            str(e.get("to_repo") or ""),
            to_local[:40],
        )

    console.print(table)
    console.print(f"\n[dim]Showing {len(edges)} edges (limit={limit})[/dim]")


@group_app.command("generate")
def group_generate_cmd(
    name: str = typer.Argument(..., help="Group name"),
    branch: str = typer.Option(
        None, "--branch", "-b",
        help="Git branch (None → repo default; 'dev' = legacy Bitbucket default)",
    ),
    skip_semgrep: bool = typer.Option(
        False, "--skip-semgrep", help="Skip SAST scan for a faster run",
    ),
    force: bool = typer.Option(
        False, "--force", help="Rebuild even if the vault already exists",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the full PRD/BRD generation pipeline for all repos in the group +
    materialize cross-repo edges + write group overview document.

    Output:
        ~/code-analysis/repos/{slug}/                  # cloned repos
        ~/code-analysis/data/{slug}/graph.fdblite      # per-repo graphs
        ~/code-analysis/groups/{name}.fdblite          # cross-repo edges
        ~/ObsidianVaults/code-analysis/projects/...    # per-repo PRDs
        ~/ObsidianVaults/code-analysis/groups/{name}/_overview.md  # group overview
    """
    _setup_logging(verbose)
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from src.generation.group_orchestrator import GroupGenerationOrchestrator
    from src.groups import get_group_manager
    from src.groups.manager import GroupNotFoundError

    mgr = get_group_manager()
    try:
        group = mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    if not group.repos:
        console.print(f"[yellow]Group '{name}' is empty. Add repos first.[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"[bold]Generating group '[cyan]{name}[/cyan]' "
        f"({len(group.repos)} repos)…[/bold]"
    )

    orch = GroupGenerationOrchestrator(group)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("…", total=None)

        def _cb(phase: str, detail: str) -> None:
            progress.update(task, description=f"[{phase}] {detail}")

        result = orch.run(
            branch=branch, skip_semgrep=skip_semgrep, force=force,
            progress_callback=_cb,
        )

    # ─── Summary ─────────────────────────────────────────
    console.print()
    console.print("[bold]Result:[/bold]")
    console.print(f"  repos generated: [green]{len(result.per_repo)}[/green]")
    if result.failures:
        console.print(f"  failures:        [red]{len(result.failures)}[/red]")
        for f in result.failures:
            console.print(f"    • {f}")
    console.print(f"  total modules:   [cyan]{result.total_modules}[/cyan]")
    console.print(f"  total features:  [cyan]{result.total_features}[/cyan]")
    console.print(f"  cross-repo edges: [magenta]{result.cross_repo_edges}[/magenta]")
    console.print(f"  total tokens:    [cyan]{result.total_tokens:,}[/cyan]")
    console.print(f"  elapsed:         {result.elapsed_seconds:.1f}s")
    if result.overview_path:
        console.print(f"\n  overview: [link]{result.overview_path}[/link]")


@group_app.command("index")
def group_index_cmd(
    name: str = typer.Argument(..., help="Group name"),
    enable_scip: bool = typer.Option(
        False, "--scip", help="Run SCIP indexers per repo (Python/TS/Go) — requires binaries in PATH",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the full pipeline: clone + index per-repo + materialize cross-repo edges.

    Every repo:
        - cloned/updated in ~/code-analysis/repos/{slug}/
        - indexed in ~/code-analysis/data/{slug}/graph.fdblite
        - if --scip: optionally enriched via scip-python/scip-typescript/scip-go
          (gracefully skip languages without an installed binary)
    Group cross-repo edges:
        - persisted in ~/code-analysis/groups/{name}.fdblite
    """
    _setup_logging(verbose)
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from src.groups import GroupIndexer, get_group_manager
    from src.groups.manager import GroupNotFoundError

    mgr = get_group_manager()
    try:
        g = mgr.load(name)
    except GroupNotFoundError:
        console.print(f"[red]Group '{name}' not found.[/red]")
        raise typer.Exit(1) from None

    if not g.repos:
        console.print(f"[yellow]Group '{name}' is empty. Add repos with `analyzer group add`.[/yellow]")
        raise typer.Exit(1)

    console.print(f"[bold]Indexing group '[cyan]{name}[/cyan]' ({len(g.repos)} repos)…[/bold]")

    indexer = GroupIndexer(g)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("…", total=None)

        def _cb(msg: str) -> None:
            progress.update(task, description=msg)

        result = indexer.index(progress_callback=_cb, enable_scip=enable_scip)

    # Summary
    console.print()
    console.print("[bold]Result:[/bold]")
    console.print(f"  repos indexed: [green]{len(result.repos_indexed)}[/green]")
    if result.failures:
        console.print(f"  failures: [red]{len(result.failures)}[/red]")
        for f in result.failures:
            console.print(f"    • {f}")
    console.print(f"  total symbols: [cyan]{result.total_symbols:,}[/cyan]")
    console.print(f"  total edges:   [cyan]{result.total_edges:,}[/cyan]")
    console.print(f"  cross-repo:    [magenta]{result.cross_repo_edges}[/magenta] edges")
    console.print(f"  elapsed:       {result.elapsed_seconds:.1f}s")

    if verbose:
        console.print()
        for r in result.repos_indexed:
            console.print(
                f"  [cyan]{r.slug}[/cyan]: {r.files_processed} files, "
                f"{r.symbols} symbols, {r.edges_resolved} edges, {r.elapsed_seconds:.1f}s"
            )


if __name__ == "__main__":
    app()
