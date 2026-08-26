"""Every model call leaves through the gateway, or it is on this list.

There was no rule. Documentation generation constructed `genai.Client(api_key=…)`
three files deep and therefore appeared in none of the tenant's routing, keys or
spend, and nothing anywhere would have told you — the code looked exactly like
the code beside it.

So the rule is written down here, in the only form that survives: two bans and
an allow-list where every entry has to say why.

  BAN 1  A raw provider SDK — genai.Client, anthropic.Anthropic, openai.OpenAI
         — outside the wrapper that exists to hold it.

  BAN 2  `get_gemini_client()`, which is the wrapper around that SDK and just
         as much a bypass of the LiteLLM gateway, outside the surfaces that
         still legitimately use it.

Writing the list out is what made it shrink. It started with five entries
labelled DEBT — Q&A answers, vault retrieval, the exploration agent, and the
review agents' two fallbacks — and naming them as debt in a file somebody reads
is what got them fixed rather than tolerated. One of the five turned out to be
a client constructed and never called at all.

It shrank again when the policy became absolute — no native provider SDK
bindings, every model call through LiteLLM — and three more entries turned out
to be exempting nothing:

  * `src/indexing/vectors/embedder.py` held a `GeminiEmbedder` with its own
    `genai.Client`. `completion._configured_embedder()` returns None, not that
    class, for the default `embedding_provider="gemini"`, so `get_embedder`
    could only ever be reached with the OTHER value and the Gemini branch was
    unreachable on every install there is. Deleted, not exempted.
  * `src/llm/client.py` names `get_gemini_client()` in a docstring comparison,
    and `_code_only` has stripped docstrings since the fourth time a file
    failed its own guard for explaining itself.
  * `src/llm/__init__.py` re-exports the names through `__getattr__`, as
    strings — it never writes the call.

An exemption that exempts nothing is worse than none: it reads as a surface
somebody decided to keep. So `test_no_exemption_outlives_its_reason` now
deletes them for us, by failing when a listed file no longer matches the ban
it is listed against.

The list shrank once more when the embedding exception was retired (2026-08):
`completion.embed`/`embed_batch` used to construct the native client for a
direct-Google workspace, on the argument that a changed transport could drift
from the vectors the Qdrant collection was built with. The argument was
answered with evidence instead of caution — the installed LiteLLM's `gemini/`
route was captured at the wire posting the same batchEmbedContents body, and
the outgoing request fields are pinned in
test_embedding_requests_do_not_drift.py — so completion.py left the list and
the client lost its embed methods entirely.

What is left are decisions, each with its reason. If an entry here cannot say
why, it is debt wearing an exemption.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

RAW_SDK = re.compile(
    r"\b(?:genai\.Client|anthropic\.Anthropic|anthropic\.AsyncAnthropic"
    r"|openai\.OpenAI|openai\.AsyncOpenAI)\s*\(")

#: Files allowed to construct a provider SDK directly, and why.
RAW_SDK_ALLOWED = {
    # The wrapper itself, and the only file in the repository that holds a
    # provider SDK at all. Everything that could move has moved: chat through
    # completion.stream_chat, documentation through build_llm_client,
    # embeddings through completion.embed (request fields pinned in
    # test_embedding_requests_do_not_drift.py), and the dead generate/
    # generate_stream/embed methods were deleted with their callers. What
    # remains is the one shape LiteLLM does not offer.
    "src/llm/gemini_client.py":
        "the Gemini wrapper — the one file allowed to hold the SDK, kept "
        "solely for the native tool loop the exploration agent needs",
}

#: Files allowed to call `get_gemini_client()` / construct GeminiClient, and why.
WRAPPER_ALLOWED = {
    # Defines the class and constructs it: `_gemini_for` (the per-workspace
    # client cache) moved in here from completion.py when the embedding
    # exception was retired, so the module that holds the SDK also provisions
    # every instance of it.
    "src/llm/gemini_client.py": "defines it and provisions the cached instances",
    # Gemini's native function-calling loop. `generate_with_tools_turn` hands
    # back the RAW types.Part objects and ExplorationAgent appends them
    # verbatim into the next turn's contents, because Gemini 3.x answers 400
    # INVALID_ARGUMENT to a follow-up whose function_call parts lost their
    # thoughtSignature. LiteLLM normalises a response into the OpenAI
    # chat-completions shape; round-tripping an opaque vendor signature
    # through that shape is not a contract it states, and the failure lands on
    # turn 2 of every exploration. Moving this is a rewrite of the agent, not
    # a change of transport — so it is a decision, and it is written here.
    "src/qa/exploration_agent.py":
        "Gemini-native function-calling loop — thought signatures round-trip "
        "as raw Parts, which an OpenAI-shaped response cannot carry",
}


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


CLIENT_IMPORT = re.compile(
    r"from src\.llm\.gemini_client import[^\n]*\b(?:get_gemini_client|GeminiClient)\b")


def _imports_the_client(code: str) -> bool:
    """Does this construct or import the direct Gemini client?

    Deliberately not "mentions the module": `_estimate_tokens` lives there too
    and is a pure token count with no transport of its own, so banning the
    whole module would push a counting helper into the allow-list and dilute
    what the list means.
    """
    return bool(
        "get_gemini_client(" in code
        or "GeminiClient(" in code
        or CLIENT_IMPORT.search(code)
    )


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _code_only(source: str) -> str:
    """Strip docstrings and comments.

    A file that MENTIONS `get_gemini_client()` in a comment explaining that it
    no longer uses one would otherwise fail its own guard — which has happened
    in this repository four times now.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return source
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
               and isinstance(body[0].value, ast.Constant) \
               and isinstance(body[0].value.value, str):
                docstrings.add(body[0].value.value)
    out = source
    for text in docstrings:
        out = out.replace(text, "")
    return "\n".join(
        line.split("#", 1)[0] for line in out.splitlines()
        if not line.strip().startswith("#")
    )


# ─── ban 1: the raw SDK ──────────────────────────────────────────────


def test_no_new_file_constructs_a_provider_sdk():
    offenders = []
    for path in _python_files():
        rel = _rel(path)
        if rel in RAW_SDK_ALLOWED:
            continue
        if RAW_SDK.search(_code_only(path.read_text(encoding="utf-8"))):
            offenders.append(rel)
    assert not offenders, (
        "these construct a provider SDK directly, so their calls never reach "
        "the LiteLLM gateway and appear in no tenant's routing, keys or "
        f"spend: {offenders}. Route them through build_llm_client, or add an "
        "entry to RAW_SDK_ALLOWED saying why not."
    )


def test_every_exemption_states_a_reason():
    """An allow-list with empty reasons is a list of things nobody rechecked."""
    for path, reason in {**RAW_SDK_ALLOWED, **WRAPPER_ALLOWED}.items():
        assert reason.strip(), f"{path} is exempt with no reason given"
        assert (ROOT / path).exists(), f"{path} is exempt but does not exist"


def test_no_exemption_outlives_its_reason():
    """The list may only contain files that would actually fail without it.

    Three entries were exempting nothing when the policy was tightened: a file
    whose only mention is in a docstring `_code_only` already strips, a
    package `__init__` that re-exports the names as strings, and an embeddings
    module whose Gemini implementation had been unreachable since the seam was
    wired. Each of them read, to anyone opening this file, as a surface
    somebody had decided to keep — which is the opposite of what an allow-list
    with reasons is for.

    Checked against the same matchers the bans use, so this can never disagree
    with them about what "needs an exemption" means.
    """
    stale = []
    for rel in RAW_SDK_ALLOWED:
        code = _code_only((ROOT / rel).read_text(encoding="utf-8"))
        if not RAW_SDK.search(code):
            stale.append(f"{rel} (RAW_SDK_ALLOWED)")
    for rel in WRAPPER_ALLOWED:
        code = _code_only((ROOT / rel).read_text(encoding="utf-8"))
        if not _imports_the_client(code):
            stale.append(f"{rel} (WRAPPER_ALLOWED)")
    assert not stale, (
        f"these are exempt but no longer do the thing they are exempt for: "
        f"{stale}. Delete the entry — the work is already done, and leaving it "
        "listed claims a native-SDK surface this repository no longer has."
    )


# ─── ban 2: the wrapper ──────────────────────────────────────────────


def test_no_new_surface_bypasses_the_gateway():
    offenders = []
    for path in _python_files():
        rel = _rel(path)
        if rel in WRAPPER_ALLOWED:
            continue
        code = _code_only(path.read_text(encoding="utf-8"))
        if _imports_the_client(code):
            offenders.append(rel)
    assert not offenders, (
        f"these bypass the LiteLLM gateway: {offenders}. Use "
        "build_llm_client(user_id, workspace_id, surface=…) — it resolves the "
        "workspace profile and swaps in the gateway deployment when one is "
        "configured."
    )


def test_generation_is_clean():
    """The surface this rule was written for. It called get_gemini_client() in
    the orchestrator and in all three generators as a fallback; the whole
    module must now be free of it."""
    for path in sorted((SRC / "generation").rglob("*.py")):
        code = _code_only(path.read_text(encoding="utf-8"))
        assert not _imports_the_client(code), (
            f"{_rel(path)} went back to the direct client. The import alone "
            "counts: it is the half somebody types first, and a guard that "
            "only catches the call lets the bypass land in two commits "
            "instead of one."
        )


def test_generation_dispatches_through_the_factory():
    engines = (SRC / "generation" / "engines.py").read_text(encoding="utf-8")
    assert "build_llm_client(" in engines
    assert 'surface="chat"' in engines


# ─── the agent really can research ───────────────────────────────────


def _tool_scopes() -> dict[str, str]:
    """The tool → scope map the MCP server enforces."""
    source = (SRC / "mcp_server" / "http_app.py").read_text(encoding="utf-8")
    return dict(re.findall(r'"([a-z_]+)":\s*"(read:[a-z]+)"', source))


#: The tools the agent's brief actually tells it to use. If one of these is not
#: reachable, the engine's whole argument — that it answers from what it looked
#: up — quietly stops being true.
RESEARCH_TOOLS = [
    "search_symbols", "get_api_surface", "find_consumers",
    "get_architecture", "list_deprecations",
]


@pytest.mark.parametrize("tool", RESEARCH_TOOLS)
def test_the_research_tools_exist(tool):
    assert tool in _tool_scopes(), (
        f"the agent brief tells the model to call {tool}, and the MCP server "
        "does not expose it"
    )


@pytest.mark.parametrize("tool", RESEARCH_TOOLS)
def test_the_agent_token_carries_the_scope_each_tool_needs(tool):
    """The engine mints a token with a fixed scope set. A tool the token cannot
    reach fails at call time, mid-document, and the model writes what it can —
    which is the guessing this engine exists to prevent."""
    from src.agent.runner import _MCP_SCOPES

    required = _tool_scopes()[tool]
    assert required in _MCP_SCOPES, (
        f"{tool} needs {required}; the agent's token carries {_MCP_SCOPES}"
    )


def test_the_brief_names_tools_that_actually_exist():
    """A brief that recommends a tool by a name the server does not answer to
    spends turns on failed calls."""
    brief = (SRC / "generation" / "claude_docs.py").read_text(encoding="utf-8")
    known = set(_tool_scopes())
    named = set(re.findall(r"\b(search_symbols|get_api_surface|find_consumers"
                           r"|get_architecture|list_deprecations)\b", brief))
    assert named, "the brief no longer names any research tool"
    assert named <= known, f"the brief names unknown tools: {named - known}"


def test_the_agent_reaches_the_server_over_mcp():
    engine = (SRC / "generation" / "claude_docs.py").read_text(encoding="utf-8")
    assert '"celmis"' in engine and '"type": "http"' in engine
    assert "_mint_mcp_token" in engine, "the agent would call the server anonymously"
    # Trailing slash: Starlette answers 307 without it, and a redirected POST
    # is not something the MCP streamable-HTTP client is guaranteed to follow.
    assert "/mcp/" in engine
