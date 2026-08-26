"""Every Bitbucket review looked its repository up at an address nothing wrote.

`PullRequest.repo_slug` is always `{provider}_{owner}-{name}`. `ParsedRepo.slug`
— which is what registration, cloning, the vault, the graph, the review-policy
row and group membership are all keyed on — drops the provider prefix for
Bitbucket and Generic, because the clones predate the prefix
(src/sync/git_providers.py:57-72).

GitHub and GitLab spell the two identically, so the divergence was invisible
for two providers out of three. For the third, four separate lookups missed:

  * the review POLICY (`_load_policy`) — so custom rules, target branches and
    per-repo model overrides silently reverted to defaults on every review;
  * the group (`_find_group_for_repo`) — so cross-repo drift never ran;
  * the vault overview and the clone path in `_load_style_guide`.

Each answered with a plausible default rather than an error, which is why none
of them was ever reported.

`local_slug` asks `parse_repo_url` instead of rebuilding the rule: the rule has
an exception, and a second copy of a rule with an exception is a second chance
to miss it.
"""

from __future__ import annotations

import pytest

from src.review.graph_context import graph_slug_candidates
from src.review.models import PullRequest
from src.sync.git_providers import parse_repo_url


def _pr(provider: str, repo: str = "acme/web-ui") -> PullRequest:
    return PullRequest(
        provider=provider, repo=repo, number=1, title="t", description="",
        author="a", base_ref="main", base_sha="s", head_ref="f", head_sha="h",
        state="open",
    )


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_the_local_slug_is_what_registration_wrote(provider):
    pr = _pr(provider)

    assert pr.local_slug == parse_repo_url(f"{provider}:{pr.repo}").slug


def test_bitbucket_is_the_one_that_differs():
    assert _pr("bitbucket").local_slug == "acme-web-ui"
    assert _pr("bitbucket").repo_slug == "bitbucket_acme-web-ui"


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_nothing_moves_for_the_other_providers(provider):
    pr = _pr(provider)

    assert pr.local_slug == pr.repo_slug


def test_a_gitlab_subgroup_keeps_its_whole_path():
    pr = _pr("gitlab", repo="acme/backend/payments")

    assert pr.local_slug == parse_repo_url("gitlab:acme/backend/payments").slug
    assert "payments" in pr.local_slug


def test_the_graph_candidates_include_the_local_spelling():
    assert graph_slug_candidates(_pr("bitbucket")) == [
        "bitbucket_acme-web-ui", "acme-web-ui",
    ]


def test_the_graph_candidates_do_not_repeat_themselves():
    assert graph_slug_candidates(_pr("github")) == ["github_acme-web-ui"]


def test_an_unparseable_repo_falls_back_rather_than_raising():
    """`local_slug` is read on every review; it must never be the thing that
    ends one."""
    pr = _pr("github", repo="")

    assert pr.local_slug  # some answer, no exception


# ─── the call sites ──────────────────────────────────────────────────
#
# Keyed on the AST attribute, not on source text: a comment naming local_slug
# must not satisfy these.


def _attrs_read_in(path: str, func: str) -> set[str]:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func),
        None,
    )
    assert target is not None, f"{func} not found in {path}"
    return {
        n.attr for n in ast.walk(target)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name) and n.value.id == "pr"
    }


def _arg_attr(path: str, func: str, callee: str) -> set[str]:
    """Which `pr.<attr>` each call to `callee` inside `func` is given.

    Asking whether `local_slug` is READ anywhere in the function is not the
    same question, and a mutation proved it: revert the lookup, add one
    `logger.debug(pr.local_slug)`, and the old assertion stayed green. What has
    to hold is that the ARGUMENT is the local slug.
    """
    import ast
    import pathlib as _p

    tree = ast.parse(_p.Path(path).read_text(encoding="utf-8"))
    target = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                  and n.name == func)
    out: set[str] = set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.id if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", ""))
        if name != callee:
            continue
        for arg in [*node.args, *(k.value for k in node.keywords)]:
            if (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
                    and arg.value.id == "pr"):
                out.add(arg.attr)
    return out


def test_drift_looks_the_group_up_by_the_local_slug():
    args = _arg_attr("src/review/cross_repo_drift.py", "detect_drift",
                     "_find_group_for_repo")

    assert args == {"local_slug"}, args


@pytest.mark.parametrize("func,callee", [
    ("_review_impl", "_load_policy"),          # the review policy row
    ("_load_repo_overview", "repo_vault_path"),  # the vault overview
    ("_load_style_guide", "repo_path"),          # the clone
])
def test_every_address_site_uses_the_local_slug(func, callee):
    """Four sites missed for Bitbucket, and only one was ever asserted."""
    args = _arg_attr("src/review/orchestrator.py", func, callee)

    assert args, f"{callee} is no longer called with a pr attribute in {func}"
    assert args == {"local_slug"}, args
