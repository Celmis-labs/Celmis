"""Tests для CIWorkflowExtractor — GitHub Actions / GitLab CI / Bitbucket Pipelines."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.ci_workflow import (
    CIWorkflowExtractor,
    _detect_provider,
    is_ci_workflow_file,
)


@pytest.fixture
def extractor() -> CIWorkflowExtractor:
    return CIWorkflowExtractor()


def _extract(extractor: CIWorkflowExtractor, source: str, file_path: str):
    return extractor.extract(Path(file_path), source=source.encode("utf-8"))


# ─── Provider detection ────────────────────────────────────────────


class TestProviderDetection:
    @pytest.mark.parametrize("path,expected", [
        (".github/workflows/ci.yml", "github_actions"),
        (".github/workflows/release.yaml", "github_actions"),
        (".gitlab-ci.yml", "gitlab_ci"),
        (".gitlab-ci.yaml", "gitlab_ci"),
        ("bitbucket-pipelines.yml", "bitbucket_pipelines"),
        ("docker-compose.yml", None),
        ("random.yaml", None),
        ("workflows/ci.yml", None),  # NOT under .github
    ])
    def test_provider(self, path: str, expected: str | None) -> None:
        assert _detect_provider(Path(path)) == expected

    def test_is_ci_workflow_file(self) -> None:
        assert is_ci_workflow_file(Path(".github/workflows/ci.yml")) is True
        assert is_ci_workflow_file(Path("docker-compose.yml")) is False


# ─── GitHub Actions ─────────────────────────────────────────────────


class TestGitHubActions:
    def test_basic_workflow(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Run tests
        run: pytest
""", ".github/workflows/ci.yml")

        # 1 workflow + 1 job
        wf = [s for s in res.symbols if s.kind == "vendor.ci.workflow"]
        jobs = [s for s in res.symbols if s.kind == "vendor.ci.job"]
        assert wf[0].name == "CI"
        assert jobs[0].name == "test"
        assert jobs[0].module == "ubuntu-latest"

        # 2 IMPORTS edges (uses: refs)
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert imports == {"actions/checkout@v4", "actions/setup-python@v5"}

    def test_multi_job_with_needs(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
name: Pipeline
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
  test:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  deploy:
    needs: [build, test]
    runs-on: ubuntu-latest
    steps:
      - uses: org/deploy-action@v1
""", ".github/workflows/pipeline.yml")

        jobs = [s for s in res.symbols if s.kind == "vendor.ci.job"]
        names = {j.name for j in jobs}
        assert names == {"build", "test", "deploy"}

        # DEPLOYS edges = job dependencies
        deploys = [e for e in res.edges if e.kind == "DEPLOYS"]
        targets = {e.raw_target for e in deploys}
        # test → build, deploy → build, deploy → test
        assert "job.build" in targets
        assert "job.test" in targets

    def test_container_image(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    container:
      image: python:3.13-slim
    steps:
      - run: pytest
""", ".github/workflows/ci.yml")

        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        assert runs[0].raw_target == "python:3.13-slim"

    def test_reusable_workflow_uses(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
name: Caller
on: push
jobs:
  call-shared:
    uses: org/.github/workflows/shared.yml@main
""", ".github/workflows/main.yml")

        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "org/.github/workflows/shared.yml@main"


# ─── GitLab CI ──────────────────────────────────────────────────────


class TestGitLabCI:
    def test_basic_jobs(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
stages:
  - test
  - deploy

variables:
  PYTHON_VERSION: "3.13"

test:
  stage: test
  image: python:3.13
  script:
    - pytest

deploy:
  stage: deploy
  image: registry.gitlab.com/myorg/deployer:v1
  script:
    - ./deploy.sh
""", ".gitlab-ci.yml")

        jobs = [s for s in res.symbols if s.kind == "vendor.ci.job"]
        names = {j.name for j in jobs}
        assert names == {"test", "deploy"}

        # Stage tracking via module field
        test_job = next(j for j in jobs if j.name == "test")
        assert test_job.module == "test"

        runs = {e.raw_target for e in res.edges if e.kind == "RUNS_IMAGE"}
        assert "python:3.13" in runs
        assert "registry.gitlab.com/myorg/deployer:v1" in runs

    def test_includes(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
include:
  - local: '/templates/test.yml'
  - project: 'shared/templates'
    file: 'pipeline.yml'
  - remote: 'https://example.com/pipeline.yml'
  - template: 'Auto-DevOps.gitlab-ci.yml'

test:
  script: pytest
""", ".gitlab-ci.yml")

        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "/templates/test.yml" in imports
        assert "shared/templates::pipeline.yml" in imports
        assert "https://example.com/pipeline.yml" in imports
        assert "template:Auto-DevOps.gitlab-ci.yml" in imports

    def test_extends_inheritance(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
.base:
  image: python:3.13

test:
  extends: .base
  script: pytest

deploy:
  extends:
    - .base
    - .deploy_template
  script: ./deploy.sh
""", ".gitlab-ci.yml")

        # `.base` — hidden template, skip
        jobs = [s for s in res.symbols if s.kind == "vendor.ci.job"]
        names = {j.name for j in jobs}
        assert names == {"test", "deploy"}  # `.base` skipped

        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        targets = {e.raw_target for e in imports}
        assert "extends:.base" in targets
        assert "extends:.deploy_template" in targets

    def test_image_object_form(self, extractor: CIWorkflowExtractor) -> None:
        """`image: { name: foo }` long syntax."""
        res = _extract(extractor, """
test:
  image:
    name: python:3.13
    entrypoint: [""]
  script: pytest
""", ".gitlab-ci.yml")

        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        assert runs[0].raw_target == "python:3.13"


# ─── Bitbucket Pipelines ────────────────────────────────────────────


class TestBitbucketPipelines:
    def test_basic_pipeline(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
image: python:3.13

pipelines:
  default:
    - step:
        name: Test
        script:
          - pytest
    - step:
        name: Build
        image: docker:24
        script:
          - docker build .
""", "bitbucket-pipelines.yml")

        steps = [s for s in res.symbols if s.kind == "vendor.ci.step"]
        names = {s.name for s in steps}
        assert names == {"Test", "Build"}

        runs = {e.raw_target for e in res.edges if e.kind == "RUNS_IMAGE"}
        # Top-level image + per-step override
        assert "python:3.13" in runs
        assert "docker:24" in runs

    def test_branch_specific_pipeline(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
pipelines:
  branches:
    main:
      - step:
          name: Deploy prod
          script:
            - ./deploy.sh prod
    develop:
      - step:
          name: Deploy staging
          script:
            - ./deploy.sh staging
""", "bitbucket-pipelines.yml")

        steps = [s for s in res.symbols if s.kind == "vendor.ci.step"]
        names = {s.name for s in steps}
        assert names == {"Deploy prod", "Deploy staging"}

    def test_pipe_imports(self, extractor: CIWorkflowExtractor) -> None:
        """Bitbucket `pipe:` references — як GitHub Actions `uses:`."""
        res = _extract(extractor, """
pipelines:
  default:
    - step:
        name: Build
        script:
          - pipe: docker/docker-build@2.0.0
            variables:
              IMAGE: myorg/myapp
          - pipe: atlassian/aws-s3-deploy@1.5.0
""", "bitbucket-pipelines.yml")

        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "docker/docker-build@2.0.0" in imports
        assert "atlassian/aws-s3-deploy@1.5.0" in imports

    def test_parallel_steps(self, extractor: CIWorkflowExtractor) -> None:
        res = _extract(extractor, """
pipelines:
  default:
    - parallel:
        - step:
            name: Lint
            script: [ruff check]
        - step:
            name: Type check
            script: [mypy]
    - step:
        name: Test
        script: [pytest]
""", "bitbucket-pipelines.yml")

        steps = [s for s in res.symbols if s.kind == "vendor.ci.step"]
        names = {s.name for s in steps}
        assert names == {"Lint", "Type check", "Test"}


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_github_workflow(self, extractor: CIWorkflowExtractor) -> None:
        source = """
name: Full CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: chartboost/ruff-action@v1

  test:
    needs: lint
    runs-on: ubuntu-latest
    container:
      image: python:3.13-slim
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pytest

  deploy:
    needs: [lint, test]
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v4
      - uses: org/deploy-action@v2
"""
        res = extractor.extract(
            Path(".github/workflows/ci.yml"),
            source=source.encode("utf-8"),
        )
        assert not res.parse_errors

        wf = [s for s in res.symbols if s.kind == "vendor.ci.workflow"]
        jobs = [s for s in res.symbols if s.kind == "vendor.ci.job"]
        assert wf[0].name == "Full CI"
        assert {j.name for j in jobs} == {"lint", "test", "deploy"}

        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "actions/checkout@v4" in imports
        assert "actions/setup-python@v5" in imports
        assert "org/deploy-action@v2" in imports

        # Job dependencies
        deploys = [e for e in res.edges if e.kind == "DEPLOYS"]
        assert len(deploys) >= 3  # test→lint, deploy→lint, deploy→test

        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        assert any(e.raw_target == "python:3.13-slim" for e in runs)
