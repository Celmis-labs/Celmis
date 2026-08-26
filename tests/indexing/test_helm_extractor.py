"""Tests для HelmExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.helm import HelmExtractor, is_helm_chart_yaml


@pytest.fixture
def extractor() -> HelmExtractor:
    return HelmExtractor()


def _extract(
    extractor: HelmExtractor, source: str, file_name: str = "Chart.yaml",
):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Content sniff ──────────────────────────────────────────────────


class TestContentSniff:
    def test_chart_yaml_detected(self) -> None:
        content = b"""
apiVersion: v2
name: my-chart
version: 1.2.3
"""
        assert is_helm_chart_yaml(content) is True

    def test_k8s_manifest_rejected(self) -> None:
        """K8s має `kind:` field — disambiguates."""
        content = b"""
apiVersion: v1
kind: Service
metadata:
  name: api
"""
        assert is_helm_chart_yaml(content) is False

    def test_compose_rejected(self) -> None:
        content = b"services:\n  web:\n    image: nginx\n"
        assert is_helm_chart_yaml(content) is False

    def test_random_yaml_rejected(self) -> None:
        content = b"foo: bar\nbaz: 42\n"
        assert is_helm_chart_yaml(content) is False


# ─── Chart.yaml ─────────────────────────────────────────────────────


class TestChartYaml:
    def test_simple_chart(self, extractor: HelmExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v2
name: my-app
version: 1.0.0
appVersion: "2.5.0"
description: My application chart
""")
        charts = [s for s in res.symbols if s.kind == "vendor.helm.chart"]
        assert len(charts) == 1
        assert charts[0].name == "my-app"
        assert charts[0].module == "1.0.0"  # chart version

    def test_chart_with_dependencies(self, extractor: HelmExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v2
name: my-app
version: 1.0.0

dependencies:
  - name: postgresql
    version: "12.5.0"
    repository: https://charts.bitnami.com/bitnami
  - name: redis
    version: "17.3.0"
    repository: https://charts.bitnami.com/bitnami
""")
        # 1 chart + 2 dependencies
        charts = [s for s in res.symbols if s.kind == "vendor.helm.chart"]
        deps = [s for s in res.symbols if s.kind == "vendor.helm.dependency"]
        assert len(charts) == 1
        assert len(deps) == 2

        dep_names = {d.name for d in deps}
        assert dep_names == {"postgresql", "redis"}

        # IMPORTS edges from chart → repos
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 2
        targets = {e.raw_target for e in imports}
        assert "https://charts.bitnami.com/bitnami::postgresql@12.5.0" in targets
        assert "https://charts.bitnami.com/bitnami::redis@17.3.0" in targets

    def test_chart_no_repository_no_imports(self, extractor: HelmExtractor) -> None:
        """Local sub-chart (file:// або relative) без repository — no IMPORTS edge."""
        res = _extract(extractor, """
apiVersion: v2
name: parent
version: 1.0.0

dependencies:
  - name: local-sub
    version: "0.1.0"
""")
        deps = [s for s in res.symbols if s.kind == "vendor.helm.dependency"]
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(deps) == 1  # registered
        assert len(imports) == 0  # no edge


# ─── values.yaml ────────────────────────────────────────────────────


class TestValuesYaml:
    def test_top_level_keys(self, extractor: HelmExtractor) -> None:
        res = _extract(extractor, """
replicaCount: 3
image:
  repository: myorg/myapp
  tag: v1.0
service:
  type: ClusterIP
  port: 80
ingress:
  enabled: false
""", file_name="values.yaml")
        values = [s for s in res.symbols if s.kind == "vendor.helm.value"]
        names = {v.name for v in values}
        assert names == {"replicaCount", "image", "service", "ingress"}

    def test_empty_values(self, extractor: HelmExtractor) -> None:
        res = _extract(extractor, "{}", file_name="values.yaml")
        values = [s for s in res.symbols if s.kind == "vendor.helm.value"]
        assert values == []


# ─── requirements.yaml (legacy Helm 2) ─────────────────────────────


class TestRequirementsYaml:
    def test_legacy_dependencies(self, extractor: HelmExtractor) -> None:
        res = _extract(extractor, """
dependencies:
  - name: mysql
    version: "1.6.6"
    repository: https://charts.helm.sh/stable
""", file_name="requirements.yaml")
        deps = [s for s in res.symbols if s.kind == "vendor.helm.dependency"]
        assert deps[0].name == "mysql"

        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert "mysql" in imports[0].raw_target


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_chart_smoke(self, extractor: HelmExtractor) -> None:
        chart_source = """
apiVersion: v2
name: acme-platform
version: 2.3.1
appVersion: "1.5.0"
description: Acme Pro продукт chart
type: application
keywords:
  - acme
  - api

dependencies:
  - name: postgresql
    version: "12.5.0"
    repository: https://charts.bitnami.com/bitnami
    condition: postgresql.enabled
  - name: redis
    version: "17.3.0"
    repository: oci://registry-1.docker.io/bitnamicharts
    condition: redis.enabled
"""
        res = extractor.extract(Path("Chart.yaml"), source=chart_source.encode("utf-8"))
        assert not res.parse_errors

        # Chart + 2 deps
        charts = [s for s in res.symbols if s.kind == "vendor.helm.chart"]
        deps = [s for s in res.symbols if s.kind == "vendor.helm.dependency"]
        assert charts[0].name == "acme-platform"
        assert charts[0].module == "2.3.1"
        assert {d.name for d in deps} == {"postgresql", "redis"}

        # IMPORTS включно з OCI URL
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert any("bitnami" in t for t in imports)
        assert any("oci://" in t for t in imports)
