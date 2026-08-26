"""Multi-repo retrieval — for Q&A against group repos.

Strategy:
  Tier 1 (Qdrant):  one query with MUST(workspace) + MUST + OR(repo IN [...])
                    filter — top-5 across all repos
  Tier 2 (graph):   sequential queries to the FalkorDB of each repo, merged
  Tier 3 (code):    disk read from the corresponding repo_paths. In markdown
                    links — the repo_slug/ prefix
                    (e.g. [file](acme-frontend/src/auth.ts#L42))

Cross-repo edges:
  Phase 3 MVP — sequential. Phase 3.2 (later): a cross_repo_imports table
  in Postgres + Cypher-like resolve via name-matching.

Output:
  RetrievalContext(prompt, system_instruction, vault_hits, files_read)
  ready to be fed into GeminiClient.generate_stream.

Tenancy:
  `retrieve(workspace_id=...)` is required. The repo list is an access
  decision, not an isolation boundary — it was the ONLY thing keeping one
  tenant's notes out of another tenant's answer, and it was reapplied by hand
  at each query. Both Qdrant reads here now carry `VectorScope` conditions:
  `_qdrant_query` (the notes that reach the prompt) and `_probe_related_repos`
  (which ran with NO filter at all and therefore scanned every tenant's points
  to name "related" repositories — a cross-tenant read whose output went into
  the answer as a repo slug).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.access import RepoAccessDecision, resolve_access
from src.config import Settings, get_settings
from src.llm.prompts.technical_answer import TECHNICAL_ANSWER_PROMPT, TECHNICAL_ANSWER_SYSTEM
from src.retrieval.tier1_vault import VaultRetriever
from src.retrieval.vector_store import VectorScope

logger = logging.getLogger(__name__)

# Tier 2 (graph) budget — the graph is a structural *hint* next to the code
# bundle, so it stays small and cheap (local FalkorDB files, no network).
_GRAPH_MAX_SYMBOLS = 8      # identifiers expanded per question
_GRAPH_MAX_BLOCKS = 14      # symbol blocks rendered into the prompt
_GRAPH_MAX_CALLERS = 6      # direct callers listed per symbol

# ── Tier 3 fallback: file selection when there is no vault ────────────
# A repo stays fully indexed (graph populated, code on disk) long before
# anyone clicks "Generate vault", and the chat UI promises answers "based on
# code and the symbol graph" in exactly that state. Everything below is what
# can be picked WITHOUT vault notes; the caps exist because an unguided pick
# has no relevance signal to stop it, so it must be stopped by budget.
_FALLBACK_MIN_FILES = 3           # thinner grep result than this → widen
_FALLBACK_MAX_FILES = 24          # across all repos
_FALLBACK_MAX_FILES_PER_REPO = 8
_FALLBACK_MAX_CHARS_PER_FILE = 8_000  # breadth beats any single file's tail
_FALLBACK_GRAPH_ROWS = 60         # densest-file rows pulled from the graph

# Root files a person opens first in an unfamiliar repo. Ordered by how much
# they answer "what is this service" — README before the manifests, manifests
# before the build files. `.env*` is deliberately absent: it is secrets, and a
# fall-open repo has no deny-glob to stop us reading it.
_ENTRY_ROOT_FILES = (
    "readme.md", "readme.rst", "readme.txt", "readme",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "package.json", "pyproject.toml", "go.mod", "composer.json",
    "cargo.toml", "pom.xml", "requirements.txt", "makefile", "dockerfile",
)
_ENTRY_ROOT_LIMIT = 4
# Conventional process entry points — where the wiring between services lives.
_ENTRY_MODULE_GLOBS = (
    "main.*", "app.*", "index.*", "server.*",
    "src/main.*", "src/app.*", "src/index.*", "src/server.*",
    "src/api/main.*", "app/main.*", "cmd/*/main.go", "manage.py",
)
_ENTRY_MODULE_LIMIT = 4
# Generated / vendored / test paths carry symbols but explain nothing.
_NOISE_PARTS = frozenset({
    "node_modules", "dist", "build", ".next", "vendor", "__pycache__",
    "migrations", "tests", "test", "__tests__", "spec", "fixtures",
})

# Shown in place of the code section when Tier 3 read nothing. The long
# explanation lives in the notice block (see `_build_no_code_notice`) — here we
# only point at it, because this section sits far from the model's attention.
_NO_CODE_MARKER = (
    "(no file was read — the reason and what to do about it are described "
    "in the «Context is incomplete» block above)"
)


@dataclass
class MultiRepoVaultHit:
    """Wrapper around VaultHit with an explicit `repo` field."""

    note_path: str
    score: float
    type: str
    module: str | None
    repo: str
    symbols: list[str]
    keywords: list[str]
    content: str
    path: str | None  # for a module — relative path inside the repo
    cross_refs: list[str] = field(default_factory=list)


@dataclass
class RetrievalContext:
    """Context ready for synthesis. Pass into `gemini.generate_stream(prompt=...)`."""

    prompt: str
    system_instruction: str
    vault_hits: list[MultiRepoVaultHit]
    files_read: list[str]  # with the repo_slug/ prefix
    # ── Stage 22: research-access boundary reporting ──────────────────
    access_notice: str = ""          # human-readable boundary message (Ukr.)
    blocked_repos: list[str] = field(default_factory=list)   # related, no access
    hidden_files: list[str] = field(default_factory=list)    # code hidden by deny
    code_included: bool = True       # source-code toggle state
    vault_unavailable: str | None = None  # Tier 1 down/empty (setup hint)
    # True when part of the bundle came from the no-vault fallback pick
    # (graph + entry points) rather than from vault-driven retrieval.
    code_fallback_used: bool = False


# ════════════════════════════════════════════════════════════════════
# Multi-repo retriever
# ════════════════════════════════════════════════════════════════════


class MultiRepoRetriever:
    """Tiers 1–3 across several repos at once.

    Tier 1   — Qdrant vector search over vault notes (semantic recall).
    Tier 1.5 — grep for the extracted identifiers (exact-name recall).
    Tier 2   — per-repo tree-sitter graph: symbol definitions + their real
               callers. This is what turns "semantically similar" into
               "structurally connected".
    Tier 3   — read the actual source files into the prompt.

    Every tier is filtered through the caller's research access, so a denied
    repo or path cannot leak through notes, grep hits, graph edges or code.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        vault_ret: VaultRetriever | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # Only the Qdrant handle is needed here; we do NOT construct a
        # VaultRetriever — it pulls in a Gemini client for workspace "default",
        # which in a BYOK deployment without an env key crashes right in the
        # constructor.
        if vault_ret is not None:
            self._qdrant = vault_ret.qdrant
        else:
            from src.retrieval.tier1_vault import _build_qdrant_client
            self._qdrant = _build_qdrant_client(self.settings)
        # Set when Tier 1 (vector store) is unusable — surfaced to the UI so
        # "no vault yet" reads as a setup step, not a broken chat.
        self.vault_unavailable: str | None = None

    async def retrieve(
        self,
        *,
        question: str,
        repos: list[str],
        history: list[tuple[str, str]] | None = None,
        user_id: str | None = None,
        is_admin: bool = True,
        workspace_id: str,
        include_code: bool = True,
    ) -> RetrievalContext:
        """Returns a ready prompt + meta for streaming.

        `workspace_id` is required (it used to default to "default"). Every
        Qdrant read below is scoped by it, so a caller that does not name a
        tenant cannot get one silently assigned.

        Research-access enforcement (Stage 22): repos the caller may not
        research are excluded from retrieval; sensitive files are hidden even
        inside accessible repos; when the question relates to a repo the
        caller lacks access to, a boundary notice is injected so the answer
        tells the user to request access from admins.
        """
        if not repos:
            raise ValueError("repos list cannot be empty")

        # ── Stage 22: resolve research access for the target repos ────
        access = resolve_access(
            user_id=user_id or "default",
            is_admin=is_admin,
            workspace_id=workspace_id,
            repos=repos,
        )
        accessible = [r for r in repos if access[r].researchable]
        denied_targets = [r for r in repos if not access[r].researchable]

        # If the caller can research NONE of the target repos, short-circuit
        # with a pure boundary answer (no retrieval at all).
        if not accessible:
            notice = self._build_access_notice(
                denied_repos=denied_targets, related_repos=[], hidden_files=[],
                access=access, no_accessible=True,
            )
            prompt = (
                notice + "\n\n"
                + TECHNICAL_ANSWER_PROMPT.format(
                    history=self._format_history(history or []),
                    question=question,
                    vault_context="(no access to any repository)",
                    graph_context="(no access)",
                    code_bundle="(no access)",
                )
            )
            return RetrievalContext(
                prompt=prompt,
                system_instruction=TECHNICAL_ANSWER_SYSTEM,
                vault_hits=[],
                files_read=[],
                access_notice=notice,
                blocked_repos=denied_targets,
                hidden_files=[],
                code_included=include_code,
            )

        # ── Tier 1: embed once, query accessible repos + probe others ─
        #
        # The collection is checked BEFORE the embedding, because embedding is
        # the paid part. A workspace whose vault has never been generated used
        # to embed the question, ask Qdrant, take the 404 and degrade — paying
        # for a vector that could not be used, on every message, in the one
        # state where the user can do nothing about it.
        scope = (VectorScope.global_admin() if is_admin
                 else VectorScope.for_workspace(workspace_id))
        hits: list[MultiRepoVaultHit] = []
        related_inaccessible: list[str] = []
        if self._collection_exists():
            from src.llm.completion import embed as _embed
            query_vec = _embed(
                question,
                operation="embed_query_multi_repo", workspace_id=workspace_id,
            )
            hits = self._qdrant_query(query_vec, accessible, scope=scope)
            # Drop vault notes whose source path is denied (creds/crypto notes).
            hits = self._filter_hits_by_access(hits, access)

            # Cross-repo boundary: probe the whole collection for repos the
            # caller is NOT allowed to research but which are relevant.
            related_inaccessible = self._probe_related_repos(
                query_vec,
                exclude=set(accessible),
                user_id=user_id or "default",
                is_admin=is_admin,
                workspace_id=workspace_id,
                scope=scope,
            )
        else:
            # The same code the 404 path produced, reached without the call.
            self.vault_unavailable = "vault_not_generated"
            logger.info("vault_skipped collection=%s — not generated yet",
                        self.settings.qdrant_collection)

        # ── Tier 1.5 + Tier 3: code (subject to toggle + path visibility)
        hidden_files: list[str] = []
        code_fallback_used = False
        if include_code:
            symbol_files = self._grep_symbol_files(question, hits, accessible)
            # Both of the usual ways into Tier 3 can be empty at once: without a
            # vault there are no `hits` whose modules we could walk, and a
            # question phrased in prose ("що це за сервіси" — "what are these
            # services") yields no identifiers to grep for. That combination
            # used to hand the model an empty bundle — which reads to it as
            # "the user attached nothing". Pick from what the deployment
            # actually has instead.
            fallback_files: list[str] = []
            if not hits and len(symbol_files) < _FALLBACK_MIN_FILES:
                fallback_files = self._fallback_files(
                    question, accessible, access, exclude=symbol_files,
                )
            files_read, code_bundle, hidden_files = self._read_code_for_hits(
                hits, priority_files=symbol_files, access=access,
                overview_files=fallback_files,
            )
            code_fallback_used = bool(set(fallback_files) & set(files_read))
        else:
            files_read, code_bundle = [], "(source code display turned off by the user)"

        # ── Access notice (denied target repos + related inaccessible) ─
        notice = self._build_access_notice(
            denied_repos=denied_targets,
            related_repos=related_inaccessible,
            hidden_files=hidden_files,
            access=access,
            no_accessible=False,
        )

        # Nothing readable at all — say why, and say what fixes it. Silence
        # here is what produced "ви не надали вихідний код" ("you did not
        # provide the source code") in production.
        # NOT gated on include_code. It was, and that gate is how the model
        # invented `process_settlement` — a function that exists in no
        # repository — rendered with fabricated line numbers 1-3 under a REAL
        # file path, in an answer whose citation check passed clean.
        #
        # With the toggle off, `files_read` is always empty, so the one block
        # that tells the model "you are working without code, say so" could
        # never fire precisely when it was needed most. The model got a
        # near-empty prompt (401 input tokens) and no instruction to be
        # careful, and filled the gap from its priors.
        no_code_notice = ""
        if not files_read:
            # `access` holds one decision per repository the question
            # reached. If every one of them withholds code, no amount of
            # indexing would have produced a line, and the notice has to say
            # so instead of naming a remedy the asker cannot reach.
            decisions = [d for d in (access or {}).values()]
            denied_by_policy = bool(decisions) and not any(
                getattr(d, "code_visible", False) for d in decisions
            )
            no_code_notice = self._build_no_code_notice(
                vault_missing=bool(self.vault_unavailable) or not hits,
                code_disabled=not include_code,
                access_denied=denied_by_policy,
            )

        # ── Tier 2: structural context from the tree-sitter graph ─────
        graph_context = self._graph_context(question, hits, accessible, access)

        # ── Build prompt ──────────────────────────────────────────
        vault_context = self._format_vault_hits(hits)
        history_str = self._format_history(history or [])

        prompt = TECHNICAL_ANSWER_PROMPT.format(
            history=history_str,
            question=question,
            vault_context=vault_context,
            graph_context=graph_context,
            code_bundle=code_bundle or _NO_CODE_MARKER,
        )
        for block in (no_code_notice, notice):  # notice first in the prompt
            if block:
                prompt = block + "\n\n" + prompt

        return RetrievalContext(
            prompt=prompt,
            system_instruction=TECHNICAL_ANSWER_SYSTEM,
            vault_hits=hits,
            files_read=files_read,
            access_notice=notice,
            blocked_repos=sorted(set(denied_targets) | set(related_inaccessible)),
            hidden_files=hidden_files,
            code_included=include_code,
            vault_unavailable=self.vault_unavailable,
            code_fallback_used=code_fallback_used,
        )

    # ─── Tier 2: graph expansion ─────────────────────────────────────

    def _graph_context(
        self,
        question: str,
        hits: list[MultiRepoVaultHit],
        repos: list[str],
        access: dict[str, RepoAccessDecision],
    ) -> str:
        """Structural context from the per-repo tree-sitter graph.

        Vector search says "these notes look related"; the graph says "this
        symbol is defined here and these are its actual callers". Feeding both
        lets the model ground an answer in real call structure instead of
        semantic similarity alone.

        Everything is filtered through the caller's research access, so a
        denied path never leaks via a caller list. Bounded on purpose: the
        graph is a hint, not the payload — the code bundle carries the detail.
        """
        from src.mcp_server import tools as graph_tools

        targets = self._symbol_targets(question, hits, min_len=3)[:_GRAPH_MAX_SYMBOLS]
        if not targets:
            return "(no symbols extracted from the question)"

        blocks: list[str] = []
        seen: set[str] = set()
        for repo in repos:
            dec = access.get(repo)
            for name in targets:
                if len(blocks) >= _GRAPH_MAX_BLOCKS:
                    break
                try:
                    defs = graph_tools.find_symbol(name, repo, limit=2)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("graph_find_symbol_failed repo=%s sym=%s err=%s", repo, name, exc)
                    continue
                for d in defs or []:
                    fpath = d.get("file") or ""
                    if dec is not None and fpath and not dec.path_visible(fpath):
                        continue
                    key = f"{repo}:{d.get('id') or name}"
                    if key in seen:
                        continue
                    seen.add(key)

                    header = (
                        f"- `{d.get('name')}` ({d.get('kind') or 'symbol'}) "
                        f"— {repo}/{fpath}:{d.get('start_line', 0)}"
                    )
                    sig = (d.get("signature") or "").strip()
                    if sig:
                        header += f"\n    signature: `{sig[:160]}`"

                    callers = self._graph_callers(
                        graph_tools, d.get("id") or name, repo, dec,
                    )
                    if callers:
                        header += "\n    called by: " + ", ".join(callers)
                    blocks.append(header)

        if not blocks:
            return "(no graph matches for the extracted symbols)"
        logger.info("graph_context symbols=%d blocks=%d", len(targets), len(blocks))
        return "\n".join(blocks)

    @staticmethod
    def _graph_callers(graph_tools, symbol_id: str, repo: str,
                       dec: RepoAccessDecision | None) -> list[str]:
        """Direct callers of `symbol_id`, access-filtered and capped."""
        try:
            res = graph_tools.find_callers(
                symbol_id=symbol_id, repo_slug=repo,
                depth=1, max_nodes=_GRAPH_MAX_CALLERS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph_find_callers_failed repo=%s sym=%s err=%s", repo, symbol_id, exc)
            return []
        out: list[str] = []
        for c in (res or {}).get("callers", [])[:_GRAPH_MAX_CALLERS]:
            cfile = c.get("file", "") or ""
            if dec is not None and cfile and not dec.path_visible(cfile):
                continue
            out.append(f"`{c.get('name')}` ({cfile}:{c.get('start_line', 0)})")
        return out

    # ─── Tier 3 fallback: selection without a vault ──────────────────

    def _fallback_files(
        self,
        question: str,
        repos: list[str],
        access: dict[str, RepoAccessDecision],
        *,
        exclude: Iterable[str] = (),
    ) -> list[str]:
        """Files worth reading when Tier 1 handed the reader nothing to walk.

        Three sources, in descending specificity: symbols the question names
        (resolved through the graph, which knows languages grep skips), the
        repo's own entry points, and the files the graph says carry the most
        symbols. Merged round-robin because a "what are these services"
        question is about *all* the repos — a per-repo cap alone would let the
        first repo spend the whole budget.

        Returns repo-prefixed paths; per-path visibility is NOT applied here —
        it stays in the single reader gate (`_read_code_for_hits`) so a denied
        file is still reported as hidden instead of silently vanishing.
        """
        from src.mcp_server import tools as graph_tools

        per_repo: list[list[str]] = []
        for repo in repos:
            dec = access.get(repo)
            # `metadata` visibility means no source may be shown at all, so
            # proposing files here would only pad the hidden-files list.
            if dec is not None and not dec.code_visible:
                continue
            repo_path = self.settings.repo_path(repo)
            if not repo_path.exists():
                continue
            picks: list[str] = []
            for rel in (
                self._graph_symbol_files(graph_tools, question, repo)
                + self._entry_point_files(repo_path)
                + self._graph_hub_files(graph_tools, repo)
            ):
                prefixed = f"{repo}/{rel}"
                if prefixed in picks:
                    continue
                picks.append(prefixed)
                if len(picks) >= _FALLBACK_MAX_FILES_PER_REPO:
                    break
            if picks:
                per_repo.append(picks)

        seen = set(exclude)
        out: list[str] = []
        for i in range(_FALLBACK_MAX_FILES_PER_REPO):
            for picks in per_repo:
                if i >= len(picks) or picks[i] in seen:
                    continue
                seen.add(picks[i])
                out.append(picks[i])
                if len(out) >= _FALLBACK_MAX_FILES:
                    logger.info("fallback_files repos=%d picked=%d (capped)",
                                len(per_repo), len(out))
                    return out
        logger.info("fallback_files repos=%d picked=%d", len(per_repo), len(out))
        return out

    def _graph_symbol_files(
        self, graph_tools, question: str, repo: str,
    ) -> list[str]:
        """Files defining the identifiers named in the question, straight from
        the graph. Grep covers a fixed extension list; the graph covers every
        language the indexer parsed, so this catches what grep cannot see."""
        out: list[str] = []
        targets = self._symbol_targets(question, [], min_len=3)[:_GRAPH_MAX_SYMBOLS]
        for name in targets:
            try:
                defs = graph_tools.find_symbol(name, repo, limit=2)
            except Exception as exc:  # noqa: BLE001
                logger.debug("fallback_find_symbol_failed repo=%s sym=%s err=%s",
                             repo, name, exc)
                continue
            for d in defs or []:
                f = str(d.get("file") or "")
                if f and f not in out:
                    out.append(f)
        return out

    def _entry_point_files(self, repo_path: Path) -> list[str]:
        """README / manifests / conventional main modules.

        For a question about the repo *as a whole* these beat any ranked pick:
        they are where a service says what it is and what it talks to.
        """
        out: list[str] = []
        try:
            root_names = {
                p.name.lower(): p.name for p in repo_path.iterdir() if p.is_file()
            }
        except OSError as exc:
            logger.debug("entry_points_listing_failed path=%s err=%s", repo_path, exc)
            return []

        for want in _ENTRY_ROOT_FILES:
            if len(out) >= _ENTRY_ROOT_LIMIT:
                break
            real = root_names.get(want)
            if real and real not in out:
                out.append(real)

        extensions = set(self.settings.supported_extensions)
        modules = 0
        for pattern in _ENTRY_MODULE_GLOBS:
            if modules >= _ENTRY_MODULE_LIMIT:
                break
            for p in sorted(repo_path.glob(pattern)):
                # The globs are broad on purpose (`main.*` also matches
                # main.css) — the extension list is what keeps them source.
                if not p.is_file() or p.suffix not in extensions:
                    continue
                rel = str(p.relative_to(repo_path))
                if rel not in out:
                    out.append(rel)
                    modules += 1
                break  # one file per pattern is enough for orientation
        return out

    def _graph_hub_files(self, graph_tools, repo: str) -> list[str]:
        """Files carrying the most symbols — the graph's own answer to "where
        does this repo do its work". Costs one aggregate over a local FalkorDB
        file; yields nothing (not an error) for a repo that was never indexed.
        """
        try:
            res = graph_tools.query_graph(
                "MATCH (s:Symbol) WHERE s.kind <> 'file_module' "
                "RETURN s.file AS file, count(s) AS n "
                "ORDER BY n DESC LIMIT $lim",
                repo_slug=repo,
                params={"lim": _FALLBACK_GRAPH_ROWS},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("fallback_graph_hubs_failed repo=%s err=%s", repo, exc)
            return []
        if not (res or {}).get("ok"):
            return []
        out: list[str] = []
        for row in res.get("rows") or []:
            f = str(row.get("file") or "")
            if f and not self._is_noise_path(f) and f not in out:
                out.append(f)
        return out

    @staticmethod
    def _is_noise_path(rel: str) -> bool:
        """Generated, vendored and test paths hold symbols but explain nothing
        — worth excluding only here, where there is no relevance signal to
        push them down the ranking on their own."""
        parts = rel.replace("\\", "/").split("/")
        if any(part in _NOISE_PARTS for part in parts):
            return True
        name = parts[-1].lower()
        return (
            name.startswith("test_")
            or name.endswith("_test.go")
            or ".test." in name
            or ".spec." in name
        )

    @staticmethod
    def _build_no_code_notice(*, vault_missing: bool,
                              code_disabled: bool = False,
                              access_denied: bool = False) -> str:
        """Injected when Tier 3 read literally nothing.

        Two failures, not one, and they pull in opposite directions.

        The first: the model sees an empty code section and answers «ви не
        надали вихідний код» — blaming the user for a missing attachment, in a
        product where the code sits on the server and the fix is a button.

        The second, found in production and worse: with `include_code` off,
        the model wrote a plausible `process_settlement` function that exists
        in no repository, gave it line numbers 1-3, and hung it under a real
        file path. The citation verifier passed it — the file exists, line 1 is
        within the file — so nothing caught it. Inventing code is not a milder
        version of blaming the user; it is the failure this product cannot
        afford, because every other answer it gives is trustworthy enough that
        a reader will believe this one too.

        `code_disabled` distinguishes the two situations, because the remedy
        differs: one is fixed by generating a vault, the other by turning a
        toggle back on. Telling a user to generate a vault they already have
        would send them round the same loop the chat banner already sends them
        round.
        """
        if code_disabled:
            return (
                "# ⚠️ You are answering WITHOUT source code (you must tell the user)\n"
                "«Include code» is switched OFF for this question, so no file "
                "was read. You have not seen this repository's code.\n"
                "NEVER write code you have not read. Do not reconstruct a "
                "function from its name, do not show a plausible "
                "implementation, do not attach line numbers to code you are "
                "imagining, and do not cite a file for a snippet you did not "
                "open. A fabricated snippet under a real path is the single "
                "worst answer this system can give.\n"
                "Answer from what is actually here (vault notes, the symbol "
                "graph), say plainly which parts you cannot confirm without "
                "reading the code, and tell the user they can switch «include "
                "code» on to get an answer grounded in the real source."
            )
        if access_denied:
            # THE THIRD REASON, AND THE ONE WITH NO BUTTON. An access rule set
            # this repository to `metadata` for the asker, so `code_visible` is
            # false and nothing could be opened however well the vault was
            # built. Sending them to «Generate vault» is advice they cannot act
            # on — the vault exists, and generating it again changes nothing.
            #
            # Observed on production: a member restricted to metadata asked for
            # the source of one function and was told to generate a vault that
            # had been generated an hour earlier. Correct behaviour (no code
            # leaked) explained by the wrong cause, which reads as a broken
            # product rather than a working policy.
            return (
                "# ⚠️ Code is withheld by policy (you must explain this to the "
                "user)\n"
                "The source was NOT sent to you because an access rule limits "
                "this person to metadata for these repositories. This is "
                "working as configured — not a missing vault, not a failed "
                "index, and NOTHING the user can fix themselves. Do NOT tell "
                "them to generate a vault, re-index, attach files or switch a "
                "setting: none of it applies and all of it wastes their time.\n"
                "Answer from what you do have (vault notes, the symbol graph), "
                "say plainly that the code itself is restricted, and name the "
                "one step that can change it: a workspace admin widens their "
                "access under «Team & access».\n"
                "NEVER write code you have not read: no reconstructed "
                "functions, no invented line numbers, no snippet attached to a "
                "file you did not open."
            )
        reason = (
            "the knowledge base (vault) for these repositories has not been "
            "generated yet, and no file was found for the symbols from the question"
            if vault_missing
            else "none of the relevant files could be opened"
        )
        return (
            "# ⚠️ Context is incomplete (you must explain this to the user)\n"
            f"Source code did not make it into this answer: {reason}. The user "
            "did NOT forget to send anything — the repositories are connected, "
            "the code sits on the server. Do NOT write «you did not provide the "
            "source code / project files» and do not ask to attach code. Answer "
            "based on what is available (vault notes, symbol graph), honestly "
            "mark the limits of the answer and name the next step: generate the "
            "vault on the «Repositories» page (the «Generate vault» button) and "
            "wait for the repositories to be indexed.\n"
            "NEVER write code you have not read: no reconstructed functions, no "
            "invented line numbers, no snippet attached to a file you did not "
            "open."
        )

    # ─── Stage 22: access helpers ────────────────────────────────────

    def _filter_hits_by_access(
        self,
        hits: list[MultiRepoVaultHit],
        access: dict[str, RepoAccessDecision],
    ) -> list[MultiRepoVaultHit]:
        """Drop vault notes whose source path is *explicitly denied* for the
        caller — so a note about credentials/crypto isn't leaked. Uses
        ``path_denied`` (deny-glob / allow-list miss), NOT ``path_visible``
        (a code-level gate): otherwise a ``metadata``-visibility repo would
        lose every note that carries a source path, defeating the metadata
        tier whose whole purpose is to surface documentation."""
        out: list[MultiRepoVaultHit] = []
        for h in hits:
            dec = access.get(h.repo)
            if dec is None:
                continue
            if h.path and dec.path_denied(h.path):
                logger.info("vault_hit_hidden repo=%s path=%s", h.repo, h.path)
                continue
            out.append(h)
        return out

    def _probe_related_repos(
        self,
        embedding: list[float],
        *,
        exclude: set[str],
        user_id: str,
        is_admin: bool,
        workspace_id: str,
        scope: VectorScope,
    ) -> list[str]:
        """Query the caller's OWN tenant; return repos with relevant content
        that the caller may NOT research (cross-repo boundary detection).

        This used to query the whole collection with no filter, and the repo
        slugs it found were written into the prompt as "the question partly
        concerns repository X". Across three tenants in one collection that is
        another company's repository name, printed to a user who is not in
        that company — the leak was the feature working as designed. The
        boundary this detects is a *within-workspace* one: repos the caller's
        colleagues can research and they cannot.
        """
        if is_admin:
            return []  # admins research everything — nothing is out of bounds
        from qdrant_client.models import Filter
        try:
            resp = self._qdrant.query_points(
                collection_name=self.settings.qdrant_collection,
                query=embedding,
                query_filter=Filter(must=scope.must_conditions()),
                limit=max(10, self.settings.retrieval_vector_topk * 2),
                with_payload=["repo"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("probe_related_failed err=%s", exc)
            return []
        candidate_repos: list[str] = []
        for point in resp.points:
            if point.score < self.settings.retrieval_min_score:
                continue
            slug = str((point.payload or {}).get("repo", ""))
            if slug and slug not in exclude and slug not in candidate_repos:
                candidate_repos.append(slug)
        if not candidate_repos:
            return []
        acc = resolve_access(
            user_id=user_id, is_admin=is_admin,
            workspace_id=workspace_id, repos=candidate_repos,
        )
        return [r for r in candidate_repos if not acc[r].researchable]

    @staticmethod
    def _build_access_notice(
        *,
        denied_repos: list[str],
        related_repos: list[str],
        hidden_files: list[str],
        access: dict[str, RepoAccessDecision],
        no_accessible: bool,
    ) -> str:
        """Compose the Ukrainian boundary message injected into the prompt so
        the answer explicitly tells the user what is out of bounds and to ask
        an admin."""
        blocked = sorted(set(denied_repos) | set(related_repos))
        lines: list[str] = []
        if blocked or hidden_files or no_accessible:
            lines.append(
                "# ⚠️ Access restrictions (you must tell the user in the answer)"
            )
        if no_accessible:
            names = ", ".join(f"`{r}`" for r in denied_repos) or "(all)"
            lines.append(
                "You do NOT have research access to these repositories: "
                f"{names}. Do not invent an answer — tell the user that "
                "research access to these repositories has not been granted, "
                "and that they should contact the administrators."
            )
            return "\n".join(lines)
        if blocked:
            names = ", ".join(f"`{r}`" for r in blocked)
            lines.append(
                "The question partly concerns functionality located in "
                f"repository(ies) {names}, to which you do NOT have research "
                "access. Answer ONLY based on the available context, and about "
                "the rest — say directly: «part of the functionality is "
                f"implemented in {names}, I do not have access to do research "
                "there — contact the administrators to be granted access»."
            )
        if hidden_files:
            tags = sorted({
                t for dec in access.values() for t in dec.sensitivity_tags
            })
            tag_str = (", ".join(tags)) if tags else "sensitive"
            lines.append(
                f"Some files ({tag_str}) are hidden by the access policy and are "
                "not included in the context — do not try to reconstruct their "
                "content; note that these parts are unavailable for research."
            )
        return "\n".join(lines)

    # ─── Tier 1 ──────────────────────────────────────────────────────

    def _collection_exists(self) -> bool:
        """One metadata call. An unreachable Qdrant answers TRUE.

        "I could not ask" is not "it is not there": returning False on a
        network blip would silently skip Tier 1 and tell the user to generate
        a vault they already have. Answering True sends the query down the
        old path, which fails on its own terms and classifies the real error.
        """
        try:
            self._qdrant.get_collection(self.settings.qdrant_collection)
            return True
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "doesn't exist" in text or "Not found" in text or "404" in text:
                return False
            logger.warning("collection_probe_failed err=%s", text[:200])
            return True

    def _qdrant_query(
        self,
        embedding: list[float],
        repos: list[str],
        *,
        scope: VectorScope,
    ) -> list[MultiRepoVaultHit]:
        """One Qdrant query with MUST(workspace) + MUST + OR(repo IN [...]) on
        a ready embedding.

        The embedding is computed once in `retrieve()` and is reused both here
        and for the cross-repo probe — so that we don't pay for the embed
        twice.
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchAny,
        )

        # ISOLATION. The repo list is who the caller ASKED about; the scope is
        # whose data exists as far as they are concerned. Both are `must`, so a
        # repo slug that happens to collide across tenants still cannot pull in
        # the other tenant's notes.
        qfilter = Filter(
            must=[
                *scope.must_conditions(),
                FieldCondition(key="repo", match=MatchAny(any=repos)),
            ]
        )
        try:
            response = self._qdrant.query_points(
                collection_name=self.settings.qdrant_collection,
                query=embedding,
                query_filter=qfilter,
                limit=self.settings.retrieval_vector_topk,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            # No collection yet (nobody generated a vault) or the vector store
            # is unreachable. Tier 1 is an ACCELERATOR, not a hard dependency —
            # degrade to grep + graph + code instead of failing the whole chat,
            # and let the caller tell the user what's missing.
            #
            # A CODE, not `str(exc)`. The raw text is Qdrant's whole response
            # body — it names the internal collection, reads as a crash, and
            # travelled to the browser and into message metadata verbatim.
            from src.llm.errors import classify_vector_store
            self.vault_unavailable = classify_vector_store(exc).code
            logger.warning("qdrant_query_failed collection=%s err=%s",
                           self.settings.qdrant_collection, exc)
            return []
        out: list[MultiRepoVaultHit] = []
        for point in response.points:
            if point.score < self.settings.retrieval_min_score:
                continue
            p = point.payload or {}
            out.append(
                MultiRepoVaultHit(
                    note_path=str(p.get("note_path", "")),
                    score=float(point.score),
                    type=str(p.get("type", "")),
                    module=p.get("module"),
                    repo=str(p.get("repo", "")),
                    symbols=list(p.get("symbols") or []),
                    keywords=list(p.get("keywords") or []),
                    content=str(p.get("content", "")),
                    path=p.get("path"),
                    cross_refs=list(p.get("cross_refs") or []),
                )
            )
        logger.info(
            "multi_repo_search repos=%s hits=%d", repos, len(out),
        )
        return out

    # ─── Tier 3: read code per repo ───────────────────────────────────

    @staticmethod
    def _symbol_targets(
        question: str, hits: list[MultiRepoVaultHit], min_len: int = 4,
    ) -> list[str]:
        """Identifiers worth chasing: those named in the question, plus symbol
        names carried by the vault hits. Shared by Tier 1.5 (grep) and Tier 2
        (graph) so both chase the same things.

        `min_len` differs per tier: grep needs 4+ chars or it drowns in noise,
        while the graph does an exact-name lookup so short names like `run`
        are both safe and useful there.
        """
        ident_re = re.compile(rf"\b[A-Za-z_][A-Za-z0-9_]{{{min_len - 1},}}\b")
        common_words = {
            "function", "method", "class", "interface", "import", "export",
            "const", "let", "var", "return", "true", "false", "null", "undefined",
            "code", "show", "where", "what", "which", "this", "that", "with",
            "from", "into", "have", "does", "data", "type", "purpose", "explain",
            "implement", "implementation", "знаходиться", "реалізовано",
        }
        question_idents = {
            m.group(0) for m in ident_re.finditer(question)
            if m.group(0).lower() not in common_words and not m.group(0).isdigit()
        }
        # CamelCase / snake_case are far more selective than bare words.
        specific = {i for i in question_idents if any(c.isupper() for c in i) or "_" in i}
        ordered: list[str] = sorted(specific or question_idents)

        hit_targets: list[str] = []
        for h in hits:
            for sym in (h.symbols or []):
                name = sym.rsplit("::", 1)[-1].rsplit("/", 1)[-1].strip()
                parts = [name] if ident_re.fullmatch(name) else []
                if "." in name:
                    parts += [p.lstrip("#").strip() for p in name.split(".")]
                for part in parts:
                    if part and len(part) >= min_len and ident_re.fullmatch(part):
                        hit_targets.append(part)

        out: list[str] = list(dict.fromkeys(ordered))
        for t in hit_targets:
            if len(out) >= 15:
                break
            if t not in out:
                out.append(t)
        return out

    def _grep_symbol_files(
        self,
        question: str,
        hits: list[MultiRepoVaultHit],
        repos: list[str],
    ) -> list[str]:
        """Finds the files where the identifiers from the question and from
        hits.symbols are defined (or mentioned). Does this through a
        subprocess grep — fast even on thousands of files.

        Returns: relative paths with the `{repo_slug}/` prefix.
        """
        import subprocess

        targets = set(self._symbol_targets(question, hits))
        if not targets:
            return []

        logger.info("grep_symbols targets=%s", targets)

        found: list[str] = []
        for repo in repos:
            repo_path = self.settings.repo_path(repo)
            if not repo_path.exists():
                continue
            try:
                # ripgrep if present, fallback to grep -rln. We search as a
                # whole word.
                pattern = "|".join(re.escape(t) for t in targets)
                # we use python grep so as not to depend on rg
                result = subprocess.run(
                    [
                        "grep", "-rlnE",
                        "--include=*.ts", "--include=*.tsx",
                        "--include=*.js", "--include=*.jsx",
                        "--include=*.vue", "--include=*.py",
                        "--include=*.go", "--include=*.php",
                        f"\\b({pattern})\\b",
                        str(repo_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                for raw in result.stdout.splitlines():
                    try:
                        rel = Path(raw).relative_to(repo_path)
                    except ValueError:
                        continue
                    rel_str = str(rel)
                    if any(part in {"node_modules", "dist", "build", ".next"}
                           for part in rel.parts):
                        continue
                    prefixed = f"{repo}/{rel_str}"
                    if prefixed not in found:
                        found.append(prefixed)
                    if len(found) >= 40:
                        break
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning("grep_failed repo=%s err=%s", repo, e)

        logger.info("grep_symbol_files found=%d", len(found))
        return found

    def _read_code_for_hits(
        self,
        hits: list[MultiRepoVaultHit],
        *,
        priority_files: list[str] | None = None,
        access: dict[str, RepoAccessDecision] | None = None,
        overview_files: list[str] | None = None,
    ) -> tuple[list[str], str, list[str]]:
        """For every hit — reads files from the corresponding repo_path.

        Returns: (files_read_list, code_bundle_markdown, hidden_files).
        `files_read` has the repo_slug/ prefix — so the LLM knows what came
        from where. If `priority_files` is passed — they are read first and
        in full.

        `overview_files` — the no-vault fallback pick. Read last, so it can
        only fill space the vault-driven material left, and with a tighter
        per-file cap: an unguided pick needs breadth across repos far more than
        it needs the tail of any one file.

        Stage 22: `access` gates every file. A file is skipped if the repo is
        not code-visible or the path falls under a deny-glob; such files are
        added to `hidden_files` (with the repo_slug/ prefix) for the boundary
        message.
        """
        from src.indexing.modules import _IGNORE_DIRS, _is_non_source_file
        from src.vault.reader import VaultReader

        reader = VaultReader(self.settings)
        extensions = set(self.settings.supported_extensions)

        files_read: list[str] = []
        code_blocks: list[str] = []
        hidden_files: list[str] = []

        def _allowed(repo_slug: str, rel: str) -> bool:
            """True if the file is code-visible for the caller. Records a
            hidden file (once) when it is not."""
            if access is None:
                return True
            dec = access.get(repo_slug)
            if dec is None or not dec.code_visible or not dec.path_visible(rel):
                prefixed = f"{repo_slug}/{rel}"
                if prefixed not in hidden_files:
                    hidden_files.append(prefixed)
                return False
            return True

        max_chars = self.settings.max_code_tokens_per_qa * 4  # ~4 chars/token
        used_chars = 0

        def _read_one(prefixed: str, limit: int) -> bool:
            """Read one repo-prefixed file into the bundle. The single place
            where a path turns into prompt text — so the access gate has no
            second door to guard. False when the file was skipped."""
            nonlocal used_chars
            if prefixed in files_read:
                return False
            try:
                repo_slug, rel = prefixed.split("/", 1)
            except ValueError:
                return False
            if not _allowed(repo_slug, rel):
                return False
            repo_root = self.settings.repo_path(repo_slug)
            p = repo_root / rel
            # The candidate paths come from grep output, graph rows and vault
            # frontmatter — none of which this module owns. A `..` in there
            # would read outside the repo entirely, where no access rule can
            # reach it, so containment is checked before the open.
            try:
                if not p.resolve().is_relative_to(repo_root.resolve()):
                    logger.warning("code_read_outside_repo repo=%s rel=%s",
                                   repo_slug, rel)
                    return False
            except OSError:
                return False
            if not p.exists() or not p.is_file():
                return False
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            if len(text) > limit:
                text = text[:limit] + "\n# … truncated"
            block = (
                f"### {prefixed}\n```{p.suffix.lstrip('.')}\n"
                f"{self._numbered(text)}\n```\n"
            )
            files_read.append(prefixed)
            code_blocks.append(block)
            used_chars += len(block)
            return True

        # Cache of resolved module paths so as not to re-read one module
        # several times
        # Key: (repo, ref)
        resolved: dict[tuple[str, str], str | None] = {}

        # ─── Priority files (from grep over symbols) — read first ─────
        # This effectively guarantees that if the question mentions
        # isTotalsAvailable, the files where it definitely lives will end up
        # in the bundle.
        for prefixed in (priority_files or []):
            _read_one(prefixed, 30_000)

        for h in hits:
            repo_path = self.settings.repo_path(h.repo)
            if not repo_path.exists():
                logger.debug("repo_path_missing %s", repo_path)
                continue

            # Determine paths to read
            paths_to_read: list[str] = []

            if h.type == "module" and h.path:
                paths_to_read.append(h.path)
            elif h.cross_refs:
                # Feature/integration — we follow into the member modules
                for ref in h.cross_refs:
                    if not ref.startswith("modules/"):
                        continue
                    key = (h.repo, ref)
                    if key in resolved:
                        if resolved[key]:
                            paths_to_read.append(resolved[key])  # type: ignore[arg-type]
                        continue
                    note_rel = ref if ref.endswith(".md") else f"{ref}.md"
                    mod_note = reader.read(h.repo, note_rel)
                    p = mod_note.metadata.get("path") if mod_note else None
                    resolved[key] = p
                    if p:
                        paths_to_read.append(p)

            # Read files from each path
            for rel_path in paths_to_read:
                module_dir = repo_path / rel_path
                if not module_dir.exists() or not module_dir.is_dir():
                    continue
                file_count = 0
                # We collect the valid files first
                candidates = [
                    p for p in module_dir.rglob("*")
                    if p.is_file()
                    and p.suffix in extensions
                    and not any(part in _IGNORE_DIRS for part in p.parts)
                    and not _is_non_source_file(p)
                ]
                # We sort: more specific names (Controller/Project/Service) get
                # priority; then by size (medium files are the most useful, too
                # large ones get truncated)
                def _file_score(p: Path) -> tuple[int, int]:
                    name_lower = p.name.lower()
                    # Negative score — a smaller number = higher priority
                    priority = 0
                    # Top-level controllers / managers / standard entry points
                    for kw in ("controller", "service", "manager", "project", "index"):
                        if kw in name_lower:
                            priority -= 10
                    # types.ts — lower priority (lookup, not logic)
                    if name_lower.endswith("types.ts") or name_lower == "types.ts":
                        priority += 5
                    return (priority, p.stat().st_size)
                candidates.sort(key=_file_score)

                for p in candidates:
                    if file_count >= 60:  # cap per module path
                        break
                    rel = str(p.relative_to(repo_path))
                    # truncate only the very large ones
                    if _read_one(f"{h.repo}/{rel}", 25_000):
                        file_count += 1

        # ─── Fallback pick — last, and only into leftover budget ─────
        for prefixed in (overview_files or []):
            if used_chars >= max_chars:
                logger.info("fallback_read_budget_reached chars=%d", used_chars)
                break
            _read_one(prefixed, _FALLBACK_MAX_CHARS_PER_FILE)

        # Token budget safety — we cap the bundle
        bundle = "\n".join(code_blocks)
        if len(bundle) > max_chars:
            bundle = bundle[:max_chars] + "\n# … bundle truncated to fit budget"
        logger.info(
            "multi_repo_code_read files=%d hidden=%d total_chars=%d",
            len(files_read),
            len(hidden_files),
            len(bundle),
        )
        return files_read, bundle, hidden_files

    # ─── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _numbered(text: str) -> str:
        """Prefix `   N  ` (right-aligned) to every line — so the LLM can
        quote line numbers exactly in markdown links."""
        lines = text.splitlines() or [""]
        width = max(3, len(str(len(lines))))
        return "\n".join(
            f"{i:>{width}}  {line}" for i, line in enumerate(lines, 1)
        )

    @staticmethod
    def _format_vault_hits(hits: Iterable[MultiRepoVaultHit]) -> str:
        if not hits:
            return "(no vault notes matched)"
        lines = []
        for h in hits:
            preview = h.content[:300] if h.content else ""
            lines.append(
                f"- [[{h.repo}/{h.note_path}]] (score {h.score:.3f}): {preview}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_history(
        history: list[tuple[str, str]],
        max_exchanges: int = 3,
        max_chars_per_msg: int = 800,
    ) -> str:
        if not history:
            return "(first message in this chat — there is no previous context)"
        relevant = history[-(max_exchanges * 2):]
        parts: list[str] = []
        for role, content in relevant:
            if len(content) > max_chars_per_msg:
                content = content[:max_chars_per_msg] + "… [truncated]"
            parts.append(f"{role.upper()}:\n{content}")
        return "\n\n---\n\n".join(parts)
