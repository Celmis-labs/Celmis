#!/usr/bin/env python3
""":latest belongs to the newest release, not to whatever was built last.

`release.yml` used to push `:latest` beside the version tag on every build,
with no condition. Rebuilding an old tag therefore repointed `:latest` at old
code, silently — the build log is identical either way, and the only way to
find out is to pull the image.

That matters here more than the usual "stale tag" annoyance: `.env.example`
ships `CELMIS_TAG=latest` and docker-compose.yml falls back to `latest` when
it is unset, so `:latest` is exactly what a first-time installation pulls.

Called from the workflow, which has the tag being built and the list of tags
that exist:

    git ls-remote --tags --refs origin \
      | sed 's|.*refs/tags/||' \
      | python3 scripts/should_move_latest.py "$tag"

Prints `true` or `false`, and exits 0 either way — `false` is an answer, not a
failure, and a non-zero exit under `set -e` would abort a release that should
still publish its own version tag.
"""

from __future__ import annotations

import re
import sys

#: A release, and nothing else. `v1.2.3-rc1` is deliberately excluded: latest
#: is what somebody gets when they have expressed no opinion, and a release
#: candidate is an opinion. `+build` metadata is excluded for the same reason —
#: two tags differing only in build metadata have no defined order, so
#: "is this the newest" has no answer for them.
_RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse(tag: str) -> tuple[int, int, int] | None:
    """The version a tag names, or None if it does not name one."""
    m = _RELEASE.match((tag or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def should_move(tag: str, all_tags) -> bool:
    """True when `tag` is a release and nothing published is newer.

    Compared as integer triples, never as text: `v0.10.0` is newer than
    `v0.9.0` and sorts before it as a string. That is the mistake this
    function exists to not make, and the one every single-digit test suite
    fails to catch.

    `>=` rather than `>` against itself: rebuilding the current newest release
    should still leave `:latest` pointing at it.
    """
    mine = parse(tag)
    if mine is None:
        return False

    others = [v for v in (parse(t) for t in (all_tags or [])) if v is not None]

    # FAIL CLOSED ON A LIST THAT CANNOT BE RIGHT. The tag being built always
    # exists on the remote — a tag push runs the workflow after the push, and
    # a manual dispatch names a tag that already exists — so a list without it
    # is a list we failed to read, not a repository without releases.
    #
    # Found by simulating the step rather than by reading it: the first
    # version of this function said "no other tags, so I must be the newest",
    # and a local run with an unreachable remote reported that v0.1.0 should
    # move :latest. An empty read moving :latest is the same defect this file
    # exists to fix, wearing the fix's name.
    if mine not in others:
        return False

    return all(mine >= other for other in others)


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else ""
    tags = [line for line in sys.stdin.read().splitlines()] if not sys.stdin.isatty() else []
    print("true" if should_move(tag, tags) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
