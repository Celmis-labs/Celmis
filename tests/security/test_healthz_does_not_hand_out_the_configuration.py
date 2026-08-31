"""`/backend/healthz` answered with the installation's configuration.

`src/api/main.py` copies the webhook sub-app's routes into the main app. The
sub-app declares its own detailed `/healthz`, and a copied route lands in
`app.router.routes` BEFORE the plain one declared by a decorator below — first
match wins. `middleware.py` exempts `/healthz` from authentication and Caddy
proxies `/backend/*`, so the whole chain was public. Observed on production
answering with `defect_model`, `contract_model`, `timeout_seconds`,
`llm_timeout_seconds`, `max_diff_size_bytes`, `has_s3`, `has_redis` and
`hot_cache_size`.

Configuration, not credentials — and a map of the installation is worth having
anyway. The reason that block existed is real and is kept: `env_file` points at
a `.env` that does not exist in the container, so a setting the compose file
does not forward silently takes the code default and nothing outside could tell
which had happened. It is served from `/api/ops/review-settings` behind an
admin now.

TWO ASSERTIONS, because either alone can pass on a broken app: the ROUTE COUNT
(the thing that actually broke), read off the application object rather than
grepped, and the RESPONSE BODY, which catches shadowing by any mechanism.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("MCP_ALLOW_UNAUTHENTICATED", "1")
    from src.api.main import build_app

    return build_app()


#: What the detailed body carried. Any of these in an anonymous answer is the
#: disclosure returning.
CONFIG_KEYS = (
    "review_settings", "defect_model", "contract_model", "timeout_seconds",
    "llm_timeout_seconds", "max_diff_size_bytes", "hot_cache_size",
    "has_s3", "has_redis",
)


def _flat(app) -> list:
    """Routes that live directly on the app — which is where a COPY lands."""
    return [r for r in app.router.routes if getattr(r, "path", "")]


def test_there_is_exactly_one_healthz(app) -> None:
    """The count is what broke. Two routes, one path, first one wins."""
    matches = [r for r in _flat(app) if r.path == "/healthz"]
    assert len(matches) == 1, (
        f"{len(matches)} routes answer /healthz. A second one arrives by being "
        f"copied out of the webhook sub-app and is matched FIRST, which is how "
        f"the detailed body became the public answer."
    )


def test_the_one_that_answers_is_the_plain_one(app) -> None:
    route = next(r for r in _flat(app) if r.path == "/healthz")
    assert route.endpoint.__module__ == "src.api.main", (
        f"/healthz is served by {route.endpoint.__module__}, not the plain "
        f"handler in src.api.main"
    )


def test_the_anonymous_answer_carries_no_configuration(app) -> None:
    """The body, not the routing table — shadowing by any route wins here."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.text
    for key in CONFIG_KEYS:
        assert key not in body, (
            f"an unauthenticated /healthz returned {key!r}: {body[:200]}"
        )


def test_the_operator_can_still_see_what_the_process_resolved() -> None:
    """The need the block served is real; only its audience changed.

    A setting the deployment does not forward takes the code default in
    silence. Removing the answer instead of gating it would have traded one
    invisible failure for another.
    """
    from src.api.routers.ops_metrics import review_settings
    from src.review.webhook import resolved_review_settings

    resolved = resolved_review_settings()
    for key in ("defect_model", "timeout_seconds", "has_s3", "hot_cache_size"):
        assert key in resolved, f"{key} is no longer visible to anybody"

    # And it is gated. Read from the signature rather than by calling it,
    # because calling it would require a user.
    import inspect

    dependency = inspect.signature(review_settings).parameters["_user"].default
    assert "require_admin" in repr(dependency), (
        "the resolved settings are served without an admin dependency"
    )


def test_the_skip_set_names_healthz() -> None:
    """Read with ast: the comment above it explains the fix and says the word.

    A substring search would have passed with the entry deleted — the failure
    this repository keeps rediscovering.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py")
    tree = ast.parse(source.read_text("utf-8"))
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Set):
            for element in node.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    literals.add(element.value)
    assert "/healthz" in literals, (
        "the set of sub-app routes NOT copied into the main app no longer "
        "names /healthz, so the detailed handler is being copied again"
    )
