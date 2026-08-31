"""Recompute every hash in an evidence pack's manifest. Standard library only.

This is a deliberate second copy of `src/deps/evidence.py::verify_pack` from
the Celmis platform, and the duplication is the point: the person who has to
check a pack is usually not the operator who produced it, so asking them to
install the producer's tooling in order to check the producer's output defeats
the checking. Nothing here imports anything that is not in Python itself, and
nothing here opens a socket.

Two copies of one rule drift. `manifest_version` is what keeps that drift from
being reported as tampering — see `verify_pack`.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

#: The pack format this verifier understands.
#:
#: A pack that declares a HIGHER major was produced by a newer Celmis, and
#: saying so is not the same as saying it was altered. Those are opposite
#: answers, and a reader who cannot tell them apart will read the first as the
#: second — an accusation aimed at whoever produced a perfectly good pack. A
#: pack with no such field predates it and is version 1.
MANIFEST_VERSION = 1

MANIFEST_NAME = "MANIFEST.json"

#: A zip declares how big each member is before you read it, and a verifier
#: that ignores that is a decompression bomb waiting for somebody. Measured
#: against celmis 0.2.0: an archive of 200 KB on disk declaring 200 MB verified
#: as OK with a 215 MB peak, and the number is the attacker's to choose. An
#: auditor runs this on a laptop, on a file that arrived by email, from a party
#: they are checking BECAUSE they do not trust them.
#:
#: A real evidence pack is kilobytes — the one this was written against is
#: 17 KB. These are three orders of magnitude of headroom, and refusing past
#: them is not a judgement about the pack's contents.
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

#: Hash a member without ever holding it whole.
_CHUNK = 1024 * 1024


class PackError(Exception):
    """The archive could not be read as an evidence pack at all."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    """The member's hash, a megabyte at a time.

    `zf.read(name)` materialises the whole member, so a declared size is an
    instruction to allocate that much. This checks the declared size first and
    then counts what actually arrives — a zip header can lie in the other
    direction too, and a stream that outgrows its own declaration is refused
    rather than followed.
    """
    if info.file_size > MAX_MEMBER_BYTES:
        raise PackError(
            f"{info.filename}: declares {info.file_size} bytes, over the "
            f"{MAX_MEMBER_BYTES}-byte limit for one file in an evidence pack",
        )
    digest = hashlib.sha256()
    seen = 0
    with zf.open(info) as stream:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            seen += len(chunk)
            if seen > info.file_size:
                raise PackError(
                    f"{info.filename}: expands past the {info.file_size} bytes "
                    f"its own header declares",
                )
            digest.update(chunk)
    return digest.hexdigest()


def _guard_sizes(zf: zipfile.ZipFile) -> None:
    """Refuse an archive that would cost more to read than it can be worth."""
    total = 0
    for info in zf.infolist():
        if info.file_size > MAX_MEMBER_BYTES:
            raise PackError(
                f"{info.filename}: declares {info.file_size} bytes, over the "
                f"{MAX_MEMBER_BYTES}-byte limit for one file",
            )
        total += info.file_size
    if total > MAX_UNCOMPRESSED_BYTES:
        raise PackError(
            f"this archive declares {total} bytes uncompressed, over the "
            f"{MAX_UNCOMPRESSED_BYTES}-byte limit — an evidence pack is "
            f"kilobytes, so this is refused rather than read",
        )


def _as_manifest(raw: object) -> dict:
    """The manifest as a mapping, or a refusal that says which it was.

    `json.loads` returns whatever the document is. A manifest that is a list
    reached `manifest.get(...)` and raised AttributeError — which `--json`
    printed as a traceback and both paths reported as exit 1, "I checked and
    found problems". The truth is exit 2, "I could not check", and the two are
    not interchangeable to a CI step.
    """
    if not isinstance(raw, dict):
        raise PackError(
            f"{MANIFEST_NAME} is {type(raw).__name__}, not an object",
        )
    files = raw.get("files")
    if files is not None and not isinstance(files, dict):
        raise PackError(
            f"{MANIFEST_NAME}: 'files' is {type(files).__name__}, not an object",
        )
    return raw


def read_manifest(blob: bytes) -> dict[str, Any]:
    """The manifest, as a dict. Raises :class:`PackError` if there is none."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            if MANIFEST_NAME not in set(zf.namelist()):
                raise PackError(f"{MANIFEST_NAME} is missing")
            _guard_sizes(zf)
            return _as_manifest(json.loads(zf.read(MANIFEST_NAME)))
    except PackError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PackError(f"unreadable archive: {exc}") from None


def read_member(blob: bytes, name: str) -> bytes:
    """One file out of the pack. Raises :class:`PackError` if it is not there."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            if name not in set(zf.namelist()):
                raise PackError(f"{name} is not in this pack")
            _guard_sizes(zf)
            return zf.read(name)
    except PackError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PackError(f"unreadable archive: {exc}") from None


def manifest_sha256(blob: bytes) -> str:
    """The sha256 of MANIFEST.json itself.

    THE ONE NUMBER THE PACK CANNOT VOUCH FOR, because a file cannot contain
    its own hash. It has to reach you by a different route than the pack did.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            if MANIFEST_NAME not in set(zf.namelist()):
                raise PackError(f"{MANIFEST_NAME} is missing")
            _guard_sizes(zf)
            return _sha256(zf.read(MANIFEST_NAME))
    except PackError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PackError(f"unreadable archive: {exc}") from None


def verify_pack(
    blob: bytes, expected_manifest_sha256: str | None = None,
) -> tuple[bool, list[str]]:
    """Recompute every hash in the manifest. Returns ``(ok, problems)``.

    Three kinds of problem, kept apart because they mean different things:
    a file the manifest lists and the archive does not hold, a file whose
    contents no longer hash to what was recorded, and a file present in the
    archive that the manifest does not vouch for. The third matters as much as
    the second — an added file is content nobody signed.

    WHAT THIS PROVES ON ITS OWN, AND WHAT IT DOES NOT. The manifest records a
    hash for every other file and none for itself. Recomputing them therefore
    proves the archive is internally CONSISTENT — enough to catch a truncated
    download, a byte flipped in transit, a file swapped or added by somebody
    who did not know the format. It is not proof against forgery, and the
    demonstration is three lines: open the zip, edit a file, write its new
    sha256 into the manifest, repack. Measured against a real pack, that
    verified as OK and exited 0.

    Pass `expected_manifest_sha256` and the chain closes: the manifest fixes
    every file, and you fix the manifest from a source the sender did not
    control. The unforgeability lives in that second channel. An unsigned
    manifest that does not hash itself cannot supply it, and a tool that
    implied otherwise would be selling the false confidence the pack exists to
    avoid.
    """
    problems: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            if MANIFEST_NAME not in names:
                return False, [f"{MANIFEST_NAME} is missing"]
            # Before anything is read: a declared size is an instruction to
            # allocate, and this runs on the machine of somebody checking a
            # file they were sent by a party they do not trust.
            _guard_sizes(zf)
            manifest = _as_manifest(json.loads(zf.read(MANIFEST_NAME)))

            # Before a single hash. Every problem below reads as "somebody
            # changed this", which is the wrong thing to tell a person holding
            # a pack this verifier is simply too old for.
            declared = manifest.get("manifest_version", 1)
            try:
                declared = int(declared)
            except (TypeError, ValueError):
                return False, [
                    f"{MANIFEST_NAME} declares manifest_version {declared!r}, which is not a "
                    "version number",
                ]
            if declared > MANIFEST_VERSION:
                return False, [
                    f"this pack is format version {declared} and this verifier "
                    f"understands {MANIFEST_VERSION} — it was produced by a newer Celmis, so "
                    "upgrade the verifier rather than treating this as a "
                    "failed check",
                ]

            # Before the per-file work: a manifest that is not the one you
            # were promised makes every hash below beside the point, because
            # they would all agree — with each other.
            if expected_manifest_sha256 is not None:
                actual = _sha256(zf.read(MANIFEST_NAME))
                wanted = expected_manifest_sha256.strip().lower()
                if actual != wanted:
                    return False, [
                        f"{MANIFEST_NAME}: sha256 is {actual}, expected {wanted} — this is not the "
                        "manifest you were given the hash for, so the rest of "
                        "the pack proves nothing",
                    ]

            listed = manifest.get("files") or {}
            for name, expected in listed.items():
                if name not in names:
                    problems.append(f"{name}: listed but absent")
                    continue
                if _sha256_member(zf, zf.getinfo(name)) != expected:
                    problems.append(f"{name}: sha256 mismatch")
            for name in sorted(names - set(listed) - {MANIFEST_NAME}):
                problems.append(
                    f"{name}: present but not in the manifest",
                )
    except PackError:
        # Refusals — size limits, a manifest that is not an object — travel to
        # the caller so the CLI can exit 2, "could not check". Returning them
        # as problems would say "I checked and it is wrong", which is a
        # different sentence and the wrong one.
        raise
    except Exception as exc:  # noqa: BLE001
        return False, [f"unreadable archive: {exc}"]
    return (not problems), problems


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "PackError",
    "manifest_sha256",
    "read_manifest",
    "read_member",
    "verify_pack",
]
