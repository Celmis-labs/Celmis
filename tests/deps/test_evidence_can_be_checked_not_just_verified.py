"""An evidence pack lets an auditor re-check a finding, not only its hash.

THE DEFECT. The pack's `MANIFEST.json` carries a sha256 of every file and they
all verify — checked independently with `shasum -a 256`, 6 of 6 matched. That
proves the pack was not ALTERED after generation.

It does not prove any finding was TRUE. Every entry in `findings.json` carried
exactly:

    aliases, ecosystem, fixed_version, id, package, repo, severity, summary,
    version

No source file, no line, no manifest path, and — the one that matters — no
COMMIT. So an auditor holding the pack has nothing to point at in the
repository: they can see the claim, and they cannot go and read the pinned
version for themselves. For a compliance artefact that is the difference
between a hash and evidence.

WHAT IS STILL MISSING, said plainly. The manifest PATH is absent because
`DepFinding` has no column for it — the `manifest` property the SBOM emits is
always empty, and filling it needs a migration. The commit closes most of the
gap: with repo + commit + ecosystem + package + version, a reader checks out
that exact state and reads the manifest themselves.
"""

from __future__ import annotations

import ast
import inspect

REQUIRED = ["commit", "source", "advisory_url", "transitive"]


def _evidence_finding_keys() -> set[str]:
    """The literal keys the evidence findings are built from.

    Read from the source with ast rather than by running a request, so this
    stays a unit test — and so a comment naming a key cannot satisfy it.
    """
    from src.api.routers import deps

    tree = ast.parse(inspect.getsource(deps))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", None) != "append":
            continue
        if getattr(getattr(func, "value", None), "id", None) != "flat_vulns":
            continue
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Dict):
            return {
                k.value for k in arg.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError("flat_vulns.append not found — did the router move?")


def test_the_pack_says_which_code_state_it_describes():
    """Without this an auditor cannot check the claim at all."""
    assert "commit" in _evidence_finding_keys()


def test_the_pack_says_who_found_it():
    """osv, pip-audit and npm-audit are not equally strong evidence, and the
    coverage panel already distinguishes them elsewhere."""
    assert "source" in _evidence_finding_keys()


def test_the_pack_links_the_advisory():
    assert "advisory_url" in _evidence_finding_keys()


def test_the_pack_marks_an_unread_advisory():
    """A finding carried without its full record must not read like one that
    was read — the same distinction the audit itself now makes."""
    assert "detail_unavailable" in _evidence_finding_keys()


def test_the_original_fields_are_all_still_there():
    """Adding context must not have dropped any."""
    keys = _evidence_finding_keys()

    for field in ("id", "package", "version", "ecosystem", "severity",
                  "summary", "fixed_version", "aliases", "repo"):
        assert field in keys, f"lost {field}"


def test_the_commit_is_the_one_the_run_actually_read():
    """This asserted `_commits_for` — the INDEX state — and that turned out to
    be the wrong source: a run auditing a branch stamped its document with
    main's sha, so the post-fix SBOM collided with the pre-fix one. The
    audited commit now wins, with the index state kept only as a fallback for
    runs recorded before it existed."""
    from src.api.routers import deps

    body = ast.unparse(ast.parse(inspect.getsource(deps)))
    assert "evidence_commits = _audited_commits(run," in body
    assert "read_index_states" in body
