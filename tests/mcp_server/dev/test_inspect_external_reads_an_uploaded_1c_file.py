"""A 1C file that is in no repository is uploaded once and then read like one.

`inspect_external()` hands out a one-time upload link; with the handle it
describes the file, with `repo=` it compares the file with that configuration
and runs the static checks; `outline` / `read_symbol` / `grep` read its code
under `repo="ext/<handle>"`. Every answer says the file is external. Only the
uploader reads it. The configuration here is the synthetic `Акме` dump committed
to a git clone, whose ordinary form keeps its module inside `Form.bin` — read
by `git_io` under the indexed path `<...>/Form.bin/module`.
"""

from __future__ import annotations

import asyncio
import re
import subprocess

import pytest

from src.mcp_server.dev_contract import HIT_RE, IDX_ENTRY_RE
from src.mcp_server.dev_profile import access, git_io, tools_external
from src.onec import store
from src.onec.report import EXTERNAL_MARK, STATIC_MARK
from tests.mcp_server.dev.conftest import SHOP
from tests.onec import acme

FORM_BIN = f"Reports/{acme.REPORT}/Forms/ФормаОтчета/Ext/Form.bin"


def _git(path, *args: str) -> str:  # noqa: ANN001
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def acme_repo(tmp_path, monkeypatch, as_caller, freshness):
    """SHOP is the `Акме` configuration in a git clone; uploads go to tmp."""
    root = acme.write_dump(tmp_path / "acme")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    sha = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(git_io, "clone_path", lambda slug: root)
    monkeypatch.setattr(git_io, "_unpacked_at", lambda slug, s: None)
    monkeypatch.setattr(store, "_root", lambda: tmp_path / "onec-external")
    tools_external._CACHE.clear()
    as_caller({SHOP: "code"})
    freshness[SHOP]["sha"] = sha
    return sha


@pytest.fixture
def uploaded(acme_repo) -> store.Stored:
    ticket = store.issue_ticket("u-1", "ws-1")  # the caller `as_caller` sets
    return store.redeem(ticket, acme.report_file(), f"{acme.REPORT}.erf")


def test_without_a_file_the_answer_is_an_upload_link(acme_repo, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "public_base_url", "https://celmis.example")
    out = tools_external.run()
    m = re.search(r"https://celmis\.example/backend/api/onec/external/(\S+)", out)
    assert m and 'curl -sS -F "file=@' in out
    saved = store.redeem(m.group(1), acme.report_file(), "r.erf")
    assert store.get(saved.handle, "u-1", "ws-1") is not None


def test_without_a_public_address_it_points_at_an_agent_session(acme_repo, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "public_base_url", "")
    out = tools_external.run()
    assert "curl" not in out and "agent session" in out


def test_the_file_is_described_and_marked_external(uploaded):
    out = tools_external.run(file=uploaded.handle)
    entry = out.splitlines()[0]
    assert f"ext/{uploaded.handle} external@{uploaded.sha256[:8]}" in entry
    assert re.search(IDX_ENTRY_RE, entry.removeprefix("idx: ").split(" | ")[0])
    assert EXTERNAL_MARK in out and STATIC_MARK in out
    assert f"object: Reports.{acme.REPORT}" in out
    assert "template Макет: 2 area(s), 2 parameter(s)" in out
    assert 'pass repo="<configuration repository>"' in out


def test_with_a_repository_it_is_compared_and_checked(uploaded):
    out = tools_external.run(file=uploaded.handle, repo=SHOP)
    assert "verdict: older than the configuration" in out
    assert "ACME-205 2025-05-12" in out
    assert "ФорматСуммы: passes 3 argument(s)" in out
    assert "the enum has no such value" in out
    assert "form ФормаОтчета: handler ПриЗакрытии" in out
    assert "not checked yet: managed forms" in out


def test_another_caller_cannot_read_it(uploaded, monkeypatch):
    monkeypatch.setattr(access, "mcp_scope", lambda candidates=None: access.DevScope(
        decisions={}, user_id="u-2", workspace_id="ws-1"))
    for out in (tools_external.run(file=uploaded.handle),
                tools_external.outline(f"ext/{uploaded.handle}", "")):
        assert "no such upload of yours" in out
        assert acme.REPORT not in out


def test_its_code_is_read_by_path_and_line(uploaded):
    repo = f"ext/{uploaded.handle}"
    obj = f"Reports/{acme.REPORT}/Ext/ObjectModule.bsl"

    listing = tools_external.outline(repo, "")
    assert EXTERNAL_MARK in listing and f"{obj} " in listing

    procs = tools_external.outline(repo, obj)
    assert "7-34 procedure Проверки()" in procs
    assert "36-37 procedure Местная(Один)" in procs

    body = tools_external.read_symbol(repo, "Местная")
    assert f"{repo} {obj}:36-37" in body and "Процедура Местная(Один)" in body

    hits = tools_external.grep("ФорматСуммы", repo)
    lines = [ln for ln in hits.splitlines() if ln.startswith(repo + " ")]
    assert [re.match(HIT_RE, ln).group("start") for ln in lines] == ["4", "8"]

    paged = tools_external.grep("ФорматСуммы", repo, limit=1)
    assert "cursor=1" in paged
    assert ":8 " in tools_external.grep("ФорматСуммы", repo, limit=1, cursor="1")


def test_the_server_routes_ext_repositories_to_the_upload(uploaded, monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    from src.mcp_server import dev_profile

    mcp = dev_profile.build_dev_mcp()
    res = asyncio.run(mcp.call_tool("outline", {"repo": f"ext/{uploaded.handle}", "path": ""}))
    text = "".join(getattr(c, "text", "") for c in (res[0] if isinstance(res, tuple) else res))
    assert EXTERNAL_MARK in text and "ObjectModule.bsl" in text


def test_a_form_bin_module_is_read_under_its_indexed_path(acme_repo):
    member = f"{FORM_BIN}/module"
    lines = git_io.show_file(SHOP, acme_repo, member)
    assert lines is not None and "Процедура ПриЗакрытии()" in lines
    at = lines.index("Процедура ПриЗакрытии()") + 1
    span = git_io.show_span(SHOP, acme_repo, member, at, at + 1)
    assert span.problem == "" and span.lines == ["Процедура ПриЗакрытии()", "КонецПроцедуры"]
    hits = git_io.grep(SHOP, acme_repo, "ПриЗакрытии")
    assert any(h.path == member and h.line == at for h in hits), [(h.path, h.line) for h in hits]
