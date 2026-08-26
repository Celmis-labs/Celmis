"""The whole API schema was readable by anybody.

Measured against production before this changed: GET /openapi.json answered
200 with 250 KB describing all 180 routes — parameters, payload shapes and
all — including /api/ops/*, the audit endpoints and GDPR. No credential
leaks that way, and no single route is a vulnerability by itself. It is a
complete map of the installation, handed to a stranger, in a product sold to
people whose own auditors ask precisely that question.

FastAPI mounts those two routes itself and they cannot be gated, so they are
switched off and re-served behind a session at the same URLs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Not the word "placeholder" — the startup check greps for it, and this
    # fixture named itself out of a job.
    os.environ.setdefault("CELMIS_JWT_SECRET", "b91d4e07c25af8361de0c47ba style".replace(" ", ""))
    os.environ.setdefault("GEMINI_API_KEY", "test-key-12345678")
    from src.api.main import build_app

    return TestClient(build_app())


def test_the_schema_needs_a_session(client):
    assert client.get("/openapi.json").status_code == 401


def test_the_docs_page_needs_a_session(client):
    assert client.get("/docs").status_code == 401


def test_it_answers_401_rather_than_pretending_to_be_absent(client):
    """404 would be a lie the next version has to keep telling, and it sends
    an operator who bookmarked the page hunting for a route that is right
    there."""
    for path in ("/openapi.json", "/docs"):
        assert client.get(path).status_code != 404, f"{path} pretends not to exist"


def test_health_stays_open(client):
    """Load balancers, uptime checks and the container's own healthcheck are
    not signed in and never will be."""
    assert client.get("/healthz").status_code == 200


def test_the_container_healthcheck_does_not_probe_a_gated_route():
    """This is the one that would have broken production: compose curled
    /docs with `-fsS`, which treats 401 as failure — the API would have been
    marked unhealthy while serving every request correctly."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    api_probe = [line for line in compose.splitlines()
                 if "localhost:8000" in line and "test:" in line]
    assert api_probe, "the api healthcheck moved — check this still holds"
    assert "/docs" not in api_probe[0], (
        "the healthcheck probes a route that now requires a session"
    )


def test_it_can_be_reopened_deliberately():
    """Somebody running this on a laptop wants Swagger back, and should not
    have to patch source to get it."""
    src = (ROOT / "src" / "api" / "main.py").read_text(encoding="utf-8")
    assert "CELMIS_PUBLIC_API_DOCS" in src
