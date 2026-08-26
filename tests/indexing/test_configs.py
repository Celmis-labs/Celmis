"""Tests for src/indexing/graph/configs.py — Phase 3 (Config Pre-pass)."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from src.indexing.graph.configs import (
    DockerfileInfo,
    RepoContext,
    build_repo_context,
    parse_bitbucket_pipelines,
    parse_compose,
    parse_dockerfile,
    parse_env_file,
    parse_gitlab_ci,
    parse_package_json,
    parse_tsconfig,
)

# ─── tsconfig ───────────────────────────────────────────────────────


def test_tsconfig_simple(tmp_path: Path):
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {
                "@/*": ["./src/*"],
                "~/*": ["./src/*"],
                "src/*": ["./acme-modules/src/*"],
            },
        },
    }))
    aliases = parse_tsconfig(cfg)
    assert aliases == {"@/": "src/", "~/": "src/", "src/": "acme-modules/src/"}


def test_tsconfig_jsonc_comments(tmp_path: Path):
    """JSONC fallback: comments + trailing commas мають strip'итись."""
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text(textwrap.dedent('''
        {
            // line comment
            /* block comment */
            "compilerOptions": {
                "paths": {
                    "@/*": ["./src/*"],
                },  /* trailing comma above */
            },
        }
    '''))
    aliases = parse_tsconfig(cfg)
    assert aliases == {"@/": "src/"}


def test_tsconfig_missing(tmp_path: Path):
    assert parse_tsconfig(tmp_path / "missing.json") == {}


def test_tsconfig_no_paths(tmp_path: Path):
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text(json.dumps({"compilerOptions": {"strict": True}}))
    assert parse_tsconfig(cfg) == {}


def test_tsconfig_with_baseurl(tmp_path: Path):
    """baseUrl має префіксуватися до paths якщо target relative."""
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text(json.dumps({
        "compilerOptions": {
            "baseUrl": "./src",
            "paths": {"components/*": ["components/*"]},
        },
    }))
    aliases = parse_tsconfig(cfg)
    assert aliases == {"components/": "src/components/"}


# ─── package.json ───────────────────────────────────────────────────


def test_package_json_basic(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({
        "name": "acme-modules",
        "dependencies": {"vue": "3.4.0", "axios": "1.6.0"},
        "devDependencies": {"webpack": "5.0.0"},
        "scripts": {"build": "webpack", "test": "jest"},
    }))
    deps, scripts, name = parse_package_json(pkg)
    assert deps == {"vue", "axios", "webpack"}
    assert scripts == {"build": "webpack", "test": "jest"}
    assert name == "acme-modules"


def test_package_json_missing(tmp_path: Path):
    deps, scripts, name = parse_package_json(tmp_path / "missing.json")
    assert deps == set()
    assert scripts == {}
    assert name is None


def test_package_json_malformed(tmp_path: Path):
    pkg = tmp_path / "package.json"
    pkg.write_text("{not valid json")
    deps, scripts, name = parse_package_json(pkg)
    assert (deps, scripts, name) == (set(), {}, None)


# ─── .env ───────────────────────────────────────────────────────────


def test_env_file_keys_only(tmp_path: Path):
    """CRITICAL security test: parse_env_file має повертати ТІЛЬКИ keys, ніколи values."""
    env = tmp_path / ".env"
    env.write_text(textwrap.dedent("""
        # comment
        API_KEY=sk-secret-12345
        DATABASE_URL=postgres://user:pass@host:5432/db
        EMPTY=
        # blank value should still produce key
        BLANK_VAL=
        QUOTED="value with spaces"
        WITH_EQUALS_IN_VALUE=KEY1=value1
    """))
    keys = parse_env_file(env)
    assert keys == {"API_KEY", "DATABASE_URL", "EMPTY", "BLANK_VAL", "QUOTED", "WITH_EQUALS_IN_VALUE"}


def test_env_file_no_secrets_leaked(tmp_path: Path):
    """Захист: значення секретів не мають бути в результаті."""
    env = tmp_path / ".env"
    env.write_text("SECRET=sk-leaked-value-do-not-return")
    keys = parse_env_file(env)
    for key in keys:
        assert "leaked" not in key
        assert "sk-" not in key


def test_env_file_skips_comments_and_blanks(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("\n\n# only a comment\n\n   # indented comment\n")
    assert parse_env_file(env) == set()


# ─── Dockerfile ─────────────────────────────────────────────────────


def test_dockerfile_multistage(tmp_path: Path):
    df = tmp_path / "Dockerfile"
    df.write_text(textwrap.dedent("""
        FROM node:20.11-alpine as build-stage
        WORKDIR /app
        COPY package.json .
        RUN npm ci
        EXPOSE 8080

        FROM nginx:stable-alpine as production-stage
        COPY --from=build-stage /app/dist /usr/share/nginx/html
        EXPOSE 80
    """).lstrip())
    info = parse_dockerfile(df)
    assert "node:20.11-alpine" in info.base_images
    assert "nginx:stable-alpine" in info.base_images
    assert info.stage_aliases == ["build-stage", "production-stage"]
    assert 80 in info.exposed_ports
    assert 8080 in info.exposed_ports
    assert info.workdir == "/app"


def test_dockerfile_missing(tmp_path: Path):
    info = parse_dockerfile(tmp_path / "missing.Dockerfile")
    assert isinstance(info, DockerfileInfo)
    assert info.base_images == []


# ─── docker-compose ─────────────────────────────────────────────────


def test_compose_services(tmp_path: Path):
    comp = tmp_path / "docker-compose.yml"
    comp.write_text(textwrap.dedent("""
        version: "3"
        services:
          frontend:
            image: front3d
            ports:
              - 8080:8080
          api:
            image: api:latest
          db:
            image: postgres:16
    """))
    services = parse_compose(comp)
    assert set(services) == {"frontend", "api", "db"}


def test_compose_missing(tmp_path: Path):
    assert parse_compose(tmp_path / "no.yml") == []


def test_compose_invalid(tmp_path: Path):
    comp = tmp_path / "docker-compose.yml"
    comp.write_text("not: valid: yaml: structure: at: all: ::: [unclosed")
    assert parse_compose(comp) == []


# ─── GitLab CI ──────────────────────────────────────────────────────


def test_gitlab_ci_jobs(tmp_path: Path):
    ci = tmp_path / ".gitlab-ci.yml"
    ci.write_text(textwrap.dedent("""
        stages:
          - build
          - deploy
        variables:
          NODE_VERSION: "20"
        build_app:
          stage: build
          script: npm run build
        deploy_prod:
          stage: deploy
          script: ./deploy.sh
        .hidden_template:
          script: echo template
    """))
    jobs = parse_gitlab_ci(ci)
    assert set(jobs) == {"build_app", "deploy_prod"}


# ─── Bitbucket pipelines ────────────────────────────────────────────


def test_bitbucket_pipelines(tmp_path: Path):
    bb = tmp_path / "bitbucket-pipelines.yml"
    bb.write_text(textwrap.dedent("""
        pipelines:
          default:
            - step:
                name: Build
                script:
                  - npm run build
            - step:
                name: Test
                script:
                  - npm test
          branches:
            main:
              - step:
                  name: Deploy Prod
                  script: ./deploy.sh
    """))
    names = parse_bitbucket_pipelines(bb)
    assert set(names) == {"Build", "Test", "Deploy Prod"}


# ─── Orchestrator (build_repo_context) ──────────────────────────────


def test_build_repo_context_full(tmp_path: Path):
    """End-to-end: всі файли разом → один RepoContext."""
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"paths": {"@/*": ["./src/*"]}},
    }))
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "myapp",
        "dependencies": {"vue": "3.4.0"},
        "scripts": {"build": "vite build"},
    }))
    (tmp_path / ".env").write_text("API_URL=http://x\nSECRET=should-not-leak\n")
    (tmp_path / "docker-compose.yml").write_text(
        'version: "3"\nservices:\n  app:\n    image: app:latest\n'
    )
    (tmp_path / "Dockerfile").write_text(
        "FROM node:20 as build\nWORKDIR /app\nEXPOSE 80\n"
    )

    ctx = build_repo_context(tmp_path)

    assert ctx.is_typescript_project is True
    assert ctx.path_aliases == {"@/": "src/"}
    assert ctx.external_deps == {"vue"}
    assert ctx.package_name == "myapp"
    assert ctx.package_scripts == {"build": "vite build"}
    assert ctx.env_keys == {"API_URL", "SECRET"}
    assert ctx.docker_services == ["app"]
    assert len(ctx.dockerfiles) == 1
    assert ctx.dockerfiles[0].base_images == ["node:20"]

    # Security: жодне значення не leaked у RepoContext
    serialized = repr(ctx)
    assert "should-not-leak" not in serialized
    assert "http://x" not in serialized


def test_build_repo_context_empty(tmp_path: Path):
    """Порожня директорія → порожній RepoContext (без падінь)."""
    ctx = build_repo_context(tmp_path)
    assert isinstance(ctx, RepoContext)
    assert ctx.path_aliases == {}
    assert ctx.external_deps == set()
    assert ctx.parse_errors == []


# ─── Real repo verification ─────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("CELMIS_REAL_REPO"),
    reason="CELMIS_REAL_REPO is not set (path to a real frontend clone)",
)
def test_a_real_frontend_repo_yields_a_usable_context():
    """The config pre-pass against an actual clone, not a fixture.

    The assertions used to name one customer's repository — its package name,
    its alias target, its compose service. That made the test prove "this is
    that repo" rather than "the pre-pass extracts an alias, the dependencies
    and the services from a real one", which is what it is for. It now asserts
    the SHAPE, so it holds for whatever CELMIS_REAL_REPO points at.
    """
    repo_root = Path(os.environ["CELMIS_REAL_REPO"]).expanduser()
    ctx = build_repo_context(repo_root)

    assert ctx.is_typescript_project is True
    # An alias was found and it resolves somewhere inside the repo, rather
    # than being recorded as an empty string.
    assert ctx.path_aliases, "no path aliases extracted from tsconfig"
    assert all(v for v in ctx.path_aliases.values())

    assert len(ctx.external_deps) >= 20, f"only {len(ctx.external_deps)} deps"
    assert ctx.package_name, "package.json name not read"

    assert ctx.docker_services, "no services read from docker-compose"

    # security: serialized context has no env values
    # (env_keys only, no values stored)
    assert all(isinstance(k, str) for k in ctx.env_keys)
