"""`celmis verify` and `celmis show`, over a local zip.

argparse, not typer or click. The whole argument for this package is that it
installs with no transitive surface into an auditor's environment; the platform
depends on both of those and this deliberately depends on neither.
"""

from __future__ import annotations

import argparse
import json
import sys

from celmis import __version__
from celmis.verify import (
    MANIFEST_VERSION,
    PackError,
    manifest_sha256,
    read_manifest,
    read_member,
    verify_pack,
)

#: Exit codes. 1 is "I checked and found problems"; 2 is "I could not check".
#: Collapsing them would make a missing file and an altered file the same
#: event to a CI step, and they are not.
EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2

WHAT_THIS_IS = """\
celmis — the offline verifier for a Celmis evidence pack.

THIS IS NOT THE CELMIS PLATFORM. The platform is six services under docker
compose and does not install with pip. To run it:

    git clone https://github.com/Celmis-labs/Celmis
    cd Celmis
    cp .env.example .env
    docker compose up -d

This package takes an evidence-pack archive and recomputes the sha256 of every
file its manifest lists. No dependencies, no network calls, and it never
contacts a Celmis installation — the person checking a pack is usually not the
operator who produced it.
"""


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise PackError(f"cannot read {path}: {exc}") from None


def _expected_hash(raw: str | None) -> str | None:
    """Validate the supplied hash as INPUT, before it becomes a verdict.

    A truncated paste is a typing mistake, and letting it fall through to the
    comparison would report it as `sha256 does not match` — telling somebody
    their pack was altered when what actually happened is that they lost eight
    characters to a line wrap. Exit 2 says "I could not check"; exit 1 says
    "I checked and it is wrong". They are different sentences.
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise PackError(
            "--manifest-sha256 takes 64 hex characters; got {} "
            "({!r}). Check the value you were given rather than the "
            "pack.".format(len(value), raw[:24] + ("…" if len(raw) > 24 else "")),
        )
    return value


def _cmd_verify(args: argparse.Namespace) -> int:
    blob = _read(args.pack)
    expected = _expected_hash(args.manifest_sha256)
    ok, problems = verify_pack(blob, expected)

    if args.json:
        payload = {
            "ok": ok,
            "problems": problems,
            "files": 0,
            "run_id": None,
            "generated_at": None,
            "algorithm": None,
            "manifest_version": None,
        }
        try:
            manifest = read_manifest(blob)
        except PackError:
            manifest = {}
        payload["files"] = len(manifest.get("files") or {})
        payload["run_id"] = manifest.get("run_id")
        payload["generated_at"] = manifest.get("generated_at")
        payload["algorithm"] = manifest.get("algorithm")
        payload["manifest_version"] = manifest.get("manifest_version", 1) if manifest else None
        try:
            payload["manifest_sha256"] = manifest_sha256(blob)
        except PackError:
            payload["manifest_sha256"] = None
        payload["manifest_sha256_checked"] = expected is not None
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK if ok else EXIT_PROBLEMS

    if ok:
        manifest = read_manifest(blob)
        print("OK — {} files, {}, run {}, generated {}".format(
            len(manifest.get("files") or {}),
            manifest.get("algorithm") or "sha256",
            manifest.get("run_id"),
            manifest.get("generated_at"),
        ))
        # THE HASH THE PACK CANNOT CARRY. Printed every time, because the line
        # above on its own means "internally consistent" and a reader will
        # take it to mean "genuine". Somebody who edits a file and updates its
        # entry in MANIFEST.json passes that check; what they cannot do is
        # make the manifest hash to a value you got from somewhere else.
        digest = manifest_sha256(blob)
        if expected is not None:
            print(f"MANIFEST.json sha256 {digest} — matches the value you supplied.")
        else:
            print(f"MANIFEST.json sha256 {digest}")
            print("  Compare that with a copy obtained separately from the pack "
                  "(--manifest-sha256).")
            print("  Without it this proves the archive is internally "
                  "consistent, not that it is the archive you were sent.")
        return EXIT_OK

    # One per line, and named. "the pack is invalid" sends somebody to diff a
    # zip by hand; the file that changed is the whole answer.
    print(f"PROBLEMS — {len(problems)}", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return EXIT_PROBLEMS


def _cmd_show(args: argparse.Namespace) -> int:
    blob = _read(args.pack)
    if args.summary:
        sys.stdout.write(read_member(blob, "summary.md").decode("utf-8", "replace"))
        return EXIT_OK
    print(json.dumps(read_manifest(blob), indent=2, sort_keys=True))
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="celmis",
        description="Offline verifier for a Celmis evidence pack.",
        epilog="A pack is exported from a running installation at "
               "GET /api/deps/{run_id}/evidence",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"celmis {__version__} (evidence-pack manifest version {MANIFEST_VERSION}, sha256)",
    )
    sub = parser.add_subparsers(dest="command")

    verify = sub.add_parser("verify", help="recompute every hash in the manifest")
    verify.add_argument("pack", help="path to an evidence-pack .zip")
    verify.add_argument("--json", action="store_true",
                        help="machine-readable output, for a CI step")
    verify.add_argument("--manifest-sha256", metavar="HEX", default=None,
                        help="the sha256 of MANIFEST.json, obtained separately "
                             "from the pack; without it this checks internal "
                             "consistency only")
    verify.set_defaults(func=_cmd_verify)

    show = sub.add_parser("show", help="print the manifest")
    show.add_argument("pack", help="path to an evidence-pack .zip")
    show.add_argument("--summary", action="store_true",
                      help="print summary.md from inside the pack instead")
    show.set_defaults(func=_cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    # No arguments: say what this is and what it is not, THEN the help. A user
    # who typed `pip install celmis` expecting the platform has to find that
    # out here rather than after a compose file that does not exist.
    if getattr(args, "command", None) is None:
        print(WHAT_THIS_IS)
        parser.print_help()
        return EXIT_OK

    try:
        return int(args.func(args))
    except PackError as exc:
        print(f"celmis: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
