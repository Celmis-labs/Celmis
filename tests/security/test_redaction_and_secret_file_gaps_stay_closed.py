"""Forms of a credential that used to get through, and look-alikes that must not
be touched.

Two layers, one rule each: `redact_for_mcp` hides the VALUE of a credential
wherever it is written, and `secret_files.classify` keeps a file that is a
credential store out of every dev tool. Every case below was a real hole; the
"stays" lists keep the fix from turning into a blanket that eats the code.
"""

from __future__ import annotations

import pytest

from src.security.mcp_redact import redact_for_mcp
from src.security.secret_files import classify

S = "Zx9Qm4Tp7Wv2Lk8Rb5Nc3Hd6Fj1Ys0A"  # made up


def _out(text: str, hint: str = "src/config.py") -> str:
    return redact_for_mcp(text, source_hint=hint)[0]


HIDDEN = {
    "k8s name/value block": f"env:\n  - name: DB_PASSWORD\n    value: {S}\n",
    "k8s name/value inline json": '{"name": "DB_PASSWORD", "value": "' + S + '"}',
    "xml element": f"<password>{S}</password>",
    "xml attribute pair": f'<add key="ApiKey" value="{S}" />',
    "npmrc auth token": f"//registry.example.com/:_authToken={S}",
    "docker login flag": f"docker login -u u -p {S} registry.example.com",
    "setter call": f'config.setPassword("{S}")',
    "define pair": f"define('DB_PASSWORD', '{S}');",
    "dockerfile ENV": f"ENV DB_PASSWORD {S}\n",
    "full-width colon": "password：" + S,
    "full-width equals": "password＝" + S,
    "prose": f"the admin password is {S}",
    "sql inside a code string": f'cur.execute("CREATE USER app WITH PASSWORD \'{S}\'")',
    "password in a connection string": f'cs = "Server=db;User Id=sa;Pwd={S};"',
}


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_a_credential_written_this_way_does_not_leave(name):
    out = _out(HIDDEN[name])
    assert S not in out, f"{name}: {out!r}"


def test_an_htpasswd_hash_does_not_leave():
    line = "deploy:$apr1$r31.....$HqJZimcKQFAMYayBlzkrA/"
    assert "HqJZimcKQFAMYayBlzkrA" not in _out(line, ".htpasswd")


STAYS = [
    ("docker run -p 8080:80 nginx", "docker run -p 8080:80"),
    ("password = new_password", "new_password"),
    ("ENV NODE_ENV production", "production"),
    ("<title>Welcome</title>", "Welcome"),
    ("config.setTimeout(30)", "setTimeout(30)"),
    ("name: LOG_LEVEL\n    value: debug", "debug"),
    ("the password is required", "required"),
    ("def set_password(user, new_password):", "set_password(user, new_password)"),
]


@pytest.mark.parametrize("text,kept", STAYS)
def test_code_that_only_looks_like_a_credential_is_left_alone(text, kept):
    assert kept in _out(text)


# ─── which files are credential stores ───────────────────────────────

DENIED = [
    "vault-password.txt", "ops/vault_pass.txt", "secrets/token.txt", "deploy/api_token",
    "x/api_token", "build/htpasswd", "infra/prod.tfvars.json", "keys/gcp-key.json",
    "firebase-adminsdk-abc.json", "wp-config.php", "app/local_settings.py", "ansible/vault.yml",
    "charts/values-secret.yaml", "certs/client.p12", "certs/client.pfx", "keys/server.pkcs12",
    ".env", "a/.env ", "a/.env​", ".env.", "config/.env.production",
    "deploy/gcp-credentials.json",
]


@pytest.mark.parametrize("path", DENIED)
def test_a_credential_store_is_kept_out_of_every_tool(path):
    assert classify(path) == "deny", path


ALLOWED = [
    "package.json", "src/token_service.py", "password_reset.py", "docs/tokens.md",
    "README.md", "locales/en.json", "src/keys.json", "src/auth/tokenizer.py",
]


@pytest.mark.parametrize("path", ALLOWED)
def test_a_file_that_only_mentions_a_secret_in_its_name_is_still_readable(path):
    assert classify(path) == "ok", path


def test_an_env_template_shows_its_names_and_never_a_value():
    assert classify(".env.example") == "keys_only"
