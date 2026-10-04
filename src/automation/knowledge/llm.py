"""Models: provider keys, LiteLLM, profiles, embeddings, usage and budget.

Written from web/app/(app)/settings/llm, src/api/routers/llm.py,
src/llm/{profiles,litellm_proxy,budget}.py, docs/LITELLM_GATEWAY.md,
web/app/(app)/admin/usage and src/api/routers/spend.py.
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="llm-setup",
        title="LLM Setup: provider keys, LiteLLM proxy, models per surface",
        keywords=(
            "llm", "model", "provider", "key", "api key", "gemini",
            "anthropic", "openai", "openrouter", "groq", "mistral", "claude",
            "litellm", "proxy", "gateway", "virtual key", "ollama", "vllm",
            "lm studio", "self-hosted", "local", "byok", "profile",
            "fallback", "language", "price", "catalog",
            "модел", "ключ", "провайдер", "проксі", "шлюз", "локальн",
            "мова", "ціни", "каталог",
            "прокси", "язык", "цены",
        ),
        strong=("litellm", "llm", "api key", "proxy", "проксі", "прокси",
                "gemini", "openai", "anthropic", "ollama"),
        body="""
Where: [LLM Setup](/settings/llm) (Settings → "LLM Setup"). Keys and models
belong to the WORKSPACE; changing them needs owner or admin of the workspace
(or a global admin) — others see "Read-only — provider keys and models belong to this workspace and can only be changed by its owner or admin." Everyone
owns their personal workspace, so they can configure that one.

Provider keys ("Provider keys" card): Google Gemini, Anthropic (Claude),
OpenAI, OpenRouter, Groq, Mistral AI.
1. Paste the key into the provider's row and press "Save".
2. Press "Test" — it reports the connection and how many models it sees.
Keys are stored encrypted; only the masked form is shown again.

LiteLLM proxy (a gateway your company runs) — in the same card; set in the UI
only, there is no environment fallback:
1. "Proxy URL": its public https address (no user:password, query or
   fragment; private addresses are refused unless the operator allow-lists
   the host with `LITELLM_PROXY_ALLOWED_HOSTS`).
2. "Virtual key": the proxy's virtual key (`sk-…`).
3. "Verify and save" — both are checked against the proxy's model list; the
   row then says "connected" and lists the models. "Remove" deletes it.
4. Pick "LiteLLM proxy" as the provider on the surface cards below.
"Model prices" (shown when the proxy is connected) sets USD per 1M tokens per
alias; "Refresh from proxy", "Reset to automatic". Separately, an operator can
run an installation-wide LiteLLM gateway (env `COMPOSE_PROFILES=gateway`,
`LITELLM_PROXY_URL`, `LITELLM_MASTER_KEY` starting `sk-`); the banner at the
top then says "Via the LiteLLM gateway".

Models per surface — a card each with "Provider", "Model" and "Save":
- "Chat / Q&A" — streamed answers; also "Documentation language" and
  "Documentation engine" ("API model — one prompt" or "Claude Code agent — researches the code").
- "Celmis agent" — this assistant; a fast cheap model is usually right.
- "PR Review" — "Engine" ("API models (BYOK)" or "Claude Code (subscription)"), "Review language", "Fallback review model".
- "Per-agent overrides" — "Model", "Max output tokens", "Reasoning",
  "Temperature" per review agent; empty inherits from the review card. A
  repository's policy can override these again (policy → "Models & limits").
- "Embeddings" — see the embeddings section.
Self-hosted (Ollama, vLLM, LM Studio): choose "Self-hosted (OpenAI-compatible)" as the provider on Chat, Review or Agent, fill "Base URL" (e.g. `http://host.docker.internal:11434/v1`), the exact model name and
"API key (optional)", then "Test connection". A private address needs the
operator's `EGRESS_ALLOW_PRIVATE_NETWORK=1`.

[Model Catalog](/settings/models) lists the models your connected providers
offer and their prices ("Refresh pricing (OpenRouter)").
""",
    ),
    Section(
        id="embeddings",
        title="Embeddings and the vector store (installation-wide), reindex",
        keywords=(
            "embedding", "embeddings", "vector", "dimension", "width",
            "qdrant", "pinecone", "reindex", "re-index", "semantic",
            "ембедин", "ембеддин", "емб", "вектор", "розмірн", "переіндекс",
            "эмбеддин", "эмбединг", "размерн", "переиндекс",
        ),
        strong=("embedding", "ембедин", "ембеддин", "эмбеддин", "dimension",
                "розмірн", "размерн", "qdrant", "vector"),
        body="""
Embeddings (the vectors behind Q&A and semantic search) are
INSTALLATION-WIDE: one profile and one shared vector collection for every
workspace. On [LLM Setup](/settings/llm), card "Embeddings": provider Google
Gemini, OpenAI or Mistral AI (the LiteLLM proxy only in the default
workspace), the model, and "Dimensions" (128–3072, default 3072). Embeddings
use that provider's key from "Provider keys".

When the server operator sets `EMBEDDING_PROVIDER` (self-hosted embeddings
via `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`), the card
is read-only ("configured by the operator (env)") and changes are made in the
server environment.

Changing the embeddings model or dimension makes existing vectors
incomparable ("The embeddings profile changed — re-indexing is required for search to work correctly."). Then press "Reindex everything" on the same card:
it queues a re-embed job per indexed repository. It needs a GLOBAL admin (a
workspace admin gets 403 Admin scope required). On a multi-workspace
installation a change of width is refused (409) because it would delete
every workspace's vectors.

"Vector store" card: "Local (bundled)" Qdrant by default, or "Qdrant (Cloud / self-hosted URL)"; global admin only. Switching stores does not migrate
vectors — regenerate vaults or reindex afterwards.
""",
    ),
    Section(
        id="usage-cost",
        title="Usage & cost, and the workspace budget",
        keywords=(
            "usage", "cost", "spend", "budget", "cap", "limit", "money",
            "price", "token", "bill", "invoice", "hard stop", "expensive",
            "витрат", "вартіст", "бюджет", "ліміт", "гроші", "рахунок",
            "скільки", "дорого",
            "расход", "стоимост", "бюджет", "лимит", "деньг", "сколько",
        ),
        strong=("budget", "бюджет", "usage", "cost", "spend", "витрат",
                "расход"),
        body="""
[Usage & cost](/admin/usage) (its own sidebar section; every member sees
their workspace's figures): "Total cost", "Input tokens", "Output tokens",
"LLM calls", a "Usage over time" chart and breakdowns "By surface" (Q&A chat,
PR review, Embeddings, Documentation, Dependency audit, Celmis agent, Claude
Code), "By review agent", "By model", "By provider", "By repository", "By operation", "By user", "By billing". Clicking a row filters the whole page.
Range "Today", "This month", "Custom"; "Granularity" hourly to monthly.

Budget — the "Workspace budget" card on the same page (owner or admin of the
workspace):
1. "Monthly cap (USD)" — 0 disables the cap.
2. "Alert at (%)" — default 80.
3. "Hard stop when the cap is reached" — blocks new LLM calls once the cap is
   hit; off = calls continue and the card says the cap is exceeded.
4. Save; "Month to date" shows progress.
A blocked call says the workspace has spent its monthly LLM budget; raise
the cap here.

Your own usage is also on [Account](/settings) ("LLM calls", "Tokens in",
"Tokens out", "Cost"). Each LLM call (model, tokens, duration; no prompt
text) is listed on [Audit log](/admin/audit).
""",
    ),
)
