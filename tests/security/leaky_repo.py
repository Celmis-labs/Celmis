"""A synthetic repository full of planted secrets, for leak tests.

Every secret value carries a per-run canary from ``secrets.token_urlsafe``, built
at runtime so no scanner fires on this file and no test can pass by luck. The
repo is a real git repository (``git ls-files`` is what the engine walks) and
the secret files ARE committed, the way they sometimes are in real life.
"""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SLUG = "acme-shop"


@dataclass
class Leaky:
    slug: str
    path: Path
    canaries: dict[str, str] = field(default_factory=dict)

    def all_canaries(self) -> list[str]:
        out = list(self.canaries.values())
        out += [base64.b64encode(c.encode()).decode() for c in self.canaries.values()]
        return out


def _c() -> str:
    return secrets.token_urlsafe(18)


def build(repos_dir: Path, slug: str = SLUG) -> Leaky:
    root = repos_dir / slug
    root.mkdir(parents=True)
    k = {n: _c() for n in (
        "dsn", "env_db", "env_jwt", "compose_pw", "ci_token", "auth_hdr", "cs_pw",
        "props_pw", "ts_dsn", "pem", "k8s_secret", "settings_default", "getenv_default",
        "yaml_pw", "query_tok", "ssh",
    )}
    pem_body = "\n".join(secrets.token_urlsafe(48) for _ in range(3))
    files: dict[str, str] = {
        "src/db/session.py": (
            "from sqlalchemy.ext.asyncio import create_async_engine\n"
            "from src.config import settings\n\n\n"
            "def get_engine():\n"
            "    return create_async_engine(\n"
            "        settings.database_url,\n"
            "        pool_size=settings.db_pool_size,\n"
            "    )\n"
        ),
        "src/config.py": (
            "from pydantic_settings import BaseSettings\n\n\n"
            "class Settings(BaseSettings):\n"
            "    database_url: str\n"
            "    db_pool_size: int = 5\n"
            "    jwt_secret: str\n"
            f"    legacy_token: str = \"{k['settings_default']}\"\n\n\n"
            "settings = Settings()\n"
        ),
        "src/db/legacy.py": (
            "import os\n\n"
            f"DSN = \"postgresql+asyncpg://app:{k['dsn']}@db:5432/app\"\n"
            f"PASSWORD = os.getenv(\"DB_PASSWORD\", \"{k['getenv_default']}\")\n"
        ),
        "web/db.ts": (
            "import { Pool } from 'pg';\n\n"
            "export const pool = new Pool({\n"
            f"  connectionString: process.env.DATABASE_URL || \"postgresql+asyncpg://app:{k['ts_dsn']}@db/app\",\n"
            "});\n"
        ),
        "app/auth.py": (
            "import jwt\nfrom fastapi import Depends\nfrom fastapi.security import HTTPBearer\n"
            "from src.config import settings\n\n"
            "bearer = HTTPBearer()\n"
            f"AUTH_HEADERS = {{\"Authorization\": \"Token {k['auth_hdr']}\"}}\n\n\n"
            "def get_current_user(creds=Depends(bearer)):\n"
            "    return jwt.decode(creds.credentials, settings.jwt_secret, algorithms=[\"HS256\"])\n"
        ),
        "appsettings.json": (
            "{\n  \"ConnectionStrings\": {\n"
            f"    \"Default\": \"Host=db;Username=app;Password={k['cs_pw']};Database=shop\"\n  }}\n}}\n"
        ),
        "application.properties": (
            "spring.datasource.url=jdbc:postgresql://db:5432/shop\n"
            "spring.datasource.username=app\n"
            f"spring.datasource.password={k['props_pw']}\n"
        ),
        ".env": (
            f"DATABASE_URL=postgresql://app:{k['env_db']}@db/app\n"
            f"JWT_SECRET={k['env_jwt']}\n"
        ),
        ".env.example": (
            "DATABASE_URL=\nDB_POOL_SIZE=5\nJWT_SECRET=\n"
            f"LEGACY_TOKEN={k['settings_default']}\n"
        ),
        "server.key": f"-----BEGIN PRIVATE KEY-----\n{pem_body}\n-----END PRIVATE KEY-----\n",
        "id_rsa": f"-----BEGIN OPENSSH PRIVATE KEY-----\n{k['ssh']}\n-----END OPENSSH PRIVATE KEY-----\n",
        "k8s/secret.yaml": (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: shop-db\ndata:\n"
            f"  password: {base64.b64encode(k['k8s_secret'].encode()).decode()}\n"
        ),
        "k8s/deploy.yaml": (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: shop\nspec:\n  template:\n    spec:\n"
            "      containers:\n        - name: api\n          env:\n"
            "            - name: DATABASE_URL\n              valueFrom:\n                secretKeyRef:\n"
            "                  name: shop-db\n                  key: url\n"
        ),
        "docker-compose.yml": (
            "services:\n  api:\n    image: shop\n    environment:\n"
            "      DATABASE_URL: ${DATABASE_URL}\n"
            "      DB_POOL_SIZE: 10\n"
            "  db:\n    image: postgres:17\n    environment:\n"
            f"      POSTGRES_PASSWORD: {k['compose_pw']}\n"
        ),
        "bitbucket-pipelines.yml": (
            "pipelines:\n  default:\n    - step:\n        script:\n"
            "          - docker build --build-arg DB=$DATABASE_URL .\n"
            f"          - curl -H \"Authorization: Bearer {k['ci_token']}\" https://hooks.example.com/x\n"
        ),
        "config/prod.yml": f"database:\n  password: {k['yaml_pw']}\n  host: db\n",
        "notes/links.md": f"see https://example.com/cb?token={k['query_tok']}&a=1\n",
        "tests/test_db.py": (
            "from sqlalchemy import create_engine\n\n"
            "def test_engine():\n    create_engine('sqlite://')\n"
        ),
    }
    for rel, body in files.items():
        fp = root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, env=env, capture_output=True)
    return Leaky(slug=slug, path=root, canaries=k)
