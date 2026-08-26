"""Seven quieter defects, all of the same family: something did not happen and
nothing said so.

A re-review added a second set of comments instead of replacing the first. A
GitLab merge request could be locked out of review by an event that was
correctly ignored. A recompute published a search payload with the provenance
missing. A queued job that was dropped answered "Queued 2 document(s)".

None of them raises, and none of them is visible from the outside — which is
why they were still here after a green suite and a production deployment.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
WEB = ROOT / "web"


# ─── inline comments accumulated ─────────────────────────────────────


def test_an_inline_comment_carries_the_marker():
    """Only the summary had one, so `replace_on_synchronize` deleted the
    summary and left every inline comment behind. Bitbucket fires
    pullrequest:updated on every push, so a branch pushed five times collected
    five full sets — up to max_inline_comments each."""
    from src.review import models
    from src.review.models import Finding
    from src.review.providers.base import _format_finding_body

    sev = getattr(models, "FindingSeverity", None) or models.Level
    f = Finding(agent="security", file_path="a.py", line=1,
                severity=list(sev)[0], title="T", body="B", rule_id="r")
    assert "<!-- x -->" in _format_finding_body(f, "<!-- x -->")
    # …and stays absent when no marker is asked for, so nothing leaks into a
    # provider that has not opted in.
    assert "<!--" not in _format_finding_body(f)


def test_every_provider_stamps_its_inline_comments():
    for name in ("github.py", "gitlab.py", "bitbucket.py"):
        source = (SRC / "review" / "providers" / name).read_text(encoding="utf-8")
        assert "_format_finding_body(finding, settings.comment_marker)" in source, (
            f"{name} posts unmarked inline comments, which the next review "
            "cannot find to delete"
        )


def test_the_cleanup_removes_all_of_them_not_just_the_summary():
    """Bitbucket overrides the lookup instead of inheriting the delete-nothing
    default below. The full delete/keep behaviour — author check, fail-closed
    listing, reply protection — is pinned end-to-end against a fake server in
    test_a_rerun_does_not_double_the_comments.py."""
    from src.review.providers.base import PullRequestProvider
    from src.review.providers.bitbucket import BitbucketPRProvider

    assert (
        BitbucketPRProvider.find_marked_comment_ids
        is not PullRequestProvider.find_marked_comment_ids
    )


def test_a_provider_without_the_lookup_keeps_todays_behaviour():
    """The default returns nothing, so a provider that has not implemented it
    deletes nothing rather than deleting the wrong things."""
    from src.review.providers.base import PullRequestProvider

    assert PullRequestProvider.find_marked_comment_ids(
        object(), "o/r", 1, "MARKER") == []


# ─── a GitLab merge request could be locked out ──────────────────────


def test_the_gitlab_key_is_claimed_only_by_an_actionable_event():
    """GitLab has no delivery id, so the key is synthesised from
    (project, iid, head sha) — it identifies a COMMIT, not an event. Claiming
    it before the filters let the first arrival burn it for 24 hours: a draft
    marked ready, a closed MR reopened, an approval on an unchanged head are
    each correctly ignored, and each took the commit's only chance at a review
    with it."""
    source = (SRC / "review" / "webhook.py").read_text(encoding="utf-8")
    start = source.index("def _gitlab") if "def _gitlab" in source else 0
    body = source[start:]
    extract = body.index("_extract_gitlab_mr(payload)")
    dedup = body.index("dedup.is_duplicate(delivery_key)")
    assert extract < dedup, (
        "the dedup key is registered before the trigger filter again"
    )
    draft = body.index('reason": "draft MR')
    assert draft < dedup, "a draft MR still burns the key for its own commit"


# ─── the search payload lost what the recompute published ────────────


def test_a_recompute_keeps_the_provenance():
    """The .md on disk kept it; the re-embed rebuilt the metadata from scratch
    and dropped it, so the SEARCH payload lost the provenance block — under a
    feature whose whole point is that a reader can tell where a document came
    from."""
    from src.vault.writer import VaultWriter

    meta = VaultWriter._metadata_from_dict(
        {"type": "module", "provenance": {"generated_by": "celmis"}}, "acme/api")
    assert meta.as_dict().get("provenance") == {"generated_by": "celmis"}


def test_a_recompute_keeps_the_per_type_extras():
    """An integration note's `service`, a feature's owner — silently discarded
    because NoteMetadata does not model them by name."""
    from src.vault.writer import VaultWriter

    meta = VaultWriter._metadata_from_dict(
        {"type": "integration", "service": "billing"}, "acme/api")
    assert meta.as_dict().get("service") == "billing"


def test_a_recompute_keeps_the_computed_block():
    """Every computed field except cross_refs was dropped — the fields the call
    exists to publish."""
    from src.vault.writer import VaultWriter

    meta = VaultWriter._metadata_from_dict(
        {"type": "module", "computed": {"cross_refs": ["a"],
                                        "callers_modules": ["b"]}}, "acme/api")
    assert meta.computed and meta.computed.get("callers_modules") == ["b"]


# ─── a dropped job answered "queued" ─────────────────────────────────


def test_the_regenerate_key_carries_the_workspace_and_the_whole_note_set():
    """It carried neither. Without the workspace one tenant's pending job
    swallowed another's request; and `[:120]` made two DIFFERENT note sets
    collide whenever their sorted join shared a 120-character prefix, which one
    long path plus a short one is enough to produce."""
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    assert 'f"regenerate:{workspace_id}:{slug}:"' in docs
    assert "hashlib.sha256" in docs
    # Comments stripped: the comment explaining why the truncation is gone
    # NAMES the truncation, and grepping the raw file finds the word in the
    # sentence about its own absence. This repository has now walked into that
    # exact trap five times.
    code = "\n".join(
        line.split("#", 1)[0] for line in docs.splitlines()
        if not line.strip().startswith("#")
    )
    assert "[:120]" not in code


def test_the_vault_key_carries_the_workspace_too():
    repos = (SRC / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    assert 'f"generate_vault:{workspace_id}:{cfg.repo_slug}"' in repos


def test_a_deduped_request_says_nothing_was_started():
    """`enqueue` returns None on a dedup hit and both endpoints answered
    "Queued N document(s)" regardless, so pressing a button twice looked like
    it worked twice."""
    docs = (SRC / "api" / "routers" / "docs.py").read_text(encoding="utf-8")
    assert "if job_id is None:" in docs
    assert '"queued": False' in docs
    repos = (SRC / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    assert "if job_id is None:" in repos


def test_the_ui_does_not_celebrate_a_dropped_job():
    docs_page = (WEB / "app" / "(app)" / "docs" / "page.tsx").read_text(encoding="utf-8")
    assert "r.queued === false" in docs_page
    repos_page = (WEB / "app" / "(app)" / "repositories" / "page.tsx").read_text(encoding="utf-8")
    assert "r.queued === false" in repos_page
    # And the dialog stays open, so the second press is not identical to the
    # first from the user's side.
    idx = repos_page.index("r.queued === false")
    assert "return;" in repos_page[idx:idx + 200]


# ─── two smaller ones with the same shape ────────────────────────────


def test_a_rejected_attachment_does_not_orphan_the_accepted_ones():
    """`setStagingId` sat after the upload loop, inside the try: one rejected
    file threw past it and discarded the id the SUCCESSFUL uploads were minted
    under. Their badges stayed on screen, createSession sent staging_id: null,
    and the agent never saw them."""
    page = (WEB / "app" / "(app)" / "claude" / "page.tsx").read_text(encoding="utf-8")
    loop = page[page.index("for (const f of files)"):]
    body = loop[:loop.index("} catch")]
    assert "setStagingId(id);" in body, (
        "the staging id is recorded after the loop again, so one rejected file "
        "discards the uploads that succeeded"
    )


def test_onboarding_does_not_tick_a_step_that_read_nothing():
    """The auditor writes status done even when every repository failed to
    clone. Step 7's own card says so — while the header ticked complete and
    counted toward allDone above it."""
    page = (WEB / "app" / "(app)" / "onboarding" / "page.tsx").read_text(encoding="utf-8")
    assert "repos_scanned" in page
    assert "proofScanned" in page


def test_the_index_all_toast_is_not_a_raw_key():
    """`t("repos.allIndexed")` had no entry in any locale, so the branch
    rendered the key itself as the toast — reached when every repo already has
    a graph, which is exactly production's state."""
    import json

    messages = WEB / "lib" / "i18n" / "messages"
    for path in sorted(messages.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "repos.allIndexed" in data, f"{path.stem} would show the raw key"
