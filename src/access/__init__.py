"""Fine-grained research-access control (Stage 22).

Governs what a team may *learn* about a repo through Q&A / graph / vector
search — down to individual paths. See :mod:`src.access.resolver`.
"""

from src.access.resolver import (
    RepoAccessDecision,
    glob_match,
    resolve_access,
    resolve_access_sync,
)

__all__ = [
    "RepoAccessDecision",
    "glob_match",
    "resolve_access",
    "resolve_access_sync",
]
