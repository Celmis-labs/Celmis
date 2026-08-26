"""The form invited a spelling that meant a different provider.

Its own hint read "Accepts: full URL, owner/name, or provider:owner/name", and
a bare `owner/name` with no scheme parses as BITBUCKET — a default that
predates GitHub support and is relied on by the tests that own it.

So installing this product from its own README, connecting GitHub through the
interface and typing exactly what the hint suggested produced four repositories
registered under the wrong provider. Their index jobs failed five times each
with "No bitbucket credentials" and died. The page showed `indexed: false` and
no reason at all.

Found by following the interface instead of prior knowledge — which is the
whole reason to install a thing you wrote as if you had never seen it.

The workspace already knew the answer. One connected provider means an
unqualified name belongs to it; two make it genuinely ambiguous, and there the
historical default stands, because guessing between two right answers is worse
than the documented one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.routers.repos import _qualify_with_connected_provider

WS, USER = "ws-alpha", "u-1"


@pytest.fixture
def connected(monkeypatch):
    """Control which Git providers the workspace has credentials for."""
    state: dict[str, list[str]] = {"providers": []}

    def resolve(provider, *, user_id, workspace_id, store=None):
        return object() if provider in state["providers"] else None

    monkeypatch.setattr("src.credentials.get_credential_store",
                        lambda: SimpleNamespace())
    monkeypatch.setattr("src.credentials.git_keys.resolve_git_credential", resolve)
    return state


def _q(value: str) -> str:
    return _qualify_with_connected_provider(value, WS, USER)


def test_a_bare_name_takes_the_one_connected_provider(connected):
    connected["providers"] = ["github"]

    assert _q("acme/api") == "github:acme/api"


def test_the_wrong_default_is_what_this_replaces(connected):
    """Without the fix the same input reaches parse_repo_url unqualified, and
    that is Bitbucket — the failure this test exists for."""
    from src.sync.git_providers import parse_repo_url

    assert parse_repo_url("acme/api").provider.value == "bitbucket"

    connected["providers"] = ["github"]
    assert parse_repo_url(_q("acme/api")).provider.value == "github"


def test_gitlab_works_the_same_way(connected):
    connected["providers"] = ["gitlab"]

    assert _q("acme/api") == "gitlab:acme/api"


def test_two_providers_leave_it_alone(connected):
    """Genuinely ambiguous. Guessing between two right answers is worse than
    the documented default, and the hint now names the explicit form."""
    connected["providers"] = ["github", "gitlab"]

    assert _q("acme/api") == "acme/api"


def test_no_provider_leaves_it_alone(connected):
    connected["providers"] = []

    assert _q("acme/api") == "acme/api"


@pytest.mark.parametrize("value", [
    "https://github.com/acme/api",
    "github:acme/api",
    "gitlab:acme/group/api",
    "git@github.com:acme/api.git",
])
def test_a_value_that_names_its_provider_is_untouched(connected, value):
    connected["providers"] = ["bitbucket"]

    assert _q(value) == value


def test_a_gitlab_subgroup_keeps_its_path(connected):
    connected["providers"] = ["gitlab"]

    assert _q("acme/group/api") == "gitlab:acme/group/api"


def test_an_unreadable_credential_store_decides_nothing(monkeypatch):
    """This runs on the registration path. A store that cannot be read must
    cost the inference, not the request."""
    def boom():
        raise RuntimeError("store down")

    monkeypatch.setattr("src.credentials.get_credential_store", boom)

    assert _q("acme/api") == "acme/api"


def test_the_hint_no_longer_recommends_the_ambiguous_form_alone():
    """The text that caused this. It must say how a bare name is resolved."""
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    en = json.loads((root / "web/lib/i18n/messages/en.json").read_text(encoding="utf-8"))
    hint = en["repositories.urlHint"]

    assert "connected provider" in hint, hint
    assert "github:" in hint, "the explicit form is not offered"
