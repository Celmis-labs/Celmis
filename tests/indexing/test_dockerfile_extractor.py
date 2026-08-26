"""Tests для DockerfileExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.dockerfile import DockerfileExtractor


@pytest.fixture
def extractor() -> DockerfileExtractor:
    return DockerfileExtractor()


def _extract(extractor: DockerfileExtractor, source: str, file_name: str = "Dockerfile"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── FROM ────────────────────────────────────────────────────────────


class TestFrom:
    def test_simple_from(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, "FROM python:3.13-slim\n")
        images = [s for s in res.symbols if s.kind == "image"]
        assert len(images) == 1
        # Без alias → auto name 'stage_0'
        assert images[0].name == "stage_0"

        built_from = [e for e in res.edges if e.kind == "BUILT_FROM"]
        assert built_from[0].raw_target == "python:3.13-slim"

    def test_from_with_alias(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, "FROM python:3.13-slim AS builder\n")
        images = [s for s in res.symbols if s.kind == "image"]
        assert images[0].name == "builder"

    def test_multi_stage(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM python:3.13 AS builder
RUN pip install foo

FROM python:3.13-slim AS runtime
COPY --from=builder /app /app
""")
        images = [s for s in res.symbols if s.kind == "image"]
        assert len(images) == 2
        names = [i.name for i in images]
        assert names == ["builder", "runtime"]

        built_from = [e for e in res.edges if e.kind == "BUILT_FROM"]
        targets = {e.raw_target for e in built_from}
        assert targets == {"python:3.13", "python:3.13-slim"}

    def test_from_without_alias_auto_naming(self, extractor: DockerfileExtractor) -> None:
        """3 FROM без alias → stage_0, stage_1, stage_2."""
        res = _extract(extractor, """
FROM alpine:3.20
FROM ubuntu:24.04
FROM debian:bookworm
""")
        images = [s for s in res.symbols if s.kind == "image"]
        names = [i.name for i in images]
        assert names == ["stage_0", "stage_1", "stage_2"]


# ─── EXPOSE ─────────────────────────────────────────────────────────


class TestExpose:
    def test_simple_expose(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM alpine
EXPOSE 8000
""")
        exposes = [e for e in res.edges if e.kind == "EXPOSES"]
        assert exposes[0].raw_target == "8000"

    def test_multi_port_expose(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM alpine
EXPOSE 8000 8080/tcp 9090/udp
""")
        exposes = [e for e in res.edges if e.kind == "EXPOSES"]
        targets = {e.raw_target for e in exposes}
        assert targets == {"8000", "8080/tcp", "9090/udp"}

    def test_expose_attached_to_correct_stage(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM alpine AS s1
EXPOSE 8000

FROM alpine AS s2
EXPOSE 9000
""")
        # Кожен EXPOSE attached до свого stage
        exposes = [e for e in res.edges if e.kind == "EXPOSES"]
        # s1 image id ends with 's1', s2 with 's2'
        s1_exposes = [e for e in exposes if e.from_id.endswith("s1")]
        s2_exposes = [e for e in exposes if e.from_id.endswith("s2")]
        assert s1_exposes[0].raw_target == "8000"
        assert s2_exposes[0].raw_target == "9000"


# ─── COPY --from ────────────────────────────────────────────────────


class TestCopyFrom:
    def test_copy_from_stage(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM golang AS builder
RUN go build

FROM alpine AS runtime
COPY --from=builder /app /app
""")
        copies = [e for e in res.edges if e.kind == "COPIES_FROM"]
        assert len(copies) == 1
        assert copies[0].raw_target == "builder"

    def test_regular_copy_no_edge(self, extractor: DockerfileExtractor) -> None:
        """COPY без --from не створює COPIES_FROM edge."""
        res = _extract(extractor, """
FROM alpine
COPY src/ /app/src/
""")
        copies = [e for e in res.edges if e.kind == "COPIES_FROM"]
        assert len(copies) == 0


# ─── ADD URL ────────────────────────────────────────────────────────


class TestAddUrl:
    def test_add_url_creates_imports(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM alpine
ADD https://example.com/file.tar.gz /tmp/
""")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "https://example.com/file.tar.gz"

    def test_add_local_no_imports(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM alpine
ADD localfile.tar.gz /tmp/
""")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 0


# ─── ENV ────────────────────────────────────────────────────────────


class TestEnv:
    def test_env_pair(self, extractor: DockerfileExtractor) -> None:
        res = _extract(extractor, """
FROM alpine
ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production
""")
        env_edges = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in env_edges}
        assert targets == {"PYTHONUNBUFFERED", "NODE_ENV"}

    def test_env_multi_pair_one_instruction(self, extractor: DockerfileExtractor) -> None:
        """`ENV K1=V1 K2=V2` — multiple pairs у одній instruction."""
        res = _extract(extractor, """
FROM alpine
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
""")
        env_edges = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in env_edges}
        assert targets == {"PYTHONUNBUFFERED", "PIP_NO_CACHE_DIR"}


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_dockerfile_smoke(self, extractor: DockerfileExtractor) -> None:
        source = '''ARG VERSION=3.13
FROM python:${VERSION}-slim AS builder
LABEL maintainer="dev@example.com"

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY --chown=app:app pyproject.toml ./
COPY src/ ./src/
ADD https://example.com/file.tar.gz /tmp/

RUN pip install -r requirements.txt

USER app
EXPOSE 8000 8080/tcp
VOLUME /data /logs

ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["app:app", "--host", "0.0.0.0"]

FROM builder AS runtime
COPY --from=builder /app /app
'''
        res = extractor.extract(Path("Dockerfile"), source=source.encode("utf-8"))
        assert not res.parse_errors

        # Symbols: file_module + 2 images (builder, runtime)
        kinds = [s.kind for s in res.symbols]
        assert kinds.count("image") == 2
        names = [s.name for s in res.symbols if s.kind == "image"]
        assert names == ["builder", "runtime"]

        # BUILT_FROM
        built_from = {e.raw_target for e in res.edges if e.kind == "BUILT_FROM"}
        assert "python:${VERSION}-slim" in built_from
        assert "builder" in built_from  # runtime extends builder

        # EXPOSES
        exposes = {e.raw_target for e in res.edges if e.kind == "EXPOSES"}
        assert exposes == {"8000", "8080/tcp"}

        # COPIES_FROM
        copies = {e.raw_target for e in res.edges if e.kind == "COPIES_FROM"}
        assert "builder" in copies

        # IMPORTS (URL)
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "https://example.com/file.tar.gz" in imports

        # ENV USES_CONFIG
        env_targets = {e.raw_target for e in res.edges if e.kind == "USES_CONFIG"}
        assert {"PYTHONUNBUFFERED", "PIP_NO_CACHE_DIR"} <= env_targets
