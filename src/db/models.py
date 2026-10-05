"""ORM models for Celmis — projects, project_repos, chats, messages.

Architecture:
    Project = a group of repos (e.g. "Acme Platform" with frontend+backend)
    ProjectRepo = N:M link between projects and repos (repo_slug as a string —
                  because repo metadata lives in Bitbucket/Qdrant, not with us)
    Chat = a conversation bound to a Project (or null = ad-hoc personal chat)
    Message = a single user/assistant turn in a Chat

Multi-tenancy:
    Shared mode (Phase 1) — all users see all projects/chats.
    `owner_user_id` is stored for audit, but is not filtered on in read queries.
    If we ever add isolation — changes go in the repository layer only, not in
    the schema.

Versioning:
    Migrations go through Alembic — `alembic/versions/`. No direct DDL changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    pass


def _uuid_pk() -> str:
    """Generate UUID v4 as string (for the PK)."""
    return str(uuid.uuid4())


# ════════════════════════════════════════════════════════════════════
# Project — a group of repos
# ════════════════════════════════════════════════════════════════════
class Project(Base, TimestampMixin):
    """A logical group of repos, for example "Acme Platform" = frontend + backend.

    Q&A against a project searches Qdrant with the OR-filter `repo IN (...slugs)`.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=_uuid_pk,
    )
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,  # null while there is no auth in the MVP
        index=True,
    )

    # Relationships
    repos: Mapped[list[ProjectRepo]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    chats: Mapped[list[Chat]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="noload",  # a separate query when needed
    )

    __table_args__ = (
        UniqueConstraint("name", "owner_user_id", name="uq_projects_name_owner"),
    )


# ════════════════════════════════════════════════════════════════════
# ProjectRepo — N:M link
# ════════════════════════════════════════════════════════════════════
class ProjectRepo(Base, TimestampMixin):
    """Binding of a repo to a project. One repo can be in several projects.

    `repo_slug` is our local slug (e.g. "acme-frontend"), the same one that is
    in the Qdrant payload.repo and in the vault directory name.

    `role` — semantic label: "frontend" / "backend" / "shared" / etc.
    Used in the UI for previews and potentially in a prompt prefix so the LLM
    knows where a file belongs.
    """

    __tablename__ = "project_repos"

    project_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    repo_slug: Mapped[str] = mapped_column(
        String(200),
        primary_key=True,
    )
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    project: Mapped[Project] = relationship(back_populates="repos")

    __table_args__ = (
        Index("ix_project_repos_repo_slug", "repo_slug"),
    )


# ════════════════════════════════════════════════════════════════════
# Chat — a conversation
# ════════════════════════════════════════════════════════════════════
class Chat(Base, TimestampMixin):
    """A Q&A conversation. Bound to a project (multi-repo context) OR
    to a single repo for backwards-compat with the old Streamlit Chat UX.

    Auto-renamed from the first user message if name=NULL.
    """

    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=_uuid_pk,
    )
    project_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # For single-repo chats (migrated from SQLite) — we store repo_slug directly
    repo_slug: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # Stage 21 — workspace isolation for Q&A history.
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")

    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    project: Mapped[Project | None] = relationship(back_populates="chats")
    messages: Mapped[list[Message]] = relationship(
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="Message.id",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_chats_updated_at_desc", "updated_at", postgresql_using="btree"),
    )


# ════════════════════════════════════════════════════════════════════
# Message — a single turn
# ════════════════════════════════════════════════════════════════════
class Message(Base):
    """A user or assistant message in a chat.

    `meta` (JSONB) — telemetry for assistant messages: type, route,
    vault_hits, files_read, tokens_in/out, elapsed_s, etc.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    chat: Mapped[Chat] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_messages_chat_id_id", "chat_id", "id"),
    )


# ════════════════════════════════════════════════════════════════════
# RepoReviewPolicy — per-repo AI-reviewer configuration (Stage 10)
# ════════════════════════════════════════════════════════════════════
class RepoReviewPolicy(Base, TimestampMixin):
    """Per-repo policy for the multi-agent PR reviewer.

    - `prompt_template` — natural-language rules that the architect agent will
      treat as authoritative (in addition to the built-in system prompt).
    - `target_branches` — only review PRs whose `base_branch` is in this list.
      Empty list / null = review every branch.
    - `folder_rules` — list of `{"pattern": "glob", "prompt": "rule"}`. Each
      rule fires when any changed file matches the glob.
    - `department` — free-form team tag. Used ONLY by the admin-panel list
      filter (`GET /api/review-policies?department=…` + fuzzy search); it has
      no effect on review behaviour, routing or notifications.
    - `disabled_agents` — agent names that must NOT run for this repo
      (e.g. `["quality", "tests"]`). Skipped agents make no LLM call, spend no
      tokens and produce no findings.
    - `suppressed_rules` — rule ids the prefilter hides for this repo; NULL
      inherits `ReviewSettings.suppressed_rules`, a list replaces it.

    Row exists per repo_slug; absence means "default behaviour, no overrides".
    """

    __tablename__ = "repo_review_policies"

    repo_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.true(),
    )
    prompt_template: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="",
    )
    # NULL inherits the workspace review defaults (then: every branch); a
    # list — [] included, meaning "every branch" — is this repository's own.
    target_branches: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    folder_rules: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )
    department: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-agent model overrides (Stage 11 — BYOK).
    # NULL = fall back to workspace default from ReviewSettings.
    #
    # THE model, for every layer that reads a repo policy. The blob below
    # deliberately carries no `model` key: two sources for one field is the
    # failure this project keeps hitting (the vendor prefix asked about one
    # model while the runtime called another, twice), and moving the model
    # into the blob would mean migrating five live columns that
    # src/review/orchestrator.py reads on every review. There is no
    # `compliance_model` because there never was one; compliance inherits its
    # model from /settings/llm, and `resolve_agent_llm` is where that is said.
    architect_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    security_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tests_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Per-repo per-agent LLM knobs the columns above cannot express:
    # {"architect": {"max_output_tokens": 32768, "reasoning": "high"}, …},
    # one entry per name in `src.review.settings.REVIEW_AGENTS`.
    #
    # ONE JSON column rather than twelve more Text ones, and shaped exactly
    # like the workspace `agents` blob in the /settings/llm config, so the
    # validator (`llm._validate_agent_entry`), the resolver
    # (`resolve_agent_llm`) and the UI control are shared between the two
    # screens instead of twinned — this layer WINS over the workspace one, and
    # a winning layer that validates differently is how an invalid combination
    # reaches a provider.
    #
    # NULLABLE with no server default: every row written before this column
    # existed reads back as NULL, and NULL is exactly "inherit" — the same
    # thing an absent key means at every other layer of the chain. A `{}`
    # default would say the same thing in a second dialect.
    agent_llm_overrides: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Per-repo per-agent system_prompt overrides (Stage 12).
    # {"architect": "...", "security": "...", ...}. Missing/empty = inherit
    # /admin/agents override → agent default.
    agent_prompt_overrides: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )

    # Per-repo per-agent team guidelines (2.3.2, migration c5d6e7f8a9b0):
    # {"security": "- Flag …", …}. ADDED to the agent's prompt, at most 2000
    # characters each; missing/empty = inherit the workspace's guidelines.
    # `agent_prompt_overrides` above is the advanced mode that REPLACES the
    # prompt; src/review/prompt_guidelines.py says how the two combine.
    agent_prompt_guidelines: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )
    # Agents whose guidelines here are added to the workspace's instead of
    # replacing them — an explicit opt-in per agent; [] = replace (Kodus).
    agent_guidelines_extend: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )

    # Per-repo MCP evidence sources (Stage 13).
    # [{"name":"sentry","url":"https://mcp.sentry.dev","auth_type":"oauth",
    #   "api_key_ref":"mcp:sentry", "allowed_tools":["get_issue","list_issues"],
    #   "trigger_patterns":["SENTRY-[A-Z0-9]+"]}, ...]
    mcp_sources: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]",
    )

    # Per-repo agent kill-switch: ["quality", "tests", ...]. The orchestrator
    # skips these before dispatching the parallel run, so a disabled agent
    # costs nothing. NULL inherits the workspace review defaults
    # (`WorkspaceReviewDefaults.disabled_agents`); a list — [] included,
    # meaning "every agent runs" — is this repository's own answer and
    # replaces the workspace's outright.
    disabled_agents: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Rule ids the review prefilter hides for this repo, e.g.
    # ["quality.todo", "tests.no-coverage"]. Replaces
    # `ReviewSettings.suppressed_rules` outright when set, so a repo can
    # narrow the default as well as widen it.
    #
    # NULLABLE with no server default where `disabled_agents` is NOT NULL
    # '[]', because here the empty list and the absent value are different
    # answers: NULL is "inherit the code default" — every row written before
    # this column existed — and [] is "hide nothing". Flattening the two into
    # one empty list would make "inherit" unsayable.
    suppressed_rules: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Whether the LLM false-positive veto runs for this repository.
    #
    # It shipped ON, which was never the intent: the only way to switch it off
    # was to name "verifier" in `disabled_agents`, and an unconfigured
    # repository names nothing. The veto is a second model call over every
    # finding in the review — the slowest single call the pipeline makes — and
    # whether it earns its price is a judgement about one repository's
    # tolerance for noise, not a charge to levy on every installation.
    #
    # NULLABLE for the reason `suppressed_rules` is: NULL is "inherit the
    # install default" (REVIEW_VERIFIER_ENABLED, itself False) and a stored
    # value is "this repository has decided". Flattened into NOT NULL DEFAULT
    # FALSE, an operator raising the install default would watch it apply to
    # nothing, because every row would already hold an answer nobody typed.
    #
    # Not an entry in `disabled_agents`: that list holds AGENTS, and the veto
    # is a stage that runs after them over their combined output. Squeezing a
    # stage into the agent deny-list is what made the default un-invertible.
    verifier_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True,
    )

    # Gitignore-ish globs for paths this repository's review never reads,
    # e.g. ["docs/**", "*.snap", "migrations/*.py"]. Applied on top of the
    # install-wide skip lists; see src/review/ignore_globs.py for the rules.
    # NULL and [] both mean "nothing extra".
    ignore_globs: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # The lowest severity posted as an inline PR comment: critical | error |
    # warning | info. NULL inherits the default, which posts everything.
    # Findings under it are still counted in the summary and stored.
    comment_min_severity: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Review output, per repository (Kodus-style customization) ──
    # NULL inherits the workspace review defaults, then the built-in ON —
    # never off. True/False is this repository's own decision. (They shipped
    # with a server default TRUE; migration e4c8a1f7b2d9 turned the TRUE no
    # operator could tell apart from "never answered" into NULL, which reads
    # the same until a workspace default says otherwise.)
    #: Post the PR summary for this repository.
    summary_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Extra instructions for the summary writer. NULL/empty = inherit.
    summary_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Post a "review started" comment when a review begins.
    started_comment_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True,
    )
    #: Output language code ("en", "uk", …; src.llm.prompts.language).
    #: NULL = the workspace's review_language.
    review_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Inline-comment cap for this repo (1..100). NULL = REVIEW_MAX_INLINE_COMMENTS.
    max_inline_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── 2.3.0 review settings (migration f1a2b3c4d5e6) ──
    # Every one NULLABLE with no server default, and NULL is "inherit the
    # workspace review default, then the built-in" — the built-ins live in
    # `src.review.review_defaults.BUILTIN_DEFAULTS`, not here, so a row that
    # predates the columns reads exactly like one that never answered.
    # `WorkspaceReviewDefaults` carries the same columns, one layer down.
    #: Opt-in agents switched ON for this repo (business_logic, …). A list —
    #: [] included — replaces the workspace's; `disabled_agents` still wins.
    enabled_agents: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: Review draft PRs too (built-in: drafts are skipped).
    run_on_drafts: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Approve the PR when the review leaves nothing to post.
    approve_when_clean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: "Request changes" when a critical finding exists.
    request_changes_on_critical: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True)
    #: A skipped / blocked review leaves a status note (built-in: on).
    status_feedback: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Render fixes as the provider's committable "suggestion" blocks.
    committable_suggestions: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Whether the severity threshold and inline cap apply to rule findings.
    apply_filters_to_rules: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: comment | description — `review_defaults.SETTING_CHOICES` for each.
    summary_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: nothing | append | replace.
    summary_on_new_commits: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: append | complement | replace.
    summary_existing_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: How review comments are written; given to every agent. ≤2000 chars.
    base_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The "review started" comment ({commit} {agents} {files} {pr_number}).
    message_started: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The final summary's header line (same placeholders).
    message_finished_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-repo models for the 2.3.0 finders, beside the five `<agent>_model`
    # columns above and for their reason: the model of THIS layer is a column
    # and never a key of `agent_llm_overrides`. `_model_field_for` in the
    # policy router and `resolve_agent_llm` find them by name.
    performance_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_logic_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_repo_review_policies_department", "department"),
    )


# ════════════════════════════════════════════════════════════════════
# WorkspaceReviewDefaults — the workspace layer under every repo policy
# ════════════════════════════════════════════════════════════════════
class WorkspaceReviewDefaults(Base, TimestampMixin):
    """What a repository's review does when its own policy says nothing.

    One row per workspace, every setting nullable: NULL is "inherit the
    install default" (env / ReviewSettings / built-in), exactly as NULL on a
    `RepoReviewPolicy` column is "inherit THIS row". Resolution, everywhere a
    review reads one of these (src/review/review_defaults.py):

        repo policy (non-null) > workspace default (non-null) > install > built-in

    Per-agent model / output ceiling / reasoning and the review language are
    NOT columns here: their workspace layer already lives in the workspace LLM
    config blob (`agents`, `review_language`, /settings/llm), and one setting
    with two workspace homes is the failure this project keeps hitting. The
    review-defaults API reads and writes them there.
    """

    __tablename__ = "workspace_review_defaults"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: Agents that do not run unless a repository says otherwise.
    disabled_agents: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: The LLM false-positive veto; NULL = REVIEW_VERIFIER_ENABLED.
    verifier_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Lowest severity posted inline; NULL = post everything.
    comment_min_severity: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Inline-comment cap 1..100; NULL = REVIEW_MAX_INLINE_COMMENTS.
    max_inline_comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_comment_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Paths no repository of this workspace reviews unless it says otherwise.
    ignore_globs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: Base branches reviewed; NULL / [] = every branch.
    target_branches: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: Prefilter rule deny-list; NULL = ReviewSettings.suppressed_rules.
    suppressed_rules: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # ── 2.3.0 (migration f1a2b3c4d5e6) — the same settings, and the same
    # meaning, as the `RepoReviewPolicy` columns of the same names; NULL =
    # the built-in in `src.review.review_defaults.BUILTIN_DEFAULTS`.
    enabled_agents: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    run_on_drafts: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    approve_when_clean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    request_changes_on_critical: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True)
    status_feedback: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    committable_suggestions: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    apply_filters_to_rules: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_on_new_commits: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_existing_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_started: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_finished_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)


# ════════════════════════════════════════════════════════════════════
# ReviewRule — the review rules library (workspace-wide or per repository)
# ════════════════════════════════════════════════════════════════════
class ReviewRule(Base, TimestampMixin):
    """One review rule the agents are told to enforce.

    `repo_slug` NULL = every repository of the workspace; a slug scopes the
    rule to that repository, and a repository rule with the same title (case
    folded) replaces the workspace one for that repository. Only `active`
    rules reach a review (src/review/rules_store.py composes them,
    src/review/policy_rules.py renders them). Everything a machine wrote —
    `generated`, `imported`, `agent` — arrives `pending` for a person to
    approve; `rejected` is kept so a re-import does not propose it again.

    The legacy per-repo `RepoReviewPolicy.folder_rules` keep working beside
    these and are not copied here (see migration a7b8c9d0e1f2).
    """

    __tablename__ = "review_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the agent is told, ≤ 2000 characters.
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    #: Files the rule is about; NULL/empty = every changed file.
    path_glob: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: info | warning | error | critical — the severity a violation reports at.
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="warning")
    #: The LLM agents the rule is for; [] = every agent.
    agents: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    examples_good: Mapped[str | None] = mapped_column(Text, nullable=True)
    examples_bad: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why a generated rule was proposed — shown to the person approving it.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: active | pending | rejected
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    #: manual | library | generated | imported | agent
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    #: Library id, file path (#section) or job id the rule came from.
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_review_rules_scope", "workspace_id", "repo_slug", "status"),
    )


class ReviewRuleJob(Base, TimestampMixin):
    """A "Generate rules" / "Import from repo files" run and its progress."""

    __tablename__ = "review_rule_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid_pk)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    #: generate | import
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: queued | running | completed | failed
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    progress: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: {"created": [ids], "skipped": n, "note": "..."}
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_review_rule_jobs_ws", "workspace_id", "created_at"),
    )


# ════════════════════════════════════════════════════════════════════
# ComplianceCheck — first-class policy rules that hard-block APPROVE
# (Stage 14). See src.review.compliance.ComplianceAgent for enforcement.
# ════════════════════════════════════════════════════════════════════
class ComplianceCheck(Base, TimestampMixin):
    """One compliance rule. Runs as its own agent after main review pipeline.

    - `scope` "workspace" applies to every repo; "repo:<slug>" scopes to one.
    - `glob_pattern` matches changed file paths.
    - `rule` is the human-readable requirement fed to the compliance agent.
    - `blocking` — if any blocking check fails, verdict cannot be APPROVE.
    """

    __tablename__ = "compliance_checks"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="workspace")
    glob_pattern: Mapped[str] = mapped_column(Text, nullable=False, server_default="**")
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="error")
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.true())
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.true())
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_compliance_scope", "scope"),
    )


# ════════════════════════════════════════════════════════════════════
# Teams + RBAC (Stage 14)
# ════════════════════════════════════════════════════════════════════
class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_team_members_user", "user_id"),
    )


class RepoTeamAccess(Base):
    __tablename__ = "repo_team_access"

    repo_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    permission: Mapped[str] = mapped_column(Text, nullable=False, server_default="review")
    # permission: 'admin' | 'review' | 'read'

    __table_args__ = (
        Index("ix_repo_team_access_repo", "repo_slug"),
    )


class RepoAccessRule(Base):
    """Fine-grained *research* visibility of a repo for a team (Stage 22).

    Whereas :class:`RepoTeamAccess` governs PR-review write permissions on a
    *whole* repo, this table governs what a team may **learn** about a repo
    through Q&A / graph / vector search — down to individual paths.

    Resolution (see ``src/access/resolver.py``):

      * ``visibility``: coarse gate — ``none`` (repo invisible for research),
        ``metadata`` (docs / architecture notes only, no source code), or
        ``code`` (source code readable too).
      * ``deny_globs``: paths always hidden even at ``code`` level — creds,
        crypto/algorithm impls, DB-connection code, secret verification.
        Deny wins over allow.
      * ``allow_globs``: if non-empty, an allow-list — only matching paths are
        code-visible (deny still subtracts from it).
      * ``sensitivity_tags``: informational labels surfaced in the boundary
        notice ("credentials", "crypto", …) — no enforcement of their own.

    Fall-open convention (mirrors ``_effective_repo_permission``): if **no**
    rule exists for a repo in the active workspace, every member has full
    ``code`` access. Once *any* rule exists for that repo, teams without a
    matching rule get ``none`` (default-deny). Global admins always bypass.
    """

    __tablename__ = "repo_access_rules"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    team_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="code")
    # visibility: 'none' | 'metadata' | 'code'
    allow_globs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    deny_globs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    sensitivity_tags: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "team_id", "repo_slug",
            name="uq_repo_access_rule",
        ),
        Index("ix_repo_access_rules_repo", "workspace_id", "repo_slug"),
    )


# ════════════════════════════════════════════════════════════════════
# Stage 15 — cross-repo intelligence + Grafana-style notifications
# ════════════════════════════════════════════════════════════════════


class NotificationChannel(Base):
    """Grafana-style contact point. `webhook_url` is the transport; `kind`
    picks the payload adapter (slack blocks / discord embeds / gchat cards
    / raw JSON)."""

    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChannelBinding(Base):
    """Route (repo_slug OR workspace, event) → channel."""

    __tablename__ = "channel_bindings"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    min_severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="info")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.true())

    __table_args__ = (
        Index("ix_channel_bindings_repo_event", "repo_slug", "event"),
    )


class OwnershipSnapshot(Base):
    """One computed ownership graph per repo. Overwritten on rebuild."""

    __tablename__ = "ownership_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    computed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    lookback_days: Mapped[int] = mapped_column(nullable=False, server_default="90")
    paths: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        Index("ix_ownership_repo", "repo_slug"),
    )


class RepoSummary(Base):
    """Auto-generated architecture summary (Markdown)."""

    __tablename__ = "repo_summaries"

    repo_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    summary_md: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    computed_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeprecatedSymbol(Base):
    """Tracked deprecation. Consumers list is refreshed by scanner."""

    __tablename__ = "deprecated_symbols"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    replacement: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_removal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    deprecated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    deprecated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    consumers: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    __table_args__ = (
        Index("ix_deprecated_repo_symbol", "repo_slug", "symbol", unique=True),
    )


# ════════════════════════════════════════════════════════════════════
# Stage 18 — durable job queue + repo index state
# ════════════════════════════════════════════════════════════════════


class SyncJob(Base):
    """Postgres-backed job queue row. Dequeue uses `SELECT ... FOR UPDATE
    SKIP LOCKED` so multiple workers coexist without stepping on each
    other. Retries with exponential backoff via `next_run_at`."""

    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: The tenant this job belongs to — NULL when it genuinely has none.
    #:
    #: Deliberately nullable and WITHOUT the `server_default="default"` that
    #: every other workspace column in this file carries. A nightly
    #: ownership_rebuild or a cross-repo materialize is queue-wide
    #: maintenance; stamping it "default" would hand one tenant a row that
    #: is not theirs, which is the exact failure this column exists to
    #: prevent. NULL means "no tenant" and reads as global-admin-only.
    workspace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(nullable=False, server_default="5")
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_sync_jobs_workspace", "workspace_id", "next_run_at"),
    )


class RepoIndexState(Base):
    """One row per repo — last known indexed HEAD sha. Enables the
    indexer to skip a full rebuild when `HEAD == last_indexed_sha` and
    walk only `git diff last_indexed_sha..HEAD` otherwise."""

    __tablename__ = "repo_index_state"

    repo_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    last_indexed_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_rebuild_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_incremental_files: Mapped[int] = mapped_column(nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: When the REMOTE was last asked, and what it said.
    #:
    #: Separate from the indexing columns above because "indexed three days
    #: ago" answers a different question from "current as of this morning".
    #: A row with `last_checked_at` NULL has never been asked; one where
    #: `last_remote_sha == last_indexed_sha` is up to date and known to be.
    #: Collapsing those into a single timestamp produces a screen that cannot
    #: tell "no changes" from "nobody looked".
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_remote_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why the last check failed. Not `last_error`, which belongs to indexing:
    #: an index that succeeded and a check that cannot reach the remote are
    #: unrelated conditions with unrelated remedies.
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ════════════════════════════════════════════════════════════════════
# Stage 19 — OAuth 2.1 + multi-tenant workspaces
# ════════════════════════════════════════════════════════════════════


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    allowed_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class OAuthAuthCode(Base):
    __tablename__ = "oauth_auth_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(Text, nullable=False, server_default="S256")
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_workspace_members_user", "user_id"),
    )


class OAuthRefreshToken(Base):
    """OAuth refresh token — SHA-256 hashed. Rotation-aware chain
    (family_id) so a reused token invalidates the whole chain."""

    __tablename__ = "oauth_refresh_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    family_id: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())

    __table_args__ = (
        Index("ix_refresh_family", "family_id"),
        Index("ix_refresh_user_client", "user_id", "client_id"),
    )


# ════════════════════════════════════════════════════════════════════
# Stage 23 — workspace LLM budgets + spend ledger
# ════════════════════════════════════════════════════════════════════


class WorkspaceBudget(Base):
    """Per-workspace spend cap. `hard_stop` decides whether exceeding the cap
    blocks further LLM calls or merely raises an alert."""

    __tablename__ = "workspace_budgets"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    monthly_usd_cap: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    alert_pct: Mapped[int] = mapped_column(nullable=False, server_default="80")
    hard_stop: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class LlmSpend(Base):
    """Append-only ledger of LLM cost, written by every surface (chat, review
    agents, embeddings). Reviews used to be the only tracked cost — this makes
    a workspace budget meaningful across all of them."""

    __tablename__ = "llm_spend"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    # Where the spend came from: qa (chat), review (PR agents), embeddings.
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    # For the review surface — which agent (architect/security/quality/tests/
    # verifier/compliance/breaking_change); NULL for chat & embeddings.
    agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # 'openrouter_actual' when OpenRouter reported the real charge, otherwise
    # 'litellm_estimate' / 'unknown' — surfaced so cost figures aren't trusted
    # more than they deserve.
    cost_source: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    tokens_in: Mapped[int] = mapped_column(nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(nullable=False, server_default="0")
    cached_tokens_in: Mapped[int] = mapped_column(nullable=False, server_default="0")
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What the call was FOR — module_prd, qa_answer, automation_interpret,
    #: deps_report. The surface says which part of the product spent it; this
    #: says which job inside that part, which is the difference between "the
    #: vault cost $40" and "$34 of it was integration guides".
    operation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_llm_spend_ws_time", "workspace_id", "created_at"),
        Index("ix_llm_spend_surface", "surface"),
        # Both breakdowns the Usage page draws scan a workspace's window and
        # group; without these they are sequential scans that get slower every
        # day the ledger grows.
        Index("ix_llm_spend_ws_repo", "workspace_id", "repo_slug"),
        Index("ix_llm_spend_ws_op", "workspace_id", "operation"),
    )


# ════════════════════════════════════════════════════════════════════
# Stage 23 — review-finding feedback, password reset, workspace invites
# ════════════════════════════════════════════════════════════════════


class FindingFeedback(Base):
    """Human verdict on one review finding.

    Without this the review loop is a monologue: agents post findings, nobody
    records which were noise, and the signal-to-noise ratio never improves.
    `finding_key` is a stable hash of (run_id, file, line, rule/title) so the
    verdict survives re-runs that reorder findings.
    """

    __tablename__ = "finding_feedback"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    finding_key: Mapped[str] = mapped_column(Text, nullable=False)
    repo_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str | None] = mapped_column(Text, nullable=True)
    # accepted | dismissed
    state: Mapped[str] = mapped_column(Text, nullable=False)
    # free-text or a preset: false_positive | wont_fix | not_relevant | style
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("run_id", "finding_key", name="uq_finding_feedback"),
        Index("ix_finding_feedback_run", "run_id"),
        Index("ix_finding_feedback_agent", "agent", "state"),
    )


# ════════════════════════════════════════════════════════════════════
# ReviewIssue — one review finding followed across the runs of one PR
# ════════════════════════════════════════════════════════════════════
class ReviewIssue(Base):
    """A finding with an identity that survives a new commit.

    Findings live per run (a JSON blob on the SQLite run row) and nothing tied
    the finding of push N to the same finding on push N+1, so "was it fixed?"
    could not be asked. An issue is keyed by a FINGERPRINT —
    sha256(rule_id | file_path | normalised title), deliberately without the
    line (a push above it shifts the line) and without the run id — and is
    scoped to one pull request: the same defect on two PRs is two issues,
    each with its own lifecycle.

    status: open | fixed | dismissed | resolved
      - fixed: a later run on the same PR no longer found it AND its file
        changed between the two heads (resolution_source=auto_next_commit),
        or someone said so by hand (manual);
      - dismissed: a person said it is noise (manual, or finding feedback);
      - resolved: closed for another reason (the PR was closed unmerged).

    category is derived (bug | security | performance | maintainability |
    style | other) — see `src.review.issues.categorize` for the mapping.
    """

    __tablename__ = "review_issues"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid_pk)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The text of the flagged line (see issues.plan_sync): an unrepeated
    #: issue whose line is still in the new diff is not fixed.
    anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="other")
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="warning")
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # open | fixed | dismissed | resolved
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    # auto_next_commit | manual | feedback | pr_closed
    resolution_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    pr_repo: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixed_in_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "repo_slug", "pr_number", "fingerprint",
            name="uq_review_issue_pr_fingerprint",
        ),
        Index("ix_review_issues_ws_status", "workspace_id", "status"),
        Index("ix_review_issues_ws_seen", "workspace_id", "first_seen_at"),
        Index("ix_review_issues_pr", "workspace_id", "pr_provider", "pr_repo", "pr_number"),
    )


# ════════════════════════════════════════════════════════════════════
# ReviewPullRequest — the PRs Celmis reviewed, and what became of them
# ════════════════════════════════════════════════════════════════════
class ReviewPullRequest(Base):
    """One pull request as the review pipeline knows it.

    Upserted after every review run (title, author, head, last review status,
    how many reviews) and on the provider's close/merge webhook (state). The
    run rows live in SQLite and know nothing about a PR's fate, so "issues
    still open on merged PRs" had nowhere to be answered from.

    `file_hashes` is {path: sha256 of that file's section of the last reviewed
    diff}. Comparing it with the next run's is how "the file changed between
    the two heads" is decided for the fixed-in-next-commit check without
    fetching the commit range from the provider.
    """

    __tablename__ = "review_pull_requests"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid_pk)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    repo_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # open | merged | closed
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    head_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    # success | skipped | failed — the last review's outcome, as the list shows it
    last_review_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviews_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    file_hashes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider", "repo", "number",
            name="uq_review_pull_request",
        ),
        Index("ix_review_pull_requests_ws_updated", "workspace_id", "updated_at"),
    )


class PasswordResetToken(Base):
    """Single-use password-reset token (SHA-256 hashed, short-lived)."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (Index("ix_reset_user", "user_id"),)


class AgentSession(Base, TimestampMixin):
    """One embedded Claude Code run: a repo + a task, executed headlessly on
    the server. The session outlives the browser — events append to
    `agent_session_events` and the UI replays + tails them, so a run started
    from a phone keeps going after the tab closes."""

    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The first repo of the session. Kept as a plain column because everything
    # written before multi-repo reads it — the spend ledger, the push
    # notification, the session list — and because a one-repo session is still
    # the common case.
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    # Every repo the session cloned, in pick order. Empty on rows that predate
    # multi-repo, which is why `slugs` below is the only supported reader.
    repo_slugs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # The project the set came from, when it came from one. Deliberately not a
    # foreign key: a finished session is a record of what ran, and it must not
    # disappear because someone tidied the project away afterwards.
    project_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # queued | running | paused | done | error | cancelled
    #
    # `paused` is new and is what makes a session resumable: no process, no
    # slot held, branch already pushed, transcript stored. It is deliberately
    # NOT "running with nothing happening" — the one-live-session-per-workspace
    # rule counts only queued and running, so a paused session costs a tenant
    # nothing while it waits for its owner to come back.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    # How to run it: "standard" = one agent, subagents forced to the
    # foreground; "workflow" = subagents may fan out in the background and the
    # runner collects their results. See src/agent/modes.py.
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="standard")
    # Empty = whatever the CLI defaults to for the connected account.
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # {branch, pr_url, compare_url, summary, turns, cost_usd?}
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Liveness: the runner heartbeats every ~30s. Startup orphan-marking only
    # touches rows with a STALE heartbeat, so a rolling deploy or second
    # replica can never kill another instance's live session.
    runner_instance: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # How many times this conversation has been picked back up. Kept because
    # "resumed 6 times over three days" is the difference between a session and
    # a long-running one, and neither the event log nor the transcript says it
    # plainly.
    resume_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    # After this, the stored transcript is swept and the session stops being
    # resumable. A conversation kept for ever is a conversation nobody pruned:
    # the transcripts are the largest thing we store per session.
    resumable_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        Index("ix_agent_sessions_ws", "workspace_id", "created_at"),
        Index("ix_agent_sessions_user", "user_id"),
    )

    @property
    def slugs(self) -> list[str]:
        """Every repo in the session, oldest rows included.

        Reading `repo_slugs` directly returns [] for anything created before
        the column existed, which would silently drop the repo from a finished
        session's own record.
        """
        stored = [s for s in (self.repo_slugs or []) if s]
        return stored or ([self.repo_slug] if self.repo_slug else [])


class AgentSessionTranscript(Base):
    """The CLI's own conversation transcript, mirrored out of the container.

    Distinct from `agent_session_events`, and the difference matters. Events
    are what a HUMAN reads on the session page — text, tool names, a result.
    This is what the CLI reads to RESUME: raw entries in the SDK's own shape,
    written through `ClaudeAgentOptions.session_store` and handed back verbatim
    on the next run. Neither can substitute for the other.

    It lives here rather than under CLAUDE_CONFIG_DIR because that directory is
    inside the api container and dies with every deploy — which is exactly the
    event a resumable session has to survive.
    """

    __tablename__ = "agent_session_transcripts"

    # Identity, and the ORDER. A conversation replayed out of order is not a
    # conversation, and a timestamp ties on a fast batch.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The SDK may split a session across sub-transcripts (subagents). Empty for
    # the main one.
    subpath: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    entry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        # Every read is "this session, this subpath, in order".
        Index("ix_agent_transcripts_session", "session_id", "subpath", "id"),
        # Retention sweeps by age across all sessions.
        Index("ix_agent_transcripts_created", "created_at"),
    )


class AgentSessionEvent(Base):
    """Append-only event log for an agent session. The BIGINT identity PK is
    the tail cursor: SSE replay selects `id > after` ordered by id, so replay
    + live tail can never drop or duplicate an event (unlike appending into a
    JSONB array, which loses concurrent writes)."""

    __tablename__ = "agent_session_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    # meta | text | tool_use | tool_result | result | error
    event: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (Index("ix_agent_events_session", "session_id", "id"),)


class PushSubscription(Base, TimestampMixin):
    """One browser (one device) that agreed to be told when work finishes.

    The point of the product on a phone is to start something and put the
    phone away — which no amount of reconnect logic can serve, because iOS
    stops a backgrounded tab regardless. A push subscription outlives the tab.

    `endpoint` is the push service URL and is globally unique per browser
    install; it is the natural key, so re-subscribing the same device updates
    the row instead of accumulating duplicates that would each deliver a copy
    of every notification.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Public key + auth secret from the browser's PushSubscription. Not our
    # secrets — without them the push service cannot decrypt for the device.
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    # Lets a person tell "iPhone" from "work laptop" when revoking one.
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # A push service reports a dead subscription with 404/410; we delete on
    # that. This counts the softer failures so a permanently broken endpoint
    # can be retired instead of retried forever.
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (Index("ix_push_subs_user", "user_id"),)


class ResourceSample(Base):
    """One resource/usage snapshot (default every 30s) — the raw material for
    sizing docs: RAM/CPU at time X, how many reviews/jobs/agent sessions ran
    in parallel, how many LLM calls went out in that interval."""

    __tablename__ = "resource_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    # Process (api container) + system
    cpu_pct: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    rss_mb: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    sys_mem_pct: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    load1: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Parallelism gauges at sample time
    reviews_running: Mapped[int] = mapped_column(nullable=False, server_default="0")
    jobs_running: Mapped[int] = mapped_column(nullable=False, server_default="0")
    jobs_pending: Mapped[int] = mapped_column(nullable=False, server_default="0")
    agent_sessions_running: Mapped[int] = mapped_column(nullable=False, server_default="0")
    # Deltas since the previous sample
    llm_calls: Mapped[int] = mapped_column(nullable=False, server_default="0")
    llm_tokens_in: Mapped[int] = mapped_column(nullable=False, server_default="0")
    llm_tokens_out: Mapped[int] = mapped_column(nullable=False, server_default="0")
    http_requests: Mapped[int] = mapped_column(nullable=False, server_default="0")

    __table_args__ = (Index("ix_resource_samples_ts", "ts"),)


class DepAuditRun(Base, TimestampMixin):
    """One dependency audit sweep over a workspace's registered repos."""

    __tablename__ = "dep_audit_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    # queued | running | done | error
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    # {repos_total, repos_scanned, repos_skipped:[], packages, outdated,
    #  vulnerable, by_severity:{critical,high,medium,low}}
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_dep_runs_ws", "workspace_id", "created_at"),)


class DepFinding(Base):
    """One package in one repo, as of one audit run — current vs latest vs
    known vulnerabilities (OSV)."""

    __tablename__ = "dep_findings"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_slug: Mapped[str] = mapped_column(Text, nullable=False)
    ecosystem: Mapped[str] = mapped_column(Text, nullable=False)   # npm|PyPI|Go|crates.io
    package: Mapped[str] = mapped_column(Text, nullable=False)
    current_version: Mapped[str] = mapped_column(Text, nullable=False)
    latest_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # none | patch | minor | major — how far behind latest
    outdated: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    is_dev: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())
    # [{id, severity, summary, fixed_in, url}] from OSV
    vulns: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    # worst severity across vulns: none|low|medium|high|critical
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="none")
    # update_now | update_safe | plan_major | ok
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, server_default="ok")
    #: Whether this repository's own code names the package, with up to five
    #: file:line sites. Three states — imported | not_found | unknown — because
    #: a PyPI package name does not determine its module name and reporting
    #: "not imported" for `beautifulsoup4` would be a silent zero.
    #:
    #: NOT reachability. See src/deps/imports.py for what that would require
    #: and why this installation cannot answer it. Nullable: a row written
    #: before the scan existed has no answer, which is not "no".
    named_in_code: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_dep_findings_run", "run_id", "severity"),
        Index("ix_dep_findings_repo", "run_id", "repo_slug"),
    )


class IncomingAlert(Base):
    """An alert ingested from monitoring (Grafana webhook or generic JSON).

    The ingest endpoint is unauthenticated but tenant-bound: the URL token
    embeds the workspace id and a secret half verified against the stored
    value, so an alert can only ever land in its own workspace."""

    __tablename__ = "incoming_alerts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="generic")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default="warning")
    # new | acked | fixed
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    repo_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ISO 639-1 code of the language the question was written in, as the
    #: model reported it. The canned parts of the reply are shown in it — they
    #: exist in sixteen languages so nothing pays a model to say them, and
    #: rendering them in the INTERFACE language answered a Ukrainian question
    #: with an English paragraph.
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (Index("ix_alerts_ws", "workspace_id", "created_at"),)


class WorkspaceInvite(Base):
    """Invitation to join a workspace — either addressed to an email or an
    open link. The raw token is shown once at creation; only its hash is
    stored."""

    __tablename__ = "workspace_invites"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # NULL → open link anyone with the URL may redeem.
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    max_uses: Mapped[int] = mapped_column(nullable=False, server_default="1")
    used_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=func.false())
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The issuer's user id — what accept resolves the granting authority by.
    # `created_by` (an email) stays for display and for rows that predate it.
    created_by_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (Index("ix_invite_workspace", "workspace_id"),)


class AccessRequest(Base):
    """A signed-in person asking for access to team workspaces in general.

    Not addressed to a workspace — the requester never learns which ones
    exist. The superadmin decides: approve with one or more (workspace, role)
    grants, applied atomically through `change_memberships`, or reject with a
    reason. `user_id` is a string reference into the SQLite user store, like
    every other user column here.

    status: pending | approved | rejected | cancelled. At most one PENDING row
    per user — the partial unique index enforces it against a double submit
    racing the 409 check in the router.
    """

    __tablename__ = "access_requests"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid_pk)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: [{workspace_id, workspace_name, role}] — written on approval only, so a
    #: pending or rejected request carries no workspace names at all.
    grants: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_access_requests_user", "user_id", "created_at"),
        Index("ix_access_requests_status", "status", "created_at"),
        Index(
            "uq_access_requests_one_pending", "user_id", unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )


class AutomationRun(Base, TimestampMixin):
    """One thing a person asked the Celmis agent to do.

    The page used to hold all of this in React state, so leaving it threw away
    the question, the plan and any record that the work had been started. The
    work itself survived — `/execute` queues jobs server-side and returns 202 —
    which made the loss worse rather than better: documentation would be
    generating for twenty repositories with nothing on screen to say so.

    A row is written when the sentence is READ, not when it is confirmed, so a
    plan that was never approved is still visible afterwards. That is
    deliberate: "what did I ask it, and did I press the button" is exactly the
    question somebody has when they come back to the page.
    """

    __tablename__ = "automation_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    #: Who asked. In a shared workspace a queued sweep over twenty repositories
    #: needs a name beside it.
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The sentence, verbatim. The plan is a reading of it and can be wrong;
    #: without the original there is no way to see that it was misread.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    #: The plan, as a list of steps. One sentence is often two jobs — arm
    #: review on a release branch and audit a feature branch — and a single
    #: action column could only ever record the half the model picked.
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    #: The queue job doing the reading. Kept so the reading can be stopped:
    #: that is the whole reason it is a job rather than a background task.
    job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Which conversation this belongs to: "the four things I asked on Tuesday
    #: while setting up the release" is one thread to a person and four
    #: unrelated rows to the database. It is also the agent's memory — the
    #: earlier settled rows of the same session, by the same person, in the
    #: same workspace, go with each new sentence (src.automation.memory), so
    #: "а для цього репо?" has something to refer to.
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ISO 639-1 code of the language the question was written in, as the
    #: model reported it. The canned parts of the reply are shown in it — they
    #: exist in sixteen languages so nothing pays a model to say them, and
    #: rendering them in the INTERFACE language answered a Ukrainian question
    #: with an English paragraph.
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    resolved_repos: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: The same sentence WHILE it is still being written, updated as the model
    #: streams it and left alone once `note` is final.
    #:
    #: It is a column rather than a socket on purpose. The reading takes a few
    #: seconds and used to show nothing at all until every part of the plan
    #: existed — the sentence is generated first now, so there is something to
    #: read almost immediately. Persisting it is what makes this better than
    #: ordinary chat streaming: close the page mid-sentence, come back, and
    #: the sentence is still there, because it was never only in flight.
    #:
    #: While `status` is "reading" this is the freshest text there is; once the
    #: run reaches a terminal status `note` is authoritative and this is the
    #: leftover of how it got there.
    partial_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: reading | planned | started | failed | stopped. There is no "done":
    #: what execute starts is a set of background jobs, and their state
    #: belongs to them, not here.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="planned")
    #: {queued: [{repo, job_id}], skipped: [{repo, reason}], run_id}
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (Index("ix_automation_runs_ws", "workspace_id", "created_at"),)
