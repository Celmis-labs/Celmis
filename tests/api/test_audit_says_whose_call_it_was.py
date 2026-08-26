"""An audit log with no tenant on it can only be all-or-nothing.

"Чому не видно audit logs owner воркспейсу" — because `AuditRecord` had no
workspace field. One append-only JSONL for the whole installation, no
attribution: showing it to one workspace owner would have shown them every
other tenant's models, repository names, which files were sent and how much
they spent. The page was global-admin-only for a good reason, and the fix
was upstream, not on the gate.

These tests pin the properties that make opening it safe:

  * the record carries the tenant, and the writers stamp it;
  * "default" is NOT an attribution — it is the placeholder both LLM clients
    fall back to when nobody told them a workspace, so treating it as a
    tenant would hand untenanted calls to whoever owns that workspace;
  * a record with no tenant is visible to global admins only, and the count
    of them is reported rather than silently dropped;
  * the FACETS are scoped too. That is the one that is easy to miss: an
    unscoped facet list is a naked set of every repository name in the
    installation, handed over even while the record list itself filters
    correctly;
  * a caller with no tenant of their own matches nothing, not everything.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.api.routers import audit as router_mod
from src.security.audit import AuditLogger, AuditRecord, normalize_workspace_id

ALICE_WS = "ws-alice"
BOB_WS = "ws-bob"


@dataclass
class _User:
    """Just the two attributes the endpoints read off a User."""

    id: str
    email: str
    is_admin: bool = False


ALICE = _User(id="u-alice", email="alice@example.com")
BOB = _User(id="u-bob", email="bob@example.com")
ADMIN = _User(id="u-root", email="root@example.com", is_admin=True)


def _rec(**kw) -> dict:
    base = {
        "request_id": kw.pop("request_id", "r"),
        "timestamp": kw.pop("timestamp", "2026-08-19T10:00:00+00:00"),
        "mode": kw.pop("mode", "qa"),
        "model": kw.pop("model", "gemini-3.1-pro"),
        "operation": kw.pop("operation", "answer_streaming"),
        "input_tokens_estimated": kw.pop("input_tokens_estimated", 100),
        "output_tokens_estimated": kw.pop("output_tokens_estimated", 10),
    }
    base.update(kw)
    return base


@pytest.fixture()
def log(tmp_path: Path, monkeypatch) -> Path:
    """Three tenants' worth of history in one file, the way production has it:
    Alice's, Bob's, and one record from before the field existed."""
    path = tmp_path / "audit.jsonl"
    lines = [
        _rec(request_id="a1", workspace_id=ALICE_WS, repo="alice/frontend"),
        _rec(request_id="b1", workspace_id=BOB_WS, repo="bob/secret-trading-bot",
             input_tokens_estimated=999, error="boom"),
        # No workspace_id key at all — every record written before today.
        _rec(request_id="legacy", repo="acme/legacy", mode="batch",
             operation="generate"),
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    monkeypatch.setattr(router_mod, "_audit_files", lambda: [path])
    return path


def _list(user, ws: str) -> dict:
    return router_mod.list_audit(
        from_ts=None, to_ts=None, mode=None, operation=None, repo=None,
        limit=100, offset=0, user=user, workspace_id=ws,
    )


def _ids(payload: dict) -> set[str]:
    return {r["request_id"] for r in payload["records"]}


# ─── the record ──────────────────────────────────────────────────────


def test_the_record_carries_the_tenant(tmp_path: Path):
    log = AuditLogger(tmp_path / "audit.jsonl")
    with log.track(mode="qa", model="m", operation="ask", workspace_id=ALICE_WS):
        pass
    written = json.loads(log.log_path.read_text().splitlines()[0])
    assert written["workspace_id"] == ALICE_WS


def test_a_record_from_before_the_field_existed_is_still_valid():
    """The file is append-only history — nothing can be backfilled into it."""
    old = AuditRecord(request_id="r", timestamp="t", mode="qa", model="m",
                      operation="ask")
    assert old.workspace_id is None


def test_default_is_a_placeholder_and_never_an_attribution(tmp_path: Path):
    """Both LLM clients declare `workspace_id: str = "default"`, so "default"
    is what arrives when the caller never knew a tenant."""
    assert normalize_workspace_id("default") is None
    assert normalize_workspace_id("   ") is None
    assert normalize_workspace_id(None) is None
    assert normalize_workspace_id(123) is None
    assert normalize_workspace_id(" ws-alice ") == ALICE_WS

    log = AuditLogger(tmp_path / "audit.jsonl")
    with log.track(mode="qa", model="m", operation="ask", workspace_id="default"):
        pass
    written = json.loads(log.log_path.read_text().splitlines()[0])
    assert written["workspace_id"] is None, (
        "'default' as a tenant shows untenanted calls to whoever owns that "
        "workspace"
    )


# ─── the writers ─────────────────────────────────────────────────────


def _track_calls(obj) -> list[dict[str, str]]:
    """Every `*.track(...)` call in `obj`, as {keyword: unparsed value}.

    Read off the AST rather than grepped out of the source: a token that
    appears only in a comment must not be able to satisfy an assertion about
    the code.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    return [
        {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "track"
    ]


def test_the_llm_client_stamps_the_tenant_it_bills(tmp_path: Path):
    """One source for the audit trail and the invoice: `self._workspace_id`."""
    from src.llm.client import LLMClient

    calls = _track_calls(LLMClient.generate)
    assert len(calls) == 1
    assert calls[0].get("workspace_id") == "self._workspace_id"

    client = LLMClient(resolve_key=lambda p: "k", workspace_id=ALICE_WS,
                       audit=AuditLogger(tmp_path / "audit.jsonl"))
    assert client._workspace_id == ALICE_WS


def test_every_gemini_call_stamps_the_tenant():
    """The subagent tool loop — the one native call left after generate,
    streaming, embed and embed_batch moved to LiteLLM (which stamps the
    tenant in completion.py / client.py, asserted around this test). A
    missed stamp is a record its owner can never see."""
    from src.llm.gemini_client import GeminiClient

    calls = _track_calls(GeminiClient)
    assert len(calls) == 1, f"expected 1 track() site, found {len(calls)}"
    unstamped = [c for c in calls if c.get("workspace_id") != "self.workspace_id"]
    assert not unstamped, f"{len(unstamped)} of {len(calls)} track() calls unstamped"


def test_the_gateway_stream_stamps_the_tenant():
    """`answer_streaming` is the record a workspace owner actually asks about,
    and it is written from completion.py, not from either client."""
    from src.llm import completion

    calls = _track_calls(completion._litellm_stream)
    assert len(calls) == 1
    assert calls[0].get("workspace_id") == "workspace_id"
    assert calls[0].get("operation") == "'answer_streaming'"


# ─── the reads ───────────────────────────────────────────────────────


def test_a_workspace_owner_sees_their_own_records(log):
    assert _ids(_list(ALICE, ALICE_WS)) == {"a1"}


def test_a_workspace_owner_never_sees_another_tenants_records(log):
    assert "b1" not in _ids(_list(ALICE, ALICE_WS))
    assert _ids(_list(BOB, BOB_WS)) == {"b1"}


def test_an_untenanted_record_is_global_admin_only(log):
    assert "legacy" not in _ids(_list(ALICE, ALICE_WS))
    assert "legacy" in _ids(_list(ADMIN, ALICE_WS))


def test_a_global_admin_still_sees_the_installation(log):
    assert _ids(_list(ADMIN, ALICE_WS)) == {"a1", "b1", "legacy"}


def test_a_caller_with_no_tenant_matches_nothing_not_everything(log):
    """`current_workspace_id` can resolve to the 'default' placeholder. If
    that degraded into "no filter" the fix would be worse than the bug."""
    assert _list(ALICE, "default")["records"] == []
    assert _list(ALICE, "  ")["records"] == []


def test_the_file_list_is_installation_storage(log):
    assert "files" not in _list(ALICE, ALICE_WS)
    assert "files" in _list(ADMIN, ALICE_WS)


# ─── the facets — the easy one to miss ───────────────────────────────


def test_facets_do_not_leak_other_tenants_repository_names(log):
    facets = router_mod.audit_facets(user=ALICE, workspace_id=ALICE_WS)
    assert facets["repos"] == ["alice/frontend"]
    assert "bob/secret-trading-bot" not in facets["repos"], (
        "the dropdown hands over every repository name in the installation "
        "even when the records themselves are filtered"
    )
    assert "acme/legacy" not in facets["repos"]


def test_facets_are_whole_for_a_global_admin(log):
    facets = router_mod.audit_facets(user=ADMIN, workspace_id=ALICE_WS)
    assert set(facets["repos"]) == {"alice/frontend", "bob/secret-trading-bot",
                                    "acme/legacy"}


# ─── the aggregates ──────────────────────────────────────────────────


def _stats(user, ws: str) -> dict:
    return router_mod.audit_stats(
        from_ts=None, to_ts=None, mode=None, operation=None, repo=None,
        user=user, workspace_id=ws,
    )


def test_stats_aggregate_only_the_callers_own_calls(log):
    alice = _stats(ALICE, ALICE_WS)
    assert alice["total_calls"] == 1
    assert alice["input_tokens"] == 100, "999 is Bob's spend"
    assert alice["errors"] == 0, "the failing call is Bob's"


def test_stats_admit_what_they_are_not_showing(log):
    """A short total presented as the whole truth is its own bug."""
    assert _stats(ALICE, ALICE_WS)["hidden_unattributed"] == 1
    assert _stats(ADMIN, ALICE_WS)["hidden_unattributed"] == 0, (
        "nothing is hidden from a global admin"
    )


def test_stats_do_not_count_another_tenants_records_as_hidden(log):
    """`hidden_unattributed` must not become a row-count oracle: how many
    calls Bob made is a fact about Bob."""
    assert _stats(ALICE, ALICE_WS)["hidden_unattributed"] == 1  # not 2


def test_stats_read_the_field_the_writer_actually_writes(log):
    """The record field is `input_tokens_estimated`; reading the bare name
    made every token tile on the page read 0."""
    assert _stats(ADMIN, ALICE_WS)["input_tokens"] == 100 + 999 + 100


# ─── the export ──────────────────────────────────────────────────────


async def _drain(resp) -> str:
    """StreamingResponse wraps a sync iterator in a threadpool async one."""
    parts = [c.decode() if isinstance(c, bytes) else c
             async for c in resp.body_iterator]
    return "".join(parts)


def test_the_export_is_scoped_like_the_list(log):
    resp = router_mod.export_audit(
        from_ts=None, to_ts=None, mode=None, operation=None, repo=None,
        user=ALICE, workspace_id=ALICE_WS,
    )
    body = asyncio.run(_drain(resp))
    assert "alice/frontend" in body
    assert "bob/secret-trading-bot" not in body, (
        "a filter that only holds on screen is not a filter"
    )
    assert "acme/legacy" not in body


# ─── what stays installation-level ───────────────────────────────────


def test_retention_and_rotation_have_no_route_at_all():
    """Purge deletes files holding every tenant's records; one workspace
    owner must not be able to erase another's history."""
    from starlette.routing import Route

    for route in router_mod.router.routes:
        assert isinstance(route, Route)
        assert route.methods <= {"GET", "HEAD"}, (
            f"{route.path} mutates shared audit storage over HTTP"
        )
        assert route.endpoint is not router_mod.purge_expired_audit
