"""CI/CD workflow extractor (Stage 13, May 2026).

Covers three popular CI/CD providers through a single extractor:

    GitHub Actions:    .github/workflows/*.yml
    GitLab CI:         .gitlab-ci.yml + included files
    Bitbucket Pipelines: bitbucket-pipelines.yml

Provider detection: filename pattern + parent directory.

Symbol model (vendor namespace because it is CI-specific):
    file_module                     — synthetic per workflow file
    vendor.ci.workflow              — workflow root (GitHub Actions, GitLab top-level)
    vendor.ci.job                   — job (GitHub Actions jobs.X, GitLab top-level keys)
    vendor.ci.step                  — Bitbucket step (named step entities)

Edge model:
    IMPORTS    — `uses: org/action@version` (GitHub) / `include:` (GitLab) /
                 `pipe:` (Bitbucket) — references to external actions/pipelines
    RUNS_IMAGE — `image:` field per job/step → image reference
    DEPLOYS    — `needs:` between GitHub Actions jobs (job dependency graph)

What is NOT covered:
    - Reusable workflows full resolution
    - Matrix strategy expansion
    - Conditional steps (if expressions)
    - Action input/output validation
    - Secrets/variables flow tracking

Stage: 13. Implemented (MVP).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo

logger = logging.getLogger(__name__)


@dataclass
class _WalkContext:
    file: str
    seen_ids: set[str] = field(default_factory=set)


class CIWorkflowExtractor(SymbolExtractor):
    language = "ci_workflow"
    extensions = ()  # filename pattern dispatch

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
            data = yaml.safe_load(source)
        except yaml.YAMLError as e:
            return ExtractionResult(parse_errors=[f"yaml_parse_error: {e}"])

        rel = self._relpath(file_path)
        ctx = _WalkContext(file=rel)
        result = ExtractionResult()

        if not isinstance(data, dict):
            result.parse_errors.append(f"not_a_mapping file={rel}")
            return result

        # File-module
        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).name, kind="file_module",
            file=rel, start_line=1, end_line=None, language="ci_workflow",
        ))
        ctx.seen_ids.add(file_sym_id)

        provider = _detect_provider(file_path)
        if provider == "github_actions":
            self._extract_github_actions(data, ctx, result)
        elif provider == "gitlab_ci":
            self._extract_gitlab_ci(data, ctx, result)
        elif provider == "bitbucket_pipelines":
            self._extract_bitbucket_pipelines(data, ctx, result)

        # DEFINED_IN edges
        for sym in result.symbols:
            if sym.id == file_sym_id:
                continue
            result.edges.append(EdgeInfo(
                from_id=sym.id, to_id=file_sym_id,
                kind="DEFINED_IN", confidence="strong",
            ))

        return result

    # ─── GitHub Actions ─────────────────────────────────────────

    def _extract_github_actions(
        self, data: dict, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """GitHub Actions workflow: name + on + jobs.{id}.{steps[].uses, runs-on}."""
        wf_name = str(data.get("name") or Path(ctx.file).stem)
        wf_id = _make_id(ctx, f"workflow.{wf_name}")
        result.symbols.append(SymbolInfo(
            id=wf_id, name=wf_name, kind="vendor.ci.workflow",
            file=ctx.file, start_line=1, end_line=None,
            language="ci_workflow", is_exported=True,
        ))
        ctx.seen_ids.add(wf_id)

        jobs = data.get("jobs") or {}
        if not isinstance(jobs, dict):
            return

        for job_id, job_def in jobs.items():
            if not isinstance(job_id, str) or not isinstance(job_def, dict):
                continue
            self._handle_github_job(job_id, job_def, wf_id, ctx, result)

    def _handle_github_job(
        self,
        job_id: str,
        job_def: dict,
        wf_id: str,
        ctx: _WalkContext,
        result: ExtractionResult,
    ) -> None:
        sym_id = _make_id(ctx, f"job.{job_id}")
        result.symbols.append(SymbolInfo(
            id=sym_id, name=job_id, kind="vendor.ci.job",
            file=ctx.file, start_line=1, end_line=None,
            language="ci_workflow", is_exported=True,
            module=str(job_def.get("runs-on") or "") or None,
        ))
        ctx.seen_ids.add(sym_id)

        # needs: dependency edges between jobs
        needs = job_def.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        if isinstance(needs, list):
            for n in needs:
                if isinstance(n, str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="DEPLOYS", confidence="strong",
                        raw_target=f"job.{n}",
                    ))

        # container/services image refs
        container = job_def.get("container")
        if isinstance(container, str):
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="RUNS_IMAGE", confidence="strong",
                raw_target=container,
            ))
        elif isinstance(container, dict):
            img = container.get("image")
            if isinstance(img, str):
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=None,
                    kind="RUNS_IMAGE", confidence="strong",
                    raw_target=img,
                ))

        # steps[].uses → action references
        steps = job_def.get("steps") or []
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = step.get("uses")
                if isinstance(uses, str) and uses.strip():
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="IMPORTS", confidence="strong",
                        raw_target=uses,
                    ))

        # uses (reusable workflow)
        wf_uses = job_def.get("uses")
        if isinstance(wf_uses, str) and wf_uses.strip():
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="IMPORTS", confidence="strong",
                raw_target=wf_uses,
            ))

    # ─── GitLab CI ──────────────────────────────────────────────

    def _extract_gitlab_ci(
        self, data: dict, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """GitLab CI: top-level keys = jobs (skipping reserved meta)."""
        # File-level workflow symbol
        wf_id = _make_id(ctx, "workflow")
        result.symbols.append(SymbolInfo(
            id=wf_id, name="workflow", kind="vendor.ci.workflow",
            file=ctx.file, start_line=1, end_line=None,
            language="ci_workflow", is_exported=True,
        ))
        ctx.seen_ids.add(wf_id)

        # include: directives → IMPORTS edges
        includes = data.get("include")
        for target in _flatten_gitlab_includes(includes):
            result.edges.append(EdgeInfo(
                from_id=wf_id, to_id=None,
                kind="IMPORTS", confidence="strong",
                raw_target=target,
            ))

        # Jobs = top-level keys except the reserved ones
        reserved = {
            "default", "include", "stages", "variables", "workflow",
            "image", "services", "before_script", "after_script", "cache",
        }
        for key, val in data.items():
            if not isinstance(key, str) or key in reserved or key.startswith("."):
                # `.hidden` entries (templates) — skip
                continue
            if not isinstance(val, dict):
                continue
            self._handle_gitlab_job(key, val, ctx, result)

    def _handle_gitlab_job(
        self,
        job_name: str,
        job_def: dict,
        ctx: _WalkContext,
        result: ExtractionResult,
    ) -> None:
        sym_id = _make_id(ctx, f"job.{job_name}")
        stage = str(job_def.get("stage") or "") or None
        result.symbols.append(SymbolInfo(
            id=sym_id, name=job_name, kind="vendor.ci.job",
            file=ctx.file, start_line=1, end_line=None,
            language="ci_workflow", is_exported=True,
            module=stage,
        ))
        ctx.seen_ids.add(sym_id)

        # image: → RUNS_IMAGE
        img = job_def.get("image")
        if isinstance(img, str):
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="RUNS_IMAGE", confidence="strong",
                raw_target=img,
            ))
        elif isinstance(img, dict):
            inner = img.get("name")
            if isinstance(inner, str):
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=None,
                    kind="RUNS_IMAGE", confidence="strong",
                    raw_target=inner,
                ))

        # extends: → IMPORTS (job inheritance)
        extends = job_def.get("extends")
        if isinstance(extends, str):
            extends = [extends]
        if isinstance(extends, list):
            for e in extends:
                if isinstance(e, str):
                    result.edges.append(EdgeInfo(
                        from_id=sym_id, to_id=None,
                        kind="IMPORTS", confidence="strong",
                        raw_target=f"extends:{e}",
                    ))

    # ─── Bitbucket Pipelines ────────────────────────────────────

    def _extract_bitbucket_pipelines(
        self, data: dict, ctx: _WalkContext, result: ExtractionResult,
    ) -> None:
        """Bitbucket Pipelines: pipelines.{default,branches,...}[].step."""
        wf_id = _make_id(ctx, "pipelines")
        result.symbols.append(SymbolInfo(
            id=wf_id, name="pipelines", kind="vendor.ci.workflow",
            file=ctx.file, start_line=1, end_line=None,
            language="ci_workflow", is_exported=True,
        ))
        ctx.seen_ids.add(wf_id)

        # Default image at top-level
        top_image = data.get("image")
        if isinstance(top_image, str):
            result.edges.append(EdgeInfo(
                from_id=wf_id, to_id=None,
                kind="RUNS_IMAGE", confidence="strong",
                raw_target=top_image,
            ))

        pipelines = data.get("pipelines") or {}
        if not isinstance(pipelines, dict):
            return

        # Different pipeline types: default, branches.X, pull-requests, custom, tags
        for trigger, trigger_def in pipelines.items():
            if not isinstance(trigger, str):
                continue
            self._extract_bitbucket_trigger(trigger, trigger_def, ctx, result)

    def _extract_bitbucket_trigger(
        self,
        trigger: str,
        trigger_def: Any,
        ctx: _WalkContext,
        result: ExtractionResult,
    ) -> None:
        """Pipeline trigger entry — a list of steps or branch→steps mapping."""
        # `default: [...]` — list of step entries
        if isinstance(trigger_def, list):
            for i, item in enumerate(trigger_def):
                self._extract_bitbucket_step_item(
                    item, ctx, result, parent=f"trigger.{trigger}", idx=i,
                )
        elif isinstance(trigger_def, dict):
            # `branches: { master: [...], 'feature/*': [...] }`
            for branch_pattern, items in trigger_def.items():
                if not isinstance(branch_pattern, str):
                    continue
                if isinstance(items, list):
                    for i, item in enumerate(items):
                        self._extract_bitbucket_step_item(
                            item, ctx, result,
                            parent=f"trigger.{trigger}.{branch_pattern}",
                            idx=i,
                        )

    def _extract_bitbucket_step_item(
        self,
        item: Any,
        ctx: _WalkContext,
        result: ExtractionResult,
        parent: str,
        idx: int,
    ) -> None:
        """One elem in the pipeline list — may be `step:`, `parallel:`, `stage:`."""
        if not isinstance(item, dict):
            return
        if "step" in item:
            self._handle_bitbucket_step(
                item["step"], ctx, result, parent=parent, idx=idx,
            )
        elif "parallel" in item:
            par = item["parallel"]
            par_steps = par if isinstance(par, list) else par.get("steps", [])
            if isinstance(par_steps, list):
                for j, sub in enumerate(par_steps):
                    self._extract_bitbucket_step_item(
                        sub, ctx, result, parent=f"{parent}.parallel{idx}", idx=j,
                    )
        elif "stage" in item:
            stage = item["stage"]
            if isinstance(stage, dict):
                stage_steps = stage.get("steps", [])
                stage_name = stage.get("name", f"stage{idx}")
                if isinstance(stage_steps, list):
                    for j, sub in enumerate(stage_steps):
                        self._extract_bitbucket_step_item(
                            sub, ctx, result,
                            parent=f"{parent}.{stage_name}", idx=j,
                        )

    def _handle_bitbucket_step(
        self,
        step_def: dict,
        ctx: _WalkContext,
        result: ExtractionResult,
        parent: str,
        idx: int,
    ) -> None:
        if not isinstance(step_def, dict):
            return
        step_name = str(step_def.get("name") or f"{parent}_step{idx}")
        sym_id = _make_id(ctx, f"step.{parent}.{step_name}")
        result.symbols.append(SymbolInfo(
            id=sym_id, name=step_name, kind="vendor.ci.step",
            file=ctx.file, start_line=1, end_line=None,
            language="ci_workflow", is_exported=True,
            module=parent,
        ))
        ctx.seen_ids.add(sym_id)

        # image → RUNS_IMAGE
        img = step_def.get("image")
        if isinstance(img, str):
            result.edges.append(EdgeInfo(
                from_id=sym_id, to_id=None,
                kind="RUNS_IMAGE", confidence="strong",
                raw_target=img,
            ))
        elif isinstance(img, dict):
            inner = img.get("name")
            if isinstance(inner, str):
                result.edges.append(EdgeInfo(
                    from_id=sym_id, to_id=None,
                    kind="RUNS_IMAGE", confidence="strong",
                    raw_target=inner,
                ))

        # script.[].pipe: → IMPORTS (Bitbucket pipes — like GitHub actions)
        script = step_def.get("script") or []
        if isinstance(script, list):
            for cmd in script:
                if isinstance(cmd, dict):
                    pipe = cmd.get("pipe")
                    if isinstance(pipe, str) and pipe.strip():
                        result.edges.append(EdgeInfo(
                            from_id=sym_id, to_id=None,
                            kind="IMPORTS", confidence="strong",
                            raw_target=pipe,
                        ))

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


# ─── helpers ────────────────────────────────────────────────────────


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


def _make_id(ctx: _WalkContext, name: str) -> str:
    base = f"{ctx.file}::{name}"
    if base not in ctx.seen_ids:
        return base
    return f"{base}@{len(ctx.seen_ids)}"


def _detect_provider(file_path: Path) -> str | None:
    """Detect the CI provider by filename + parent directory."""
    name = file_path.name.lower()
    parts = [p.lower() for p in file_path.parts]

    # GitHub Actions: .github/workflows/*.yml|*.yaml
    if "workflows" in parts and ".github" in parts:
        return "github_actions"

    # GitLab CI
    if name in (".gitlab-ci.yml", ".gitlab-ci.yaml"):
        return "gitlab_ci"

    # Bitbucket Pipelines
    if name in ("bitbucket-pipelines.yml", "bitbucket-pipelines.yaml"):
        return "bitbucket_pipelines"

    return None


def is_ci_workflow_file(file_path: Path) -> bool:
    """Filename matcher for LanguageRegistry — True if file path matches CI patterns."""
    return _detect_provider(file_path) is not None


def _flatten_gitlab_includes(includes: Any) -> list[str]:
    """GitLab `include:` takes different forms — we normalize to a list of identifiers.

    Forms:
        include: "/templates/foo.yml"             → ["/templates/foo.yml"]
        include: ["a.yml", "b.yml"]               → ["a.yml", "b.yml"]
        include:
          - project: 'shared/templates'
            file: 'pipeline.yml'                  → ["shared/templates::pipeline.yml"]
          - remote: 'https://...'                 → ["https://..."]
          - local: '/path.yml'                    → ["/path.yml"]
          - template: 'Auto-DevOps.gitlab-ci.yml' → ["template:Auto-DevOps.gitlab-ci.yml"]
    """
    out: list[str] = []
    if includes is None:
        return out
    if isinstance(includes, str):
        return [includes]
    if isinstance(includes, list):
        for entry in includes:
            if isinstance(entry, str):
                out.append(entry)
            elif isinstance(entry, dict):
                if isinstance(entry.get("local"), str):
                    out.append(entry["local"])
                elif isinstance(entry.get("remote"), str):
                    out.append(entry["remote"])
                elif isinstance(entry.get("template"), str):
                    out.append(f"template:{entry['template']}")
                elif isinstance(entry.get("project"), str):
                    proj = entry["project"]
                    file_ = entry.get("file") or "<unknown>"
                    if isinstance(file_, list):
                        for f in file_:
                            out.append(f"{proj}::{f}")
                    else:
                        out.append(f"{proj}::{file_}")
    return out
