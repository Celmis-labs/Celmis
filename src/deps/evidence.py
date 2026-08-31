"""An evidence pack: what we knew, when we knew it, and what we did.

The dependency audit already answers "what is wrong with this repository".
That is a developer's question. An auditor asks a different one, and asks it
about a moment in the past: on the day this was exploited, what did you know,
when did you learn it, and what did you do about it?

From 11 September 2026 the EU Cyber Resilience Act makes the second question
enforceable — an actively exploited vulnerability must be reported within 24
hours, with post-market monitoring and SBOM transparency behind it. A
dashboard cannot answer it, because a dashboard shows the present. A record
can.

So this exports one archive per audit run:

    sbom/<repo>.cdx.json      the inventory, CycloneDX, one per repository
    findings.json             every vulnerability, with its severity and fix
    timeline.jsonl            when each run happened and what it found
    summary.md                the human-readable cover sheet
    MANIFEST.json             sha256 of every file above

The manifest is what makes it evidence rather than a folder. An archive whose
contents can be edited afterwards proves nothing; hashes let the holder show
the pack is the one that was generated, and let a third party check it without
trusting us.

What this deliberately does NOT do: claim compliance. It produces the
artefacts a filing needs. Whether the filing is adequate is a lawyer's
judgement, and a tool that implies otherwise is selling a false sense of
safety — the one thing this whole subsystem exists to avoid.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

PRODUCT = "Celmis"


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        key = str(f.get("severity") or "unknown").lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def _summary_md(
    *, run: dict, findings: list[dict], repos: list[str],
    generated_at: datetime, sbom_count: int,
) -> str:
    counts = _severity_counts(findings)
    order = ["critical", "high", "medium", "moderate", "low", "info", "unknown"]
    lines = [
        f"# {PRODUCT} — dependency evidence pack",
        "",
        f"Generated {generated_at.isoformat()}",
        f"Audit run `{run.get('id', 'unknown')}`, "
        f"started {run.get('started_at', 'unknown')}",
        "",
        "## What this is",
        "",
        "The inventory of every dependency in the repositories below, the "
        "vulnerabilities known against them at the time of this run, and a "
        "hash of each file so the pack can be verified without trusting the "
        "tool that produced it.",
        "",
        "It is evidence, not a compliance certificate. Whether it satisfies a "
        "particular obligation is a judgement this tool does not make.",
        "",
        "## Scope",
        "",
        f"- Repositories: {len(repos)}",
        f"- SBOM documents: {sbom_count} (CycloneDX 1.5)",
        f"- Findings: {len(findings)}",
        "",
    ]
    if counts:
        lines += ["## Findings by severity", ""]
        for key in order:
            if counts.get(key):
                lines.append(f"- {key}: {counts[key]}")
        for key in sorted(set(counts) - set(order)):
            lines.append(f"- {key}: {counts[key]}")
        lines.append("")

    unfixable = [f for f in findings if not f.get("fixed_version")]
    if unfixable:
        lines += [
            "## Findings with no fixed version",
            "",
            f"{len(unfixable)} of {len(findings)} have no upstream fix at the "
            "time of this run. These are the ones a filing usually has to "
            "explain rather than resolve.",
            "",
        ]

    lines += ["## Repositories", ""]
    lines += [f"- {slug}" for slug in repos]
    lines.append("")
    lines += [
        "## Verifying this pack",
        "",
        "`MANIFEST.json` lists a sha256 for every other file — and none for "
        "itself, because a file cannot contain its own hash. Recompute them "
        "and compare; any mismatch means this archive is not internally "
        "consistent.",
        "",
        "THAT ALONE IS NOT PROOF THE PACK IS THE ONE YOU WERE SENT. Somebody "
        "who edits a file and rewrites its entry in the manifest passes that "
        "check. What they cannot do is make the manifest hash to a value you "
        "obtained elsewhere, so ask whoever gave you the pack for the sha256 "
        "of `MANIFEST.json` through a different channel — it is returned in "
        "the `X-Celmis-Manifest-SHA256` header at export — and check it:",
        "",
        # NAME THE COMMAND. "Recompute them and compare" was true and had no
        # executable answer: the only route to the checker was cloning an AGPL
        # repository and installing forty dependencies under Python 3.13. The
        # verifier is now a dependency-free package that runs from 3.9, so the
        # instruction can end in something the reader can actually type — and
        # deliberately not in "ask us for a tool", since the whole point is
        # that checking us must not require trusting us.
        "    pip install celmis",
        "    celmis verify --manifest-sha256 <hash> <this-file>.zip",
        "",
        "That checker is a few hundred lines of Python standard library with "
        "no dependencies and no network calls: <https://pypi.org/project/"
        "celmis/>. Its source is in `packaging/pypi/` of the repository below, "
        "and you are not required to use it — the manifest is plain JSON and "
        "sha256 is sha256.",
        "",
    ]
    return "\n".join(lines)


#: The pack format, so a verifier can tell "I do not understand this" from
#: "this has been altered". Those are opposite answers and a reader who cannot
#: separate them will read the first as the second — an accusation of tampering
#: aimed at whoever produced a perfectly good pack with a newer Celmis.
#:
#: Bump the MAJOR only when an existing verifier would compute the wrong
#: answer; anything additive keeps this number. A pack written before this
#: field existed is version 1, which is what `verify_pack` assumes.
MANIFEST_VERSION = 1

#: A zip says how big each member is before you read it, and a verifier that
#: ignores that is a decompression bomb waiting for somebody. Measured against
#: the published verifier: 200 KB on disk declaring 200 MB verified as OK with
#: a 215 MB peak. A real pack is kilobytes; these are three orders of
#: magnitude of headroom, and the same numbers as the standalone copy.
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
_CHUNK = 1024 * 1024


class PackRefused(Exception):
    """The archive was not read at all — a size limit, or a manifest that is
    not an object. Distinct from "read it and found problems"."""


def _guard_sizes(zf: zipfile.ZipFile) -> None:
    total = 0
    for info in zf.infolist():
        if info.file_size > MAX_MEMBER_BYTES:
            raise PackRefused(
                f"{info.filename}: declares {info.file_size} bytes, over the "
                f"{MAX_MEMBER_BYTES}-byte limit for one file")
        total += info.file_size
    if total > MAX_UNCOMPRESSED_BYTES:
        raise PackRefused(
            f"this archive declares {total} bytes uncompressed, over the "
            f"{MAX_UNCOMPRESSED_BYTES}-byte limit")


def _as_manifest(raw: object) -> dict:
    """A manifest that is valid JSON and not an object reached `.get()`."""
    if not isinstance(raw, dict):
        raise PackRefused(f"MANIFEST.json is {type(raw).__name__}, not an object")
    files = raw.get("files")
    if files is not None and not isinstance(files, dict):
        raise PackRefused(
            f"MANIFEST.json: 'files' is {type(files).__name__}, not an object")
    return raw


def _sha256_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """The member's hash, a megabyte at a time, never held whole."""
    if info.file_size > MAX_MEMBER_BYTES:
        raise PackRefused(
            f"{info.filename}: declares {info.file_size} bytes, over the limit")
    digest = hashlib.sha256()
    seen = 0
    with zf.open(info) as stream:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            seen += len(chunk)
            if seen > info.file_size:
                raise PackRefused(
                    f"{info.filename}: expands past its own declared size")
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_pack(
    *,
    run: dict,
    findings: list[dict],
    sboms: dict[str, dict],
    timeline: list[dict] | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    """One zip archive. Returns its bytes.

    `sboms` maps repo slug → CycloneDX document. `timeline` is the history of
    audit runs, which is the part that answers "when did you know".
    """
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    repos = sorted(sboms)

    files: dict[str, bytes] = {}

    for slug, doc in sboms.items():
        safe = slug.replace("/", "_")
        files[f"sbom/{safe}.cdx.json"] = (
            json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode()

    files["findings.json"] = (
        json.dumps(findings, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()

    # JSONL rather than JSON: a timeline is appended to over time, and a
    # format that can be appended to without rewriting is the one that stays
    # honest across exports.
    files["timeline.jsonl"] = "".join(
        json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
        for row in (timeline or [])
    ).encode()

    files["summary.md"] = _summary_md(
        run=run, findings=findings, repos=repos,
        generated_at=now, sbom_count=len(sboms),
    ).encode()

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "product": PRODUCT,
        "generated_at": now.isoformat(),
        "run_id": run.get("id"),
        "algorithm": "sha256",
        "files": {name: _sha256(blob) for name, blob in sorted(files.items())},
    }
    files["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()

    buf = io.BytesIO()
    # Deterministic: fixed timestamps inside the archive, sorted entries. Two
    # exports of the same run then produce identical bytes, which is what lets
    # somebody prove a pack was not regenerated with different contents.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    return buf.getvalue()


def manifest_sha256(blob: bytes) -> str:
    """The sha256 of MANIFEST.json itself.

    THE ONE NUMBER THE PACK CANNOT VOUCH FOR. Everything else in the archive
    is covered by a hash the manifest records; the manifest is not, because a
    file cannot contain its own hash. So this has to travel by a different
    route than the pack — read off at export, published, mailed, put in a
    ticket — and that second channel is what turns the check into evidence.
    """
    return _sha256(_read_manifest_bytes(blob))


def _read_manifest_bytes(blob: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.read("MANIFEST.json")


def verify_pack(
    blob: bytes, *, expected_manifest_sha256: str | None = None,
) -> tuple[bool, list[str]]:
    """Recompute every hash in MANIFEST.json. Returns (ok, problems).

    WHAT THIS PROVES WITHOUT `expected_manifest_sha256`, AND WHAT IT DOES NOT.
    The manifest lists a hash for every other file and no hash for itself, so
    recomputing them proves the archive is internally CONSISTENT. It catches a
    truncated download, a byte flipped in transit, a file swapped or added by
    somebody who did not know the format. It does not catch forgery, and the
    demonstration is three lines: open the zip, edit `summary.md`, write the
    new sha256 into `MANIFEST.json`, repack. Measured against a real
    production pack, that alteration verified as `OK` and exited 0.

    With the expected hash supplied, the chain closes: the manifest fixes
    every file, and the caller fixes the manifest from a source the person who
    handed over the pack did not control. Unforgeability lives in that second
    channel, not in the archive — an unsigned manifest that does not hash
    itself cannot deliver it, and saying otherwise would be selling exactly
    the false confidence this subsystem exists to avoid.
    """
    problems: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            if "MANIFEST.json" not in names:
                return False, ["MANIFEST.json is missing"]
            _guard_sizes(zf)
            manifest = _as_manifest(json.loads(zf.read("MANIFEST.json")))

            # A pack from a newer Celmis is not a broken pack. Say which it is
            # before checking a single hash: every problem reported below reads
            # as "somebody changed this", and that is the wrong thing to tell
            # a person holding a pack this verifier is simply too old for.
            declared = manifest.get("manifest_version", 1)
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                return False, [
                    f"MANIFEST.json declares manifest_version {declared!r}, "
                    f"which is not a version number",
                ]
            if declared > MANIFEST_VERSION:
                return False, [
                    f"this pack is format version {declared} and this verifier "
                    f"understands {MANIFEST_VERSION} — it was produced by a "
                    f"newer Celmis, so upgrade the verifier rather than "
                    f"treating this as a failed check",
                ]
            # Before the per-file work, because a manifest that is not the one
            # you were promised makes every hash below beside the point: they
            # would all agree, with each other.
            if expected_manifest_sha256 is not None:
                actual = _sha256(zf.read("MANIFEST.json"))
                wanted = expected_manifest_sha256.strip().lower()
                if actual != wanted:
                    return False, [
                        f"MANIFEST.json: sha256 is {actual}, expected {wanted} "
                        f"— this is not the manifest you were given the hash "
                        f"for, so the rest of the pack proves nothing",
                    ]

            listed = manifest.get("files") or {}
            for name, expected in listed.items():
                if name not in names:
                    problems.append(f"{name}: listed but absent")
                    continue
                actual = _sha256_member(zf, zf.getinfo(name))
                if actual != expected:
                    problems.append(f"{name}: sha256 mismatch")
            extra = names - set(listed) - {"MANIFEST.json"}
            for name in sorted(extra):
                # An unlisted file is as much a problem as a changed one: it is
                # content the manifest does not vouch for.
                problems.append(f"{name}: present but not in the manifest")
    except PackRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        return False, [f"unreadable archive: {exc}"]
    return (not problems), problems


__all__ = ["PackRefused", "build_evidence_pack", "manifest_sha256", "verify_pack"]
