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
        "`MANIFEST.json` lists a sha256 for every other file. Recompute them "
        "and compare; any mismatch means the pack was altered after it was "
        "generated.",
        "",
        # NAME THE COMMAND. "Recompute them and compare" was true and had no
        # executable answer: the only route to the checker was cloning an AGPL
        # repository and installing forty dependencies under Python 3.13. The
        # verifier is now a dependency-free package that runs from 3.9, so the
        # instruction can end in something the reader can actually type — and
        # deliberately not in "ask us for a tool", since the whole point is
        # that checking us must not require trusting us.
        "    pip install celmis",
        "    celmis verify <this-file>.zip",
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


def verify_pack(blob: bytes) -> tuple[bool, list[str]]:
    """Recompute every hash in MANIFEST.json. Returns (ok, problems).

    Here rather than only in a document, because "you can verify this" is a
    claim, and a claim nobody can execute is decoration.
    """
    problems: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            if "MANIFEST.json" not in names:
                return False, ["MANIFEST.json is missing"]
            manifest = json.loads(zf.read("MANIFEST.json"))

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
            listed = manifest.get("files") or {}
            for name, expected in listed.items():
                if name not in names:
                    problems.append(f"{name}: listed but absent")
                    continue
                actual = _sha256(zf.read(name))
                if actual != expected:
                    problems.append(f"{name}: sha256 mismatch")
            extra = names - set(listed) - {"MANIFEST.json"}
            for name in sorted(extra):
                # An unlisted file is as much a problem as a changed one: it is
                # content the manifest does not vouch for.
                problems.append(f"{name}: present but not in the manifest")
    except Exception as exc:  # noqa: BLE001
        return False, [f"unreadable archive: {exc}"]
    return (not problems), problems


__all__ = ["build_evidence_pack", "verify_pack"]
