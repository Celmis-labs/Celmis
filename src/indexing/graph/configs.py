"""Config extractor — builds a RepoContext from the project's config files.

Extracts:
    - tsconfig.json    → compilerOptions.paths (path aliases @/, ~/, src/*)
    - package.json     → dependencies, devDependencies, scripts, name
    - .env*            → env var KEYS (we never store the values)
    - Dockerfile       → base images, exposed ports, build stages
    - docker-compose.* → service names
    - .gitlab-ci.yml   → job names
    - bitbucket-pipelines.yml → step names

RepoContext is used by the resolver before the main code parsing — without
`path_aliases` the resolver cannot resolve `import x from '@/utils/foo'`.

Decision: we parse JSON/YAML with the stdlib `json` + `pyyaml` (battle-tested,
we do not need positional info). We use tree-sitter for the Dockerfile
(there is no simple Python parser for it).

Phase: 3 (Config Pre-pass). Implemented.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ───────────────────────── data structures ─────────────────────────


@dataclass
class DockerfileInfo:
    """What was extracted from a single Dockerfile."""

    path: str
    base_images: list[str] = field(default_factory=list)  # ["node:20.11-alpine", "nginx:stable-alpine"]
    stage_aliases: list[str] = field(default_factory=list)  # ["build-stage", "production-stage"]
    exposed_ports: list[int] = field(default_factory=list)
    workdir: str | None = None


@dataclass
class RepoContext:
    """Context extracted from the configs — fed into the resolver."""

    repo_root: Path

    # ─── for the resolver (CRITICAL for graph precision) ───────────
    path_aliases: dict[str, str] = field(default_factory=dict)
    # {"src/": "src/", "@/": "src/"} — normalized, without the `*`

    external_deps: set[str] = field(default_factory=set)
    # {"axios", "vue", "vuex", ...} — package names from package.json

    # ─── for infrastructure Q&A ────────────────────────────────────
    env_keys: set[str] = field(default_factory=set)
    # {"VITE_API_URL", "KEYCLOAK_REALM_NAME", ...} — WITHOUT the values

    package_name: str | None = None
    package_scripts: dict[str, str] = field(default_factory=dict)
    # {"basic": "cross-env NODE_ENV=production webpack ..."}

    is_typescript_project: bool = False  # True if tsconfig.json exists

    dockerfiles: list[DockerfileInfo] = field(default_factory=list)
    docker_services: list[str] = field(default_factory=list)
    # ["frontend", "d_admin", "d_basic", ...]

    ci_jobs: list[str] = field(default_factory=list)
    # GitLab job names or Bitbucket step names

    # ─── for diagnostics ───────────────────────────────────────────
    parse_errors: list[str] = field(default_factory=list)


# ───────────────────────── tsconfig.json ─────────────────────────


_JSONC_TRAILING_COMMA = re.compile(r",(\s*[\]}])")


def _strip_jsonc_comments(text: str) -> str:
    """Remove `// ...` and `/* ... */` comments without touching string literals.

    A simple character scanner — a regex will not do, because `/*` can be
    inside a string (e.g. the tsconfig path `"@/*"`).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    string_quote = ""

    while i < n:
        c = text[i]

        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == string_quote:
                in_string = False
            i += 1
            continue

        if c in ('"', "'"):
            in_string = True
            string_quote = c
            out.append(c)
            i += 1
            continue

        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                # line comment — skip to \n (we keep the \n)
                i += 2
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                # block comment — skip to */
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2  # skip closing */
                continue

        out.append(c)
        i += 1

    return "".join(out)


def _load_jsonc(text: str) -> Any:
    """Load JSON with an optional JSONC fallback (comments + trailing commas)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = _strip_jsonc_comments(text)
        cleaned = _JSONC_TRAILING_COMMA.sub(r"\1", cleaned)
        return json.loads(cleaned)


def parse_tsconfig(path: Path) -> dict[str, str]:
    """Extract the path aliases from tsconfig.json.

    Returns a `prefix → target` map without `*`-wildcards.

    Example: `{"src/*": ["./src/*"]}` → `{"src/": "src/"}`.

    If the tsconfig extends another one — extends is NOT resolved for now (only
    local paths).

    Returns:
        dict prefix → target. Paths relative to the repo root, without a
        leading "./".
    """
    if not path.exists():
        return {}

    try:
        data = _load_jsonc(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("tsconfig_parse_failed path=%s err=%s", path, e)
        return {}

    compiler_opts = data.get("compilerOptions") or {}
    base_url = compiler_opts.get("baseUrl") or "."
    paths = compiler_opts.get("paths") or {}

    if not isinstance(paths, dict):
        return {}

    aliases: dict[str, str] = {}
    for key, targets in paths.items():
        if not isinstance(targets, list) or not targets:
            continue
        target = targets[0]  # we take the first variant
        if not isinstance(target, str):
            continue

        # Normalization: prefix without `*`, target without `./` and `*`
        prefix = key.rstrip("*").rstrip("/")
        if prefix:
            prefix = prefix + "/"

        target_path = target.lstrip("./").rstrip("*").rstrip("/")
        if base_url and base_url != "." and not Path(target_path).is_absolute():
            target_path = f"{base_url.lstrip('./').rstrip('/')}/{target_path}"
        if target_path:
            target_path = target_path + "/"

        aliases[prefix] = target_path

    return aliases


# ───────────────────────── package.json ─────────────────────────


def parse_package_json(path: Path) -> tuple[set[str], dict[str, str], str | None]:
    """Extract external deps + scripts + name.

    Returns:
        (deps, scripts, package_name)
        deps: union of dependencies + devDependencies (package names)
        scripts: name → command
        package_name: the value of the "name" field, or None
    """
    if not path.exists():
        return set(), {}, None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("package_json_parse_failed path=%s err=%s", path, e)
        return set(), {}, None

    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key) or {}
        if isinstance(section, dict):
            deps.update(name for name in section if isinstance(name, str))

    scripts_raw = data.get("scripts") or {}
    scripts: dict[str, str] = {}
    if isinstance(scripts_raw, dict):
        scripts = {k: v for k, v in scripts_raw.items() if isinstance(k, str) and isinstance(v, str)}

    name = data.get("name") if isinstance(data.get("name"), str) else None

    return deps, scripts, name


# ───────────────────────── .env files ─────────────────────────


_ENV_KEY_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)


def parse_env_file(path: Path) -> set[str]:
    """Extract the variable keys. THE VALUES ARE NEVER STORED.

    Security: this function must NOT return, log, or transform values.
    Robustness: comments (`# ...`) are ignored thanks to the regex anchor `^`.
    """
    if not path.exists():
        return set()

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("env_read_failed path=%s err=%s", path, e)
        return set()

    keys: set[str] = set()
    for line in content.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_KEY_PATTERN.match(stripped)
        if match:
            keys.add(match.group(1))

    return keys


# ───────────────────────── Dockerfile ─────────────────────────


def parse_dockerfile(path: Path) -> DockerfileInfo:
    """Extract base images / stages / EXPOSE / WORKDIR from a Dockerfile.

    Uses tree-sitter-dockerfile (through tree-sitter-language-pack).
    """
    info = DockerfileInfo(path=str(path))

    if not path.exists():
        return info

    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as e:
        logger.warning("tree_sitter_pack_unavailable err=%s", e)
        return info

    try:
        source = path.read_bytes()
    except OSError as e:
        logger.warning("dockerfile_read_failed path=%s err=%s", path, e)
        return info

    parser = get_parser("dockerfile")
    tree = parser.parse(source)
    root = tree.root_node

    def text_of(node) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    for child in root.named_children:
        if child.type == "from_instruction":
            spec = next((c for c in child.named_children if c.type == "image_spec"), None)
            if spec:
                name_node = spec.child_by_field_name("name")
                tag_node = spec.child_by_field_name("tag")
                image = text_of(name_node) if name_node else ""
                if tag_node:
                    image += text_of(tag_node)  # tag node text like ":20.11-alpine"
                if image:
                    info.base_images.append(image)
            alias_node = child.child_by_field_name("as")
            if alias_node:
                info.stage_aliases.append(text_of(alias_node))
        elif child.type == "expose_instruction":
            for token in text_of(child).split()[1:]:
                # formats: "80", "80/tcp", "8080-8090"
                num = token.split("/")[0].split("-")[0]
                if num.isdigit():
                    info.exposed_ports.append(int(num))
        elif child.type == "workdir_instruction":
            txt = text_of(child).strip()
            # drop the "WORKDIR " keyword
            parts = txt.split(maxsplit=1)
            if len(parts) == 2:
                info.workdir = parts[1].strip()

    return info


# ───────────────────────── YAML configs ─────────────────────────


def parse_compose(path: Path) -> list[str]:
    """Extract the service names from docker-compose.yml."""
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("compose_parse_failed path=%s err=%s", path, e)
        return []

    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []
    return [name for name in services if isinstance(name, str)]


# GitLab CI top-level reserved keywords that are NOT jobs
_GITLAB_RESERVED = frozenset({
    "image", "services", "stages", "types", "before_script", "after_script",
    "variables", "cache", "include", "default", "workflow", "pages",
})


def parse_gitlab_ci(path: Path) -> list[str]:
    """Extract the job names from .gitlab-ci.yml. Top-level keys that are not reserved."""
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("gitlab_ci_parse_failed path=%s err=%s", path, e)
        return []

    if not isinstance(data, dict):
        return []
    return [
        name for name in data
        if isinstance(name, str) and not name.startswith(".") and name not in _GITLAB_RESERVED
    ]


def parse_bitbucket_pipelines(path: Path) -> list[str]:
    """Extract the step names from bitbucket-pipelines.yml.

    Structure:
        pipelines:
          default:
            - step:
                name: "Build"
            - step:
                name: "Deploy"
          branches:
            main:
              - step: { name: "..." }
    """
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        logger.warning("bitbucket_parse_failed path=%s err=%s", path, e)
        return []

    if not isinstance(data, dict):
        return []
    pipelines = data.get("pipelines")
    if not isinstance(pipelines, dict):
        return []

    names: list[str] = []
    for branch_or_default in pipelines.values():
        if isinstance(branch_or_default, list):
            _collect_step_names(branch_or_default, names)
        elif isinstance(branch_or_default, dict):
            for step_list in branch_or_default.values():
                if isinstance(step_list, list):
                    _collect_step_names(step_list, names)
    return names


def _collect_step_names(items: list, out: list[str]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        step = item.get("step") or item.get("parallel")
        if isinstance(step, dict):
            name = step.get("name")
            if isinstance(name, str):
                out.append(name)
        elif isinstance(step, list):
            _collect_step_names(step, out)


# ───────────────────────── orchestrator ─────────────────────────


# The files we look for. Paths relative to the repo root.
_TSCONFIG_NAMES = ("tsconfig.json",)  # we do not resolve the extends chain yet
_PACKAGE_JSON = "package.json"
_ENV_GLOB = (".env", ".env.*")
_DOCKERFILE_GLOBS = ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "*.dockerfile")
_COMPOSE_GLOBS = ("docker-compose.yml", "docker-compose.yaml", "docker-compose.*.yml")
_GITLAB_CI = ".gitlab-ci.yml"
_BITBUCKET_CI = "bitbucket-pipelines.yml"


def build_repo_context(repo_root: Path) -> RepoContext:
    """Parse all the relevant config files and return a RepoContext.

    Looks only at the root level (not a recursive search).
    """
    repo_root = Path(repo_root).resolve()
    ctx = RepoContext(repo_root=repo_root)

    # tsconfig
    for name in _TSCONFIG_NAMES:
        path = repo_root / name
        if path.exists():
            ctx.is_typescript_project = True
            try:
                ctx.path_aliases = parse_tsconfig(path)
            except Exception as e:  # noqa: BLE001
                ctx.parse_errors.append(f"tsconfig: {e}")
            break

    # package.json
    pkg_path = repo_root / _PACKAGE_JSON
    if pkg_path.exists():
        try:
            deps, scripts, name = parse_package_json(pkg_path)
            ctx.external_deps = deps
            ctx.package_scripts = scripts
            ctx.package_name = name
        except Exception as e:  # noqa: BLE001
            ctx.parse_errors.append(f"package.json: {e}")

    # .env files
    for env_path in _discover_files(repo_root, _ENV_GLOB):
        try:
            ctx.env_keys.update(parse_env_file(env_path))
        except Exception as e:  # noqa: BLE001
            ctx.parse_errors.append(f"{env_path.name}: {e}")

    # Dockerfile(s)
    for dockerfile_path in _discover_files(repo_root, _DOCKERFILE_GLOBS):
        try:
            ctx.dockerfiles.append(parse_dockerfile(dockerfile_path))
        except Exception as e:  # noqa: BLE001
            ctx.parse_errors.append(f"{dockerfile_path.name}: {e}")

    # docker-compose
    for compose_path in _discover_files(repo_root, _COMPOSE_GLOBS):
        try:
            ctx.docker_services.extend(parse_compose(compose_path))
        except Exception as e:  # noqa: BLE001
            ctx.parse_errors.append(f"{compose_path.name}: {e}")

    # CI yamls
    gitlab_path = repo_root / _GITLAB_CI
    if gitlab_path.exists():
        try:
            ctx.ci_jobs.extend(parse_gitlab_ci(gitlab_path))
        except Exception as e:  # noqa: BLE001
            ctx.parse_errors.append(f"{_GITLAB_CI}: {e}")

    bitbucket_path = repo_root / _BITBUCKET_CI
    if bitbucket_path.exists():
        try:
            ctx.ci_jobs.extend(parse_bitbucket_pipelines(bitbucket_path))
        except Exception as e:  # noqa: BLE001
            ctx.parse_errors.append(f"{_BITBUCKET_CI}: {e}")

    logger.info(
        "repo_context_built repo=%s aliases=%d deps=%d env_keys=%d "
        "dockerfiles=%d services=%d ci_jobs=%d",
        repo_root.name,
        len(ctx.path_aliases),
        len(ctx.external_deps),
        len(ctx.env_keys),
        len(ctx.dockerfiles),
        len(ctx.docker_services),
        len(ctx.ci_jobs),
    )

    return ctx


def _discover_files(repo_root: Path, globs: tuple[str, ...]) -> list[Path]:
    """Find files in the repo root by glob patterns (not recursively)."""
    out: list[Path] = []
    seen: set[Path] = set()
    for pattern in globs:
        for p in repo_root.glob(pattern):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    return sorted(out)
