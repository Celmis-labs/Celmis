"""`celmis verify` and `celmis show`, over a local zip.

argparse, not typer or click. The whole argument for this package is that it
installs with no transitive surface into an auditor's environment; the platform
depends on both of those and this deliberately depends on neither.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from celmis import __version__
from celmis.verify import (
    MANIFEST_VERSION,
    PackError,
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
        raise PackError("cannot read {0}: {1}".format(path, exc)) from None


def _cmd_verify(args: argparse.Namespace) -> int:
    blob = _read(args.pack)
    ok, problems = verify_pack(blob)

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
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK if ok else EXIT_PROBLEMS

    if ok:
        manifest = read_manifest(blob)
        print("OK — {0} files, {1}, run {2}, generated {3}".format(
            len(manifest.get("files") or {}),
            manifest.get("algorithm") or "sha256",
            manifest.get("run_id"),
            manifest.get("generated_at"),
        ))
        return EXIT_OK

    # One per line, and named. "the pack is invalid" sends somebody to diff a
    # zip by hand; the file that changed is the whole answer.
    print("PROBLEMS — {0}".format(len(problems)), file=sys.stderr)
    for problem in problems:
        print("  {0}".format(problem), file=sys.stderr)
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
        version="celmis {0} (evidence-pack manifest version {1}, sha256)".format(
            __version__, MANIFEST_VERSION,
        ),
    )
    sub = parser.add_subparsers(dest="command")

    verify = sub.add_parser("verify", help="recompute every hash in the manifest")
    verify.add_argument("pack", help="path to an evidence-pack .zip")
    verify.add_argument("--json", action="store_true",
                        help="machine-readable output, for a CI step")
    verify.set_defaults(func=_cmd_verify)

    show = sub.add_parser("show", help="print the manifest")
    show.add_argument("pack", help="path to an evidence-pack .zip")
    show.add_argument("--summary", action="store_true",
                      help="print summary.md from inside the pack instead")
    show.set_defaults(func=_cmd_show)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
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
        print("celmis: {0}".format(exc), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
