"""Tests для DockerComposeExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.compose import DockerComposeExtractor


@pytest.fixture
def extractor() -> DockerComposeExtractor:
    return DockerComposeExtractor()


def _extract(extractor: DockerComposeExtractor, source: str, file_name: str = "docker-compose.yml"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Services ───────────────────────────────────────────────────────


class TestServices:
    def test_simple_service(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  web:
    image: nginx:latest
""")
        services = [s for s in res.symbols if s.kind == "service"]
        assert len(services) == 1
        assert services[0].name == "web"

    def test_multiple_services(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  web:
    image: nginx
  db:
    image: postgres:16
  redis:
    image: redis:7
""")
        services = [s for s in res.symbols if s.kind == "service"]
        names = {s.name for s in services}
        assert names == {"web", "db", "redis"}

    def test_image_runs_image_edge(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    image: myapp:v2.3
""")
        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        assert runs[0].raw_target == "myapp:v2.3"

    def test_build_short_syntax(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    build: ./backend
""")
        built = [e for e in res.edges if e.kind == "BUILT_FROM"]
        assert built[0].raw_target == "./backend/Dockerfile"

    def test_build_long_syntax(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    build:
      context: ./backend
      dockerfile: Dockerfile.prod
""")
        built = [e for e in res.edges if e.kind == "BUILT_FROM"]
        assert built[0].raw_target == "./backend/Dockerfile.prod"


# ─── Ports ──────────────────────────────────────────────────────────


class TestPorts:
    def test_ports_string(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  web:
    image: nginx
    ports:
      - "8080:80"
      - "443:443"
""")
        exposes = [e for e in res.edges if e.kind == "EXPOSES"]
        targets = {e.raw_target for e in exposes}
        assert targets == {"8080:80", "443:443"}

    def test_ports_long_syntax(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  web:
    image: nginx
    ports:
      - target: 80
        published: 8080
        protocol: tcp
""")
        exposes = [e for e in res.edges if e.kind == "EXPOSES"]
        assert exposes[0].raw_target == "8080:80"


# ─── depends_on ─────────────────────────────────────────────────────


class TestDependsOn:
    def test_depends_on_list(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  web:
    image: nginx
    depends_on:
      - db
      - redis
  db:
    image: postgres
  redis:
    image: redis
""")
        deps = [e for e in res.edges if e.kind == "DEPLOYS"]
        targets = {e.raw_target for e in deps}
        assert targets == {"db", "redis"}

    def test_depends_on_long_syntax(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  web:
    image: nginx
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
""")
        deps = [e for e in res.edges if e.kind == "DEPLOYS"]
        targets = {e.raw_target for e in deps}
        assert targets == {"db", "redis"}


# ─── Environment ────────────────────────────────────────────────────


class TestEnvironment:
    def test_env_list(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    image: myapp
    environment:
      - NODE_ENV=production
      - PORT=3000
      - LOG_LEVEL
""")
        env = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in env}
        assert targets == {"NODE_ENV", "PORT", "LOG_LEVEL"}

    def test_env_dict(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    image: myapp
    environment:
      DATABASE_URL: postgres://...
      SECRET_KEY: changeme
""")
        env = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in env}
        assert targets == {"DATABASE_URL", "SECRET_KEY"}

    def test_env_file_string(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    image: myapp
    env_file: .env.production
""")
        env = [e for e in res.edges if e.kind == "USES_CONFIG"]
        assert env[0].raw_target == "file:.env.production"

    def test_env_file_list(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  api:
    image: myapp
    env_file:
      - .env
      - .env.production
""")
        env = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in env}
        assert targets == {"file:.env", "file:.env.production"}


# ─── Volumes ────────────────────────────────────────────────────────


class TestVolumes:
    def test_volumes_short(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  db:
    image: postgres
    volumes:
      - pgdata:/var/lib/postgresql/data
      - /host/path:/container/path
""")
        mounts = [e for e in res.edges if e.kind == "MOUNTS"]
        targets = {e.raw_target for e in mounts}
        assert targets == {
            "pgdata:/var/lib/postgresql/data",
            "/host/path:/container/path",
        }

    def test_volumes_long(self, extractor: DockerComposeExtractor) -> None:
        res = _extract(extractor, """
services:
  db:
    image: postgres
    volumes:
      - type: volume
        source: pgdata
        target: /var/lib/postgresql/data
""")
        mounts = [e for e in res.edges if e.kind == "MOUNTS"]
        assert mounts[0].raw_target == "pgdata:/var/lib/postgresql/data"


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_compose_smoke(self, extractor: DockerComposeExtractor) -> None:
        source = """
services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - api
    environment:
      NGINX_HOST: example.com
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf

  api:
    build:
      context: ./backend
      dockerfile: Dockerfile.prod
    ports:
      - "3000:3000"
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
    env_file: .env.production
    environment:
      - DATABASE_URL=postgres://db:5432/myapp

  db:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7

volumes:
  pgdata: {}
"""
        res = extractor.extract(Path("compose.yml"), source=source.encode("utf-8"))
        assert not res.parse_errors

        services = [s for s in res.symbols if s.kind == "service"]
        names = {s.name for s in services}
        assert names == {"web", "api", "db", "redis"}

        # web RUNS_IMAGE nginx
        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        run_targets = {e.raw_target for e in runs}
        assert run_targets == {"nginx:latest", "postgres:16", "redis:7"}

        # api BUILT_FROM
        built = [e for e in res.edges if e.kind == "BUILT_FROM"]
        assert any(e.raw_target == "./backend/Dockerfile.prod" for e in built)

        # web depends_on api; api depends_on db, redis
        deps = [e for e in res.edges if e.kind == "DEPLOYS"]
        dep_targets = {e.raw_target for e in deps}
        assert dep_targets == {"api", "db", "redis"}

        # ports
        exposes = {e.raw_target for e in res.edges if e.kind == "EXPOSES"}
        assert "80:80" in exposes
        assert "443:443" in exposes
        assert "3000:3000" in exposes

        # env config
        env_targets = {e.raw_target for e in res.edges if e.kind == "USES_CONFIG"}
        assert "NGINX_HOST" in env_targets
        assert "DATABASE_URL" in env_targets
        assert "file:.env.production" in env_targets

        # volumes
        mounts = {e.raw_target for e in res.edges if e.kind == "MOUNTS"}
        assert "pgdata:/var/lib/postgresql/data" in mounts
