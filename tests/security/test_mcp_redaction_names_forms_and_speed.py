"""Names that carry secrets, connection forms the first rule set missed, and hostile input.

Canaries are generated at runtime so no scanner fires on this file.
"""

from __future__ import annotations

import secrets
import string
import time

import pytest

from src.security.mcp_redact import redact_for_mcp, redact_structure

ALNUM = string.ascii_letters + string.digits


def _rand(n: int = 14) -> str:
    # at least one digit and one letter, the way generated values look
    while True:
        v = "".join(secrets.choice(ALNUM) for _ in range(n))
        if any(c.isdigit() for c in v) and any(c.isalpha() for c in v):
            return v


NAMES = [
    "DB_PASS", "db_pass", "dbPass", "PGPASS", "pass", "PW", "db_pw", "cred", "creds", "KEY",
    "HMAC_KEY", "AES_KEY", "SESSION_KEY", "COOKIE_KEY", "JWT_KEY", "LICENSE_KEY", "AccountKey",
    "PEPPER", "SEED", "otp_seed", "master", "SIGNING", "PRIVATE", "STRIPE_WHSEC", "ENC_KEY",
]
TEMPLATES = [
    "{n}={v}", '{n}="{v}"', "{n}: {v}", '{n}: "{v}"', "export {n}={v}", '"{n}": "{v}"',
]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("template", TEMPLATES)
def test_a_value_under_a_secret_name_is_redacted_whatever_the_form(name, template):
    for _ in range(5):
        v = _rand()
        out, _ = redact_for_mcp(template.format(n=name, v=v), source_hint="x.yml")
        assert v not in out, (name, template, out)


@pytest.mark.parametrize("name", ["HMAC_KEY", "AES_KEY", "SESSION_KEY", "JWT_KEY", "LICENSE_KEY", "PEPPER", "SEED", "KEY"])
def test_a_generated_hex_value_under_a_key_name_is_redacted(name):
    for n in (40, 64):
        v = secrets.token_hex(n // 2)
        assert v not in redact_for_mcp(f"{name}={v}")[0]
        assert v not in redact_for_mcp(f'{name} = "{v}"')[0]
        assert redact_structure({name.lower(): v})[name.lower()] != v


def test_a_quoted_hex_value_under_a_neutral_name_is_redacted():
    v = secrets.token_hex(32)
    assert v not in redact_for_mcp(f'FOO = "{v}"')[0]


def test_a_sha_stays_where_the_text_says_it_is_one():
    sha = secrets.token_hex(20)
    for text in (f'"sha": "{sha}"', f"commit {sha}", f'"integrity": "{sha}"', f'"digest": "{sha}"'):
        assert sha in redact_for_mcp(text)[0], text


def test_a_name_value_pair_record_is_judged_by_its_name():
    v = _rand()
    data = {"env": [{"name": "DB_PASSWORD", "value": v}, {"name": "LOG_LEVEL", "value": "debug"}]}
    out = redact_structure(data)
    assert out["env"][0]["value"] != v
    assert out["env"][1]["value"] == "debug"


def test_names_that_only_look_like_secrets_keep_their_values():
    for text in (
        "primary_key = id", 'sort_key: "created_at"', "cache_key = user_id", 'key: "name"',
        'bypass: "enabled-for-all"', "compass = north", 'passing = "yes"', 'keyboard: "qwerty123x"',
        "key_id = abc1234567", "password_env = DB_PASSWORD",
    ):
        assert redact_for_mcp(text)[0] == text, text


FORMS = [
    ("azure storage account key", lambda s: f"DefaultEndpointsProtocol=https;AccountName=a;AccountKey={s}==;EndpointSuffix=core.windows.net"),
    ("service bus shared access key", lambda s: f"Endpoint=sb://x.example.net/;SharedAccessKeyName=root;SharedAccessKey={s}="),
    ("slack webhook", lambda s: f"https://hooks.slack.com/services/T0{s[:8].upper()}/B0{s[8:16].upper()}/{s}"),
    ("discord webhook", lambda s: f"https://discord.com/api/webhooks/123456789012345678/{s}"),
    ("telegram bot token", lambda s: f"https://api.telegram.org/bot123456789:{s[:35].ljust(35, 'a')}/getMe"),
    ("pre-signed url signature", lambda s: f"https://b.s3.example.com/k?X-Amz-Algorithm=AWS4&X-Amz-Signature={s}"),
    ("curl user flag", lambda s: f"curl -u admin:{s} https://api.example.com"),
    ("curl long user flag", lambda s: f"curl --user admin:{s} https://api.example.com"),
    ("mysql client password", lambda s: f"mysql -uroot -p{s} -h db shop"),
    ("password flag with a space", lambda s: f"tool --password {s} --host db"),
    ("cookie header", lambda s: f"Cookie: session={s}; theme=dark"),
    ("set-cookie header", lambda s: f"Set-Cookie: sid={s}; Path=/; HttpOnly"),
    ("scheme-less dsn", lambda s: f"app:{s}@db.example.com/shop"),
]


@pytest.mark.parametrize("name,build", FORMS, ids=[f[0] for f in FORMS])
def test_a_secret_in_a_connection_or_command_form_is_redacted(name, build):
    for _ in range(5):
        s = secrets.token_urlsafe(24).lstrip("-")  # a leading dash would read as a flag
        out, stats = redact_for_mcp(build(s))
        assert s not in out and s[:35] not in out, (name, out)
        assert stats.secrets_found >= 1


def test_forms_that_look_like_these_but_are_not_credentials_stay():
    for text in (
        "docker run -u 1000:1000 -p 8080:80 img", "mailto:team@example.com", "image: redis:7@sha256:abc",
        "git@github.com:acme/shop.git", "Path=/; HttpOnly", "ssh -p 2222 host",
    ):
        assert redact_for_mcp(text)[0] == text, text
    assert redact_for_mcp("Cookie: theme=dark")[0] == "Cookie: theme=[REDACTED:cookie]"


def test_a_bare_value_with_digits_in_a_code_file_is_redacted_when_it_looks_generated():
    for _ in range(20):
        v = _rand(10)
        if v.isalpha():
            continue
        out, _ = redact_for_mcp(f"password = {v}", source_hint="app.py")
        assert v not in out, out
    assert redact_for_mcp("password = new_password", source_hint="app.py")[0] == "password = new_password"
    assert redact_for_mcp("password = user1", source_hint="app.py")[0] == "password = user1"


# ─── hostile input must not stall the process ────────────────────────

SHAPES = {
    "one long word": lambda n: "a" * n,
    "dotted run": lambda n: "a." * (n // 2),
    "dashed run": lambda n: "a-" * (n // 2),
    "base64 blob": lambda n: "QUJD" * (n // 4),
    "minified": lambda n: "function(a,b){return a+b;}" * (n // 26),
    "unterminated shell default": lambda n: "${a:-" * (n // 5),
    "many pem openers": lambda n: "-----BEGIN A-----" * (n // 17),
    "many putty headers": lambda n: "PuTTY-User-Key-File-2:" * (n // 22),
    "scheme dots": lambda n: "a.b+c-" * (n // 6) + "://",
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_redaction_of_long_hostile_input_stays_fast(shape):
    text = SHAPES[shape](200_000)
    t0 = time.monotonic()
    redact_for_mcp(text)
    assert time.monotonic() - t0 < 2.0


def test_a_long_data_uri_is_withheld_whole_and_fast():
    text = "src: data:image/png;base64," + "QUJD" * 4000 + "\nnext = 1"
    t0 = time.monotonic()
    out, _ = redact_for_mcp(text)
    assert time.monotonic() - t0 < 1.0
    assert "QUJD" * 100 not in out and "[REDACTED:long-literal]" in out and "next = 1" in out


def test_a_pem_key_is_still_found_among_many_openers():
    body = secrets.token_urlsafe(40)
    text = "-----BEGIN CERTIFICATE-----" * 50 + f"\n-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    assert body not in redact_for_mcp(text)[0]


def test_a_redaction_that_runs_out_of_time_is_withheld_by_the_guard(monkeypatch):
    import asyncio

    from mcp import types
    from mcp.server.fastmcp import FastMCP

    from src.mcp_server.output_guard import WITHHELD, install_output_guard
    from src.security import mcp_redact

    monkeypatch.setattr(mcp_redact, "MAX_SECONDS", -1.0)
    mcp = FastMCP("t")

    @mcp.tool()
    def echo() -> str:
        return "hello"

    install_output_guard(mcp)
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="echo", arguments={}))
    result = asyncio.run(handler(req)).root
    assert result.isError and result.content[0].text == WITHHELD
