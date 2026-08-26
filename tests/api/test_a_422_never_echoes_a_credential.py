"""A validation error does not hand the credential back.

THE DEFECT, observed on production while testing `PUT /api/connections/github`.
The request omitted `provider`; FastAPI's default handler answered:

    {"detail":[{"type":"missing","loc":["body","provider"],
                "msg":"Field required",
                "input":{"token":"ghp_…"}}]}

For a MISSING field the echoed `input` is the whole request body, so the GitHub
personal access token came straight back — into the response, and from there
into every access log, reverse-proxy log and browser devtools panel that saw
it. The token had to be treated as compromised.

Nothing else about the 422 changes. `loc`, `msg` and `type` are what make it
useful to a client and none of them carries the value.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

SECRET = "ghp_thisIsNotARealTokenAtAll0123456789"


@pytest.fixture(scope="module")
def client() -> TestClient:
    from src.api.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_a_missing_field_does_not_return_the_token(client):
    r = client.put("/api/connections/github", json={"token": SECRET})

    assert r.status_code in (401, 403, 422)
    assert SECRET not in r.text


def test_the_error_is_still_useful(client):
    """Redaction that also destroys `loc` would trade one defect for another —
    a client could no longer tell which field it got wrong."""
    r = client.put("/api/connections/github", json={"token": SECRET})
    if r.status_code != 422:
        pytest.skip("endpoint refused before validation; covered elsewhere")

    detail = r.json()["detail"]
    assert any(e.get("loc") for e in detail)
    assert any(e.get("msg") for e in detail)
    assert json.dumps(detail).count("[redacted]") >= 1


def test_a_password_is_redacted_too(client):
    r = client.post("/api/auth/login", json={"password": "hunter2-not-real"})

    assert "hunter2-not-real" not in r.text


@pytest.mark.parametrize("field", [
    "token", "access_token", "refresh_token", "password", "client_secret",
    "api_key", "apiKey", "private_key", "passphrase", "credential",
])
def test_every_secret_shaped_name_is_covered(field: str):
    """Matched on substring and case-insensitively, so a new field called
    `webhook_secret` or `gitlab_token` is covered the day it is added rather
    than the day somebody remembers to add it here."""
    from src.api.main import _redact

    out = _redact({field: SECRET}, key=None)

    assert out[field] == "[redacted]"


def test_a_secret_nested_in_a_list_is_redacted():
    from src.api.main import _redact

    out = _redact({"connections": [{"token": SECRET}, {"token": SECRET}]})

    assert SECRET not in json.dumps(out)


def test_an_ordinary_value_is_left_alone():
    """Over-redaction hides the information a 422 exists to give."""
    from src.api.main import _redact

    out = _redact({"provider": "github", "account_label": "default"})

    assert out == {"provider": "github", "account_label": "default"}
