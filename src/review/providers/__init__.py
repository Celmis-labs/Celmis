"""Per-provider PR adapters — fetch PR + diff + post review/comments.

Provider abstraction:
    PullRequestProvider — interface
    GitHubPRProvider, GitLabPRProvider, BitbucketPRProvider — implementations

Spread existing API clients (Phase 9: github_api.py / gitlab_api.py /
bitbucket_api.py) for basic auth + httpx setup. We extend the PR-specific
endpoints here.
"""

from src.review.providers.base import PullRequestProvider, get_provider_for
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider

__all__ = [
    "BitbucketPRProvider",
    "GitHubPRProvider",
    "GitLabPRProvider",
    "PullRequestProvider",
    "get_provider_for",
]
