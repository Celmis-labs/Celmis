"""Review-specific settings — isolated from the main Settings.

Environment vars:
    REVIEW_LOCAL_CACHE_DIR    — local snapshot cache (default ~/code-analysis/review-cache)
    REVIEW_S3_BUCKET          — optional S3 bucket for the cold tier (None → local-only)
    REVIEW_S3_PREFIX          — S3 key prefix (default 'snapshots/')
    REVIEW_HOT_CACHE_SIZE     — number of simultaneously warm repos (default 8)
    REVIEW_HOT_TTL_SECONDS    — idle eviction (default 600 = 10min)
    REVIEW_MAX_INLINE_COMMENTS — limit inline comments per review (default 20)
    REVIEW_SUPPRESSED_RULES   — JSON list of rule ids the prefilter hides (see the field)
    REVIEW_DEFECT_MODEL       — model for the defect agent (default 'gemini-3.6-flash')
    REVIEW_DEFECT_PASSES      — how many times defect reads the diff (default 2)
    REVIEW_TIMEOUT_SECONDS    — wall-clock budget for one review (default 900)
    REVIEW_LLM_TIMEOUT_SECONDS — deadline for one model call (default 300)
    REVIEW_VERIFIER_ENABLED   — run the LLM false-positive veto (default off)
    REVIEW_LLM_TIMEOUT_RETRY_FACTOR — how much longer a timeout's retry gets (2.0)
    REVIEW_CVE_LOOKUP_TIMEOUT_SECONDS — wall clock for the OSV sweep (120)
    REVIEW_MAX_DIFF_SIZE_BYTES — skip a review whose diff is larger (default 500000)
    REVIEW_CONTRACT_MODEL     — model for the contract agent (default 'gemini-3.6-flash')
    REVIEW_SECURITY_MODEL     — model for the security agent (default 'gemini-3.6-flash')
    REVIEW_VERIFIER_MODEL     — final FP filter (default 'gemini-3.6-flash')
    (REVIEW_ARCHITECT_MODEL / REVIEW_QUALITY_MODEL / REVIEW_TESTS_MODEL are the
     pre-restructure names; still honoured as fallbacks, with a warning)
    REVIEW_AGENT_MAX_OUTPUT_TOKENS      — reply ceiling for the review agents
    REVIEW_AGENT_CONCURRENCY  — simultaneous LLM agent calls per review (default 3)
    REVIEW_VERIFIER_MAX_OUTPUT_TOKENS   — same, for the verifier's short reply
    REVIEW_COMPLIANCE_MAX_OUTPUT_TOKENS — same, for one compliance verdict
    REVIEW_WEBHOOK_SECRET     — HMAC secret for GitHub/Bitbucket webhooks
    REVIEW_GITLAB_TOKEN       — plaintext token for GitLab webhooks
    REVIEW_REDIS_URL          — optional Redis for dedup + queue (None → in-memory)
    REVIEW_COMMENT_MARKER     — idempotent marker (default '<!-- code-analyzer:review -->')
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class ReviewSettings(BaseSettings):
    """PR review microservice configuration."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_prefix="REVIEW_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Lifecycle / caching ─────────────────────────────────────────
    local_cache_dir: Path = Path("~/code-analysis/review-cache").expanduser()
    hot_cache_size: int = 8
    hot_ttl_seconds: int = 600
    cold_snapshot_format: str = "rdb.zst"

    # ─── Cold tier (optional S3) ─────────────────────────────────────
    s3_bucket: str | None = None
    s3_prefix: str = "snapshots/"
    s3_region: str = "us-east-1"

    # ─── LLM models (per-agent) ──────────────────────────────────────
    #
    # Three finders since the Phase-18 restructure: defect (single-file, the
    # main finder), contract (cross-file, graph + drift), security. The old
    # architect/quality/tests trio is gone — see agents/__init__.py for the
    # measured reasons — and their env vars are honoured as fallbacks by the
    # validator below, so an install that pinned REVIEW_ARCHITECT_MODEL keeps
    # its pin on the agent that inherited the remit.
    #
    # All three finders default to the PRO tier now. quality/tests sat on
    # flash as "pattern-matching" agents; the defect agent that absorbed
    # their remit is where 45 of 46 missed bug-goldens lived, and SWR-Bench
    # (arXiv:2509.01494) measured what a stronger model buys on this exact
    # task: identical precision, 67% more recall. Recall is what F2 pays for.
    #
    # `gemini-3-pro` DOES NOT EXIST — the previous defaults named it, and on
    # a fresh install three agents failed TERMINAL on every review while every
    # CONFIGURED workspace silently ran its own override and never saw it.
    # Verified: Google's model list has no gemini-3-pro and no
    # gemini-3-pro-preview (which IS in litellm's table — a name being in a
    # capability table and a name existing at the provider are two different
    # facts). The test beside this walks every `*_model` field and refuses
    # both spellings.
    #: How many times the defect agent reads the same diff.
    #:
    #: MEASURED, and the number replaces a hypothesis that failed. Folding
    #: three finder agents into one halved the findings (4.08 → 2.08 per PR)
    #: while raising precision per claim, and the first explanation — that one
    #: answer has a natural list length, so the agent was seeing more than it
    #: wrote — was tested by removing every restraint from the prompt and
    #: adding a per-line sweep. That bought +0.35 findings per PR, 0 new true
    #: positives and 3 false ones. The agent was writing what it saw.
    #:
    #: What is left is SAMPLING. The same benchmark had already measured that
    #: 43 of 43 findings lost between two runs of identical code were "this
    #: time it did not write it", not "wrote it badly" — per-finding variance,
    #: not per-finding quality. Three agents were three draws on an
    #: overlapping pool; one agent is one draw. Arithmetic on the run data:
    #: architect+quality+tests took 66 true positives, defect alone takes 45,
    #: so one draw recovers p ≈ 0.68 of what three took, and 1-(1-p)² ≈ 0.90 —
    #: a second draw should recover most of the gap.
    #:
    #: 2, not 3: the second draw is predicted to close 0.68 → 0.90 and the
    #: third only 0.90 → 0.97, for the same price. Set to 1 to turn the second
    #: look off (REVIEW_DEFECT_PASSES=1) — it costs one LLM call per review,
    #: which is exactly the call the five-to-three merge freed.
    defect_passes: int = 2

    #: The model a FRESH INSTALL reviews with.
    #:
    #: FLASH, NOT PRO, AND THE ARGUMENT ABOVE IS WHY IT HAD TO BE MEASURED
    #: RATHER THAN INHERITED. Everything said there about a stronger model is
    #: still true: SWR-Bench measured identical precision and 67% more recall
    #: on this exact task, and recall is what F2 pays for. It just does not
    #: decide this, because it assumes the call comes back.
    #:
    #: ONE REVIEW MAKES THREE CONCURRENT CALLS — defect, contract and security
    #: run in parallel — and the shipped `CELMIS_SYNC_WORKER_CONCURRENCY` is 2,
    #: so two overlapping reviews make six. Measured against Google directly,
    #: same prompt, same day:
    #:
    #:     gemini-3.1-pro-preview   3 concurrent → 3/3, slowest 51.6s
    #:                              4 concurrent → 3/4, one HTTP 503
    #:     gemini-3.6-flash         3 concurrent → 3/3, slowest 10.0s
    #:                              4 concurrent → 4/4, slowest 11.1s
    #:
    #: 51.6 seconds at the concurrency ONE review creates, and a refusal one
    #: step past it. A person who installs this and opens a pull request meets
    #: that before they see any recall at all — and the fallback field that
    #: masked it on our own benchmark install does not exist in a fresh one.
    #:
    #: The recall is still there for whoever wants it:
    #: `REVIEW_DEFECT_MODEL=gemini-3.1-pro-preview` and lower
    #: `REVIEW_AGENT_CONCURRENCY` to 2, which is the pairing the numbers above
    #: say is needed. Shipping it as the default means shipping the pairing,
    #: and one of the two would have been forgotten.
    defect_model: str = "gemini-3.6-flash"
    contract_model: str = "gemini-3.6-flash"
    security_model: str = "gemini-3.6-flash"
    verifier_model: str = "gemini-3.6-flash"

    # ─── Review limits ───────────────────────────────────────────────
    #: Стеля відповіді агента. 4096 було розраховано на модель без reasoning;
    #: у Gemini 3.x токени думання рахуються в ТОЙ САМИЙ бюджет і з'їдають
    #: його майже цілком — заміряно 572 reasoning на 613 вихідних. Architect
    #: у 20 з 28 викликів упирався в стелю, і обрізаний JSON приходив як
    #: "no JSON array in the reply", найчастіша причина падінь агента.
    #: Модель тримає 65 536; беремо чверть, щоб думання мало де розміститись,
    #: а масив знахідок не обривався на півслові.
    #: Sampling temperature for the review agents. Low on purpose: a reviewer
    #: that answers differently each time it reads the same diff is not a
    #: reviewer. Settable because not every model takes the same range.
    agent_temperature: float = 0.1

    agent_max_output_tokens: int = 16384

    #: Дві короткі структуровані відповіді, яким та сама стеля не потрібна.
    #: Verifier повертає {"keep": [...]} — список індексів; compliance повертає
    #: {"passes": bool, "reason": "одне речення"}. Обидва числа стояли
    #: захардкодженими у verifier.py і compliance.py; вони не помилка, вони
    #: рішення — але як літерали їх не можна було ні побачити, ні підняти.
    #: Тепер це ПІДЛОГА ланцюжка успадкування: per-agent налаштування підніме
    #: її, коли модель почне витрачати вихідний бюджет на думання (compliance
    #: на 256 — це рівно та сама пастка, з якої architect вибирався).
    #: 1024 was chosen when the verifier returned a short list of indices and
    #: nothing else. Measured on a 50-PR benchmark run: 128 of 144 verifier
    #: calls (89%) hit that ceiling, so the agent that decides WHICH findings
    #: survive was cut off mid-answer nine times out of ten — the same trap
    #: architect climbed out of when its ceiling went from 4096. A ceiling is
    #: a bound, not a target: a verifier that needs 400 tokens still spends
    #: 400.
    verifier_max_output_tokens: int = 32768
    #: Left small deliberately: compliance answers {"passes": bool, "reason":
    #: "one sentence"} and has never been measured against this bound. Raise it
    #: the day a measurement says it truncates, not before.
    compliance_max_output_tokens: int = 256

    #: How many agents may hold a provider connection at once, per review.
    #: The executor used to be sized len(agents): six agents, six simultaneous
    #: provider connections per PR — benchmarked as the source of ConnectError
    #: on a weak uplink (5 of the 9 agent failures in one run) and of 503s
    #: from Gemini. Three is half the roster; the calls are I/O-bound and
    #: long, so the wall-clock cost of queueing is small next to the cost of
    #: an agent dying on its own connection. Counts LLM agents only — the
    #: deterministic ones open no provider connection and run outside the
    #: pool (see `ReviewOrchestrator._run_agents_parallel`).
    agent_concurrency: int = 3

    max_inline_comments: int = 20
    #: Rule ids the deterministic prefilter drops before anything else sees
    #: them. Measured on the Martian bench with the LLM veto OFF (14 PRs):
    #: these six produced 6-7 false positives and not one true positive —
    #: "add a test", "resolve this TODO", "annotate the type", "this number
    #: should be a constant" are observations a judge never rewards and a
    #: reader stops reading after. A set in CODE rather than a line in a
    #: prompt ("avoid these categories"), because a prompt is followed most of
    #: the time and a gate every time; Kodus keeps its gates deterministic
    #: for the same reason. A repo policy may replace the whole set
    #: (`repo_review_policies.suppressed_rules`, NULL = this default, [] =
    #: hide nothing), and every drop is counted on the batch by rule
    #: (`ReviewBatch.dropped_by_rule`) so a run can say what it hid.
    #: Env: REVIEW_SUPPRESSED_RULES, a JSON list like the skip patterns below.
    #: Both spellings of each banned category: the old-prefix ids (rows and
    #: policies written before the Phase-18 restructure still name them) and
    #: the defect.* ids the merged agent would emit today. One category, two
    #: spellings — dropping the old ones would un-ban them for any replayed
    #: or re-judged historical run.
    suppressed_rules: frozenset[str] = frozenset({
        "tests.no-coverage",
        "quality.todo",
        "quality.typing",
        "quality.duplication",
        "quality.maintainability",
        "quality.magic_numbers",
        "defect.no-coverage",
        "defect.todo",
        "defect.typing",
        "defect.duplication",
        "defect.maintainability",
        "defect.magic_numbers",
    })
    max_diff_size_bytes: int = 500_000  # 500 KB — skip review if larger
    max_files_reviewed: int = 100
    max_hunk_lines: int = 500

    # ─── Comments idempotency ───────────────────────────────────────
    comment_marker: str = "<!-- code-analyzer:review -->"
    replace_on_synchronize: bool = True

    # ─── Webhooks ────────────────────────────────────────────────────
    webhook_secret: SecretStr | None = None
    gitlab_token: SecretStr | None = None
    bitbucket_secret: SecretStr | None = None

    # ─── Async / queue ───────────────────────────────────────────────
    redis_url: str | None = None  # 'redis://localhost:6379/0' — optional
    #: Total wall-clock budget for one PR review, in seconds.
    #:
    #: TWO THINGS WERE WRONG WITH IT AND EACH HID THE OTHER.
    #:
    #: The name doubled the prefix. `env_prefix = "REVIEW_"` plus a field
    #: called `review_timeout_seconds` reads REVIEW_REVIEW_TIMEOUT_SECONDS,
    #: while REVIEW_TIMEOUT_SECONDS — the one anybody would actually write —
    #: was read by nothing and ignored in silence. It is the only field in
    #: this class that carried the class's own prefix; the rename makes the
    #: env var the obvious one, and `_legacy_env_bridge` keeps the doubled
    #: spelling working for an install that found it.
    #:
    #: And NOTHING ENFORCED IT. A setting named "total budget per PR review",
    #: read by no code path, promising a bound the product does not apply.
    #:
    #: 900, not 300, and the number is measured rather than chosen. Across 175
    #: real reviews from seven benchmark runs: median 85s, p90 341s, p99
    #: 1438s. A 300-second deadline would have cut 14.3% of them; 900 cuts
    #: 2.3%, which is the tail that genuinely hangs. Enforcing the old default
    #: as written would have truncated one review in seven.
    timeout_seconds: int = 900

    #: Wall-clock deadline for ONE call to a model, in seconds.
    #:
    #: It was 120, as the default of a keyword argument in
    #: `LLMClient.generate` that no review agent ever passed — so no operator
    #: could change it, and nothing in the product named it. Reasoning models
    #: routinely think for longer than two minutes on a large diff: sixteen
    #: agent failures in eight hours on the benchmark install were all this
    #: deadline, and because a timeout was classified as
    #: `provider_unavailable`, the run of failures read as a provider outage.
    #:
    #: 300 is chosen against the only measurement available. Over 517 real
    #: reviews the WHOLE review has median 74s and p90 328s — and a review
    #: runs its finders concurrently, so one call taking longer than the p90
    #: of an entire review is anomalous rather than merely slow. The
    #: transient retry means a genuinely stuck call still costs 600s, which
    #: is why this must stay well under `timeout_seconds`.
    #:
    #: No single number is right for every model, which is the actual fix
    #: here: this one has a name and can be raised.
    llm_timeout_seconds: int = 300

    #: How much longer the SECOND attempt gets when the first died on the
    #: deadline. 2.0 doubles it; 1.0 turns the widening off.
    #:
    #: A number without a name is the defect this file spent a day removing,
    #: and `_llm_timeout() * 2` was one — written the same afternoon, in the
    #: same repository, three commits after the argument for why 120 being
    #: unreachable was the whole problem. The multiplier decides whether a
    #: slow model gets a real second chance or a second guaranteed failure,
    #: which is exactly the kind of decision an operator running a slow model
    #: needs to be able to make.
    #:
    #: `llm_timeout_seconds * (1 + this)` is the worst case for one agent, so
    #: raising it eats into `timeout_seconds` — a test pins that the three
    #: numbers still fit together.
    llm_timeout_retry_factor: float = 2.0

    #: Wall clock for the CVE agent's whole vulnerability lookup.
    #:
    #: It was a module constant in `src/review/agents/cve.py`, which made it
    #: unreachable for the operator most likely to need it: a monorepo whose
    #: lockfiles run to thousands of packages sweeps more batches than a
    #: small repository, and 120 seconds is a judgement about lockfile size
    #: that only one installation can make.
    cve_lookup_timeout_seconds: float = 120.0

    #: Whether the LLM false-positive veto runs when a repository has not said.
    #:
    #: OFF, and that is the product's answer rather than a placeholder. The
    #: veto is a second model call over every finding in the review — the
    #: slowest single call the pipeline makes — and whether it earns its price
    #: is a judgement about one repository's tolerance for noise. Charging it
    #: to every installation by default is not that judgement.
    #:
    #: It shipped ON, and not by decision: the only way to switch it off was to
    #: name "verifier" in a policy's `disabled_agents`, and an unconfigured
    #: repository names nothing. A default that exists only because its
    #: opposite was unsayable is not a default.
    #:
    #: The deterministic prefilter — exact dedup, near-duplicate clustering,
    #: the rule deny-list, the confidence floor, the severity sort — is NOT
    #: this and always runs. Only the model's veto is off.
    verifier_enabled: bool = False

    # ─── Skip patterns ──────────────────────────────────────────────
    skip_filename_patterns: list[str] = Field(
        default_factory=lambda: [
            "*.lock", "package-lock.json", "yarn.lock", "Cargo.lock", "go.sum",
            "poetry.lock", "Pipfile.lock", "composer.lock", "pnpm-lock.yaml",
            "*.min.js", "*.min.css", "*.bundle.js",
            "*.generated.*", "*_pb2.py", "*_pb2_grpc.py", "*.pb.go",
            "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp",
            "*.woff", "*.woff2", "*.ttf", "*.eot",
            "*.pdf", "*.zip", "*.tar.gz", "*.exe", "*.dll", "*.so",
        ]
    )
    skip_directory_patterns: list[str] = Field(
        default_factory=lambda: [
            "node_modules", "vendor", "dist", "build", "out",
            ".next", ".nuxt", "target", "__pycache__",
            ".idea", ".vscode", "coverage",
        ]
    )

    @property
    def has_s3(self) -> bool:
        return self.s3_bucket is not None and self.s3_bucket.strip() != ""

    @property
    def has_redis(self) -> bool:
        return self.redis_url is not None and self.redis_url.strip() != ""

    def ensure_directories(self) -> None:
        self.local_cache_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_review_settings() -> ReviewSettings:
    settings = ReviewSettings()
    _legacy_env_bridge(settings)
    return settings


def _legacy_env_bridge(settings: ReviewSettings) -> None:
    """Honour the pre-restructure model env vars, once per process.

    REVIEW_ARCHITECT_MODEL / REVIEW_QUALITY_MODEL stopped being field names
    when the agents were restructured, and pydantic reads only declared
    fields — so an install that pinned one would silently lose its pin, which
    is the exact bug class `test_a_configured_reasoning_setting_survives_the_
    save` exists for at another layer. The mapping follows the remit:
    architect's went to contract, quality's to defect. The NEW var wins when
    both are set. REVIEW_TESTS_MODEL maps nowhere — two legacy vars cannot
    share one field, quality is the closer bulk-role ancestor — so it is
    logged and ignored rather than silently dropped.
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    for legacy_var, field, new_var in (
        ("REVIEW_ARCHITECT_MODEL", "contract_model", "REVIEW_CONTRACT_MODEL"),
        ("REVIEW_QUALITY_MODEL", "defect_model", "REVIEW_DEFECT_MODEL"),
    ):
        value = (os.environ.get(legacy_var) or "").strip()
        if value and not (os.environ.get(new_var) or "").strip():
            object.__setattr__(settings, field, value)
            log.warning("%s is the pre-restructure name — applied to %s; "
                        "rename it to %s", legacy_var, field, new_var)

    # The doubled-prefix spelling. `review_timeout_seconds` under
    # `env_prefix="REVIEW_"` meant the variable was REVIEW_REVIEW_TIMEOUT_
    # SECONDS; anyone who found that and set it keeps it working.
    doubled = (os.environ.get("REVIEW_REVIEW_TIMEOUT_SECONDS") or "").strip()
    if doubled and not (os.environ.get("REVIEW_TIMEOUT_SECONDS") or "").strip():
        try:
            object.__setattr__(settings, "timeout_seconds", int(doubled))
            log.warning("REVIEW_REVIEW_TIMEOUT_SECONDS is the doubled-prefix "
                        "spelling — applied; rename it to "
                        "REVIEW_TIMEOUT_SECONDS")
        except ValueError:
            log.warning("REVIEW_REVIEW_TIMEOUT_SECONDS=%r is not a number",
                        doubled)
    if (os.environ.get("REVIEW_TESTS_MODEL") or "").strip():
        log.warning("REVIEW_TESTS_MODEL is ignored: the tests agent merged "
                    "into defect, whose model REVIEW_DEFECT_MODEL sets")


# ─── Per-agent LLM configuration ─────────────────────────────────────
#
# The model has been per-agent since Stage 11; the output ceiling and the
# reasoning level were not, and the second of the two reached nothing at all —
# `Settings.gemini_thinking_budget` was wired only into the native
# `gemini_client.py`, so on every LiteLLM call the setting existed in the UI
# and changed no request. This is the resolver both of them now go through,
# deliberately the SAME mechanism the model already used rather than a
# parallel one beside it.
#
# Inheritance order, highest first:
#
#   1. Repo policy — the flat `<agent>_model` columns for the MODEL, and the
#      per-agent `agents` entry (`repo_review_policies.agent_llm_overrides`)
#      for the ceiling and the reasoning. Never both for one field: the entry
#      carries no `model` key, and the API that writes it refuses one.
#   2. Workspace `agents` entry — {architect: {model?, max_output_tokens?,
#      reasoning?}, …} inside the existing workspace JSON blob.
#   3. Workspace surface selection — the `model` the workspace picked for every
#      agent at once, on /settings/llm.
#   4. ReviewSettings — the floor. Never None, so the call always has a number.
#
# Every field is optional at every layer; absent means inherit.
#
# Layer 3 is the MODEL and only the model, and that is deliberate. The same
# workspace blob has carried a top-level `max_output_tokens` since long before
# this resolver existed: PUT /api/llm/config writes it on every save with a
# default of 4096, and no completion has ever read it. Reading it here would
# have made it live retroactively — and 4096 is precisely the ceiling that
# failed the architect agent in 43% of runs, because Gemini 3.x spends most of
# an output budget thinking before it answers. Every workspace that had ever
# opened the settings page would have been silently returned to it, with
# nobody having changed anything. A workspace-wide REVIEW ceiling, if it is
# ever wanted, needs a key of its own that no old blob already holds a number
# in. `tests/review/test_each_agent_gets_its_own_ceiling.py` pins this.

#: The agents a person may configure. `structural` and `breaking_change` are
#: absent on purpose: they are deterministic and never call a model, so an
#: output ceiling for them would be a control that does nothing.
REVIEW_AGENTS: tuple[str, ...] = (
    "defect", "contract", "security", "verifier", "compliance",
)

#: Pre-restructure agent names → who inherited the remit. Consulted where a
#: stored configuration may predate the restructure: the legacy env vars in
#: `_legacy_env_bridge`, the repo policy's flat `<agent>_model` columns in
#: `resolve_agent_llm`, and nowhere else. `tests` maps to defect because its
#: remit (the untested-branch clause) lives there now; a policy that DISABLED
#: `tests` is deliberately NOT mapped — disabling the old sidecar must not
#: disable the main finder (`review_policies` logs and ignores unknown names).
LEGACY_AGENT_NAMES: dict[str, str] = {
    "architect": "contract",
    "quality": "defect",
    "tests": "defect",
}


@dataclass(frozen=True)
class AgentLLMSettings:
    """One agent's resolved LLM knobs.

    One object rather than three parallel maps because the three travel
    together everywhere they go — and because `_generate_and_parse` doubles
    `max_output_tokens` on its corrective retry, so the code that owns the
    number has to be able to see the model it belongs to.
    """

    model: str | None = None
    max_output_tokens: int | None = None
    #: A word ("low"/"high") or a token budget, depending on what the model
    #: takes. Not normalised here: `src.llm.capabilities` asks the installed
    #: LiteLLM which of the two this model accepts, and a vocabulary decided
    #: here would be the stale per-vendor table that module exists to avoid.
    reasoning: str | int | None = None
    #: Sampling temperature for this agent. Review wants a low one — the same
    #: diff read twice should not produce two different findings — but the
    #: number was a literal 0.1 in two call sites, so a model that refuses that
    #: value could not be accommodated without editing code. Some models accept
    #: only their own default (claude-sonnet-5 takes temperature=1 and 400s on
    #: anything else); for those the call drops the parameter entirely rather
    #: than sending a value equal to the default, which they refuse too.
    temperature: float | None = None
    #: The model to try ONCE when the primary has exhausted its attempts on a
    #: THROTTLED/TRANSIENT failure — a same-vendor sibling is the measured
    #: remedy (gemini-3.7-flash refused 40% of calls in a benchmark window
    #: where gemini-3.6-flash refused none). None means fail as before. A
    #: terminal failure never engages it: a bad key or an unknown model fails
    #: identically on any model, and a fallback that "fixed" it would be
    #: masking a configuration mistake.
    fallback_model: str | None = None


def default_agent_model(agent: str, settings: ReviewSettings | None = None) -> str:
    """The floor of the model chain for `agent`.

    `compliance` has no env default of its own and falls back to the contract
    model (it used to borrow architect's, whose remit contract inherited).
    """
    s = settings or get_review_settings()
    return str(getattr(s, f"{agent}_model", None) or s.contract_model)


def default_agent_max_output_tokens(
    agent: str, settings: ReviewSettings | None = None,
) -> int:
    """The floor of the output-ceiling chain for `agent`.

    Verifier and compliance have their own, much smaller, floors — see the
    comment on those fields. Everything else shares `agent_max_output_tokens`.
    """
    s = settings or get_review_settings()
    return int(
        getattr(s, f"{agent}_max_output_tokens", None) or s.agent_max_output_tokens
    )


def _entry(cfg: dict | None, agent: str) -> dict:
    """The `agents.<name>` sub-dict of a config blob, or an empty one.

    Tolerant by design: this reads a JSON blob a person edits through the UI,
    and a malformed corner of it must cost that corner's override, not the
    review.
    """
    blob = (cfg or {}).get("agents")
    if not isinstance(blob, dict):
        return {}
    entry = blob.get(agent)
    return entry if isinstance(entry, dict) else {}


def _first_str(*candidates: Any) -> str | None:
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_positive_int(*candidates: Any) -> int | None:
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            # A layer that holds nonsense is skipped rather than fatal — the
            # next layer down is a working answer, and refusing the whole
            # review over one bad field would be the expensive kind of strict.
            continue
        if number > 0:
            return number
    return None


def _first_reasoning(*candidates: Any) -> str | int | None:
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_temperature(*values: object) -> float | None:
    """First usable temperature in the chain. 0.0 is a legitimate value — the
    most deterministic one there is — so `or` cannot be used to walk this
    chain, which is the bug the sibling helpers were written to avoid.
    """
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 2.0:
            return f
    return None


def resolve_agent_llm(
    agent: str,
    *,
    policy: dict | None = None,
    workspace_cfg: dict | None = None,
    settings: ReviewSettings | None = None,
) -> AgentLLMSettings:
    """Walk the inheritance chain for one agent. Always returns a number.

    `max_output_tokens` is never None on the way out: the floor is applied
    here so there is exactly ONE place that knows an agent's default ceiling.
    `LLMReviewAgent._generate_and_parse` still falls back to
    `agent_max_output_tokens` when it is handed None, but that path is now
    only for a caller who never went through this resolver at all.
    """
    s = settings or get_review_settings()
    policy = policy or {}
    workspace_cfg = workspace_cfg or {}
    policy_entry = _entry(policy, agent)
    ws_entry = _entry(workspace_cfg, agent)

    # Legacy repo-policy columns. The table still carries architect_model /
    # quality_model / tests_model from before the restructure; a repo that
    # pinned one keeps its pin on the agent that inherited the remit. The
    # NEW-name key wins when both are somehow present.
    legacy_column = next(
        (f"{old}_model" for old, new_ in LEGACY_AGENT_NAMES.items()
         if new_ == agent), None)

    return AgentLLMSettings(
        model=_first_str(
            # The repo layer's model is the `<agent>_model` COLUMN and nothing
            # else. `policy_entry` used to be consulted here too, in
            # anticipation of a repo-level blob that might carry one — and the
            # day that blob landed it would have been a second home for one
            # field, silently outranked by the column. That is the shape of
            # every model bug this file has already been through, so the repo
            # blob carries the ceiling and the reasoning only, and
            # `src.api.routers.review_policies` refuses a `model` key in it.
            policy.get(f"{agent}_model"),
            policy.get(legacy_column) if legacy_column else None,
            ws_entry.get("model"),
            workspace_cfg.get("model"),
            default_agent_model(agent, s),
        ),
        # Deliberately NOT `workspace_cfg["max_output_tokens"]` — see the note
        # on layer 3 above. That key is the legacy 4096 nothing ever read.
        max_output_tokens=_first_positive_int(
            policy_entry.get("max_output_tokens"),
            ws_entry.get("max_output_tokens"),
            default_agent_max_output_tokens(agent, s),
        ),
        reasoning=_first_reasoning(
            policy_entry.get("reasoning"),
            ws_entry.get("reasoning"),
        ),
        temperature=_first_temperature(
            policy_entry.get("temperature"),
            ws_entry.get("temperature"),
            s.agent_temperature,
        ),
        # Workspace level ONLY, for now — deliberately not read from the
        # policy blob or the per-agent entries. One layer means there is
        # exactly one place a surprising fallback call can have come from;
        # the policy layer can inherit a slot in this chain the day a repo
        # needs its own, the way the ceiling and the reasoning already do.
        fallback_model=_first_str(workspace_cfg.get("review_fallback_model")),
    )
