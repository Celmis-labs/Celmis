"""Target branches take patterns: names, globs and `!` exclusions.

Kodus accepts "staging, !master, !main". The gate here compared the base
branch against the list for equality, so `release/*` matched nothing and
`!main` was a branch literally called "!main". Pinned here:

  * the matcher: exact names still match exactly, `release/*` matches its
    children, an exclusion wins over any include, and a list of exclusions
    only means "every branch except those";
  * the orchestrator gate skips and passes with sentences that name the
    deciding pattern;
  * the webhook's early draft skip records a draft into an excluded branch
    as a branch mismatch, not "reviewed once it is marked ready";
  * both settings layers refuse an entry that can never match.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from src.review.branch_patterns import (
    branch_targeted,
    clean_patterns,
    match_branch,
    pass_sentence,
    pattern_error,
    skip_sentence,
    split_patterns,
)
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _Agent,
    _orch,
    _Provider,
    _stage,
    bound,
    no_ledger,
    queue,
    store,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (  # noqa: F401
    _pr,
    env,
)

# ─── the matcher ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("branch", "patterns", "expected"), [
    # No list: everything.
    ("main", [], True),
    ("main", None, True),
    # Exact names, as the gate always did.
    ("main", ["main"], True),
    ("main-old", ["main"], False),
    ("feature/main", ["main"], False),
    # Globs.
    ("release/1.2", ["release/*"], True),
    ("release/1.2/hotfix", ["release/*"], True),
    ("releases/1.2", ["release/*"], False),
    ("hotfix-12", ["main", "hotfix-*"], True),
    # Case-sensitive, like git.
    ("Main", ["main"], False),
    # Negations only: every branch except those.
    ("staging", ["!master", "!main"], True),
    ("main", ["!master", "!main"], False),
    ("master", ["!master", "!main"], False),
    # Kodus's example, verbatim.
    ("staging", ["staging", "!master", "!main"], True),
    ("develop", ["staging", "!master", "!main"], False),
    ("main", ["staging", "!master", "!main"], False),
    # Exclusion wins over a matching include.
    ("release/old", ["release/*", "!release/old"], False),
    ("release/new", ["release/*", "!release/old"], True),
    ("release/legacy-1", ["release/*", "!release/legacy-*"], False),
    # Whitespace and duplicates are noise.
    ("main", ["  main ", "main"], True),
])
def test_the_matcher(branch, patterns, expected):
    assert branch_targeted(branch, patterns) is expected


def test_an_unknown_base_branch_is_reviewed_as_before():
    """The gate has always reviewed a PR whose base it could not read."""
    assert match_branch("", ["main"]).reason == "unknown"
    assert branch_targeted(None, ["!main"]) is True


def test_the_deciding_pattern_is_named():
    assert match_branch("main", ["release/*", "!main"]).pattern == "!main"
    assert match_branch("release/2", ["main", "release/*"]).pattern == "release/*"
    assert match_branch("dev", ["!main"]).reason == "not_excluded"
    assert match_branch("dev", ["main"]).reason == "unmatched"


def test_split_and_clean():
    assert clean_patterns([" main", "", "main", "!dev "]) == ["main", "!dev"]
    assert split_patterns(["main", "!dev", "release/*"]) == (
        ["main", "release/*"], ["dev"])


@pytest.mark.parametrize("entry", ["!", " ! ", "!!main", "my branch", "!my branch"])
def test_an_entry_that_can_never_match_is_refused(entry):
    assert pattern_error(entry)


@pytest.mark.parametrize("entry", ["main", "!main", "release/*", "!release/old-*",
                                   "feature/[a-z]*"])
def test_a_real_entry_is_accepted(entry):
    assert pattern_error(entry) is None


# ─── the sentences ───────────────────────────────────────────────────


def test_the_skip_sentence_names_the_exclusion():
    s = skip_sentence("main", ["staging", "!master", "!main"])
    assert s.startswith("Branch mismatch: target branch 'main' is excluded by '!main'")
    assert "['staging', '!master', '!main']" in s


def test_the_skip_sentence_for_no_include_keeps_its_old_words():
    assert skip_sentence("master", ["main", "release/*"]) == (
        "Branch mismatch: target branch 'master' does not match configured "
        "patterns ['main', 'release/*'].")


def test_the_pass_sentences():
    assert "matches configured patterns ['main', 'release/*'] (via 'release/*')" in (
        pass_sentence("release/9", ["main", "release/*"]))
    assert pass_sentence("main", ["main"]) == (
        "Target branch 'main' matches configured patterns ['main'].")
    assert "is not excluded" in pass_sentence("dev", ["!main"])
    assert "No target-branch restriction" in pass_sentence("dev", [])


# ─── the orchestrator gate ───────────────────────────────────────────


def _gate(monkeypatch, base: str, patterns: list[str]) -> dict:
    from src.review.stages import StageRecorder

    pr = _pr()
    pr.base_ref = base
    orch = _orch(monkeypatch, [_Agent("defect")],
                 policy={**POLICY, "target_branches": patterns})
    rec = StageRecorder()
    orch.review("github", "o/r", 1, provider=_Provider(pr), stages=rec,
                post_comments=False)
    return _stage(rec, "gate_target_branch")


def test_the_gate_skips_an_excluded_branch(env, monkeypatch):  # noqa: F811
    gate = _gate(monkeypatch, "main", ["!master", "!main"])
    assert gate["status"] == "skipped"
    assert "is excluded by '!main'" in gate["reason"]


def test_the_gate_reviews_a_branch_the_negations_leave_in(env, monkeypatch):  # noqa: F811
    gate = _gate(monkeypatch, "staging", ["!master", "!main"])
    assert gate["status"] == "success"
    assert "is not excluded" in gate["reason"]


def test_the_gate_reviews_a_glob_match(env, monkeypatch):  # noqa: F811
    gate = _gate(monkeypatch, "release/2.3", ["main", "release/*"])
    assert gate["status"] == "success"
    assert "(via 'release/*')" in gate["reason"]


def test_exclusion_wins_at_the_gate(env, monkeypatch):  # noqa: F811
    gate = _gate(monkeypatch, "release/old", ["release/*", "!release/old"])
    assert gate["status"] == "skipped"


# ─── the webhook's early draft skip ──────────────────────────────────


def test_a_draft_into_an_excluded_branch_is_a_branch_mismatch(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    import src.review.review_defaults as rd
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg())
    monkeypatch.setattr(rd, "target_branches_for_repo",
                        lambda provider, repo: ["staging", "!main"])
    asyncio.run(_dispatch_review("github", "acme/payments", 8,
                                 expected_workspace_id="ws-1", skip_reason="draft",
                                 pr_meta={"title": "WIP", "base_ref": "main"}))
    assert queue == []
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 8)
    assert row.status == "skipped"
    assert row.status_reason.startswith(
        "Skipped — Branch mismatch: target branch 'main' is excluded by '!main'")
    assert [st["key"] for st in row.stages][-2] == "gate_target_branch"


def test_a_draft_into_a_targeted_branch_is_still_a_draft_skip(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    import src.review.review_defaults as rd
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg())
    monkeypatch.setattr(rd, "target_branches_for_repo",
                        lambda provider, repo: ["!master"])
    asyncio.run(_dispatch_review("github", "acme/payments", 9,
                                 expected_workspace_id="ws-1", skip_reason="draft",
                                 pr_meta={"title": "WIP", "base_ref": "main"}))
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 9)
    assert row.status_reason.startswith("Skipped — Draft")


def test_the_resolver_reads_repo_then_workspace(tmp_path, monkeypatch):
    """`target_branches_for_repo` resolves like the gate: repo policy, then
    the workspace default, then nothing (= every branch)."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    import src.api.auto_review as ar_mod
    from src.api.auto_review import RepoConfig
    from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults
    from src.review.review_defaults import target_branches_for_repo

    @compiles(JSONB, "sqlite")
    def _json(type_, compiler, **kw):  # pragma: no cover
        return "JSON"

    store_ = ar_mod.AutoReviewStore(tmp_path / "ar.db")
    monkeypatch.setattr(ar_mod, "_default_store", store_)
    store_.upsert(RepoConfig(user_id="u1", repo_slug="github_acme-api", provider="github",
                             full_name="acme/api", url="https://github.com/acme/api",
                             workspace_id="ws-1", enabled=True))
    url = f"sqlite:///{tmp_path}/celmis.db"
    engine = sa.create_engine(url)
    RepoReviewPolicy.__table__.create(engine)
    WorkspaceReviewDefaults.__table__.create(engine)
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        assert target_branches_for_repo("github", "acme/api") == []
        with engine.begin() as conn:
            conn.execute(WorkspaceReviewDefaults.__table__.insert().values(
                workspace_id="ws-1", target_branches=["!main"]))
        assert target_branches_for_repo("github", "acme/api") == ["!main"]
        with engine.begin() as conn:
            conn.execute(RepoReviewPolicy.__table__.insert().values(
                repo_slug="github_acme-api", workspace_id="ws-1", enabled=True,
                prompt_template="", folder_rules=[], agent_prompt_overrides={},
                mcp_sources=[], target_branches=["release/*"]))
        assert target_branches_for_repo("github", "acme/api") == ["release/*"]
        assert target_branches_for_repo("github", "someone/else") == []
    finally:
        engine.dispose()


# ─── both settings layers refuse what can never match ────────────────


def test_the_save_path_refuses_a_bare_negation():
    from src.api.routers.review_policies import target_branches_from_payload

    with pytest.raises(HTTPException) as exc:
        target_branches_from_payload(["main", "!"])
    assert exc.value.status_code == 422
    assert "target_branches" in exc.value.detail
    assert target_branches_from_payload([" main", "!master", "main"]) == ["main", "!master"]
    assert target_branches_from_payload(None) is None
    assert target_branches_from_payload([]) == []
