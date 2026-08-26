"""The audit trail answers "who changed what, from where".

THE DEFECT. `/api/audit` is the page the product calls its Audit log, and
`AuditRecord` had no actor field — no user, no id, no IP, no target. It
recorded model calls: mode, model, tokens, duration, repo. Nothing else.

So on production, within twenty minutes:

    a login as fightm45@gmail.com          → no row
    a GitHub personal access token saved   → no row
    three repositories registered          → no row
    three indexes run                      → no row

Every one of them appeared only in an in-memory ring buffer that holds 3000
records — about 29 minutes at that traffic — and is lost on restart. Its own
source says it is "a debugging aid, not an audit trail". The thing it points
at as the audit trail could not answer the only question an auditor asks.

Actions share the file with model calls on purpose, distinguished by
`mode="action"`, so they inherit rotation, retention, tenant scoping,
filtering and CSV export rather than growing a second half-finished copy of
each.
"""

from __future__ import annotations

import json

import pytest

from src.security.audit import ACTION_MODE, AuditRecord, record_action


@pytest.fixture()
def log(tmp_path, monkeypatch):
    from src.security import audit as mod

    logger = mod.AuditLogger(log_path=tmp_path / "audit.jsonl")
    monkeypatch.setattr(mod, "get_audit_logger", lambda: logger)

    def read() -> list[dict]:
        p = tmp_path / "audit.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    return read


def test_an_action_records_the_actor(log):
    record_action(action="connection.saved", actor="alice@example.com",
                  actor_id="u-1", workspace_id="ws-1", target="github:default",
                  ip="203.0.113.7")

    rec = log()[0]
    assert rec["actor"] == "alice@example.com"
    assert rec["actor_id"] == "u-1"
    assert rec["ip"] == "203.0.113.7"
    assert rec["target"] == "github:default"
    assert rec["operation"] == "connection.saved"
    assert rec["mode"] == ACTION_MODE


def test_an_action_is_tenant_scoped_like_every_other_record(log):
    record_action(action="repo.registered", actor="a@b.c", workspace_id="ws-9")

    assert log()[0]["workspace_id"] == "ws-9"


def test_a_model_call_record_still_has_no_actor(log):
    """The fields are optional. Every record written before they existed
    parses as None, and so does every LLM-call record."""
    rec = AuditRecord(request_id="r", timestamp="t", mode="qa", model="m")

    assert rec.actor is None
    assert rec.ip is None


def test_an_audit_write_never_breaks_the_operation(log, monkeypatch):
    """It is written on the successful path. A failure here must not undo
    what it is recording."""
    from src.security import audit as mod

    class Boom:
        def write(self, record):
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(mod, "get_audit_logger", lambda: Boom())
    record_action(action="connection.saved", actor="a@b.c")  # must not raise


def test_the_detail_carries_shape_not_secrets(log):
    """Callers pass a provider name and an account label. A token in an audit
    row would be worse than the 422 that echoed one, because this is written
    when the operation SUCCEEDS."""
    record_action(action="connection.saved", actor="a@b.c",
                  detail={"provider": "github", "username": "celmis-bot"})

    blob = json.dumps(log()[0])
    assert "github" in blob
    assert "ghp_" not in blob


@pytest.mark.parametrize("router,action", [
    ("connections", "connection.saved"),
    ("connections", "connection.deleted"),
    ("repos", "repo.registered"),
    ("auth", "auth.login"),
])
def test_the_actions_that_had_no_row_now_have_one(router: str, action: str):
    """Named individually because these are the four that were missing on the
    production trace, not a generic "something is wired" check."""
    import importlib
    import inspect

    mod = importlib.import_module(f"src.api.routers.{router}")
    src = inspect.getsource(mod)

    assert f'action="{action}"' in src


def test_the_client_ip_is_the_client_not_the_proxy():
    """The only IP recorded anywhere was 172.18.0.7 — the Docker bridge
    address of the reverse proxy — because nothing read X-Forwarded-For."""
    from types import SimpleNamespace as NS

    from src.api.deps import client_ip

    req = NS(headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
             client=NS(host="172.18.0.7"))

    assert client_ip(req) == "203.0.113.7"


def test_the_client_ip_falls_back_to_the_socket():
    from types import SimpleNamespace as NS

    from src.api.deps import client_ip

    assert client_ip(NS(headers={}, client=NS(host="198.51.100.4"))) == "198.51.100.4"
