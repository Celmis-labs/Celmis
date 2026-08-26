"""RepoGroup module — cross-repo analysis over several repositories.

Group = a logical union of repositories (for example "acme-platform"
includes frontend + backend + mobile). Index/ask operations automatically
cover all repos of the group; cross-repo edges get materialized between them.

Stage 5 (May 2026) — implementation.
"""

from src.groups.cross_repo import CrossRepoEdge, CrossRepoMaterializer
from src.groups.indexer import GroupIndexer, GroupIndexResult, index_group
from src.groups.manager import GroupManager, GroupNotFoundError, get_group_manager
from src.groups.models import RepoGroup

__all__ = [
    "CrossRepoEdge",
    "CrossRepoMaterializer",
    "GroupIndexer",
    "GroupIndexResult",
    "GroupManager",
    "GroupNotFoundError",
    "RepoGroup",
    "get_group_manager",
    "index_group",
]
