"""When the PR head is not in the clone, the PR's code is base + diff, never the
clone's newer HEAD - and a drop resting on newer code is not honoured.

Real throw-away git repositories: the clone holds the PR's base commit and a
NEWER commit on the default branch that defines the very constant the pull
request mistyped. A mock of git would test the mock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.review import head_fetch
from src.review.agents import verifier_evidence as ve
from src.review.agents.base import AgentContext
from src.review.agents.verifier import VerifierAgent
from src.review.code_revision import (
    KIND_BASE_PLUS_DIFF,
    KIND_CLONE_HEAD,
    KIND_HEAD,
    overlay_file,
    resolve_code_revision,
)
from src.review.models import Finding, Hunk, PullRequest
from src.review.structural_constants import find_undefined_constants

MODEL = "<?php\nclass Model {\n    const STATUS_PENDING = 1;\n}\n"
MODEL_NEWER = "<?php\nclass Model {\n    const STATUS_PENDING = 1;\n    const STATUS_PENDNG = 9;\n}\n"
PAGE = "<?php\n$a = 1;\n$b = 2;\n"
ABSENT = "f" * 40


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def world(tmp_path):
    """(root, pr): base commit C0, then a newer C1 on the default branch that
    defines STATUS_PENDNG. The PR (branched at C0) adds a line using it."""
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "Model.php").write_text(MODEL)
    (root / "page.php").write_text(PAGE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "Model.php").write_text(MODEL_NEWER)
    _git(root, "commit", "-q", "-am", "newer")
    hunk = Hunk(file_path="page.php", old_file_path="page.php", old_start=2, old_count=1,
                new_start=2, new_count=2,
                content="@@ -2,1 +2,2 @@\n $a = 1;\n+$c = Model::STATUS_PENDNG;\n")
    pr = PullRequest(provider="github", repo="o/n", number=3, title="t", description="",
                     author="a", base_ref="main", base_sha=base, head_ref="f",
                     head_sha=ABSENT, state="open", hunks=[hunk])
    return root, pr


def _settings(root: Path):
    return SimpleNamespace(repo_path=lambda slug: root)


def _finding() -> Finding:
    return Finding(file_path="page.php", line=3, title="Undefined constant `Model::STATUS_PENDNG`",
                   body="", rule_id="defect.x", agent="defect")


# ─── resolution order ────────────────────────────────────────────────


def test_resolution_order_head_then_base_then_newer_clone_head(world):
    root, pr = world
    head = _git(root, "rev-parse", "HEAD")
    pr.head_sha = head
    assert resolve_code_revision(pr, root).kind == KIND_HEAD
    pr.head_sha = ABSENT
    rev = resolve_code_revision(pr, root)
    assert rev.kind == KIND_BASE_PLUS_DIFF and rev.rev == pr.base_sha and rev.is_pr_code
    pr.base_sha = "e" * 40
    rev = resolve_code_revision(pr, root)
    assert rev.kind == KIND_CLONE_HEAD and not rev.is_pr_code
    assert "NEWER than the pull request" in rev.label
    assert resolve_code_revision(pr, None).kind == "none"


def test_overlay_rebuilds_the_head_text_and_refuses_a_diff_that_does_not_fit(world):
    _root, pr = world
    assert overlay_file(PAGE, pr.hunks) == "<?php\n$a = 1;\n$c = Model::STATUS_PENDNG;\n$b = 2;\n"
    assert overlay_file("<?php\n$a = 9;\n$b = 2;\n", pr.hunks) is None  # context differs
    assert overlay_file(None, [Hunk("n.php", "n.php", 0, 0, 1, 1, "@@ -0,0 +1 @@\n+x\n")]) == "x\n"


# ─── the constant check ──────────────────────────────────────────────


def test_constant_check_uses_base_plus_diff_and_fires_despite_a_newer_definition(world):
    root, pr = world
    out = find_undefined_constants(pr, root)
    assert len(out) == 1 and "STATUS_PENDNG" in out[0].title
    assert "base commit plus this diff" in out[0].body
    assert out[0].suggestion == "Use `STATUS_PENDING`."


def test_without_base_the_newer_head_is_the_last_resort_and_is_labelled(world):
    root, pr = world
    pr.base_sha = "e" * 40
    # The newer HEAD defines it: silent is the known blind spot of this tier.
    assert find_undefined_constants(pr, root) == []


# ─── evidence provenance ─────────────────────────────────────────────


def test_evidence_from_base_plus_diff_is_the_prs_code(world, monkeypatch):
    root, pr = world
    b = ve.gather_evidence([_finding()], pr, app_settings=_settings(root))
    ev = b.items[0]
    assert "STATUS_PENDNG" in ev.window and "(PR base) + this PR's diff" in ev.window_source
    hit = next(h for h in ev.hits if h.symbol == "STATUS_PENDNG")
    # The only STATUS_PENDNG at the PR's code is the line the PR adds; the newer
    # definition in Model.php is not in what the PR sees.
    assert hit.pr_code and all("Model.php" not in blk for blk in hit.blocks)
    assert ev.non_pr_source is False
    assert b.stats["clone"] == "base + diff"


def test_evidence_from_the_newer_head_is_labelled_and_flagged(world):
    root, pr = world
    pr.base_sha = "e" * 40
    b = ve.gather_evidence([_finding()], pr, app_settings=_settings(root))
    ev = b.items[0]
    hit = next(h for h in ev.hits if h.symbol == "STATUS_PENDNG")
    assert not hit.pr_code and "NEWER" in hit.source
    assert any("Model.php" in blk for blk in hit.blocks)
    assert ev.non_pr_source is True
    text = ve.render_evidence(ev)
    assert "NOT THE PULL REQUEST'S CODE" in text


# ─── the veto cannot drop on newer code ──────────────────────────────

REPLY = '{"keep": [], "reasons": {"0": "Model.php defines const STATUS_PENDNG"}}'


def _veto(monkeypatch, root, pr):
    monkeypatch.setattr(ve, "_default_app_settings", lambda: _settings(root))

    class Client:
        def generate(self, **kw):
            return SimpleNamespace(text=REPLY, input_tokens=1, output_tokens=1)

    ctx = AgentContext(pull_request=pr)
    ctx.llm_client = Client()
    f = _finding()
    return f, VerifierAgent()._llm_verify([f], ctx)


def test_a_drop_resting_on_the_newer_head_is_overridden_in_code(world, monkeypatch):
    root, pr = world
    pr.base_sha = "e" * 40
    f, out = _veto(monkeypatch, root, pr)
    assert out.verified == [f] and out.dropped == []
    assert out.evidence["drops_kept_non_pr_evidence"] == 1


def test_a_drop_on_the_prs_own_code_is_still_honoured(world, monkeypatch):
    root, pr = world
    f, out = _veto(monkeypatch, root, pr)
    assert out.verified == [] and [d[0] for d in out.dropped] == [f]


# ─── the stage record ────────────────────────────────────────────────


def test_the_head_fetch_stage_says_which_code_is_read(world, monkeypatch):
    root, pr = world
    monkeypatch.setattr(head_fetch, "_find_clone", lambda p: root)
    monkeypatch.setattr(head_fetch, "_ensure",
                        lambda *a: head_fetch.HeadFetch(False, "unavailable", "no network"))
    res = head_fetch.ensure_head_in_clone(pr)
    assert res.source_kind == KIND_BASE_PLUS_DIFF
    assert "code read from: clone @" in res.sentence and "PR base" in res.sentence
    pr.base_sha = "e" * 40
    res = head_fetch.ensure_head_in_clone(pr)
    assert "NEWER than the pull request" in res.sentence
