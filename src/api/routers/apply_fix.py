"""Apply-suggested-fix — turns a Finding.suggestion into a concrete change
on the underlying git provider.

Approach (MVP):
  * User POSTs the finding payload (file, line range, replacement text) +
    PR ref. We DO NOT re-derive it server-side — the UI has it already,
    and passing it back keeps this endpoint stateless.
  * We branch from the PR head, apply the replacement, and open ONE new
    commit on `celmis-fix/<pr>-<n>`. We then post a comment on the
    PR linking to the commit and quoting the diff.
  * GitHub only for now; GitLab support ships in a follow-up (same
    interface, different API client). We short-circuit with 501 for
    other providers so the UI can hide the button.

Auth:
  * Reads the workspace's stored provider connection (same store
    /connections uses).
  * Requires the user's provider token to have `repo:write` scope
    (GitHub) / equivalent — we don't verify upfront, just surface the
    provider's error.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import current_workspace_id, get_current_user
from src.http import build_client
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reviews", tags=["apply-fix"])


class ApplyFixIn(BaseModel):
    provider: str = Field(pattern="^(github|gitlab|bitbucket)$")
    repo: str = Field(min_length=1, max_length=200)   # owner/name
    pr_number: int = Field(ge=1)
    head_ref: str = Field(min_length=1)               # PR source branch name
    head_sha: str = Field(min_length=1)               # PR head commit sha
    file_path: str = Field(min_length=1, max_length=1000)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    replacement: str = Field(min_length=0, max_length=100_000)
    finding_id: str = Field(default="", max_length=200)
    commit_message: str | None = Field(default=None, max_length=200)

    model_config = ConfigDict(extra="forbid")


class ApplyFixOut(BaseModel):
    ok: bool
    commit_sha: str | None = None
    commit_url: str | None = None
    branch: str | None = None
    detail: str
    #: What the post-apply check could say. `applied_check_silent` means the
    #: rule stopped matching — a fact about the check, NOT a promise that the
    #: code is fixed. `still_fires` is the one that matters: the patch landed
    #: and the finding is still there, which the product used to report as
    #: plain success.
    check_state: str = ""
    check_reason: str = ""


def _load_provider_token(provider: str, user_id: str, workspace_id: str = "default") -> str | None:
    from src.credentials import resolve_git_credential
    from src.credentials.store import CredentialStoreError

    try:
        row = resolve_git_credential(provider, user_id=user_id, workspace_id=workspace_id)
    except CredentialStoreError:
        return None
    return row.secret if row else None


@router.post("/{run_id}/apply-fix", response_model=ApplyFixOut)
def apply_fix(
    run_id: str,
    payload: ApplyFixIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ApplyFixOut:
    if payload.provider != "github":
        raise HTTPException(
            status_code=501,
            detail=f"{payload.provider} apply-fix not implemented yet",
        )

    token = _load_provider_token("github", user.id, workspace_id)
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No GitHub connection — add a token on /connections",
        )
    return _apply_fix_github(run_id, payload, token=token, user=user)


def _github_token_kind(token: str) -> str:
    """Classic vs fine-grained PAT, from the documented prefixes.

    GitHub's 403 body is identical for both ("Resource not accessible by
    personal access token"), but the remedy is not — classic needs an OAuth
    *scope*, fine-grained needs a repository *permission*. The prefix is the
    only way to tell them apart without an extra round-trip.
    """
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith(("ghp_", "gho_", "ghu_")):
        return "classic"
    if token.startswith("ghs_"):
        return "app"
    return "unknown"


def _reindent(replacement: str, replaced_line: str) -> str:
    """Give the replacement the indentation of the line it replaces.

    The suggestion arrives from a rule pack as a bare snippet with no leading
    whitespace, and it was spliced in at column 0 over an indented line. In
    Python that silently terminates the enclosing block; everywhere else it
    just reads as damage. Only the leading run of whitespace is copied, and
    only onto lines that carry none of their own, so a multi-line suggestion
    keeps its own internal shape.
    """
    indent = replaced_line[:len(replaced_line) - len(replaced_line.lstrip())]
    if not indent or not replacement.strip():
        return replacement
    out = []
    for line in replacement.splitlines():
        out.append(line if (not line.strip() or line[:1] in " \t")
                   else indent + line)
    return "\n".join(out)


@dataclass(frozen=True)
class _PatchCheck:
    """What we can honestly say about the patch we just built.

    `applied_check_silent` is deliberately not called "verified". A rule can
    stop matching because the fix worked, or because the code it matched on
    was deleted — `def load(items=[])` replaced by `def f(x=None): x = x or []`
    parses cleanly and stops matching, having renamed and emptied the
    function. The name states a fact about the CHECK, which is all we have.
    """

    state: str            # refused_broke_file | still_fires | applied_unchecked
                          # | applied_check_silent
    reason: str = ""
    language: str = ""


def _check_patch(p: ApplyFixIn, original: str, patched: str) -> _PatchCheck:
    """Parse the patched file, and re-run the rule that produced the finding.

    Differential on purpose: a file that did not parse BEFORE we touched it is
    not evidence against this patch, and refusing there would block fixes to
    exactly the files most likely to need them.
    """
    from src.review import structural

    language = structural.language_for(p.file_path)
    if language is None:
        return _PatchCheck("applied_unchecked", "unsupported file type")

    before_ok, _ = structural.parses(original, language)
    after_ok, reason = structural.parses(patched, language)
    if before_ok and not after_ok:
        return _PatchCheck("refused_broke_file", reason, language)

    rule = structural.rule_by_id(p.finding_id)
    if rule is None:
        return _PatchCheck("applied_unchecked", "no rule behind this finding",
                           language)
    end = p.line_start + p.replacement.count("\n")
    if structural.rule_still_matches(rule, patched, language,
                                     line_start=p.line_start, line_end=end):
        return _PatchCheck("still_fires", f"rule {rule.id} still matches",
                           language)
    return _PatchCheck("applied_check_silent", "", language)


def _gh_message(resp: httpx.Response) -> str:
    """GitHub's own one-line reason, or nothing.

    `resp.text[:200]` put the provider's JSON — braces, `documentation_url`,
    nested `errors` — straight into a toast. GitHub always supplies a plain
    `message`; that is the only part a person can act on, and the full body
    belongs in the log.
    """
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return "see server log"
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
        if message:
            return message[:160]
    return "see server log"


def _github_write_denied(resp: httpx.Response, *, token: str, repo: str, action: str) -> str:
    """Turn GitHub's opaque write-denial into instructions the user can act on.

    Read endpoints on a public repo succeed with almost any token, so the first
    thing that actually exercises write permission is this call — which is why
    the failure only ever shows up at apply-fix time and reads like a bug.
    """
    kind = _github_token_kind(token)
    # GitHub names the exact permission/scope it wanted in these headers.
    needed_perm = resp.headers.get("x-accepted-github-permissions", "")
    needed_scope = resp.headers.get("x-accepted-oauth-scopes", "")
    have_scope = resp.headers.get("x-oauth-scopes", "")

    if kind == "fine-grained":
        remedy = (
            f"Your GitHub token is fine-grained and has no write access to {repo}. "
            "Open GitHub → Settings → Developer settings → Fine-grained tokens → "
            f"edit the token → Repository access must include {repo}, and set "
            "Permissions → Contents to 'Read and write' (Pull requests: 'Read and "
            "write' too, so Celmis can comment). Then re-save the token in Celmis."
        )
    elif kind == "classic":
        remedy = (
            f"Your GitHub token is classic and lacks the scope needed to write to {repo}. "
            "Open GitHub → Settings → Developer settings → Tokens (classic) → edit the "
            "token → tick 'repo' (or 'public_repo' for public repositories only). "
            "Then re-save the token in Celmis."
        )
    else:
        remedy = (
            f"The stored GitHub token has no write access to {repo}. A fine-grained "
            "token needs Contents: 'Read and write'; a classic token needs the 'repo' "
            "scope. Update it and re-save the token in Celmis."
        )

    hint = ""
    if needed_perm:
        hint = f" GitHub requires permission: {needed_perm}."
    elif needed_scope:
        hint = f" GitHub requires scope: {needed_scope}."
        if have_scope:
            hint += f" Token currently has: {have_scope}."

    return f"Cannot {action} — {remedy}{hint}"


def _apply_fix_github(
    run_id: str,
    p: ApplyFixIn,
    *,
    token: str,
    user: User,
) -> ApplyFixOut:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{p.repo}"

    with build_client(timeout=15.0) as http:
        # 0) Preflight the token's write access. Every step below is a read
        #    until step 3, so without this the user waits through three
        #    round-trips before finding out the token was never going to work.
        #    For PATs, GitHub reports the *token's* effective permissions here.
        repo_info = http.get(api, headers=headers)
        if repo_info.status_code == 200:
            perms = repo_info.json().get("permissions") or {}
            if perms and not perms.get("push"):
                raise HTTPException(
                    status_code=403,
                    detail=_github_write_denied(
                        repo_info, token=token, repo=p.repo,
                        action="commit the fix",
                    ),
                )
        elif repo_info.status_code in (401, 403, 404):
            raise HTTPException(
                status_code=502,
                detail=f"Cannot read {p.repo} ({repo_info.status_code}) — check that the "
                       "stored GitHub token grants access to this repository.",
            )

        # 1) Read the file on head_ref.
        r = http.get(
            f"{api}/contents/{p.file_path}", headers=headers,
            params={"ref": p.head_ref},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502,
                                detail=f"file fetch failed: {r.text[:200]}")
        blob = r.json()
        content = base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        file_sha = blob["sha"]

        # 2) Splice line range.
        lines = content.splitlines(keepends=True)
        if p.line_start < 1 or p.line_end < p.line_start or p.line_end > len(lines):
            raise HTTPException(status_code=400, detail="line range out of bounds")
        replacement = _reindent(p.replacement, lines[p.line_start - 1])
        new_lines = (
            lines[: p.line_start - 1]
            + [replacement if replacement.endswith("\n") else replacement + "\n"]
            + lines[p.line_end:]
        )
        new_content = "".join(new_lines)

        # 2b) Look at what we just built, BEFORE creating a branch.
        #
        # This endpoint used to write the replacement into the file, commit it,
        # and comment "applied" on the pull request without ever reading the
        # result. Every structural rule ships PROSE as its suggestion —
        # "except Exception:", "logger.exception('...') or raise",
        # "=== / !==" — and the UI sends that string as the replacement, so
        # pressing the button committed syntactically broken code into
        # somebody's repository and reported success.
        #
        # The gate sits here rather than after `create_ref` because a refusal
        # further down would leave an orphan `celmis-fix/*` branch in the
        # customer's repo on every rejection.
        check = _check_patch(p, content, new_content)
        if check.state == "refused_broke_file":
            raise HTTPException(
                status_code=422,
                detail=(f"That suggestion does not produce valid "
                        f"{check.language}: {check.reason}. It reads as guidance "
                        f"rather than code — edit it before applying."),
            )

        # 3) Create branch off head_sha.
        # finding_id comes from the engine and is not guaranteed ref-safe; an
        # illegal ref name would 422 here and be misread as "branch exists".
        # File and line are in the slug because a finding id is a RULE id —
        # two `print-debug` findings in one pull request collided on one
        # branch, so the second commit landed on top of the first one's file.
        slug_src = f"{p.finding_id}-{p.file_path}-{p.line_start}"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug_src).strip("-.") or "suggestion"
        branch = f"celmis-fix/{p.pr_number}-{slug[:80]}"
        create_ref = http.post(
            f"{api}/git/refs", headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": p.head_sha},
        )
        branch_reused = False
        if create_ref.status_code == 422:
            # Branch exists — reuse.
            branch_reused = True
            logger.info("apply_fix_branch_reused branch=%s", branch)
        elif create_ref.status_code in (401, 403, 404):
            raise HTTPException(
                status_code=403,
                detail=_github_write_denied(
                    create_ref, token=token, repo=p.repo,
                    action=f"create branch {branch}",
                ),
            )
        elif create_ref.status_code >= 300:
            raise HTTPException(status_code=502,
                                detail=f"branch create failed: {create_ref.text[:200]}")

        # 3b) The branch was reused, so the blob sha read from head_ref in step
        # 1 is stale — the earlier attempt already committed a different blob
        # there. GitHub's contents API wants the sha of the file AS IT EXISTS
        # ON THE TARGET BRANCH and answers 409 "<path> does not match <sha>"
        # otherwise. The branch was reused; the sha was not. That is why the
        # ordinary "pressed it, nothing happened, pressed again" flow could
        # never succeed.
        already_applied = False
        if branch_reused:
            on_branch = http.get(
                f"{api}/contents/{p.file_path}", headers=headers,
                params={"ref": branch},
            )
            if on_branch.status_code == 200:
                current = on_branch.json()
                file_sha = current["sha"]
                existing = base64.b64decode(
                    current["content"]).decode("utf-8", errors="replace")
                # A retry after a success must not splice the fix in twice.
                already_applied = existing == new_content
            elif on_branch.status_code == 404:
                # Branch exists but the file does not (deleted there). Creating
                # it needs NO sha at all; sending a stale one is a guaranteed
                # 409.
                file_sha = None

        # 4) Commit the modified file to that branch.
        commit_msg = p.commit_message or (
            f"Apply Celmis suggestion for {p.file_path}:{p.line_start}"
        )
        if already_applied:
            # Pressing the button twice is the commonest thing a person does
            # when the first press seemed to do nothing. Committing an
            # identical blob would be a no-op commit at best; reporting a
            # failure would be a lie. Say what is true: it is already there.
            logger.info("apply_fix_noop branch=%s file=%s — already applied",
                        branch, p.file_path)
            return ApplyFixOut(
                ok=True, commit_sha=None,
                commit_url=f"https://github.com/{p.repo}/tree/{branch}",
                branch=branch,
                detail="already applied on this branch",
                check_state=check.state, check_reason=check.reason,
            )
        body: dict[str, Any] = {
            "message": commit_msg,
            "content": base64.b64encode(new_content.encode()).decode(),
            "branch": branch,
            "committer": {"name": "Celmis", "email": "bot@celmis.local"},
        }
        # Creating a file takes no sha; sending a null one is rejected.
        if file_sha:
            body["sha"] = file_sha
        put = http.put(
            f"{api}/contents/{p.file_path}", headers=headers, json=body,
        )
        if put.status_code in (401, 403, 404):
            raise HTTPException(
                status_code=403,
                detail=_github_write_denied(
                    put, token=token, repo=p.repo,
                    action=f"commit to {p.file_path}",
                ),
            )
        if put.status_code == 409:
            # A sha conflict, which now means the branch moved between the read
            # two lines up and this write — somebody else pushed. The raw body
            # is GitHub's JSON and used to be dumped into a toast.
            logger.warning("apply_fix_conflict branch=%s file=%s body=%s",
                           branch, p.file_path, put.text[:300])
            raise HTTPException(
                status_code=409,
                detail=(f"`{branch}` changed while this fix was being applied. "
                        f"Open it, check the file, and apply again."),
            )
        if put.status_code >= 300:
            logger.warning("apply_fix_commit_failed status=%s body=%s",
                           put.status_code, put.text[:300])
            raise HTTPException(
                status_code=502,
                detail=f"commit failed ({put.status_code}): {_gh_message(put)}",
            )
        commit_sha = put.json().get("commit", {}).get("sha")
        commit_url = put.json().get("commit", {}).get("html_url")

        # 5) Comment on the PR pointing at the new branch.
        still = check.state == "still_fires"
        comment_body = (
            f"{'⚠️' if still else '🔧'} Celmis suggestion applied by "
            f"{user.email} on [`{branch}`]({commit_url or '#'})\n\n"
            + ("**The finding still matches after this change** — the patch "
               "landed, but it did not resolve what was reported. Review it "
               "before merging.\n\n" if still else "")
            + f"To merge: `git checkout {branch} && git push` "
              f"(or open a new PR from `{branch}`)."
        )
        http.post(
            f"{api}/issues/{p.pr_number}/comments", headers=headers,
            json={"body": comment_body},
        )

    # The check state rides the log line too: it answers "which rules produce
    # suggestions that break files" on day one, with no schema change.
    logger.info("apply_fix_ok run=%s pr=%s branch=%s sha=%s check=%s rule=%s by=%s",
                run_id, p.pr_number, branch, commit_sha, check.state,
                p.finding_id, user.email)
    return ApplyFixOut(
        ok=True, commit_sha=commit_sha, commit_url=commit_url, branch=branch,
        detail=("committed, but the finding still matches"
                if check.state == "still_fires" else "patch committed to branch"),
        check_state=check.state, check_reason=check.reason,
    )


def apply_replacement_on_default_branch(
    *,
    repo_slug: str,      # owner/name (github)
    file_path: str,
    line: int,
    old_text: str,
    new_text: str,
    user_id: str,
    workspace_id: str = "default",
    branch_name: str,
    commit_message: str,
) -> dict[str, Any]:
    """Bulk-migration helper: create a branch off the repo's default
    branch, replace `old_text` with `new_text` at the given line, commit,
    and open a PR body-less (caller adds description). Returns dict
    with keys: status, branch, commit_url, pr_url (if PR opened), reason.

    Non-raising. This is called by the MCP tool `migrate_consumers` —
    every error is captured so the fan-out doesn't abort.
    """
    token = _load_provider_token("github", user_id, workspace_id)
    if not token:
        return {"status": "skipped", "reason": "no github token"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api = f"https://api.github.com/repos/{repo_slug}"

    with build_client(timeout=15.0) as http:
        # Discover default branch + its head sha.
        info = http.get(api, headers=headers)
        if info.status_code != 200:
            return {"status": "skipped",
                    "reason": f"repo lookup failed {info.status_code}"}
        default = info.json().get("default_branch") or "main"
        ref = http.get(f"{api}/git/ref/heads/{default}", headers=headers)
        if ref.status_code != 200:
            return {"status": "skipped",
                    "reason": f"default ref lookup failed {ref.status_code}"}
        base_sha = ref.json().get("object", {}).get("sha")
        if not base_sha:
            return {"status": "skipped", "reason": "no default sha"}

        # Read the file at default branch.
        f = http.get(f"{api}/contents/{file_path}", headers=headers,
                     params={"ref": default})
        if f.status_code != 200:
            return {"status": "skipped",
                    "reason": f"file fetch failed {f.status_code}"}
        blob = f.json()
        file_sha = blob["sha"]
        content = base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        idx = line - 1
        if idx < 0 or idx >= len(lines):
            return {"status": "skipped", "reason": "line out of bounds"}
        if old_text not in lines[idx]:
            return {"status": "skipped",
                    "reason": f"old_text not found at {file_path}:{line}"}
        lines[idx] = lines[idx].replace(old_text, new_text)
        new_content = "".join(lines)

        # Create branch (idempotent — 422 = exists).
        cb = http.post(f"{api}/git/refs", headers=headers,
                       json={"ref": f"refs/heads/{branch_name}", "sha": base_sha})
        if cb.status_code not in (201, 422):
            return {"status": "skipped",
                    "reason": f"branch create failed {cb.status_code}"}

        # Commit the file to the branch.
        put = http.put(
            f"{api}/contents/{file_path}", headers=headers,
            json={
                "message": commit_message,
                "content": base64.b64encode(new_content.encode()).decode(),
                "sha": file_sha,
                "branch": branch_name,
                "committer": {"name": "Celmis", "email": "bot@celmis.local"},
            },
        )
        if put.status_code >= 300:
            return {"status": "skipped",
                    "reason": f"commit failed {put.status_code}"}
        commit_sha = put.json().get("commit", {}).get("sha")
        commit_url = put.json().get("commit", {}).get("html_url")

        # Open PR (idempotent-ish — if already exists we ignore).
        pr = http.post(f"{api}/pulls", headers=headers, json={
            "title": commit_message,
            "head": branch_name, "base": default,
            "body": ("Automated migration by Celmis. "
                     "Review carefully before merging."),
        })
        pr_url = None
        if pr.status_code in (200, 201):
            pr_url = pr.json().get("html_url")
    return {
        "status": "ok",
        "branch": branch_name,
        "commit_sha": commit_sha,
        "commit_url": commit_url,
        "pr_url": pr_url,
    }


def apply_replacement_on_default_branch_gitlab(
    *,
    repo_slug: str,      # namespace/project (url-encoded on the wire)
    file_path: str,
    line: int,
    old_text: str,
    new_text: str,
    user_id: str,
    workspace_id: str = "default",
    branch_name: str,
    commit_message: str,
) -> dict[str, Any]:
    """Same shape as the GitHub variant, GitLab REST v4."""
    import urllib.parse
    token = _load_provider_token("gitlab", user_id, workspace_id)
    if not token:
        return {"status": "skipped", "reason": "no gitlab token"}
    headers = {"PRIVATE-TOKEN": token}
    proj = urllib.parse.quote_plus(repo_slug)
    api = f"https://gitlab.com/api/v4/projects/{proj}"

    with build_client(timeout=15.0) as http:
        info = http.get(api, headers=headers)
        if info.status_code != 200:
            return {"status": "skipped",
                    "reason": f"project lookup failed {info.status_code}"}
        default = info.json().get("default_branch") or "main"

        # Read file — base64-encoded in the wire response.
        f_url = f"{api}/repository/files/{urllib.parse.quote_plus(file_path)}"
        f = http.get(f_url, headers=headers, params={"ref": default})
        if f.status_code != 200:
            return {"status": "skipped",
                    "reason": f"file fetch failed {f.status_code}"}
        raw = base64.b64decode(f.json().get("content", "")).decode("utf-8", errors="replace")
        lines = raw.splitlines(keepends=True)
        idx = line - 1
        if idx < 0 or idx >= len(lines):
            return {"status": "skipped", "reason": "line out of bounds"}
        if old_text not in lines[idx]:
            return {"status": "skipped",
                    "reason": f"old_text not at {file_path}:{line}"}
        lines[idx] = lines[idx].replace(old_text, new_text)
        new_content = "".join(lines)

        # Create branch (idempotent — 400 if exists).
        cb = http.post(
            f"{api}/repository/branches", headers=headers,
            params={"branch": branch_name, "ref": default},
        )
        if cb.status_code not in (201, 400):
            return {"status": "skipped",
                    "reason": f"branch create failed {cb.status_code}"}

        # Update the file on the new branch.
        put = http.put(
            f_url, headers=headers,
            json={
                "branch": branch_name,
                "content": new_content,
                "commit_message": commit_message,
            },
        )
        if put.status_code >= 300:
            return {"status": "skipped",
                    "reason": f"commit failed {put.status_code}: {put.text[:150]}"}

        # Open MR.
        mr = http.post(
            f"{api}/merge_requests", headers=headers,
            json={
                "source_branch": branch_name, "target_branch": default,
                "title": commit_message,
                "description": "Automated migration by Celmis.",
                "remove_source_branch": True,
            },
        )
        mr_url = mr.json().get("web_url") if mr.status_code in (200, 201) else None
    return {
        "status": "ok",
        "branch": branch_name,
        "commit_sha": None,       # gitlab file-update API does not return commit sha directly
        "commit_url": None,
        "pr_url": mr_url,
    }


def apply_replacement_on_default_branch_bitbucket(
    *,
    repo_slug: str,      # workspace/repo
    file_path: str,
    line: int,
    old_text: str,
    new_text: str,
    user_id: str,
    workspace_id: str = "default",
    branch_name: str,
    commit_message: str,
) -> dict[str, Any]:
    """Bitbucket Cloud 2.0. Uses Basic auth (email:app_password) stored as
    single secret with `auth_scheme=basic`; falls back to Bearer for
    workspace access tokens."""
    from src.credentials import resolve_git_credential
    from src.credentials.store import CredentialStoreError
    try:
        cred = resolve_git_credential("bitbucket", user_id=user_id, workspace_id=workspace_id)
    except CredentialStoreError:
        cred = None
    if cred is None:
        return {"status": "skipped", "reason": "no bitbucket credentials"}
    meta = cred.metadata or {}
    if meta.get("auth_scheme") == "basic":
        import base64 as _b64
        blob = _b64.b64encode(f"{meta.get('username','')}:{cred.secret}".encode()).decode()
        auth_header = f"Basic {blob}"
    else:
        auth_header = f"Bearer {cred.secret}"
    headers = {"Authorization": auth_header}
    api = f"https://api.bitbucket.org/2.0/repositories/{repo_slug}"

    with build_client(timeout=15.0) as http:
        info = http.get(api, headers=headers)
        if info.status_code != 200:
            return {"status": "skipped",
                    "reason": f"repo lookup failed {info.status_code}"}
        default = (info.json().get("mainbranch") or {}).get("name") or "main"

        # Read raw file at default.
        f = http.get(f"{api}/src/{default}/{file_path}", headers=headers)
        if f.status_code != 200:
            return {"status": "skipped",
                    "reason": f"file fetch failed {f.status_code}"}
        raw = f.text
        lines = raw.splitlines(keepends=True)
        idx = line - 1
        if idx < 0 or idx >= len(lines):
            return {"status": "skipped", "reason": "line out of bounds"}
        if old_text not in lines[idx]:
            return {"status": "skipped",
                    "reason": f"old_text not at {file_path}:{line}"}
        lines[idx] = lines[idx].replace(old_text, new_text)
        new_content = "".join(lines)

        # Bitbucket writes commits via multipart POST /src with the branch
        # arg — this both creates the branch (if missing) AND commits.
        write = http.post(
            f"{api}/src", headers=headers,
            data={
                "branch": branch_name,
                "message": commit_message,
                file_path: new_content,
            },
        )
        if write.status_code >= 300:
            return {"status": "skipped",
                    "reason": f"commit failed {write.status_code}: {write.text[:150]}"}

        # Open PR.
        pr = http.post(
            f"{api}/pullrequests", headers=headers,
            json={
                "title": commit_message,
                "source": {"branch": {"name": branch_name}},
                "destination": {"branch": {"name": default}},
                "description": "Automated migration by Celmis.",
                "close_source_branch": True,
            },
        )
        pr_url = None
        if pr.status_code in (200, 201):
            pr_url = (pr.json().get("links") or {}).get("html", {}).get("href")
    return {
        "status": "ok",
        "branch": branch_name,
        "commit_sha": None,
        "commit_url": None,
        "pr_url": pr_url,
    }


__all__ = [
    "router",
    "apply_replacement_on_default_branch",
    "apply_replacement_on_default_branch_gitlab",
    "apply_replacement_on_default_branch_bitbucket",
]
