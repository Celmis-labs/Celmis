"""Single-tenant installs over HTTP: the token's repo list is the authority.

Product requirement 1 (a repository with no team rule is closed by default) is
proved at the resolver by the access lane's own tests. What is checked HERE is
what a developer sees through ``/mcp/dev/`` and ``/mcp/`` on a single_tenant
deployment, in both settings of ``CELMIS_UNRULED_REPO_ACCESS``:

* ``deny`` (the default) and ``open`` both leave a token inside its repo list;
* neither answer names a repository the token cannot read, in particular not
  through the Q&A bookkeeping fields ``blocked_repos`` / ``access_notice``;
* a self-signed legacy JWT is refused.

The boot test runs on any tree; the rest need the access and tools lanes.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e_local.client import McpClient, McpHttpError
from tests.e2e_local.stack import Stack

DEV = "/mcp/dev/"
FULL = "/mcp/"
SHOP, BILLING, GATEWAY = "acme/shop", "acme/billing", "acme/gateway"
#: The old Q&A bookkeeping that listed repositories the caller could NOT read.
LEAKY_KEYS = ("blocked_repos", "access_notice")
SAFE_ARGS = {"query": "create_order", "name": "create_order", "symbol": "create_order",
             "pattern": "create_order", "question": "where is create_order defined",
             "name_pattern": "create_order"}


def _present(st: Stack, text: str) -> set[str]:
    return {name for name, repo in st.repos.items() if repo.slug in text}


def test_a_single_tenant_stack_boots_and_serves_the_workspace_it_has(tmp_path) -> None:
    with Stack(tmp_path / "boot", mode="single_tenant") as st:
        assert st.mode == "single_tenant"
        res = McpClient(st.url, FULL, st.token("su", profile="full")).call("list_accessible_repos")
        assert not res.is_error and _present(st, res.raw) <= set(st.repos)


@pytest.fixture(scope="module", params=["deny", "open"])
def stack(request, tmp_path_factory):
    extra = {"CELMIS_UNRULED_REPO_ACCESS": request.param}
    with Stack(tmp_path_factory.mktemp(f"single-{request.param}"), mode="single_tenant",
               extra_env=extra) as st:
        st.setting = request.param  # type: ignore[attr-defined]
        yield st


@pytest.mark.needs_lane("access")
@pytest.mark.needs_lane("tools")
def test_a_token_stays_inside_its_repo_list_whatever_the_unruled_setting_says(stack) -> None:
    token = stack.token("dev", repos=[SHOP])
    res = McpClient(stack.url, DEV, token).call("repos")
    assert not res.is_error
    assert _present(stack, res.text) == {SHOP}, stack.setting  # type: ignore[attr-defined]
    for tool, args in (("grep", {"pattern": "create_order"}), ("find", {"query": "create_order"})):
        out = McpClient(stack.url, DEV, token).call(tool, args)
        assert _present(stack, out.raw) <= {SHOP}, (tool, stack.setting)  # type: ignore[attr-defined]


@pytest.mark.needs_lane("access")
@pytest.mark.needs_lane("tools")
def test_no_answer_names_a_repository_the_token_cannot_read(stack) -> None:
    hidden = [stack.slug_of(r) for r in (BILLING, GATEWAY)]
    client = McpClient(stack.url, FULL, stack.token("dev", repos=[SHOP], profile="full"))
    tools = {t["name"]: t for t in client.list_tools()}
    asked = 0
    for name in ("list_accessible_repos", "list_repos", "find_symbol", "get_symbol",
                 "search_code", "ask_code", "get_project"):
        if name not in tools:
            continue
        required = tools[name].get("inputSchema", {}).get("required", [])
        args = {k: SAFE_ARGS.get(k, stack.slug_of(SHOP) if "repo" in k else "x") for k in required}
        res = client.call(name, args)
        asked += 1
        for slug in hidden:
            assert slug not in res.raw, (name, slug)
        for key in LEAKY_KEYS:
            if key in res.raw:  # an empty list is fine; a name in it is not
                payload = res.structured if isinstance(res.structured, dict) else {}
                assert not payload.get(key), (name, key, json.dumps(payload.get(key))[:80])
    assert asked, "none of the read tools was reachable"


@pytest.mark.needs_lane("access")
@pytest.mark.needs_lane("tools")
def test_a_legacy_jwt_is_refused_on_a_single_tenant_install_too(stack) -> None:
    with pytest.raises(McpHttpError) as exc:
        McpClient(stack.url, DEV, stack.legacy_token("dev")).call("repos")
    assert exc.value.status in (401, 403)
