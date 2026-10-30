"""Does this advisory apply to THIS version, and what fixes it from here?

Three audit bugs shared one root: a fix version or an advisory was carried
around without the version it was reported for. A multi-branch advisory
(`fixed` 2.3.2, 2.12.2, 2.15.0) printed the LOWEST fix to someone already on
2.16 — a "downgrade" shown as the remedy — and an advisory reported for an old
copy of a package got painted on a row showing a newer one.

Everything here is pure and ecosystem-aware:

* PyPI    — PEP 440 through `packaging.version`;
* npm / Go / crates.io — SemVer 2.0 precedence (pre-releases sort below the
  release, numeric identifiers compare numerically);
* anything else — a lenient numeric comparison, which is what the rest of the
  audit already used.

Three-valued on purpose. `affected` / `not_affected` are only returned when the
record carries enough range data to decide; otherwise `unknown`, and the caller
KEEPS the advisory. An advisory dropped on a guess is the silent zero this
audit exists to refuse.
"""

from __future__ import annotations

import re
from functools import cmp_to_key
from typing import Any

AFFECTED = "affected"
NOT_AFFECTED = "not_affected"
UNKNOWN = "unknown"

_SEMVER_ECOSYSTEMS = {"npm", "go", "crates.io"}


# ─── Comparison ──────────────────────────────────────────────────────


def _base(ecosystem: str) -> str:
    return str(ecosystem or "").split(":", 1)[0].strip().lower()


def _semver_parts(v: str) -> tuple[list[int], list[Any]]:
    v = str(v).strip().lstrip("vV=").split("+", 1)[0]
    core, _, pre = v.partition("-")
    nums: list[int] = []
    for chunk in core.split("."):
        m = re.match(r"\d+", chunk)
        nums.append(int(m.group(0)) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    idents: list[Any] = []
    for ident in (pre.split(".") if pre else []):
        idents.append((0, int(ident), "") if ident.isdigit() else (1, 0, ident))
    return nums, idents


def _cmp_semver(a: str, b: str) -> int:
    na, pa = _semver_parts(a)
    nb, pb = _semver_parts(b)
    if na != nb:
        return -1 if na < nb else 1
    if not pa and not pb:
        return 0
    if not pa:
        return 1            # a release outranks its own pre-release
    if not pb:
        return -1
    if pa == pb:
        return 0
    return -1 if pa < pb else 1


def _cmp_pep440(a: str, b: str) -> int:
    from packaging.version import InvalidVersion, Version

    try:
        va, vb = Version(str(a)), Version(str(b))
    except InvalidVersion:
        return _cmp_loose(a, b)
    return (va > vb) - (va < vb)


def _loose_key(v: str) -> tuple:
    out: list[tuple[int, Any]] = []
    for chunk in re.split(r"[.\-+_]", str(v).strip().lstrip("vV")):
        out.append((0, int(chunk)) if chunk.isdigit() else (-1, chunk))
    while out and out[-1] == (0, 0):
        out.pop()
    return tuple(out)


def _cmp_loose(a: str, b: str) -> int:
    ka, kb = _loose_key(a), _loose_key(b)
    try:
        return (ka > kb) - (ka < kb)
    except TypeError:
        return (str(a) > str(b)) - (str(a) < str(b))


def compare_versions(ecosystem: str, a: str, b: str) -> int:
    """-1 / 0 / 1 with the ordering the ecosystem itself uses."""
    eco = _base(ecosystem)
    if eco == "pypi":
        return _cmp_pep440(a, b)
    if eco in _SEMVER_ECOSYSTEMS:
        return _cmp_semver(a, b)
    return _cmp_loose(a, b)


def sort_versions(ecosystem: str, versions: list[str]) -> list[str]:
    return sorted(versions, key=cmp_to_key(lambda a, b: compare_versions(ecosystem, a, b)))


# ─── OSV records ─────────────────────────────────────────────────────


def _intervals(events: list[dict], ecosystem: str) -> list[tuple[str | None, str | None, bool]]:
    """(introduced, end, end_inclusive) per interval; `None` start = from the
    beginning, `None` end = open-ended (no fix known). A `limit` event caps
    every interval that would run past it (exclusive)."""
    evs = []
    for ev in events:
        for kind in ("introduced", "fixed", "last_affected", "limit"):
            if kind in ev:
                evs.append((kind, str(ev[kind])))

    ordered = sorted(
        evs,
        key=cmp_to_key(lambda x, y: (
            0 if x[1] == y[1] else
            (-1 if x[1] == "0" else 1 if y[1] == "0" else
             compare_versions(ecosystem, x[1], y[1])))),
    )
    limit = next((v for k, v in ordered if k == "limit"), None)
    out: list[tuple[str | None, str | None, bool]] = []
    start: str | None = None
    open_ = False
    for kind, val in ordered:
        if kind == "introduced":
            start, open_ = (None if val == "0" else val), True
        elif kind in ("fixed", "last_affected") and open_:
            out.append((start, val, kind == "last_affected"))
            open_ = False
        # Several events at the same version: introduced sorts first in the
        # input order of a stable sort, which is the OSV convention.
    if open_:
        out.append((start, None, False))
    if limit is not None:
        capped = []
        for s, e, inc in out:
            if e is None or compare_versions(ecosystem, e, limit) > 0:
                e, inc = limit, False
            capped.append((s, e, inc))
        out = capped
    return out


def _in_interval(ecosystem: str, version: str, iv) -> bool:
    start, end, inclusive = iv
    if start is not None and compare_versions(ecosystem, version, start) < 0:
        return False
    if end is None:
        return True
    c = compare_versions(ecosystem, version, end)
    return c <= 0 if inclusive else c < 0


def _matching_entries(detail: dict, raw_ecosystem: str, package: str):
    base = _base(raw_ecosystem)
    name = str(package or "").lower()
    for aff in detail.get("affected") or []:
        if not isinstance(aff, dict):
            continue
        pkg = aff.get("package") or {}
        if str(pkg.get("name") or "").lower() != name:
            continue
        if base and _base(pkg.get("ecosystem")) != base:
            continue
        yield aff


def evaluate_osv(detail: dict, raw_ecosystem: str, package: str,
                 version: str) -> tuple[str, str | None]:
    """(state, fixed_in) of one OSV record against one installed version.

    `fixed_in` is the fix of the affected range that CONTAINS `version` — never
    the lowest fix overall and never one for a branch the version is not on.
    When the version is inside a range with no fix (or only `last_affected`)
    there is nothing to jump to and `fixed_in` is None. Without a `version`
    the answer is `unknown` and `fixed_in` is the lowest fix, as before.
    """
    eco = raw_ecosystem
    entries = list(_matching_entries(detail, raw_ecosystem, package))
    if not entries:
        return UNKNOWN, None

    if not version:
        fixes = [str(ev["fixed"]) for a in entries for r in a.get("ranges") or []
                 for ev in r.get("events") or [] if "fixed" in ev]
        return UNKNOWN, (sort_versions(eco, fixes)[0] if fixes else None)

    containing_fixes: list[str] = []
    any_hit = False
    undecided = False
    for aff in entries:
        listed = [str(v) for v in aff.get("versions") or []]
        usable = [r for r in aff.get("ranges") or []
                  if str(r.get("type") or "").upper() in ("SEMVER", "ECOSYSTEM")]
        entry_hit = version in listed or any(
            compare_versions(eco, version, v) == 0 for v in listed)
        if entry_hit:
            any_hit = True
        for rng in usable:
            for iv in _intervals(rng.get("events") or [], eco):
                if _in_interval(eco, version, iv):
                    any_hit = True
                    if iv[1] is not None and not iv[2]:
                        containing_fixes.append(iv[1])
        if not usable and not listed:
            undecided = True        # GIT-only / empty: cannot say
    if any_hit:
        fixed = sort_versions(eco, containing_fixes)[0] if containing_fixes else None
        return AFFECTED, fixed
    if undecided:
        return UNKNOWN, None
    return NOT_AFFECTED, None


# ─── Other sources ───────────────────────────────────────────────────


def fixes_say_fixed(ecosystem: str, version: str, fix_versions: list[str]) -> bool:
    """True only when `version` is at or above EVERY listed fix. Conservative
    for multi-branch advisories: on 2.13 with fixes (2.12.2, 2.15.0) it says
    False, because 2.13 is not on a fixed branch."""
    fixes = [f for f in fix_versions if f]
    if not version or not fixes:
        return False
    top = sort_versions(ecosystem, fixes)[-1]
    return compare_versions(ecosystem, version, top) >= 0


def lowest_fix_above(ecosystem: str, version: str, fix_versions: list[str]) -> str | None:
    """The upgrade target from a bare list of fix versions: the lowest one
    above the installed version. None if nothing is above it — never a
    downgrade."""
    fixes = sort_versions(ecosystem, [f for f in fix_versions if f])
    if not version:
        return fixes[0] if fixes else None
    for f in fixes:
        if compare_versions(ecosystem, f, version) > 0:
            return f
    return None


_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?\s*v?(\d[\w.\-+]*)$")


def semver_range_contains(range_expr: str, version: str, ecosystem: str = "npm") -> bool | None:
    """Whether `version` satisfies an npm-style VULNERABLE range such as
    ">=1.0.0 <1.2.3 || >=2.0.0 <2.1.0". None when the expression is something
    this does not parse (caret, tilde, x-ranges): the caller keeps the
    advisory."""
    expr = str(range_expr or "").strip()
    if not expr or not version:
        return None
    if expr in ("*", "x", "latest"):
        return True
    for alt in expr.split("||"):
        tokens = alt.replace(">= ", ">=").replace("<= ", "<=").replace("> ", ">") \
            .replace("< ", "<").replace("= ", "=").split()
        ok = bool(tokens)
        for tok in tokens:
            m = _COMPARATOR.match(tok)
            if not m:
                return None
            op, ver = m.group(1) or "=", m.group(2)
            c = compare_versions(ecosystem, version, ver)
            ok = ok and {">=": c >= 0, "<=": c <= 0, ">": c > 0,
                         "<": c < 0, "=": c == 0}[op]
        if ok:
            return True
    return False


def upper_bound_fix(range_expr: str, ecosystem: str = "npm") -> str | None:
    """The first version outside a vulnerable range when the range is a single
    `<X` / `>=a <X` — "fixed in X". Anything fancier returns None rather than a
    guessed target."""
    expr = str(range_expr or "").strip()
    if not expr or "||" in expr:
        return None
    uppers = re.findall(r"(?<![<>=])<\s*v?(\d[\w.\-+]*)", expr)
    return uppers[0] if len(uppers) == 1 else None
