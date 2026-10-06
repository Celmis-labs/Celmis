#!/usr/bin/env python3
"""Developer-request scenarios against Celmis's compact ``/mcp/dev/`` profile.

What it answers: for the requests developers actually make ("make a DB
connection like in service X", "who calls this", "where is this defined"),
does the profile return the right file and line, inside its token budget,
without a single secret value, and how much cheaper is that than a
grep-and-read agent?

Two ways to get a server::

    scripts/dev_mcp_e2e.py --spawn                    # in-process stack, fixture repos
    scripts/dev_mcp_e2e.py --spawn --pg               # same, on a throwaway Postgres
    scripts/dev_mcp_e2e.py --url https://celmis.example.com --token-env CELMIS_TOKEN \
        --src-root ~/code --repos shop,billing \
        --scenarios ~/private/scenarios.yaml          # any running Celmis

For an external server the scenario file and the output stay OUTSIDE this
repository (they may name real services): both ``--out`` and ``--scenarios``
are refused when they resolve into a git working tree that contains this
script, and ``--out`` defaults to ``~/.cache/celmis-e2e/<timestamp>``.

Scenario file schema: see ``tests/fixtures/dev_mcp/gold.yaml``.

Secrets. The runner never prints a secret value. ``--spawn`` knows its planted
canaries. For ``--url`` it loads the values of the local ``.env*`` files of the
source repositories into memory (never to disk, never to the report) and scans
every response; a hit is reported as ``leak: <tool>, <VAR_NAME>``.

The report always says whether the leak scan was armed. With ``--url`` and no ``--src-root``
it knows no secret values, so use ``--require-leak-scan`` to make that a failure, and
``--min-scenarios N`` so that scenarios skipped as "not visible to this token" cannot
leave a nearly empty run green.

Exit status: 0 when every scenario passed, no budget was exceeded and nothing
leaked; 1 otherwise; 2 for a usage error.

Not covered here: a real-agent run (``claude -p`` with and without the plugin). It spends
tokens and is not automated; try the plugin by hand on the same scenario requests.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from tests.e2e_local.client import McpClient, McpHttpError  # noqa: E402

BUNDLED_GOLD = ROOT / "tests" / "fixtures" / "dev_mcp" / "gold.yaml"
DEV_PATH = "/mcp/dev/"
CHARS_PER_TOKEN = 3.7
#: A dev tool's hard budget is about 4k tokens; anything above is a failure.
DEFAULT_MAX_CHARS = 16_000
IDX_LINE_RE = r"^idx: (?P<entries>.+)$"
IDX_ENTRY_RE = (r"(?P<slug>[A-Za-z0-9._/-]+) (?P<branch>\S+)@(?P<sha>[0-9a-f]{7,40}) "
                r"(?P<age>\S+) (?P<state>fresh|STALE|unknown)")
try:  # the server's frozen contract wins when it is on this branch
    from src.mcp_server import dev_contract as _contract

    IDX_LINE_RE, IDX_ENTRY_RE = _contract.IDX_LINE_RE, _contract.IDX_ENTRY_RE
    DEV_PATH = _contract.DEV_PATH.rstrip("/") + "/"
except ImportError:  # pragma: no cover
    pass


def est_tokens(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


# ───────────────────────────── paths and hygiene ───────────────────────────


def git_toplevel(path: Path) -> Path | None:
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        out = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.strip()).resolve()


def inside_a_checkout_of_this_tool(path: Path) -> bool:
    """True when ``path`` resolves into the git working tree that holds this script."""
    top = git_toplevel(ROOT)
    resolved = path.expanduser().resolve()
    return top is not None and (resolved == top or top in resolved.parents)


def check_paths(out: Path, scenarios: Path | None) -> None:
    if inside_a_checkout_of_this_tool(out):
        raise SystemExit(f"refusing --out {out}: it is inside the repository; real-service "
                         "results must not be written where they could be committed")
    if scenarios is not None and scenarios.resolve() != BUNDLED_GOLD.resolve() \
            and inside_a_checkout_of_this_tool(scenarios):
        raise SystemExit(f"refusing --scenarios {scenarios}: scenario files for real services "
                         "belong outside the repository")


# ───────────────────────────── scenarios ───────────────────────────────────


def load_scenarios(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "scenarios" not in data:
        raise SystemExit(f"{path}: no 'scenarios' key")
    return data


def _norm(slug: str) -> str:
    return re.sub(r"[^a-z0-9]", "", slug.lower())


def parse_idx_entries(text: str) -> list[dict[str, str]]:
    first = text.splitlines()[0] if text else ""
    m = re.match(IDX_LINE_RE, first)
    if not m:
        return []
    out = []
    for chunk in m.group("entries").split(" · "):
        e = re.fullmatch(IDX_ENTRY_RE, chunk.strip())
        if e:
            out.append(e.groupdict())
    return out


def resolve_slugs(visible: list[str], logical: list[str]) -> dict[str, str]:
    """Map logical names (``acme/shop``) to the server's slugs, when exactly one fits."""
    out: dict[str, str] = {}
    for name in logical:
        key = _norm(name)
        hits = [s for s in visible if _norm(s) == key or _norm(s).endswith(key)]
        exact = [s for s in visible if s == name]
        hits = exact or hits
        if len(hits) == 1:
            out[name] = hits[0]
    return out


def _substitute(value: Any, slugs: dict[str, str]) -> Any:
    if isinstance(value, str):
        return slugs.get(value, value)
    if isinstance(value, list):
        return [_substitute(v, slugs) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, slugs) for k, v in value.items()}
    return value


# ───────────────────────────── scoring ─────────────────────────────────────


def gold_found(text: str, gold: dict[str, Any]) -> bool:
    """A gold fact is found when the answer pins ``path`` and ``line``.

    Either ``path:start[-end]`` covers the line, or the line that carries the
    gold ``text`` also carries the line number as a token (an outline's
    ``13-30 class X`` or a numbered body). The text alone, anywhere, is not
    enough: that would be a hit for every answer that mentions the symbol.
    """
    path, line, needle = gold["path"], int(gold["line"]), gold.get("text", "")
    # ``alt_lines``: a tool may name the enclosing symbol's first line instead of
    # the call site (a reference list does); both pin the same fact.
    lines = [line, *(int(n) for n in gold.get("alt_lines", ()))]
    for m in re.finditer(re.escape(path) + r":(\d+)(?:-(\d+))?", text):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if any(start <= n <= end for n in lines):
            return True
    if needle:
        for ln in text.splitlines():
            if needle in ln and any(re.search(rf"(?<![\w.]){n}(?![\w.])", ln) for n in lines):
                return True
    return False


def _has_name(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def names_section(text: str) -> str:
    """The indented lines under every ``inputs (names only ...)`` header of a howto answer.

    A name quoted inside a code slice must not count as "listed": the answer is
    only useful when it says which names to configure and where they come from.
    """
    out: list[str] = []
    inside = False
    for ln in text.splitlines():
        if ln.startswith("inputs"):
            inside = True
        elif inside and ln[:1] in (" ", "\t"):
            out.append(ln)
        else:
            inside = False
    return "\n".join(out)


@dataclass
class Arm:
    calls: int = 0
    chars: int = 0
    gold_found: int = 0
    gold_total: int = 0
    names_missing: list[str] = field(default_factory=list)
    sources_missing: list[str] = field(default_factory=list)
    text_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    idx_missing_on: list[str] = field(default_factory=list)
    over_budget: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def tokens(self) -> int:
        return est_tokens(self.chars)

    @property
    def hit_rate(self) -> float:
        return self.gold_found / self.gold_total if self.gold_total else 1.0

    @property
    def passed(self) -> bool:
        return (not self.errors and self.hit_rate == 1.0 and not self.names_missing
                and not self.sources_missing and not self.text_missing
                and not self.idx_missing_on and not self.over_budget and not self.leaks)


def score(arm: Arm, scenario: dict[str, Any], text: str, *, check_text: bool = True) -> None:
    gold = scenario.get("gold", [])
    arm.gold_total = len(gold)
    arm.gold_found = sum(1 for g in gold if gold_found(text, g))
    listed = names_section(text)
    arm.names_missing = [n for n in scenario.get("names", []) if not _has_name(listed, n)]
    arm.sources_missing = [s for s in scenario.get("sources", []) if s not in text]
    if check_text:
        low = text.lower()
        missing = [p for p in scenario.get("guidance", []) if p.lower() not in low]
        missing += [p for p in scenario.get("warnings", []) if p.lower() not in low]
        missing += [p for p in scenario.get("expect_text", []) if p not in text]
        arm.text_missing = missing
    for forbidden in scenario.get("forbid", []):
        if forbidden in text:
            arm.errors.append("a forbidden string appeared in the answer")
    for pattern in scenario.get("forbid_regex", []):
        if re.search(pattern, text):
            arm.errors.append(f"a forbidden pattern appeared in the answer: {pattern}")


# ───────────────────────────── secrets ─────────────────────────────────────


@dataclass(frozen=True)
class Secret:
    name: str     # what is reported: a label or a variable NAME, never the value
    value: str

    def variants(self) -> list[str]:
        return [self.value, base64.b64encode(self.value.encode()).decode()]


def scan(text: str, secrets: list[Secret]) -> list[str]:
    """Names of the secrets present in ``text`` (never their values)."""
    return sorted({s.name for s in secrets if any(v in text for v in s.variants() if v)})


_DSN_PASSWORD = re.compile(r"://[^:/@\s]+:([^@\s]+)@")
_TRIVIAL = {"true", "false", "localhost", "production", "development", "postgres", "changeme"}
_ENV_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}


def load_env_secrets(src_root: Path, dirs: list[str]) -> list[Secret]:
    """Values of every local ``.env*`` file of the given source directories.

    In memory only. Values also present in the committed example files are
    public by construction and skipped, as are trivial ones.
    """
    secrets_found: list[Secret] = []
    for d in dirs:
        base = src_root / d
        if not base.is_dir():
            continue
        public: set[str] = set()
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in _ENV_SKIP_DIRS]
            if len(Path(dirpath).relative_to(base).parts) > 3:
                dirnames[:] = []
                continue
            for fn in filenames:
                if not fn.startswith(".env"):
                    continue
                if fn.endswith((".example", ".sample", ".template")):
                    public.update(_env_values(Path(dirpath) / fn).values())
                else:
                    files.append(Path(dirpath) / fn)
        for f in files:
            for key, value in _env_values(f).items():
                if len(value) < 8 or value in public or value.lower() in _TRIVIAL:
                    continue
                if value.isalpha() and len(value) < 12:
                    continue
                secrets_found.append(Secret(key, value))
                m = _DSN_PASSWORD.search(value)
                if m and len(m.group(1)) >= 6:
                    secrets_found.append(Secret(key, m.group(1)))
    return secrets_found


def _env_values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and value:
            out[key] = value
    return out


# ───────────────────────────── the two arms ────────────────────────────────


def run_celmis_arm(client: McpClient, scenario: dict[str, Any], slugs: dict[str, str],
                   secrets_: list[Secret], max_chars: int) -> tuple[Arm, str]:
    arm = Arm()
    parts: list[str] = []
    t0 = time.time()
    for step in scenario["steps"]:
        tool, args = step["tool"], _substitute(step.get("args", {}), slugs)
        arm.calls += 1
        try:
            res = client.call(tool, args)
        except McpHttpError as exc:
            arm.errors.append(f"{tool}: HTTP {exc.status}")
            continue
        except Exception as exc:  # noqa: BLE001
            arm.errors.append(f"{tool}: {type(exc).__name__}")
            continue
        text = res.text
        arm.chars += len(text)
        parts.append(text)
        if res.is_error:
            detail = text.strip().splitlines()[0][:100] if text.strip() else ""
            if scan(detail, secrets_):  # an error line is shown only when it carries no secret
                detail = "(withheld)"
            arm.errors.append(f"{tool}: tool error {detail}".rstrip())
        first = text.splitlines()[0] if text else ""
        if not re.match(IDX_LINE_RE, first):
            arm.idx_missing_on.append(tool)
        if len(text) > max_chars:
            arm.over_budget.append(f"{tool} {len(text)} chars")
        # The raw JSON escapes quotes, backslashes and newlines; the text does not.
        for name in sorted(set(scan(text, secrets_)) | set(scan(res.raw or "", secrets_))):
            arm.leaks.append(f"{tool}, {name}")
    arm.seconds = time.time() - t0
    full = "\n".join(parts)
    score(arm, scenario, full)
    return arm, full


def _git_grep(clone: Path, term: str) -> list[tuple[str, int, str]]:
    done = subprocess.run(["git", "-C", str(clone), "grep", "-n", "-I", "-F", "-e", term],
                          capture_output=True, text=True, timeout=30)
    rows = []
    for line in done.stdout.splitlines():
        m = re.match(r"^(.*?):(\d+):(.*)$", line)
        if m:
            rows.append((m.group(1), int(m.group(2)), m.group(3)))
    return rows


def run_baseline_arm(scenario: dict[str, Any], clones: dict[str, Path], top_k: int = 3,
                     context: int = 40) -> tuple[Arm | None, str]:
    """Deterministic grep-and-read agent: ``git grep`` each term, read the top files."""
    spec = scenario.get("baseline") or {}
    terms, repos = spec.get("terms", []), spec.get("repos", [])
    if not terms or not repos:
        return None, ""
    arm = Arm()
    parts: list[str] = []
    for repo in repos:
        clone = clones.get(repo)
        if clone is None:
            continue
        per_file: dict[str, list[int]] = {}
        for term in terms:
            arm.calls += 1
            rows = _git_grep(clone, term)
            for path, line, content in rows[:200]:
                parts.append(f"{path}:{line}:{content}")
                per_file.setdefault(path, []).append(line)
        for path, lines in sorted(per_file.items(), key=lambda kv: -len(kv[1]))[:top_k]:
            arm.calls += 1
            try:
                body = (clone / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            lo, hi = (1, len(body)) if len(body) <= 300 else (
                max(1, min(lines) - context), min(len(body), min(lines) + context))
            parts.extend(f"{path}:{n}:{body[n - 1]}" for n in range(lo, hi + 1))
    text = "\n".join(parts)
    arm.chars = len(text)
    score(arm, scenario, text, check_text=False)
    arm.errors = []  # the baseline has no contract to violate
    return arm, text


# ───────────────────────────── the run ─────────────────────────────────────


@dataclass
class ScenarioResult:
    id: str
    ask: str
    skipped: str = ""
    celmis: Arm | None = None
    baseline: Arm | None = None
    baseline_exposes: list[str] = field(default_factory=list)


@dataclass
class Report:
    endpoint: str
    results: list[ScenarioResult]
    tools_list_chars: int | None = None
    tools_list_count: int | None = None
    leaks: list[str] = field(default_factory=list)
    extra_leaks: list[str] = field(default_factory=list)
    #: how many secret names the leak scan knew; 0 means it was NOT armed
    leak_scan_names: int = 0
    require_leak_scan: bool = False
    min_scenarios: int = 1

    @property
    def ran(self) -> list[ScenarioResult]:
        return [r for r in self.results if not r.skipped]

    @property
    def leak_scan_armed(self) -> bool:
        return self.leak_scan_names > 0

    @property
    def ok(self) -> bool:
        ran = self.ran
        return len(ran) >= max(1, self.min_scenarios) \
            and all(r.celmis and r.celmis.passed for r in ran) \
            and not self.leaks and not self.extra_leaks \
            and (self.leak_scan_armed or not self.require_leak_scan)


def run_scenarios(client: McpClient, data: dict[str, Any], *, clones: dict[str, Path],
                  secrets_: list[Secret], only: set[str] | None = None, baseline: bool = True,
                  max_chars: int = DEFAULT_MAX_CHARS, repos_filter: set[str] | None = None,
                  require_leak_scan: bool = False, min_scenarios: int = 1) -> Report:
    report = Report(endpoint=client.url, results=[], leak_scan_names=len({s.name for s in secrets_}),
                    require_leak_scan=require_leak_scan, min_scenarios=min_scenarios)
    try:
        tools = client.list_tools()
        report.tools_list_count = len(tools)
        report.tools_list_chars = len(json.dumps(tools, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        report.extra_leaks.append(f"tools/list failed: {type(exc).__name__}")
    logical = sorted({r for s in data["scenarios"] for st in s["steps"]
                      for r in _repo_names(st.get("args", {}), data)})
    visible: list[str] = []
    try:
        listing = client.call("repos", {})
        visible = [e["slug"] for e in parse_idx_entries(listing.text)]
    except Exception:  # noqa: BLE001
        pass
    slugs = resolve_slugs(visible, logical)
    for scenario in data["scenarios"]:
        if only and scenario["id"].split("-")[0] not in only and scenario["id"] not in only:
            continue
        result = ScenarioResult(id=scenario["id"], ask=scenario.get("ask", ""))
        needed = {r for st in scenario["steps"] for r in _repo_names(st.get("args", {}), data)}
        if repos_filter is not None and not (needed <= {r for r in repos_filter}):
            result.skipped = "repository filtered out by --repos"
        elif needed - set(slugs):
            result.skipped = "not visible to this token: " + ", ".join(sorted(needed - set(slugs)))
        else:
            result.celmis, text = run_celmis_arm(client, scenario, slugs, secrets_, max_chars)
            report.leaks += [f"leak: {leak}" for leak in result.celmis.leaks
                             if f"leak: {leak}" not in report.leaks]
            if baseline:
                result.baseline, btext = run_baseline_arm(scenario, clones)
                result.baseline_exposes = scan(btext, secrets_)
        report.results.append(result)
    return report


def _repo_names(args: dict[str, Any], data: dict[str, Any]) -> list[str]:
    known = set((data.get("repos") or {}).keys())
    out = []
    for key in ("repo", "repos"):
        v = args.get(key)
        for item in ([v] if isinstance(v, str) else (v or [])):
            if item and (not known or item in known):
                out.append(item)
    return out


# ───────────────────────────── rendering ───────────────────────────────────


def render(report: Report) -> str:
    lines = [f"endpoint {report.endpoint}"]
    if report.tools_list_chars is not None:
        lines.append(f"tools/list: {report.tools_list_count} tools, {report.tools_list_chars} chars "
                     f"(~{est_tokens(report.tools_list_chars)} tokens)")
    lines.append("")
    lines.append(f"{'scenario':34} {'celmis':>28} {'baseline':>26} {'saving':>7}")
    t_c = t_b = 0
    for r in report.results:
        if r.skipped:
            lines.append(f"{r.id:34} SKIPPED ({r.skipped})")
            continue
        c, b = r.celmis, r.baseline
        cs = (f"{'PASS' if c.passed else 'FAIL'} {c.calls}c {c.tokens}t "
              f"{c.gold_found}/{c.gold_total}")
        bs = f"{b.calls}c {b.tokens}t {b.gold_found}/{b.gold_total}" if b else "-"
        saving = f"{round(100 * (1 - c.tokens / b.tokens))}%" if b and b.tokens else "-"
        if b:
            t_c, t_b = t_c + c.tokens, t_b + b.tokens
        lines.append(f"{r.id:34} {cs:>28} {bs:>26} {saving:>7}")
        for what, items in (("names missing", c.names_missing), ("sources missing", c.sources_missing),
                            ("text missing", c.text_missing), ("no idx line on", c.idx_missing_on),
                            ("over budget", c.over_budget), ("errors", c.errors)):
            if items:
                lines.append(f"    {what}: {', '.join(items)}")
        if r.baseline_exposes:
            lines.append(f"    (the grep baseline would have shown: {', '.join(r.baseline_exposes)})")
    if t_b:
        lines.append(f"\ntotal over scenarios with a baseline: celmis ~{t_c} tokens, baseline ~{t_b} "
                     f"tokens, {round(100 * (1 - t_c / t_b))}% fewer (fixtures are tiny: reported, "
                     "not gated)")
    skipped = len(report.results) - len(report.ran)
    lines.append(f"scenarios: {len(report.ran)} ran, {skipped} skipped"
                 + (f" (at least {report.min_scenarios} must run)" if report.min_scenarios > 1 else ""))
    if report.leak_scan_armed:
        lines.append(f"leak scan: armed with {report.leak_scan_names} secret names")
    else:
        lines.append("leak scan: NOT armed (no known secret values: pass --src-root with the "
                     "source repositories, or use --spawn); a PASS says nothing about leaks")
    for leak in report.leaks + report.extra_leaks:
        lines.append(leak)
    if report.require_leak_scan and not report.leak_scan_armed:
        lines.append("failed: --require-leak-scan and the scan was not armed")
    if len(report.ran) < report.min_scenarios:
        lines.append(f"failed: only {len(report.ran)} scenarios ran, --min-scenarios is "
                     f"{report.min_scenarios}")
    lines.append("RESULT " + ("PASS" if report.ok else "FAIL"))
    return "\n".join(lines)


def report_json(report: Report) -> dict[str, Any]:
    def arm(a: Arm | None):
        if a is None:
            return None
        d = asdict(a)
        d.update(tokens=a.tokens, hit_rate=a.hit_rate, passed=a.passed)
        return d

    return {"endpoint": report.endpoint, "ok": report.ok,
            "leak_scan_armed": report.leak_scan_armed, "leak_scan_names": report.leak_scan_names,
            "scenarios_ran": len(report.ran), "scenarios_skipped": len(report.results) - len(report.ran),
            "tools_list_chars": report.tools_list_chars,
            "tools_list_count": report.tools_list_count,
            "leaks": report.leaks + report.extra_leaks,
            "scenarios": [{"id": r.id, "ask": r.ask, "skipped": r.skipped,
                           "celmis": arm(r.celmis), "baseline": arm(r.baseline),
                           "baseline_exposes": r.baseline_exposes} for r in report.results]}


# ───────────────────────────── entry point ─────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--spawn", action="store_true", help="start an in-process Celmis on fixtures")
    src.add_argument("--url", help="base URL of a running Celmis (no /mcp suffix)")
    p.add_argument("--pg", action="store_true", help="with --spawn: run on a throwaway Postgres")
    p.add_argument("--token-env", default="CELMIS_TOKEN", help="env var holding the bearer token")
    p.add_argument("--src-root", type=Path, help="directory holding local clones (for baseline "
                   "and the leak scan)")
    p.add_argument("--repos", help="comma-separated logical repos or directories to include")
    p.add_argument("--scenarios", type=Path, help="scenario YAML (default: the bundled fixtures)")
    p.add_argument("--only", help="comma-separated scenario ids or prefixes (S1,S3)")
    p.add_argument("--out", type=Path, help="report directory (default ~/.cache/celmis-e2e/<ts>)")
    p.add_argument("--path", default=DEV_PATH, help=f"MCP endpoint path (default {DEV_PATH})")
    p.add_argument("--no-baseline", action="store_true")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p.add_argument("--require-leak-scan", action="store_true",
                   help="fail when no secret values are known to scan for (--url without --src-root)")
    p.add_argument("--min-scenarios", type=int, default=1,
                   help="fail unless at least this many scenarios ran (the rest were skipped as "
                        "not visible to the token)")
    return p


def _spawn_pg() -> tuple[str, str]:
    """Start a throwaway Postgres container; returns (async url, container id)."""
    import secrets as _secrets
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pw = _secrets.token_urlsafe(18)
    cid = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:5432",
         "-e", f"POSTGRES_PASSWORD={pw}", "-e", "POSTGRES_DB=celmis_e2e", "postgres:17-alpine"],
        capture_output=True, text=True, check=True).stdout.strip()
    deadline = time.time() + 60
    while time.time() < deadline:
        ready = subprocess.run(["docker", "exec", cid, "pg_isready", "-U", "postgres"],
                               capture_output=True)
        if ready.returncode == 0:
            time.sleep(1.5)
            break
        time.sleep(1)
    return f"postgresql+asyncpg://postgres:{pw}@127.0.0.1:{port}/celmis_e2e", cid


def main(argv: list[str] | None = None) -> int:
    """Run; logger levels are lowered for the run and put back after (tests call this)."""
    import logging

    names = ("mcp", "httpx", "uvicorn", "src", "alembic")
    before = {n: logging.getLogger(n).level for n in names}
    try:
        return _main(argv)
    finally:
        for n, level in before.items():
            logging.getLogger(n).setLevel(level)


def _main(argv: list[str] | None) -> int:
    args = _parser().parse_args(argv)
    import logging

    for noisy in ("mcp", "httpx", "uvicorn", "src", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = (args.out or Path("~/.cache/celmis-e2e") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out = out.expanduser()
    check_paths(out, args.scenarios)
    scenario_path = args.scenarios or BUNDLED_GOLD
    data = load_scenarios(scenario_path)
    only = set(args.only.split(",")) if args.only else None
    wanted = set(args.repos.split(",")) if args.repos else None

    pg_container = None
    stack = None
    try:
        if args.spawn:
            from tests.e2e_local.stack import Stack

            tmp = Path(tempfile.mkdtemp(prefix="celmis-e2e-"))
            db_url = None
            if args.pg:
                db_url, pg_container = _spawn_pg()
            stack = Stack(tmp, database_url=db_url)
            stack.start()
            url = stack.url
            token = stack.token("dev", repos=["*"])
            clones = dict(stack.world.repos) if stack.world else {}
            secrets_ = [Secret(k, v) for k, v in (stack.world.canaries.items() if stack.world else [])]
        else:
            url = args.url
            token = os.environ.get(args.token_env, "")
            if not token:
                print(f"error: ${args.token_env} is empty", file=sys.stderr)
                return 2
            clones, secrets_ = {}, []
            if args.src_root:
                for logical, spec in (data.get("repos") or {}).items():
                    if wanted is None or logical in wanted or spec.get("dir") in wanted:
                        d = args.src_root.expanduser() / spec["dir"]
                        if d.is_dir():
                            clones[logical] = d
                secrets_ = load_env_secrets(args.src_root.expanduser(),
                                            [p.name for p in clones.values()])
                print(f"leak scan armed with {len({s.name for s in secrets_})} variable names "
                      "from local .env files (values stay in memory)")
        client = McpClient(url, args.path, token)
        report = run_scenarios(client, data, clones=clones, secrets_=secrets_, only=only,
                               baseline=not args.no_baseline, max_chars=args.max_chars,
                               repos_filter=wanted, require_leak_scan=args.require_leak_scan,
                               min_scenarios=args.min_scenarios)
        if stack is not None:
            for label in scan(stack.log_text(), secrets_):
                report.extra_leaks.append(f"leak: server log, {label}")
            for label in scan(json.dumps(stack.audit_rows(), default=str), secrets_):
                report.extra_leaks.append(f"leak: audit row, {label}")
    finally:
        if stack is not None:
            stack.stop()
            shutil.rmtree(stack.root, ignore_errors=True)
        if pg_container:
            subprocess.run(["docker", "stop", pg_container], capture_output=True)

    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    (out / "report.json").write_text(json.dumps(report_json(report), indent=2), encoding="utf-8")
    text = render(report)
    (out / "report.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"report: {out}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
