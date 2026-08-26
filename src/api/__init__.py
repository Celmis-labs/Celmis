"""Celmis REST API — FastAPI app for the Next.js web client.

Exposes existing services (review, indexing, providers, credentials, users)
as JSON endpoints that the Next.js frontend can call.
"""

from src.api.main import build_app

__all__ = ["build_app"]
