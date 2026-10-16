"""What a git token must be allowed to do, and what a given token is missing.

One catalogue, read by the Connections page (the list shown next to every
token form), by the verify endpoints (which compare it with the scopes the
provider reports for the token) and by the agent's knowledge. Each entry says
what to tick, as the provider's token page names it, and what breaks
without it — so "Verify" can answer "missing read:user:bitbucket: re-runs
cannot remove their old comments" instead of "OK".

Where the scopes come from:
    GitHub classic PAT   — the `X-OAuth-Scopes` response header.
    GitHub fine-grained  — not enumerable; the list is shown, not checked.
    GitLab               — GET /personal_access_tokens/self → `scopes`.
    Bitbucket            — the `x-oauth-scopes` response header (API tokens
                           report `read:user:bitbucket`-style names, access
                           tokens the classic `pullrequest:write` ones).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScopeNeed:
    #: Stable id; the UI translates `connections.scope.<key>` for `purpose`.
    key: str
    #: What to tick, exactly as the provider's token page names it.
    label: str
    #: Granted scope names that satisfy this need (any one is enough).
    any_of: tuple[str, ...]
    #: False: only one feature needs it (named in `purpose`).
    required: bool
    #: What breaks without it, in English (the fallback of the i18n key).
    purpose: str


_BB_API = (
    ScopeNeed("bb_user", "read:user:bitbucket", ("read:user:bitbucket",), True,
              "Celmis recognises its own comments: re-runs update the summary "
              "and remove their old inline comments instead of duplicating "
              "them, and approve / request changes can be withdrawn."),
    ScopeNeed("bb_workspace", "read:workspace:bitbucket",
              ("read:workspace:bitbucket",), True,
              "Read the workspace and list its repositories."),
    ScopeNeed("bb_repo_read", "read:repository:bitbucket",
              ("read:repository:bitbucket",), True,
              "Clone and index repositories, read diffs."),
    ScopeNeed("bb_pr_read", "read:pullrequest:bitbucket",
              ("read:pullrequest:bitbucket",), True,
              "List and read pull requests (manual reviews and polling)."),
    ScopeNeed("bb_pr_write", "write:pullrequest:bitbucket",
              ("write:pullrequest:bitbucket",), True,
              "Post review comments, approve and request changes."),
    ScopeNeed("bb_repo_write", "write:repository:bitbucket",
              ("write:repository:bitbucket",), False,
              "\"Apply fix\": push the suggested change as a commit."),
    ScopeNeed("bb_webhook", "read:webhook:bitbucket + write:webhook:bitbucket",
              ("write:webhook:bitbucket",), False,
              "\"Install webhook\": instant reviews instead of polling every "
              "~2 minutes."),
)

#: Repository / project / workspace access tokens: classic permission names.
_BB_ACCESS = (
    ScopeNeed("bb_repo_read", "Repositories: Read", ("repository", "repository:write",
              "repository:admin"), True, "Clone and index repositories, read diffs."),
    ScopeNeed("bb_pr_write", "Pull requests: Write", ("pullrequest:write",), True,
              "Post review comments, approve and request changes."),
    ScopeNeed("bb_repo_write", "Repositories: Write", ("repository:write",
              "repository:admin"), False,
              "\"Apply fix\": push the suggested change as a commit."),
    ScopeNeed("bb_webhook", "Webhooks: Read and write", ("webhook",), False,
              "\"Install webhook\": instant reviews instead of polling every "
              "~2 minutes."),
)

_GH_CLASSIC = (
    ScopeNeed("gh_repo", "repo", ("repo",), True,
              "Read private repositories, post review comments, approve and "
              "request changes, \"Apply fix\"."),
    ScopeNeed("gh_notifications", "notifications", ("notifications",), False,
              "Auto-review by polling (without a webhook)."),
    ScopeNeed("gh_hook", "admin:repo_hook", ("admin:repo_hook", "write:repo_hook"),
              False, "\"Install webhook\": instant reviews."),
)

#: Fine-grained tokens: listed for the person creating one; not checkable.
_GH_FINE = (
    ScopeNeed("gh_contents", "Contents: Read and write", (), True,
              "Read repositories; write is needed for \"Apply fix\"."),
    ScopeNeed("gh_pulls", "Pull requests: Read and write", (), True,
              "Post review comments, approve and request changes."),
    ScopeNeed("gh_metadata", "Metadata: Read", (), True, "List repositories."),
    ScopeNeed("gh_webhooks", "Webhooks: Read and write", (), False,
              "\"Install webhook\": instant reviews. Polling needs a classic "
              "token with `notifications` instead."),
)

_GITLAB = (
    ScopeNeed("gl_api", "api", ("api",), True,
              "Read repositories and post merge request comments and approvals "
              "(`read_api` can read but cannot post)."),
)

CATALOGUE: dict[str, tuple[ScopeNeed, ...]] = {
    "github:classic": _GH_CLASSIC,
    "github:fine_grained": _GH_FINE,
    "gitlab": _GITLAB,
    "bitbucket:api_token": _BB_API,
    "bitbucket:access_token": _BB_ACCESS,
}

#: The kind shown before a token is entered — the one the instructions use.
DEFAULT_KIND = {"github": "github:fine_grained", "gitlab": "gitlab",
                "bitbucket": "bitbucket:api_token"}


def token_kind(provider: str, token: str | None) -> str:
    """The catalogue entry for this token, from its well-known prefix."""
    tok = token or ""
    if provider == "github":
        return "github:fine_grained" if tok.startswith("github_pat_") else "github:classic"
    if provider == "bitbucket":
        return "bitbucket:access_token" if tok.startswith("ATCTT") else "bitbucket:api_token"
    return DEFAULT_KIND.get(provider, provider)


def parse_header_scopes(raw: str | None) -> list[str]:
    """`a, b,c` → ["a", "b", "c"]."""
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def assess(kind: str, granted: list[str] | None) -> dict:
    """What the token of `kind` lacks. `granted` None = the provider did not
    say (fine-grained GitHub token, an older GitLab) → `checked` False and
    nothing is reported missing."""
    needs = CATALOGUE.get(kind, ())
    checkable = granted is not None and all(n.any_of for n in needs)
    have = set(granted or [])
    missing_required: list[dict] = []
    missing_optional: list[dict] = []
    if checkable:
        for need in needs:
            if not have.intersection(need.any_of):
                (missing_required if need.required else missing_optional).append(
                    _public(need))
    return {
        "kind": kind,
        "checked": checkable,
        "granted": sorted(have),
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }


def _public(need: ScopeNeed) -> dict:
    out = asdict(need)
    out.pop("any_of")
    return out


def catalogue_for_ui() -> dict:
    """{kind: [need...]} plus the default kind per provider."""
    return {
        "kinds": {kind: [_public(n) for n in needs] for kind, needs in CATALOGUE.items()},
        "default_kind": dict(DEFAULT_KIND),
    }
