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
- [Code review](/reviews): past and running PR reviews; trigger a review by PR URL. [Pull requests](/pull-requests) and [Issues](/issues) across connected providers; [Analytics](/analytics) (editors, admins and owners only). [Review policies](/admin/review-policies), [Review agents](/admin/agents), [Compliance](/admin/compliance), [Deprecations](/admin/deprecations).
- [Ask the code](/projects): a project groups indexed repositories so one question searches all of them. [All chats](/chats), [Code search](/search).
- [Claude agent](/claude): connect a Claude subscription token, then run coding sessions against a repository.
- [Celmis agent](/automation): this conversation as a full page, with the list of past chats. Also opened from the round button at the bottom right of every page.
- Monitoring: [Alerts](/alerts) (Grafana or any webhook), [Notifications](/admin/notifications) (Slack, Telegram, email channels), [Job queue](/admin/jobs), [Audit log](/admin/audit) (every LLM call).
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
- Bitbucket: an Atlassian API token (Atlassian → Security → Create API token),
  workspace-scoped, with Repositories Admin/Write and Pull requests Write. You
  also enter the workspace slug (bitbucket.org/<workspace>/) and the Atlassian
  email you log in with.

After connecting, add repositories on [Repositories](/repositories). Turn on
automatic review per repository there, or ask this agent to do it for a set.

## Review prompts and rules

- Per-policy prompt: open [Review policies](/admin/review-policies), pick a
  policy, then the "Prompt & rules" tab — the prompt template and folder rules
  live there. Other tabs: General & branches, Models & limits, MCP sources,
  Agents (per-policy agent prompt overrides).
- Workspace-wide agent prompts: [Review agents](/admin/agents) lists the
  specialised reviewers; "Edit prompt" opens one, saves a workspace override,
  and "Reset to default" restores the built-in text.

## LLM keys and models

- [LLM keys & models](/settings/llm): provider keys (Google Gemini, Anthropic,
  OpenAI, OpenRouter, Groq, Mistral) — paste, Save, then Test. The Google key
  is also used for embeddings. The same page holds a LiteLLM proxy (an
  OpenAI-compatible gateway: its URL and key) and the model profile for each
  surface: chat, review, agent and embeddings. A self-hosted server (Ollama,
  vLLM, LM Studio) is picked there as the "self-hosted" provider.
- Changing the embeddings model or dimension needs a re-index ("Re-index all"
  on the same page).
- Which models exist and what they cost: [Model catalog](/settings/models).

## Roles

Workspace roles, lowest to highest: viewer, member, editor, admin, owner.
Admins and owners change settings and invite people on
[Workspaces & members](/admin/workspaces).
"""

#: `[label](/route)` — the only link shape the guide uses and the only one an
#: answer may keep.
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]*)\)")


def _routes(text: str) -> frozenset[str]:
    return frozenset(
        m.group(2) for m in _LINK.finditer(text) if m.group(2).startswith("/"))


#: Every in-app route the guide names. Derived, never listed by hand: a second
#: list would be the one that is not updated when a page moves.
GUIDE_ROUTES: frozenset[str] = _routes(GUIDE)


#: Named routes whose children are real pages addressed by a name or an id:
#: `/admin/review-policies/default`, `/admin/agents/security`. Listed rather
#: than inferred from "any child of a known route", because `/settings` is
#: known and `/settings/github` is a 404.
_PARENTS_OF_DETAIL_PAGES = frozenset({
    "/admin/review-policies", "/admin/agents", "/projects", "/claude",
})


def _known(href: str) -> bool:
    """A route the guide names, or one detail page under a listed parent.

    Nothing outside the app is known: an answer about tokens that links to a
    provider's site is linking to a page nobody here checked, and the
    connections page already links to the right screen of each provider.
    """
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
        return m.group(0) if _known(m.group(2)) else m.group(1)

    return _LINK.sub(_one, text or "")


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
