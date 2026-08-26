"""PR Review microservice — isolated module with its own settings + domain model.

Phase 17 (May 2026): on-demand activation, multi-agent reviewer
(Architect/Security via Gemini 3 Pro + Quality/Tests via Gemini 3 Flash + Verifier).

Designed as a standalone microservice — `src/review/` has:
    - A separate ReviewSettings (env: REVIEW_*)
    - Self-contained domain model (PullRequest, Hunk, Finding, ReviewBatch)
    - Provider abstractions (GitHub/GitLab/Bitbucket — share existing API clients)
    - Lifecycle management (warm cache + cold S3 snapshot)
    - Webhook server (FastAPI)
    - Reuses existing graph/credentials infrastructure

It can be moved out into a separate process / deployable unit later — the module
has no hard imports from generation/ or qa/ (only spread credentials/, sync/,
indexing/).
"""

from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.settings import ReviewSettings, get_review_settings

__all__ = [
    "Finding",
    "FindingSeverity",
    "Hunk",
    "PullRequest",
    "ReviewBatch",
    "ReviewSettings",
    "ReviewVerdict",
    "get_review_settings",
]
