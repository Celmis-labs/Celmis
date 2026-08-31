"""`/readyz` and `/metrics` were public, and both said more than a probe needs.

Both sit in `middleware.py::_EXEMPT_PREFIXES` — no authentication — and Caddy
proxies `/backend/*`, so both answered the internet.

`/readyz` returned per-dependency detail including `{"users": count()}` and
`str(exc)[:200]` from a failed connection, which for a bad DSN is a fragment of
the DSN. `/metrics` returned the whole Prometheus slice: queue depths, spend,
review counts, error rates — an operational map of the installation and of its
customers' activity.

Neither endpoint is removed. A probe needs 200 or 503 and gets exactly that;
the bundled Prometheus scrapes `api:8000/metrics` DIRECTLY over the compose
network (observability/prometheus.yml) and keeps working. What changed is who
else can read them.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("MCP_ALLOW_UNAUTHENTICATED", "1")
    from fastapi.testclient import TestClient

    from src.api.main import build_app

    with TestClient(build_app()) as c:
        yield c


# ─── /readyz: the verdict is public, the detail is not ───────────────


def test_readyz_answers_a_probe(client) -> None:
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
    assert set(response.json()) == {"ok"}, (
        f"a probe was handed more than ok/not-ok: {response.json()}"
    )


def test_readyz_does_not_name_dependencies_or_errors(client) -> None:
    body = client.get("/readyz").text
    for leak in ("checks", "users", "error", "postgres", "qdrant", "user_store"):
        assert leak not in body, f"/readyz still discloses {leak!r}: {body[:200]}"


def test_the_detail_still_exists_for_an_operator() -> None:
    """Gated, not deleted — the deep check is how an operator finds a down dep."""
    import inspect

    from src.api.routers.ops_metrics import readyz_detail

    dependency = inspect.signature(readyz_detail).parameters["_user"].default
    assert "require_admin" in repr(dependency)


# ─── /metrics: the scraper, not the internet ─────────────────────────


def test_a_direct_request_is_served(client) -> None:
    """No X-Forwarded-For means it did not pass a proxy — the scraper's case."""
    response = client.get("/metrics")
    assert response.status_code == 200


def test_a_proxied_request_without_a_token_is_not(client) -> None:
    """The public path. 404 rather than 401: a "wrong token" confirms the door."""
    response = client.get("/metrics", headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 404
    assert "celmis_" not in response.text


def test_a_proxied_request_with_the_token_is_served(client, monkeypatch) -> None:
    monkeypatch.setenv("CELMIS_METRICS_TOKEN", "s3cret-metrics-token")
    response = client.get(
        "/metrics",
        headers={"X-Forwarded-For": "203.0.113.9",
                 "Authorization": "Bearer s3cret-metrics-token"},
    )
    assert response.status_code == 200


def test_a_wrong_token_is_refused(client, monkeypatch) -> None:
    monkeypatch.setenv("CELMIS_METRICS_TOKEN", "s3cret-metrics-token")
    response = client.get(
        "/metrics",
        headers={"X-Forwarded-For": "203.0.113.9", "Authorization": "Bearer wrong"},
    )
    assert response.status_code == 404


def test_the_forwarded_header_cannot_be_removed_by_a_caller() -> None:
    """Why the rule is safe, stated where somebody will read it.

    A caller can ADD X-Forwarded-For, which only marks them as proxied and
    costs them access. They cannot cause Caddy's to be absent. The check
    therefore fails closed for the public path, which is the direction that
    matters.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py")
    tree = ast.parse(source.read_text("utf-8"))
    metrics = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "metrics"
    )
    names = {n.id for n in ast.walk(metrics) if isinstance(n, ast.Name)}
    assert "proxied" in names, "the proxy check is gone from /metrics"
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "compare_digest"
        for n in ast.walk(metrics)
    ), "the token comparison is no longer constant-time"
