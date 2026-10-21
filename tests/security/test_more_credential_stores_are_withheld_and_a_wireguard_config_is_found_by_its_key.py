"""The login files of a few more tools are withheld, and a WireGuard config is withheld by the key it holds.

THE DEFECT. A hunt over the stores that tools drop into a repository or a home directory found some that were still
read like any file: Cloudflare Workers' `.dev.vars`, `rclone.conf`, `.databrickscfg`, `kaggle.json`, MySQL's
`.mylogin.cnf`, PostgreSQL's `.pg_service.conf`, a CIFS `.smbcredentials`, Hugging Face's `token`, an extensionless
file called `secrets`, `credentials.cnf` / `.config` / `.hcl`, Android's `keystore.properties`, and a WireGuard
interface (`PrivateKey = ...`) in a file of any name.

THE DECISION, one by one.
  * denied by name, for every reader: all of the above, plus Terraform's `.terraformrc`, an FTP `.ftppass`, the
    user-level `~/.gradle/gradle.properties` and the CLI tokens of hcloud, doctl, ngrok, glab and gcloud;
  * `gradle.properties` of a project is mostly versions and JVM flags and sometimes a signing password: `keys_only`
    (shown with its values withheld, as `.env.example`); `.dev.vars.example` is `keys_only` as well;
  * a WireGuard config is told by its CONTENT (`PrivateKey` / `PresharedKey` = 44 base64 characters in a data file);
    `wg0.conf` is also denied by name, since the name says it;
  * names that merely look alike (`src/secrets.py`, `docs/secrets.md`, `src/token.py`, `internal/token/token.go`,
    `wg.conf`, `gradle-wrapper.properties`) stay readable.
"""

from __future__ import annotations

import pytest

from src.security import secret_files as sf

KEY = "CANARYWG" + "a" * 35 + "="            # 44 characters, the shape of a WireGuard key
CANARY = "CANARY_VALUE_5e1d"

DENIED = [
    "secrets", "deploy/secrets", ".davfs2/secrets", "Secrets",
    ".dev.vars", "workers/api/.dev.vars", ".dev.vars.production",
    "rclone.conf", ".config/rclone/rclone.conf", ".databrickscfg", "kaggle.json", ".kaggle/kaggle.json",
    ".mylogin.cnf", ".pg_service.conf", "pg_service.conf", ".smbcredentials", "smbcredentials",
    "wg0.conf", "wireguard/wg1.conf", "etc/wg12.conf",
    ".huggingface/token", ".cache/huggingface/token", "home/u/.cache/huggingface/token", "huggingface/token",
    ".gradle/gradle.properties", "home/u/.gradle/gradle.properties", "android/keystore.properties", "signing.properties",
    "credentials.cnf", "credentials.config", "credentials.hcl", "credentials.prod.cnf", "credentials.prod.hcl",
    "credentials.tfrc", "creds/Credentials.HCL",
    ".terraformrc", "terraform.rc", ".ftppass",
    ".config/hcloud/cli.toml", ".config/doctl/config.yaml", ".config/ngrok/ngrok.yml", ".config/glab-cli/config.yml",
    ".config/gcloud/access_tokens.db", ".azure/msal_token_cache.bin", ".azure/service_principal_entries.bin",
    ".vscode/sftp.json",
]
ORDINARY = [
    "src/secrets.py", "src/secrets.go", "docs/secrets.md", "secrets.md",
    "rclone.md", "docs/kaggle.md", "kaggle.py", "wg.conf", "wgraph.conf", "wg0.md", "docs/wireguard.md",
    "gradle/wrapper/gradle-wrapper.properties", "build.gradle", "settings.gradle", "gradle.md",
    "src/token.py", "internal/token/token.go", "huggingface/token.py", "docs/huggingface/README.md",
    "config.hcl", "pg_service.md", ".vscode/settings.json", ".vscode/launch.json", ".config/ngrok/README.md",
    ".config/hcloud/README.md", "terraform.tfvars.md", "keystore.md", "docs/credentials.md",
]
KEYS_ONLY = [
    "gradle.properties", "app/gradle.properties", "Gradle.Properties",
    ".dev.vars.example", "workers/.dev.vars.sample", ".dev.vars.template",
]


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    return root


def _put(root, rel: str, text: str | bytes = f"token = {CANARY}\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())


@pytest.mark.parametrize("rel", DENIED)
def test_a_credential_store_is_denied_for_every_reader(repo, rel):
    from src.indexing.graph.languages.factory import walk_repo_files
    from src.mcp_server.howto import engine
    from src.retrieval.tier3_code import CodeReader

    _put(repo, rel)

    assert sf.classify(rel) == "deny" and sf.is_secret_path(rel) is True
    assert sf.plain_file(repo, rel) is None
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(repo, rel)
    assert CodeReader().read_full_file(repo, rel) is None
    assert engine._read(repo, rel) is None
    assert not [p for p in walk_repo_files(repo) if p.relative_to(repo.resolve()).as_posix() == rel]


@pytest.mark.parametrize("rel", ORDINARY)
def test_a_file_that_only_looks_like_a_credential_store_stays_readable(repo, rel):
    _put(repo, rel, "x = 1\n")

    assert sf.classify(rel) == "ok" and sf.is_secret_path(rel) is False
    assert sf.classify_file(repo, rel) == "ok" and sf.plain_file(repo, rel) is not None


@pytest.mark.parametrize("rel", KEYS_ONLY)
def test_gradle_properties_and_the_dev_vars_examples_are_shown_with_their_values_withheld(repo, rel):
    from src.mcp_server.howto import engine
    from src.retrieval.tier3_code import CodeReader

    _put(repo, rel, f"org.gradle.jvmargs=-Xmx2g\nsigning.password={CANARY}\nandroid.useAndroidX=true\nEMPTY=\n")

    assert sf.classify(rel) == "keys_only" and sf.is_secret_path(rel) is True
    for shown in (CodeReader().read_full_file(repo, rel).content, engine._read(repo, rel)):
        assert CANARY not in shown and "-Xmx2g" not in shown
        assert "signing.password=[value withheld]" in shown
        assert "android.useAndroidX=true" in shown and "EMPTY=" in shown


def test_the_gradle_properties_in_the_users_gradle_home_are_denied_not_masked(repo):
    _put(repo, ".gradle/gradle.properties", f"signing.password={CANARY}\n")

    assert sf.classify(".gradle/gradle.properties") == "deny"


# --- WireGuard ---------------------------------------------------------------------------------------------------

INTERFACE = f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.8.0.2/32\nDNS = 10.8.0.1\n\n[Peer]\nPublicKey = {'B' * 43}=\n"
PRESHARED = f"[Peer]\nPublicKey = {'B' * 43}=\nPresharedKey = {KEY}\nAllowedIPs = 10.8.0.0/24\n"
PEER_ONLY = f"[Peer]\nPublicKey = {'B' * 43}=\nAllowedIPs = 10.8.0.0/24\nEndpoint = vpn.example.com:51820\n"


@pytest.mark.parametrize("rel", ["vpn/client.conf", "etc/wireguard/office.conf", "vpn/peer.txt", "vpn/notes", "vpn/office.cfg"])
@pytest.mark.parametrize(("text", "how"), [
    (INTERFACE, "utf-8"), (PRESHARED, "utf-8"),
    (INTERFACE.replace("\n", "\r\n"), "crlf"), (INTERFACE.replace("\n", "\r"), "cr"),
    (INTERFACE, "utf-16"), (PRESHARED, "utf-16-le"),
    (INTERFACE.replace("PrivateKey = ", "PrivateKey=").replace("\n", "\n  "), "tight-indented"),
])
def test_a_wireguard_config_is_withheld_by_the_key_it_holds_whatever_it_is_called(repo, rel, text, how):
    from src.retrieval.tier3_code import CodeReader

    data = text.encode("utf-16" if how == "utf-16" else "utf-16-le" if how == "utf-16-le" else "utf-8")
    _put(repo, rel, data)

    assert sf.classify(rel) == "ok", "the name alone does not say it"
    assert sf.classify_file(repo, rel) == "deny"
    assert sf.plain_file(repo, rel) is None
    assert CodeReader().read_full_file(repo, rel) is None


@pytest.mark.parametrize("text", [
    PEER_ONLY,
    "[Interface]\nPrivateKey = <your private key>\nAddress = 10.8.0.2/32\n",
    "[Interface]\nPrivateKey = ${WG_PRIVATE_KEY}\n",
    "[Interface]\nPrivateKey = \n",
    f"[Interface]\n# PrivateKey = {KEY}\n",
    f"[Interface]\nPrivateKey = {KEY[:-1]}xx\n",                 # not 44 characters
    f"note = the PrivateKey = {KEY} is in the vault\n",         # not a line of its own
])
def test_a_config_without_a_wireguard_secret_stays_readable(repo, text):
    _put(repo, "vpn/client.conf", text)

    assert sf.classify_file(repo, "vpn/client.conf") == "ok"
    assert sf.plain_file(repo, "vpn/client.conf") is not None


def test_a_wireguard_example_in_prose_or_code_stays_readable(repo):
    _put(repo, "docs/wireguard.md", f"```\n{INTERFACE}```\n")
    _put(repo, "src/wg.py", f'EXAMPLE = """{INTERFACE}"""\n')

    assert sf.classify_file(repo, "docs/wireguard.md") == "ok"
    assert sf.classify_file(repo, "src/wg.py") == "ok"
