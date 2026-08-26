"""A Software Bill of Materials, in the format a compliance tool can read.

The dependency audit already knows every package and version in a repository —
`DeclaredDep` is exactly the SBOM row, produced from manifests AND lock files
so transitive dependencies are covered. What was missing was the ability to
hand that inventory to somebody who is not us.

That matters on a date. From 11 September 2026 the EU Cyber Resilience Act
requires reporting an actively exploited vulnerability within 24 hours, and
the reporting obligation assumes you can say what you shipped and when. An
auditor does not accept a screenshot of a dashboard; they accept a document,
and CycloneDX is the one procurement and vulnerability tooling already parses.

Two deliberate choices:

  * CycloneDX 1.5 JSON, not SPDX. Both are accepted; CycloneDX carries
    VULNERABILITIES in the same document as the components, so one file
    answers "what is in it" and "what is wrong with it" together. Splitting
    those across two formats is how a bill of materials becomes a filing
    exercise rather than a record.

  * Package URLs (purl) for every component. A name and a version are
    ambiguous across ecosystems — `requests` is a PyPI package and an npm one
    — and every downstream matcher keys on purl. Without it the document is
    readable by humans and useless to tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

SPEC_VERSION = "1.5"
PRODUCT = "Celmis"

#: OSV ecosystem name → the purl type registered for it. The two vocabularies
#: differ and neither is derivable from the other, so the map is explicit.
_PURL_TYPE = {
    "npm": "npm",
    "PyPI": "pypi",
    "Go": "golang",
    "crates.io": "cargo",
    "Packagist": "composer",
    "RubyGems": "gem",
    "NuGet": "nuget",
    "Maven": "maven",
    "Hex": "hex",
    "Pub": "pub",
}

#: CVSS-less severities from the various auditors, mapped to the CycloneDX
#: vocabulary. Anything unrecognised becomes "unknown" rather than "none" —
#: claiming a vulnerability is harmless is worse than admitting we do not know.
_SEVERITY = {
    "critical": "critical",
    "high": "high",
    "moderate": "medium",
    "medium": "medium",
    "low": "low",
    "info": "info",
    "informational": "info",
}


def _purl(ecosystem: str, package: str, version: str) -> str:
    """A package URL, or a best-effort string when the ecosystem is unknown.

    Maven coordinates carry a namespace in `group:artifact` form, and purl
    spells that with a slash; everything else passes the name through.
    """
    ptype = _PURL_TYPE.get(ecosystem, (ecosystem or "generic").lower())
    name = package
    namespace = ""
    if ptype == "maven" and ":" in package:
        namespace, name = package.split(":", 1)
    elif package.startswith("@") and "/" in package:      # npm scope
        namespace, name = package.split("/", 1)
    parts = [f"pkg:{ptype}"]
    if namespace:
        parts.append(quote(namespace, safe=""))
    parts.append(quote(name, safe=""))
    purl = "/".join(parts)
    return f"{purl}@{quote(version, safe='')}" if version else purl


def _ref(ecosystem: str, package: str, version: str) -> str:
    """A stable bom-ref. Hashed so it is safe in every JSON consumer."""
    raw = f"{ecosystem}|{package}|{version}".encode()
    return "c-" + hashlib.sha256(raw).hexdigest()[:24]


def build_sbom(
    *,
    repo_slug: str,
    deps: list[Any],
    vulnerabilities: list[dict] | None = None,
    commit: str = "",
    generated_at: datetime | None = None,
) -> dict:
    """One CycloneDX document for one repository.

    `deps` are `DeclaredDep`-shaped: ecosystem, package, version, is_dev,
    manifest. `vulnerabilities` are the audit's own findings, keyed to the
    same package and version.
    """
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)

    components: list[dict] = []
    by_key: dict[tuple[str, str, str], str] = {}
    for dep in deps:
        eco = getattr(dep, "ecosystem", "") or ""
        pkg = getattr(dep, "package", "") or ""
        ver = getattr(dep, "version", "") or ""
        if not pkg:
            continue
        key = (eco, pkg, ver)
        if key in by_key:
            # The same package can be declared in several manifests of one
            # repo. One component, several occurrences — duplicating it would
            # inflate every count an auditor reads off the document.
            continue
        ref = _ref(eco, pkg, ver)
        by_key[key] = ref
        components.append({
            "type": "library",
            "bom-ref": ref,
            "name": pkg,
            "version": ver,
            "purl": _purl(eco, pkg, ver),
            "scope": "optional" if getattr(dep, "is_dev", False) else "required",
            "properties": [
                {"name": "celmis:ecosystem", "value": eco},
                {"name": "celmis:manifest",
                 "value": getattr(dep, "manifest", "") or ""},
            ],
        })

    vulns: list[dict] = []
    for v in vulnerabilities or []:
        pkg = str(v.get("package") or "")
        ver = str(v.get("version") or "")
        eco = str(v.get("ecosystem") or "")
        ref = by_key.get((eco, pkg, ver))
        ident = str(v.get("id") or v.get("cve") or "")
        if not ident:
            continue
        entry: dict[str, Any] = {
            "id": ident,
            "ratings": [{
                "severity": _SEVERITY.get(
                    str(v.get("severity") or "").lower(), "unknown"),
            }],
            "description": str(v.get("summary") or "")[:2000],
        }
        aliases = [a for a in (v.get("aliases") or []) if a]
        if aliases:
            entry["references"] = [{"id": a, "source": {"name": "OSV"}}
                                   for a in aliases[:10]]
        if ref:
            entry["affects"] = [{"ref": ref}]
        else:
            # A finding whose component is not in the inventory is worth
            # keeping and worth flagging: it means the auditor saw something
            # the manifest scan did not, which is a coverage gap, not noise.
            entry["properties"] = [
                {"name": "celmis:unmatched-component",
                 "value": f"{eco}:{pkg}@{ver}"},
            ]
        if v.get("fixed_version"):
            entry["recommendation"] = f"Update to {v['fixed_version']}"
        vulns.append(entry)

    doc: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        # Deterministic from the repo and the commit, so regenerating the same
        # state produces the same identifier rather than a new one each run.
        "serialNumber": "urn:uuid:" + str(_deterministic_uuid(repo_slug, commit)),
        "version": 1,
        "metadata": {
            "timestamp": now.isoformat(),
            "tools": [{"vendor": PRODUCT, "name": f"{PRODUCT} dependency audit"}],
            "component": {
                "type": "application",
                "bom-ref": "root",
                "name": repo_slug,
                "version": commit[:12] or "unknown",
            },
            "properties": [
                {"name": "celmis:repo", "value": repo_slug},
                {"name": "celmis:commit", "value": commit},
            ],
        },
        "components": components,
    }
    # ALWAYS PRESENT WHEN A SCAN HAPPENED, even at zero.
    #
    # This was `if vulns:`, so a clean repository produced a document with no
    # `vulnerabilities` key — byte-for-byte indistinguishable from one made by
    # a tool that does not look for vulnerabilities at all. A consumer reading
    # it cannot tell "we audited this and it is clean" from "nobody checked",
    # and under a reporting obligation those are opposite claims. It is the
    # same confusion `deps/document.py` and `deps/ci.py` already name in their
    # own words: "an unchecked ecosystem reports zero vulnerabilities exactly
    # like a clean one".
    #
    # The `None` default carries the other half. A caller that did not scan
    # passes nothing and gets no key plus a property that says why — silence
    # with a reason attached, rather than silence that reads as a clean bill.
    if vulnerabilities is None:
        doc["metadata"]["properties"].append(
            {"name": "celmis:vulnerability-scan", "value": "not-performed"})
    else:
        doc["vulnerabilities"] = vulns
        doc["metadata"]["properties"].append(
            {"name": "celmis:vulnerability-scan", "value": "performed"})
    return doc


def _deterministic_uuid(repo_slug: str, commit: str) -> str:
    """A UUID derived from what the document describes.

    Random per run would mean two exports of the same commit are two different
    documents to anything that de-duplicates by serial number, which is how a
    filing ends up with six copies of one bill of materials.
    """
    digest = hashlib.sha256(f"{repo_slug}|{commit}".encode()).hexdigest()
    return (f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}"
            f"-{digest[16:20]}-{digest[20:32]}")


def to_json(doc: dict) -> str:
    """Stable serialisation — sorted keys, so a diff between two exports is
    about the contents rather than about dictionary ordering."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


__all__ = ["build_sbom", "to_json", "SPEC_VERSION"]
