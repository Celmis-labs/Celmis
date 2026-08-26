"""An SBOM with no `vulnerabilities` key claims nothing at all.

`build_sbom` ended with `if vulns: doc["vulnerabilities"] = vulns`, so a
repository with no known vulnerabilities produced a document byte-for-byte
indistinguishable from one made by a tool that does not look for
vulnerabilities. A consumer cannot tell "we audited this and it is clean" from
"nobody checked", and under a reporting obligation those are opposite claims.

The module's own docstring is what makes this the wrong default: the reason
for choosing CycloneDX over SPDX is that it "carries VULNERABILITIES in the
same document as the components, so one file answers 'what is in it' and
'what is wrong with it' together". A clean repository got a file that answered
only the first.

It is also the confusion two neighbouring modules already name in their own
words — deps/ci.py: "an ecosystem nobody audited reports zero vulnerabilities
exactly like a clean one"; deps/document.py says the same. This is that,
inside the artefact meant for somebody outside the company.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.deps.sbom import build_sbom, to_json


@dataclass
class Dep:
    ecosystem: str = "npm"
    package: str = "lodash"
    version: str = "4.17.21"
    is_dev: bool = False
    manifest: str = "package.json"


def _props(doc) -> dict:
    return {p["name"]: p["value"] for p in doc["metadata"]["properties"]}


def test_a_clean_repo_still_carries_the_key():
    doc = build_sbom(repo_slug="acme/worker", deps=[Dep()], vulnerabilities=[])
    assert "vulnerabilities" in doc
    assert doc["vulnerabilities"] == []


def test_a_clean_repo_says_a_scan_was_performed():
    doc = build_sbom(repo_slug="acme/worker", deps=[Dep()], vulnerabilities=[])
    assert _props(doc)["celmis:vulnerability-scan"] == "performed"


def test_an_unscanned_document_says_so_instead_of_looking_clean():
    """The other half. `vulnerabilities=None` is a caller that did not scan —
    the CVE agent builds an input document for osv-scanner this way — and it
    must not read as a clean bill."""
    doc = build_sbom(repo_slug="acme/worker", deps=[Dep()])
    assert "vulnerabilities" not in doc
    assert _props(doc)["celmis:vulnerability-scan"] == "not-performed"


def test_the_two_documents_are_distinguishable():
    """The property, stated as a property: the clean document and the
    unscanned one must not serialise to the same bytes."""
    clean = to_json(build_sbom(repo_slug="acme/worker", deps=[Dep()],
                               vulnerabilities=[], commit="abc123"))
    unscanned = to_json(build_sbom(repo_slug="acme/worker", deps=[Dep()],
                                   commit="abc123"))
    assert clean != unscanned


def test_findings_still_come_through():
    doc = build_sbom(
        repo_slug="acme/worker", deps=[Dep()],
        vulnerabilities=[{
            "id": "CVE-2021-23337", "ecosystem": "npm", "package": "lodash",
            "version": "4.17.21", "severity": "high",
            "summary": "command injection", "fixed_version": "4.17.21",
        }],
    )
    assert len(doc["vulnerabilities"]) == 1
    assert _props(doc)["celmis:vulnerability-scan"] == "performed"


def test_the_scan_property_does_not_displace_the_existing_ones():
    doc = build_sbom(repo_slug="acme/worker", deps=[Dep()],
                     vulnerabilities=[], commit="abc123")
    props = _props(doc)
    assert props["celmis:repo"] == "acme/worker"
    assert props["celmis:commit"] == "abc123"


def test_the_serial_number_is_still_deterministic():
    """Two exports of the same commit must remain one document to anything
    that de-duplicates by serial number."""
    a = build_sbom(repo_slug="acme/worker", deps=[Dep()], vulnerabilities=[],
                   commit="abc123")
    b = build_sbom(repo_slug="acme/worker", deps=[Dep()], vulnerabilities=[],
                   commit="abc123")
    assert a["serialNumber"] == b["serialNumber"]
