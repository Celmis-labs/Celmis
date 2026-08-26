"""Tests для LanguageRegistry — extension/filename/content-sniff matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.extractor import ExtractionResult, SymbolExtractor
from src.indexing.graph.languages.registry import LanguageRegistry


class _StubExtractor(SymbolExtractor):
    """Mock extractor що просто запам'ятовує що його викликали."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.called_with: list[Path] = []

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        self.called_with.append(file_path)
        return ExtractionResult()


@pytest.fixture
def registry() -> LanguageRegistry:
    return LanguageRegistry()


# ─── Extension matching ─────────────────────────────────────────────


def test_extension_match_returns_extractor(registry: LanguageRegistry) -> None:
    py_ext = _StubExtractor("python")
    registry.register(py_ext, name="python", extensions=(".py", ".pyi"))

    result = registry.match(Path("src/foo.py"))
    assert result is py_ext


def test_extension_match_case_insensitive(registry: LanguageRegistry) -> None:
    py_ext = _StubExtractor("python")
    registry.register(py_ext, name="python", extensions=(".py",))

    assert registry.match(Path("FOO.PY")) is py_ext
    assert registry.match(Path("foo.Py")) is py_ext


def test_no_match_returns_none(registry: LanguageRegistry) -> None:
    registry.register(_StubExtractor("python"), name="python", extensions=(".py",))
    assert registry.match(Path("foo.rs")) is None


def test_priority_order(registry: LanguageRegistry) -> None:
    """Vue extractor має priority > generic TS — для .vue files Vue first."""
    ts = _StubExtractor("typescript")
    vue = _StubExtractor("vue")
    registry.register(ts, name="typescript", extensions=(".ts", ".vue"), priority=10)
    registry.register(vue, name="vue", extensions=(".vue",), priority=20)

    # Vue має priority 20 > TS 10 для .vue
    assert registry.match(Path("App.vue")) is vue
    # .ts — лише TS match
    assert registry.match(Path("foo.ts")) is ts


# ─── Filename pattern matching ──────────────────────────────────────


def test_filename_pattern_dockerfile(registry: LanguageRegistry) -> None:
    docker = _StubExtractor("dockerfile")
    registry.register(
        docker,
        name="dockerfile",
        filename_patterns=("Dockerfile", "Dockerfile.*", "*.Dockerfile"),
    )

    assert registry.match(Path("Dockerfile")) is docker
    assert registry.match(Path("Dockerfile.prod")) is docker
    assert registry.match(Path("api.Dockerfile")) is docker
    assert registry.match(Path("not-a-docker.txt")) is None


def test_filename_pattern_compose(registry: LanguageRegistry) -> None:
    compose = _StubExtractor("compose")
    registry.register(
        compose,
        name="compose",
        filename_patterns=("docker-compose.yml", "docker-compose.*.yml", "compose.yml"),
    )

    assert registry.match(Path("docker-compose.yml")) is compose
    assert registry.match(Path("docker-compose.prod.yml")) is compose
    assert registry.match(Path("compose.yml")) is compose
    assert registry.match(Path("random.yml")) is None


# ─── Content sniff matching ─────────────────────────────────────────


def test_content_sniff_k8s(registry: LanguageRegistry, tmp_path: Path) -> None:
    """K8s manifest — detect через apiVersion+kind у content."""
    k8s = _StubExtractor("k8s")

    def is_k8s(head: bytes) -> bool:
        text = head.decode("utf-8", errors="ignore")
        return "apiVersion:" in text and "kind:" in text

    registry.register(k8s, name="k8s", content_sniffer=is_k8s)

    # Real K8s manifest
    k8s_file = tmp_path / "deployment.yaml"
    k8s_file.write_text("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3
""")
    assert registry.match(k8s_file) is k8s

    # Random YAML — не K8s
    other_file = tmp_path / "config.yaml"
    other_file.write_text("foo: bar\nbaz: 42\n")
    assert registry.match(other_file) is None


def test_extension_match_skips_content_sniff(
    registry: LanguageRegistry, tmp_path: Path,
) -> None:
    """Якщо extension match знайдено — content sniff не запускається."""
    fast = _StubExtractor("fast")
    sniffer = _StubExtractor("sniffer")
    sniff_called = []

    def expensive_sniffer(head: bytes) -> bool:
        sniff_called.append(True)
        return True

    registry.register(fast, name="fast", extensions=(".py",), priority=10)
    registry.register(sniffer, name="sniffer", content_sniffer=expensive_sniffer, priority=5)

    py_file = tmp_path / "foo.py"
    py_file.write_text("print('hi')")
    assert registry.match(py_file) is fast
    assert sniff_called == []  # sniffer не викликався


def test_disable_sniff(registry: LanguageRegistry, tmp_path: Path) -> None:
    """read_for_sniff=False вимикає content read entirely."""
    sniffer = _StubExtractor("sniffer")
    registry.register(
        sniffer, name="sniffer", content_sniffer=lambda head: True,
    )

    f = tmp_path / "any.unknown"
    f.write_text("anything")
    # З sniff
    assert registry.match(f) is sniffer
    # Без sniff
    assert registry.match(f, read_for_sniff=False) is None


def test_unreadable_file_safe(registry: LanguageRegistry, tmp_path: Path) -> None:
    """Файл недоступний для read — не падає, повертає None."""
    sniffer = _StubExtractor("sniffer")
    registry.register(sniffer, name="sniffer", content_sniffer=lambda head: True)

    nonexistent = tmp_path / "ghost.unknown"
    # File не існує, sniff має graceful fail
    assert registry.match(nonexistent) is None


# ─── Filtering ──────────────────────────────────────────────────────


def test_filter_to_subset(registry: LanguageRegistry) -> None:
    py = _StubExtractor("python")
    go = _StubExtractor("go")
    php = _StubExtractor("php")
    registry.register(py, name="python", extensions=(".py",))
    registry.register(go, name="go", extensions=(".go",))
    registry.register(php, name="php", extensions=(".php",))

    # Залишимо тільки python + go
    filtered = registry.filter({"python", "go"})
    assert filtered.match(Path("foo.py")) is py
    assert filtered.match(Path("foo.go")) is go
    assert filtered.match(Path("foo.php")) is None  # PHP виключений


def test_names_listing(registry: LanguageRegistry) -> None:
    registry.register(_StubExtractor("a"), name="python", extensions=(".py",), priority=10)
    registry.register(_StubExtractor("b"), name="go", extensions=(".go",), priority=5)

    # Sorted by priority — python first (вищий)
    assert registry.names() == ["python", "go"]
