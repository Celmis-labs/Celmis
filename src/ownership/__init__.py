"""Ownership graph — auto-derived per-file/path ownership from
git blame + CODEOWNERS + recent PR authorship.

No YAML to maintain. Rebuilt on demand or nightly. Powers:
    * MCP tool `get_owner(repo_slug, path_or_symbol)`
    * Auto-reviewer assignment on PR open
    * Sentry-style incident routing
    * Breaking-change consumer notifications
"""

from src.ownership.builder import compute_ownership, load_snapshot

__all__ = ["compute_ownership", "load_snapshot"]
