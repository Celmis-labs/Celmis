"""The committed-secret shapes the generic redactor misses are masked in every tool,
and the smaller review fixes: cut order, cursor size, metadata outline, missing revision.

Secret-looking values come from conftest fragments, never literals in this file.
"""

from __future__ import annotations

import pytest

from src.mcp_server.dev_profile import (
    cursor,
    emit,
    literals,
    paths,
    tools_grep,
    tools_read,
    tools_refs,
    tools_repos,
)
from src.mcp_server.dev_profile import freshness as freshness_mod
from tests.mcp_server.dev.conftest import BILLING, LONG_SECRET, SHAPE_PW, SHOP

PW = SHAPE_PW


# ─── the shapes ──────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    f"DB_PASSWORD={PW}",
    f"password: {PW}",
    f"POSTGRES_PASSWORD: {PW}",
    f"psycopg2.connect(host='h', user='u', password='{PW}')",
    f"redis://:{PW}@cache:6379/0",
    f"amqp://guest:{PW}@mq/vhost",
    f'  secret_key: "{PW}Aa"',
    f'{{"password": "{PW}"}}',
    f"payload = '{{\"api_key\": \"{PW}\"}}'",
    f'password: str = "{PW}"',
    f"export API_TOKEN={PW}",
    f"- DB_PASSWORD={PW}",
    f"SECRET_KEY = os.environ.get('SECRET_KEY', '{PW}')",
    f"const k = process.env.DB_PASSWORD || '{PW}'",
    f"DB_PASSWORD=${{DB_PASSWORD:-{PW}}}",
    f"client_secret = '{PW}",  # a literal cut short: no closing quote
])
def test_a_committed_secret_is_masked_whatever_shape_it_has(line):
    assert PW not in emit.redact_text(line)


@pytest.mark.parametrize("line", [
    "DB_PASSWORD=${DB_PASSWORD}",
    "password = settings.db_password",
    "connect(password=cfg.pw, user=u)",
    "password: ${OK_PASSWORD}",
    "password_file: /run/secrets/db_password",
    "max_tokens: 4096",
    "token = get_token()",
    "DB_PASSWORD=changeme",
    "api_key: <your key>",
    "password: str",
    "SECRET_KEY = os.environ['SECRET_KEY']",
])
def test_a_pointer_or_a_placeholder_is_left_alone_so_the_pattern_stays_readable(line):
    assert emit.redact_text(line) == line


def test_a_bare_word_after_a_password_key_is_masked_because_the_text_has_no_file_type():
    # The central layer treats a bare identifier as a variable only for a known
    # code file; dev output mixes files, so it errs on the side of masking.
    assert "db_password" not in emit.redact_text("password=db_password")


def test_the_name_and_the_quotes_survive_so_the_code_still_reads():
    out = emit.redact_text(f"psycopg2.connect(host='h', password='{PW}')")
    assert out == f"psycopg2.connect(host='h', password='{literals.MASK}')"


def test_redaction_is_idempotent():
    once = emit.redact_text(f"DB_PASSWORD={PW}")
    assert emit.redact_text(once) == once


# ─── in every tool ───────────────────────────────────────────────────

def test_grep_never_prints_a_committed_secret_shape(as_caller, world):
    as_caller({SHOP: "code", BILLING: "code"})
    for pattern in ("PASSWORD", "password", "secret_key", "redis://", "api_key"):
        out = tools_grep.run(pattern, repo=SHOP)
        assert PW not in out, pattern
    out = tools_grep.run("PASSWORD", repo=SHOP)
    assert "deploy/compose.yml" in out and "[REDACTED:" in out


def test_read_symbol_never_prints_a_committed_secret_shape(as_caller, world):
    as_caller({SHOP: "code"})
    out = tools_read.read_symbol(SHOP, "connect", max_lines=40)
    assert "psycopg2.connect" in out and PW not in out


def test_a_text_ref_in_a_sibling_repo_is_masked(as_caller, world, monkeypatch):
    monkeypatch.setattr(tools_refs, "_siblings", lambda scope, slug: [SHOP])
    as_caller({SHOP: "code", BILLING: "code"})
    out = tools_refs.run(BILLING, "create_order_remote")
    assert "text-ref" in out and "app/db.py" in out
    assert PW not in out


# ─── redact first, cut second ────────────────────────────────────────

def test_a_secret_straddling_the_line_cut_is_masked_not_halved(as_caller, world):
    as_caller({SHOP: "code"})
    out = tools_grep.run("SECRET_KEY", repo=SHOP)
    assert "app/long_line.py" in out
    assert LONG_SECRET not in out
    assert LONG_SECRET[:12] not in out and "django-insecure" not in out


def test_clip_redacts_before_it_cuts():
    line = "a" * 150 + f" SECRET_KEY = '{LONG_SECRET}'"
    out = emit.clip(line, 160)
    assert "django" not in out and len(out) <= 160


def test_clip_withholds_when_the_hook_raises(monkeypatch):
    def boom(_text):
        raise RuntimeError("no")

    monkeypatch.setattr(emit, "_redact", boom)
    assert emit.clip("anything", 50) == "[withheld]"


# ─── the cursor is small and survives unrelated pushes ───────────────

def test_a_cursor_stays_short_over_a_whole_fleet():
    pin = {f"github_acme-svc{i}": f"{i:040x}" for i in range(65)}
    cur = cursor.encode(cursor.fingerprint(tool="find", q="x"), 40, pin)
    assert len(cur) <= 40


def test_a_cursor_resolves_only_against_the_same_pin():
    h = cursor.fingerprint(tool="find", q="x")
    pin = {"a": "1" * 40, "b": "2" * 40}
    cur = cursor.encode(h, 7, pin)
    assert cursor.resolve(cur, h, dict(reversed(list(pin.items())))) == (7, "")
    assert cursor.resolve(cur, h, {**pin, "b": "3" * 40}) == (0, cursor.STALE_NOTE)
    assert cursor.resolve(cur, "other", pin) == (0, cursor.MISMATCH_NOTE)
    assert cursor.resolve("not-a-cursor", h, pin) == (0, cursor.MISMATCH_NOTE)


def test_page_two_survives_a_push_to_a_repo_that_had_no_hits(as_caller, world):
    as_caller({SHOP: "code", BILLING: "code"})
    first = tools_grep.run("def ", repo="", limit=3)
    cur = next((part.split("cursor=")[1].split()[0] for part in first.split("\n")
                if "cursor=" in part), "")
    assert cur and len(cur) <= 40
    second = tools_grep.run("def ", repo="", limit=3, cursor_=cur)
    assert "cursor stale" not in second


# ─── outline, paths, revision, repos ─────────────────────────────────

def test_outline_of_a_metadata_only_repo_prints_names_not_signature_text(as_caller, world):
    as_caller({SHOP: "metadata"})
    out = tools_read.outline(SHOP, "app/db.py")
    assert "connect" in out
    assert "password" not in out and PW not in out


@pytest.mark.parametrize("path", [
    ".env", ".env.production", "./.env", ".npmrc", ".netrc", "config/.env",
    "app/.env.local", "server.key", "././.env",
])
def test_root_level_dotfiles_are_secret_paths(path):
    assert paths.is_secret_path(path)


def test_an_ordinary_dotfile_is_not_a_secret_path():
    assert not paths.is_secret_path(".gitignore")
    assert not paths.is_secret_path("src/.eslintrc.json")


def test_grep_says_so_when_the_indexed_revision_is_not_in_the_clone(as_caller, world, freshness):
    as_caller({SHOP: "code"})
    freshness[SHOP]["sha"] = "f" * 40
    out = tools_grep.run("create_order", repo=SHOP)
    assert "not in the clone" in out and "re-index" in out
    assert "no committed text matches" not in out
    assert out.splitlines()[0].startswith("idx: ") and "ffffffff" in out.splitlines()[0]


def test_a_repos_page_is_never_longer_than_the_idx_line_can_describe():
    assert tools_repos.DEFAULT_LIMIT <= freshness_mod.MAX_ENTRIES
