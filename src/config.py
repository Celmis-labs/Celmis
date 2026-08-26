"""Central system configuration. All settings come from .env or defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root — where .env lives (src/config.py → ../)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


#: Embedding backends. `gemini` is what every existing install runs and stays
#: the default; `openai_compatible` is one HTTP client aimed at any server that
#: answers POST {base_url}/embeddings.
EMBEDDING_PROVIDERS = ("gemini", "openai_compatible")


class Settings(BaseSettings):
    """All system settings in one place."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Secrets ─────────────────────────────────────────────────────
    # Optional since keys went per-workspace: they are entered in the UI and
    # stored encrypted in the credential store, so production runs with this
    # empty on purpose — an env-level key is a cross-tenant fallback that every
    # workspace without its own would silently spend on. Required here meant
    # the API refused to START without a value it does not use.
    gemini_api_key: SecretStr = Field(
        default=SecretStr(""), description="Legacy global Gemini key; prefer LLM Setup",
    )
    # Vector store. Empty url → EMBEDDED local Qdrant persisted under
    # workspace_dir/qdrant_local — zero-config default. Set QDRANT_URL for a
    # server (the compose stack ships a local qdrant container) or Qdrant
    # Cloud (with QDRANT_API_KEY). A UI-saved config overrides both — see
    # src/retrieval/vector_store.py.
    qdrant_url: str = Field(default="", description="Qdrant URL (empty = embedded local)")
    qdrant_api_key: SecretStr | None = Field(default=None, description="Qdrant API key (cloud)")
    bitbucket_username: str | None = None
    bitbucket_token: SecretStr | None = None

    # ─── SMTP (optional outbound email) ──────────────────────────────
    # Unset smtp_host → no email is ever sent and every flow keeps its
    # mailer-less behaviour (links returned/logged for manual delivery).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None          # e.g. "Celmis <no-reply@example.com>"
    smtp_starttls: bool = True            # False → implicit TLS on port 465 (SMTPS)
    # Public origin used to absolutize relative links in emails,
    # e.g. "http://63.250.57.159" or "https://celmis.example.com".
    public_base_url: str | None = None

    # ─── Paths ───────────────────────────────────────────────────────
    workspace_dir: Path = Path("~/code-analysis").expanduser()
    vault_dir: Path = Path("~/ObsidianVaults/code-analysis").expanduser()

    # ─── Gemini Models ───────────────────────────────────────────────
    # Fixed by decision: Gemini 3.1 Pro for generation, Gemini Embedding 2 for embeddings.
    # Verified via API ListModels (Apr 2026): the current model IDs:
    #   - generation:  "gemini-3.1-pro-preview" (gemini-3.1-pro without -preview does not exist)
    #   - embeddings:  "gemini-embedding-2" (stable; -preview also exists)
    gemini_generation_model: str = "gemini-3.1-pro-preview"
    # Subagent for the exploration loop (tool-use, multiple cheap turns).
    # gemini-3-flash-preview was chosen from the results of an A/B against
    # lite-preview: on the same 3 questions lite skipped key files
    # (FieldHelper, OrdersController, MathHelper setters), full
    # Flash found them.
    # Trade-off: ~+65% latency (~30s → 50s subagent), ~+67% per-query cost.
    # lite-preview stays available as a fallback for high-volume/dev mode
    # (override via env GEMINI_SUBAGENT_MODEL).
    gemini_subagent_model: str = "gemini-3-flash-preview"
    gemini_embedding_model: str = "gemini-embedding-2"
    gemini_embedding_dimensions: int = 3072  # MRL truncation; 128–3072 valid
    # Task types (per Gemini docs):
    #   RETRIEVAL_DOCUMENT     — for embedding chunks during indexing
    #   CODE_RETRIEVAL_QUERY   — for the NL→code query during retrieval
    gemini_embedding_task_doc: str = "RETRIEVAL_DOCUMENT"
    gemini_embedding_task_query: str = "CODE_RETRIEVAL_QUERY"
    # Asymmetric retrieval, ON by default: the two sides above are embedded
    # with their own task type all the way to the provider, so a query is
    # embedded as a question and a chunk as a document. That is the behaviour
    # the model is being paid for.
    #
    # OFF means SYMMETRIC, not "unset": both sides use
    # `gemini_embedding_task_doc`, one embedding space, still working. It is
    # the escape hatch for a proxy or a model that rejects the field — not a
    # tuning knob. Flipping it once vectors exist changes the query side away
    # from the space the index was built in, so it is a re-index either way.
    #
    # A provider with no such field (anything OpenAI-shaped) degrades to
    # symmetric on its own, without this switch — see
    # `_expressible_task_type` in src/llm/completion.py.
    embedding_task_type_enabled: bool = True

    # ─── Embeddings provider ─────────────────────────────────────────
    # Indexing is the one call that ships the customer's SOURCE CODE off the
    # box, so it is the one call a regulated or air-gapped buyer must be able
    # to keep at home. `gemini` is the default and means today's behaviour
    # exactly; `openai_compatible` posts to any server speaking POST
    # {base_url}/embeddings — Ollama, vLLM, llama.cpp, TEI and Infinity all do.
    # One client, many servers: there is no per-vendor class here on purpose.
    embedding_provider: str = "gemini"
    # e.g. "http://127.0.0.1:11434/v1" (Ollama), "http://ollama:11434/v1"
    # (compose), "http://127.0.0.1:8080/v1" (llama.cpp / TEI / Infinity).
    # Prefer a literal IP or a container-internal name: a hostname resolved by
    # an outside resolver sends a DNS packet even when the connection does not.
    embedding_base_url: str = ""
    # Most local servers ignore auth entirely; vLLM/TEI can be started with a
    # token. Empty → no Authorization header is sent.
    embedding_api_key: SecretStr = Field(default=SecretStr(""))
    embedding_model: str = ""          # e.g. "nomic-embed-text", "BAAI/bge-m3"
    embedding_dimensions: int = 0      # 0 → whatever the server returns
    embedding_timeout_seconds: int = 60
    # Asymmetric-retrieval prefixes. Gemini expresses this as `task_type`;
    # OpenAI-compatible servers have no such field, so the models that need it
    # (nomic-embed-text, e5, bge) want a literal prefix on the text instead.
    # Wrong or missing prefixes do not fail — they silently cost recall, which
    # is why they are configuration and not a guess baked into the client.
    embedding_document_prefix: str = ""   # e.g. "search_document: "
    embedding_query_prefix: str = ""      # e.g. "search_query: "

    # ─── Gemini Tuning ───────────────────────────────────────────────
    gemini_temperature_generation: float = 0.3  # for the PRD — a little creativity
    gemini_temperature_qa: float = 0.1  # for Q&A — factual
    gemini_max_output_tokens: int = 8192

    #: Deadline for ONE documentation-generation call, in seconds.
    #:
    #: It was `LLMClient.generate`'s own default of 120, inherited because the
    #: engine passed no timeout — the same defect the review path carried
    #: until yesterday, in the one other place that asks a model for a large
    #: answer. The engine is careful about its neighbours (there is a comment
    #: right above the call about temperature and the output ceiling meaning
    #: "the provider's default" rather than the installation's) and the clock
    #: was the field that got missed.
    #:
    #: NOT MEASURED HERE, and saying so is the point. This installation has
    #: never run a vault build, so `llm_spend` holds 2436 review calls and
    #: zero documentation ones. What can be argued: a document is capped at
    #: `gemini_max_output_tokens` (8192) over a whole module's code context,
    #: which puts it in the same size class as the review calls that were
    #: measurably being cut at 120s — and unlike a review, a vault build is a
    #: batch job with nobody watching a spinner, so the cost of waiting is
    #: patience and the cost of cutting is a module with no documentation.
    #:
    #: `generation_call_finished` now logs each call's duration, so the number
    #: that replaces this one can be measured instead of argued.
    generation_timeout_seconds: int = 600

    #: Resends after a transport failure on a documentation call.
    #:
    #: STATED, not inherited. LiteLLM's default is 3, which quietly makes any
    #: deadline a ceiling four times its own size — 120 became 480, and 600
    #: would become 2400.
    #:
    #: One, so two attempts in total. The review agents settled on the same
    #: shape for the same reason: two attempts is a retry, three is a way to
    #: turn somebody else's outage into our bill. It is not zero, as it is on
    #: the review path, because there is no ladder above this one — the
    #: generator catches the exception, records the module as failed and moves
    #: on, so a resend here is the only resend there is.
    generation_num_retries: int = 1

    #: Deadline for one Claude-agent documentation SESSION, in seconds.
    #:
    #: The agent engine had no deadline AT ALL. `async for message in
    #: client.receive_response()` waits for the next message, and a session
    #: that stops producing them waits forever — no `max_turns` helps, because
    #: turns are only counted when they arrive.
    #:
    #: That went from bad to worse the day the job lease started renewing
    #: itself. Before, a hung vault build lost its lease after ten minutes and
    #: a sibling worker took the row — wrong, but it moved. Now the heartbeat
    #: keeps saying "a worker is alive on this", which is TRUE and is exactly
    #: why the row never comes back: the worker is alive and waiting on a
    #: session that will not answer, and the slot is gone until the container
    #: restarts.
    #:
    #: Much larger than `generation_timeout_seconds` because it bounds a
    #: different thing: up to `_MAX_TURNS` (24) exchanges, most of them MCP
    #: tool calls against the index, rather than one completion. 1800 allows
    #: roughly 75 seconds a turn, which is generous for a tool call and short
    #: enough that a wedged session is a failed module rather than a lost
    #: worker.
    generation_agent_timeout_seconds: int = 1800

    #: Thinking budget for Gemini 3.x, in tokens. The provider default is a
    #: DYNAMIC budget — the model decides, and a review of a large diff can
    #: spend more on thinking than on the answer. That is invisible in the
    #: response and visible only in `thoughts_token_count` on the bill.
    #:
    #:   -1  dynamic (provider default)
    #:    0  off — cheapest, and the right baseline to measure against
    #:  128+ a floor; Flash accepts up to 24576
    #:
    #: Set explicitly so a benchmark run is reproducible: a dynamic budget
    #: makes two runs of the same PR cost different amounts.
    gemini_thinking_budget: int = -1
    gemini_top_p: float = 0.95
    gemini_timeout_seconds: int = 120

    # ─── Qdrant ──────────────────────────────────────────────────────
    qdrant_collection: str = "code_analysis_vault"
    qdrant_symbols_collection: str = "code_analysis_symbols"
    qdrant_upsert_batch_size: int = 64

    # ─── Token budgets ───────────────────────────────────────────────
    max_code_tokens_per_module: int = 30_000
    max_code_tokens_per_qa: int = 30_000
    max_graph_nodes_per_query: int = 500
    max_vault_notes_retrieved: int = 5

    # ─── Retrieval ───────────────────────────────────────────────────
    retrieval_vector_topk: int = 5
    retrieval_graph_depth: int = 6
    # Exploration subagent (Layer 2 retrieval). Caps to bound cost/latency.
    subagent_max_turns: int = 15         # cap on the number of turn-loops
    subagent_max_tool_calls: int = 50    # global cap (including parallel ones within one turn)
    subagent_max_bodies: int = 18        # cap on the number of function bodies in the final bundle
    subagent_max_output_tokens: int = 4096  # per turn — so parallel tool_calls fit
    # Note: chain "function → file_module → IMPORTS → file_module → class → method
    # → CALLS → file_module" needs 6 hops. The Phase 5d DoD passed at depth=6.
    # max_graph_nodes_per_query bounds the explosion (500 — agreed with 5d).
    retrieval_min_score: float = 0.35  # Cosine similarity; below that — the LLM judges for itself

    # ─── Graph backend (FalkorDBLite, embedded) ──────────────────────
    graph_db_filename: str = "graph.fdblite"  # located in data_dir/{repo_slug}/
    graph_default_depth: int = 2
    graph_max_nodes_per_query: int = 300

    # ─── Tree-sitter language adapters ───────────────────────────────
    # Which adapters are active. Registered in factory.build_default_registry().
    # Empty list → all registered extractors active.
    # The user can sub-set them via .env to narrow things down (for example,
    # ENABLED_LANGUAGE_ADAPTERS=typescript,python for focused indexing).
    enabled_language_adapters: list[str] = Field(
        default_factory=list  # default = empty → all enabled (Stage 2.B + Stage 4)
    )

    # ─── Vue SFC handling ────────────────────────────────────────────
    vue_use_compiler_sfc: bool = False  # True → fall back to @vue/compiler-sfc via Node
    vue_min_coverage_threshold: float = 0.85  # warn if < 85% of Vue files parse

    # ─── Code chunker (LlamaIndex CodeSplitter) ──────────────────────
    chunk_lines: int = 40
    chunk_lines_overlap: int = 10
    chunk_max_chars: int = 1500

    # ─── Indexing ────────────────────────────────────────────────────
    # CGC removed in v3.0 — replaced by our own tree-sitter analyzer
    semgrep_rulesets: list[str] = Field(
        default_factory=lambda: [
            "p/security-audit",
            "p/owasp-top-ten",
            "p/typescript",
            "p/javascript",
            "p/react",
            "p/python",
            "p/php",
            "p/golang",
        ]
    )
    semgrep_timeout: int = 900

    # ─── Security ────────────────────────────────────────────────────
    # Root-level domains. Subdomain match (.allowed) passes automatically:
    # - `generativelanguage.googleapis.com`, `aiplatform.googleapis.com` → ✅
    # - `us-central1-aiplatform.googleapis.com` → ✅ (regional Vertex AI endpoint)
    # - `api.bitbucket.org` → ✅
    # - `googleapis.com.evil.com` → ❌ (lookalike attack)
    egress_allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "googleapis.com",   # Gemini API + embeddings + Vertex AI (incl. regional)
            "bitbucket.org",    # Bitbucket Cloud (api.bitbucket.org via subdomain match)
            "github.com",       # GitHub API root domain (api.github.com via subdomain match)
            "githubusercontent.com",  # raw.githubusercontent.com — for readme/file fetches
            "gitlab.com",       # GitLab.com (gitlab.com/api/v4 — same root)
            # Dependency audit: version registries + OSV vulnerability DB
            "osv.dev",          # api.osv.dev via subdomain match
            "npmjs.org",        # registry.npmjs.org
            "pypi.org",
            "golang.org",       # proxy.golang.org
            "crates.io",
        ]
    )
    # The allowlist above names hosts on the PUBLIC INTERNET. This is the
    # separate question of whether a socket may be opened to something that
    # never leaves the network — loopback or RFC1918, never link-local (the
    # cloud metadata address lives there). An air-gapped install empties
    # `egress_allowed_hosts` and turns this ON: nothing reachable outside, the
    # local model server reachable because it is not outside.
    egress_allow_private_network: bool = False
    audit_log_file: Path | None = None  # will be computed in a property
    audit_retention_days: int = 90
    redaction_fail_closed: bool = True
    redaction_enabled: bool = True

    # ─── Sync ────────────────────────────────────────────────────────
    git_clone_depth: int = 50
    git_clone_filter: str = "blob:none"

    # ─── Supported file extensions (for UI / file filters) ──────────
    # NOTE: pipeline.py does NOT use this list — file dispatch is done by
    # LanguageRegistry.match() via the factory. This list is for UI hints
    # and for CLI listing of which languages the system can handle.
    #: Which files count as source for everything that reads a repository
    #: WITHOUT going through the graph: module detection, the Q&A retriever's
    #: file selection, and feature-documentation generation.
    #:
    #: It has to hold every language the indexer parses, and for a long time it
    #: did not — it was a third hand-written list, and the one that decided
    #: whether "ask the code" would even look at a file. Keep it in step with
    #: `factory.supported_suffixes()`; a test fails when the two drift, which
    #: is how the sixteen languages below were found missing in the first place.
    #: Not derived at runtime on purpose: this is a `Settings` default, and
    #: importing the whole extractor registry to construct settings would put
    #: tree-sitter in the import path of every process that reads a config.
    supported_extensions: list[str] = Field(
        default_factory=lambda: [
            # Frontend (Stage 2 / Phase B exclude — only vue remains):
            ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".cts", ".mts",
            ".vue",
            # Backend (Stage 2.B):
            ".py", ".pyi",                        # Python
            ".go",                                # Go
            ".php", ".phtml",                     # PHP
            ".java",                              # Java
            ".cs", ".csx",                        # C#
            ".cpp", ".cc", ".cxx", ".hpp", ".h",  # C++ (.h shared with C)
            ".hh", ".hxx", ".c",                  # C++/C headers and sources
            # Languages served by the generic tags extractor (see
            # factory.TAGS_LANGUAGES) — these had a graph and no place in any
            # of the three lists that decide what gets read, embedded or
            # documented, so a Ruby repository answered questions about
            # nothing.
            ".rb", ".rake", ".gemspec",           # Ruby
            ".rs",                                # Rust
            ".kt", ".kts",                        # Kotlin
            ".swift",                             # Swift
            ".scala", ".sc",                      # Scala
            ".ex", ".exs",                        # Elixir
            ".dart",                              # Dart
            ".lua",                               # Lua
            ".r",                                 # R
            ".sol",                               # Solidity
            ".ml", ".mli",                        # OCaml
            ".fs", ".fsi", ".fsx",                # F#
            ".elm",                               # Elm
            ".gleam",                             # Gleam
            ".rkt",                               # Racket
            ".f90", ".f95", ".f03", ".f08",       # Fortran
            # Infrastructure (Stage 4):
            # Dockerfile detected via filename pattern, not extension
            ".tf",                                # Terraform
            ".yml", ".yaml",                      # docker-compose, k8s, CI yamls
        ]
    )

    # ─── Derived paths (computed) ────────────────────────────────────
    @property
    def repos_dir(self) -> Path:
        return self.workspace_dir / "repos"

    @property
    def data_dir(self) -> Path:
        return self.workspace_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.workspace_dir / "logs"

    @property
    def audit_log_path(self) -> Path:
        return self.audit_log_file or (self.logs_dir / "audit.jsonl")

    @property
    def chats_db_path(self) -> Path:
        """SQLite file where Q&A chats (multi-turn conversations) are stored."""
        return self.workspace_dir / "chats.db"

    def repo_path(self, repo_slug: str) -> Path:
        """Local path to the cloned repo. `repo_slug` — e.g. 'acme-frontend'."""
        return self.repos_dir / repo_slug

    def repo_data_path(self, repo_slug: str) -> Path:
        """Local path to derived data (graph, sarif)."""
        return self.data_dir / repo_slug

    def repo_graph_path(self, repo_slug: str) -> Path:
        """Path to the FalkorDBLite graph file for a particular repo."""
        return self.repo_data_path(repo_slug) / self.graph_db_filename

    def repo_vault_path(self, repo_slug: str) -> Path:
        """Path inside the vault for a particular repo."""
        return self.vault_dir / "projects" / repo_slug

    @field_validator("workspace_dir", "vault_dir", mode="before")
    @classmethod
    def expand_path(cls, v: str | Path) -> Path:
        return Path(str(v)).expanduser().resolve()

    @field_validator("embedding_provider", mode="before")
    @classmethod
    def known_embedding_provider(cls, v: str) -> str:
        """Reject a typo at startup rather than at the first indexed chunk.

        A misspelt provider that silently fell back to `gemini` is precisely
        the failure this whole change exists to prevent: the operator believes
        the code stays home and it does not.
        """
        name = str(v or "gemini").strip().lower()
        if name not in EMBEDDING_PROVIDERS:
            raise ValueError(
                f"embedding_provider={v!r} is not one of {sorted(EMBEDDING_PROVIDERS)}"
            )
        return name

    def ensure_directories(self) -> None:
        """Create every needed directory if it does not exist."""
        for d in (self.workspace_dir, self.repos_dir, self.data_dir, self.logs_dir, self.vault_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton access to the settings. Reads .env once."""
    return Settings()  # type: ignore[call-arg]
