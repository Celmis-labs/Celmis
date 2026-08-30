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


class PackError(Exception):
    """The archive could not be read as an evidence pack at all."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(blob: bytes) -> dict[str, Any]:
    """The manifest, as a dict. Raises :class:`PackError` if there is none."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            if MANIFEST_NAME not in set(zf.namelist()):
                raise PackError(f"{MANIFEST_NAME} is missing")
            return json.loads(zf.read(MANIFEST_NAME))
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
            manifest = json.loads(zf.read(MANIFEST_NAME))

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
                if _sha256(zf.read(name)) != expected:
                    problems.append(f"{name}: sha256 mismatch")
            for name in sorted(names - set(listed) - {MANIFEST_NAME}):
                problems.append(
                    f"{name}: present but not in the manifest",
                )
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
