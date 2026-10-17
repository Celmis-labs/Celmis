"""`celmis-pack verify` asks the server with the token your agent connects with.

The contract (bin/celmis-pack, bin/celmis-auth-header, the plugin README):

  * the helper that gives Claude Code its header reads the keychain entry
    `celmis-token` first and `CELMIS_TOKEN` only when the keychain has none;
  * `celmis-pack` looks for your own token in exactly that order, so `verify`
    answers for the token the agent really uses (a stale keychain entry is not
    quietly bypassed by the variable, and the other way round);
  * the tool's own documentation and the README say that same order.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re

import pytest

from tests.plugin import contract as C

CLI = C.PLUGIN / "bin" / "celmis-pack"
HELPER = C.PLUGIN / "bin" / "celmis-auth-header"


def load_cli():
    loader = importlib.machinery.SourceFileLoader("celmis_pack_token_order", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_cli()


@pytest.fixture
def keychain(monkeypatch):
    """A fake keychain: `entries[service]` is what the lookup finds."""
    entries: dict[str, str] = {}
    monkeypatch.setattr(cli, "_keychain", lambda service: entries.get(service, ""))
    monkeypatch.delenv("CELMIS_TOKEN", raising=False)
    return entries


def test_the_keychain_entry_wins_over_the_variable_as_it_does_for_the_helper(keychain, monkeypatch):
    keychain["celmis-token"] = "from.the.keychain-1"
    monkeypatch.setenv("CELMIS_TOKEN", "from.the.variable-2")
    assert cli.consumer_token() == "from.the.keychain-1"


def test_the_variable_is_used_when_the_keychain_has_nothing(keychain, monkeypatch):
    monkeypatch.setenv("CELMIS_TOKEN", " from.the.variable-2 \n")
    assert cli.consumer_token() == "from.the.variable-2"


def test_with_neither_the_tool_says_where_a_token_may_be_kept(keychain):
    with pytest.raises(cli.Stop) as caught:
        cli.consumer_token()
    assert caught.value.code == cli.EXIT_USAGE
    assert "celmis-token" in caught.value.message and "CELMIS_TOKEN" in caught.value.message


def test_the_helper_reads_the_keychain_before_the_variable():
    text = HELPER.read_text(encoding="utf-8")
    keychain_at = text.index("find-generic-password -s celmis-token")
    variable_at = text.index('token="${CELMIS_TOKEN:-}"')
    assert keychain_at < variable_at
    assert re.search(r'\[ -n "\$token" \] \|\| token="\$\{CELMIS_TOKEN:-\}"', text)


def test_the_tools_documentation_and_the_readme_state_the_same_order():
    docstring = CLI.read_text(encoding="utf-8").split('"""')[1]
    block = docstring[docstring.index("CELMIS_TOKEN  "):]
    block = " ".join(block.split())
    assert 'the keychain entry "celmis-token" first, else CELMIS_TOKEN' in block
    assert "celmis-auth-header" in block
    readme = " ".join((C.PLUGIN / "README.md").read_text(encoding="utf-8").split())
    assert "the keychain entry `celmis-token`, else `CELMIS_TOKEN`" in readme
    assert "the order of `celmis-auth-header`" in readme
