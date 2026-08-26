"""Architecture-summary generator (Stage 15).

Single LLM call — the *only* review-adjacent path that fires without a
PR. Produces a short markdown summary a new engineer can read to get
oriented. Cached in ``repo_summaries``; users re-run manually when big
refactors land.

Not part of the review agent pipeline — this is a standalone helper the
API endpoint invokes on demand.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


_SYSTEM = """You produce a concise ARCHITECTURE SUMMARY for a code \
repository. Deliverable format (strict Markdown, at most ~600 words):

# Overview
2-3 sentences: what this repo does, who runs it.

# Entry points
Bullet list of executables / HTTP entry files / CLI commands with one-liner each.

# Main flows
3-6 bullets describing the highest-value user flows and which modules \
they touch. Cite file paths in backticks.

# Domain & data
1 paragraph: primary data model / persistence.

# External integrations
Bullet list of external systems the repo talks to (DBs, queues, HTTP APIs).

# For new engineers
3 bullets: "start here" files, common pitfalls, how to run tests.

Rules:
- Cite only files/symbols you have direct evidence for.
- No filler. If something isn't clear, say "unclear".
- Do not invent modules that don't appear in the input.
"""


def generate_summary(
    *,
    repo_slug: str,
    user_id: str,
    workspace_id: str = "default",
    max_files: int = 40,
    max_bytes: int = 20_000,
) -> tuple[str, str | None, int]:
    """Returns (markdown, model_used, total_tokens).

    Never raises; an empty markdown string means the summary could NOT be
    produced. The caller must treat that as a failure rather than storing it —
    an empty summary saved under a success response is how "Architecture
    rebuilt" ended up sitting above "(no summary — rebuild)".
    """
    context = _collect_context(repo_slug, max_files=max_files, max_bytes=max_bytes)
    if not context:
        logger.info("arch_no_context repo=%s — not indexed or not cloned", repo_slug)
        return "", None, 0

    # Go out through the workspace PROFILE, the same way the dependency report
    # does, rather than through `build_llm_client`.
    #
    # The old path read the workspace config, fell back to
    # `get_review_settings().quality_model` and handed the result to
    # build_llm_client. On a workspace routed through the LiteLLM gateway —
    # which is every workspace in production — that model name is a bare
    # Google one, so the client went looking for a raw `gemini` key the
    # workspace does not hold (it holds a LiteLLM virtual key). Passing the
    # gateway model instead does not help either: build_llm_client rejects the
    # `litellm_proxy/` provider outright.
    #
    # Rebuild therefore failed on every gateway workspace, and reported it as
    # "the model call failed, or this repository has no indexed clone" — which
    # sent people off to index a repository that was already indexed.
    try:
        from src.llm.profiles import resolve_profile
        p = resolve_profile("review", workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("arch_profile_failed ws=%s err=%s", workspace_id, exc)
        return "", None, 0
    if not p.api_key:
        logger.warning("arch_no_key ws=%s provider=%s", workspace_id, p.provider)
        return "", None, 0

    prompt = f"{_SYSTEM}\n\n{context}"
    try:
        # LiteLLM for every provider, Google included. `p.litellm_model` is
        # `litellm_proxy/<deployment>` behind the gateway and `gemini/<model>`
        # without one, so the same call covers both and nothing here is bound
        # to one vendor's SDK.
        import litellm
        resp = litellm.completion(
            model=p.litellm_model, api_key=p.api_key,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=2048, timeout=120,
        )
        text = resp.choices[0].message.content or ""
        model_used = p.litellm_model
        usage = getattr(resp, "usage", None)
        tokens = ((getattr(usage, "prompt_tokens", 0) or 0)
                  + (getattr(usage, "completion_tokens", 0) or 0))
        # Spend ledger — without this a rebuild is invisible on the Usage
        # page, the same blind spot that hid the failing review agents.
        from src.llm.completion import record_completion_spend
        record_completion_spend(
            p, resp, operation="architecture_summary",
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("arch_llm_failed repo=%s err=%s", repo_slug, exc)
        return "", None, 0
    return text.strip(), model_used, tokens


def _collect_context(
    repo_slug: str, *, max_files: int, max_bytes: int,
) -> str:
    """Build a compact evidence blob: top-level file tree + first N bytes
    of the readmes / entry files."""
    from src.config import get_settings
    settings = get_settings()
    p = settings.repo_path(repo_slug)
    if not p.exists():
        return ""
    files = _tracked_files(p)
    if not files:
        return ""
    tree_preview = "\n".join(f"  - {f}" for f in files[:80])

    # Read up to `max_files` heuristically-picked entry files.
    picks = _pick_entry_files(files, cap=max_files)
    snippets: list[str] = []
    budget = max_bytes
    for f in picks:
        try:
            data = (p / f).read_text(errors="replace")
        except OSError:
            continue
        take = min(1500, budget)
        if take <= 0:
            break
        snippets.append(f"### `{f}`\n```\n{data[:take]}\n```")
        budget -= take
    joined = "\n\n".join(snippets)
    return (
        f"# Repository: {repo_slug}\n\n"
        f"## File tree (first 80)\n{tree_preview}\n\n"
        f"## Entry files (excerpts)\n{joined}\n"
    )


def _tracked_files(p) -> list[str]:
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", str(p), "ls-files"],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _pick_entry_files(files: list[str], *, cap: int) -> list[str]:
    """Prioritise READMEs, main files, packaging descriptors, HTTP routers."""
    picks: list[str] = []
    seen = set()

    def add(fname: str):
        if fname not in seen and fname in files:
            picks.append(fname)
            seen.add(fname)

    # Top-level markers.
    for m in ("README.md", "README.rst", "package.json", "pyproject.toml",
              "Cargo.toml", "go.mod", "Dockerfile", "docker-compose.yml"):
        add(m)

    # Common entry-point names.
    keywords = ("main.", "server.", "app.", "index.", "wsgi.", "asgi.",
                "cli.", "routes", "urls.", "handler.")
    for f in files:
        base = f.rsplit("/", 1)[-1].lower()
        if any(base.startswith(k) for k in keywords):
            add(f)
            if len(picks) >= cap:
                break
    return picks[:cap]


__all__ = ["generate_summary"]
