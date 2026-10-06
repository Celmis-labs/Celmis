"""What a developer needs verbatim must survive the redactor.

Over-redaction hurts as much as a leak in this product: the ``idx:`` header
carries the indexed sha, and "how is it done" answers are full of references to
secrets that are not secrets (``os.getenv("DB_PASSWORD")``, ``${DB_PASSWORD}``).
"""

from __future__ import annotations

import pytest

from src.security.mcp_redact import redact_for_mcp, redact_structure

SHA40 = "3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e"
SHA64 = SHA40 + "0123456789abcdef01234567"

KEPT = [
    ("the idx header line", f"idx: acme/shop develop@{SHA40[:7]} 2h fresh", ""),
    ("a full sha in text", f"commit {SHA40} on main", ""),
    ("a sha 256 in text", f"digest {SHA64}", ""),
    ("a quoted sha 1", f'sha = "{SHA40}"', ""),
    ("a uuid", 'id = "550e8400-e29b-41d4-a716-446655440000"', ""),
    ("an env read", 'os.getenv("DB_PASSWORD")', "a.py"),
    ("an env read with a safe default", 'os.getenv("DB_HOST", "localhost")', "a.py"),
    ("a js env read", "const p = process.env.DB_PASSWORD;", "a.js"),
    ("a compose reference", "      DB_PASSWORD: ${DB_PASSWORD}", "docker-compose.yml"),
    ("a shell reference", "export DB_PASSWORD=${DB_PASSWORD}", ""),
    ("a helm template", "password: {{ .Values.db.password }}", "t.yaml"),
    ("a secret key ref", "secretKeyRef:\n  name: shop-db\n  key: password", "d.yaml"),
    ("a vault path", "password: vault:secret/data/shop#password", "c.yml"),
    ("an attribute chain", "password=settings.db_password", "a.py"),
    ("a self attribute", "self.password = password", "a.py"),
    ("a call in a kwarg", "password=get_password()", "a.py"),
    ("a placeholder", "password: changeme", "c.yml"),
    ("an angle bracket placeholder", "password: <your-password>", "c.yml"),
    ("an empty value", "DB_PASSWORD=", ".env.example"),
    ("a dsn without a password", "postgresql://app@db:5432/shop", ""),
    ("a plain url", "https://example.com/a/b?page=2&q=search", ""),
    ("an import path", "from src.mcp_server.dev_profile.tools_find import handler", "a.py"),
    ("a lockfile integrity hash", 'integrity "sha512-' + "AbCdEfGhIj" * 9 + '=="', ""),
    ("a css class hash", ".button-3f9c2b1d { color: red }", "a.css"),
    ("a token url", 'token_url: "https://auth.example.com/oauth/token"', "c.yml"),
    ("a pointer to an env name", 'password_env = "DB_PASSWORD"', "a.py"),
    ("a token count", "max_tokens = 4096", "a.py"),
    ("a camel case identifier", 'const name = "OrderServiceFactoryBuilderImpl";', "a.ts"),
    ("a file path literal", 'path = "src/mcp_server/dev_profile/tools_find.py"', "a.py"),
]


@pytest.mark.parametrize("name,text,hint", KEPT, ids=[k[0] for k in KEPT])
def test_something_that_is_not_a_secret_is_left_alone(name, text, hint):
    out, stats = redact_for_mcp(text, source_hint=hint)
    assert out == text, (name, out)
    assert stats.secrets_found == 0


def test_a_sha_under_an_exempt_key_survives_even_when_it_looks_random():
    data = {"indexed_sha": SHA40, "sha": SHA40[:12], "cursor": "p2", "id": "a1b2c3d4"}
    assert redact_structure(data) == data


def test_a_sha_under_any_other_key_is_not_touched_either():
    data = {"note": SHA40}
    assert redact_structure(data) == data
