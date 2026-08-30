"""Two copies of one rule, and the day they disagree.

`src/deps/evidence.py` builds evidence packs. `packaging/pypi/celmis/verify.py`
is a standalone copy of the checking half, published to PyPI so that an auditor
can verify a pack without installing the platform or trusting it. The
duplication is deliberate and it is also the hazard: the day somebody adds a
file to `build_evidence_pack()`, every installed `celmis verify` reports

    <newfile>: present but not in the manifest

and an auditor reads that as the operator having altered the pack. A false
accusation of tampering, produced by the project's own tooling, is the most
expensive bug available to a product whose whole position is that its artefacts
survive checking.

So the drift fails a build here instead of an audit there. This runs the
PACKAGED verifier — loaded from its own file, not imported from `src` — over a
pack the CURRENT producer just made.
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGED = ROOT / "packaging" / "pypi" / "celmis" / "verify.py"
FIXTURES = (
    ROOT / "tests" / "fixtures" / "evidence-pack-v1.zip",
    ROOT / "packaging" / "pypi" / "tests" / "fixtures" / "evidence-pack-v1.zip",
)


def _packaged_verifier():
    """Load the published module by path, with no dependency on `src`."""
    assert PACKAGED.is_file(), (
        f"{PACKAGED} is gone — the published verifier moved, and this test is "
        f"the only thing keeping it in step with the producer"
    )
    spec = importlib.util.spec_from_file_location("_packaged_celmis_verify", PACKAGED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_pack() -> bytes:
    from src.deps.evidence import build_evidence_pack

    return build_evidence_pack(
        run={"id": "run-drift", "started_at": "2026-08-30T09:00:00Z"},
        findings=[
            {"id": "GHSA-x", "package": "lodash", "version": "4.17.11",
             "ecosystem": "npm", "severity": "high", "fixed_version": "4.17.21"},
        ],
        sboms={"acme/gateway": {"bomFormat": "CycloneDX", "specVersion": "1.5",
                                "version": 1, "components": []}},
        timeline=[{"run_id": "run-drift", "at": "2026-08-30T09:00:00Z"}],
        generated_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
    )


def test_the_published_verifier_accepts_a_pack_built_today() -> None:
    """The whole point. A new file in the producer fails here, loudly."""
    ok, problems = _packaged_verifier().verify_pack(_fresh_pack())
    assert ok is True, (
        f"the packaged verifier rejects a pack this repository just produced: "
        f"{problems}. Every installed `celmis verify` would report the same, "
        f"and an auditor reads it as tampering. Update "
        f"packaging/pypi/celmis/verify.py and bump the pack's manifest_version "
        f"if the format really changed."
    )


def test_both_copies_agree_on_the_format_version() -> None:
    from src.deps.evidence import MANIFEST_VERSION as produced

    assert produced == _packaged_verifier().MANIFEST_VERSION, (
        "the producer and the published verifier disagree about which format "
        "version is current, which is the one number that decides whether a "
        "mismatch is reported as tampering or as an out-of-date verifier"
    )


def test_the_producer_stamps_the_version_it_claims() -> None:
    import io

    from src.deps.evidence import MANIFEST_VERSION

    with zipfile.ZipFile(io.BytesIO(_fresh_pack())) as zf:
        manifest = json.loads(zf.read("MANIFEST.json"))
    assert manifest["manifest_version"] == MANIFEST_VERSION


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_committed_fixture_still_verifies(fixture: Path) -> None:
    """A fixture in each tree, and each must pass the published checker."""
    assert fixture.is_file(), f"{fixture} is missing"
    ok, problems = _packaged_verifier().verify_pack(fixture.read_bytes())
    assert ok is True, problems


def test_the_two_fixtures_are_the_same_bytes() -> None:
    """One artefact, committed twice. Copies that drift test nothing."""
    first, second = (path.read_bytes() for path in FIXTURES)
    assert first == second, (
        "the fixture in tests/ and the one in packaging/pypi/tests/ differ — "
        "regenerate both from build_evidence_pack() rather than editing either"
    )


def test_the_packaged_verifier_imports_nothing_of_ours() -> None:
    """Its entire value is that it installs where the platform cannot.

    A single `from src...` here and the published wheel is broken for every
    user, while every test in this repository keeps passing.
    """
    source = PACKAGED.read_text(encoding="utf-8")
    tree_lines = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.lstrip().startswith("#")
    ]
    allowed = {"hashlib", "io", "json", "zipfile", "typing", "__future__"}
    for line in tree_lines:
        module = line.split()[1].split(".")[0]
        assert module in allowed, (
            f"{PACKAGED.name} imports {module!r}, which is not in the standard "
            f"library set this package promises: {sorted(allowed)}"
        )


def test_the_pack_tells_the_reader_how_to_check_it() -> None:
    """"Recompute them and compare" needed an answer somebody can type.

    The instruction was true and, until the verifier was published, had no
    executable form: the only route to the checker was cloning an AGPL
    repository and installing forty dependencies under Python 3.13. If the
    package ever goes away this fails, which is the right time to find out —
    a pack that names a tool nobody can install is worse than one that names
    none.
    """
    import io

    with zipfile.ZipFile(io.BytesIO(_fresh_pack())) as zf:
        summary = zf.read("summary.md").decode("utf-8")

    assert "pip install celmis" in summary
    assert "celmis verify" in summary
    assert "https://pypi.org/project/celmis/" in summary
    # And it must not read as "you have to use ours". The manifest is plain
    # JSON; the whole position is that checking us does not require trusting
    # us, and a tool we hand out is not the only way to recompute a sha256.
    assert "not required to use it" in summary


# ─── the manifest does not hash itself, and the product says so ──────


def _forged_pack() -> bytes:
    """Edit a file and rewrite its manifest entry — what an attacker does."""
    import hashlib
    import io

    with zipfile.ZipFile(io.BytesIO(_fresh_pack())) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    entries["summary.md"] = entries["summary.md"] + b"\nquietly edited\n"
    manifest = json.loads(entries["MANIFEST.json"])
    manifest["files"]["summary.md"] = hashlib.sha256(entries["summary.md"]).hexdigest()
    entries["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    return buf.getvalue()


def test_a_forged_pack_passes_the_hashes_alone() -> None:
    """The limit, pinned. Measured, not assumed.

    If this ever starts failing somebody has made the pack self-authenticating,
    and every sentence about what the pack proves should be revisited — that
    would be good news rather than a broken test.
    """
    from src.deps.evidence import verify_pack

    ok, problems = verify_pack(_forged_pack())
    assert ok is True, problems


def test_the_manifest_hash_is_what_catches_it() -> None:
    from src.deps.evidence import manifest_sha256, verify_pack

    genuine = manifest_sha256(_fresh_pack())
    ok, problems = verify_pack(_forged_pack(), expected_manifest_sha256=genuine)
    assert ok is False
    assert any("proves nothing" in p for p in problems), problems


def test_both_copies_compute_the_same_manifest_hash() -> None:
    from src.deps.evidence import manifest_sha256

    blob = _fresh_pack()
    assert manifest_sha256(blob) == _packaged_verifier().manifest_sha256(blob)


def test_the_pack_does_not_claim_more_than_it_proves() -> None:
    """The wording is part of the artefact.

    "a third party can check nothing was edited afterwards without trusting
    us" was the claim, and it is false of a manifest that does not hash
    itself. The pack now says which of the two things it establishes.
    """
    import io

    with zipfile.ZipFile(io.BytesIO(_fresh_pack())) as zf:
        summary = zf.read("summary.md").decode("utf-8")

    assert "none for" in summary and "itself" in summary, (
        "the pack no longer says the manifest omits its own hash"
    )
    assert "NOT PROOF" in summary.upper()
    assert "X-Celmis-Manifest-SHA256" in summary
    assert "--manifest-sha256" in summary


def test_the_export_hands_over_the_second_channel() -> None:
    """A hash nobody is given is a hash nobody can compare.

    Read with ast, not searched for. The docstring above the code explains why
    the header is there and therefore contains its name — a substring check
    passed with the header itself deleted, which is the exact failure this
    file's own subject is about.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "api" / "routers" / "deps.py").read_text("utf-8"))
    export = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "export_evidence"
    )
    header_keys = {
        key.value
        for node in ast.walk(export) if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert "X-Celmis-Manifest-SHA256" in header_keys, (
        f"the evidence export no longer returns the manifest hash, so there is "
        f"no second channel for a recipient to check against. Header keys "
        f"found: {sorted(header_keys)}"
    )
