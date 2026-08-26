"""An SBOM identifies the code it describes.

THE DEFECT. `build_sbom` derives `serialNumber` from (repo_slug, commit) —
deliberately, so that two exports of the same commit are one document and two
exports of different commits are two. Not one of the three call sites in the
deps router ever passed a commit, so every export for a repo carried a
byte-identical serial and `metadata.component.version` read "unknown".

Proven by running two audits of the same repository and diffing the output:
identical `urn:uuid:…`, both times, with `celmis:commit` empty in both.

A consumer that de-duplicates by serialNumber — standard CycloneDX practice,
and the reason the field exists — would treat the SBOM published AFTER a
vulnerability was fixed as the one published before it. The function's own
docstring says it exists to prevent that.
"""

from __future__ import annotations

from src.deps.sbom import build_sbom

DEPS = [{"ecosystem": "PyPI", "package": "requests", "version": "2.25.1"}]


def sbom(commit: str = ""):
    return build_sbom(repo_slug="github_acme-payments", deps=DEPS, commit=commit)


def test_two_commits_are_two_documents():
    a = sbom("aaaaaaaaaaaa1111")
    b = sbom("bbbbbbbbbbbb2222")

    assert a["serialNumber"] != b["serialNumber"]


def test_the_same_commit_is_the_same_document():
    """The other half of the contract: regenerating an unchanged state must not
    manufacture a new identity."""
    assert sbom("aaaaaaaaaaaa1111")["serialNumber"] == sbom("aaaaaaaaaaaa1111")["serialNumber"]


def test_the_commit_is_recorded_where_a_reader_looks():
    doc = sbom("aaaaaaaaaaaa1111")

    props = {p["name"]: p["value"] for p in doc["metadata"]["properties"]}
    assert props["celmis:commit"] == "aaaaaaaaaaaa1111"
    assert doc["metadata"]["component"]["version"] == "aaaaaaaaaaaa"


def test_no_commit_still_produces_a_valid_document():
    """An SBOM with an unknown commit is what shipped for months. A lookup that
    fails must degrade to it, not refuse the export."""
    doc = sbom("")

    assert doc["serialNumber"].startswith("urn:uuid:")
    assert doc["metadata"]["component"]["version"] == "unknown"


def test_every_sbom_call_site_passes_a_commit():
    """The bug was never in `build_sbom` — it was in the three callers that
    took the parameter default. Reads the router source with comments stripped
    so a comment mentioning the parameter cannot satisfy it."""
    import ast
    import inspect

    from src.api.routers import deps as deps_router

    tree = ast.parse(inspect.getsource(deps_router))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "build_sbom"
    ]

    assert calls, "no build_sbom call found — did the router move?"
    for call in calls:
        kwargs = {k.arg for k in call.keywords}
        assert "commit" in kwargs, f"build_sbom call at line {call.lineno} omits commit"


# ─── the commit must be the one that was READ ────────────────────────


def test_the_run_supplies_the_commit_it_audited():
    """`_commits_for` answers a different question — what the knowledge graph
    was last built from — and using it here defeated the whole fix.

    Measured on production: a run audited branch `celmis-agent/da972e2f` and
    correctly reported its contents (7 packages at fixed versions, 0
    vulnerabilities), then stamped the document with MAIN's sha. The result
    was two CycloneDX files with an identical `serialNumber`, identical
    `metadata.component.version` and contradictory contents — the pre-fix one
    with 38 vulnerabilities and the post-fix one with none. A consumer
    de-duplicating by serial, which is standard practice and the reason the
    field exists, sees one document.
    """
    from types import SimpleNamespace as NS

    from src.api.routers.deps import _audited_commits

    run = NS(summary={"audited_commits": {"acme-api": "b" * 40}})

    assert _audited_commits(run, ["acme-api"]) == {"acme-api": "b" * 40}


def test_a_run_without_the_field_falls_back(monkeypatch):
    """Runs recorded before `audited_commits` existed must not lose the commit
    entirely — the old source is worse, not useless."""
    from types import SimpleNamespace as NS

    from src.api.routers import deps

    monkeypatch.setattr(deps, "_commits_for", lambda slugs: {"acme-api": "a" * 40})
    run = NS(summary={})

    assert deps._audited_commits(run, ["acme-api"]) == {"acme-api": "a" * 40}


def test_an_empty_audited_sha_does_not_mask_the_fallback(monkeypatch):
    """`_head_sha` returns "" on failure, and an empty string must not be
    taken as an answer."""
    from types import SimpleNamespace as NS

    from src.api.routers import deps

    monkeypatch.setattr(deps, "_commits_for", lambda slugs: {"acme-api": "a" * 40})
    run = NS(summary={"audited_commits": {"acme-api": ""}})

    assert deps._audited_commits(run, ["acme-api"]) == {"acme-api": "a" * 40}


def test_every_sbom_call_site_uses_the_audited_commit():
    """The bug was never in `build_sbom` — it was in what the callers passed."""
    import ast
    import inspect

    from src.api.routers import deps as deps_router

    body = ast.unparse(ast.parse(inspect.getsource(deps_router)))
    assert body.count("_audited_commits(run,") >= 3


def test_the_auditor_records_what_it_read():
    """The other side of the system: the sha has to be captured where the
    clone is, not inferred later."""
    import ast
    import inspect

    from src.deps import auditor

    body = ast.unparse(ast.parse(inspect.getsource(auditor)))
    assert "audited_commits[cfg.repo_slug] = _head_sha(repo_path)" in body
    assert "'audited_commits'" in body
