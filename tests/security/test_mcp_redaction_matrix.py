"""The MCP redactor removes secrets of every shape the probes know, and only those.

Canaries are built at runtime (``secrets.token_urlsafe``), so the test file
holds no secret-shaped literal and no probe can pass by coincidence.
"""

from __future__ import annotations

import base64
import secrets
import urllib.parse

import pytest

from src.security.mcp_redact import redact_for_mcp, redact_structure


def _canary(n: int = 9) -> str:
    return secrets.token_urlsafe(n)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# (id, builder(canary) -> text, source_hint)
PROBES = [
    ("dsn with a driver suffix", lambda c: f"postgresql+asyncpg://app:{c}@db:5432/app", ""),
    ("redis url with an empty user", lambda c: f"redis://:{c}@cache:6379/0", ""),
    ("amqps url", lambda c: f"amqps://svc:{c}@mq.example.com/vhost", ""),
    ("mongodb srv url", lambda c: f"mongodb+srv://svc:{c}@cluster.example.com/db", ""),
    ("libpq key value string", lambda c: f"host=db dbname=x password={c} user=app", ""),
    ("ado.net connection string", lambda c: f"Server=db;User Id=sa;Password={c};Database=x", ""),
    ("jdbc query password", lambda c: f"jdbc:postgresql://db/x?user=a&password={c}", ""),
    ("quoted short password", lambda c: f'password = "{c}"', ""),
    ("spring datasource property", lambda c: f"spring.datasource.password={c}", "application.properties"),
    ("yaml upper case key", lambda c: f"DB_PASSWORD: {c}", "config.yml"),
    ("authorization token header", lambda c: f'"Authorization": "Token {c}"', ""),
    ("bearer header in curl", lambda c: f'curl -H "Authorization: Bearer {c}" https://x.example', ""),
    ("api key subscript", lambda c: f'headers["X-Api-Key"] = "{c}"', ""),
    ("pgpassword before a command", lambda c: f"PGPASSWORD={c} psql -h db", ""),
    ("quoted env assignment with spaces", lambda c: f'export API_TOKEN="{c} {c}"', ""),
    ("url encoded password in a dsn", lambda c: f"postgres://u:{urllib.parse.quote(c + '@!/', safe='')}@h/db", ""),
    ("url encoded query token", lambda c: f"https://x.example/cb?token={urllib.parse.quote(c)}&a=1", ""),
    ("basic auth header with base64", lambda c: f"Authorization: Basic {_b64('svc:' + c)}", ""),
    ("docker config auth field", lambda c: f'{{"auths": {{"r.example": {{"auth": "{_b64("u:" + c)}"}}}}}}', ""),
    ("pydantic field default", lambda c: f'legacy_token: str = "{c}"', "config.py"),
    ("typescript typed const", lambda c: f'const dbPassword: string = "{c}";', "db.ts"),
    ("getenv with a literal default", lambda c: f'os.getenv("DB_PASSWORD", "{c}")', "a.py"),
    ("js env or literal", lambda c: f'process.env.API_TOKEN || "{c}"', "a.js"),
    ("shell default expansion", lambda c: f"${{DB_PASSWORD:-{c}}}", ""),
    ("json field", lambda c: f'{{"db": {{"password": "{c}"}}}}', ""),
    ("kubernetes secret data", lambda c: f"kind: Secret\nmetadata:\n  name: x\ndata:\n  password: {_b64(c)}\n", "s.yaml"),
    ("pem private key", lambda c: f"-----BEGIN PRIVATE KEY-----\n{c}\n{c}\n-----END PRIVATE KEY-----", ""),
    ("openssh private key", lambda c: f"-----BEGIN OPENSSH PRIVATE KEY-----\n{c}\n-----END OPENSSH PRIVATE KEY-----", ""),
    ("truncated private key", lambda c: f"-----BEGIN RSA PRIVATE KEY-----\n{c}\n{c}", ""),
    ("github token", lambda c: "ghp_" + (c * 5)[:36].replace("-", "a").replace("_", "b"), ""),
    ("high entropy quoted literal", lambda c: f'key = "{secrets.token_hex(16)}"', ""),
    ("token in https userinfo", lambda c: f"https://{(c * 4)[:30].replace('-', 'a').replace('_', 'b')}@host.example/r.git", ""),
]


@pytest.mark.parametrize("name,build,hint", PROBES, ids=[p[0] for p in PROBES])
def test_a_planted_secret_never_survives_redaction(name, build, hint):
    canary = _canary()
    text = build(canary)
    # the hex probe has no separable canary: use the whole literal
    out, stats = redact_for_mcp(text, source_hint=hint)
    probe_value = canary
    if "token_hex" in name or name == "high entropy quoted literal":
        probe_value = text.split('"')[1]
    assert probe_value not in out, (name, out)
    for variant in (urllib.parse.quote(canary), _b64(canary), _b64("svc:" + canary), _b64("u:" + canary)):
        assert variant not in out, (name, variant, out)
    assert "[REDACTED:" in out
    assert stats.secrets_found >= 1


def test_the_shape_of_a_dsn_survives_so_the_pattern_stays_readable():
    out, _ = redact_for_mcp(f"postgresql+asyncpg://app:{_canary()}@db:5432/app")
    assert out == "postgresql+asyncpg://app:[REDACTED:dsn-password]@db:5432/app"


def test_every_string_of_a_json_structure_is_judged_by_its_key_too():
    c = _canary()
    out = redact_structure({"password": c, "nested": [{"client_secret": c}], "note": "fine"})
    assert c not in repr(out)
    assert out["note"] == "fine"


def test_a_secret_field_is_redacted_even_when_nothing_in_the_text_gives_it_away():
    c = _canary(3)  # 4 characters: no entropy rule could catch it
    assert c not in repr(redact_structure({"token": c}))


def test_the_value_of_a_key_that_only_points_at_a_secret_is_kept():
    out = redact_structure({"password_env": "DB_PASSWORD", "token_url": "https://auth.example/t", "secret_name": "shop-db"})
    assert out == {"password_env": "DB_PASSWORD", "token_url": "https://auth.example/t", "secret_name": "shop-db"}


def test_redaction_is_idempotent():
    c = _canary()
    once, _ = redact_for_mcp(f"postgresql://u:{c}@h/db and password={c}")
    twice, stats = redact_for_mcp(once)
    assert once == twice and stats.secrets_found == 0


def test_the_redactor_runs_whatever_the_llm_path_setting_says(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("REDACTION_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().redaction_enabled is False
        c = _canary()
        out, _ = redact_for_mcp(f"password={c}")
        assert c not in out
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("hint", ["app/auth.py", "web/api.ts", "cmd/main.go", "ci/call.sh"])
@pytest.mark.parametrize(
    "shape",
    [
        'AUTH_HEADERS = {{"Authorization": "Token {c}"}}',
        "headers = {{'Authorization': 'Bearer {c}'}}",
        'h["x-api-key"] = "{c}"',
        'curl -H "X-API-Key: {c}" https://example.test',
        "X-Api-Key: {c}",
    ],
)
def test_a_header_secret_made_only_of_letters_and_digits_is_redacted_in_code_files_too(shape, hint):
    """An identifier-shaped value is a variable only when it stands bare and reads like a name."""
    for _ in range(300):
        c = secrets.token_urlsafe(18)
        out, _stats = redact_for_mcp(shape.format(c=c), source_hint=hint)
        assert c not in out, shape
