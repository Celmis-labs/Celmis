"""Automatic reviewer assignment has never assigned anybody.

`assign_reviewers_by_ownership` took one `repo` argument and used it as two
different addresses:

  * the provider's REST path — "owner/name" — which is correct, and
  * the key of the ownership snapshot, which `compute_ownership` writes under
    the local indexed slug the indexer used ("github_owner-name").

So `lookup_owner("acme/api", f)` searched for a snapshot stored under
"github_acme-api", found nothing for every changed file, and the function
returned `{"status": "noop", "reason": "no ownership snapshot"}` on every
review that had one.

The reason string named the missing thing, and it was not missing — which is
the shape that makes this class of defect survive: the failure explains itself
with a sentence that is true of the address it looked at and false of the
repository.
"""

from __future__ import annotations

import pytest

from src.review import reviewer_assignment as ra

FULL = "acme/api"
INDEXED = "github_acme-api"
FILES = ["src/billing.py", "src/api/handler.py"]


@pytest.fixture
def snapshot(monkeypatch):
    """An ownership snapshot that exists only under the indexed slug."""
    seen: list[str] = []

    def lookup_owner(repo_slug, path):
        seen.append(repo_slug)
        if repo_slug != INDEXED:
            return None
        return {"primary_owner": "dana@example.com"}

    monkeypatch.setattr("src.ownership.builder.lookup_owner", lookup_owner)
    return seen


@pytest.fixture
def assigned(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ra, "_assign_github",
                        lambda **kw: calls.append(kw) or {"status": "ok",
                                                          "requested": ["dana@example.com"]})
    return calls


def test_the_snapshot_is_read_under_the_indexed_slug(snapshot, assigned):
    ra.assign_reviewers_by_ownership(
        provider="github", repo=FULL, repo_slug=INDEXED, pr_number=1,
        changed_files=FILES, author="a", user_id="u",
    )

    assert set(snapshot) == {INDEXED}


def test_an_owner_is_actually_found(snapshot, assigned):
    out = ra.assign_reviewers_by_ownership(
        provider="github", repo=FULL, repo_slug=INDEXED, pr_number=1,
        changed_files=FILES, author="a", user_id="u",
    )

    assert out["status"] != "noop", out.get("reason")
    assert assigned and assigned[0]["candidates"] == [("dana@example.com", 2)]


def test_the_provider_still_gets_the_path_it_needs(snapshot, assigned):
    ra.assign_reviewers_by_ownership(
        provider="github", repo=FULL, repo_slug=INDEXED, pr_number=1,
        changed_files=FILES, author="a", user_id="u",
    )

    assert assigned[0]["repo"] == FULL, "the REST call needs owner/name"


def test_without_the_slug_it_falls_back_to_the_old_behaviour(snapshot, assigned):
    """Callers that pass only `repo` keep working — and keep finding nothing,
    which is what makes the parameter worth having."""
    out = ra.assign_reviewers_by_ownership(
        provider="github", repo=FULL, pr_number=1,
        changed_files=FILES, author="a", user_id="u",
    )

    assert out["status"] == "noop"
    assert set(snapshot) == {FULL}


def test_the_orchestrator_passes_the_local_slug():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("src/review/orchestrator.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "assign_reviewers_by_ownership"]
    assert calls, "the call is gone"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert "repo_slug" in kw, "the snapshot key is not passed"
        assert isinstance(kw["repo_slug"], ast.Attribute)
        assert kw["repo_slug"].attr == "local_slug"
