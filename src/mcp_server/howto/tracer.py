"""From the code slices to the *names* an agent has to supply, and where each
value comes from.

The output of this module is names, files, line numbers and the *form* a
value takes at its source (``${REF}``, ``secretKeyRef``, "literal, withheld",
"deployment variable"). It never carries a value: a source line is read only
to classify it, and nothing from the right-hand side of an assignment is kept
except a reference that is itself a name (``${DATABASE_URL}``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from src.security.secret_files import classify, plain_file

logger = logging.getLogger(__name__)

MAX_TRACE_FILE_BYTES = 200_000
MAX_SOURCES_PER_NAME = 6

# ─── Names ───────────────────────────────────────────────────────────

_ENV_NAME = r"[A-Za-z_][A-Za-z0-9_.\-:]{1,80}"

# (regex, default_group) - ``n`` is the name; ``d`` marks a default in code.
_READ_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (
        # python / php / ruby / generic call forms
        rf"""\b(?:os\.getenv|os\.environ\.get|environ\.get|getenv|env|config|ENV\.fetch|ENV)\s*\(\s*['"](?P<n>{_ENV_NAME})['"]\s*(?P<d>,[^)]*)?\)""",
        rf"""\bos\.environ\[\s*['"](?P<n>{_ENV_NAME})['"]\s*\]""",
        rf"""\$_(?:ENV|SERVER)\[\s*['"](?P<n>{_ENV_NAME})['"]\s*\](?P<d>\s*\?\?)?""",
        # js / ts
        r"""\bprocess\.env\.(?P<n>[A-Za-z_][A-Za-z0-9_]*)(?P<d>\s*(?:\|\||\?\?)\s*['"\w])?""",
        rf"""\bprocess\.env\[\s*['"](?P<n>{_ENV_NAME})['"]\s*\](?P<d>\s*(?:\|\||\?\?)\s*['"\w])?""",
        r"""\bimport\.meta\.env\.(?P<n>[A-Za-z_][A-Za-z0-9_]*)(?P<d>\s*(?:\|\||\?\?)\s*['"\w])?""",
        rf"""\bconfigService\.get(?:OrThrow)?(?:<[^>]*>)?\(\s*['"](?P<n>{_ENV_NAME})['"]\s*(?P<d>,[^)]*)?\)""",
        # go
        rf"""\bos\.(?:Getenv|LookupEnv)\(\s*"(?P<n>{_ENV_NAME})"\s*\)""",
        rf"""\bviper\.(?:Get\w*|BindEnv|SetDefault)\(\s*"(?P<n>{_ENV_NAME})"\s*(?P<d>,[^)]*)?\)""",
        rf"""`(?:envconfig|env):"(?P<n>{_ENV_NAME})"(?P<d>[^`]*(?:default|envDefault)[^`]*)?`""",
        # jvm
        rf"""\bSystem\.getenv\(\s*"(?P<n>{_ENV_NAME})"\s*\)""",
        rf"""@Value\(\s*"\$\{{(?P<n>{_ENV_NAME})(?P<d>:[^}}]*)?\}}"\s*\)""",
        # .net
        rf"""\bGetEnvironmentVariable\(\s*"(?P<n>{_ENV_NAME})"\s*\)""",
        rf"""\bConfiguration\[\s*"(?P<n>{_ENV_NAME})"\s*\]""",
        rf"""\bGetConnectionString\(\s*"(?P<n>{_ENV_NAME})"\s*\)""",
        # placeholders in config files / compose / shell
        rf"""\$\{{(?P<n>{_ENV_NAME})(?P<d>:-?[^}}]*)?\}}""",
    )
)
_SETTINGS_ATTR = re.compile(
    r"\b(?:settings|get_settings\(\)|cfg|conf|config|CONFIG|SETTINGS)\.(?P<n>[a-z_][a-z0-9_]{1,60})\b(?!\()"
)
_SETTINGS_CLASS = re.compile(
    r"^class\s+(?P<cls>\w+)\s*\((?P<bases>[^)]*BaseSettings[^)]*)\)\s*:", re.MULTILINE
)
_PREFIX = re.compile(r"""env_prefix\s*=\s*['"](?P<p>[^'"]*)['"]""")
_FIELD = re.compile(
    r"^(?P<ind>[ \t]+)(?P<f>[a-z_][a-z0-9_]*)\s*:\s*(?P<t>[^=\n#]+?)\s*(?:=\s*(?P<d>[^#\n]+?))?\s*(?:#.*)?$",
    re.IGNORECASE,
)
_ALIAS = re.compile(r"""(?:alias|validation_alias|env)\s*=\s*['"](?P<a>[A-Za-z_][\w.\-]*)['"]""")

_SECRET_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|PASSPHRASE|PWD|SECRET|TOKEN|API[_-]?KEY|APIKEY|PRIVATE[_-]?KEY|CREDENTIAL|"
    r"DATABASE[_-]?URL|DB[_-]?URL|DSN|CONN(?:ECTION)?[_-]?STR(?:ING)?|MONGO[_-]?URI|REDIS[_-]?URL|"
    r"AMQP[_-]?URL|BROKER[_-]?URL|ACCESS[_-]?KEY|SIGNING[_-]?KEY|ENCRYPTION[_-]?KEY|SALT|CLIENT[_-]?SECRET)",
    re.IGNORECASE,
)
_NOT_A_SECRET_SUFFIX = re.compile(r"(?:_|\.|-)?(?:FILE|PATH|NAME|ENV|ID|TTL|TYPE|TOKENS|LIMIT|COUNT|SIZE|URI_TEMPLATE|HEADER|PREFIX|EXPIRES?\w*)$", re.IGNORECASE)


def classify_name(name: str) -> str:
    """``secret`` when the name suggests a value that must not be shared."""
    if _SECRET_NAME.search(name) and not _NOT_A_SECRET_SUFFIX.search(name):
        return "secret"
    return "config"


@dataclass
class NameInfo:
    name: str
    kind: str = "config"
    reads: list[str] = field(default_factory=list)   # "src/config.py:15"
    default_in_code: bool | None = None
    sources: list[str] = field(default_factory=list)
    via: str = ""                                    # "settings.db_url" when the env name is derived

    def add_read(self, loc: str) -> None:
        if loc not in self.reads and len(self.reads) < 3:
            self.reads.append(loc)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# A helper that takes the variable name: ``required("DATABASE_URL")``,
# ``mustEnv("JWT_KEY")``. Only trusted inside a config loader file, where a
# SCREAMING_SNAKE literal handed to a function is an environment name.
_HELPER_READ = re.compile(
    r"""\b[A-Za-z_]\w*\(\s*['"](?P<n>[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)['"]\s*(?P<d>,[^)]*)?\)"""
)


def extract_names(
    text: str,
    rel: str,
    first_line: int,
    *,
    keep: Callable[[str], bool] | None = None,
    loader: bool = False,
) -> dict[str, NameInfo]:
    """Env/setting names read in ``text`` (a slice starting at ``first_line``).

    ``loader`` marks a config loader file, where helper calls that take an
    upper-case name (``required("DATABASE_URL")``) count as reads too.
    """
    found: dict[str, NameInfo] = {}
    for rx in (*_READ_PATTERNS, *((_HELPER_READ,) if loader else ())):
        for m in rx.finditer(text):
            name = m.group("n")
            if keep is not None and not keep(name):
                continue
            info = found.setdefault(name, NameInfo(name, classify_name(name)))
            info.add_read(f"{rel}:{first_line + _line_of(text, m.start()) - 1}")
            has_default = bool(m.groupdict().get("d"))
            if info.default_in_code is None or has_default:
                info.default_in_code = has_default
    return found


def attr_refs(text: str) -> set[str]:
    """``settings.database_url`` style references (lower-case attribute names)."""
    return {m.group("n") for m in _SETTINGS_ATTR.finditer(text)}


@dataclass
class SettingsField:
    env: str
    field: str
    cls: str
    loc: str
    has_default: bool
    cls_line: int = 1


def parse_settings_classes(text: str, rel: str) -> list[SettingsField]:
    """Fields of pydantic ``BaseSettings`` classes, with their env names."""
    out: list[SettingsField] = []
    lines = text.split("\n")
    for m in _SETTINGS_CLASS.finditer(text):
        start = _line_of(text, m.start())
        body: list[tuple[int, str]] = []
        for i in range(start, len(lines)):
            line = lines[i]
            if line.strip() and not line.startswith((" ", "\t")):
                break
            body.append((i + 1, line))
        body_text = "\n".join(b for _, b in body)
        prefix = ""
        pm = _PREFIX.search(body_text)
        if pm:
            prefix = pm.group("p")
        for lineno, line in body:
            fm = _FIELD.match(line)
            if not fm or fm.group("f") in ("model_config",) or "ClassVar" in fm.group("t"):
                continue
            if len(fm.group("ind").replace("\t", "    ")) > 4:
                continue
            d = fm.group("d") or ""
            am = _ALIAS.search(d)
            env = am.group("a") if am else f"{prefix}{fm.group('f')}".upper()
            has_default = bool(d) and not re.match(r"^(?:Field\(\s*\.\.\.|\.\.\.)", d.strip())
            if d.strip().startswith("Field(") and "default" not in d and not re.match(r"Field\(\s*[^.\s)]", d.strip()):
                has_default = False
            out.append(SettingsField(env=env, field=fm.group("f"), cls=m.group("cls"),
                                     loc=f"{rel}:{lineno}", has_default=has_default, cls_line=start))
    return out


# ─── Secret stores (names of places, never values) ───────────────────

_STORE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("vault", re.compile(r"""\bvault:(?P<p>[\w/.\-#]+)""")),
    ("vault", re.compile(r"""read_secret_version\(\s*(?:path\s*=\s*)?['"](?P<p>[^'"]+)['"]""")),
    ("aws-secrets-manager", re.compile(r"""get_secret_value\(\s*SecretId\s*=\s*['"](?P<p>[^'"]+)['"]""")),
    ("docker-secret", re.compile(r"""/run/secrets/(?P<p>[\w.\-]+)""")),
    ("k8s-secret", re.compile(r"""secretKeyRef:\s*\{?\s*name:\s*(?P<p>[\w.\-]+)""")),
    ("gcp-secret-manager", re.compile(r"""projects/[\w\-]+/secrets/(?P<p>[\w\-]+)""")),
)


#: What a secret store's NAME may look like. It is text the repository wrote,
#: read by a model: anything else (a newline, a sentence, markup) is how an
#: instruction ends up looking like part of the answer, so such a name is dropped.
_STORE_NAME = re.compile(r"^[A-Za-z0-9_./:#\-]{1,80}$")
_SAFE_REL = re.compile(r"^[\w./@+\- ]{1,200}$")


def safe_rel(rel: str) -> str:
    """A repo path that is safe to print on one line (else a stand-in)."""
    return rel if _SAFE_REL.match(rel or "") else "<path withheld>"


_SAFE_SYMBOL = re.compile(r"^[\w.$<>:\-]{1,120}$")


def safe_symbol(name: str) -> str:
    return name if _SAFE_SYMBOL.match(name or "") else ""


def find_secret_stores(text: str, rel: str, first_line: int) -> list[str]:
    out: list[str] = []
    where = safe_rel(rel)
    for kind, rx in _STORE_PATTERNS:
        for m in rx.finditer(text):
            name = m.group("p")
            if not _STORE_NAME.match(name):
                continue
            out.append(f"{kind} {name} ({where}:{first_line + _line_of(text, m.start()) - 1})")
    return list(dict.fromkeys(out))


# ─── Where the values come from ──────────────────────────────────────

_COMPOSE = re.compile(r"(?:^|/)(?:docker-)?compose[\w.\-]*\.ya?ml$", re.IGNORECASE)
_ENV_EXAMPLE_EXACT = re.compile(r"(?:^|/)(?:\.env\.[\w.\-]*(?:example|sample|template|dist|defaults)|[\w.\-]*\.env\.(?:example|sample|template)|env\.example|example\.env)$", re.IGNORECASE)
_CI_KIND: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|/)bitbucket-pipelines\.ya?ml$"), "bitbucket"),
    (re.compile(r"(?:^|/)\.gitlab-ci\.ya?ml$"), "gitlab"),
    (re.compile(r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$"), "github"),
    (re.compile(r"(?:^|/)azure-pipelines\.ya?ml$"), "azure"),
    (re.compile(r"(?:^|/)\.circleci/config\.ya?ml$"), "circleci"),
    (re.compile(r"(?:^|/)Jenkinsfile$"), "jenkins"),
)
_DOCKERFILE = re.compile(r"(?:^|/)(?:[\w.\-]*\.)?Dockerfile(?:\.[\w.\-]+)?$|(?:^|/)Dockerfile[\w.\-]*$")
_APPCONF = re.compile(
    r"(?:^|/)(?:application[\w\-]*\.(?:ya?ml|properties)|appsettings[\w.\-]*\.json|values[\w.\-]*\.ya?ml|"
    r"config[\w.\-]*\.(?:ya?ml|toml|ini)|[\w.\-]*\.conf)$",
    re.IGNORECASE,
)
_K8S_HINT = re.compile(r"secretKeyRef|configMapKeyRef|valueFrom", re.IGNORECASE)
_YAML = re.compile(r"\.ya?ml$", re.IGNORECASE)

_CI_LABEL = {
    "bitbucket": "pipeline/deployment variable (Bitbucket)",
    "gitlab": "CI/CD variable (GitLab)",
    "github": "Actions secret/variable (GitHub)",
    "azure": "pipeline variable (Azure)",
    "circleci": "project/context variable (CircleCI)",
    "jenkins": "Jenkins credential/parameter",
}


def trace_sources(
    names: dict[str, NameInfo],
    repo_path: Path,
    files: Iterable[str],
    *,
    visible: Callable[[str], bool] = lambda _p: True,
) -> None:
    """Fill ``NameInfo.sources`` for ``names`` from the repo's own files."""
    if not names:
        return
    want = sorted(names, key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in want)
    name_rx = re.compile(rf"(?<![A-Za-z0-9_.])({alt})(?![A-Za-z0-9_])")
    # dotted names (spring.datasource.url) are also written as nested yaml; keep to flat forms.

    for rel in files:
        kind = _file_kind(rel)
        if kind is None or not visible(rel):
            continue
        verdict = classify(rel)
        if verdict == "deny":
            continue
        fp = plain_file(repo_path, rel)
        if fp is None:
            continue
        try:
            if fp.stat().st_size > MAX_TRACE_FILE_BYTES:
                continue
            raw = fp.read_bytes()
        except OSError:
            continue
        if classify(rel, raw[:8192]) == "deny":  # content sniff: kind: Secret, private keys
            continue
        text = raw.decode("utf-8", errors="replace")
        if kind == "k8s" and not _K8S_HINT.search(text):
            continue
        if not name_rx.search(text):
            continue
        {
            "env-example": _trace_env_example,
            "compose": _trace_compose,
            "ci": _trace_ci,
            "k8s": _trace_k8s,
            "dockerfile": _trace_dockerfile,
            "appconf": _trace_appconf,
        }[kind](rel, text, name_rx, names)


def _file_kind(rel: str) -> str | None:
    p = rel.replace("\\", "/")
    if _ENV_EXAMPLE_EXACT.search(p):
        return "env-example"
    if _COMPOSE.search(p):
        return "compose"
    if any(rx.search(p) for rx, _ in _CI_KIND):
        return "ci"
    if _DOCKERFILE.search(p):
        return "dockerfile"
    if _APPCONF.search(p):
        return "appconf"
    if _YAML.search(p):
        return "k8s"
    return None


def _add(names: dict[str, NameInfo], name: str, src: str) -> None:
    info = names.get(name)
    if info is not None and src not in info.sources and len(info.sources) < MAX_SOURCES_PER_NAME:
        info.sources.append(src)


def _trace_env_example(rel: str, text: str, rx: re.Pattern[str], names: dict[str, NameInfo]) -> None:
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][\w.\-]*)\s*=", line)
        if m and m.group(1) in names:
            _add(names, m.group(1), f"{rel}:{i}")


def _value_form(rest: str) -> str:
    """The *form* of a compose/CI value. Only a reference keeps its text."""
    v = rest.strip().strip("'\"")
    if not v or v == "~":
        return "passthrough (value from host env / env_file)"
    m = re.match(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)", v)
    if m:
        return f"${{{m.group(1)}}}"
    return "literal (value withheld)"


def _trace_compose(rel: str, text: str, rx: re.Pattern[str], names: dict[str, NameInfo]) -> None:
    service = ""
    in_services = False
    svc_indent: int | None = None
    child_indent: int | None = None
    block = ""          # "environment" | "env_file" | "secrets" | ""
    env_files: dict[str, list[str]] = {}
    for i, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_services = stripped.startswith("services:")
            service, block, svc_indent, child_indent = "", "", None, None
            continue
        if not in_services:
            continue
        if svc_indent is None:
            svc_indent = indent
        if indent == svc_indent:
            service, block, child_indent = stripped.rstrip(":").strip("'\""), "", None
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            key, _, tail = stripped.partition(":")
            block = key.strip() if key.strip() in ("environment", "env_file", "secrets") else ""
            tail = tail.strip()
            if block == "env_file" and tail:
                env_files.setdefault(service, []).append(tail.strip("[]'\" "))
            continue
        if block == "env_file" and stripped.startswith("-"):
            env_files.setdefault(service, []).append(stripped[1:].strip().strip("'\""))
            continue
        m = rx.search(stripped)
        if not m:
            continue
        name = m.group(1)
        if block == "environment":
            if stripped[: m.start()].lstrip("- ").strip():
                continue  # the name sits inside a value, not in key position
            sep = re.match(r"^\s*[:=]\s?(.*)$", stripped[m.end():])
            form = _value_form(sep.group(1)) if sep else "passthrough (value from host env / env_file)"
            _add(names, name, f"{rel}:{i} {service or '?'}.environment {form}")
        elif block == "secrets":
            _add(names, name, f"{rel}:{i} {service or '?'} docker secret")
    # An env_file is named, never opened here (it is usually a denied .env).
    for svc, files in env_files.items():
        for f in files[:2]:
            tag = f"{rel} {svc}.env_file {f} (not readable; may define it)"
            for info in names.values():
                if info.kind == "secret" and not info.sources:
                    info.sources.append(tag)


def _trace_ci(rel: str, text: str, rx: re.Pattern[str], names: dict[str, NameInfo]) -> None:
    kind = next(k for r, k in _CI_KIND if r.search(rel.replace("\\", "/")))
    label = _CI_LABEL[kind]
    for i, line in enumerate(text.split("\n"), 1):
        for m in rx.finditer(line):
            name = m.group(1)
            before = line[: m.start()]
            after = line[m.end():]
            if before.rstrip().endswith(("secrets.", "secrets .")) or "secrets." in before[-10:]:
                what = "Actions secret (GitHub)"
            elif "vars." in before[-8:]:
                what = "Actions variable (GitHub)"
            elif re.match(r"^\s*:", after) and not before.strip().strip("-").strip():
                what = f"set in {kind} config (value withheld)"
            else:
                what = label
            _add(names, name, f"{rel}:{i} {what}")
            break


def _trace_dockerfile(rel: str, text: str, rx: re.Pattern[str], names: dict[str, NameInfo]) -> None:
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(r"^\s*(ARG|ENV)\s+([A-Za-z_]\w*)(\s*=\s*(.*))?", line)
        if m and m.group(2) in names:
            has_default = bool(m.group(3) and m.group(4))
            _add(names, m.group(2), f"{rel}:{i} Dockerfile {m.group(1)} (default in image: {'yes' if has_default else 'no'})")


def _trace_k8s(rel: str, text: str, rx: re.Pattern[str], names: dict[str, NameInfo]) -> None:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^\s*-?\s*name:\s*['\"]?([A-Za-z_][\w.\-]*)['\"]?\s*$", line)
        if not m or m.group(1) not in names:
            continue
        indent = len(line) - len(line.lstrip())
        window = lines[i + 1: i + 9]
        sec = cm = key = ref = None
        for w in window:
            if w.strip() and len(w) - len(w.lstrip()) < indent - 2:
                break
            if re.match(r"^\s*-\s*name:", w) and len(w) - len(w.lstrip()) <= indent:
                break
            if "secretKeyRef" in w:
                sec = True
            if "configMapKeyRef" in w:
                cm = True
            mm = re.match(r"^\s*(?:name|key):\s*['\"]?([\w.\-]+)['\"]?\s*$", w)
            if mm and (sec or cm):
                if w.strip().startswith("name:") and ref is None:
                    ref = mm.group(1)
                elif w.strip().startswith("key:"):
                    key = mm.group(1)
        if sec or cm:
            what = "secretKeyRef" if sec else "configMapKeyRef"
            _add(names, m.group(1), f"{rel}:{i + 1} k8s {what} {ref or '?'}/{key or m.group(1)}")
        elif any(w.strip().startswith("value:") for w in window[:2]):
            _add(names, m.group(1), f"{rel}:{i + 1} k8s env literal (value withheld)")


def _trace_appconf(rel: str, text: str, rx: re.Pattern[str], names: dict[str, NameInfo]) -> None:
    is_values = PurePosixPath(rel).name.lower().startswith("values")
    for i, line in enumerate(text.split("\n"), 1):
        m = rx.search(line)
        if not m:
            continue
        name = m.group(1)
        before = line[: m.start()]
        if before.rstrip().endswith("${") or "${" in line[max(0, m.start() - 2): m.start()]:
            _add(names, name, f"{rel}:{i} placeholder ${{{name}}}")
        elif re.match(r"^\s*[\"']?$", before) and re.match(r"^[\"']?\s*[:=]", line[m.end():]):
            what = "Helm values key" if is_values else "config key (value withheld)"
            _add(names, name, f"{rel}:{i} {what}")


__all__ = [
    "NameInfo",
    "SettingsField",
    "attr_refs",
    "classify_name",
    "extract_names",
    "find_secret_stores",
    "parse_settings_classes",
    "trace_sources",
]
