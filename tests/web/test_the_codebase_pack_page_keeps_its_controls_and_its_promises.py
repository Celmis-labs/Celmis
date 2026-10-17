"""What the pack page and the upload dialog promise, held against the code.

* Maintainer controls (upload, delete, the Sharing and Usage tabs, the team,
  person and invite editors) are drawn only for a maintainer. The API refuses
  everybody else anyway; a button that can only fail is a worse page, and a
  Sharing tab that renders for a reader would show them a members list the API
  never sent.
* Destructive actions ask first (delete a pack, make an older version current,
  remove a person, revoke a link).
* An upload goes through `requestHeaders`, so the active-workspace hint travels
  with it (a hand-rolled bearer silently resolves to the account's DEFAULT
  workspace — the bug `test_download_workspace_header` exists for), it can be
  cancelled, shows its progress, refuses an oversize pick before sending it,
  and shows the server's refusal with its places rather than a bare message.
* The shared error reader turns a structured `detail` into its sentence and
  leaves every older shape exactly as it was.

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
    _lift,
    _strip_comments,
)
from tests.web.test_the_seams_between_page_and_server_hold import _compile_and_run

API = WEB / "lib" / "api.ts"
PAGE = WEB / "app" / "(app)" / "codebase" / "packs" / "[id]" / "page.tsx"
COMPONENTS = WEB / "components" / "codebase"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


# ─── who sees which control ──────────────────────────────────────────


def test_upload_and_delete_are_drawn_only_for_a_maintainer():
    code = _code(PAGE)
    assert "const maintainer = data?.can_maintain === true" in code
    for needle in ("codebase.upload.open", "<Trash2Icon />"):
        before = code[:code.index(needle)]
        guard = before.rfind("{maintainer && (")
        assert guard != -1 and "</Button>" not in before[guard:], (
            f"{needle} is not directly inside a maintainer-only block")


def test_sharing_and_usage_exist_only_for_a_maintainer():
    code = _code(PAGE)
    assert 'maintainer ? ["content", "versions", "sharing", "usage"] : ["content", "versions"]' in code
    assert 'active === "sharing" && maintainer' in code
    assert 'active === "usage" && maintainer' in code
    # A deep link to a tab the person does not have falls back, it does not 404.
    assert 'tabs.includes(tab) ? tab : "content"' in code


def test_the_content_tab_offers_an_upload_only_to_whoever_may_upload():
    code = _code(PAGE)
    assert "onUpload={maintainer ? () => setUploading(true) : undefined}" in code
    tab = _code(COMPONENTS / "content-tab.tsx")
    assert "onUpload ? t(" in tab, "the empty state must differ for readers"


# ─── asking first ────────────────────────────────────────────────────

CONFIRMS = {
    "packs/[id]/page.tsx": ("codebase.pack.confirmDelete",),
    "versions-tab.tsx": ("codebase.versions.confirmActivate",),
    "sharing-tab.tsx": ("codebase.sharing.confirmRemove", "codebase.sharing.confirmRevoke",
                        "codebase.tokens.confirmRevoke"),
}


@pytest.mark.parametrize("name,keys", CONFIRMS.items())
def test_a_destructive_action_asks_before_it_acts(name, keys):
    path = PAGE if name.startswith("packs/") else COMPONENTS / name
    code = _code(path)
    assert "useConfirm()" in code
    for key in keys:
        at = code.index(key)
        ask = code.rfind("await confirm(", 0, at)
        assert ask != -1, key
        assert re.search(r"if \(!?ok\)", code[at:at + 600]), f"{key}: the answer is never read"


# ─── the upload ──────────────────────────────────────────────────────


def _upload_function() -> str:
    code = _code(API)
    start = code.index("export function uploadPackVersion(")
    return code[start:code.index("\nexport const codebaseApi", start)]


def test_the_upload_carries_the_workspace_hint_and_no_hand_made_bearer():
    body = _upload_function()
    assert "requestHeaders(token)" in body
    assert "Authorization" not in body and "Bearer" not in body


def test_the_upload_reports_progress_and_can_be_cancelled():
    body = _upload_function()
    assert "xhr.upload.onprogress" in body
    assert "lengthComputable" in body
    assert 'new DOMException("Upload cancelled", "AbortError")' in body
    assert "opts.signal?.aborted" in body, "an already-cancelled upload must not be sent"
    assert "removeEventListener" in body or '{ once: true }' in body


def test_the_upload_rejects_with_the_error_class_the_pages_already_handle():
    body = _upload_function()
    assert "new ApiError(xhr.status, body," in body
    assert "errorMessage(body," in body


def test_the_dialog_refuses_what_the_server_would_before_sending_it():
    code = _code(COMPONENTS / "upload-version.tsx")
    assert "const tooBig = bytes > UPLOAD_LIMITS.archiveBytes" in code
    assert "(folder?.kept ?? 0) > UPLOAD_LIMITS.files" in code     # only files the server keeps count
    assert "disabled={!picked || tooBig || tooMany || !token}" in code, (
        "Upload must be unavailable for an oversize pick, not merely warn about it")


def test_the_dialog_picks_a_zip_or_a_folder_and_sends_each_name_as_its_path():
    code = _code(COMPONENTS / "upload-version.tsx")
    assert "webkitdirectory" in code
    assert "accept=\".zip,application/zip\"" in code
    assert "AbortController" in code and "controller.current?.abort()" in code
    assert 'role="progressbar"' in code
    assert "<IngestError error={upload.error} />" in code
    api = _code(API)
    assert 'form.append("files", file, relativePathOf(file))' in api
    # Folders are named in every language, and a file name in a part header is
    # not read as UTF-8 everywhere; the same path as a plain field always is.
    assert 'form.append("paths", relativePathOf(file))' in api
    assert 'form.append("archive", input.archive, input.archive.name)' in api


def test_closing_the_dialog_mid_upload_cancels_the_request():
    """The form is rendered only while the dialog is open, so closing it unmounts
    the form; the cleanup then aborts the request instead of leaving it running
    behind a screen that no longer shows it. While it runs, Cancel says so."""
    code = _code(COMPONENTS / "upload-version.tsx")
    assert "useEffect(() => () => controller.current?.abort(), [])" in code
    assert "<UploadForm packId={packId} onClose={() => onOpenChange(false)} />" in code
    assert "onClick={() => controller.current?.abort()}" in code


def test_a_refused_upload_shows_the_code_the_message_and_the_places():
    code = _code(COMPONENTS / "ingest-error.tsx")
    assert "refusalOf(error)" in code
    assert "refusal.code" in code and "refusal.message" in code
    assert "hitLabel(hit)" in code
    # Nothing but the three fields of a hit is ever rendered.
    assert "hit.match" not in code and "hit.text" not in code and "hit.value" not in code


# ─── the shared error reader ─────────────────────────────────────────

DETAILS = {
    "a plain sentence": ({"detail": "Requires a pack maintainer"}, "Requires a pack maintainer"),
    "a structured refusal": (
        {"detail": {"code": "no_files", "message": "No usable text files were found.", "hits": []}},
        "No usable text files were found."),
    "validation errors": (
        {"detail": [{"loc": ["body", "note"], "msg": "too long"},
                    {"loc": ["body", "teams", 0, "permission"], "msg": "bad value"},
                    {"loc": [], "msg": "no place"}]},
        "note: too long; teams.0.permission: bad value; no place"),
    "no detail at all": ({"error": "x"}, "FALLBACK"),
    "not an object": ("<html>bad gateway</html>", "FALLBACK"),
    "nothing": (None, "FALLBACK"),
    "an empty list": ({"detail": []}, ""),
    "a number": ({"detail": 429}, "429"),
}


def test_the_error_reader_gives_every_shape_of_detail_its_sentence():
    if not TSC.exists():
        pytest.skip("web deps not installed")
    lifted = _lift("errorMessage", _code(API), API)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        got = _compile_and_run(
            Path(tmp), {},
            lifted + "\n\n"
            f"const bodies: any[] = {json.dumps([v[0] for v in DETAILS.values()])};\n"
            "console.log(JSON.stringify(bodies.map((b) => errorMessage(b, 'FALLBACK'))));\n",
        )
    assert dict(zip(DETAILS, got, strict=True)) == {k: v[1] for k, v in DETAILS.items()}


def test_the_shared_client_still_throws_the_body_it_was_given():
    """The structured refusal is read from `ApiError.body`; the sentence is only
    for places that show a message."""
    code = _code(API)
    assert "throw new ApiError(resp.status, body, errorMessage(body," in code
