"""Kubernetes manifest extractor (Stage 4.3, May 2026).

Covers .yml/.yaml that pass the content-sniff (apiVersion + kind present).
Multi-document YAML is supported (`---` separators).

Common kinds (by api group):
    v1                              — Pod, Service, ConfigMap, Secret, Namespace,
                                      ServiceAccount, PersistentVolume(Claim)
    apps/v1                         — Deployment, StatefulSet, DaemonSet, ReplicaSet
    batch/v1                        — Job, CronJob
    networking.k8s.io/v1            — Ingress, NetworkPolicy
    rbac.authorization.k8s.io/v1    — Role, RoleBinding, ClusterRole, ClusterRoleBinding

Symbol model (mapping kind → SymbolKind):
    Pod, Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob → 'deployment'
    Service                                                            → 'service'
    ConfigMap                                                          → 'configmap'
    Secret                                                             → 'secret'
    Ingress                                                            → 'ingress'
    container (inside a Pod template)                                  → 'container'
    Others (Namespace, RBAC, PV/PVC)                                   → 'vendor.k8s.{kind}'

Edge model:
    RUNS_IMAGE     — container → image
    SELECTS        — Service → labels signature (target — Service spec.selector)
                     Ingress → backend Service name
    USES_CONFIG    — workload/container → ConfigMap/Secret name (via envFrom/volume)
    MOUNTS         — container → volume reference (ConfigMap, Secret, PVC, hostPath)
    DEPLOYS        — workload (Deployment/StatefulSet) → workload (rare, via ownerReferences)

What is NOT covered:
    - Helm template syntax `{{ .Values.x }}` — then the YAML parse fails (that
      is a separate tier)
    - Kustomize overlays / patches — separate files, not resolved
    - CRD instances — registered as vendor.k8s.{kind}
    - Pod affinity / topology spread — as syntax, not as edges
    - Init containers — registered as container
    - Sidecars (1.28+ native) — as container

Stage: 4.3. Implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


# Mapping K8s kind → core symbol kind
_KIND_MAP: dict[str, str] = {
    "Pod": "deployment",
    "Deployment": "deployment",
    "StatefulSet": "deployment",
    "DaemonSet": "deployment",
    "ReplicaSet": "deployment",
    "Job": "deployment",
    "CronJob": "deployment",
    "Service": "service",
    "ConfigMap": "configmap",
    "Secret": "secret",
    "Ingress": "ingress",
}


def is_k8s_manifest(content_head: bytes) -> bool:
    """Content-sniffer for LanguageRegistry: yaml has apiVersion + kind."""
    try:
        text = content_head.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return False
    return "apiVersion:" in text and "kind:" in text


@dataclass
class _WalkContext:
    file: str
    seen_ids: set[str] = field(default_factory=set)


class K8sManifestExtractor(SymbolExtractor):
    language = "k8s"
    extensions = ()  # dispatch ONLY via content-sniff

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root
            else (ctx.repo_root if ctx else None)
        )

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        try:
            documents = list(yaml.safe_load_all(source))
        except yaml.YAMLError as e:
            return ExtractionResult(parse_errors=[f"yaml_parse_error: {e}"])

        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel)
        result = ExtractionResult()

        # File-module symbol
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).name, kind="file_module",
            file=rel, start_line=1, end_line=None, language="k8s",
        ))
        ctx.seen_ids.add(file_sym_id)

        for doc_idx, doc in enumerate(documents):
            if not isinstance(doc, dict):
                continue
            self._handle_document(doc, doc_idx, ctx, result)

        # DEFINED_IN file_module
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    def _handle_document(
        self,
        doc: dict,
        doc_idx: int,
        ctx: _WalkContext,
        result: ExtractionResult,
    ) -> None:
        kind = doc.get("kind", "")
        if not isinstance(kind, str) or not kind:
            return

        metadata = doc.get("metadata", {}) or {}
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not name:
            name = f"{kind.lower()}_{doc_idx}"
        namespace = (
            metadata.get("namespace")
            if isinstance(metadata, dict) else None
        )

        sym_kind = _KIND_MAP.get(kind, f"vendor.k8s.{kind}")
        sym_id = _make_id(ctx, f"{kind}/{name}")
        result.symbols.append(SymbolInfo(
            id=sym_id, name=name, kind=sym_kind,
            file=ctx.file, start_line=1, end_line=None,
            language="k8s", is_exported=True,
            module=str(namespace) if namespace else None,
        ))
        ctx.seen_ids.add(sym_id)

        # Per-kind processing
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet",
                    "Job", "CronJob"):
            self._process_workload(doc, sym_id, ctx, result)
        elif kind == "Pod":
            self._process_pod_template(
                doc.get("spec", {}), sym_id, ctx, result, parent_name=name,
            )
        elif kind == "Service":
            self._process_service(doc, sym_id, ctx, result)
        elif kind == "Ingress":
            self._process_ingress(doc, sym_id, ctx, result)

    def _process_workload(
        self, doc: dict, owner_id: str,
        ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Deployment/StatefulSet/Job — extract pod template."""
        spec = doc.get("spec", {})
        if not isinstance(spec, dict):
            return
        # CronJob: spec.jobTemplate.spec.template.spec.containers
        # (one level deeper than Deployment.spec.template.spec)
        if "jobTemplate" in spec:
            job_template_spec = spec.get("jobTemplate", {}).get("spec", {})
            if isinstance(job_template_spec, dict):
                pod_template = job_template_spec.get("template", {})
                if isinstance(pod_template, dict):
                    pod_spec = pod_template.get("spec", {})
                    if isinstance(pod_spec, dict):
                        self._process_pod_template(
                            pod_spec, owner_id, ctx, result,
                            parent_name=doc.get("metadata", {}).get("name", "workload"),
                        )
            return

        template = spec.get("template", {})
        if isinstance(template, dict):
            template_spec = template.get("spec", {})
            if isinstance(template_spec, dict):
                self._process_pod_template(
                    template_spec, owner_id, ctx, result,
                    parent_name=doc.get("metadata", {}).get("name", "workload"),
                )

    def _process_pod_template(
        self,
        pod_spec: dict,
        owner_id: str,
        ctx: _WalkContext,
        result: ExtractionResult,
        parent_name: str,
    ) -> None:
        """Pod spec → containers + volumes."""
        if not isinstance(pod_spec, dict):
            return
        containers = pod_spec.get("containers", [])
        init_containers = pod_spec.get("initContainers", [])

        # Collect volume → source mapping for MOUNTS edges
        volumes = pod_spec.get("volumes", []) or []
        volume_sources: dict[str, dict] = {}
        if isinstance(volumes, list):
            for v in volumes:
                if not isinstance(v, dict):
                    continue
                vol_name = v.get("name")
                if isinstance(vol_name, str):
                    volume_sources[vol_name] = v

        for container in (containers or []) + (init_containers or []):
            if not isinstance(container, dict):
                continue
            self._process_container(
                container, owner_id, ctx, result,
                parent_name=parent_name, volume_sources=volume_sources,
            )

    def _process_container(
        self,
        container: dict,
        owner_id: str,
        ctx: _WalkContext,
        result: ExtractionResult,
        parent_name: str,
        volume_sources: dict[str, dict],
    ) -> None:
        c_name = container.get("name", "container")
        if not isinstance(c_name, str):
            c_name = "container"
        qualified = f"{parent_name}/{c_name}"
        c_id = _make_id(ctx, qualified)
        result.symbols.append(SymbolInfo(
            id=c_id, name=c_name, kind="container",
            file=ctx.file, start_line=1, end_line=None,
            language="k8s", is_exported=True,
            module=parent_name,
        ))
        ctx.seen_ids.add(c_id)
        result.edges.append(EdgeInfo(
            from_id=c_id, to_id=owner_id,
            kind="DEFINED_IN", confidence="strong",
        ))

        # image → RUNS_IMAGE
        image = container.get("image")
        if isinstance(image, str) and image:
            result.edges.append(EdgeInfo(
                from_id=c_id, to_id=None,
                kind="RUNS_IMAGE", confidence="strong",
                raw_target=image,
            ))

        # envFrom → USES_CONFIG (configMapRef / secretRef)
        env_from = container.get("envFrom", []) or []
        if isinstance(env_from, list):
            for ef in env_from:
                if not isinstance(ef, dict):
                    continue
                cm_ref = ef.get("configMapRef")
                if isinstance(cm_ref, dict) and isinstance(cm_ref.get("name"), str):
                    result.edges.append(EdgeInfo(
                        from_id=c_id, to_id=None,
                        kind="USES_CONFIG", confidence="strong",
                        raw_target=f"configmap:{cm_ref['name']}",
                    ))
                sec_ref = ef.get("secretRef")
                if isinstance(sec_ref, dict) and isinstance(sec_ref.get("name"), str):
                    result.edges.append(EdgeInfo(
                        from_id=c_id, to_id=None,
                        kind="USES_CONFIG", confidence="strong",
                        raw_target=f"secret:{sec_ref['name']}",
                    ))

        # env: [{ name, valueFrom: { configMapKeyRef | secretKeyRef } }]
        env = container.get("env", []) or []
        if isinstance(env, list):
            for e in env:
                if not isinstance(e, dict):
                    continue
                value_from = e.get("valueFrom")
                if not isinstance(value_from, dict):
                    continue
                cm_key = value_from.get("configMapKeyRef")
                if isinstance(cm_key, dict) and isinstance(cm_key.get("name"), str):
                    result.edges.append(EdgeInfo(
                        from_id=c_id, to_id=None,
                        kind="USES_CONFIG", confidence="strong",
                        raw_target=f"configmap:{cm_key['name']}",
                    ))
                sec_key = value_from.get("secretKeyRef")
                if isinstance(sec_key, dict) and isinstance(sec_key.get("name"), str):
                    result.edges.append(EdgeInfo(
                        from_id=c_id, to_id=None,
                        kind="USES_CONFIG", confidence="strong",
                        raw_target=f"secret:{sec_key['name']}",
                    ))

        # volumeMounts: [{ name, mountPath }] → MOUNTS edge to the volume source
        volume_mounts = container.get("volumeMounts", []) or []
        if isinstance(volume_mounts, list):
            for vm in volume_mounts:
                if not isinstance(vm, dict):
                    continue
                vol_name = vm.get("name")
                if not isinstance(vol_name, str):
                    continue
                vol_source = volume_sources.get(vol_name, {})
                target = self._volume_source_to_target(vol_name, vol_source)
                if target:
                    result.edges.append(EdgeInfo(
                        from_id=c_id, to_id=None,
                        kind="MOUNTS", confidence="strong",
                        raw_target=target,
                    ))

    @staticmethod
    def _volume_source_to_target(vol_name: str, vol_source: dict) -> str:
        """Convert a volume source dict into a canonical target string."""
        if not isinstance(vol_source, dict):
            return f"volume:{vol_name}"
        if "configMap" in vol_source:
            cm = vol_source.get("configMap", {})
            if isinstance(cm, dict) and isinstance(cm.get("name"), str):
                return f"configmap:{cm['name']}"
        if "secret" in vol_source:
            sec = vol_source.get("secret", {})
            if isinstance(sec, dict) and isinstance(sec.get("secretName"), str):
                return f"secret:{sec['secretName']}"
        if "persistentVolumeClaim" in vol_source:
            pvc = vol_source.get("persistentVolumeClaim", {})
            if isinstance(pvc, dict) and isinstance(pvc.get("claimName"), str):
                return f"pvc:{pvc['claimName']}"
        if "hostPath" in vol_source:
            hp = vol_source.get("hostPath", {})
            if isinstance(hp, dict) and isinstance(hp.get("path"), str):
                return f"hostPath:{hp['path']}"
        # emptyDir, projected, etc. — generic
        return f"volume:{vol_name}"

    def _process_service(
        self, doc: dict, sym_id: str,
        ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Service.spec.selector → SELECTS edge."""
        spec = doc.get("spec", {})
        if not isinstance(spec, dict):
            return
        selector = spec.get("selector", {})
        if isinstance(selector, dict) and selector:
            # Serialize the selector into a canonical string: 'k1=v1,k2=v2'
            selector_str = ",".join(
                f"{k}={v}" for k, v in sorted(selector.items())
                if isinstance(k, str)
            )
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="SELECTS", confidence="strong",
                raw_target=f"labels:{selector_str}",
            ))

    def _process_ingress(
        self, doc: dict, sym_id: str,
        ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Ingress.spec.rules.[].http.paths.[].backend.service.name → SELECTS edge."""
        spec = doc.get("spec", {})
        if not isinstance(spec, dict):
            return
        rules = spec.get("rules", [])
        if not isinstance(rules, list):
            return

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            http = rule.get("http", {})
            if not isinstance(http, dict):
                continue
            paths = http.get("paths", [])
            if not isinstance(paths, list):
                continue
            for p in paths:
                if not isinstance(p, dict):
                    continue
                backend = p.get("backend", {})
                if not isinstance(backend, dict):
                    continue
                # New API: backend.service.name + .port
                svc = backend.get("service", {})
                if isinstance(svc, dict) and isinstance(svc.get("name"), str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="SELECTS", confidence="strong",
                        raw_target=f"service:{svc['name']}",
                    ))

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{len(ctx.seen_ids)}"
