"""What a caller may not read never shows up, and what is printed is redacted."""

from __future__ import annotations

import pytest

from src.mcp_server.dev_profile import (
    emit,
    tools_find,
    tools_grep,
    tools_read,
    tools_refs,
    tools_repos,
)
from src.mcp_server.dev_profile.access import NOT_ACCESSIBLE
from tests.mcp_server.dev.conftest import BILLING, CANARY_PW, CANARY_TOKEN, SHOP

ALL_TOOL_OUTPUTS = (
    lambda: tools_repos.run(),
    lambda: tools_find.run("create_order"),
    lambda: tools_find.run("invoice"),
    lambda: tools_grep.run("create_"),
    lambda: tools_grep.run("def ", regex=False),
    lambda: tools_refs.run(SHOP, "create_order"),
)


# ─── a repo with no access at all ────────────────────────────────────

def test_a_repo_with_no_rule_is_absent_from_every_listing_and_search(as_caller, world):
    as_caller({SHOP: "code", BILLING: "none"})
    for call in ALL_TOOL_OUTPUTS:
        out = call()
        assert BILLING not in out and "src/invoices.py" not in out and "src/client.py" not in out


def test_naming_a_repo_without_access_answers_exactly_like_naming_one_that_does_not_exist(as_caller, world):
    as_caller({SHOP: "code", BILLING: "none"})
    denied = tools_find.run("x", repo=BILLING)
    missing = tools_find.run("x", repo="github_acme-nonexistent")
    assert denied.replace(BILLING, "R") == missing.replace("github_acme-nonexistent", "R")
    assert NOT_ACCESSIBLE in denied


def test_read_symbol_on_a_repo_without_access_looks_like_a_missing_repo(as_caller, world):
    as_caller({SHOP: "code", BILLING: "none"})
    a = tools_read.read_symbol(BILLING, "create_invoice")
    b = tools_read.read_symbol("github_acme-nonexistent", "create_invoice")
    assert a.replace(BILLING, "R") == b.replace("github_acme-nonexistent", "R")


def test_outline_and_map_on_a_repo_without_access_say_not_accessible(as_caller, world):
    as_caller({SHOP: "code", BILLING: "none"})
    assert NOT_ACCESSIBLE in tools_read.outline(BILLING, "src/invoices.py")
    assert NOT_ACCESSIBLE in tools_read.repo_map(BILLING)


# ─── metadata-only access ────────────────────────────────────────────

def test_metadata_access_lists_symbol_locations_but_never_a_body_or_a_grep_line(as_caller, world):
    as_caller({SHOP: "metadata", BILLING: "metadata"})
    assert "create_order" in tools_find.run("create_order")
    out = tools_read.read_symbol(SHOP, "create_order")
    assert "total = sum" not in out and "OrderService().create_order" not in out
    grep = tools_grep.run("RETRY_LIMIT")
    assert "config/settings.yaml" not in grep and "README.md" not in grep


def test_a_metadata_find_does_not_print_signature_text_from_the_source(as_caller, world):
    as_caller({SHOP: "metadata"})
    out = tools_find.run("create_order", repo=SHOP, response_format="detailed")
    assert "def create_order(" not in out or "(cart, user)" not in out


# ─── path rules ──────────────────────────────────────────────────────

def test_a_denied_path_pattern_hides_its_symbols_hits_and_outline(as_caller, world):
    as_caller({SHOP: "code"}, deny={SHOP: ("vendor/**", "tests/**")})
    out = tools_find.run("create_order", repo=SHOP)
    assert "vendor/lib.py" not in out and "tests/test_orders.py" not in out
    assert "vendor/" not in tools_read.repo_map(SHOP)
    assert "vendor/lib.py" not in tools_grep.run("def create_order", repo=SHOP)
    denied = tools_read.outline(SHOP, "vendor/lib.py")
    missing = tools_read.outline(SHOP, "vendor/nothing_here.py")
    assert denied.replace("lib.py", "X") == missing.replace("nothing_here.py", "X")


def test_refs_do_not_name_a_caller_that_lives_in_a_denied_path(as_caller, world):
    as_caller({SHOP: "code"}, deny={SHOP: ("tests/**",)})
    out = tools_refs.run(SHOP, "create_order")
    assert "post_order" in out and "test_create_order" not in out


# ─── secrets never appear ────────────────────────────────────────────

@pytest.mark.parametrize("needle", ["DB_PASSWORD", "API_TOKEN", "BEGIN RSA", "MIIBOgIBAAJBAK"])
def test_grep_never_searches_env_and_key_files(as_caller, world, needle):
    out = tools_grep.run(needle, repo=SHOP)
    assert ".env" not in out and "server.key" not in out
    assert CANARY_PW not in out and CANARY_TOKEN not in out


def test_grep_for_a_secret_value_finds_nothing_even_though_the_source_holds_it(as_caller, world):
    out = tools_grep.run("RETRY_LIMIT=9", repo=SHOP)
    assert ".env" not in out


def test_the_canary_password_in_a_source_file_is_redacted_in_every_tool(as_caller, world):
    outs = [
        tools_read.read_symbol(SHOP, "OrderService"),
        tools_read.read_symbol(SHOP, "DATABASE_URL"),
        tools_grep.run("DATABASE_URL", repo=SHOP),
        tools_read.outline(SHOP, "app/services/orders.py", response_format="detailed"),
        tools_find.run("DATABASE_URL", response_format="detailed"),
    ]
    for out in outs:
        assert CANARY_PW not in out, out
        assert CANARY_TOKEN not in out


def test_the_secret_directory_is_neither_outlined_nor_mapped_nor_found(as_caller, world):
    assert "secrets/" not in tools_read.repo_map(SHOP)
    assert "secrets/loader.py" not in tools_find.run("load_secret")
    denied = tools_read.outline(SHOP, "secrets/loader.py")
    assert denied.replace("loader.py", "X") == tools_read.outline(
        SHOP, "secrets/zzz.py").replace("zzz.py", "X")


def test_a_path_that_escapes_the_repository_is_refused(as_caller, world):
    for bad in ("../../etc/passwd", "/etc/passwd", "app/../../x"):
        out = tools_read.outline(SHOP, bad)
        assert "root:" not in out


# ─── the redaction hook ──────────────────────────────────────────────

def test_every_line_of_text_is_passed_through_the_single_redaction_hook(monkeypatch, as_caller, world):
    seen: list[str] = []
    monkeypatch.setattr(emit, "_redact", lambda t: (seen.append(t), t.replace("create_order", "[X]"))[1])
    out = tools_find.run("create_order", repo=SHOP)
    assert seen and "[X]" in out and "create_order" not in out.split("\n", 1)[1]


def test_when_the_hook_raises_the_body_is_withheld_not_printed(monkeypatch, as_caller, world):
    def boom(_t: str) -> str:
        raise RuntimeError("redactor down")

    monkeypatch.setattr(emit, "_redact", boom)
    out = tools_find.run("create_order", repo=SHOP)
    assert emit.WITHHELD in out and "create_order" not in out
    assert out.startswith("idx: ")


def test_the_default_hook_masks_a_connection_string_password():
    masked = emit._redact(f'DATABASE_URL = "postgresql://app:{CANARY_PW}@db.internal/shop"')
    assert CANARY_PW not in masked
