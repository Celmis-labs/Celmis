"""Supply-chain hygiene — the problems no vulnerability scanner reports.

Every auditor in `native.py` answers one question: "does a package version
match a published advisory?". None of them ask:

  * does the lock file still agree with the manifest? (a package present in
    one and absent from the other means `npm ci` installs something nobody
    reviewed, or CI resolves a version no developer has ever run);
  * does a direct dependency execute code at install time? (`postinstall`,
    `build.rs`, a `setup.py` that does real work — the classic vector, and it
    fires before any test suite ever runs);
  * does a dependency come from somewhere other than the official registry?
    (a git URL, a tarball behind a link, a private mirror);
  * does a package name merely *resemble* a popular one? (typosquatting).

These are findings about *provenance*, not about known CVEs, and they are
kept in their own block so they can never inflate a vulnerability count.
The typosquat check is deliberately conservative and offline: it flags a
SUSPECT with the reason attached, it never claims a package is malicious.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from src.deps.locks import (
    OFFICIAL_HOSTS,
    LockEntry,
    parse_cargo_lock,
    parse_package_lock,
    parse_pnpm_lock,
    parse_poetry_lock,
    parse_yarn_lock,
)

logger = logging.getLogger(__name__)

KIND_LOCK_DRIFT = "lock_drift"
KIND_INSTALL_SCRIPT = "install_script"
KIND_NON_REGISTRY = "non_registry"
KIND_SUSPECT_NAME = "suspect_name"

_MAX_PER_KIND = 60

# npm lifecycle hooks that run automatically on `npm install`.
_INSTALL_HOOKS = ("preinstall", "install", "postinstall", "prepare", "prepublish")

# Popular names, for the typosquat neighbourhood check only. Not a whitelist,
# not a popularity ranking — just "names an attacker would imitate".
_POPULAR = {
    "npm": {
        "react", "react-dom", "lodash", "axios", "express", "chalk", "commander",
        "debug", "moment", "dayjs", "typescript", "webpack", "vite", "eslint",
        "prettier", "jest", "vitest", "next", "vue", "angular", "svelte",
        "tailwindcss", "postcss", "autoprefixer", "zod", "yup", "uuid", "dotenv",
        "cross-env", "rimraf", "glob", "minimist", "yargs", "inquirer", "colors",
        "request", "node-fetch", "cheerio", "puppeteer", "playwright", "socket.io",
        "mongoose", "sequelize", "prisma", "knex", "pg", "mysql2", "redis",
        "jsonwebtoken", "bcrypt", "passport", "cors", "helmet", "morgan", "winston",
        "classnames", "clsx", "immer", "redux", "zustand", "recoil", "graphql",
        "apollo-client", "styled-components", "framer-motion", "three", "d3",
        "babel-core", "rollup", "esbuild", "nodemon", "ts-node", "husky",
    },
    "PyPI": {
        "requests", "urllib3", "numpy", "pandas", "scipy", "flask", "django",
        "fastapi", "pydantic", "sqlalchemy", "alembic", "celery", "redis",
        "psycopg2", "pymysql", "boto3", "botocore", "click", "typer", "rich",
        "httpx", "aiohttp", "pytest", "tox", "black", "ruff", "mypy", "isort",
        "setuptools", "wheel", "pip", "virtualenv", "poetry", "jinja2", "markupsafe",
        "pyyaml", "toml", "tomli", "python-dateutil", "pytz", "six", "attrs",
        "cryptography", "pyjwt", "passlib", "bcrypt", "pillow", "opencv-python",
        "matplotlib", "seaborn", "scikit-learn", "torch", "tensorflow", "transformers",
        "openai", "anthropic", "langchain", "beautifulsoup4", "lxml", "selenium",
        "uvicorn", "gunicorn", "starlette", "loguru", "tqdm", "colorama",
    },
    "crates.io": {
        "serde", "serde_json", "tokio", "reqwest", "clap", "anyhow", "thiserror",
        "rand", "regex", "log", "env_logger", "chrono", "uuid", "itertools",
        "futures", "async-trait", "axum", "actix-web", "hyper", "tracing", "rayon",
    },
}


@dataclass(frozen=True)
class HygieneFinding:
    kind: str          # lock_drift | install_script | non_registry | suspect_name
    severity: str      # info | low | medium | high
    ecosystem: str
    package: str
    detail: str        # human-readable reason, always specific
    location: str      # repo-relative manifest / lock path
    subproject: str = ""
    #: The evidence, separated from the sentence about it.
    #:
    #: `detail` is prose that sometimes embedded a snippet; `location` was a
    #: path with no line. So on "this package runs a script at install" the
    #: reader saw the package and the file — and not the script, which IS the
    #: finding. A deterministic check whose evidence cannot be looked at is
    #: indistinguishable, to the person reading it, from a guess.
    #:
    #: None where the source genuinely does not carry it: npm's lock states
    #: `hasInstallScript` as a boolean and never the script body, and
    #: inventing a line number would be worse than admitting there is none.
    line: int | None = None
    excerpt: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _line_of(text: str | None, needle: str) -> tuple[int | None, str]:
    """Where `needle` appears, and the line it appears on.

    Cheap and deliberately literal: the checks below have already decided
    something is wrong, and this only answers "where do I look". A miss
    returns (None, "") rather than a guess — a wrong line number sends the
    reader to innocent code and costs more than no line at all.
    """
    if not text or not needle:
        return None, ""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            stripped = line.strip()
            return number, (stripped[:200] + "…" if len(stripped) > 200 else stripped)
    return None, ""


# ─── 1. manifest ↔ lock drift ────────────────────────────────────────


def check_lock_drift(
    declared: dict[str, str],
    lock: list[LockEntry],
    *,
    ecosystem: str,
    manifest: str,
    lockfile: str,
    subproject: str = "",
    report_extra: bool = True,
) -> list[HygieneFinding]:
    """`declared` is {package: version-spec} from the manifest; `lock` is the
    resolved tree. Three divergences matter:

      * declared but not locked — the lock is stale, `npm ci` will not install
        it, and CI is testing a different dependency set than the developer;
      * locked as a *direct* dependency but not declared — something added a
        package straight to the lock (a bad merge, or a hand edit);
      * version outside the declared pin — only reported for exact pins
        ("==1.2.3" for PyPI, "=1.2.3" for cargo, a bare "1.2.3" for npm),
        where a mismatch is unambiguous.

    `report_extra` guards the second check: only npm and pnpm locks record the
    direct set independently of the manifest. poetry.lock, Cargo.lock and
    yarn.lock list the flattened tree, so "in the lock, not in the manifest"
    there means "transitive", not "drift" — reporting it would be noise.
    """
    out: list[HygieneFinding] = []
    locked_direct = {e.package.lower(): e for e in lock if not e.transitive}
    locked_any = {e.package.lower(): e for e in lock}

    for name, spec in declared.items():
        low = name.lower()
        entry = locked_any.get(low)
        if entry is None:
            out.append(HygieneFinding(
                KIND_LOCK_DRIFT, "medium", ecosystem, name,
                f"declared in {manifest} ({spec or 'any'}) but missing from {lockfile}",
                manifest, subproject))
            continue
        pinned = _exact_pin(spec, ecosystem)
        if pinned and entry.version and entry.version != pinned:
            out.append(HygieneFinding(
                KIND_LOCK_DRIFT, "medium", ecosystem, name,
                f"{manifest} pins {pinned}, {lockfile} resolves {entry.version}",
                lockfile, subproject))

    declared_low = {n.lower() for n in declared}
    for low, entry in (locked_direct.items() if report_extra else ()):
        if declared_low and low not in declared_low:
            out.append(HygieneFinding(
                KIND_LOCK_DRIFT, "low", ecosystem, entry.package,
                f"present in {lockfile} as a direct dependency "
                f"({entry.version or 'unknown version'}) but absent from {manifest}",
                lockfile, subproject))
    return out


_VERSION = r"v?(\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)"
# Each ecosystem spells "exactly this version" differently, and getting it
# wrong turns every caret range into a false drift report: npm's bare "1.2.3"
# IS exact, cargo's bare "1.2.3" means "^1.2.3", pip's means nothing at all.
_EXACT_PATTERNS = {
    "npm": re.compile(rf"^\s*(?:=)?\s*{_VERSION}\s*$"),
    "PyPI": re.compile(rf"^\s*==\s*{_VERSION}\s*$"),
    "crates.io": re.compile(rf"^\s*=\s*{_VERSION}\s*$"),
}


def _exact_pin(spec: str, ecosystem: str = "npm") -> str | None:
    pattern = _EXACT_PATTERNS.get(ecosystem)
    if pattern is None:
        return None
    m = pattern.match(str(spec or ""))
    return m.group(1) if m else None


# ─── 2. install-time code execution ──────────────────────────────────


def check_install_scripts(
    package_json: dict | None,
    lock: list[LockEntry],
    *,
    subproject: str = "",
    manifest: str = "package.json",
    raw: str | None = None,
) -> list[HygieneFinding]:
    """Install hooks in the repo's own manifest, plus direct dependencies the
    lock file marks with `hasInstallScript`.

    npm states this outright in lockfileVersion 2+; that flag is the only
    reliable way to see it without unpacking node_modules.
    """
    out: list[HygieneFinding] = []
    scripts = (package_json or {}).get("scripts") or {}
    if isinstance(scripts, dict):
        for hook in _INSTALL_HOOKS:
            body = scripts.get(hook)
            if body:
                line, excerpt = _line_of(raw, f'"{hook}"')
                out.append(HygieneFinding(
                    KIND_INSTALL_SCRIPT, "low", "npm", "(this project)",
                    f"{manifest} runs `{hook}` at install time",
                    manifest, subproject,
                    line=line,
                    # The script itself, which is the finding. Falls back to
                    # the parsed value when the raw text was not available.
                    excerpt=excerpt or f'"{hook}": {str(body)[:200]}'))
    direct_names = set((package_json or {}).get("dependencies") or {}) | set(
        (package_json or {}).get("devDependencies") or {})
    for entry in lock:
        if not entry.has_install_script:
            continue
        if direct_names and entry.package not in direct_names:
            continue
        # No excerpt is possible here and that is a property of the source:
        # npm's lock records `hasInstallScript` as a boolean and never the
        # script body. Saying so beats inventing one.
        out.append(HygieneFinding(
            KIND_INSTALL_SCRIPT, "medium", entry.ecosystem, entry.package,
            f"direct dependency {entry.package}@{entry.version or '?'} runs an "
            f"install script (hasInstallScript in {entry.lockfile}; the lock "
            f"records the flag, not the script)",
            entry.lockfile, subproject))
    return out


def check_python_build_hooks(
    setup_py: str | None, *, location: str = "setup.py", subproject: str = "",
) -> list[HygieneFinding]:
    """A `setup.py` that only calls `setup(...)` is boilerplate. One that
    imports `os`/`subprocess`/`socket` or opens a network connection executes
    attacker-chosen code the moment anyone runs `pip install .`."""
    if setup_py is None:
        return []
    risky = [kw for kw in ("subprocess", "os.system", "socket", "urllib",
                           "requests.", "base64.b64decode", "eval(", "exec(",
                           "__import__")
             if kw in setup_py]
    if not risky:
        return []
    # Point at the FIRST risky construct rather than the file. "setup.py
    # executes code at install time" is a claim; line 14 running
    # `subprocess.check_output(...)` is the thing itself.
    line, excerpt = _line_of(setup_py, sorted(risky)[0])
    return [HygieneFinding(
        KIND_INSTALL_SCRIPT, "medium", "PyPI", "(this project)",
        f"{location} executes code at install time (uses {', '.join(sorted(risky))})",
        location, subproject, line=line, excerpt=excerpt)]


def check_cargo_build_script(
    cargo_toml: dict | None, has_build_rs: bool, *,
    location: str = "Cargo.toml", subproject: str = "",
) -> list[HygieneFinding]:
    build = (cargo_toml or {}).get("package", {}).get("build") if isinstance(
        (cargo_toml or {}).get("package"), dict) else None
    if not build and not has_build_rs:
        return []
    return [HygieneFinding(
        KIND_INSTALL_SCRIPT, "low", "crates.io", "(this project)",
        f"build script runs at compile time ({build or 'build.rs'})",
        location, subproject)]


# ─── 3. dependencies from outside the official registry ──────────────

_VCS_PREFIXES = ("git+", "git:", "git@", "ssh://", "svn+", "hg+", "bzr+")
_LOCAL_PREFIXES = ("file:", "link:", "portal:", "path:", "workspace:")


def classify_source(spec: str, ecosystem: str = "npm") -> tuple[str, str] | None:
    """(severity, reason) when a dependency specifier does not resolve to the
    ecosystem's public registry — otherwise None."""
    s = str(spec or "").strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith(_VCS_PREFIXES) or low.endswith(".git"):
        return ("medium", f"resolved from version control ({s[:120]}) — no registry "
                          "signature, tags can be moved")
    if low.startswith(_LOCAL_PREFIXES):
        return ("low", f"resolved from a local path ({s[:120]}) — reproducible only "
                       "inside this checkout")
    if re.match(r"^[\w.-]+/[\w.-]+(#.*)?$", s) and "@" not in s and ecosystem == "npm":
        return ("medium", f"GitHub shorthand ({s}) — installs whatever that branch "
                          "points at today")
    if low.startswith(("http://", "https://")):
        host = (urlparse(s).hostname or "").lower()
        official = OFFICIAL_HOSTS.get(ecosystem, ())
        if host and not any(host == o or host.endswith("." + o) for o in official):
            severity = "high" if low.startswith("http://") else "medium"
            return (severity, f"downloaded from {host}, not the {ecosystem} registry")
        if low.startswith("http://"):
            return ("high", "downloaded over plain HTTP — trivially tamperable")
    return None


def check_non_registry(
    declared_specs: dict[str, str],
    lock: list[LockEntry],
    *,
    ecosystem: str = "npm",
    manifest: str = "package.json",
    subproject: str = "",
) -> list[HygieneFinding]:
    out: list[HygieneFinding] = []
    seen: set[tuple[str, str]] = set()
    for name, spec in (declared_specs or {}).items():
        verdict = classify_source(str(spec), ecosystem)
        if verdict and (name, verdict[1]) not in seen:
            seen.add((name, verdict[1]))
            out.append(HygieneFinding(KIND_NON_REGISTRY, verdict[0], ecosystem,
                                      name, verdict[1], manifest, subproject))
    for entry in lock:
        if not entry.resolved:
            continue
        verdict = classify_source(entry.resolved, entry.ecosystem)
        if verdict and (entry.package, verdict[1]) not in seen:
            seen.add((entry.package, verdict[1]))
            out.append(HygieneFinding(
                KIND_NON_REGISTRY, verdict[0], entry.ecosystem, entry.package,
                verdict[1] + (" (transitive)" if entry.transitive else ""),
                entry.lockfile, subproject))
    return out


# ─── 4. typosquat suspicion (offline, conservative) ──────────────────


def _edit_distance_at_most_1(a: str, b: str) -> bool:
    """True when `a` becomes `b` with one insert, delete, substitute or an
    adjacent transposition — the four mistakes typosquatters bank on."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        if len(diff) == 2 and diff[1] == diff[0] + 1:
            i, j = diff
            return a[i] == b[j] and a[j] == b[i]
        return False
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] != long[j]:
            if skipped:
                return False
            skipped = True
            j += 1
            continue
        i += 1
        j += 1
    return True


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def check_suspect_names(
    names: list[str], *, ecosystem: str = "npm", manifest: str = "",
    subproject: str = "", popular: set[str] | None = None,
) -> list[HygieneFinding]:
    """Flag names that sit one keystroke away from a well-known package.

    No network, no popularity API — the backend has neither. The output is a
    SUSPECT with the lookalike named, for a human to confirm or dismiss; it is
    never presented as a vulnerability.
    """
    known = {_canonical(p) for p in (popular if popular is not None
                                     else _POPULAR.get(ecosystem, set()))}
    if not known:
        return []
    out: list[HygieneFinding] = []
    for raw in names:
        name = str(raw)
        # Scoped npm packages (@scope/name) are registry-namespaced; imitating
        # a scope is a different (and much rarer) attack.
        bare = name.split("/")[-1] if name.startswith("@") else name
        canon = _canonical(bare)
        if len(canon) < 4 or canon in known:
            continue
        near = sorted(p for p in known if _edit_distance_at_most_1(canon, p))
        if not near:
            continue
        out.append(HygieneFinding(
            KIND_SUSPECT_NAME, "medium", ecosystem, name,
            f"SUSPECT: one character away from {near[0]!r}"
            + (f" (and {len(near) - 1} more)" if len(near) > 1 else "")
            + " — confirm this is the package you meant",
            manifest or "(manifest)", subproject))
    return out


# ─── Repo walker ─────────────────────────────────────────────────────

_SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "build", ".venv",
              "venv", "__pycache__", ".next", "target"}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _json(path: Path) -> dict | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _toml(path: Path) -> dict | None:
    text = _read(path)
    if text is None:
        return None
    try:
        import tomllib
        return tomllib.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _dirs_with(repo_path: Path, filename: str, limit: int = 8) -> list[Path]:
    found: list[tuple[int, Path]] = []
    for path in repo_path.rglob(filename):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        found.append((len(rel.parts), path.parent))
    found.sort(key=lambda t: (t[0], str(t[1])))
    seen: set[Path] = set()
    out: list[Path] = []
    for _, d in found:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out[:limit]


def check_repo(repo_path: Path) -> list[HygieneFinding]:
    """Every hygiene check across every subproject of a clone. Best-effort:
    an unreadable or exotic file is skipped, never fatal."""
    out: list[HygieneFinding] = []

    for directory in _dirs_with(repo_path, "package.json"):
        sub = str(directory.relative_to(repo_path))
        sub = "" if sub == "." else sub
        prefix = f"{sub}/" if sub else ""
        pkg = _json(directory / "package.json") or {}
        specs: dict[str, str] = {}
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            block = pkg.get(key)
            if isinstance(block, dict):
                specs.update({str(k): str(v) for k, v in block.items()})

        lock: list[LockEntry] = []
        lock_name = ""
        if (directory / "package-lock.json").is_file():
            lock_name = "package-lock.json"
            data = _json(directory / lock_name) or {}
            lock = parse_package_lock(data, prefix + lock_name)
        elif (directory / "pnpm-lock.yaml").is_file():
            lock_name = "pnpm-lock.yaml"
            lock = parse_pnpm_lock(_read(directory / lock_name) or "", prefix + lock_name)
        elif (directory / "yarn.lock").is_file():
            lock_name = "yarn.lock"
            lock = parse_yarn_lock(_read(directory / lock_name) or "", prefix + lock_name)

        if lock_name:
            out += check_lock_drift(specs, lock, ecosystem="npm",
                                    manifest=prefix + "package.json",
                                    lockfile=prefix + lock_name, subproject=sub,
                                    # yarn.lock is a flat resolution table with
                                    # no notion of "direct" — see check_lock_drift.
                                    report_extra=lock_name != "yarn.lock")
        out += check_install_scripts(pkg, lock, subproject=sub,
                                     raw=_read(directory / "package.json"),
                                     manifest=prefix + "package.json")
        out += check_non_registry(specs, lock, ecosystem="npm",
                                  manifest=prefix + "package.json", subproject=sub)
        out += check_suspect_names(list(specs), ecosystem="npm",
                                   manifest=prefix + "package.json", subproject=sub)

    for directory in _dirs_with(repo_path, "pyproject.toml"):
        sub = str(directory.relative_to(repo_path))
        sub = "" if sub == "." else sub
        prefix = f"{sub}/" if sub else ""
        data = _toml(directory / "pyproject.toml") or {}
        specs = _python_specs(data)
        lock: list[LockEntry] = []
        if (directory / "poetry.lock").is_file():
            lock = parse_poetry_lock(_read(directory / "poetry.lock") or "",
                                     prefix + "poetry.lock")
            out += check_lock_drift(specs, lock, ecosystem="PyPI",
                                    manifest=prefix + "pyproject.toml",
                                    lockfile=prefix + "poetry.lock", subproject=sub,
                                    report_extra=False)
        out += check_non_registry(specs, lock, ecosystem="PyPI",
                                  manifest=prefix + "pyproject.toml", subproject=sub)
        out += check_suspect_names(list(specs), ecosystem="PyPI",
                                   manifest=prefix + "pyproject.toml", subproject=sub)
        setup = directory / "setup.py"
        if setup.is_file():
            out += check_python_build_hooks(_read(setup), location=prefix + "setup.py",
                                            subproject=sub)

    for directory in _dirs_with(repo_path, "Cargo.toml"):
        sub = str(directory.relative_to(repo_path))
        sub = "" if sub == "." else sub
        prefix = f"{sub}/" if sub else ""
        data = _toml(directory / "Cargo.toml") or {}
        specs = {
            str(k): (v if isinstance(v, str) else str((v or {}).get("version")
                     or (v or {}).get("git") or (v or {}).get("path") or ""))
            for k, v in (data.get("dependencies") or {}).items()
        }
        lock = (parse_cargo_lock(_read(directory / "Cargo.lock") or "", prefix + "Cargo.lock")
                if (directory / "Cargo.lock").is_file() else [])
        if lock:
            out += check_lock_drift(specs, lock, ecosystem="crates.io",
                                    manifest=prefix + "Cargo.toml",
                                    lockfile=prefix + "Cargo.lock", subproject=sub,
                                    report_extra=False)
        out += check_cargo_build_script(data, (directory / "build.rs").is_file(),
                                        location=prefix + "Cargo.toml", subproject=sub)
        out += check_suspect_names(list(specs), ecosystem="crates.io",
                                   manifest=prefix + "Cargo.toml", subproject=sub)

    return _cap(out)


def _python_specs(pyproject: dict) -> dict[str, str]:
    specs: dict[str, str] = {}
    project = pyproject.get("project") or {}
    for req in project.get("dependencies") or []:
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", str(req))
        if m:
            specs[m.group(1).lower()] = m.group(2).strip()
    poetry = ((pyproject.get("tool") or {}).get("poetry") or {})
    for name, spec in (poetry.get("dependencies") or {}).items():
        if str(name).lower() == "python":
            continue
        specs[str(name).lower()] = (
            spec if isinstance(spec, str)
            else str((spec or {}).get("version") or (spec or {}).get("git")
                     or (spec or {}).get("url") or (spec or {}).get("path") or ""))
    return specs


def _cap(items: list[HygieneFinding]) -> list[HygieneFinding]:
    """Bound the payload: a repo with a stale lock can produce hundreds of
    identical-shaped rows, and a summary nobody reads helps nobody."""
    kept: list[HygieneFinding] = []
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        if counts[item.kind] <= _MAX_PER_KIND:
            kept.append(item)
    return kept


def summarise(findings: list[HygieneFinding]) -> dict:
    """Counts by kind + the findings themselves, for the run summary JSONB."""
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    return {"total": len(findings), "by_kind": by_kind,
            "items": [f.as_dict() for f in findings]}


__all__ = [
    "KIND_INSTALL_SCRIPT",
    "KIND_LOCK_DRIFT",
    "KIND_NON_REGISTRY",
    "KIND_SUSPECT_NAME",
    "HygieneFinding",
    "check_cargo_build_script",
    "check_install_scripts",
    "check_lock_drift",
    "check_non_registry",
    "check_python_build_hooks",
    "check_repo",
    "check_suspect_names",
    "classify_source",
    "summarise",
]
