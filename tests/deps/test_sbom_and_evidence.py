"""The two artefacts an auditor accepts, and the reasons they are not decoration.

The dependency audit answers a developer's question: what is wrong with this
repository, now. An auditor asks a different one, about a moment in the past —
on the day this was exploited, what did you know, when, and what did you do.

The tests below are mostly about the ways a document like this becomes
worthless while still looking complete:

  * a component without a purl reads fine to a human and matches nothing in
    any downstream tool;
  * a serial number regenerated per export turns one bill of materials into
    six copies in whatever de-duplicates by it;
  * an archive whose contents can be edited afterwards proves nothing at all;
  * and a severity we did not recognise, quietly rendered as "none", says a
    vulnerability is harmless because we could not read its label.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

from src.deps.evidence import build_evidence_pack, verify_pack
from src.deps.sbom import build_sbom, to_json


@dataclass(frozen=True)
class _Dep:
    ecosystem: str
    package: str
    version: str
    raw_spec: str = ""
    is_dev: bool = False
    manifest: str = "package.json"


AT = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


# ─── the inventory ───────────────────────────────────────────────────


def test_every_component_carries_a_purl():
    """A name and a version are ambiguous across ecosystems — `requests` is a
    PyPI package and an npm one — and every downstream matcher keys on purl.
    Without it the document is readable and useless."""
    doc = build_sbom(repo_slug="acme/api", deps=[
        _Dep("PyPI", "requests", "2.31.0", manifest="requirements.txt"),
        _Dep("npm", "left-pad", "1.3.0"),
    ], generated_at=AT)
    purls = {c["purl"] for c in doc["components"]}
    assert "pkg:pypi/requests@2.31.0" in purls
    assert "pkg:npm/left-pad@1.3.0" in purls


def test_ecosystem_names_are_translated_not_lowercased():
    """OSV says `crates.io` and `PyPI`; purl says `cargo` and `pypi`. Neither
    is derivable from the other, so a lowercase() would produce
    `pkg:crates.io/...`, which no tool resolves."""
    doc = build_sbom(repo_slug="acme/svc", deps=[
        _Dep("crates.io", "serde", "1.0.0", manifest="Cargo.toml"),
        _Dep("Go", "github.com/pkg/errors", "0.9.1", manifest="go.mod"),
    ], generated_at=AT)
    purls = {c["purl"] for c in doc["components"]}
    assert "pkg:cargo/serde@1.0.0" in purls
    assert any(p.startswith("pkg:golang/") for p in purls)


def test_scoped_and_namespaced_packages_keep_their_namespace():
    doc = build_sbom(repo_slug="acme/web", deps=[
        _Dep("npm", "@types/node", "20.1.0"),
        _Dep("Maven", "org.apache.commons:commons-lang3", "3.14.0",
             manifest="pom.xml"),
    ], generated_at=AT)
    purls = {c["purl"] for c in doc["components"]}
    assert "pkg:npm/%40types/node@20.1.0" in purls
    assert "pkg:maven/org.apache.commons/commons-lang3@3.14.0" in purls


def test_a_package_declared_twice_is_one_component():
    """The same dependency appears in several manifests of one repo.
    Duplicating it inflates every count an auditor reads off the document."""
    doc = build_sbom(repo_slug="acme/api", deps=[
        _Dep("PyPI", "urllib3", "2.0.7", manifest="requirements.txt"),
        _Dep("PyPI", "urllib3", "2.0.7", manifest="requirements-dev.txt"),
    ], generated_at=AT)
    assert len(doc["components"]) == 1


def test_dev_dependencies_are_marked_not_dropped():
    """They are in the artefact's supply chain even when they are not in the
    artefact — a compromised test-time package still ran on the build box."""
    doc = build_sbom(repo_slug="acme/api", deps=[
        _Dep("npm", "jest", "29.0.0", is_dev=True),
        _Dep("npm", "express", "4.18.0"),
    ], generated_at=AT)
    scopes = {c["name"]: c["scope"] for c in doc["components"]}
    assert scopes["jest"] == "optional"
    assert scopes["express"] == "required"


def test_the_serial_number_is_deterministic():
    """Random per run means two exports of the same commit are two documents
    to anything that de-duplicates by serial number — which is how a filing
    ends up holding six copies of one bill of materials."""
    args = dict(repo_slug="acme/api",
                deps=[_Dep("PyPI", "requests", "2.31.0")],
                commit="abc123def456")
    first = build_sbom(**args, generated_at=AT)
    second = build_sbom(**args, generated_at=datetime.now(UTC))
    assert first["serialNumber"] == second["serialNumber"]
    # …and it changes when the thing it describes changes.
    other = build_sbom(**{**args, "commit": "999999999999"}, generated_at=AT)
    assert other["serialNumber"] != first["serialNumber"]


def test_vulnerabilities_travel_in_the_same_document():
    """CycloneDX over SPDX for exactly this: one file answers both "what is in
    it" and "what is wrong with it"."""
    doc = build_sbom(
        repo_slug="acme/api",
        deps=[_Dep("PyPI", "requests", "2.20.0")],
        vulnerabilities=[{
            "id": "GHSA-xxxx", "package": "requests", "version": "2.20.0",
            "ecosystem": "PyPI", "severity": "high",
            "summary": "Header injection", "fixed_version": "2.31.0",
            "aliases": ["CVE-2023-32681"],
        }],
        generated_at=AT)
    vuln = doc["vulnerabilities"][0]
    assert vuln["id"] == "GHSA-xxxx"
    assert vuln["ratings"][0]["severity"] == "high"
    assert "2.31.0" in vuln["recommendation"]
    # Linked to the component, or a reader cannot tell what it affects.
    assert vuln["affects"][0]["ref"] == doc["components"][0]["bom-ref"]


def test_an_unrecognised_severity_is_unknown_not_none():
    """Rendering a label we could not read as "none" tells the reader a
    vulnerability is harmless because our map was incomplete."""
    doc = build_sbom(
        repo_slug="acme/api", deps=[_Dep("PyPI", "x", "1.0")],
        vulnerabilities=[{"id": "V-1", "package": "x", "version": "1.0",
                          "ecosystem": "PyPI", "severity": "catastrophic"}],
        generated_at=AT)
    assert doc["vulnerabilities"][0]["ratings"][0]["severity"] == "unknown"


def test_a_finding_with_no_matching_component_is_kept_and_flagged():
    """It means the auditor saw something the manifest scan did not — a
    coverage gap, which is exactly what an evidence pack should surface rather
    than silently drop."""
    doc = build_sbom(
        repo_slug="acme/api", deps=[_Dep("PyPI", "requests", "2.31.0")],
        vulnerabilities=[{"id": "V-2", "package": "ghost", "version": "0.1",
                          "ecosystem": "PyPI", "severity": "low"}],
        generated_at=AT)
    vuln = doc["vulnerabilities"][0]
    assert "affects" not in vuln
    assert any(p["name"] == "celmis:unmatched-component"
               for p in vuln["properties"])


def test_the_json_is_stable_across_runs():
    doc = build_sbom(repo_slug="acme/api",
                     deps=[_Dep("npm", "b", "1.0"), _Dep("npm", "a", "2.0")],
                     generated_at=AT)
    assert to_json(doc) == to_json(doc)
    assert '"bomFormat": "CycloneDX"' in to_json(doc)


# ─── the evidence pack ───────────────────────────────────────────────


def _pack() -> bytes:
    return build_evidence_pack(
        run={"id": "run-7", "started_at": "2026-09-11T10:00:00Z"},
        findings=[
            {"id": "GHSA-a", "package": "requests", "version": "2.20.0",
             "ecosystem": "PyPI", "severity": "high", "fixed_version": "2.31.0"},
            {"id": "GHSA-b", "package": "old-lib", "version": "0.1",
             "ecosystem": "npm", "severity": "critical"},
        ],
        sboms={"acme/api": build_sbom(
            repo_slug="acme/api", deps=[_Dep("PyPI", "requests", "2.20.0")],
            generated_at=AT)},
        timeline=[{"run": "run-6", "at": "2026-09-04T10:00:00Z", "findings": 3},
                  {"run": "run-7", "at": "2026-09-11T10:00:00Z", "findings": 2}],
        generated_at=AT)


def test_the_pack_contains_what_a_filing_needs():
    with zipfile.ZipFile(__import__("io").BytesIO(_pack())) as zf:
        names = set(zf.namelist())
    assert "sbom/acme_api.cdx.json" in names
    assert "findings.json" in names
    assert "timeline.jsonl" in names
    assert "summary.md" in names
    assert "MANIFEST.json" in names


def test_every_file_is_hashed():
    """The manifest is what makes it evidence rather than a folder."""
    blob = _pack()
    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
        listed = set(manifest["files"])
        present = set(zf.namelist()) - {"MANIFEST.json"}
    assert listed == present
    assert manifest["algorithm"] == "sha256"


def test_an_intact_pack_verifies():
    ok, problems = verify_pack(_pack())
    assert ok, problems


def test_an_edited_pack_does_not_verify():
    """The claim "you can check this" has to be executable, or it is
    decoration."""
    import io

    original = _pack()
    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, \
            zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "findings.json":
                data = b"[]\n"          # somebody removes the findings
            dst.writestr(name, data)

    ok, problems = verify_pack(tampered.getvalue())
    assert not ok
    assert any("findings.json" in p for p in problems)


def test_a_file_added_after_the_fact_does_not_verify():
    """An unlisted file is as much a problem as a changed one: it is content
    the manifest does not vouch for."""
    import io

    original = _pack()
    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as src, \
            zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("extra-note.txt", b"added later")

    ok, problems = verify_pack(tampered.getvalue())
    assert not ok
    assert any("extra-note.txt" in p for p in problems)


def test_two_exports_of_the_same_run_are_byte_identical():
    """So somebody can show a pack was not regenerated with different
    contents. Archive timestamps are fixed and entries sorted for this."""
    assert _pack() == _pack()


def test_the_summary_does_not_claim_compliance():
    """It produces the artefacts a filing needs. Whether the filing is
    adequate is a lawyer's judgement, and implying otherwise sells exactly the
    false sense of safety this subsystem exists to avoid."""
    import io

    with zipfile.ZipFile(io.BytesIO(_pack())) as zf:
        summary = zf.read("summary.md").decode()
    assert "not a compliance certificate" in summary
    assert "judgement this tool does not make" in summary


def test_the_summary_surfaces_findings_with_no_fix():
    """Those are the ones a filing has to explain rather than resolve, and
    they are invisible in a plain severity count."""
    import io

    with zipfile.ZipFile(io.BytesIO(_pack())) as zf:
        summary = zf.read("summary.md").decode()
    assert "no fixed version" in summary


def test_an_unreadable_archive_is_reported_not_raised():
    ok, problems = verify_pack(b"not a zip at all")
    assert not ok
    assert problems and "unreadable" in problems[0]


# ─── the README makes claims; they have to stay true ─────────────────


def test_every_check_the_readme_names_actually_exists():
    """The README lists the deterministic checks by their internal names, as
    the thing that distinguishes this product. A rename would turn a selling
    point into a lie, and nothing else would notice."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text()
    hygiene = (root / "src" / "deps" / "hygiene.py").read_text()

    for check in ("install_script", "python_build_hooks", "cargo_build_script",
                  "non_registry", "suspect_name", "lock_drift"):
        assert check in readme, f"README no longer names {check}"
        assert check in hygiene, f"{check} is claimed but not in hygiene.py"

    drift = (root / "src" / "review" / "cross_repo_drift.py")
    assert "cross_repo_drift" in readme
    assert drift.exists()


def test_the_readme_does_not_claim_compliance():
    """Same rule as the evidence pack itself: produce the artefacts, do not
    assert that somebody's filing is adequate."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    section = readme[readme.index("## Deterministic checks"):]
    section = section[:section.index("## Architecture")]
    # Whitespace-normalised: the sentence is wrapped in the file, and asserting
    # on prose that a reflow can break is a test that fails for the wrong
    # reason. It is the CLAIM that must survive, not the line width.
    flat = " ".join(section.split())
    assert "does not claim your filing is adequate" in flat
    for overclaim in ("CRA compliant", "guarantees compliance",
                      "ensures compliance"):
        assert overclaim.lower() not in flat.lower()


# ─── the pack says which format it is ────────────────────────────────
#
# "You can verify this without trusting us" is a claim the product makes in
# `summary.md` inside every pack. A verifier that cannot tell "I am too old
# for this" from "this has been altered" turns the first into the second, and
# the accusation lands on whoever produced a perfectly good pack.


# `_pack()` above is the shared fixture — it pins `generated_at`, which is
# what makes two exports byte-identical. Defining a second one here shadowed
# it and broke three tests that had nothing to do with this change.

def test_the_manifest_declares_its_format() -> None:
    import io
    import json
    import zipfile

    from src.deps.evidence import MANIFEST_VERSION

    with zipfile.ZipFile(io.BytesIO(_pack())) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
    assert manifest["manifest_version"] == MANIFEST_VERSION


def test_a_pack_from_a_newer_celmis_is_not_called_tampered() -> None:
    """The whole reason the field exists."""
    import io
    import json
    import zipfile

    from src.deps.evidence import MANIFEST_VERSION, verify_pack

    original = _pack()
    with zipfile.ZipFile(io.BytesIO(original)) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["MANIFEST.json"])
    manifest["manifest_version"] = MANIFEST_VERSION + 1
    entries["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])

    ok, problems = verify_pack(buf.getvalue())
    assert ok is False
    joined = " ".join(problems)
    assert "upgrade the verifier" in joined
    assert "altered" not in joined and "mismatch" not in joined, (
        f"a newer format was reported as tampering: {problems}"
    )


def test_a_pack_without_the_field_is_still_version_one() -> None:
    """Packs made before the field existed must keep verifying."""
    import io
    import json
    import zipfile

    from src.deps.evidence import verify_pack

    with zipfile.ZipFile(io.BytesIO(_pack())) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(entries["MANIFEST.json"])
    del manifest["manifest_version"]
    body = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    # The manifest hashes the OTHER files, not itself, so dropping a key from
    # it leaves every recorded digest correct.
    entries["MANIFEST.json"] = body

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])

    ok, problems = verify_pack(buf.getvalue())
    assert ok is True, problems


def test_a_changed_byte_is_still_reported() -> None:
    """The version gate must not have swallowed the check it stands in front of."""
    import io
    import zipfile

    from src.deps.evidence import verify_pack

    with zipfile.ZipFile(io.BytesIO(_pack())) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    victim = next(n for n in entries if n != "MANIFEST.json")
    entries[victim] = entries[victim] + b" "

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])

    ok, problems = verify_pack(buf.getvalue())
    assert ok is False
    assert any(victim in p for p in problems), (
        f"the altered file was not named: {problems}"
    )
