"""Tests для K8sManifestExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.k8s import K8sManifestExtractor, is_k8s_manifest


@pytest.fixture
def extractor() -> K8sManifestExtractor:
    return K8sManifestExtractor()


def _extract(extractor: K8sManifestExtractor, source: str, file_name: str = "manifest.yaml"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Content sniff ──────────────────────────────────────────────────


class TestContentSniff:
    def test_k8s_manifest_detected(self) -> None:
        head = b"apiVersion: v1\nkind: Service\n"
        assert is_k8s_manifest(head) is True

    def test_random_yaml_rejected(self) -> None:
        head = b"foo: bar\nbaz: 42\n"
        assert is_k8s_manifest(head) is False

    def test_compose_rejected(self) -> None:
        head = b"services:\n  web:\n    image: nginx\n"
        assert is_k8s_manifest(head) is False


# ─── Single document ────────────────────────────────────────────────


class TestSingleDocument:
    def test_simple_pod(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: Pod
metadata:
  name: my-pod
  namespace: default
spec:
  containers:
    - name: app
      image: nginx:latest
""")
        deployments = [s for s in res.symbols if s.kind == "deployment"]
        containers = [s for s in res.symbols if s.kind == "container"]
        assert deployments[0].name == "my-pod"
        assert deployments[0].module == "default"
        assert containers[0].name == "app"

        # RUNS_IMAGE
        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        assert runs[0].raw_target == "nginx:latest"

    def test_deployment_with_replicas(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 3
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: myorg/api:v2
        - name: sidecar
          image: envoyproxy/envoy:v1.28
""")
        deployments = [s for s in res.symbols if s.kind == "deployment"]
        containers = [s for s in res.symbols if s.kind == "container"]
        assert deployments[0].name == "api"
        names = {c.name for c in containers}
        assert names == {"api", "sidecar"}

        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        targets = {e.raw_target for e in runs}
        assert targets == {"myorg/api:v2", "envoyproxy/envoy:v1.28"}

    def test_service_selector(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: Service
metadata:
  name: api-svc
spec:
  selector:
    app: api
    tier: backend
  ports:
    - port: 80
""")
        services = [s for s in res.symbols if s.kind == "service"]
        assert services[0].name == "api-svc"

        selects = [e for e in res.edges if e.kind == "SELECTS"]
        # Sorted keys: app=api,tier=backend
        assert selects[0].raw_target == "labels:app=api,tier=backend"

    def test_configmap(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  KEY1: value1
  KEY2: value2
""")
        cms = [s for s in res.symbols if s.kind == "configmap"]
        assert cms[0].name == "app-config"

    def test_secret(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: Secret
metadata:
  name: api-keys
type: Opaque
""")
        secrets = [s for s in res.symbols if s.kind == "secret"]
        assert secrets[0].name == "api-keys"

    def test_ingress_routes_to_service(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web-ingress
spec:
  rules:
    - host: example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: api-svc
                port:
                  number: 80
          - path: /admin
            pathType: Prefix
            backend:
              service:
                name: admin-svc
                port:
                  number: 8080
""")
        ingresses = [s for s in res.symbols if s.kind == "ingress"]
        assert ingresses[0].name == "web-ingress"

        selects = [e for e in res.edges if e.kind == "SELECTS"]
        targets = {e.raw_target for e in selects}
        assert targets == {"service:api-svc", "service:admin-svc"}


# ─── Multi-document ──────────────────────────────────────────────────


class TestMultiDocument:
    def test_multi_resource_file(self, extractor: K8sManifestExtractor) -> None:
        """Один YAML file з 3 resources через `---`."""
        res = _extract(extractor, """
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  K: V
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  selector:
    matchLabels:
      app: api
  template:
    spec:
      containers:
        - name: app
          image: myapp:1.0
---
apiVersion: v1
kind: Service
metadata:
  name: api-svc
spec:
  selector:
    app: api
""")
        kinds = {s.kind for s in res.symbols}
        assert "configmap" in kinds
        assert "deployment" in kinds
        assert "service" in kinds
        assert "container" in kinds


# ─── Container env / volumes ────────────────────────────────────────


class TestContainerEnv:
    def test_envfrom_configmap_ref(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  selector: {matchLabels: {app: api}}
  template:
    spec:
      containers:
        - name: api
          image: myapp
          envFrom:
            - configMapRef:
                name: app-config
            - secretRef:
                name: api-secrets
""")
        uses = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in uses}
        assert "configmap:app-config" in targets
        assert "secret:api-secrets" in targets

    def test_env_value_from(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: Pod
metadata:
  name: p
spec:
  containers:
    - name: c
      image: myapp
      env:
        - name: DB_URL
          valueFrom:
            configMapKeyRef:
              name: app-cfg
              key: db_url
        - name: API_KEY
          valueFrom:
            secretKeyRef:
              name: secrets
              key: api_key
""")
        uses = [e for e in res.edges if e.kind == "USES_CONFIG"]
        targets = {e.raw_target for e in uses}
        assert "configmap:app-cfg" in targets
        assert "secret:secrets" in targets


class TestVolumeMounts:
    def test_configmap_volume_mount(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: Pod
metadata:
  name: p
spec:
  containers:
    - name: c
      image: myapp
      volumeMounts:
        - name: config-vol
          mountPath: /etc/config
        - name: data-vol
          mountPath: /data
  volumes:
    - name: config-vol
      configMap:
        name: app-config
    - name: data-vol
      persistentVolumeClaim:
        claimName: data-pvc
""")
        mounts = [e for e in res.edges if e.kind == "MOUNTS"]
        targets = {e.raw_target for e in mounts}
        assert "configmap:app-config" in targets
        assert "pvc:data-pvc" in targets

    def test_secret_volume_mount(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: v1
kind: Pod
metadata:
  name: p
spec:
  containers:
    - name: c
      image: myapp
      volumeMounts:
        - name: tls
          mountPath: /etc/tls
  volumes:
    - name: tls
      secret:
        secretName: tls-cert
""")
        mounts = [e for e in res.edges if e.kind == "MOUNTS"]
        assert mounts[0].raw_target == "secret:tls-cert"


# ─── CronJob ────────────────────────────────────────────────────────


class TestCronJob:
    def test_cronjob_unwraps_to_containers(self, extractor: K8sManifestExtractor) -> None:
        """CronJob має spec.jobTemplate.spec.template.spec.containers."""
        res = _extract(extractor, """
apiVersion: batch/v1
kind: CronJob
metadata:
  name: backup
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: backup:v1
""")
        containers = [s for s in res.symbols if s.kind == "container"]
        assert containers[0].name == "backup"
        runs = [e for e in res.edges if e.kind == "RUNS_IMAGE"]
        assert runs[0].raw_target == "backup:v1"


# ─── CRD / unknown kinds ────────────────────────────────────────────


class TestUnknownKinds:
    def test_unknown_kind_uses_vendor_prefix(self, extractor: K8sManifestExtractor) -> None:
        res = _extract(extractor, """
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: my-cert
spec:
  secretName: my-cert-tls
""")
        # Не у _KIND_MAP — отримує vendor.k8s.{kind}
        certs = [s for s in res.symbols if s.kind == "vendor.k8s.Certificate"]
        assert certs[0].name == "my-cert"


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_app_manifest_smoke(self, extractor: K8sManifestExtractor) -> None:
        source = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: prod
data:
  LOG_LEVEL: info
---
apiVersion: v1
kind: Secret
metadata:
  name: api-keys
  namespace: prod
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  replicas: 3
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: myorg/api:v2.3
          envFrom:
            - configMapRef:
                name: app-config
            - secretRef:
                name: api-keys
          volumeMounts:
            - name: storage
              mountPath: /data
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: api-data
---
apiVersion: v1
kind: Service
metadata:
  name: api-svc
  namespace: prod
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-ingress
  namespace: prod
spec:
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: api-svc
                port:
                  number: 80
"""
        res = extractor.extract(Path("manifest.yaml"), source=source.encode("utf-8"))
        assert not res.parse_errors

        # 1 file_module + ConfigMap + Secret + Deployment + container + Service + Ingress = 7
        kinds_count: dict[str, int] = {}
        for s in res.symbols:
            kinds_count[s.kind] = kinds_count.get(s.kind, 0) + 1
        assert kinds_count.get("configmap") == 1
        assert kinds_count.get("secret") == 1
        assert kinds_count.get("deployment") == 1
        assert kinds_count.get("container") == 1
        assert kinds_count.get("service") == 1
        assert kinds_count.get("ingress") == 1

        # Чи всі мають namespace 'prod'?
        for s in res.symbols:
            if s.kind in ("deployment", "service", "configmap", "secret", "ingress"):
                assert s.module == "prod", f"{s.name} module={s.module}"

        # End-to-end edges:
        # api Deployment runs image
        runs = {e.raw_target for e in res.edges if e.kind == "RUNS_IMAGE"}
        assert runs == {"myorg/api:v2.3"}

        # api container uses configmap + secret
        uses = {e.raw_target for e in res.edges if e.kind == "USES_CONFIG"}
        assert "configmap:app-config" in uses
        assert "secret:api-keys" in uses

        # mount → pvc:api-data
        mounts = {e.raw_target for e in res.edges if e.kind == "MOUNTS"}
        assert "pvc:api-data" in mounts

        # Service selects labels:app=api
        # Ingress selects service:api-svc
        selects = {e.raw_target for e in res.edges if e.kind == "SELECTS"}
        assert "labels:app=api" in selects
        assert "service:api-svc" in selects
