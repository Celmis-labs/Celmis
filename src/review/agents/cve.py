"""CVE review agent — deterministic advisory lookup for the PR's own
dependency changes. No LLM anywhere in this file.

Why this exists as its own agent:
    The security agent's prompt lists OWASP A06 "Vulnerable Components", but a
    model is given no package versions and no vulnerability data — it "knows"
    CVEs up to its training date and invents the rest. Meanwhile the diff
    parser files every lockfile under `skipped_files` as review noise, so a PR
    adding lodash@4.17.15 with a known RCE used to pass review without a word
    and the standalone dependency audit noticed only after the merge.

    The data is exact — purl + version → advisory — so asking a model to relay
    it would add latency, cost and a hallucination channel to something a
    lookup answers. StructuralAgent is the shipped precedent for an agent with
    no model in it; this one mirrors its shape, which also means operators can
    disable it per repo like any other (`disabled_agents`), it shows up in
    `agents_run`/`agents_failed` honestly, and its findings are provably
    deterministic instead of sharing the security model's reputation.

Scope — the PR's OWN dependency changes, nothing else:
    The standalone audit (src/deps) owns the whole repository; this agent owns
    the delta. It reads the diff — `pr.hunks` for the manifests that survive
    skip-pattern filtering, `pr.raw_diff` for the lockfiles that did not (the
    raw text is the provider's full diff, kept precisely for fallback parsing)
    — and extracts the packages the PR ADDS or moves to a NEW version.

The parsing cut, and what it misses:
    Full lockfile grammars are a swamp, and the osv-scanner binary cannot be
    handed a diff fragment — a modified lockfile's hunks are not a parseable
    file, and the local checkout is the indexed base, not the PR head. So the
    cut is: parse the DIFF ourselves, but only formats where the package name
    and an exact version are on the changed line or within its context lines
    (lockfiles pin exact versions, which is what OSV needs), then hand the
    LOOKUP to the shipped machinery — the osv-scanner binary over a generated
    CycloneDX SBOM of the extracted packages when the binary is present, the
    OSV querybatch API otherwise. Known misses, on purpose:
      * Maven/Gradle/NuGet/SwiftPM manifests — versions live in properties or
        BOMs, not on the declaring line; guessing would be wrong often.
      * pyproject.toml / Cargo.toml range floors — package.json is parsed as
        a floor because npm PRs without a lockfile are common; the TOML pair
        would double the grammar for ecosystems whose lockfiles we already
        read.
      * an entry whose name line sits outside the hunk's context lines —
        we skip rather than guess the name.
    Every miss is still covered by the post-merge audit; this agent narrows
    the pre-merge gap, it does not replace the audit.

Failure honesty:
    Zero-findings-because-blind is the silent-APPROVE bug this project already
    fixed once for unreadable LLM replies (see AgentReplyUnreadable). So: no
    binary AND no reachable OSV API → the agent FAILS into `agents_failed`
    and the partial banner names it. A PR that touches no dependency files
    reports zero findings WITHOUT running any scanner — that is a clean
    answer, not a gap.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.models import Finding, FindingSeverity, Hunk, HunkSide, PullRequest

logger = logging.getLogger(__name__)

#: Hard wall-clock ceiling for the whole lookup (binary or HTTP), so review
#: latency stays bounded. 120s because: the binary's scan of a one-file SBOM
#: is a handful of OSV queries (seconds; its ceiling below leaves room for a
#: slow day), and the HTTP path is one querybatch POST per 100 packages plus
#: detail GETs, each under the 30s httpx timeout registries.py already uses.
#: The LLM agents in the same thread pool routinely take this long themselves,
#: so the ceiling adds nothing to total review time — it only stops a hung
#: scan from holding the review open. On expiry the agent FAILS honestly; the
#: abandoned worker thread winds down on its own subprocess/httpx timeouts.
def _lookup_timeout_default() -> float:
    """The sweep's wall clock, from the settings rather than from this line.

    A monorepo whose lockfiles run to thousands of packages sweeps more
    batches than a small repository, so 120 seconds is a judgement about
    lockfile size that only one installation can make — and as a module
    constant it was one nobody could make."""
    from src.review.settings import get_review_settings
    return float(get_review_settings().cve_lookup_timeout_seconds)


#: The default an unconfigured install gets. Kept as a name because tests and
#: the docstrings above refer to it; `_lookup_timeout_default()` is what the
#: agent actually reads, so an operator's value wins over this.
LOOKUP_TIMEOUT_SECONDS = 120.0

#: The subprocess ceiling handed to osv-scanner, deliberately under the wall
#: clock above so the kill comes from `run_tool` (which reports TIMEOUT as a
#: reason code) rather than from the outer deadline abandoning the thread.
#: No HTTP fallback after a binary timeout: burning the budget twice is how a
#: timebox stops being one.
BINARY_TIMEOUT_SECONDS = 100

#: Room between the HTTP sweep's own deadline and the wall clock that would
#: abandon it. One in-flight request (30s at `registries._TIMEOUT`) plus the
#: shaping of what came back, so a sweep that stops cleanly still gets its
#: findings home before the outer timebox gives up on the thread.
_DEADLINE_MARGIN = 35.0

#: Findings per package, worst severity first. An ancient package can carry
#: a dozen advisories; the first few are the ones that matter and the rest
#: would only push other agents' findings past `max_inline_comments`.
MAX_FINDINGS_PER_PACKAGE = 5

#: OSV severity words → review severity, graded once here so the binary path
#: and the HTTP path (both already normalised by src.deps.severity /
#: registries.osv_severity) cannot disagree. "high" is ERROR because a known
#: exploitable dependency is a blocker; an advisory whose severity OSV does
#: not state maps to WARNING, not INFO — claiming a vulnerability is harmless
#: is worse than admitting we do not know (the same call sbom.py makes).
_SEVERITY_MAP = {
    "critical": FindingSeverity.CRITICAL,
    "high": FindingSeverity.ERROR,
    "medium": FindingSeverity.WARNING,
    "moderate": FindingSeverity.WARNING,
    "low": FindingSeverity.INFO,
    "none": FindingSeverity.INFO,
}


class CveLookupUnavailable(RuntimeError):
    """Neither the binary nor the OSV API could answer — the agent is blind
    and must say so instead of reporting a clean zero."""


# ─── Extraction: diff → (ecosystem, package, exact version) ─────────


@dataclass(frozen=True)
class PackageChange:
    """One package the PR introduces or re-pins, with its diff anchor."""

    ecosystem: str
    package: str
    version: str
    file_path: str
    line: int                 # new-side line number of the introducing line
    line_text: str = ""       # that line's text — for the suggestion block
    is_floor: bool = False    # version is a range's floor, not a pin


@dataclass(frozen=True)
class _NewLine:
    line: int
    text: str
    added: bool


def _sides(hunk: Hunk) -> tuple[list[_NewLine], list[str]]:
    """A hunk's new side (with line numbers and added-flags) and old side."""
    new_side: list[_NewLine] = []
    old_side: list[str] = []
    line = (hunk.new_start or 1) - 1
    for raw in (hunk.content or "").splitlines():
        if raw.startswith("@@"):
            continue
        if raw.startswith("-"):
            old_side.append(raw[1:])
            continue
        if raw.startswith("+"):
            line += 1
            new_side.append(_NewLine(line, raw[1:], True))
            continue
        text = raw[1:] if raw.startswith(" ") else raw
        line += 1
        new_side.append(_NewLine(line, text, False))
        old_side.append(text)
    return new_side, old_side


# Each parser reads one hunk's worth of lines and yields
# (package, exact-version, index-of-the-version-carrying-line). The caller
# decides which entries count as the PR's doing (the yielded line was a `+`
# line) and which merely restate the old state.
_Entry = tuple[str, str, int]

_REQ_RE = re.compile(
    r"\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:\[[^\]]*\])?\s*==\s*([0-9][A-Za-z0-9.!+\-]*)")


def _parse_requirements(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    for i, text in enumerate(lines):
        if text.lstrip().startswith("#"):
            continue
        m = _REQ_RE.match(text)
        if m:
            out.append((m.group(1).lower(), m.group(2), i))
    return out


_GO_MOD_RE = re.compile(r"(?:require\s+)?([\w.~\-]+(?:/[\w.~\-]+)+)\s+v([0-9][\w.\-+]*)")


def _parse_go_mod(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    for i, text in enumerate(lines):
        s = text.strip()
        if not s or s.startswith(("module", "go ", "toolchain", "replace",
                                  "exclude", "//", ")")):
            continue
        m = _GO_MOD_RE.match(s)
        if m:
            out.append((m.group(1), m.group(2), i))
    return out


_GO_SUM_RE = re.compile(
    r"([\w.~\-]+(?:/[\w.~\-]+)+)\s+v([0-9][\w.\-+]*?)(?:/go\.mod)?\s+h1:")


def _parse_go_sum(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    for i, text in enumerate(lines):
        m = _GO_SUM_RE.match(text)
        if m:
            out.append((m.group(1), m.group(2), i))
    return out


_GEM_RE = re.compile(r"\s{4}([A-Za-z0-9_\-.]+)\s+\(([0-9][\w.\-]*)\)\s*$")


def _parse_gemfile_lock(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    for i, text in enumerate(lines):
        m = _GEM_RE.match(text)
        if m:
            out.append((m.group(1), m.group(2), i))
    return out


_YARN_HEADER_RE = re.compile(r'"?(@?[^@"]+)@')
_YARN_VERSION_RE = re.compile(r'\s+version\s+"([^"]+)"')


def _parse_yarn_lock(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    name: str | None = None
    for i, text in enumerate(lines):
        if text and not text[0].isspace() and "@" in text and text.rstrip().endswith(":"):
            m = _YARN_HEADER_RE.match(text)
            name = m.group(1) if m else None
            continue
        m = _YARN_VERSION_RE.match(text)
        if m and name:
            out.append((name, m.group(1), i))
    return out


_NPM_LOCK_NAME_RE = re.compile(r'\s*"(?:.*node_modules/)((?:@[^"/]+/)?[^"/]+)":\s*\{')
_JSON_VERSION_RE = re.compile(r'\s*"version":\s*"v?([0-9][^"]*)"')


def _parse_npm_lock(lines: list[str]) -> list[_Entry]:
    """package-lock.json v2/v3 — the `"node_modules/x"` entries. v1 lockfiles
    (npm ≤6, pre-2020) have no such prefix and are skipped: their `"x": {`
    keys are indistinguishable from arbitrary nested objects in a diff
    fragment, and a guessed name is worse than a named miss."""
    out: list[_Entry] = []
    name: str | None = None
    for i, text in enumerate(lines):
        m = _NPM_LOCK_NAME_RE.match(text)
        if m:
            name = m.group(1)
            continue
        m = _JSON_VERSION_RE.match(text)
        if m and name:
            out.append((name, m.group(1), i))
            name = None  # one version per entry; do not pair leftovers
    return out


_PNPM_KEY_RE = re.compile(
    r"\s+'?/?((?:@[\w.\-]+/)?[\w.\-]+)[@/]v?([0-9][\w.\-+]*)")


def _parse_pnpm_lock(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    for i, text in enumerate(lines):
        if not text.rstrip().endswith(":"):
            continue
        m = _PNPM_KEY_RE.match(text)
        if m:
            out.append((m.group(1), m.group(2), i))
    return out


_TOML_NAME_RE = re.compile(r'\s*name\s*=\s*"([^"]+)"')
_TOML_VERSION_RE = re.compile(r'\s*version\s*=\s*"([0-9][^"]*)"')


def _parse_toml_pairs(lines: list[str]) -> list[_Entry]:
    """Cargo.lock / poetry.lock / uv.lock — `name = "x"` then `version = "y"`
    inside a [[package]] block. The name line is adjacent to the version line
    in all three formats, so a version-bump hunk's context carries it."""
    out: list[_Entry] = []
    name: str | None = None
    for i, text in enumerate(lines):
        m = _TOML_NAME_RE.match(text)
        if m:
            name = m.group(1)
            continue
        m = _TOML_VERSION_RE.match(text)
        if m and name:
            out.append((name, m.group(1), i))
            name = None
    return out


_COMPOSER_NAME_RE = re.compile(r'\s*"name":\s*"([\w.\-]+/[\w.\-]+)"')


def _parse_composer_lock(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    name: str | None = None
    for i, text in enumerate(lines):
        m = _COMPOSER_NAME_RE.match(text)
        if m:
            name = m.group(1)
            continue
        m = _JSON_VERSION_RE.match(text)
        if m and name:
            out.append((name, m.group(1), i))
            name = None
    return out


_PIPFILE_NAME_RE = re.compile(r'\s*"([A-Za-z0-9._\-]+)":\s*\{')
_PIPFILE_VERSION_RE = re.compile(r'\s*"version":\s*"==([^"]+)"')


def _parse_pipfile_lock(lines: list[str]) -> list[_Entry]:
    out: list[_Entry] = []
    name: str | None = None
    for i, text in enumerate(lines):
        m = _PIPFILE_NAME_RE.match(text)
        if m:
            name = m.group(1).lower()
            continue
        m = _PIPFILE_VERSION_RE.match(text)
        if m and name:
            out.append((name, m.group(1), i))
            name = None
    return out


_NPM_SECTION_RE = re.compile(
    r'\s*"(dependencies|devDependencies|optionalDependencies|peerDependencies)"\s*:\s*\{')
_NPM_PAIR_RE = re.compile(r'\s*"((?:@[^"/]+/)?[^"/]+)":\s*"([^"]+)"')


def _parse_package_json(lines: list[str]) -> list[_Entry]:
    """package.json — a RANGE, so what comes out is the range's floor
    (`^4.17.15` → 4.17.15) via the same normaliser the manifest scanner uses.
    The caller marks these `is_floor` and skips this parser entirely when an
    npm lockfile changed in the same PR — the lockfile's pins are the truth
    and a floor beside them would double-report at a possibly-wrong version."""
    from src.deps.scanner import _norm_version
    out: list[_Entry] = []
    in_section = False
    section_indent = 0
    for i, text in enumerate(lines):
        m = _NPM_SECTION_RE.match(text)
        if m:
            in_section = True
            section_indent = len(text) - len(text.lstrip())
            continue
        if in_section and text.strip().startswith("}") \
                and (len(text) - len(text.lstrip())) <= section_indent:
            in_section = False
            continue
        if not in_section:
            continue
        m = _NPM_PAIR_RE.match(text)
        if m:
            floor = _norm_version(m.group(2))
            if floor:
                out.append((m.group(1), floor, i))
    return out


#: filename (or requirements*.txt) → (OSV ecosystem, parser, is_floor).
#: Ecosystem names match OSV exactly — the same vocabulary src/deps uses.
_PARSERS: dict[str, tuple[str, object, bool]] = {
    "package.json": ("npm", _parse_package_json, True),
    "package-lock.json": ("npm", _parse_npm_lock, False),
    "npm-shrinkwrap.json": ("npm", _parse_npm_lock, False),
    "yarn.lock": ("npm", _parse_yarn_lock, False),
    "pnpm-lock.yaml": ("npm", _parse_pnpm_lock, False),
    "go.mod": ("Go", _parse_go_mod, False),
    "go.sum": ("Go", _parse_go_sum, False),
    "Cargo.lock": ("crates.io", _parse_toml_pairs, False),
    "poetry.lock": ("PyPI", _parse_toml_pairs, False),
    "uv.lock": ("PyPI", _parse_toml_pairs, False),
    "Pipfile.lock": ("PyPI", _parse_pipfile_lock, False),
    "Gemfile.lock": ("RubyGems", _parse_gemfile_lock, False),
    "composer.lock": ("Packagist", _parse_composer_lock, False),
}

_NPM_LOCKFILES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"})

#: Ecosystems whose registries are case-insensitive and whose auditors
#: lowercase names (pip-audit, parse_osv_scanner) — matched here so one
#: package under two spellings stays one package.
_LOWERCASE_ECOSYSTEMS = frozenset({"PyPI"})


def _parser_for(path: str):
    name = PurePosixPath(path).name
    entry = _PARSERS.get(name)
    if entry:
        return entry
    if name.startswith("requirements") and name.endswith(".txt"):
        return ("PyPI", _parse_requirements, False)
    return None


def _collect_dependency_hunks(pr: PullRequest) -> dict[str, list[Hunk]]:
    """Changed dependency files → their hunks.

    Two sources, because the diff parser splits them: manifests survive into
    `pr.hunks`; lockfiles match `skip_filename_patterns` and land in
    `pr.skipped_files` as bare paths — their hunks exist only in `pr.raw_diff`,
    which every provider keeps as the unfiltered original. Re-parsing it here
    (without the skip filter) is what lets a filtered lockfile still be seen.
    """
    out: dict[str, list[Hunk]] = {}
    for h in pr.hunks:
        if _parser_for(h.file_path) is not None:
            out.setdefault(h.file_path, []).append(h)

    if not pr.raw_diff or not pr.raw_diff.strip():
        return out
    try:
        from unidiff import PatchSet
        patch_set = PatchSet(pr.raw_diff)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cve_raw_diff_unparseable err=%s", exc)
        return out
    for pf in patch_set:
        new_path = _strip_prefix(pf.target_file or "")
        if new_path == "/dev/null":
            continue  # a deleted dependency file only removes packages
        if new_path in out or _parser_for(new_path) is None:
            continue
        if pf.is_binary_file:
            continue
        for hunk in pf:
            out.setdefault(new_path, []).append(Hunk(
                file_path=new_path,
                old_file_path=_strip_prefix(pf.source_file or ""),
                old_start=hunk.source_start,
                old_count=hunk.source_length,
                new_start=hunk.target_start,
                new_count=hunk.target_length,
                content=str(hunk),
            ))
    return out


def _strip_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _extract_changes(
    dep_hunks: dict[str, list[Hunk]],
) -> tuple[list[PackageChange], set[tuple[str, str, str]]]:
    """(packages the PR introduces, packages that were already there).

    An entry counts as the PR's doing only when its version-carrying line is a
    `+` line; the before-set is parsed from the hunks' old sides so a moved or
    reformatted entry — same package, same version, new line — cancels out
    instead of raising an alarm about a dependency the PR did not touch.

    The two filters overlap on purpose: a context line lands on both sides of
    the hunk, so the before-set alone already cancels every well-formed
    context entry. The added-check is the backstop for the pair-tracking
    parsers, whose old-side name/version pairing can miss (a name line edited
    out from between two context lines) — without it, such a miss would turn
    an untouched dependency into a proven-evidence finding, which is the one
    kind of false positive this agent must never produce.
    """
    has_npm_lock = any(
        PurePosixPath(p).name in _NPM_LOCKFILES for p in dep_hunks)
    added: list[PackageChange] = []
    before: set[tuple[str, str, str]] = set()
    for path, hunks in dep_hunks.items():
        eco, parser, is_floor = _parser_for(path)
        if is_floor and has_npm_lock:
            continue  # the lockfile's exact pins supersede the manifest range
        for h in hunks:
            new_side, old_lines = _sides(h)
            for name, version, idx in parser([nl.text for nl in new_side]):
                nl = new_side[idx]
                if not nl.added:
                    continue
                added.append(PackageChange(
                    ecosystem=eco,
                    package=_norm_name(eco, name),
                    version=version,
                    file_path=path,
                    line=nl.line,
                    line_text=nl.text,
                    is_floor=is_floor,
                ))
            for name, version, _idx in parser(old_lines):
                before.add((eco, _norm_name(eco, name), version))
    return added, before


def _norm_name(ecosystem: str, name: str) -> str:
    return name.lower() if ecosystem in _LOWERCASE_ECOSYSTEMS else name


# ─── Lookup: (ecosystem, package, version) → advisories ─────────────


@dataclass(frozen=True)
class _Advisory:
    ecosystem: str
    package: str
    version: str
    vuln_id: str
    cve: str | None
    severity_word: str
    fixed_in: str | None
    summary: str
    url: str


def _default_osv_query(deps: list[tuple[str, str, str]],
                       *, deadline: float | None = None):
    """The OSV HTTP fallback — the guarded client and the batch query (with
    its length-mismatch protection) the deps module already ships.

    `deadline` is a `time.monotonic()` instant, and it is what makes the
    agent's timebox reachable: one POST per hundred packages plus a GET per
    advisory, each bounded at 30s and nothing bounding the sequence, could not
    finish inside 120 seconds for any large lockfile however healthy OSV was.
    The sweep now stops at the deadline and reports the packages it did not
    reach as failed batches — partial, which is a different and far more
    useful answer than blind.
    """
    from src.deps.registries import fetch_vulns_batch, make_client
    with make_client() as client:
        return fetch_vulns_batch(client, deps, deadline=deadline)


# ─── Agent ──────────────────────────────────────────────────────────


class CveAgent(ReviewAgent):
    """Known-vulnerability check for the PR's dependency changes.

    Deterministic, no tokens. `runner` and `osv_query` are injectable the way
    `audit_repo(runner=…)` already is in src/deps — tests run without the
    binary and without the network.
    """

    name = "cve"
    severity_default = FindingSeverity.ERROR

    def __init__(
        self,
        *,
        runner=None,
        osv_query=None,
        lookup_timeout: float | None = None,
        binary_timeout: int = BINARY_TIMEOUT_SECONDS,
    ) -> None:
        if runner is None:
            from src.deps.native import run_tool
            runner = run_tool
        self._runner = runner
        self._osv_query = osv_query or _default_osv_query
        # None → the setting, read per instance so an operator's change
        # takes effect. An explicit value is a test or a caller that has
        # already decided, and still wins.
        self._lookup_timeout = (
            _lookup_timeout_default() if lookup_timeout is None else lookup_timeout
        )
        self._binary_timeout = binary_timeout

    def review(self, context: AgentContext) -> AgentRunResult:
        t0 = time.time()
        pr = context.pull_request

        dep_hunks = _collect_dependency_hunks(pr)
        if not dep_hunks:
            # No dependency file changed — a clean answer, not a gap. The
            # scanner is deliberately never invoked on this path.
            return AgentRunResult(
                agent=self.name, findings=[], elapsed_seconds=time.time() - t0)

        added, before = _extract_changes(dep_hunks)
        changes = [c for c in added
                   if (c.ecosystem, c.package, c.version) not in before]
        # Dedupe (a package can appear in go.mod AND go.sum) — prefer the
        # anchor in a file the review actually shows (`pr.hunks`), because an
        # inline comment there is read where a lockfile comment is folded away.
        shown = {h.file_path for h in pr.hunks}
        changes.sort(key=lambda c: c.file_path not in shown)
        by_key: dict[tuple[str, str, str], PackageChange] = {}
        for c in changes:
            by_key.setdefault((c.ecosystem, c.package, c.version), c)
        if not by_key:
            return AgentRunResult(
                agent=self.name, findings=[], elapsed_seconds=time.time() - t0)

        # The hard timebox. One mechanism for both lookup paths: the worker
        # thread is abandoned on expiry (it winds down on its own subprocess/
        # httpx timeouts) and the agent fails HONESTLY — a timeout is the
        # agent being blind, and blind must never read as clean.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cve-lookup")
        try:
            future = pool.submit(self._lookup, pr, list(by_key.values()))
            advisories = future.result(timeout=self._lookup_timeout)
        except FutureTimeout:
            return AgentRunResult(
                agent=self.name,
                error=(f"CVE lookup exceeded {self._lookup_timeout:.0f}s — "
                       f"{len(by_key)} changed package(s) went unchecked"),
                elapsed_seconds=time.time() - t0,
            )
        except CveLookupUnavailable as exc:
            return AgentRunResult(
                agent=self.name, error=str(exc),
                elapsed_seconds=time.time() - t0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cve_lookup_failed err=%s", exc)
            return AgentRunResult(
                agent=self.name, error=f"CVE lookup failed: {exc}",
                elapsed_seconds=time.time() - t0,
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        findings = self._to_findings(by_key, advisories, dep_hunks, shown)
        logger.info(
            "cve_done packages=%d advisories=%d findings=%d elapsed=%.2fs",
            len(by_key), len(advisories), len(findings), time.time() - t0,
        )
        return AgentRunResult(
            agent=self.name, findings=findings,
            elapsed_seconds=time.time() - t0,
        )

    # ── Lookup paths ──

    def _lookup(
        self, pr: PullRequest, changes: list[PackageChange],
    ) -> list[_Advisory]:
        native = self._scan_with_binary(pr, changes)
        if native is not None:
            return native
        return self._query_api(changes)

    def _scan_with_binary(
        self, pr: PullRequest, changes: list[PackageChange],
    ) -> list[_Advisory] | None:
        """osv-scanner over a generated CycloneDX SBOM of the changed packages.

        The binary reads purls natively, so one SBOM covers every ecosystem
        without a synthetic lockfile per format, and `audit_repo` brings the
        v1 fallback, the exit-code semantics and the reason codes with it.
        Returns None when the binary is not the answer (absent, broken, or an
        old build that does not read SBOMs) — the HTTP path takes over.
        Raises on timeout: the timebox is spent, and burning it twice is how
        a timebox stops being one.
        """
        from src.deps.osv_scanner import REASON_TIMEOUT, audit_repo
        from src.deps.sbom import build_sbom, to_json
        from src.deps.scanner import DeclaredDep

        deps = [DeclaredDep(c.ecosystem, c.package, c.version, c.version,
                            False, "bom.json")
                for c in changes]
        with tempfile.TemporaryDirectory(prefix="cve-agent-") as tmp:
            doc = build_sbom(repo_slug=pr.repo_slug, deps=deps,
                             commit=pr.head_sha)
            (Path(tmp) / "bom.json").write_text(to_json(doc), encoding="utf-8")
            result = audit_repo(Path(tmp), timeout=self._binary_timeout,
                                runner=self._runner)

        if any(c.status == "checked" for c in result.checks):
            return [_Advisory(
                ecosystem=f.ecosystem, package=f.package, version=f.version,
                vuln_id=f.vuln_id, cve=f.cve, severity_word=f.severity,
                fixed_in=f.fixed_in, summary=f.summary, url=f.url,
            ) for f in result.findings]

        code = result.checks[0].reason_code if result.checks else ""
        if code == REASON_TIMEOUT:
            raise CveLookupUnavailable(
                f"osv-scanner timed out after {self._binary_timeout}s — "
                "changed packages went unchecked")
        logger.info("cve_binary_unavailable code=%s — falling back to OSV API",
                    code)
        return None

    def _query_api(self, changes: list[PackageChange]) -> list[_Advisory]:
        triples = [(c.ecosystem, c.package, c.version) for c in changes]
        # Leave the outer timebox room to notice: the abandon-the-thread path
        # is the LAST resort, for a lookup wedged inside a single request, and
        # a sweep that stops on its own returns the advisories it did find.
        deadline = time.monotonic() + max(1.0, self._lookup_timeout - _DEADLINE_MARGIN)
        try:
            vulns_by_key, failed_batches = self._osv_query(triples, deadline=deadline)
        except TypeError:
            # An injected query from before the deadline existed. Tests pass
            # two-argument stubs, and a scan without the bound is what this
            # path always did.
            vulns_by_key, failed_batches = self._osv_query(triples)
        if failed_batches:
            # Partial blindness is still blindness: whatever the failed
            # batches held went unchecked, and a zero built on that would be
            # the silent APPROVE this agent exists to prevent.
            raise CveLookupUnavailable(
                f"OSV API unreachable ({failed_batches} query batch(es) "
                "failed) and no osv-scanner binary — CVE signal unavailable")
        out: list[_Advisory] = []
        for (eco, pkg, ver), vulns in vulns_by_key.items():
            for v in vulns:
                out.append(_Advisory(
                    ecosystem=eco, package=pkg, version=ver,
                    vuln_id=str(v.get("id") or ""),
                    cve=v.get("cve"),
                    severity_word=str(v.get("severity") or ""),
                    fixed_in=v.get("fixed_in"),
                    summary=str(v.get("summary") or ""),
                    url=str(v.get("url") or ""),
                ))
        return out

    # ── Findings ──

    def _to_findings(
        self,
        by_key: dict[tuple[str, str, str], PackageChange],
        advisories: list[_Advisory],
        dep_hunks: dict[str, list[Hunk]],
        shown: set[str],
    ) -> list[Finding]:
        per_package: dict[tuple[str, str, str], int] = {}
        rank = {FindingSeverity.CRITICAL: 0, FindingSeverity.ERROR: 1,
                FindingSeverity.WARNING: 2, FindingSeverity.INFO: 3}
        ordered = sorted(
            advisories,
            key=lambda a: rank.get(
                _SEVERITY_MAP.get(a.severity_word.lower(), FindingSeverity.WARNING), 9))
        findings: list[Finding] = []
        for adv in ordered:
            key = (adv.ecosystem, _norm_name(adv.ecosystem, adv.package),
                   adv.version)
            change = by_key.get(key)
            if change is None:
                # The scanner answered about something we did not ask about
                # (an alias or a re-canonicalised name) — keep it, anchored to
                # the first dependency file, rather than drop a real advisory.
                change = next(iter(by_key.values()))
                change = PackageChange(
                    ecosystem=adv.ecosystem, package=adv.package,
                    version=adv.version, file_path=change.file_path, line=1)
            count = per_package.get(key, 0)
            if count >= MAX_FINDINGS_PER_PACKAGE:
                continue
            per_package[key] = count + 1
            findings.append(self._finding(adv, change, dep_hunks, shown))
        return findings

    def _finding(
        self,
        adv: _Advisory,
        change: PackageChange,
        dep_hunks: dict[str, list[Hunk]],
        shown: set[str],
    ) -> Finding:
        file_path, line, line_text = change.file_path, change.line, change.line_text
        if file_path not in shown:
            # The introducing line sits in a filtered lockfile. A manifest in
            # the reviewed diff that names the same package is where a human
            # will actually read the comment — anchor there when one exists.
            anchor = self._manifest_anchor(change.package, dep_hunks, shown)
            if anchor is not None:
                file_path, line, line_text = anchor
            # Otherwise keep the lockfile anchor: the file IS part of the PR
            # on the provider's side even though review filtered it locally,
            # and when the provider rejects the line the posting layer already
            # folds rejected inline comments into the summary comment — an
            # unanchorable finding still appears (providers/base, github 422
            # handling).

        ident = adv.vuln_id or adv.cve or "advisory"
        severity = _SEVERITY_MAP.get(
            adv.severity_word.lower(), FindingSeverity.WARNING)
        version_note = (
            f"declared floor {adv.version}" if change.is_floor
            else f"version {adv.version}")
        body_parts = [
            f"`{adv.package}` ({adv.ecosystem}, {version_note}) is affected by "
            f"**{ident}**" + (f" / {adv.cve}" if adv.cve and adv.cve != ident else "")
            + ".",
        ]
        if adv.summary:
            body_parts.append(adv.summary.strip().rstrip(".") + ".")
        body_parts.append(
            f"Fixed in **{adv.fixed_in}**." if adv.fixed_in
            else "OSV lists no fixed version yet.")
        if change.is_floor:
            body_parts.append(
                "The version is the declared range's floor — the installed "
                "resolution may already be newer; check the lockfile.")
        if adv.url:
            body_parts.append(adv.url)

        suggestion = None
        if (adv.fixed_in and line_text and adv.version in line_text
                and not change.is_floor):
            suggestion = line_text.replace(adv.version, adv.fixed_in)

        return Finding(
            file_path=file_path,
            line=line,
            side=HunkSide.RIGHT,
            severity=severity,
            title=f"{adv.package} {adv.version} carries known vulnerability {ident}",
            body="\n\n".join(body_parts),
            suggestion=suggestion,
            agent=self.name,
            rule_id=f"sec.cve-{ident}",
            confidence=1.0,
            # A purl and a version matched an OSV record. Like a structural
            # match, the reader can check it in seconds at the advisory URL —
            # which is what keeps it out of the inferred findings' reputation.
            evidence_kind="proven",
        )

    @staticmethod
    def _manifest_anchor(
        package: str,
        dep_hunks: dict[str, list[Hunk]],
        shown: set[str],
    ) -> tuple[str, int, str] | None:
        pattern = re.compile(rf"""["']?{re.escape(package)}["']?\s*[:=@ ]""")
        for path, hunks in dep_hunks.items():
            if path not in shown:
                continue
            for h in hunks:
                for nl in _sides(h)[0]:
                    if nl.added and pattern.search(nl.text):
                        return path, nl.line, nl.text
        return None
