"""The pages that show repository sharing keep the rules the server enforces.

  * /admin/sharing - owner and admin of the workspace that GIVES the share
    (`require_share_admin`), and only where the build mounts /api/sharing.
  * /admin/mcp-tokens - superadmin only, as it was before sharing; the new
    "Shared with us" tab and the share picker sit inside that gate, so an admin
    of the receiving workspace who is not a superadmin sees none of it.

Four things are held against the server rather than against each other: the
limits the forms check before they ask (`src/access/shares.py`), the routes the
client calls (the two routers), the capability keys the page and the tab read
(`src/api/routers/capabilities.py`), and the licensed half (making a share, giving
more of one), which only the sharing page may import and only draws when the
capabilities document says the routes are mounted. Read with comments stripped,
so a comment describing the rule cannot stand in for the rule.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
MESSAGES = WEB / "lib" / "i18n" / "messages"
PAGE = WEB / "app" / "(app)" / "admin" / "sharing" / "page.tsx"
TOKENS_PAGE = WEB / "app" / "(app)" / "admin" / "mcp-tokens" / "page.tsx"
API = WEB / "lib" / "api.ts"
RULES = WEB / "lib" / "sharing.ts"
EE_DIALOGS = WEB / "ee" / "sharing" / "create-share-dialog.tsx"

#: Every file that draws a sentence of the sharing feature.
SHARING_FILES = (
    RULES,
    PAGE,
    TOKENS_PAGE,
    EE_DIALOGS,
    WEB / "components" / "sharing" / "narrow-dialog.tsx",
    WEB / "components" / "sharing" / "share-detail.tsx",
    WEB / "components" / "section-tabs.tsx",
)

_OUR_KEYS = re.compile(
    r"""["'`]((?:sharing\.[\w.]+|mcpTokens\.shared\.[\w.]+|mcpTokens\.calls\.shared|nav\.sharing))["'`]"""
)


def _code(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def _en() -> dict[str, str]:
    return json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))


def _tab(href: str) -> str:
    tabs = _code(WEB / "components" / "section-tabs.tsx")
    entry = re.search(r'\{ href: "' + re.escape(href) + r'"[^}]*\}', tabs)
    assert entry, f"the {href} tab is gone"
    return entry.group(0)


# ─── who may open them ───────────────────────────────────────────────


def test_the_sharing_tab_is_for_owners_and_admins_where_sharing_is_mounted() -> None:
    entry = _tab("/admin/sharing")
    assert "workspaceAdminOnly: true" in entry
    assert 'requiresFeature: "repo_sharing"' in entry
    assert "superadminOnly" not in entry, "an owner's admin is not a superadmin"


def test_the_capability_keys_the_web_reads_are_the_ones_the_server_publishes() -> None:
    server = (WEB.parent / "src" / "api" / "routers" / "capabilities.py").read_text(
        encoding="utf-8"
    )
    assert 'Feature("repo_sharing", ("/api/sharing",), ("/admin/sharing",))' in server
    assert 'Feature("repo_sharing_grant", ("/api/sharing-grants",)' in server
    assert "features?.repo_sharing_grant?.available === true" in _code(PAGE)


def test_the_sharing_page_asks_nothing_until_it_knows_the_role() -> None:
    page = _code(PAGE)
    assert "useCanManageWorkspace()" in page
    # The hooks that fetch live in the inner view, which is not rendered for a
    # refused role or while the membership loads.
    gate, inner = page[page.index("export default function SharingPage") :].split(
        "function SharingView()"
    )
    assert "canManage === undefined || canManage === false" in gate
    assert "return <SharingView />" in gate
    assert "useQuery" not in gate
    assert "sharingApi.list" in inner
    for key in ("sharing.forbiddenTitle", "sharing.forbiddenDesc"):
        assert key in gate
        assert key in _en()


def test_the_mcp_tokens_page_stays_for_superadmins_with_the_shared_tab_inside_the_gate() -> None:
    page = _code(TOKENS_PAGE)
    root = page[page.index("export default function McpTokensPage") :]
    root = root[: root.index("\nfunction ")]
    assert "Boolean(session?.isSuperadmin)" in root
    assert "session && !allowed" in root
    assert root.index("mcpTokens.superadminOnly") < root.index("<SharedTab />")
    assert root.index("mcpTokens.superadminOnly") < root.index("<TokensTab />")
    assert "superadminOnly: true" in _tab("/admin/mcp-tokens")


# ─── the licensed half ───────────────────────────────────────────────


def test_only_the_sharing_page_imports_the_licensed_dialogs() -> None:
    importers = []
    for path in WEB.rglob("*.ts*"):
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        if path.relative_to(WEB).parts[0] == "ee":
            continue
        if re.search(r"""from\s+["']@/ee/sharing/""", _code(path)):
            importers.append(path.relative_to(WEB).as_posix())
    assert importers == ["app/(app)/admin/sharing/page.tsx"]


def test_the_licensed_dialogs_are_drawn_only_when_the_routes_are_mounted() -> None:
    page = _code(PAGE)
    assert "const canGrant = caps.data?.features?.repo_sharing_grant?.available === true" in page
    assert re.search(r"\{canGrant && \(\s*<CreateShareDialog", page)
    assert "{canGrant && extending && (" in page
    # The button that opens the dialog is behind the same flag.
    assert "actions={canGrant ?" in page
    # The note about the licence is for a build that SAYS it is off, not one that is silent.
    assert 'featureOff(caps.data, "repo_sharing_grant")' in page


def test_the_licensed_dialogs_carry_the_enterprise_header() -> None:
    head = EE_DIALOGS.read_text(encoding="utf-8")[:300]
    assert "Celmis Enterprise Edition. Licensed under LICENSE_EE" in head


def test_the_create_dialog_picks_the_workspace_from_the_servers_list_and_types_it_otherwise() -> None:
    dialog = _code(EE_DIALOGS)
    # The list is asked once, without retries: a 404 (multi_tenant) or a 403 (a lapsed
    # licence) is the answer, not a failure to wait out.
    assert "sharingApi.workspaces(token!)" in dialog
    assert re.search(r"sharing-targets.*?retry: false", dialog, flags=re.S)
    # Typed only when the list is refused; picked from the list otherwise.
    assert "const typed = targets.isError" in dialog
    assert re.search(r"\{typed \? \(.*?sharing\.dialog\.workspace\".*?\) : \(.*?sharing\.dialog\.workspacePick",
                     dialog, flags=re.S)
    # The ids of the picker are what is sent.
    assert "workspaceOptions(targets.data ?? [])" in dialog


def test_the_free_components_do_not_reach_into_the_enterprise_folder() -> None:
    for path in (WEB / "components" / "sharing").glob("*.tsx"):
        assert "@/ee/" not in _code(path), path.name


# ─── the limits are the server's ─────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "MAX_SHARES_PER_TOKEN",
        "MIN_PEOPLE",
        "MAX_PEOPLE",
        "DEFAULT_PEOPLE",
        "DEFAULT_DAYS",
        "MAX_DENY_GLOBS",
    ],
)
def test_the_limit_the_form_checks_is_the_limit_the_server_enforces(name: str) -> None:
    shares = importlib.import_module("src.access.shares")
    found = re.search(rf"export const {name} = (\d+);", _code(RULES))
    assert found, f"{name} is gone from web/lib/sharing.ts"
    assert int(found.group(1)) == getattr(shares, name)


# ─── the client calls what the routers serve ─────────────────────────


def _flatten(literal: str) -> str:
    return re.sub(r"\$\{[^}]*\}", "{}", literal).split("?")[0]


def _client_calls() -> set[tuple[str, str]]:
    code = _code(API)
    start = code.index("export const sharingApi = {")
    body = code[start : code.index("\n};", start)]
    calls: set[tuple[str, str]] = set()
    for entry in re.split(r"\n(?=  \w+: )", body)[1:]:
        found = re.search(r'api<[^>]*>\(\s*(`[^`]*`|"[^"]*")', entry)
        assert found, f"no request in: {entry.strip()[:80]}"
        method = re.search(r'method: "(\w+)"', entry)
        calls.add(((method.group(1) if method else "GET"), _flatten(found.group(1)[1:-1])))
    return calls


def _served(module: str) -> set[tuple[str, str]]:
    router = importlib.import_module(module).router
    return {
        (method, re.sub(r"\{[^}]*\}", "{}", route.path))
        for route in router.routes
        for method in route.methods
    }


def test_the_sharing_client_calls_exactly_the_routes_the_routers_serve() -> None:
    calls = _client_calls()
    assert calls == {
        ("GET", "/api/sharing/shares"),
        ("GET", "/api/sharing/shares/{}"),
        ("GET", "/api/sharing/shares/{}/activity"),
        ("PATCH", "/api/sharing/shares/{}"),
        ("POST", "/api/sharing/shares/{}/revoke"),
        ("POST", "/api/sharing-grants"),
        ("POST", "/api/sharing-grants/{}/extend"),
        ("GET", "/api/sharing-grants/workspaces"),
    }
    served = _served("src.api.routers.sharing") | _served("src.ee.sharing.router")
    assert calls <= served, f"called but not served: {calls - served}"


def test_the_token_page_reads_the_shares_and_patches_them_on_the_token() -> None:
    served = _served("src.api.routers.mcp_tokens")
    assert ("GET", "/api/admin/mcp-shares") in served
    assert ("PATCH", "/api/admin/mcp-tokens/{}") in served
    api = _code(API)
    assert "/api/admin/mcp-shares?workspace=" in api
    page = _code(TOKENS_PAGE)
    # Editing a token's shares is the PATCH that replaces the list: no reissue.
    assert "sharesToSave(" in page
    assert "mcpTokensApi.patch(" in page


# ─── the words ───────────────────────────────────────────────────────


def _used_keys() -> set[str]:
    return {key for path in SHARING_FILES for key in _OUR_KEYS.findall(_code(path))}


def test_every_sentence_the_sharing_files_use_exists() -> None:
    missing = sorted(_used_keys() - set(_en()))
    assert not missing, missing


def test_no_sharing_sentence_is_left_without_a_user() -> None:
    ours = {
        k
        for k in _en()
        if k.startswith(("sharing.", "mcpTokens.shared."))
        or k in ("nav.sharing", "mcpTokens.calls.shared")
    }
    unused = sorted(ours - _used_keys())
    assert not unused, unused


def test_the_sharing_sentences_are_translated_not_copied() -> None:
    en = _en()
    ours = sorted(
        k
        for k in en
        if k.startswith(("sharing.", "mcpTokens.shared."))
        or k in ("nav.sharing", "mcpTokens.calls.shared")
    )
    assert len(ours) > 100
    for path in sorted(MESSAGES.glob("*.json")):
        if path.stem == "en":
            continue
        loc = json.loads(path.read_text(encoding="utf-8"))
        same = [k for k in ours if loc[k] == en[k]]
        # A few stay as they are on purpose (the "Q&A" column, "{count} / {max}").
        assert len(same) <= len(ours) * 0.15, (path.stem, same)
        for key in (
            "sharing.title",
            "sharing.intro",
            "sharing.dialog.askWarn",
            "sharing.error.widen",
        ):
            assert loc[key] != en[key], (path.stem, key)


# ─── what the pages say, and how they fit a phone ────────────────────

DETAIL = WEB / "components" / "sharing" / "share-detail.tsx"
NARROW = WEB / "components" / "sharing" / "narrow-dialog.tsx"
SETTINGS_MCP = WEB / "app" / "(app)" / "settings" / "mcp" / "page.tsx"


def test_a_build_with_the_licence_is_not_told_that_narrowing_or_blocking_needs_one() -> None:
    """The two sentences that mention the licence are for a build without it. With the
    licensed routes mounted the page says how to give more (Extend) instead, and the
    choice is made by one helper that the tests of `lib/sharing.ts` pin."""
    detail, narrow = _code(DETAIL), _code(NARROW)
    assert "blockConfirmKey(canGrant)" in detail
    assert "narrowIntroKey(canGrant)" in narrow
    assert re.search(r"<NarrowDialog[^>]*\bcanGrant=\{canGrant\}", detail)
    for text in (detail, narrow):
        assert '"sharing.blockConfirm"' not in text and '"sharing.narrowDialog.intro"' not in text
    en = _en()
    for key in ("sharing.blockConfirm", "sharing.narrowDialog.intro"):
        assert "licence" in en[key]
    for key in ("sharing.blockConfirmLicensed", "sharing.narrowDialog.introLicensed"):
        assert "licence" not in en[key].lower()


@pytest.mark.parametrize("path", [PAGE, DETAIL], ids=["shares", "readers"])
def test_the_lists_of_people_and_shares_are_cards_on_a_phone(path: Path) -> None:
    """A nine-column table does not fit a phone: the table is drawn from the medium
    breakpoint up and a list of cards below it, each carrying every column."""
    code = _code(path)
    assert 'className="hidden md:block"' in code
    assert re.search(r'<ul className="[^"]*\bmd:hidden', code)


def test_the_create_dialog_binds_every_label_and_scolds_only_after_a_change() -> None:
    dialog = _code(EE_DIALOGS)
    assert re.search(r'<Label htmlFor="share-repo">.*?<Select id="share-repo"', dialog, flags=re.S)
    # A form that has just opened is not an error.
    assert "const [touched, setTouched] = useState(false)" in dialog
    assert "setTouched(true)" in dialog
    assert re.search(r"\{touched && problem && \(", dialog)
    # The two number fields share their rows, so their boxes line up.
    assert dialog.count("className={NUMBER_ROW}") == 2
    assert dialog.count("className={NUMBER_FIELD}") == 4


def test_a_person_sees_the_shares_on_their_own_token() -> None:
    """`/api/mcp/tokens/me` lists the shares a token carries; the page that lists the
    person's tokens must not show a token that reads shared repositories as if it
    read only its own."""
    page = _code(SETTINGS_MCP)
    assert re.search(r"row\.shares \?\? \[\]\)\.length > 0", page)
    assert '"mcpTokens.shared.chip"' in page
    assert "sh.owner_name" in page
