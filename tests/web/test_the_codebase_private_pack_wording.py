"""A private pack is called that, a publish token is shown once, and the Usage
tab shows counts and times — never a person.

* A restricted pack with no team behind it is "Only the people you add
  (private)" everywhere its audience is named: the badge, the picker in the
  create dialog and on the Sharing tab, its hint and description. A reader cannot
  see the team list, so the plain "Restricted" stays for them.
* The Sharing tab has a Publish tokens section for maintainers: create with a
  label and an expiry, show the token once with its copy button and the exact
  `celmis-pack push` command (with `CELMIS_URL`), list, revoke after asking. The
  token lives in component state only; it is never written to browser storage,
  a URL or the query cache.
* The Usage tab renders the API's `usage` object and nothing the API does not
  send; the old `usage_30d` number is gone from the client.

Read with comments stripped: a name surviving in prose must not count as the
name surviving in code.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    WEB,
    _strip_comments,
)
from tests.web.test_the_seams_between_page_and_server_hold import _compile_and_run

LIB = WEB / "lib" / "codebase.ts"
API = WEB / "lib" / "api.ts"
COMPONENTS = WEB / "components" / "codebase"
APP = WEB / "app" / "(app)" / "codebase"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _en() -> dict[str, str]:
    return json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))


def _run(tmp_path, body: str):
    return _compile_and_run(
        tmp_path, {"codebase.ts": LIB.read_text(encoding="utf-8")},
        'import * as cb from "./codebase";\n' + body)


# ─── the private pack's name ─────────────────────────────────────────


def test_the_private_pack_is_named_in_the_words_the_product_promises():
    en = _en()
    assert en["codebase.audience.private"] == "Only the people you add (private)"
    for key in ("short", "desc"):
        assert en[f"codebase.audience.private.{key}"]
    # It says who does NOT see it, in the places a maintainer reads.
    assert "owners or admins" in en["codebase.audience.private.desc"]
    assert "super-admin" in en["codebase.audience.privateWarning"]
    assert "private" in en["codebase.guide.privateNote"]


@pytest.mark.skipif(not TSC.exists(), reason="web deps not installed")
def test_only_a_restricted_pack_with_no_team_is_private(tmp_path):
    got = _run(tmp_path, """
const cases: [string, number | undefined][] = [
  ["restricted", 0], ["restricted", 1], ["restricted", undefined],
  ["teams", 0], ["workspace", 0],
];
console.log(JSON.stringify(cases.map(([a, n]) => [cb.isPrivatePack(a, n), cb.audienceKey(a, n)])));
""")
    assert got == [
        [True, "codebase.audience.private"],
        [False, "codebase.audience.restricted"],
        [False, "codebase.audience.restricted"],     # a reader does not see the teams
        [False, "codebase.audience.teams"],
        [False, "codebase.audience.workspace"],
    ]


def test_every_place_that_names_the_audience_knows_how_many_teams_back_it():
    picker = _code(COMPONENTS / "audience.tsx")
    assert "audienceKey(audience, teamCount)" in picker           # the badge
    assert "label: t(audienceKey(a, teamCount))" in picker        # the picker's options
    assert "codebase.audience.private.short" in picker and "codebase.audience.private.desc" in picker
    assert "codebase.audience.privateWarning" in picker
    create = _code(COMPONENTS / "create-pack.tsx")
    assert "teamCount={grants.length}" in create
    sharing = _code(COMPONENTS / "sharing-tab.tsx")
    assert "teamCount={pack.teams.length}" in sharing and sharing.count("teamCount=") >= 2
    # A reader is never given the count: the list it comes from is empty for them.
    for page in (APP / "page.tsx", APP / "packs" / "[id]" / "page.tsx"):
        assert re.search(r"teamCount=\{\w+\.can_maintain \? \w+\.teams\.length : undefined\}", _code(page)), page


# ─── publish tokens ──────────────────────────────────────────────────


def test_the_sharing_tab_offers_publish_tokens_and_the_api_client_has_all_three_calls():
    sharing = _code(COMPONENTS / "sharing-tab.tsx")
    assert "<PublishTokensCard pack={pack} />" in sharing
    for call in ("codebaseApi.publishTokens(", "codebaseApi.createPublishToken(",
                 "codebaseApi.revokePublishToken("):
        assert call in sharing, call
    api = _code(API)
    assert "`${packPath(id)}/tokens`" in api and "`${packPath(id)}/tokens/${encodeURIComponent(tokenId)}`" in api


def test_the_token_is_shown_once_with_its_command_and_kept_nowhere():
    sharing = _code(COMPONENTS / "sharing-tab.tsx")
    card = sharing[sharing.index("function PublishTokensCard"):]
    # held in component state: gone when the card unmounts
    assert "useState<PackPublishTokenCreated | null>(null)" in card
    assert "setFresh(r)" in card
    # shown with a copy button, and the command that uses it
    assert 'CodeBlock text={fresh.token}' in card
    assert "publishCommands(address, pack.slug" in card
    # and only there: not in browser storage, a URL, the query cache or a toast
    for forbidden in ("localStorage", "sessionStorage", "setQueryData", "router.", "toast.success(fresh",
                      "console."):
        assert forbidden not in card, forbidden
    assert re.search(r"queryKey: \[\"codebase\", \"tokens\", pack\.id\]", card)


@pytest.mark.skipif(not TSC.exists(), reason="web deps not installed")
def test_the_command_names_the_address_the_folder_and_never_the_token(tmp_path):
    got = _run(tmp_path, """
console.log(JSON.stringify([
  cb.publishCommands("https://celmis.example.com/backend", "billing", "the token above"),
  cb.publishCommands("", "", "the token above"),
]));
""")
    first, second = got
    assert first.splitlines() == [
        "export CELMIS_URL=https://celmis.example.com/backend",
        "export CELMIS_PACK_TOKEN=<the token above>",
        "celmis-pack push ./skills/billing",
    ]
    assert second.splitlines()[0] == "export CELMIS_URL=<server address>"
    assert second.splitlines()[-1] == "celmis-pack push ./skills/<folder>"
    assert "cpk_" not in first and "cpk_" not in second


@pytest.mark.skipif(not TSC.exists(), reason="web deps not installed")
def test_a_token_is_active_revoked_or_expired_as_the_server_would_treat_it(tmp_path):
    now = "Date.parse('2026-10-01T00:00:00Z')"
    got = _run(tmp_path, f"""
const cases: any[] = [
  {{ revoked: false, expires_at: "2026-11-01T00:00:00Z" }},
  {{ revoked: true, expires_at: "2026-11-01T00:00:00Z" }},
  {{ revoked: false, expires_at: "2026-09-30T23:59:59Z" }},
  {{ revoked: false, expires_at: "2026-10-01T00:00:00Z" }},
  {{ revoked: true, expires_at: "2026-09-01T00:00:00Z" }},
  {{ revoked: false, expires_at: null }},
];
console.log(JSON.stringify(cases.map((c) => cb.tokenState(c, {now}))));
""")
    assert got == ["active", "revoked", "expired", "expired", "revoked", "active"]


@pytest.mark.skipif(not TSC.exists(), reason="web deps not installed")
def test_the_limits_the_page_states_for_a_token_are_the_servers(tmp_path):
    from src.knowledge import store

    got = _run(tmp_path, "console.log(JSON.stringify(cb.PUBLISH_TOKEN_LIMITS));")
    assert got == {"defaultDays": store.DEFAULT_TOKEN_DAYS, "maxDays": store.MAX_TOKEN_DAYS,
                   "maxActive": store.MAX_ACTIVE_TOKENS, "labelChars": store.MAX_LABEL}
    # every expiry the picker offers is one the server accepts
    options = re.search(r"const TOKEN_DAYS = \[([^\]]*)\]", _code(COMPONENTS / "sharing-tab.tsx"))
    assert options
    days = [int(d) for d in re.findall(r"\d+", options.group(1))]
    assert days and all(1 <= d <= store.MAX_TOKEN_DAYS for d in days)
    assert store.DEFAULT_TOKEN_DAYS in days


# ─── the Usage tab ───────────────────────────────────────────────────


def test_the_usage_tab_renders_the_usage_object_and_the_old_number_is_gone():
    page = _code(APP / "packs" / "[id]" / "page.tsx")
    assert 'active === "usage" && maintainer && <UsageTab pack={p} />' in page
    tab = _code(COMPONENTS / "usage-tab.tsx")
    for field in ("usage.calls_30d", "usage.last_used_at", "usage.skills", "s.skill", "s.opens_30d",
                  "s.last_used_at"):
        assert field in tab, field
    for path in [API, *COMPONENTS.rglob("*.tsx"), *APP.rglob("*.tsx")]:
        assert "usage_30d" not in _code(path), path.name
    api = _code(API)
    assert "usage: PackUsage | null;" in api


def test_the_usage_type_has_exactly_the_servers_fields_and_no_person():
    from src.api.routers import codebase

    api = _code(API)
    usage = re.search(r"export type PackUsage = \{([^}]*)\}", api)
    skill = re.search(r"export type PackSkillUsage = \{([^}]*)\}", api)
    assert usage and skill
    assert set(re.findall(r"(\w+):", usage.group(1))) == set(codebase.UsageOut.model_fields)
    assert set(re.findall(r"(\w+):", skill.group(1))) == set(codebase.SkillUsageOut.model_fields)
    # nothing in either side names a person or a token
    for name in (*codebase.UsageOut.model_fields, *codebase.SkillUsageOut.model_fields):
        assert not re.search(r"user|email|person|people|actor|token|client", name), name
