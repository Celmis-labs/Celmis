# Changelog

This file begins on **19 August 2026**, at the state the repository was in
that day. It does not reconstruct what came before, and that is a fact about
the repository rather than a shortcut: this tree starts at a single root
commit. Development happened privately before it and is not published.
[`PROVENANCE.md`](PROVENANCE.md) states the licence position and the origin of
the code.

A changelog written backwards from a squashed tree would be fiction. So the
first entry below is the first change made *after* the rebuild, and everything
older is described in one line as the starting state.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/spec/v2.0.0.html). The version
number lives in exactly one file — `src/__init__.py` — and everything else
derives it from there.

---

## [Unreleased]

Nothing has been tagged or released. The version has read `0.1.0` since the
root commit; it is a placeholder, not a shipped release, and this section is
where the first real one will be cut from.

### Fixed

- **The container image builds on arm64.** The runtime stage fetched
  `osv-scanner_linux_amd64` by name, so on the arm64 hosts both deployment
  guides recommend — `docs/HETZNER.md` picks a Hetzner CAX21, `docs/ORACLE_CICD.md`
  builds natively on an Oracle Ampere A1 — the download succeeded, the checksum
  matched, and the `--version` gate immediately after it failed with `exec
  format error`. The documented setup could not build at all. The artifact name
  and its expected digest are now selected from BuildKit's `TARGETARCH`.

### Changed

- **Pinned binaries are verified per architecture, and an unknown architecture
  aborts the build.** `amd64` and `arm64` each carry their own SHA-256; any
  other `TARGETARCH` — including the empty value the legacy non-BuildKit builder
  supplies — stops the build with a message naming the platform, instead of
  falling through to amd64. Guessing is what produced an image that could not
  run its own scanner.

- **`uv` is pinned to 0.12.5 and installed from a checksummed release
  artifact.** The builder previously ran
  `curl -LsSf https://astral.sh/uv/install.sh | sh`, which was unpinned (each
  rebuild silently adopted whatever uv was current that day) and unverified
  (the build executed whatever that URL returned). The second half is the
  same finding this project's own dependency scanner raises against npm
  packages that run install scripts; it is not a rule we can enforce outward
  and not inward. Digests are Astral's published `.sha256` files, checked
  against a local download of both tarballs.

- **One version, one place.** `0.1.0` was written independently in
  `pyproject.toml`, `src/__init__.py`, and two FastAPI constructors.
  `pyproject.toml` now declares `dynamic = ["version"]` and reads
  `src.__version__`, which setuptools resolves by parsing the file rather than
  importing it. `src/__init__.py` was chosen as the source because it is the
  copy readable without an install — `importlib.metadata.version()` answers
  only after one, and returns `"unknown"` otherwise, which is how every
  generated document once ended up stamped `version: unknown`
  (`src/vault/provenance.py`).

  The resulting value is unchanged: `0.1.0` before and after. Distribution
  metadata still carries it, so the vault's provenance stamp keeps working.

### Added

- This file.

### Known

- Two FastAPI applications still hardcode the version string in their OpenAPI
  documents: `src/api/main.py` (`Celmis API`) and `src/review/webhook.py`
  (`code-analyzer review webhook`). Both need `from src import __version__`;
  neither was changed here, because `src/` was outside this change's scope.
- `web/package.json` carries its own `"version": "0.1.0"`. It is an npm
  package version for a private Next.js app and is deliberately not coupled to
  the Python distribution — coupling them needs a build step, and nothing reads
  it today.
- `docker-compose.yml` still passes `CLAUDE_CODE_VERSION` as a build arg to the
  `api` service. The `ARG` it fed was removed when the Claude Code CLI install
  was dropped from the image, so BuildKit now warns that the argument is
  unconsumed. Harmless, but it is a stale line.
