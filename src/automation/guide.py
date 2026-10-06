"""The product guide the agent answers how-to questions from.

`help` is the one verb whose answer the model writes in full, so it needs
something true to write it from. This is that: a short, curated map of where
things are and how the common setup tasks are done. It is handed to the
planner in the system prompt (a stable prefix, so a provider that caches
prompts pays for it once), and the model is told to answer ONLY from it.

A Python string rather than a .md file beside this module: `.dockerignore`
drops every `*.md`, so a markdown guide would exist in the repository and be
missing from the image the agent actually runs in.

Every in-app route the agent may link to is written here as a markdown link.
`GUIDE_ROUTES` is parsed out of this text, and `keep_known_links` turns any
other link in an answer back into plain words — the guide is the allow-list.
A model that invents `/settings/github` would otherwise hand the person a
link that lands on a 404, which is worse than no link: it looks like an
answer. Keep the guide short: it travels with every planner call. When a page
moves, this is the file to edit.
"""

from __future__ import annotations

import re

GUIDE = """\
## Where things are

- [Dashboard](/dashboard): overview; [Setup wizard](/onboarding) walks through the first repository; [What you can do](/capabilities).
- [Repositories](/repositories): add a repository (paste a URL or pick one from a connected provider), start indexing, see index state. Also [Dependencies](/dependencies) (audits), [Docs](/docs) (generated documentation), [Repo intelligence](/admin/intel).
- [Code review](/reviews): past and running PR reviews; trigger a review by PR URL. [Issues](/issues): findings followed across a PR's pushes (open, fixed, dismissed). [Pull requests](/pull-requests): the reviewed PRs and their state. [Review rules](/admin/review-rules) (rules library: add, generate, import, approve pending). [Memories](/memories) (facts the team taught the reviewers: add, approve pending, per repository or directory; owners, admins and editors only). [Settings](/review-settings): every review setting, Global and per repository. [Analytics](/analytics): review trends for owners, admins and editors (enterprise licence). [Productivity](/productivity): cycle time, deployments and delivery stability for owners and admins (enterprise licence). Under "More": [Compliance](/admin/compliance), [Deprecations](/admin/deprecations).
- [Ask the code](/projects): a project groups indexed repositories so one question searches all of them. [All chats](/chats), [Code search](/search).
- [Claude agent](/claude): connect a Claude subscription token, then run coding sessions against a repository.
- [Celmis agent](/automation): this conversation as a full page, with the list of past chats. Also opened from the round button at the bottom right of every page.
- Monitoring: [Alerts](/alerts) (Grafana or any webhook), [Notifications](/admin/notifications) (Slack, Discord, Google Chat or webhook channels), [Job queue](/admin/jobs), [Audit log](/admin/audit) (every LLM call).
- [Usage & cost](/admin/usage): spend per workspace and the budget cap.
- Team: [Workspaces & members](/admin/workspaces) (invite people, set roles), [Teams](/admin/teams), [Code access](/admin/access).
- Settings: [Account](/settings), [LLM keys & models](/settings/llm), [Model catalog](/settings/models), [Git connections](/connections), [MCP for your editor](/settings/mcp).

## Connecting GitHub, GitLab or Bitbucket

All three are saved once per workspace on [Git connections](/connections); the
page links straight to each provider's token screen. Tokens are encrypted with
a workspace-scoped key and used to clone, list repositories and post review
comments.

- GitHub: a fine-grained token (Settings → Developer settings → Fine-grained
  tokens). Repository access: all, or the repositories to review. Permissions:
  Contents read and write, Pull requests read and write, Metadata read.
  Contents must be write or "Apply fix" fails with 403. A classic token needs
  the whole `repo` scope; automatic review by polling also needs the classic
  `notifications` scope — fine-grained tokens cannot poll, use a webhook.
- GitLab: a personal access token (avatar → Edit profile → Access tokens) with
  the `api` scope; `read_api` is not enough. gitlab.com requires an expiry
  (at most 400 days). The value starts with `glpat-` and is shown once.
  Self-hosted GitLab: enter the instance address in "GitLab URL" on the same
  card (e.g. https://gitlab.example.com or https://example.com/gitlab) and
  create the token on that instance. The Celmis server must be able to reach
  it — an internal-only GitLab (office network, VPN) is not reachable from a
  cloud server unless networking is arranged; a private CA or a private
  address needs the operator settings GITLAB_CA_BUNDLE / GITLAB_ALLOWED_HOSTS.
  Installing webhooks needs the Maintainer role.
- Bitbucket: an Atlassian API token (Atlassian → Security → Create API token),
  workspace-scoped, with Repositories Admin/Write and Pull requests Write. You
  also enter the workspace slug (bitbucket.org/<workspace>/) and the Atlassian
  email you log in with.

After connecting, add repositories on [Repositories](/repositories). Each
row has "Install webhook" (owner or admin): Celmis creates the review webhook
on the provider with the workspace's token and switches auto-review on. The
per-repository switches are also in the "Auto-review PRs" panel on
[Code review](/reviews), with the URL and secret for a manual setup in the
card below it. Or ask this agent to switch review on for a set.

## Review prompts and rules

- [Code review settings](/review-settings) (Code review → "Settings") holds
  every review setting. Left: "Global" (the workspace defaults, for every
  repository) and "Per repository" (search; the orange number is how many
  settings a repository overrides). Sections: "General" (on/off, target
  branches with globs and `!` exclusions such as `main, release/*, !legacy`,
  drafts, approve / request changes, commit status, review language),
  "Review categories" (which agents run, each one's "Model & limits", the
  verifier), "Review filters" ("Minimum severity",
  "Inline comments per review", "Ignored paths", "Suppressed rules"),
  "Custom prompts" (the base
  instruction and each agent's prompt: at Global the workspace prompt, per
  repository that repository's own), "PR summary", "Rules" (counts and a
  link to the [rules library](/admin/review-rules)), "Custom messages"
  (started / finished texts) and "Advanced" (MCP sources, legacy folder
  rules). Each field says "Overridden" or "Inherited from Global"; the
  reset icon hands it back. One "Save settings" for the scope. A link opens
  a place directly: `/review-settings?repo=<repo>&section=prompts`.
- A repository's prompt for an agent wins over the workspace one, which
  wins over the built-in prompt; rules and the base instruction are added
  on top.

## LLM keys and models

- [LLM keys & models](/settings/llm): provider keys (Google Gemini, Anthropic,
  OpenAI, OpenRouter, Groq, Mistral) — paste, Save, then Test. Embeddings use
  the key of the embeddings provider chosen there (Google, OpenAI or
  Mistral). The same page holds a LiteLLM proxy (an OpenAI-compatible
  gateway: its URL and key) and the model profile for each
  surface: chat, review and agent. A self-hosted server (Ollama,
  vLLM, LM Studio) is picked there as the "Self-hosted (OpenAI-compatible)"
  provider.
- Embeddings are installation-wide (one model for every workspace): only a
  global admin changes them, on the same page; changing the model or
  dimension needs "Reindex everything".
- Which models exist and what they cost: [Model catalog](/settings/models).

## Roles

Workspace roles, lowest to highest: viewer, member, editor, admin, owner.
Admins and owners change settings and invite people on
[Workspaces & members](/admin/workspaces). An owner grants and removes admin,
editor, member and viewer; an admin only member and viewer; owner itself is
granted by the superadmin.
"""

#: `[label](/route)` — the only link shape the guide uses and the only one an
#: answer may keep.
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]*)\)")

#: Every other inline-link shape markdown accepts: a space before the
#: destination, `<...>`, a title. Matched loosely so nothing that a markdown
#: renderer turns into a link slips past the exact form above.
_ANY_INLINE_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]*)\)")

#: A link reference definition — `[a]: /\\host` — which turns `[x][a]` or a
#: bare `[a]` anywhere in the note into a link the inline pattern never sees.
#: The guide uses none, so an answer keeps none.
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:.*$\n?", re.M)

#: `<scheme:...>` autolinks. Nothing outside the app is linkable.
_AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^<>\s]*)>")


def _routes(text: str) -> frozenset[str]:
    return frozenset(
        m.group(2) for m in _LINK.finditer(text) if m.group(2).startswith("/"))


def _knowledge_routes() -> frozenset[str]:
    """The routes the deeper knowledge (`src.automation.knowledge`) links."""
    from src.automation.knowledge import all_text

    return _routes(all_text())


#: Every in-app route the guide or the knowledge behind it names. Derived,
#: never listed by hand: a second list would be the one that is not updated
#: when a page moves.
GUIDE_ROUTES: frozenset[str] = _routes(GUIDE) | _knowledge_routes()


#: Named routes whose children are real pages addressed by a name or an id:
#: `/projects/<id>`, `/claude/<id>`. Listed rather
#: than inferred from "any child of a known route", because `/settings` is
#: known and `/settings/github` is a 404.
_PARENTS_OF_DETAIL_PAGES = frozenset({
    "/projects", "/claude",
})


def _known(href: str) -> bool:
    """A route the guide names, or one detail page under a listed parent.

    Nothing outside the app is known: an answer about tokens that links to a
    provider's site is linking to a page nobody here checked, and the
    connections page already links to the right screen of each provider.
    """
    # A backslash is a slash to a browser (`/\\evil.com` is `//evil.com`, an
    # other site), and whitespace or control characters are stripped by it.
    if re.search(r"[\\\s\x00-\x1f]", href):
        return False
    path = href.split("#", 1)[0].split("?", 1)[0].rstrip("/") or "/"
    if not path.startswith("/") or path.startswith("//"):
        return False
    if path in GUIDE_ROUTES:
        return True
    parent, _, leaf = path.rpartition("/")
    return (parent in _PARENTS_OF_DETAIL_PAGES and parent in GUIDE_ROUTES
            and bool(re.fullmatch(r"[\w.-]+", leaf)))


def keep_known_links(text: str) -> str:
    """The answer with every link outside the guide turned into its label.

    The words stay — "open the connections page" is still true without the
    link — and only the target is dropped. A `javascript:` or off-site href
    never reaches the page that renders this as markdown.
    """
    def _one(m: re.Match[str]) -> str:
        exact = _LINK.fullmatch(m.group(0))
        return m.group(0) if exact and _known(exact.group(2)) else m.group(1)

    text = _REFERENCE_DEFINITION.sub("", text or "")
    text = _AUTOLINK.sub(lambda m: m.group(1), text)
    return _ANY_INLINE_LINK.sub(_one, text)


def guide_links(text: str) -> list[dict[str, str]]:
    """The pages an answer points at, in order, each once.

    Only the ones `keep_known_links` would keep, so a caller handed raw model
    text gets the same allow-list as one handed a parsed note.
    """
    seen: dict[str, str] = {}
    for m in _LINK.finditer(text or ""):
        label, href = m.group(1).strip(), m.group(2)
        if _known(href) and href not in seen:
            seen[href] = label
    return [{"label": label, "href": href} for href, label in seen.items()]


__all__ = ["GUIDE", "GUIDE_ROUTES", "guide_links", "keep_known_links"]
