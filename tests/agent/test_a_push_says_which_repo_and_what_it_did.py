"""Two things a pushed branch reports about itself, both measured wrong.

`repo_slug: "repo"` — every single-repo session on production reported that,
in the session result, in the `pushes` list the UI renders, and in the push
notification. `AgentWorkspace.__post_init__` derived the slug from
`repo_dir.name`, and a single-repo session clones into `<session>/repo`. The
MULTI-repo path never had the bug: it clones into `repos/<slug>/` and passes
the slug explicitly. One shape worked; the common one did not.

The commit subject — taken from the agent's first sentence, which is written
to a person in a chat window rather than to a git log. Two real subjects from
one day's sessions:

    All 3 tests pass (`3 passed in 0.01s`).
    That failure is pre-existing and unrelated to my change — it's in …

Neither says what the commit does.
"""

from __future__ import annotations

from pathlib import Path

from src.agent.workspace import (
    SUBJECT_MAX_CHARS,
    AgentWorkspace,
    RepoCheckout,
    _clip_subject,
    _subject_line,
)


def _ws(tmp_path: Path, **kw) -> AgentWorkspace:
    return AgentWorkspace(
        session_id="478d671b-75d7-41a3-8e03-7691b6bae3e8",
        repo_dir=tmp_path / "repo",
        home_dir=tmp_path / "home",
        clean_url="https://github.com/acme/worker.git",
        push_url="https://x:t@github.com/acme/worker.git",
        default_branch="main",
        **kw,
    )


# ─── which repo ──────────────────────────────────────────────────────


def test_a_single_repo_session_reports_the_registered_slug(tmp_path):
    ws = _ws(tmp_path, repo_slug="github_acme-worker")
    assert ws.repos[0].slug == "github_acme-worker"


def test_the_directory_name_is_not_the_slug(tmp_path):
    """The exact production symptom. `<session>/repo` is where a single-repo
    session clones, so the directory is called `repo` for every repository
    that ever existed."""
    ws = _ws(tmp_path, repo_slug="github_acme-worker")
    assert ws.repo_dir.name == "repo"
    assert ws.repos[0].slug != "repo"


def test_a_workspace_with_no_slug_still_works(tmp_path):
    """Back-compat: the field is new and something may construct one without
    it. Falling back to the directory name is the old behaviour, which is
    wrong but not a crash."""
    ws = _ws(tmp_path)
    assert ws.repos[0].slug == "repo"


def test_a_multi_repo_workspace_keeps_its_own_slugs(tmp_path):
    """The path that was already right must stay right."""
    checkouts = [
        RepoCheckout(slug="github_acme-worker", path=tmp_path / "repos/w",
                     clean_url="u", push_url="p", default_branch="main"),
        RepoCheckout(slug="github_acme-billing", path=tmp_path / "repos/b",
                     clean_url="u", push_url="p", default_branch="main"),
    ]
    ws = _ws(tmp_path, repo_slug="github_acme-worker", repos=checkouts)
    assert [r.slug for r in ws.repos] == ["github_acme-worker", "github_acme-billing"]


# ─── what it did ─────────────────────────────────────────────────────


def test_the_title_wins_over_the_agents_closing_sentence():
    """Both real, from the same session."""
    assert _subject_line(
        "Fix split_settlement remainder loss",
        "All 3 tests pass (`3 passed in 0.01s`).",
        "478d671b",
    ) == "Fix split_settlement remainder loss"


def test_without_a_title_the_summary_is_still_used():
    """Still better than `Celmis agent session 8ca7b349`, which is what this
    replaced — a line saying an agent was here and nothing else."""
    assert _subject_line("", "Fixed the remainder loss.", "478d671b") \
        == "Fixed the remainder loss."


def test_a_bare_heading_is_skipped_but_a_heading_with_content_is_not():
    """Agents open with `## Summary`. "Summary" is not a subject; "Fix the
    retry backoff" is, and both start with hashes — so the test is on the
    words, not the markup."""
    assert _subject_line("", "## Summary\n\nFixed the remainder loss.", "x") \
        == "Fixed the remainder loss."
    assert _subject_line("", "## Fix the retry backoff\n\nbody", "x") \
        == "Fix the retry backoff"


def test_with_nothing_at_all_it_names_the_session():
    assert _subject_line("", "", "478d671b-75d7") == "Celmis agent session 478d671b"


# ─── the clip ────────────────────────────────────────────────────────


def test_a_long_subject_stops_at_a_word():
    long = ("Fix split_settlement so the parts always sum to the total "
            "exactly, distributing the remainder")
    out = _clip_subject(long)
    assert len(out) <= SUBJECT_MAX_CHARS
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")
    # The give-away for a mid-word cut: the last word of the output is not a
    # whole word of the input. Trailing punctuation is stripped by the clip,
    # so compare against words with theirs stripped too.
    last = out[:-1].split()[-1]
    words = {w.strip(" ,.;:—-") for w in long.split()}
    assert last in words, f"{last!r} is not a whole word of the subject"


def test_a_short_subject_is_untouched():
    assert _clip_subject("Fix the retry backoff") == "Fix the retry backoff"


def test_a_subject_at_exactly_the_limit_is_untouched():
    exact = "x" * SUBJECT_MAX_CHARS
    assert _clip_subject(exact) == exact


def test_one_unbroken_word_is_still_clipped():
    """No space to cut at. Better a hard cut than a 400-character subject."""
    out = _clip_subject("a" * 200)
    assert len(out) <= SUBJECT_MAX_CHARS


def test_newlines_never_reach_the_subject():
    """A subject with a newline in it is not a subject — git reads everything
    after the first blank line as the body."""
    out = _subject_line("Fix the\nretry backoff", "", "x")
    assert "\n" not in out
