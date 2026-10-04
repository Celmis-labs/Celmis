"""The agent answers how-to questions from knowledge deep enough to answer them.

The failure this exists for, asked in Ukrainian: "do the AI agent settings
allow changing only the global prompt, or per repository too?" The answer was
"prompts are described in the Review agents section of our guide" — a pointer
to a text the person cannot see, about a thing the product does. The guide
said where the page was and nothing about what it does.

So the planner is now handed the knowledge sections a question is about
(`src.automation.knowledge`), picked deterministically, under a budget. These
tests pin that:

  * the per-repository prompt section is what that question reads, in
    Ukrainian, Russian and English;
  * the knowledge says the thing it was asked: per-repository overrides exist,
    and which prompt wins;
  * every page it links is a page and every label it quotes is on screen —
    the same two rules the guide is held to;
  * the prompt stays inside its budget, and the mocked model call receives
    the section the question is about;
  * the answer is told who is asking, so it can say whether THEY may do it.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_PER_REPO_PROMPT_QUESTIONS = [
    # The question as it was asked.
    "Налаштування AI-агентів дозволяють змінювати лише глобальний промпт, "
    "чи й для кожного репозиторію окремо?",
    "чи можна задати свій промпт агента для окремого репо?",
    "Можно ли переопределить промпт агента для конкретного репозитория?",
    "Do the AI agent settings allow only global prompt changes, or per repo too?",
    "How do I override the security agent prompt for one repository?",
    "which prompt wins: the workspace agent prompt or the repository one?",
]


def _stub_client(monkeypatch, reply: dict, seen: dict):
    def _generate(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(text=json.dumps(reply))

    import src.llm.client as llm_client
    monkeypatch.setattr(
        llm_client, "build_llm_client",
        lambda *a, **kw: types.SimpleNamespace(generate=_generate))


def _en_strings() -> set[str]:
    en = json.loads((_REPO / "web" / "lib" / "i18n" / "messages" / "en.json")
                    .read_text(encoding="utf-8"))
    out: set[str] = set()

    def _walk(node):
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, str):
            out.add(node)

    _walk(en)
    return out


# ─── retrieval ───────────────────────────────────────────────────────


@pytest.mark.parametrize("question", _PER_REPO_PROMPT_QUESTIONS)
def test_the_per_repo_prompt_question_reads_the_prompt_section(question):
    from src.automation.knowledge import select_sections

    ids = [s.id for s in select_sections(question)]
    assert ids and ids[0] == "agent-prompts", (question, ids)


@pytest.mark.parametrize(("question", "section"), [
    ("Як налаштувати вебхук для GitLab?", "webhooks"),
    ("what is the webhook URL for GitHub?", "webhooks"),
    ("які скоупи потрібні токену GitHub?", "git-connections"),
    ("What scopes does a Bitbucket token need?", "git-connections"),
    ("Как пригласить человека в рабочее пространство?", "members-invites"),
    ("who can grant the editor role?", "roles"),
    ("чому на сторінці Issues нічого немає?", "issues-prs"),
    ("embedding dimension mismatch after changing the model", "troubleshooting"),
    ("як налаштувати LiteLLM проксі?", "llm-setup"),
    ("where do I set the monthly budget?", "usage-cost"),
    ("how do I connect Claude Code over MCP?", "mcp"),
    ("як увімкнути SSO через OIDC?", "sso-licence"),
])
def test_a_question_reads_the_section_it_is_about(question, section):
    from src.automation.knowledge import select_sections

    ids = [s.id for s in select_sections(question)]
    assert section in ids[:3], (question, ids)


def test_a_question_with_no_keyword_still_gets_the_page_map():
    from src.automation.knowledge import select_sections

    ids = [s.id for s in select_sections("xyzzy")]
    assert "pages" in ids


def test_retrieval_is_deterministic():
    from src.automation.knowledge import select_sections

    q = _PER_REPO_PROMPT_QUESTIONS[0]
    assert [s.id for s in select_sections(q)] == [s.id for s in select_sections(q)]


@pytest.mark.parametrize("question", _PER_REPO_PROMPT_QUESTIONS + [
    "review policy prompt template agents webhook token roles invite issues "
    "analytics LiteLLM embeddings budget alerts MCP SSO licence",
])
def test_the_knowledge_in_one_call_stays_inside_the_budget(question):
    from src.automation.knowledge import KNOWLEDGE_BUDGET_CHARS, knowledge_for

    assert len(knowledge_for(question)) <= KNOWLEDGE_BUDGET_CHARS


def test_every_section_fits_the_budget_on_its_own():
    """A section bigger than the budget could never be picked."""
    from src.automation.knowledge import KNOWLEDGE_BUDGET_CHARS, SECTIONS

    too_big = [s.id for s in SECTIONS if len(s.render()) > KNOWLEDGE_BUDGET_CHARS // 2]
    assert not too_big, too_big


# ─── what the knowledge says ─────────────────────────────────────────


def test_the_knowledge_says_agent_prompts_can_be_overridden_per_repository():
    from src.automation.knowledge import BY_ID

    body = BY_ID["agent-prompts"].body
    assert "/admin/agents" in body and "/admin/review-policies" in body
    assert "per repository" in body.lower()
    # The precedence, in the order src/review/agents/base.py resolves it.
    order = [body.index(m) for m in (
        "1. The repository's override", "2. The workspace override",
        "3. The built-in prompt")]
    assert order == sorted(order), "the precedence is stated out of order"


def test_the_precedence_matches_the_code():
    """If the resolution order in the review agents changes, this fails
    rather than the agent describing the old order."""
    src = (_REPO / "src" / "review" / "agents" / "base.py").read_text()
    block = src[src.index("Resolution order"):][:600]
    assert block.index("Per-repo per-agent override") < block.index(
        "Global per-agent override") < block.index("built-in default")


def test_the_roles_section_is_written_from_roles_py():
    """Who may grant which role is computed from `grantable_roles`, so a
    change to the grant rule changes the answer with no edit here."""
    from src.automation.knowledge import BY_ID
    from src.users.roles import WORKSPACE_ROLE_RANK

    body = BY_ID["roles"].body
    for role in WORKSPACE_ROLE_RANK:
        assert f"`{role}`" in body, role
    assert "superadmin" in body


# ─── links and labels ────────────────────────────────────────────────


def test_every_route_the_knowledge_links_is_a_page():
    from src.automation.guide import _routes
    from src.automation.knowledge import all_text

    app = _REPO / "web" / "app" / "(app)"
    routes = _routes(all_text())
    assert routes
    missing = sorted(r for r in routes
                     if not (app / r.lstrip("/") / "page.tsx").is_file())
    assert not missing, f"knowledge links routes with no page: {missing}"


def test_every_route_the_knowledge_links_survives_the_filter():
    from src.automation.guide import _routes, keep_known_links
    from src.automation.knowledge import all_text

    for route in _routes(all_text()):
        link = f"[x]({route})"
        assert keep_known_links(link) == link, route


@pytest.mark.parametrize("section_id", [
    s for s in __import__("src.automation.knowledge", fromlist=["BY_ID"]).BY_ID])
def test_every_label_a_section_quotes_is_on_screen(section_id):
    from src.automation.knowledge import BY_ID

    strings = _en_strings()
    quoted = re.findall(r'"([^"\n]+)"', BY_ID[section_id].render())
    stale = [q for q in quoted if q not in strings]
    assert not stale, f"{section_id} quotes labels not in en.json: {stale}"


def test_the_knowledge_quotes_labels_at_all():
    from src.automation.knowledge import all_text

    assert len(re.findall(r'"([^"\n]+)"', all_text())) > 40


# ─── the call ────────────────────────────────────────────────────────


def test_the_model_is_handed_the_section_the_question_is_about(monkeypatch):
    from src.automation.chat import interpret
    from src.automation.guide import GUIDE
    from src.automation.knowledge import BY_ID

    seen: dict = {}
    _stub_client(monkeypatch, {"language": "uk", "note": "x", "steps": []}, seen)
    interpret(_PER_REPO_PROMPT_QUESTIONS[0], workspace_id="ws", user_id="u")

    system = seen["system_instruction"]
    assert GUIDE in system
    assert BY_ID["agent-prompts"].body.strip() in system
    # The stable part stays a prefix: the guide before the per-question part.
    assert system.index(GUIDE) < system.index(BY_ID["agent-prompts"].body.strip())
    assert seen["max_output_tokens"] >= 2000


def test_the_whole_system_prompt_stays_bounded(monkeypatch):
    from src.automation.chat import interpret

    seen: dict = {}
    _stub_client(monkeypatch, {"language": "en", "note": "x", "steps": []}, seen)
    interpret("review policy prompt webhook token roles invite issues "
              "analytics LiteLLM embeddings budget alerts MCP SSO",
              workspace_id="ws", user_id="u")
    # ~4 chars a token: the whole system prompt under ~10k tokens.
    assert len(seen["system_instruction"]) < 40_000


def test_the_help_prompt_forbids_pointing_at_the_guide():
    from src.automation.chat import _HELP

    assert "numbered steps" in _HELP
    assert "see the guide" in _HELP  # named, as the thing never to say
    assert "language of the request" in _HELP


@pytest.mark.parametrize(("caller", "expected"), [
    ({"role": "editor", "is_admin": False, "is_superadmin": False},
     "role in this workspace is editor"),
    ({"role": None, "is_admin": True, "is_superadmin": False},
     "global admin"),
    ({"role": "owner", "is_admin": True, "is_superadmin": True},
     "superadmin"),
    (None, "not known"),
])
def test_the_model_is_told_who_is_asking(monkeypatch, caller, expected):
    from src.automation.chat import interpret

    seen: dict = {}
    _stub_client(monkeypatch, {"language": "en", "note": "x", "steps": []}, seen)
    interpret("how do I invite someone?", workspace_id="ws", user_id="u",
              caller=caller)
    assert expected in seen["prompt"]


def test_the_plan_endpoint_queues_who_is_asking():
    """The worker has no session to look the role up in; the request does."""
    src = (_REPO / "src" / "api" / "routers" / "automation.py").read_text()
    assert '"caller": caller' in src
    handler = (_REPO / "src" / "sync" / "handlers.py").read_text()
    assert 'caller=p.get("caller")' in handler
