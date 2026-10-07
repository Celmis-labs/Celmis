"""Who may touch the Jira connection and the credential endpoints, role by role.

The rule: saving, verifying and removing a connection, and every
`/api/task-context/*` read (it spends the Jira token), is for an owner or an
admin of the ACTIVE workspace, and for a global admin. An editor, a member, a
viewer, somebody who belongs to another workspace and somebody who belongs to
none are all refused. The one open door is the connection LIST: any member may
learn that a provider is connected (the dashboard needs it) but is told no
account, no host and no e-mail; and no caller is ever sent a token.

Built on tests/api/rbac_world.py: roles are the real membership rows.
"""

from __future__ import annotations

import json

import pytest

from tests.api.rbac_world import world

TOKEN = "jira-token-do-not-leak-0123456789"
GIT_TOKEN = "ghp_do-not-leak-0123456789abcdefghij"
EMAIL = "robot@acme.example"
SITE = "https://acme.atlassian.net"

REFUSED = ("editor_a", "member_a", "viewer_a", "member_b")
PASS = ("owner_a", "admin_a", "gadmin", "su")

#: (method, url, body) — every route that changes or spends a credential.
GUARDED = [
    ("PUT", "/api/connections/jira",
     {"provider": "jira", "token": TOKEN, "email": EMAIL, "base_url": SITE}),
    ("PUT", "/api/connections/github", {"provider": "github", "token": GIT_TOKEN}),
    ("DELETE", "/api/connections/jira", None),
    ("DELETE", "/api/connections/github", None),
    ("POST", "/api/connections/jira/verify", None),
    ("POST", "/api/connections/github/verify", None),
    ("GET", "/api/task-context/issue/PROJ-123", None),
    ("GET", "/api/task-context/fields", None),
    ("GET", "/api/task-context/projects", None),
]


@pytest.fixture(autouse=True)
def _no_provider_is_called(monkeypatch):
    """A save verifies the token against the provider first; nothing here may
    leave the machine, so the provider answers "no" and nothing is stored."""
    from src.api.routers import connections
    from src.api.schemas import ConnectionVerifyResult

    def refuse(*args, **kwargs):
        provider = args[0] if args and isinstance(args[0], str) else "jira"
        return ConnectionVerifyResult(ok=False, provider=provider, error="offline in tests")

    monkeypatch.setattr(connections, "_verify_jira", refuse)
    monkeypatch.setattr(connections, "_verify_token", refuse)

    # The task-context reads open a real client on the saved connection; its
    # transport answers as an unreachable site would, so the reads still go
    # through the whole route and its error wording, and SITE is never dialled.
    from src.review.task_context.jira_client import JiraClient, JiraError

    def unreachable(self, *args, **kwargs):
        raise JiraError("unreachable",
                        f"Jira at {self.instance.host}: could not connect (offline in tests)")

    monkeypatch.setattr(JiraClient, "_get", unreachable)


def _store():
    from src.credentials import get_credential_store

    return get_credential_store()


def _save_connections(w) -> None:
    """A Jira and a GitHub credential in workspace A's own slot."""
    from src.credentials import git_workspace_slot

    slot = git_workspace_slot(w.ws["ws-a"])
    _store().save("jira", TOKEN, user_id=slot, account_label="default",
                  metadata={"atlassian_email": EMAIL, "jira_base_url": SITE})
    _store().save("github", GIT_TOKEN, user_id=slot, account_label="default",
                  metadata={"login": "octocat-login"})


@pytest.mark.parametrize("who", REFUSED)
async def test_nobody_below_admin_can_change_or_spend_a_connection(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _save_connections(w)
        for method, url, body in GUARDED:
            r = await w.client.request(method, url, json=body, headers=w.h(who, "ws-a"))
            assert r.status_code in (401, 403), f"{who} {method} {url}: {r.status_code}"
            assert TOKEN not in r.text and GIT_TOKEN not in r.text
        # Nothing was changed by the refused calls.
        from src.credentials import git_workspace_slot

        slot = git_workspace_slot(w.ws["ws-a"])
        assert _store().load("jira", user_id=slot, update_last_used=False).secret == TOKEN
        assert _store().load("github", user_id=slot, update_last_used=False) is not None


@pytest.mark.parametrize("who", PASS)
async def test_owners_admins_and_global_admins_pass_the_gate(tmp_path, monkeypatch, who):
    """Past the gate the answer is the endpoint's own: here no connection is
    saved, so a task read is a 404 and a re-verify is a 404, never a 403."""
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        for method, url, body in GUARDED:
            if method == "PUT" or (method == "DELETE" and "jira" not in url):
                continue  # these reach a provider or change data; see the other tests
            r = await w.client.request(method, url, json=body, headers=w.h(who, "ws-a"))
            assert r.status_code not in (401, 403), f"{who} {method} {url}: {r.status_code}"


async def test_a_task_read_with_no_connection_saved_is_a_404_for_an_admin(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        for url in ("/api/task-context/issue/PROJ-123", "/api/task-context/fields",
                    "/api/task-context/projects"):
            r = await w.client.get(url, headers=w.h("admin_a", "ws-a"))
            assert r.status_code == 404, f"{url}: {r.status_code}"


@pytest.mark.parametrize("who", ["admin_b", "loner"])
async def test_a_role_in_one_workspace_opens_nothing_of_another(tmp_path, monkeypatch, who):
    """Somebody who is not a member of A but names workspace A is served as
    their own workspace (the resolver's fall-back; a person with no workspace
    gets a personal one, of which they are the owner): whatever it does,
    A's credentials are not read, replaced or removed, and none of it is shown."""
    from src.credentials import git_workspace_slot

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _save_connections(w)
        slot_a = git_workspace_slot(w.ws["ws-a"])
        for method, url, body in GUARDED:
            r = await w.client.request(method, url, json=body, headers=w.h(who, "ws-a"))
            assert TOKEN not in r.text and GIT_TOKEN not in r.text, f"{method} {url}"
        listed = await w.client.get("/api/connections", headers=w.h(who, "ws-a"))
        assert "octocat-login" not in listed.text and EMAIL not in listed.text
        assert _store().load("jira", user_id=slot_a, update_last_used=False).secret == TOKEN
        assert _store().load("github", user_id=slot_a, update_last_used=False) is not None


async def test_the_list_says_a_provider_is_connected_but_not_whose_account(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _save_connections(w)
        for who in ("viewer_a", "member_a", "editor_a"):
            r = await w.client.get("/api/connections", headers=w.h(who, "ws-a"))
            assert r.status_code == 200, f"{who}: {r.status_code}"
            rows = {row["provider"]: row for row in r.json()}
            assert rows["jira"]["connected"] is True
            assert rows["jira"]["metadata"] == {}
            assert rows["jira"]["account_label"] == "default"
            assert rows["github"]["metadata"] == {}, "no account detail for a non-admin"
            for needle in (TOKEN, GIT_TOKEN, EMAIL, SITE, "octocat-login"):
                assert needle not in r.text, f"{who} was told {needle!r}"


@pytest.mark.parametrize("who", PASS)
async def test_an_admin_is_told_the_account_but_never_the_token(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _save_connections(w)
        r = await w.client.get("/api/connections", headers=w.h(who, "ws-a"))
        assert r.status_code == 200
        rows = {row["provider"]: row for row in r.json()}
        assert rows["jira"]["metadata"]["atlassian_email"] == EMAIL
        assert rows["github"]["metadata"]["login"] == "octocat-login"
        assert TOKEN not in r.text and GIT_TOKEN not in r.text


async def test_no_endpoint_of_the_surface_returns_a_stored_secret(tmp_path, monkeypatch):
    """Every readable route of the connection and task-context routers, asked by
    every role, never carries a secret - not even as a substring of an error."""
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        _save_connections(w)
        reads = [url for method, url, _ in GUARDED if method == "GET"] + ["/api/connections"]
        for who in (*PASS, *REFUSED, "viewer_a", "member_a"):
            for url in reads:
                r = await w.client.get(url, headers=w.h(who, "ws-a"))
                blob = r.text + json.dumps(dict(r.headers))
                assert TOKEN not in blob and GIT_TOKEN not in blob, f"{who} {url}"


def _routers() -> tuple:
    from src.api.routers import task_context

    return (task_context.router,)
