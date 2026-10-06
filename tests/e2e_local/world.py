"""Build the synthetic ``acme`` repositories on disk, with canaries.

``tests/fixtures/dev_mcp/`` holds plain source trees. This module turns them
into git repositories in a temporary directory and plants, at run time, the
things that must never reach a tool answer:

* the ``__CANARY_*__`` placeholder inside committed fixture code (a password
  written into a DSN) is replaced by a fresh random canary;
* secret FILES are created from the templates below, with fresh canaries: a
  ``.env``, a private key, a Kubernetes Secret, a service-account JSON. They
  are committed into the temporary repository because the threat model is
  somebody having committed them, and so they exist in the clone that gets
  indexed. They are never written inside this repository's working tree.

Nothing here is a real credential; each value is ``CANARY_`` plus random hex,
so a leak is one substring search and a secret scanner has nothing to find in
this repository (the values exist only in a temp dir for the length of a run).
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "dev_mcp"

#: logical repository name -> (fixture directory, remote URL the server registers)
REPOS = {
    "acme/shop": ("acme-shop", "https://github.com/acme/shop"),
    "acme/billing": ("acme-billing", "https://github.com/acme/billing"),
    "acme/gateway": ("acme-gateway", "https://github.com/acme/gateway"),
}

PLACEHOLDER = re.compile(r"__CANARY_([A-Z0-9_]+)__")
_TEXT_SUFFIXES = {".py", ".ts", ".go", ".yml", ".yaml", ".json", ".md", ".txt", ".toml", ".mod",
                  ".example", ""}


def _pem(label: str, body_line: str) -> str:
    """A PEM-shaped block whose body is made of ``body_line`` (a canary)."""
    head = "-----BEGIN " + label + "-----"
    tail = "-----END " + label + "-----"
    filler = base64.b64encode(secrets.token_bytes(48)).decode()
    return f"{head}\n{body_line}\n{filler}\n{tail}\n"


#: path -> template; ``__CANARY_X__`` is replaced by the run's canary X.
SECRET_FILES: dict[str, dict[str, str]] = {
    "acme/shop": {
        ".env": ("DB_PASSWORD=__CANARY_SHOP_DB_PASSWORD__\n"
                 "JWT_JWKS_URL=https://auth.example.com/jwks\n"),
        "secrets/db_password.txt": "__CANARY_SHOP_SECRETS_DIR__\n",
    },
    "acme/billing": {
        ".env": ("DATABASE_URL=postgresql://billing:__CANARY_BILLING_DB_PASSWORD__@db/billing\n"
                 "STRIPE_API_KEY=sk_live___CANARY_STRIPE__\n"
                 "AWS_ACCESS_KEY_ID=__CANARY_AWS_KEY__\n"),
        "deploy/server.key": "@@PEM:PRIVATE KEY:__CANARY_BILLING_PEM__@@",
        "deploy/k8s/secret.yaml": ("apiVersion: v1\nkind: Secret\nmetadata:\n"
                                   "  name: billing-db\ntype: Opaque\ndata:\n"
                                   "  password: @@B64:__CANARY_K8S_SECRET__@@\n"),
    },
    "acme/gateway": {
        "id_rsa": "@@PEM:OPENSSH PRIVATE KEY:__CANARY_GATEWAY_SSH__@@",
        ".env.production": "JWT_SIGNING_SECRET=__CANARY_GATEWAY_JWT__\n",
        "deploy/gcp-credentials.json": ('{"type": "service_account", "private_key_id": '
                                        '"__CANARY_GATEWAY_SA__", "client_email": '
                                        '"svc@acme.example.com"}\n'),
    },
}


def _canary(label: str) -> str:
    if label.endswith("AWS_KEY"):
        return "AKIA" + "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
                                for _ in range(16))
    return "CANARY_" + secrets.token_hex(12)


@dataclass
class World:
    """The repositories on disk and every canary planted in them."""

    root: Path
    repos: dict[str, Path] = field(default_factory=dict)       # logical name -> git repo
    canaries: dict[str, str] = field(default_factory=dict)     # label -> value
    secret_paths: dict[str, list[str]] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)

    def all_canaries(self) -> list[str]:
        """Every value that must never be in an answer, including the base64 form."""
        out = list(self.canaries.values())
        out += [base64.b64encode(v.encode()).decode() for v in self.canaries.values()]
        return out

    def leaks_in(self, text: str) -> list[str]:
        """The labels whose canary appears in ``text`` (never the values)."""
        found = []
        for label, value in self.canaries.items():
            if value in text or base64.b64encode(value.encode()).decode() in text:
                found.append(label)
        return found


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "acme", "GIT_AUTHOR_EMAIL": "dev@acme.example.com",
           "GIT_COMMITTER_NAME": "acme", "GIT_COMMITTER_EMAIL": "dev@acme.example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _fill(template: str, canaries: dict[str, str]) -> str:
    def pem(m: re.Match) -> str:
        label, body = m.group(1), m.group(2)
        return _pem(label, PLACEHOLDER.sub(lambda p: canaries[p.group(1)], body))

    def b64(m: re.Match) -> str:
        inner = PLACEHOLDER.sub(lambda p: canaries[p.group(1)], m.group(1))
        return base64.b64encode(inner.encode()).decode()

    text = re.sub(r"@@PEM:([A-Z ]+):(.+?)@@", pem, template)
    text = re.sub(r"@@B64:(.+?)@@", b64, text)
    return PLACEHOLDER.sub(lambda p: canaries[p.group(1)], text)


def materialize(dest: Path, *, only: list[str] | None = None) -> World:
    """Copy the fixtures into ``dest``, plant canaries, ``git init`` and commit."""
    dest = Path(dest)
    if dest.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("refusing to materialise canaries inside the source tree")
    world = World(root=dest)
    for name, (dirname, _url) in REPOS.items():
        if only is not None and name not in only:
            continue
        target = dest / dirname
        shutil.copytree(FIXTURES / dirname, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        # Canaries: collect every label this repository mentions, committed code
        # and secret templates alike.
        labels: set[str] = set()
        for f in target.rglob("*"):
            if f.is_file() and f.suffix in _TEXT_SUFFIXES:
                labels.update(PLACEHOLDER.findall(f.read_text(encoding="utf-8")))
        for tpl in SECRET_FILES.get(name, {}).values():
            labels.update(PLACEHOLDER.findall(tpl))
        for label in sorted(labels):
            world.canaries.setdefault(label, _canary(label))
        for f in target.rglob("*"):
            if f.is_file() and f.suffix in _TEXT_SUFFIXES and PLACEHOLDER.search(
                    f.read_text(encoding="utf-8")):
                f.write_text(_fill(f.read_text(encoding="utf-8"), world.canaries),
                             encoding="utf-8")
        planted = []
        for rel, tpl in SECRET_FILES.get(name, {}).items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_fill(tpl, world.canaries), encoding="utf-8")
            planted.append(rel)
        world.secret_paths[name] = planted
        _git(target, "init", "-q", "-b", "develop")
        _git(target, "add", "-A", "-f")
        _git(target, "commit", "-q", "-m", "initial import")
        world.repos[name] = target
        world.heads[name] = _git(target, "rev-parse", "HEAD")
    return world
