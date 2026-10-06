"""A bearer token over http crosses the network readable: the helper refuses,
the hooks say so. A server on this machine is the one exception."""

from __future__ import annotations

import importlib.util
import json
import subprocess

import pytest

from tests.plugin import contract as C

HELPER = C.PLUGIN / "bin" / "celmis-auth-header"
_BARE = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}

_spec = importlib.util.spec_from_file_location("celmis_hook_https_check", C.HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _helper(url: str):
    env = {**_BARE, "CELMIS_TOKEN": "tok.en-123"}
    if url:
        env["CELMIS_URL"] = url
    return subprocess.run(["/bin/sh", str(HELPER)], capture_output=True, text=True,
                          env=env, timeout=10)


@pytest.mark.parametrize("url", [
    "https://celmis.example.com", "https://celmis.example.com/mcp/dev", "",
    "http://localhost:8000", "http://127.0.0.1:8000/mcp", "http://[::1]:8000",
])
def test_the_helper_sends_the_token_to_https_or_to_this_machine(url):
    done = _helper(url)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"Authorization": "Bearer tok.en-123"}


@pytest.mark.parametrize("url", [
    "http://celmis.example.com", "http://celmis.example.com:8000/mcp",
    "http://localhost.evil.example.com", "http://127.0.0.1.evil.example.com",
    "ftp://celmis.example.com", "celmis.example.com",
])
def test_the_helper_refuses_to_send_the_token_over_anything_else(url):
    done = _helper(url)
    assert done.returncode == 1 and done.stdout == ""
    assert "https" in done.stderr and "tok.en-123" not in done.stderr


@pytest.mark.parametrize("url,insecure", [
    ("https://celmis.example.com", False), ("http://localhost:8000", False),
    ("http://127.0.0.1", False), ("http://celmis.example.com", True),
    ("http://localhost.evil.example.com", True), ("ws://celmis.example.com", True),
])
def test_the_hook_tells_secure_from_insecure_addresses(url, insecure):
    assert hook.insecure_url(url) is insecure


def test_a_session_on_a_plain_http_server_is_told_not_to_use_the_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("CELMIS_URL", "http://celmis.example.com")
    monkeypatch.setenv("CELMIS_TOKEN", "tok.en-123")
    out = hook.cmd_session({"cwd": str(tmp_path)})
    assert "not https" in out and "tok.en-123" not in out
    assert "Celmis is connected" not in out


def test_the_grep_nudge_is_silent_on_a_plain_http_server(monkeypatch):
    monkeypatch.setenv("CELMIS_URL", "http://celmis.example.com")
    monkeypatch.setenv("CELMIS_TOKEN", "tok.en-123")
    assert hook.cmd_pregrep({"tool_input": {"pattern": "load_config"}}) == ""


def test_the_readme_says_the_shipped_entry_does_not_check_the_scheme():
    """The default `.mcp.json` has no scheme guard (only the helper does), and
    the README must not let a reader assume it has."""
    text = (C.PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "must be `https://`" in text and "nothing in it checks the scheme" in text
    cfg = json.loads((C.PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["celmis"]
    assert "headersHelper" not in cfg, "if the entry gains the helper, drop this warning"
