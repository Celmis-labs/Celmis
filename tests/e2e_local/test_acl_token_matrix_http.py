"""Who may see what over HTTP: the token and ACL matrix, on the real stack.

Needs the access lane (grants, revocation, per-token repo patterns) and the tools
lane (the ``/mcp/dev/`` endpoint); skipped by feature probe until both are on the
branch, and failing under ``CELMIS_E2E_STRICT=1``.

Rules checked here:

* a token sees exactly the repositories its patterns name AND its workspace holds;
* a repository the caller may not read answers like one that does not exist: the
  same text once the caller's own spelling of the name is normalised. The single
  allowed difference is the ``similar:`` hint, which ranks the CALLER'S OWN visible
  repositories against what they typed and so depends on the spelling (the plan's
  "byte-identical" wording is read that way; the hint is checked separately);
* ``tools/list`` is the same list for every caller of a profile;
* expired, revoked and missing credentials never reach a tool;
* a read-only token cannot use a write tool.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e_local.client import McpClient, McpHttpError
from tests.e2e_local.stack import Stack

pytestmark = [pytest.mark.needs_lane("access"), pytest.mark.needs_lane("tools")]

DEV = "/mcp/dev/"
FULL = "/mcp/"
SHOP, BILLING, GATEWAY, LEDGER = "acme/shop", "acme/billing", "acme/gateway", "bco/ledger"
NOBODY = "github_nobody-nothing"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    with Stack(tmp_path_factory.mktemp("acl")) as st:
        st.add_foreign_repo()
        yield st


def _slug(st: Stack, logical: str) -> str:
    return st.repos[logical].slug


def _plain(text: str, typed: str) -> str:
    """An answer with the caller's own spelling and the ranked hint taken out."""
    return "\n".join(ln for ln in text.replace(typed, "<R>").splitlines()
                     if not ln.startswith("similar:"))


def _present(st: Stack, text: str) -> set[str]:
    return {name for name, repo in st.repos.items() if repo.slug in text}


#: caller id -> (who, patterns, repos visible in the workspace)
CALLERS = {
    "su_all": ("su", ["*"], {SHOP, BILLING, GATEWAY}),
    "owner_all": ("owner", ["*"], {SHOP, BILLING, GATEWAY}),
    "token_exact": ("dev", [SHOP], {SHOP}),
    "token_glob": ("dev", ["github_acme-b*"], {BILLING}),
    "token_other_workspace": ("dev", [LEDGER], set()),
}


def _dev_token(st: Stack, caller: str) -> str:
    who, patterns, _ = CALLERS[caller]
    return st.token(who, repos=patterns)


@pytest.mark.parametrize("caller", list(CALLERS))
def test_repos_lists_exactly_what_the_token_allows(stack, caller) -> None:
    visible = CALLERS[caller][2]
    res = McpClient(stack.url, DEV, _dev_token(stack, caller)).call("repos")
    assert not res.is_error, res.text
    assert _present(stack, res.text) == visible
    assert _slug(stack, LEDGER) not in res.raw


@pytest.mark.parametrize("caller", list(CALLERS))
def test_a_search_without_a_repo_never_leaves_the_allowed_set(stack, caller) -> None:
    visible = CALLERS[caller][2]
    client = McpClient(stack.url, DEV, _dev_token(stack, caller))
    for tool, args in (("grep", {"pattern": "create_order"}), ("find", {"query": "create_order"}),
                       ("find", {"query": "Config"})):
        res = client.call(tool, args)
        assert _present(stack, res.raw) <= visible, (tool, caller)
        assert _slug(stack, LEDGER) not in res.raw, (tool, caller)


@pytest.mark.parametrize("caller", list(CALLERS))
def test_an_allowed_repository_answers_and_a_hidden_one_answers_like_a_missing_one(
        stack, caller) -> None:
    visible = CALLERS[caller][2]
    client = McpClient(stack.url, DEV, _dev_token(stack, caller))
    calls = {
        "grep": lambda r: {"pattern": "create_order", "repo": r},
        "find": lambda r: {"query": "create_order", "repo": r},
        "outline": lambda r: {"repo": r, "path": "."},
        "read_symbol": lambda r: {"repo": r, "name": "create_order"},
        "refs": lambda r: {"repo": r, "symbol": "create_order"},
        "map": lambda r: {"repo": r},
    }
    hidden = [n for n in stack.repos if n not in visible]
    for tool, make in calls.items():
        ghost = client.call(tool, make(NOBODY))
        for logical in hidden:
            slug = _slug(stack, logical)
            res = client.call(tool, make(slug))
            same = (res.is_error, _plain(res.text, slug)) == (ghost.is_error, _plain(ghost.text, NOBODY))
            assert same, f"{caller}/{tool}: {logical} is distinguishable from a missing repo"
            # The only part allowed to differ is the "similar:" hint, which ranks the
            # CALLER'S OWN visible repositories against the spelling they typed.
            for line in res.text.splitlines():
                if line.startswith("similar:"):
                    assert all(_slug(stack, h) not in line for h in hidden), (caller, tool)
    for logical in visible:
        ok = client.call("map", {"repo": _slug(stack, logical)})
        assert not ok.is_error and _slug(stack, logical) in ok.text, (caller, logical)


def test_tools_list_is_identical_for_every_caller_of_the_dev_profile(stack) -> None:
    lists = {c: json.dumps(McpClient(stack.url, DEV, _dev_token(stack, c)).list_tools(),
                           sort_keys=True) for c in CALLERS}
    assert len(set(lists.values())) == 1, "tools/list leaks who the caller is"
    names = {t["name"] for t in json.loads(next(iter(lists.values())))}
    assert {"repos", "find", "outline", "read_symbol", "refs", "grep", "map"} <= names


def test_the_existing_endpoint_applies_the_same_patterns(stack) -> None:
    for caller, (who, patterns, visible) in CALLERS.items():
        token = stack.token(who, repos=patterns, profile="full")
        res = McpClient(stack.url, FULL, token).call("list_accessible_repos")
        assert _present(stack, res.raw) == visible, caller
        assert _slug(stack, LEDGER) not in res.raw, caller


# ─── credentials that must not reach a tool ──────────────────────────


@pytest.mark.parametrize("path", [DEV, FULL])
def test_no_credentials_is_refused_before_any_tool(stack, path) -> None:
    assert McpClient(stack.url, path, None).initialize_status() == 401


@pytest.mark.parametrize("path", [DEV, FULL])
def test_a_forged_token_is_refused(stack, path) -> None:
    assert McpClient(stack.url, path, "not-a-token").initialize_status() in (401, 403)


def test_an_expired_grant_is_refused(stack) -> None:
    token = stack.token("dev", repos=[SHOP])
    assert McpClient(stack.url, DEV, token).call("repos").text
    stack.expire_token(stack.last_token_id)
    with pytest.raises(McpHttpError) as exc:
        McpClient(stack.url, DEV, token).call("repos")
    assert exc.value.status in (401, 403)


def test_a_revoked_grant_stops_working_at_once(stack) -> None:
    token = stack.token("dev", repos=[SHOP])
    assert not McpClient(stack.url, DEV, token).call("repos").is_error
    stack.revoke_token(stack.last_token_id)
    with pytest.raises(McpHttpError) as exc:
        McpClient(stack.url, DEV, token).call("repos")
    assert exc.value.status in (401, 403)


def test_a_legacy_jwt_is_refused_under_the_default_setting(stack) -> None:
    """A self-signed JWT has no row behind it (no repo list, no revocation): refused, not filtered."""
    token = stack.legacy_token("dev")
    for path in (DEV, FULL):
        with pytest.raises(McpHttpError) as exc:
            McpClient(stack.url, path, token).call("repos" if path == DEV else "list_accessible_repos")
        assert exc.value.status in (401, 403), path


def test_a_read_only_token_cannot_use_a_write_tool(stack) -> None:
    token = stack.token("dev", repos=["*"], profile="full", allow_write=False)
    client = McpClient(stack.url, FULL, token)
    assert "add_repo" not in {t["name"] for t in client.list_tools()}
    before = set(stack.repos)
    res = client.call("add_repo", {"url": "https://github.com/acme/never-added", "index": False})
    refused = res.is_error or any(w in res.text.lower() for w in ("denied", "scope", "not allowed",
                                                                  "forbidden", "unknown tool"))
    assert refused, res.text[:200]
    assert "never-added" not in json.dumps(
        McpClient(stack.url, FULL, stack.token("su", profile="full"))
        .call("list_accessible_repos").raw)
    assert set(stack.repos) == before


def test_the_dev_profile_cannot_be_given_write_rights(stack) -> None:
    with pytest.raises(Exception, match="read-only|write"):
        stack.token("dev", repos=["*"], profile="dev", allow_write=True)
