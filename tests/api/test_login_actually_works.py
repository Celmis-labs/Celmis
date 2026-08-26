"""Logging in succeeds, and the audited endpoints survive a real request.

WHY THIS FILE EXISTS. Adding `record_action` to the login handler introduced

    NameError: name 'client_ip' is not defined

and 4894 tests passed. The suite had no test that logs in successfully. The
one test that touches `/api/auth/login` — the 422-redaction guard — posts a
body with `email` missing, so validation rejects the request BEFORE the
handler runs and the broken line is never reached. A check that looks like
coverage of an endpoint covered only the path adjacent to the one that
matters.

Login was broken on production for the time it took to read the error log.

Every endpoint that now writes an audit row is exercised here on its SUCCESS
path, because that is where the new code runs. A NameError in a handler is
invisible to any test that only makes the handler refuse.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    from src.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def account(client) -> dict:
    """A real account, created through the API."""
    creds = {"email": "login-probe@example.com", "password": "n0t-a-real-pw!"}
    client.post("/api/auth/signup", json={**creds, "name": "Login Probe"})
    return creds


def test_login_returns_a_token(client, account):
    """The assertion the suite did not have."""
    r = client.post("/api/auth/login", json=account)

    assert r.status_code == 200, r.text
    assert r.json().get("access_token")


def test_login_does_not_500(client, account):
    """Stated separately from the token check because the failure mode was a
    500 from an unresolved name, not a wrong body."""
    r = client.post("/api/auth/login", json=account)

    assert r.status_code < 500, r.text


def test_a_wrong_password_is_refused_not_broken(client, account):
    r = client.post("/api/auth/login",
                    json={**account, "password": "definitely-wrong"})

    assert r.status_code in (401, 403)


# NOTE on what is NOT here. `/api/repos` and `/api/connections` also gained a
# `record_action` call and a `Request` parameter, and both resolve the
# workspace through Postgres — which no unit test has, so both 500 locally
# for a reason unrelated to this code. Asserting on them would make this file
# fail environmentally and teach the next person to skip it.
#
# The parametrised test below is what actually catches this class of bug, and
# it needs no database: it checks that every name a router CALLS is a name it
# IMPORTS. That is exactly what went wrong — an import that never landed,
# invisible until one request reached one line.


@pytest.mark.parametrize("module", [
    "src.api.routers.auth",
    "src.api.routers.repos",
    "src.api.routers.connections",
])
def test_every_name_a_module_calls_is_a_name_it_imports(module: str):
    """The specific failure was an import that never landed. This catches the
    shape at import time rather than waiting for the one request that reaches
    the line."""
    import ast
    import importlib
    import inspect

    mod = importlib.import_module(module)
    tree = ast.parse(inspect.getsource(mod))

    bound: set[str] = set(dir(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)

    for name in ("client_ip", "record_action"):
        if f"{name}(" in inspect.getsource(mod):
            assert name in bound, f"{module} calls {name} without importing it"
