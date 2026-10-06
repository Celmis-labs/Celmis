"""The webhook installer subscribes to comment events, and a hook installed
before they existed can be found and repaired in one press.

The hook is the only way a comment reaches the receiver; a hook that lacks the
comment events installs fine and answers no command — so the API says which
hooks are outdated and a workspace admin repairs them all at once.
"""

from __future__ import annotations

import pytest

from src.api.routers import repos as repos_router
from src.review import webhook_install as wi
from tests.review.test_the_review_webhook_installs_itself import (  # noqa: F401 — fixtures
    ADMIN,
    OTHER_WS,
    WS,
    FakeProvider,
    api,
    cfg,
    env,
    use,
)

OLD_EVENTS = {
    "github": ["pull_request", "push"],
    "gitlab": ["merge_requests_events"],
    "bitbucket": ["pullrequest:created", "pullrequest:updated",
                  "pullrequest:fulfilled", "pullrequest:rejected"],
}


@pytest.mark.parametrize("provider, needle", [
    ("github", "issue_comment"), ("gitlab", "note_events"),
    ("bitbucket", "pullrequest:comment_created"),
])
def test_the_installer_lists_the_comment_events_it_subscribes_to(provider, needle):
    assert needle in wi.EVENTS[provider]


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_a_hook_without_the_comment_events_is_missing_them(provider):
    missing = wi.missing_events(provider, OLD_EVENTS[provider])
    assert missing and set(missing) <= set(wi.EVENTS[provider])
    assert wi.missing_events(provider, wi.EVENTS[provider]) == []


def test_a_wildcard_hook_misses_nothing():
    assert wi.missing_events("bitbucket", ["*"]) == []


def test_the_status_of_an_old_hook_says_it_is_outdated(env, monkeypatch):  # noqa: F811
    full = "acme/billing"
    existing = {"id": 7, "events": OLD_EVENTS["github"], "active": True,
                "config": {"url": f"https://celmis.example.com/backend/webhook/github/{WS}"}}
    fake = FakeProvider("github", canonical=full, hooks=[existing])
    use(monkeypatch, fake)
    st = wi.status(cfg("github", full), user_id=ADMIN.id, base="https://celmis.example.com")
    assert st.status == "installed" and st.outdated
    assert "issue_comment" in st.missing_events


def _remember_old_hook(api, provider, full, *, ws=WS):  # noqa: F811
    c = cfg(provider, full, ws=ws)
    api.store.upsert(c)
    repos_router._remember_webhook_state(ws, c.repo_slug, wi.WebhookStatus(
        provider=provider, status="installed", events=OLD_EVENTS[provider],
        url=wi.webhook_url("https://celmis.example.com", provider, ws)))
    return c


def test_the_repository_list_flags_an_outdated_hook(api):  # noqa: F811
    _remember_old_hook(api, "github", "acme/billing")
    hook = api.client.get("/api/repos").json()[0]["webhook"]
    assert hook["outdated"] is True and "issue_comment" in hook["missing_events"]


def test_repairing_all_updates_each_outdated_hook_in_place(api, monkeypatch):  # noqa: F811
    c = _remember_old_hook(api, "github", "acme/billing")
    existing = {"id": 7, "events": OLD_EVENTS["github"], "active": True,
                "config": {"url": f"https://celmis.example.com/backend/webhook/github/{WS}"}}
    fake = FakeProvider("github", canonical="acme/billing", hooks=[existing])
    use(monkeypatch, fake)

    resp = api.client.post("/api/repos/webhooks/repair-outdated")

    assert resp.status_code == 200, resp.text
    [row] = resp.json()["repos"]
    assert (row["repo_slug"], row["status"]) == (c.repo_slug, "installed")
    assert fake.methods().count("POST") == 0  # updated, never duplicated
    patched = next(body for m, _, body in fake.calls if m == "PATCH")
    assert "issue_comment" in patched["events"]
    hook = api.client.get("/api/repos").json()[0]["webhook"]
    assert hook["outdated"] is False


def test_a_current_hook_is_left_alone(api, monkeypatch):  # noqa: F811
    c = cfg("github", "acme/billing")
    api.store.upsert(c)
    repos_router._remember_webhook_state(WS, c.repo_slug, wi.WebhookStatus(
        provider="github", status="installed", events=wi.EVENTS["github"]))
    fake = FakeProvider("github", canonical="acme/billing")
    use(monkeypatch, fake)
    assert api.client.post("/api/repos/webhooks/repair-outdated").json() == {"repos": []}
    assert fake.calls == []


def test_only_a_workspace_admin_may_repair(api, monkeypatch):  # noqa: F811
    _remember_old_hook(api, "github", "acme/billing")
    api.state.admins.clear()
    assert api.client.post("/api/repos/webhooks/repair-outdated").status_code == 403


def test_another_workspaces_hooks_are_not_touched(api, monkeypatch):  # noqa: F811
    _remember_old_hook(api, "github", "acme/other", ws=OTHER_WS)
    fake = FakeProvider("github", canonical="acme/other")
    use(monkeypatch, fake)
    assert api.client.post("/api/repos/webhooks/repair-outdated").json() == {"repos": []}
    assert fake.calls == []
