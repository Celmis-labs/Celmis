"""The container has been reporting UNHEALTHY since Swagger was locked down.

    HEALTHCHECK ... CMD curl -fsS http://localhost:8000/docs

`_mount_private_docs` put /docs behind `get_current_user`, so it answers 401 to
an anonymous caller. `curl -f` exits non-zero on a 401. Verified against
production: `GET /backend/docs` → 401.

NOT A LIVE OUTAGE — and this file's first version claimed it was, which is
worth recording. `docker-compose.yml` already overrides the healthcheck with
/healthz, and a compose override wins, so production has reported healthy
throughout. What was broken is the IMAGE: `docker run` on it, or any compose
file without that override, gets a container permanently marked unhealthy
while it serves every request perfectly. Two copies of one decision had
drifted, and the wrong copy is the one a new deployment starts from.

The test asserts the property rather than the URL: whatever route the
HEALTHCHECK names must answer 200 to a caller with NO credentials, because
that is the only kind of caller `curl` inside the container is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client() -> TestClient:
    from src.api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _healthcheck_paths() -> list[str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    block = text[text.index("HEALTHCHECK"):]
    block = block[:block.index("\nENTRYPOINT")]
    return re.findall(r"http://[^/\s]+(/[^\s>|]*)", block)


def test_the_dockerfile_still_has_a_healthcheck():
    assert "HEALTHCHECK" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_healthcheck_names_exactly_one_route():
    paths = _healthcheck_paths()
    assert len(paths) == 1, f"expected one URL in the HEALTHCHECK, got {paths}"


def test_that_route_answers_without_credentials(client):
    """The whole bug, as one assertion. `curl` inside the container carries no
    session and never will."""
    path = _healthcheck_paths()[0]
    r = client.get(path)
    assert r.status_code == 200, (
        f"the container healthcheck calls {path}, which answers "
        f"{r.status_code} to an anonymous caller — curl -f treats that as a "
        f"failure, so the container reports unhealthy forever"
    )


def test_docs_is_still_private(client):
    """The lockdown that caused this was correct and stays. The fix is to stop
    using an authenticated route as a liveness probe, not to reopen it."""
    assert client.get("/docs").status_code == 401


def test_the_probe_is_liveness_not_readiness(client):
    """/readyz checks Postgres, Qdrant and the LLM configuration. A container
    that restarts because Qdrant is briefly unreachable turns one dependency's
    blip into an outage of everything else — so the container probe asks only
    "is this process serving HTTP"."""
    assert _healthcheck_paths()[0] != "/readyz"
