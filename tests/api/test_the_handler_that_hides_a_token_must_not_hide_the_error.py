"""The 422 redaction handler answered 500 for every model-level rule.

It exists for a real leak: FastAPI echoes the offending input back in a
validation error, and a `POST /api/connections` missing one field returned the
personal access token inside `"input"`. Observed on production; the token had
to be treated as compromised. So the handler redacts `input` and `ctx` before
the 422 goes out.

`ctx` is where it broke. A field-level rule puts numbers there —
`{"min_length": 3}` — which JSON encodes fine. A `model_validator` that raises
ValueError puts the LIVE EXCEPTION there: `{"error": ValueError(...)}`.
`JSONResponse` cannot encode that, so the handler raised inside itself and
Starlette answered a bare `500 Internal Server Error` with no body.

Every field-level error kept working, which is why nobody saw it. It surfaced
on prod as `POST /api/agent-sessions` without a repository: the model's own
rule says "Name a repository or a project", and the caller got a 500 instead —
unable to tell "I sent the wrong thing" from "the server fell over".

Pinned here at both levels, because only one of them was ever broken and a fix
that quietly loses the other is not a fix.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, model_validator

from src.api.main import _install_validation_redaction


class _Body(BaseModel):
    """Two rules of the two kinds, and a field that looks like a secret."""

    token: str = Field(default="", max_length=200)
    name: str = Field(default="", min_length=0, max_length=50)
    size: int = Field(default=1, ge=3)

    @model_validator(mode="after")
    def _needs_one_of_them(self) -> _Body:
        if not self.token and not self.name:
            raise ValueError("Name a token or a name.")
        return self


@pytest.fixture
def client():
    app = FastAPI()
    _install_validation_redaction(app)

    @app.post("/probe")
    async def _probe(payload: _Body):  # pragma: no cover - never reached here
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_a_model_level_rule_answers_422_not_500(client):
    """The whole defect. `ctx` carries a ValueError object; the handler has to
    survive meeting one."""
    r = client.post("/probe", json={"size": 5})

    assert r.status_code == 422, "a bare 500 tells the caller nothing"


def test_the_model_rule_s_own_sentence_survives(client):
    """A 422 whose message is gone is only marginally better than a 500."""
    r = client.post("/probe", json={"size": 5})

    assert "Name a token or a name." in r.text


def test_a_field_level_rule_still_answers_422(client):
    """The half that always worked, kept working."""
    r = client.post("/probe", json={"name": "x", "size": 1})

    assert r.status_code == 422
    body = r.json()["detail"][0]
    assert body["loc"] == ["body", "size"]
    assert body["ctx"]["ge"] == 3, "the field rule's own context is intact"


def test_the_token_is_still_redacted(client):
    """The leak this handler exists for. A regression here is worse than the
    500 it just stopped answering."""
    secret = "ghp_" + "z" * 36
    r = client.post("/probe", json={"token": secret, "size": 1})

    assert r.status_code == 422
    assert secret not in r.text


def test_every_error_is_json_encodable(client):
    """The general form, not the one instance: whatever pydantic puts in `ctx`
    has to come out of this handler as something a JSON encoder accepts."""
    import json

    r = client.post("/probe", json={"size": 5})
    json.dumps(r.json())  # raises if anything survived un-encoded
